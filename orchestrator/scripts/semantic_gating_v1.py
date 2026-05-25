"""Hierarchical Semantic Gating v1 — Priorities 1+2+3 production pipeline.

ARCHITECTURE:
  Raster Bridge primitives (17K/page)
    -> Priority 1: Candidate Metadata Zone Detector (cheap structural, NO OCR)
    -> Priority 2: Region-Scoped OCR (PaddleOCR INSIDE candidate bboxes only)
    -> Priority 3: OCR-Affinity Pruning (preserve near semantic candidates)
    -> PRG (unchanged, modality-blind contract)

CRITICAL: PRG never modified. Pruner replaces Build 2 v1's global geometric scoring
with locality-to-semantically-valid-OCR-regions scoring.

Positive lexical signals (title-block presence):
  SHEET, DRAWN BY, PROJECT, REVISION, REV, SCALE, DATE, CHECKED, DESIGNED,
  DRAWING, sheet ID patterns (A-101, FP-2, E-3, S-2.1)

Negative lexical signals (NOT title block):
  ROOM + number, dimension chains (X'-Y\"), schedules (EQUIPMENT/DOOR/PANEL),
  repeated tiny numerics, keynote clouds
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
import warnings
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

warnings.filterwarnings("ignore")

try:
    import pymupdf as fitz
except ImportError:
    import fitz

sys.path.insert(0, str(Path(__file__).parent))
from raster_bridge_v1 import reconstruct_page, RASTER_CACHE, PDF_NATIVE_DPI
from prg_v29 import (
    run_prg, swap_regions, pick_alternate_quadrant,
    bbox_iou, classify_trajectory,
)

OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")


# ============================================================
# Priority 1 — Candidate Metadata Zone Detector
# ============================================================

POSITIVE_KEYWORDS = {
    "SHEET": 2.0, "SH": 0.8, "SHT": 0.8,
    "DRAWN BY": 1.5, "DRAWN": 1.0, "DRWN": 1.0,
    "PROJECT": 1.5, "PROJ": 1.0,
    "REVISION": 1.5, "REV": 1.2, "REVISIONS": 1.5,
    "SCALE": 1.0, "SCL": 0.7,
    "DATE": 0.7, "DT": 0.4,
    "CHECKED": 1.0, "CHKD": 0.8,
    "DESIGNED": 1.0, "DSGN": 0.8,
    "APPROVED": 1.0, "APPR": 0.8,
    "DRAWING": 1.2, "DWG": 1.0,
    "NUMBER": 0.5, "NO": 0.3,
    "TITLE": 1.2, "OF": 0.2,
    "ARCHITECT": 1.3, "ENGINEER": 1.3, "CLIENT": 1.0,
    "OWNER": 1.0, "DESIGNER": 1.2,
}

NEGATIVE_KEYWORDS = {
    "ROOM": -0.8, "RM": -0.6,
    "EQUIPMENT": -1.0, "SCHEDULE": -1.5,
    "DOOR": -0.8, "WINDOW": -0.8, "PANEL": -0.8,
    "KEYNOTE": -1.0, "NOTES": -0.5,
    "DETAIL": -0.5, "SECTION": -0.5,
    "ELEVATION": -0.5, "PLAN": -0.3,
    "TYPICAL": -0.5, "TYP": -0.4,
    "FIXTURE": -0.7, "OUTLET": -0.6,
}

# Patterns
SHEET_ID_RE = re.compile(r"\b[ACEFMPS]-?\d{1,3}(?:\.\d{1,2})?\b", re.IGNORECASE)
DIMENSION_RE = re.compile(r"\b\d{1,3}['-]\d{1,3}\"?\b")
ROOM_NUM_RE = re.compile(r"\b\d{2,4}\b")


def detect_candidate_zones(primitives: list[dict],
                            page_w_pt: float, page_h_pt: float,
                            n_candidates: int = 8) -> list[dict]:
    """Cheap structural heuristics identify metadata zone candidates.
    No OCR yet. Output: bboxes with structural_score for downstream OCR gating.
    """
    if not primitives:
        return []

    # Compute short-line density grid (microcell-density proxy)
    GRID_SIZE = 60.0  # ~0.83 inch
    grid_w = max(1, int(math.ceil(page_w_pt / GRID_SIZE)))
    grid_h = max(1, int(math.ceil(page_h_pt / GRID_SIZE)))
    short_density = np.zeros((grid_h, grid_w), dtype=np.float32)
    line_density = np.zeros((grid_h, grid_w), dtype=np.float32)
    ortho_density = np.zeros((grid_h, grid_w), dtype=np.float32)

    for p in primitives:
        if p.get("primitive_type") != "line":
            continue
        try:
            cx = float(p["cx"])
            cy = float(p["cy"])
            length = float(p.get("length", 0))
            dx = float(p["x1"]) - float(p["x0"])
            dy = float(p["y1"]) - float(p["y0"])
        except (KeyError, TypeError, ValueError):
            continue
        gx = min(grid_w - 1, max(0, int(cx // GRID_SIZE)))
        gy = min(grid_h - 1, max(0, int(cy // GRID_SIZE)))
        line_density[gy, gx] += 1
        if length < 25:
            short_density[gy, gx] += 1
        if abs(dx) < 1e-6 or abs(dy) < 1e-6:
            ortho_density[gy, gx] += 1
        else:
            angle = math.degrees(math.atan2(dy, dx)) % 180
            if angle <= 5 or angle >= 175 or abs(angle - 90) <= 5:
                ortho_density[gy, gx] += 1

    # Bottom-right Gaussian prior (title blocks bias)
    br_prior = np.zeros_like(short_density)
    for gy in range(grid_h):
        for gx in range(grid_w):
            rx = (gx + 0.5) / grid_w
            ry = (gy + 0.5) / grid_h
            # Distance from bottom-right corner (1.0, 1.0)
            d = math.hypot(1 - rx, 1 - ry)
            br_prior[gy, gx] = math.exp(-d * 2.5)  # Gaussian centered at BR

    # Composite structural score per grid cell
    # Normalize each component
    def safe_norm(arr):
        m = arr.max()
        return arr / m if m > 0 else arr
    score_grid = (
        2.5 * safe_norm(short_density)        # microcell density (dominant)
        + 1.0 * safe_norm(ortho_density)      # orthogonal structure
        + 0.5 * safe_norm(line_density)       # general density
        + 1.5 * br_prior                      # bottom-right bias
    )

    # Find top-N regions via non-maximum suppression on score_grid
    candidates = []
    score_copy = score_grid.copy()
    for _ in range(n_candidates):
        flat_idx = int(np.argmax(score_copy))
        gy, gx = divmod(flat_idx, grid_w)
        peak_score = float(score_copy[gy, gx])
        if peak_score < 0.5:
            break
        # Grow region around peak by including high-score neighbors
        # Simple: 3x3 grid window around peak
        gy0 = max(0, gy - 2)
        gy1 = min(grid_h, gy + 3)
        gx0 = max(0, gx - 2)
        gx1 = min(grid_w, gx + 3)
        # bbox in PDF pt
        x0_pt = gx0 * GRID_SIZE
        y0_pt = gy0 * GRID_SIZE
        x1_pt = min(page_w_pt, gx1 * GRID_SIZE)
        y1_pt = min(page_h_pt, gy1 * GRID_SIZE)
        candidates.append({
            "bbox_pt": [x0_pt, y0_pt, x1_pt, y1_pt],
            "structural_score": round(peak_score, 3),
            "short_line_count": int(short_density[gy0:gy1, gx0:gx1].sum()),
            "ortho_line_count": int(ortho_density[gy0:gy1, gx0:gx1].sum()),
            "br_prior": round(float(br_prior[gy, gx]), 3),
        })
        # Suppress this region for next iteration
        score_copy[gy0:gy1, gx0:gx1] = 0
    return candidates


# ============================================================
# Priority 2 — Region-Scoped OCR + Semantic Keyword Scoring
# ============================================================

class _OCRCache:
    """Singleton wrapper so we initialize PaddleOCR once."""
    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            from paddleocr import PaddleOCR
            print("    initializing PaddleOCR (one-time, ~10s)...", flush=True)
            # CRITICAL: enable_mkldnn=False bypasses Paddle 3.3.1 Windows oneDNN crash
            # (NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support
            # [pir::ArrayAttribute<pir::DoubleAttribute>])
            # PP-OCRv4 mobile is fast + stable; PP-OCRv5 server has same bug
            cls._instance = PaddleOCR(
                lang="en",
                ocr_version="PP-OCRv4",
                enable_mkldnn=False,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        return cls._instance


def ocr_region(image: np.ndarray, bbox_pt: list[float],
                 dpi: float, page_h_px: int) -> list[dict]:
    """Run PaddleOCR on a cropped region. bbox_pt is in PDF pt; image is raster pixels."""
    scale = dpi / PDF_NATIVE_DPI
    x0_px = max(0, int(bbox_pt[0] * scale))
    y0_px = max(0, int(bbox_pt[1] * scale))
    x1_px = min(image.shape[1], int(bbox_pt[2] * scale))
    y1_px = min(image.shape[0], int(bbox_pt[3] * scale))
    if x1_px - x0_px < 20 or y1_px - y0_px < 20:
        return []
    crop = image[y0_px:y1_px, x0_px:x1_px]
    if crop.size == 0:
        return []
    if len(crop.shape) == 2:
        crop_bgr = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)
    else:
        crop_bgr = crop
    ocr = _OCRCache.get()
    try:
        result = ocr.predict(crop_bgr)
    except Exception as exc:
        return [{"_ocr_error": str(exc)[:100]}]
    spans = []
    if not result:
        return []
    # PaddleOCR v3+ returns list of dicts with rec_texts/rec_scores
    for page_result in result:
        if isinstance(page_result, dict):
            texts = page_result.get("rec_texts", [])
            scores = page_result.get("rec_scores", [])
            boxes = page_result.get("rec_polys") or page_result.get("dt_polys", [])
            for i, txt in enumerate(texts):
                conf = scores[i] if i < len(scores) else 0.0
                spans.append({
                    "text": txt,
                    "confidence": float(conf),
                })
    return spans


def score_region_semantically(spans: list[dict]) -> dict:
    """Compute titleblock_semantic_score from OCR text spans."""
    if not spans or (len(spans) == 1 and "_ocr_error" in spans[0]):
        return {"score": 0.0, "n_spans": 0, "positive_hits": [],
                "negative_hits": [], "sheet_id": None}
    score = 0.0
    positive_hits = []
    negative_hits = []
    sheet_id = None
    n_dimension_hits = 0
    n_room_hits = 0
    for span in spans:
        text = (span.get("text") or "").upper().strip()
        conf = span.get("confidence", 0.0)
        if conf < 0.5:
            continue
        # Positive keywords
        for kw, weight in POSITIVE_KEYWORDS.items():
            if kw in text:
                score += weight * conf
                positive_hits.append((kw, text))
                break
        # Negative keywords
        for kw, weight in NEGATIVE_KEYWORDS.items():
            if kw in text:
                score += weight * conf
                negative_hits.append((kw, text))
                break
        # Sheet ID pattern (strong positive)
        if SHEET_ID_RE.search(text):
            score += 1.8 * conf
            if not sheet_id:
                sheet_id = SHEET_ID_RE.search(text).group(0)
        # Dimension chain (negative)
        if DIMENSION_RE.search(text):
            n_dimension_hits += 1
        # Room number (weak negative if appears alone)
        if ROOM_NUM_RE.fullmatch(text):
            n_room_hits += 1
    # Apply pattern penalties
    if n_dimension_hits >= 3:
        score -= 1.5  # likely a dimension chain region, not title block
    if n_room_hits >= 5:
        score -= 1.0  # repeated numerics suggest schedule or room labels
    return {
        "score": round(score, 3),
        "n_spans": len(spans),
        "positive_hits": positive_hits[:10],
        "negative_hits": negative_hits[:5],
        "sheet_id": sheet_id,
        "n_dimension_hits": n_dimension_hits,
        "n_room_hits": n_room_hits,
    }


# ============================================================
# Priority 3 — OCR-Affinity Pruning (Build 2 v2)
# ============================================================

def affinity_prune(primitives: list[dict],
                    scored_regions: list[dict],
                    affinity_radius: float = 80.0,
                    min_region_score: float = 1.0,
                    structural_backbone_threshold: float = 100.0
                  ) -> tuple[list[dict], dict]:
    """Preserve primitives near semantically-valid regions; aggressive prune elsewhere.

    Rules:
    1. If primitive within affinity_radius of a region with score > min_region_score:
       PRESERVE.
    2. Else if primitive is structural backbone (length > 100pt, isolated long line):
       PRESERVE (page borders, walls).
    3. Else:
       DROP.
    """
    valid_regions = [r for r in scored_regions
                     if r["semantic"]["score"] >= min_region_score]
    if not valid_regions:
        # No valid OCR regions found — fall back to keeping structural backbone only
        kept = []
        for p in primitives:
            length = float(p.get("length", 0))
            if length >= structural_backbone_threshold:
                kept.append(p)
        return kept, {"kept_method": "structural_backbone_only",
                       "valid_regions": 0,
                       "primitives_in": len(primitives),
                       "primitives_out": len(kept)}

    kept = []
    for p in primitives:
        try:
            cx = float(p["cx"])
            cy = float(p["cy"])
            length = float(p.get("length", 0))
        except (KeyError, TypeError, ValueError):
            continue
        keep = False
        # Rule 1: near any valid region
        for r in valid_regions:
            bx0, by0, bx1, by1 = r["bbox_pt"]
            # Bbox + affinity radius
            if (bx0 - affinity_radius <= cx <= bx1 + affinity_radius and
                by0 - affinity_radius <= cy <= by1 + affinity_radius):
                keep = True
                break
        # Rule 2: structural backbone
        if not keep and length >= structural_backbone_threshold:
            keep = True
        if keep:
            kept.append(p)
    return kept, {
        "kept_method": "semantic_affinity",
        "valid_regions": len(valid_regions),
        "primitives_in": len(primitives),
        "primitives_out": len(kept),
    }


# ============================================================
# End-to-end pipeline
# ============================================================

def process_raster_page(pdf_path: Path, page_idx: int, dpi: float = 150
                         ) -> dict:
    """Full pipeline: bridge -> P1 -> P2 -> P3 -> PRG swap test."""
    t0 = time.time()
    primitives, (pw, ph), bridge_telem = reconstruct_page(pdf_path, page_idx, dpi=dpi)
    if not primitives:
        return {"page_idx": page_idx, "error": "no synthetic primitives",
                "bridge_failure": bridge_telem.failure_mode}

    # Render the raster page once for OCR cropping
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_idx]
        zoom = dpi / PDF_NATIVE_DPI
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    finally:
        doc.close()

    t_p1 = time.time()
    candidates = detect_candidate_zones(primitives, pw, ph, n_candidates=6)
    p1_ms = (time.time() - t_p1) * 1000

    # PRIORITY 2: OCR each candidate
    t_p2 = time.time()
    scored_regions = []
    for c in candidates:
        spans = ocr_region(img, c["bbox_pt"], dpi, img.shape[0])
        sem = score_region_semantically(spans)
        scored_regions.append({**c, "semantic": sem, "ocr_spans": len(spans)})
    p2_ms = (time.time() - t_p2) * 1000

    # PRIORITY 3: affinity pruning
    t_p3 = time.time()
    pruned, prune_stats = affinity_prune(primitives, scored_regions)
    p3_ms = (time.time() - t_p3) * 1000

    # PRG swap protocol on pruned primitives
    t_prg = time.time()
    for i, p in enumerate(pruned):
        p["primitive_id"] = i
    baseline = run_prg(pruned, (pw, ph))
    if not baseline:
        return {
            "page_idx": page_idx,
            "primitives_in": len(primitives),
            "primitives_after_prune": len(pruned),
            "candidates": len(candidates),
            "valid_regions": prune_stats["valid_regions"],
            "trajectory_class": "NO_BASELINE",
            "baseline_found": False,
            "p1_ms": round(p1_ms, 1),
            "p2_ms": round(p2_ms, 1),
            "p3_ms": round(p3_ms, 1),
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
        }
    ablated = run_prg(pruned, (pw, ph), exclude_edge_frame=True)
    bbox = baseline["bbox"]
    pad = 20.0
    swap_src = (max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                 min(pw, bbox[2] + pad), min(ph, bbox[3] + pad))
    swap_dst = pick_alternate_quadrant(bbox, pw, ph)
    swapped = swap_regions(pruned, pw, ph, swap_src, swap_dst)
    post_swap = run_prg(swapped, (pw, ph))

    baseline_pids = set(baseline.get("primitive_ids", []))
    if post_swap:
        post_swap_pids = set(post_swap.get("primitive_ids", []))
        overlap = baseline_pids & post_swap_pids
        recall = len(overlap) / max(len(baseline_pids), 1)
        precision = len(overlap) / max(len(post_swap_pids), 1)
        sx0, sy0, sx1, sy1 = swap_src
        dx0, dy0, dx1, dy1 = swap_dst
        projected = [bbox[0] + (dx0 - sx0), bbox[1] + (dy0 - sy0),
                      bbox[2] + (dx0 - sx0), bbox[3] + (dy0 - sy0)]
        iou_f = bbox_iou(post_swap["bbox"], projected)
        iou_s = bbox_iou(post_swap["bbox"], bbox)
    else:
        recall = precision = iou_f = iou_s = 0.0

    swap_geom = {"src": list(swap_src), "dst": list(swap_dst)}
    traj = classify_trajectory(baseline, post_swap, swap_geom, recall)
    prg_ms = (time.time() - t_prg) * 1000

    # Best region's semantic info (for confidence + receipts later)
    best_region = max(scored_regions, key=lambda r: r["semantic"]["score"],
                        default=None) if scored_regions else None
    return {
        "page_idx": page_idx,
        "baseline_found": True,
        "primitives_in": len(primitives),
        "primitives_after_prune": len(pruned),
        "candidates": len(candidates),
        "valid_regions": prune_stats["valid_regions"],
        "best_region_score": best_region["semantic"]["score"] if best_region else 0,
        "best_region_sheet_id": best_region["semantic"]["sheet_id"] if best_region else None,
        "best_region_positives": best_region["semantic"]["positive_hits"][:3] if best_region else [],
        "trajectory_class": traj,
        "follows_content": traj == "FOLLOW",
        "stays_at_position": traj == "STAY",
        "primitive_recall": round(recall, 3),
        "primitive_precision": round(precision, 3),
        "iou_follow": round(iou_f, 3),
        "iou_stay": round(iou_s, 3),
        "ablation_survives": ablated is not None,
        "p1_ms": round(p1_ms, 1),
        "p2_ms": round(p2_ms, 1),
        "p3_ms": round(p3_ms, 1),
        "prg_ms": round(prg_ms, 1),
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
    }


# ============================================================
# Main benchmark
# ============================================================

def main():
    print("=== Hierarchical Semantic Gating v1 (Priorities 1+2+3) ===\n", flush=True)
    print("Architecture:")
    print("  P1: structural candidate zones (NO OCR)")
    print("  P2: PaddleOCR INSIDE candidates + lexical scoring (positive/negative)")
    print("  P3: OCR-affinity pruning (preserve near semantically-valid regions)")
    print("  -> PRG swap test (unchanged)\n", flush=True)

    raster_pdfs = sorted(RASTER_CACHE.glob("raster150_*.pdf"))
    if not raster_pdfs:
        print("ERROR: no rasterized cache.", flush=True)
        return

    all_results = []
    per_pdf = defaultdict(lambda: {"baseline_found": 0, "follows": 0, "stays": 0,
                                     "ocr_zones": 0, "best_sheet_ids": []})
    traj_counts = defaultdict(int)
    follows = 0
    stays = 0
    n_baselines = 0
    ablation_survives = 0
    recall_sum = 0.0
    precision_sum = 0.0
    iou_sum = 0.0
    runtime_sum = 0.0
    primitives_in_sum = 0
    primitives_out_sum = 0

    for pdf in raster_pdfs:
        print(f"=== {pdf.name} ===", flush=True)
        doc = fitz.open(str(pdf))
        n_pages = doc.page_count
        doc.close()
        n_test = min(n_pages, 6)
        for pg in range(n_test):
            try:
                r = process_raster_page(pdf, pg, dpi=150)
                all_results.append({"pdf_name": pdf.name, **r})
                if "error" in r:
                    print(f"  page {pg}: ERROR {r['error']}", flush=True)
                    continue
                if r.get("baseline_found"):
                    n_baselines += 1
                    per_pdf[pdf.name]["baseline_found"] += 1
                    if r.get("ablation_survives"):
                        ablation_survives += 1
                    traj = r.get("trajectory_class")
                    if traj:
                        traj_counts[traj] += 1
                    if r.get("follows_content"):
                        follows += 1
                        per_pdf[pdf.name]["follows"] += 1
                    if r.get("stays_at_position"):
                        stays += 1
                        per_pdf[pdf.name]["stays"] += 1
                    recall_sum += r.get("primitive_recall", 0)
                    precision_sum += r.get("primitive_precision", 0)
                    iou_sum += r.get("iou_follow", 0)
                per_pdf[pdf.name]["ocr_zones"] += r.get("valid_regions", 0)
                if r.get("best_region_sheet_id"):
                    per_pdf[pdf.name]["best_sheet_ids"].append(r["best_region_sheet_id"])
                runtime_sum += r.get("elapsed_ms", 0)
                primitives_in_sum += r.get("primitives_in", 0)
                primitives_out_sum += r.get("primitives_after_prune", 0)
                print(f"  page {pg}: prims {r.get('primitives_in',0):5d}->{r.get('primitives_after_prune',0):4d}  "
                      f"zones={r.get('candidates',0)} valid={r.get('valid_regions',0)}  "
                      f"sheet_id={r.get('best_region_sheet_id')}  "
                      f"traj={r.get('trajectory_class','?'):<14s} "
                      f"r={r.get('primitive_recall',0):.2f} p={r.get('primitive_precision',0):.2f} "
                      f"iou={r.get('iou_follow',0):.2f}  "
                      f"{r.get('elapsed_ms',0):.0f}ms (p2_ocr={r.get('p2_ms',0):.0f}ms)",
                      flush=True)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"  page {pg}: EXCEPTION {type(exc).__name__}: {str(exc)[:120]}",
                       flush=True)

    # === VERDICT ===
    print("\n=== HIERARCHICAL SEMANTIC GATING VERDICT ===", flush=True)
    total = sum(1 for r in all_results if "error" not in r)
    print(f"\ntotal pages tested:        {total}")
    print(f"primitives in (avg):       {primitives_in_sum/max(total,1):.0f}/page")
    print(f"primitives after prune:    {primitives_out_sum/max(total,1):.0f}/page")
    print(f"runtime avg:               {runtime_sum/max(total,1):.0f}ms/page")
    print(f"\n--- TRAJECTORY (raster path) ---")
    for cls in ("FOLLOW", "STAY", "DRIFT_NEAR", "SPLIT_CLUSTER", "THIRD_REGION",
                 "NULL_COLLAPSE", "NO_BASELINE"):
        c = traj_counts.get(cls, 0)
        print(f"  {cls:<18s} {c:3d}  ({c/max(n_baselines,1)*100:.1f}%)")

    follows_rate = follows / max(n_baselines, 1)
    avg_recall = recall_sum / max(n_baselines, 1)
    avg_precision = precision_sum / max(n_baselines, 1)
    avg_iou = iou_sum / max(n_baselines, 1)
    print(f"\n--- GATES (raster semantic gating) vs Build 1 / Vector r8 ---")
    print(f"  follows:    {follows_rate*100:5.1f}%  Build1=50.0%  v_r8=89.1%")
    print(f"  stays:      {stays/max(n_baselines,1)*100:5.1f}%  Build1=16.7%  v_r8=2.3%")
    print(f"  recall:     {avg_recall:.3f}  Build1=0.484  v_r8=0.84")
    print(f"  precision:  {avg_precision:.3f}  Build1=0.412  v_r8=0.81")
    print(f"  IoU:        {avg_iou:.3f}  Build1=0.478  v_r8=0.79")
    print(f"  ablation:   {ablation_survives}/{n_baselines}  ({ablation_survives/max(n_baselines,1)*100:.0f}%)")

    print(f"\n--- PER-PDF (with extracted sheet IDs!) ---")
    for pdf_name, stats in sorted(per_pdf.items()):
        marker = " <-- FOLLOWS" if stats["follows"] > 0 else ""
        ids_sample = ", ".join(set(stats["best_sheet_ids"]))[:50]
        print(f"  {pdf_name[:45]:<45s}  base={stats['baseline_found']}  "
              f"follows={stats['follows']}  ocr_zones={stats['ocr_zones']}  "
              f"sheet_ids=[{ids_sample}]{marker}")

    out_path = OUT_DIR / "semantic_gating_v1_2026-05-25.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "semantic_gating_v1",
            "total_pages": total,
            "follows_rate": round(follows_rate, 4),
            "avg_recall": round(avg_recall, 4),
            "avg_precision": round(avg_precision, 4),
            "avg_iou": round(avg_iou, 4),
            "trajectory": dict(traj_counts),
            "per_pdf": {k: {**v, "best_sheet_ids": list(set(v["best_sheet_ids"]))}
                         for k, v in per_pdf.items()},
            "per_page": all_results,
        }, f, indent=2)
    print(f"\nsaved: {out_path}", flush=True)


if __name__ == "__main__":
    main()
