"""SRR + Azure DI v2 — Phase 1.2.

Architectural shift from v1:
  v1: OCR -> heatmap -> top-3 zones -> extract primitives ONLY in zones (~124/page)
  v2: OCR -> full-page Hough/contour -> score by Azure-affinity -> top-K per bucket (~1000/page)

Acceptance gates (Phase 1.2):
  - Runtime p95 <5s/page (carry from 1.1; should stay similar)
  - Sheet ID recovery preserved or improved (24/24 baseline)
  - Primitive count: 500-1500/page (vs 124 in v1)
  - Cost <$0.005/page (Azure Read tier $0.0015)
  - Tier calibration: GREEN tier present, RED <50%
  - Border ablation survives
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
from text_extractor import get_text_extractor, compute_cache_key
from semantic_pruner_v2 import semantic_prune, SHEET_ID_RE, POSITIVE_TOKENS
from prg_v29 import (
    run_prg, swap_regions, pick_alternate_quadrant,
    bbox_iou, classify_trajectory,
)

RASTER_CACHE = Path(r"C:\Users\tonio\Projects\myapp\blueprints\raster_r9")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")
PDF_NATIVE_DPI = 72.0
OCR_DPI = 150.0
TARGET_PRIMITIVES = 1000  # Phase 1.2 target middle of 500-1500 range


# ============================================================
# Full-page primitive extraction (Hough + contour, no zoning)
# ============================================================

def extract_full_page_primitives(page_gray: np.ndarray, dpi: float,
                                   pw_pt: float, ph_pt: float
                                  ) -> list[dict]:
    """Extract ALL primitives across the page. Scoring/pruning happens after."""
    h_px, w_px = page_gray.shape
    scale_to_pt = PDF_NATIVE_DPI / dpi

    # Normalize + binarize
    normed = cv2.normalize(page_gray, None, 0, 255, cv2.NORM_MINMAX)
    binary = cv2.adaptiveThreshold(normed, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                      cv2.THRESH_BINARY_INV, 25, 10)
    kernel = np.ones((2, 2), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

    min_len = max(8, int(0.05 * dpi))
    max_gap = max(3, int(0.02 * dpi))
    threshold = max(30, int(0.04 * dpi))
    lines_h = cv2.HoughLinesP(binary, rho=1, theta=np.pi / 180,
                                 threshold=threshold, minLineLength=min_len,
                                 maxLineGap=max_gap)

    contours, _ = cv2.findContours(binary, cv2.RETR_LIST,
                                      cv2.CHAIN_APPROX_TC89_L1)

    all_lines_px = []
    if lines_h is not None:
        for line in lines_h:
            x0, y0, x1, y1 = line[0]
            all_lines_px.append((float(x0), float(y0), float(x1), float(y1)))

    min_len_sq = (0.04 * dpi) ** 2
    for c in contours:
        if len(c) < 2:
            continue
        approx = cv2.approxPolyDP(c, 1.5, False)
        pts = approx.reshape(-1, 2)
        for i in range(len(pts) - 1):
            x0, y0 = float(pts[i, 0]), float(pts[i, 1])
            x1, y1 = float(pts[i + 1, 0]), float(pts[i + 1, 1])
            if (x1 - x0) ** 2 + (y1 - y0) ** 2 < min_len_sq:
                continue
            all_lines_px.append((x0, y0, x1, y1))

    # Endpoint snap (DPI-aware)
    snap_r = max(3.0, 6.0 * dpi / PDF_NATIVE_DPI)
    if all_lines_px:
        eps = []
        for x0, y0, x1, y1 in all_lines_px:
            eps += [(x0, y0), (x1, y1)]
        bins = defaultdict(list)
        for i, (x, y) in enumerate(eps):
            bins[(round(x / snap_r), round(y / snap_r))].append(i)
        canon = {k: (sum(eps[i][0] for i in v) / len(v),
                      sum(eps[i][1] for i in v) / len(v))
                 for k, v in bins.items()}
        snapped_eps = [canon[(round(x / snap_r), round(y / snap_r))]
                        for x, y in eps]
        snapped = []
        for i in range(len(all_lines_px)):
            x0, y0 = snapped_eps[2 * i]
            x1, y1 = snapped_eps[2 * i + 1]
            if math.hypot(x1 - x0, y1 - y0) < 1.0:
                continue
            snapped.append((x0, y0, x1, y1))
        all_lines_px = snapped

    primitives = []
    for lx0, ly0, lx1, ly1 in all_lines_px:
        # PDF coordinates
        px0 = lx0 * scale_to_pt
        py0 = ly0 * scale_to_pt
        px1 = lx1 * scale_to_pt
        py1 = ly1 * scale_to_pt
        length_pt = math.hypot(px1 - px0, py1 - py0)
        if length_pt < 2:
            continue
        # angle for orthogonality check
        dx = lx1 - lx0
        dy = ly1 - ly0
        angle = math.degrees(math.atan2(dy, dx)) % 180 if (dx != 0 or dy != 0) else 0
        primitives.append({
            "primitive_type": "line",
            "x0": px0, "y0": py0, "x1": px1, "y1": py1,
            "cx": (px0 + px1) / 2, "cy": (py0 + py1) / 2,
            "length": length_pt,
            # Pixel-space fields for semantic_pruner
            "cx_px": (lx0 + lx1) / 2,
            "cy_px": (ly0 + ly1) / 2,
            "length_px": math.hypot(lx1 - lx0, ly1 - ly0),
            "angle": angle,
            "source": "srr_full_page_azure",
            "confidence": 0.85,
        })
    return primitives


# ============================================================
# Confidence + cluster uniqueness (Phase 1.3 prep)
# ============================================================

def cluster_uniqueness(primitives, page_extent):
    from prg_v29 import type_primitives, build_relational_graph, mine_admin_motifs
    types_per, grid = type_primitives(primitives, page_extent)
    edges, micro, etypes = build_relational_graph(primitives, types_per, grid)
    motifs = mine_admin_motifs(edges, micro, primitives, types_per)
    if len(motifs) < 2:
        return 10.0  # Lone motif = max uniqueness
    motifs.sort(key=lambda m: -m["size"] * (0.3 + m["alignment_score"]))
    top = motifs[0]["size"] * (0.3 + motifs[0]["alignment_score"])
    sec = motifs[1]["size"] * (0.3 + motifs[1]["alignment_score"])
    return min(top / sec if sec > 0 else 10.0, 10.0)


def confidence_tier(uniqueness, iou, top_score, recall, traj):
    # Relaxed thresholds per Phase 1.2 calibration intent
    if traj == "FOLLOW" and iou >= 0.5 and uniqueness >= 1.3 and (top_score >= 1.0 or recall >= 0.8):
        return "GREEN"
    if traj in ("FOLLOW", "DRIFT_NEAR") and iou >= 0.25 and uniqueness >= 1.05:
        return "YELLOW"
    return "RED"


# ============================================================
# Render
# ============================================================

def render_page_gray(pdf_path, page_idx, dpi):
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_idx]
        pw_pt = page.rect.width
        ph_pt = page.rect.height
        mat = fitz.Matrix(dpi / PDF_NATIVE_DPI, dpi / PDF_NATIVE_DPI)
        pix = page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        return img, pw_pt, ph_pt
    finally:
        doc.close()


# ============================================================
# Per-page pipeline
# ============================================================

def process_page(pdf_path, page_idx):
    t0 = time.time()
    img, pw_pt, ph_pt = render_page_gray(pdf_path, page_idx, OCR_DPI)
    ph_px, pw_px = img.shape

    # OCR
    extractor = get_text_extractor()
    cache_key = compute_cache_key(pdf_path, page_idx, OCR_DPI)
    ocr_result = extractor.extract(img, dpi=OCR_DPI, page_idx=page_idx, cache_key=cache_key)
    if ocr_result.failure_mode:
        return {"page_idx": page_idx, "error": ocr_result.failure_mode,
                "elapsed_ms": (time.time() - t0) * 1000}
    ocr_spans = ocr_result.spans

    t_geom_start = time.time()
    # FULL PAGE primitives (no zone limiting)
    raw_primitives = extract_full_page_primitives(img, OCR_DPI, pw_pt, ph_pt)
    n_raw = len(raw_primitives)
    geom_ms = (time.time() - t_geom_start) * 1000

    # SEMANTIC PRUNE (Phase 1.2 unlock)
    t_prune_start = time.time()
    pruned, prune_stats = semantic_prune(
        raw_primitives, ocr_spans, pw_px, ph_px,
        target_count=TARGET_PRIMITIVES,
    )
    prune_ms = (time.time() - t_prune_start) * 1000

    # Sheet ID
    sheet_id = None
    for s in ocr_spans:
        m = SHEET_ID_RE.search((s.text or "").upper())
        if m:
            sheet_id = m.group(0); break

    # Top-zone semantic score (for tier)
    top_score = sum(
        s.confidence for s in ocr_spans
        if any(t in (s.text or "").upper() for t in POSITIVE_TOKENS)
    )

    if not pruned:
        return {
            "page_idx": page_idx,
            "n_ocr_spans": len(ocr_spans),
            "n_raw_primitives": n_raw,
            "n_pruned": 0,
            "sheet_id": sheet_id,
            "trajectory_class": "NO_PRIMITIVES_AFTER_PRUNE",
            "engine": ocr_result.engine,
            "cost_usd": ocr_result.cost_usd_est,
            "ocr_ms": round(ocr_result.runtime_ms, 1),
            "geom_ms": round(geom_ms, 1),
            "prune_ms": round(prune_ms, 1),
            "elapsed_ms": (time.time() - t0) * 1000,
        }

    for i, p in enumerate(pruned):
        p["primitive_id"] = i

    # PRG + swap
    baseline = run_prg(pruned, (pw_pt, ph_pt))
    if not baseline:
        return {
            "page_idx": page_idx,
            "n_ocr_spans": len(ocr_spans),
            "n_raw_primitives": n_raw,
            "n_pruned": len(pruned),
            "sheet_id": sheet_id,
            "trajectory_class": "NO_BASELINE",
            "engine": ocr_result.engine,
            "cost_usd": ocr_result.cost_usd_est,
            "ocr_ms": round(ocr_result.runtime_ms, 1),
            "geom_ms": round(geom_ms, 1),
            "prune_ms": round(prune_ms, 1),
            "elapsed_ms": (time.time() - t0) * 1000,
        }

    ablated = run_prg(pruned, (pw_pt, ph_pt), exclude_edge_frame=True)
    bbox = baseline["bbox"]; pad = 20.0
    swap_src = (max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                 min(pw_pt, bbox[2] + pad), min(ph_pt, bbox[3] + pad))
    swap_dst = pick_alternate_quadrant(bbox, pw_pt, ph_pt)
    swapped = swap_regions(pruned, pw_pt, ph_pt, swap_src, swap_dst)
    post = run_prg(swapped, (pw_pt, ph_pt))

    bpids = set(baseline.get("primitive_ids", []))
    if post:
        ppids = set(post.get("primitive_ids", []))
        ov = bpids & ppids
        recall = len(ov) / max(len(bpids), 1)
        precision = len(ov) / max(len(ppids), 1)
        sx0, sy0, sx1, sy1 = swap_src
        dx0, dy0, dx1, dy1 = swap_dst
        proj = [bbox[0] + (dx0 - sx0), bbox[1] + (dy0 - sy0),
                 bbox[2] + (dx0 - sx0), bbox[3] + (dy0 - sy0)]
        iou_f = bbox_iou(post["bbox"], proj)
        iou_s = bbox_iou(post["bbox"], bbox)
    else:
        recall = precision = iou_f = iou_s = 0.0

    sg = {"src": list(swap_src), "dst": list(swap_dst)}
    traj = classify_trajectory(baseline, post, sg, recall)
    uniq = cluster_uniqueness(pruned, (pw_pt, ph_pt))
    tier = confidence_tier(uniq, iou_f, top_score, recall, traj)

    return {
        "page_idx": page_idx,
        "engine": ocr_result.engine,
        "cost_usd": ocr_result.cost_usd_est,
        "ocr_ms": round(ocr_result.runtime_ms, 1),
        "ocr_cached": ocr_result.cached,
        "geom_ms": round(geom_ms, 1),
        "prune_ms": round(prune_ms, 1),
        "n_ocr_spans": len(ocr_spans),
        "n_raw_primitives": n_raw,
        "n_pruned": len(pruned),
        "n_pos_tokens": prune_stats.get("n_pos_tokens", 0),
        "n_sheet_id_tokens": prune_stats.get("n_sheet_id_tokens", 0),
        "n_dim_tokens": prune_stats.get("n_dimension_tokens", 0),
        "score_median": prune_stats.get("score_median", 0),
        "sheet_id": sheet_id,
        "trajectory_class": traj,
        "follows_content": traj == "FOLLOW",
        "stays_at_position": traj == "STAY",
        "primitive_recall": round(recall, 3),
        "primitive_precision": round(precision, 3),
        "iou_follow": round(iou_f, 3),
        "iou_stay": round(iou_s, 3),
        "cluster_uniqueness": round(uniq, 3),
        "top_zone_semantic_score": round(top_score, 3),
        "ablation_survives": ablated is not None,
        "confidence_tier": tier,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
    }


def main():
    backend = os.environ.get("ASPIRE_OCR_BACKEND", "azure_di")
    print(f"=== SRR + Azure DI v2 — Phase 1.2 (backend={backend}) ===\n", flush=True)
    print(f"OCR_DPI={OCR_DPI}  target_primitives_per_page={TARGET_PRIMITIVES}", flush=True)
    print(f"Architecture: full-page extract -> Azure-affinity score -> top-K per bucket\n", flush=True)

    raster_pdfs = sorted(RASTER_CACHE.glob("raster150_*.pdf"))
    if not raster_pdfs:
        print("ERROR: no raster cache"); return

    all_results = []
    per_pdf = defaultdict(lambda: {"baseline_found": 0, "follows": 0,
                                     "sheet_ids": [], "tiers": defaultdict(int),
                                     "total_cost": 0.0, "total_runtime_ms": 0.0})
    traj_counts = defaultdict(int)
    tier_counts = defaultdict(int)
    follows = 0; n_baselines = 0; ablation_survives = 0
    total_cost = 0.0; total_runtime = 0.0
    primitives_raw_sum = 0; primitives_pruned_sum = 0
    latencies = []
    recall_sum = precision_sum = iou_sum = 0.0

    for pdf in raster_pdfs:
        print(f"=== {pdf.name} ===", flush=True)
        doc = fitz.open(str(pdf))
        n_pages = doc.page_count; doc.close()
        n_test = min(n_pages, 6)
        for pg in range(n_test):
            try:
                r = process_page(pdf, pg)
                all_results.append({"pdf_name": pdf.name, **r})
                if "error" in r:
                    print(f"  page {pg}: ERROR {r['error']}", flush=True); continue
                traj = r.get("trajectory_class", "?")
                traj_counts[traj] += 1
                cost = r.get("cost_usd", 0)
                runtime = r.get("elapsed_ms", 0)
                total_cost += cost
                total_runtime += runtime
                latencies.append(runtime)
                primitives_raw_sum += r.get("n_raw_primitives", 0)
                primitives_pruned_sum += r.get("n_pruned", 0)
                tier = r.get("confidence_tier", "RED")
                tier_counts[tier] += 1
                per_pdf[pdf.name]["total_cost"] += cost
                per_pdf[pdf.name]["total_runtime_ms"] += runtime
                if traj not in ("NO_PRIMITIVES_AFTER_PRUNE", "NO_BASELINE"):
                    n_baselines += 1
                    per_pdf[pdf.name]["baseline_found"] += 1
                    if r.get("ablation_survives"):
                        ablation_survives += 1
                    if r.get("follows_content"):
                        follows += 1
                        per_pdf[pdf.name]["follows"] += 1
                    recall_sum += r.get("primitive_recall", 0)
                    precision_sum += r.get("primitive_precision", 0)
                    iou_sum += r.get("iou_follow", 0)
                if r.get("sheet_id"):
                    per_pdf[pdf.name]["sheet_ids"].append(r["sheet_id"])
                per_pdf[pdf.name]["tiers"][tier] += 1
                print(f"  page {pg}: spans={r.get('n_ocr_spans',0):3d} "
                      f"raw={r.get('n_raw_primitives',0):5d}->kept={r.get('n_pruned',0):4d} "
                      f"sheet={r.get('sheet_id')}  "
                      f"traj={traj:<14s} r={r.get('primitive_recall',0):.2f} "
                      f"p={r.get('primitive_precision',0):.2f} iou={r.get('iou_follow',0):.2f} "
                      f"u={r.get('cluster_uniqueness',0):.2f}  {tier}  "
                      f"{runtime:.0f}ms (ocr={r.get('ocr_ms',0):.0f} prune={r.get('prune_ms',0):.0f})",
                      flush=True)
            except Exception as exc:
                import traceback; traceback.print_exc()
                print(f"  page {pg}: EXC {type(exc).__name__}: {str(exc)[:100]}", flush=True)

    total = sum(1 for r in all_results if "error" not in r)
    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)]
    else:
        p50 = p95 = 0

    print(f"\n=== Phase 1.2 — Semantic Pruner v2 Benchmark ===")
    print(f"\ntotal pages: {total}")
    print(f"primitives RAW (avg):       {primitives_raw_sum/max(total,1):.0f}/page")
    print(f"primitives PRUNED (avg):    {primitives_pruned_sum/max(total,1):.0f}/page  "
          f"(target 500-1500, was 124 in v1)")
    print(f"runtime: avg={total_runtime/max(total,1):.0f}ms  p50={p50:.0f}ms  p95={p95:.0f}ms  (target <5000)")
    print(f"total cost: ${total_cost:.4f}  ({total_cost/max(total,1)*100:.2f}¢/page)")

    avg_recall = recall_sum / max(n_baselines, 1)
    avg_precision = precision_sum / max(n_baselines, 1)
    avg_iou = iou_sum / max(n_baselines, 1)
    follows_rate = follows / max(n_baselines, 1)

    print(f"\n--- TRAJECTORY ---")
    for cls in ("FOLLOW","STAY","DRIFT_NEAR","SPLIT_CLUSTER","THIRD_REGION",
                 "NULL_COLLAPSE","NO_BASELINE","NO_PRIMITIVES_AFTER_PRUNE"):
        c = traj_counts.get(cls, 0)
        if c > 0:
            print(f"  {cls:<30s} {c:3d}  ({c/max(total,1)*100:.1f}%)")

    print(f"\n--- ACCEPTANCE GATES (Phase 1.2) ---")
    print(f"  runtime p95:        {p95:.0f}ms (target <5000)  {'PASS' if p95 < 5000 else 'FAIL'}")
    print(f"  prims/page in range: {primitives_pruned_sum/max(total,1):.0f}  (target 500-1500)  "
          f"{'PASS' if 500 <= primitives_pruned_sum/max(total,1) <= 1500 else 'FAIL'}")
    print(f"  follows:            {follows_rate*100:5.1f}%")
    print(f"  recall:             {avg_recall:.3f}")
    print(f"  precision:          {avg_precision:.3f}")
    print(f"  IoU(follow):        {avg_iou:.3f}")
    print(f"  ablation:           {ablation_survives}/{n_baselines}")

    print(f"\n--- CONFIDENCE TIERS ---")
    for t in ("GREEN","YELLOW","RED"):
        c = tier_counts.get(t, 0)
        print(f"  {t:<8s} {c:3d}  ({c/max(total,1)*100:.1f}%)")
    print(f"  GREEN present: {'YES' if tier_counts.get('GREEN',0) > 0 else 'NO (tier calibration check)'}")
    print(f"  RED dominance: {tier_counts.get('RED',0)/max(total,1)*100:.0f}%  (target <50%)")

    print(f"\n--- PER-PDF ---")
    for pdf_name, stats in sorted(per_pdf.items()):
        ids = ", ".join(set(stats["sheet_ids"]))[:60]
        avg_rt = stats["total_runtime_ms"] / max(stats["baseline_found"], 1)
        marker = " <-- FOLLOWS" if stats["follows"] > 0 else ""
        print(f"  {pdf_name[:42]:<42s}  base={stats['baseline_found']}  follows={stats['follows']}  "
              f"avg={avg_rt:.0f}ms  ${stats['total_cost']:.4f}  sheets=[{ids}]{marker}")

    out_path = OUT_DIR / "srr_azure_v2_phase1.2_2026-05-25.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "srr_azure_v2_phase1.2",
            "backend": backend,
            "target_primitives": TARGET_PRIMITIVES,
            "total_pages": total,
            "avg_raw_primitives": int(primitives_raw_sum / max(total, 1)),
            "avg_pruned_primitives": int(primitives_pruned_sum / max(total, 1)),
            "follows_rate": round(follows_rate, 4),
            "avg_recall": round(avg_recall, 4),
            "avg_precision": round(avg_precision, 4),
            "avg_iou": round(avg_iou, 4),
            "avg_runtime_ms": round(total_runtime / max(total, 1), 1),
            "p50_runtime_ms": p50, "p95_runtime_ms": p95,
            "total_cost_usd": round(total_cost, 4),
            "trajectory": dict(traj_counts),
            "confidence_tiers": dict(tier_counts),
            "per_pdf": {k: {**v, "sheet_ids": list(set(v["sheet_ids"])),
                              "tiers": dict(v["tiers"])} for k, v in per_pdf.items()},
            "per_page": all_results,
        }, f, indent=2)
    print(f"\nsaved: {out_path}", flush=True)


if __name__ == "__main__":
    main()
