"""Semantic Region-First Reconstruction (SRR) v1.

The architectural inversion:
  NOT "geometry -> semantics" (old Hierarchical Gating)
  YES "semantics -> geometry"

Pipeline:
  1. Render page (one image)
  2. OCR ONCE on full page (cached, singleton engine)
  3. Build semantic density heatmap from OCR boxes + title-keyword scores
  4. Select top-K (default 3) semantic zones from heatmap peaks
  5. Extract primitives (Hough + contour) ONLY inside those zones
  6. Synthesize PRG-compatible graph with semantic metadata
  7. Compute cluster_uniqueness_score for confidence calibration

Expected outcomes vs Hierarchical Gating v1:
  - runtime: 47s -> 5-10s (one OCR call vs 6)
  - primitives/page: 17K -> 500-2K (zone-limited)
  - SPLIT_CLUSTER: 31.8% -> single-digit (fewer competing motifs)
  - follows: 50% -> 70%+ (locked production gate)

PRG remains UNCHANGED. SRR adapts TO PRG's primitive contract.
"""
from __future__ import annotations

import json
import math
import os
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
from prg_v29 import (
    run_prg, swap_regions, pick_alternate_quadrant,
    bbox_iou, classify_trajectory,
)

RASTER_CACHE = Path(r"C:\Users\tonio\Projects\myapp\blueprints\raster_r9")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")

PDF_NATIVE_DPI = 72.0
OCR_DPI = 150.0  # reverted from 100dpi after Phase 1.1 attempt 1 showed sheet ID degradation
GEOM_DPI = 200.0  # render dpi for line extraction (higher for quality)
# Phase 1.1 attempt 1 findings (2026-05-25):
#   - 100dpi DPI reduction: only ~24% runtime improvement (text-span count dominates OCR cost)
#   - Sheet IDs visibly changed at 100dpi (GA26, A6 etc. vs E3, AK200 — regression signal)
#   - SIGSEGV on GAVNN (5400x3600 raster) — PaddleOCR Windows stability problem
# Phase 1.1 attempt 2 path: switch to Azure Doc Intel (already wired in Drew) OR
#   ONNX-export PaddleOCR. PaddleOCR PP-OCRv4 on Windows CPU has 3 production blockers.

# Title-block lexical signals
POSITIVE_TOKENS = {
    "SHEET": 2.0, "DRAWN": 1.5, "PROJECT": 1.5, "REVISION": 1.5, "REV": 1.2,
    "SCALE": 1.2, "DATE": 0.7, "CHECKED": 1.0, "DESIGNED": 1.0, "APPROVED": 1.0,
    "DRAWING": 1.2, "DWG": 1.0, "TITLE": 1.2, "ARCHITECT": 1.3, "ENGINEER": 1.3,
    "CLIENT": 1.0, "OWNER": 1.5, "DESIGNER": 1.2, "FIRM": 1.0, "ADDRESS": 0.7,
}
NEGATIVE_TOKENS = {
    "EQUIPMENT": -1.0, "SCHEDULE": -1.5, "DOOR": -0.7, "WINDOW": -0.7,
    "PANEL": -0.7, "KEYNOTE": -0.8, "TYPICAL": -0.5, "TYP": -0.4,
    "FIXTURE": -0.5, "ROOM": -0.5,
}
SHEET_ID_RE = re.compile(r"\b[A-Z]{1,2}[-\.]?\d{1,3}(?:\.\d{1,2})?\b")


# ============================================================
# Stage 1 — OCR Cache (singleton engine, page-level cache)
# ============================================================

class OCREngine:
    _instance = None
    _page_cache: dict[tuple, list] = {}

    @classmethod
    def get(cls):
        if cls._instance is None:
            from paddleocr import PaddleOCR
            print("    initializing PaddleOCR (one-time)...", flush=True)
            cls._instance = PaddleOCR(
                lang="en",
                ocr_version="PP-OCRv4",
                enable_mkldnn=False,  # Paddle 3.3.1 Windows bug bypass
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        return cls._instance

    @classmethod
    def ocr_page(cls, page_image: np.ndarray, cache_key: tuple) -> list[dict]:
        """OCR a full page once; return list of {text, conf, bbox_px}."""
        if cache_key in cls._page_cache:
            return cls._page_cache[cache_key]
        if len(page_image.shape) == 2:
            img_bgr = cv2.cvtColor(page_image, cv2.COLOR_GRAY2BGR)
        else:
            img_bgr = page_image
        engine = cls.get()
        try:
            result = engine.predict(img_bgr)
        except Exception as exc:
            cls._page_cache[cache_key] = []
            return []
        spans = []
        if not result:
            cls._page_cache[cache_key] = []
            return []
        for page_result in result:
            if not isinstance(page_result, dict):
                continue
            texts = page_result.get("rec_texts", [])
            scores = page_result.get("rec_scores", [])
            polys = page_result.get("rec_polys") or page_result.get("dt_polys", [])
            for i, txt in enumerate(texts):
                conf = scores[i] if i < len(scores) else 0.0
                if conf < 0.4:
                    continue
                # Compute bbox from poly (4 points)
                if i < len(polys):
                    poly = polys[i]
                    xs = [int(p[0]) for p in poly]
                    ys = [int(p[1]) for p in poly]
                    bbox_px = (min(xs), min(ys), max(xs), max(ys))
                else:
                    bbox_px = (0, 0, 0, 0)
                spans.append({
                    "text": txt,
                    "conf": float(conf),
                    "bbox_px": bbox_px,
                })
        cls._page_cache[cache_key] = spans
        return spans


# ============================================================
# Stage 2 — Semantic Density Heatmap
# ============================================================

def build_semantic_heatmap(ocr_spans: list[dict],
                            page_w_px: int, page_h_px: int,
                            grid_size_px: int = 80) -> np.ndarray:
    """Combine OCR density + title-keyword density + bottom-right prior into heatmap."""
    grid_w = max(1, page_w_px // grid_size_px + 1)
    grid_h = max(1, page_h_px // grid_size_px + 1)
    heatmap = np.zeros((grid_h, grid_w), dtype=np.float32)

    if not ocr_spans:
        return heatmap

    for span in ocr_spans:
        x0, y0, x1, y1 = span["bbox_px"]
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        text = (span["text"] or "").upper()
        conf = span["conf"]
        gx = max(0, min(grid_w - 1, (x0 + x1) // 2 // grid_size_px))
        gy = max(0, min(grid_h - 1, (y0 + y1) // 2 // grid_size_px))

        # Base density (every text contributes 1)
        weight = conf

        # Title-keyword bonus
        for kw, bonus in POSITIVE_TOKENS.items():
            if kw in text:
                weight += bonus * conf
                break
        for kw, penalty in NEGATIVE_TOKENS.items():
            if kw in text:
                weight += penalty * conf
                break
        # Sheet ID pattern is strongest
        if SHEET_ID_RE.search(text):
            weight += 2.5 * conf

        # Small-font bonus (title block text is usually tight)
        text_height = y1 - y0
        if 8 <= text_height <= 35:
            weight *= 1.4

        heatmap[gy, gx] += weight

    # Bottom-right prior (multiplicative bias toward typical title-block location)
    for gy in range(grid_h):
        for gx in range(grid_w):
            rx = (gx + 0.5) / grid_w
            ry = (gy + 0.5) / grid_h
            br_dist = math.hypot(1 - rx, 1 - ry)
            br_bonus = math.exp(-br_dist * 2.0)
            heatmap[gy, gx] *= (1.0 + br_bonus * 0.6)

    return heatmap


def select_top_zones(heatmap: np.ndarray, n_zones: int = 3,
                      page_w_px: int = 0, page_h_px: int = 0,
                      grid_size_px: int = 80,
                      zone_radius_grid: int = 3) -> list[tuple[int, int, int, int]]:
    """Non-maximum suppression on heatmap to pick top-K zone bboxes (in pixels)."""
    zones = []
    h, w = heatmap.shape
    score_copy = heatmap.copy()
    for _ in range(n_zones):
        flat_idx = int(np.argmax(score_copy))
        gy, gx = divmod(flat_idx, w)
        peak = float(score_copy[gy, gx])
        if peak < 0.5:
            break
        gy0 = max(0, gy - zone_radius_grid)
        gy1 = min(h, gy + zone_radius_grid + 1)
        gx0 = max(0, gx - zone_radius_grid)
        gx1 = min(w, gx + zone_radius_grid + 1)
        # bbox in pixels
        x0_px = gx0 * grid_size_px
        y0_px = gy0 * grid_size_px
        x1_px = min(page_w_px, gx1 * grid_size_px)
        y1_px = min(page_h_px, gy1 * grid_size_px)
        zones.append((x0_px, y0_px, x1_px, y1_px))
        # Suppress
        score_copy[gy0:gy1, gx0:gx1] = 0
    return zones


# ============================================================
# Stage 3 — Zone-Limited Primitive Extraction
# ============================================================

def extract_zone_primitives(page_gray: np.ndarray,
                              zones_px: list[tuple[int, int, int, int]],
                              dpi: float,
                              page_w_pt: float, page_h_pt: float
                            ) -> list[dict]:
    """Extract primitives ONLY inside top zones. Convert to PRG schema in PDF pt."""
    if not zones_px:
        return []
    scale_to_pt = PDF_NATIVE_DPI / dpi
    primitives = []

    for zone in zones_px:
        x0, y0, x1, y1 = zone
        if x1 - x0 < 20 or y1 - y0 < 20:
            continue
        crop = page_gray[y0:y1, x0:x1]
        if crop.size == 0:
            continue

        # Normalize + binary
        normed = cv2.normalize(crop, None, 0, 255, cv2.NORM_MINMAX)
        binary = cv2.adaptiveThreshold(
            normed, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 25, 10
        )
        kernel = np.ones((2, 2), np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

        # Hough lines
        min_len = max(8, int(0.05 * dpi))
        max_gap = max(3, int(0.02 * dpi))
        threshold_h = max(30, int(0.04 * dpi))
        lines = cv2.HoughLinesP(
            binary, rho=1, theta=np.pi / 180,
            threshold=threshold_h,
            minLineLength=min_len,
            maxLineGap=max_gap,
        )
        zone_lines = []
        if lines is not None:
            for line in lines:
                lx0, ly0, lx1, ly1 = line[0]
                # Translate back to page pixels
                px0 = float(x0 + lx0)
                py0 = float(y0 + ly0)
                px1 = float(x0 + lx1)
                py1 = float(y0 + ly1)
                zone_lines.append((px0, py0, px1, py1))

        # Contour lines (for broken structure)
        contours, _ = cv2.findContours(binary, cv2.RETR_LIST,
                                          cv2.CHAIN_APPROX_TC89_L1)
        min_len_sq = (0.04 * dpi) ** 2
        for c in contours:
            if len(c) < 2:
                continue
            approx = cv2.approxPolyDP(c, 1.5, False)
            pts = approx.reshape(-1, 2)
            for i in range(len(pts) - 1):
                lx0 = float(pts[i, 0])
                ly0 = float(pts[i, 1])
                lx1 = float(pts[i + 1, 0])
                ly1 = float(pts[i + 1, 1])
                if (lx1 - lx0) ** 2 + (ly1 - ly0) ** 2 < min_len_sq:
                    continue
                zone_lines.append((
                    float(x0 + lx0), float(y0 + ly0),
                    float(x0 + lx1), float(y0 + ly1),
                ))

        # Endpoint snap (within zone)
        snap_radius = max(3.0, 6.0 * dpi / PDF_NATIVE_DPI)
        zone_lines = snap_endpoints(zone_lines, snap_radius=snap_radius)

        # Convert to PDF pt and emit
        for lx0, ly0, lx1, ly1 in zone_lines:
            px0_pt = lx0 * scale_to_pt
            py0_pt = ly0 * scale_to_pt
            px1_pt = lx1 * scale_to_pt
            py1_pt = ly1 * scale_to_pt
            length = math.hypot(px1_pt - px0_pt, py1_pt - py0_pt)
            if length < 2:
                continue
            primitives.append({
                "primitive_type": "line",
                "x0": px0_pt, "y0": py0_pt, "x1": px1_pt, "y1": py1_pt,
                "cx": (px0_pt + px1_pt) / 2,
                "cy": (py0_pt + py1_pt) / 2,
                "length": length,
                "source": "srr_zone",
                "confidence": 0.8,
            })
    return primitives


def snap_endpoints(lines, snap_radius=6.0):
    if not lines:
        return []
    eps = []
    for x0, y0, x1, y1 in lines:
        eps.append((x0, y0))
        eps.append((x1, y1))
    bins = defaultdict(list)
    for i, (x, y) in enumerate(eps):
        key = (round(x / snap_radius), round(y / snap_radius))
        bins[key].append(i)
    canonical = {}
    for key, idxs in bins.items():
        cx = sum(eps[i][0] for i in idxs) / len(idxs)
        cy = sum(eps[i][1] for i in idxs) / len(idxs)
        canonical[key] = (cx, cy)
    snapped_eps = [canonical[(round(x / snap_radius), round(y / snap_radius))]
                    for x, y in eps]
    out = []
    for i in range(len(lines)):
        x0, y0 = snapped_eps[2 * i]
        x1, y1 = snapped_eps[2 * i + 1]
        if math.hypot(x1 - x0, y1 - y0) < 1.0:
            continue
        out.append((x0, y0, x1, y1))
    return out


# ============================================================
# Stage 5 — Cluster Uniqueness (confidence calibration prep)
# ============================================================

def cluster_uniqueness(primitives, page_extent):
    """Get top-2 motif scores; return dominant/second ratio."""
    from prg_v29 import (build_grid, type_primitives, build_relational_graph,
                          mine_admin_motifs)
    types_per_prim, grid = type_primitives(primitives, page_extent)
    edges, micro_idx, edge_types = build_relational_graph(
        primitives, types_per_prim, grid
    )
    motifs = mine_admin_motifs(edges, micro_idx, primitives, types_per_prim)
    if len(motifs) < 2:
        return 1.0  # max uniqueness — only one motif
    motifs.sort(key=lambda m: -m["size"] * (0.3 + m["alignment_score"]))
    top = motifs[0]["size"] * (0.3 + motifs[0]["alignment_score"])
    second = motifs[1]["size"] * (0.3 + motifs[1]["alignment_score"])
    if top == 0:
        return 0.0
    ratio = top / second if second > 0 else 10.0
    return min(ratio, 10.0)


# ============================================================
# Confidence Tier
# ============================================================

def confidence_tier(uniqueness: float, recall: float, precision: float,
                     iou: float, ocr_top_zone_score: float,
                     trajectory: str) -> str:
    """Map combined metrics to GREEN / YELLOW / RED."""
    if trajectory == "FOLLOW" and iou >= 0.5 and uniqueness >= 1.5 and ocr_top_zone_score >= 1.5:
        return "GREEN"
    if trajectory in ("FOLLOW", "DRIFT_NEAR") and iou >= 0.3 and uniqueness >= 1.1:
        return "YELLOW"
    return "RED"


# ============================================================
# Full SRR pipeline per page
# ============================================================

def render_page_gray(pdf_path: Path, page_idx: int, dpi: float
                      ) -> tuple[np.ndarray, float, float]:
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_idx]
        page_w_pt = page.rect.width
        page_h_pt = page.rect.height
        mat = fitz.Matrix(dpi / PDF_NATIVE_DPI, dpi / PDF_NATIVE_DPI)
        pix = page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        return img, page_w_pt, page_h_pt
    finally:
        doc.close()


def process_srr_page(pdf_path: Path, page_idx: int) -> dict:
    t0 = time.time()
    # Render at OCR dpi (faster, OCR doesn't need higher)
    img_ocr, pw_pt, ph_pt = render_page_gray(pdf_path, page_idx, OCR_DPI)
    ph_px, pw_px = img_ocr.shape

    # STAGE 1: OCR full page (cached)
    t_ocr = time.time()
    cache_key = (str(pdf_path), page_idx, OCR_DPI)
    ocr_spans = OCREngine.ocr_page(img_ocr, cache_key)
    ocr_ms = (time.time() - t_ocr) * 1000

    # STAGE 2: Semantic heatmap
    t_hm = time.time()
    heatmap = build_semantic_heatmap(ocr_spans, pw_px, ph_px)
    zones_px = select_top_zones(heatmap, n_zones=3,
                                  page_w_px=pw_px, page_h_px=ph_px)
    hm_ms = (time.time() - t_hm) * 1000

    # Score top zone semantically (for confidence)
    top_zone_score = 0.0
    if zones_px:
        x0, y0, x1, y1 = zones_px[0]
        for span in ocr_spans:
            sx0, sy0, sx1, sy1 = span["bbox_px"]
            if not (sx0 >= x0 and sx1 <= x1 and sy0 >= y0 and sy1 <= y1):
                continue
            text = (span["text"] or "").upper()
            for kw, bonus in POSITIVE_TOKENS.items():
                if kw in text:
                    top_zone_score += bonus * span["conf"]
                    break
            if SHEET_ID_RE.search(text):
                top_zone_score += 2.0 * span["conf"]

    # STAGE 3: Zone-limited primitive extraction
    t_geom = time.time()
    primitives = extract_zone_primitives(img_ocr, zones_px, OCR_DPI, pw_pt, ph_pt)
    geom_ms = (time.time() - t_geom) * 1000

    # Extract sheet ID from any zone OCR
    sheet_id = None
    for span in ocr_spans:
        text = span["text"] or ""
        m = SHEET_ID_RE.search(text.upper())
        if m:
            sheet_id = m.group(0)
            break

    if not primitives:
        return {
            "page_idx": page_idx,
            "n_ocr_spans": len(ocr_spans),
            "n_zones": len(zones_px),
            "n_primitives": 0,
            "sheet_id": sheet_id,
            "trajectory_class": "NO_PRIMITIVES",
            "failure_mode": "ZONE_EMPTY",
            "ocr_ms": round(ocr_ms, 1),
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
        }

    # PRG + SWAP
    t_prg = time.time()
    for i, p in enumerate(primitives):
        p["primitive_id"] = i
    baseline = run_prg(primitives, (pw_pt, ph_pt))
    if not baseline:
        return {
            "page_idx": page_idx,
            "n_ocr_spans": len(ocr_spans),
            "n_zones": len(zones_px),
            "n_primitives": len(primitives),
            "sheet_id": sheet_id,
            "trajectory_class": "NO_BASELINE",
            "failure_mode": "NO_BASELINE",
            "ocr_ms": round(ocr_ms, 1),
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
        }
    ablated = run_prg(primitives, (pw_pt, ph_pt), exclude_edge_frame=True)
    bbox = baseline["bbox"]
    pad = 20.0
    swap_src = (max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                 min(pw_pt, bbox[2] + pad), min(ph_pt, bbox[3] + pad))
    swap_dst = pick_alternate_quadrant(bbox, pw_pt, ph_pt)
    swapped = swap_regions(primitives, pw_pt, ph_pt, swap_src, swap_dst)
    post_swap = run_prg(swapped, (pw_pt, ph_pt))

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

    uniqueness = cluster_uniqueness(primitives, (pw_pt, ph_pt))
    conf_tier = confidence_tier(uniqueness, recall, precision, iou_f,
                                  top_zone_score, traj)
    prg_ms = (time.time() - t_prg) * 1000

    return {
        "page_idx": page_idx,
        "n_ocr_spans": len(ocr_spans),
        "n_zones": len(zones_px),
        "n_primitives": len(primitives),
        "sheet_id": sheet_id,
        "trajectory_class": traj,
        "follows_content": traj == "FOLLOW",
        "stays_at_position": traj == "STAY",
        "primitive_recall": round(recall, 3),
        "primitive_precision": round(precision, 3),
        "iou_follow": round(iou_f, 3),
        "iou_stay": round(iou_s, 3),
        "cluster_uniqueness": round(uniqueness, 3),
        "ocr_top_zone_score": round(top_zone_score, 3),
        "ablation_survives": ablated is not None,
        "confidence_tier": conf_tier,
        "ocr_ms": round(ocr_ms, 1),
        "hm_ms": round(hm_ms, 1),
        "geom_ms": round(geom_ms, 1),
        "prg_ms": round(prg_ms, 1),
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
    }


# ============================================================
# Main benchmark
# ============================================================

def main():
    print("=== Semantic Region-First Reconstruction (SRR) v1 ===\n", flush=True)
    print("Architecture: semantics -> geometry")
    print("  Stage 1: OCR ONCE per page (cached)")
    print("  Stage 2: semantic density heatmap")
    print("  Stage 3: top-3 zones from heatmap")
    print("  Stage 4: primitive extraction ONLY in zones (not full page)")
    print("  Stage 5: PRG swap + cluster uniqueness + confidence tier\n", flush=True)

    raster_pdfs = sorted(RASTER_CACHE.glob("raster150_*.pdf"))
    if not raster_pdfs:
        print("ERROR: no raster cache", flush=True)
        return

    all_results = []
    per_pdf = defaultdict(lambda: {"baseline_found": 0, "follows": 0, "stays": 0,
                                     "sheet_ids": [], "tiers": defaultdict(int)})
    traj_counts = defaultdict(int)
    tier_counts = defaultdict(int)
    follows = stays = n_baselines = ablation_survives = 0
    recall_sum = precision_sum = iou_sum = 0.0
    runtime_sum = ocr_runtime_sum = 0.0
    primitives_sum = 0

    for pdf in raster_pdfs:
        print(f"=== {pdf.name} ===", flush=True)
        doc = fitz.open(str(pdf))
        n_pages = doc.page_count
        doc.close()
        n_test = min(n_pages, 6)
        for pg in range(n_test):
            try:
                r = process_srr_page(pdf, pg)
                all_results.append({"pdf_name": pdf.name, **r})
                traj = r.get("trajectory_class", "?")
                traj_counts[traj] += 1
                runtime_sum += r.get("elapsed_ms", 0)
                ocr_runtime_sum += r.get("ocr_ms", 0)
                primitives_sum += r.get("n_primitives", 0)
                tier = r.get("confidence_tier", "RED")
                tier_counts[tier] += 1
                if r.get("trajectory_class") not in ("NO_PRIMITIVES", "NO_BASELINE"):
                    n_baselines += 1
                    per_pdf[pdf.name]["baseline_found"] += 1
                    if r.get("ablation_survives"):
                        ablation_survives += 1
                    if r.get("follows_content"):
                        follows += 1
                        per_pdf[pdf.name]["follows"] += 1
                    if r.get("stays_at_position"):
                        stays += 1
                        per_pdf[pdf.name]["stays"] += 1
                    recall_sum += r.get("primitive_recall", 0)
                    precision_sum += r.get("primitive_precision", 0)
                    iou_sum += r.get("iou_follow", 0)
                if r.get("sheet_id"):
                    per_pdf[pdf.name]["sheet_ids"].append(r["sheet_id"])
                per_pdf[pdf.name]["tiers"][tier] += 1
                print(f"  page {pg}: spans={r.get('n_ocr_spans',0):4d} "
                      f"zones={r.get('n_zones',0)} prims={r.get('n_primitives',0):4d}  "
                      f"sheet={r.get('sheet_id')}  "
                      f"traj={r.get('trajectory_class','?'):<14s} "
                      f"r={r.get('primitive_recall',0):.2f} p={r.get('primitive_precision',0):.2f} "
                      f"iou={r.get('iou_follow',0):.2f} u={r.get('cluster_uniqueness',0):.2f}  "
                      f"{r.get('confidence_tier','?')}  "
                      f"{r.get('elapsed_ms',0):.0f}ms (ocr={r.get('ocr_ms',0):.0f}ms)",
                      flush=True)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"  page {pg}: EXCEPTION {type(exc).__name__}: {str(exc)[:120]}",
                       flush=True)

    # === VERDICT ===
    total = sum(1 for r in all_results)
    print(f"\n=== SRR v1 VERDICT ===")
    print(f"\ntotal pages tested: {total}")
    print(f"avg primitives/page: {primitives_sum/max(total,1):.0f}  "
          f"(SemGate v1: ~8861, Build1: ~17K, Vector r8: ~3K)")
    print(f"avg runtime/page:    {runtime_sum/max(total,1):.0f}ms  "
          f"(SemGate v1: 47000ms, target: <5000ms)")
    print(f"avg OCR runtime:     {ocr_runtime_sum/max(total,1):.0f}ms/page")

    print(f"\n--- TRAJECTORY ---")
    for cls in ("FOLLOW", "STAY", "DRIFT_NEAR", "SPLIT_CLUSTER", "THIRD_REGION",
                 "NULL_COLLAPSE", "NO_BASELINE", "NO_PRIMITIVES"):
        c = traj_counts.get(cls, 0)
        if c > 0:
            print(f"  {cls:<18s} {c:3d}  ({c/max(total,1)*100:.1f}%)")

    follows_rate = follows / max(n_baselines, 1)
    avg_recall = recall_sum / max(n_baselines, 1)
    avg_precision = precision_sum / max(n_baselines, 1)
    avg_iou = iou_sum / max(n_baselines, 1)
    print(f"\n--- ACCEPTANCE GATES (locked) ---")
    print(f"  follows:    {follows_rate*100:5.1f}%  target>=70%  SemGate1: 50%  Vector r8: 89.1%")
    print(f"  recall:     {avg_recall:.3f}  target>=0.70  SemGate1: 0.454")
    print(f"  precision:  {avg_precision:.3f}  target>=0.70  SemGate1: 0.440")
    print(f"  IoU:        {avg_iou:.3f}  target>=0.50  SemGate1: 0.518")
    print(f"  runtime:    {runtime_sum/max(total,1):.0f}ms/page  target<5000  SemGate1: 47000ms")
    print(f"  ablation:   {ablation_survives}/{n_baselines}")

    print(f"\n--- CONFIDENCE TIERS ---")
    for tier in ("GREEN", "YELLOW", "RED"):
        c = tier_counts.get(tier, 0)
        print(f"  {tier:<8s} {c:3d}  ({c/max(total,1)*100:.1f}%)")

    print(f"\n--- PER-PDF (sheet IDs extracted) ---")
    for pdf_name, stats in sorted(per_pdf.items()):
        ids = ", ".join(set(stats["sheet_ids"]))[:60]
        tier_str = ", ".join(f"{t}={c}" for t, c in stats["tiers"].items())
        marker = " <-- FOLLOWS" if stats["follows"] > 0 else ""
        print(f"  {pdf_name[:42]:<42s}  base={stats['baseline_found']}  "
              f"follows={stats['follows']}  sheet_ids=[{ids}]  ({tier_str}){marker}")

    out_path = OUT_DIR / "srr_v1_2026-05-25.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "srr_v1",
            "total_pages": total,
            "follows_rate": round(follows_rate, 4),
            "stays_rate": round(stays / max(n_baselines, 1), 4),
            "avg_recall": round(avg_recall, 4),
            "avg_precision": round(avg_precision, 4),
            "avg_iou": round(avg_iou, 4),
            "avg_primitives_per_page": int(primitives_sum / max(total, 1)),
            "avg_runtime_ms": round(runtime_sum / max(total, 1), 1),
            "avg_ocr_ms": round(ocr_runtime_sum / max(total, 1), 1),
            "trajectory": dict(traj_counts),
            "confidence_tiers": dict(tier_counts),
            "per_pdf": {k: {**v, "sheet_ids": list(set(v["sheet_ids"])),
                              "tiers": dict(v["tiers"])}
                         for k, v in per_pdf.items()},
            "per_page": all_results,
        }, f, indent=2)
    print(f"\nsaved: {out_path}", flush=True)


if __name__ == "__main__":
    main()
