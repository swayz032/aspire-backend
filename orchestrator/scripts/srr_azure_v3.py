"""SRR + Azure DI v3 — Phase 1.1.1 latency collapse.

Architecture: parallel OCR + geometry extraction via ThreadPoolExecutor.

Key insight: OCR is IO-bound (HTTP to Azure, 3-5s blocking on poll).
Geometry extraction is CPU-bound (OpenCV Hough/contour, 0.5-1.5s).
They share no state. Run them concurrently.

Pipeline:
  Stage 0:  render image once (~200ms)
  Stage 1a: OCR via Azure DI                 ┐ parallel via ThreadPoolExecutor
  Stage 1b: full-page Hough + contour        ┘ (geometry returns 1-2s before OCR)
  Stage 2:  join — semantic prune using OCR + extracted primitives
  Stage 3:  PRG + swap + cluster uniqueness + tier

Phase 1.1.1 gates:
  - Runtime p95 strictly <5000ms
  - Zero regression on Phase 1.2's 8 passing gates
  - Cost stays $0.0015/page
  - GAVNN holds 6/6 follows
  - Sheet IDs preserved
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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from srr_azure_v2 import (
    extract_full_page_primitives, cluster_uniqueness,
    confidence_tier, render_page_gray,
)

RASTER_CACHE = Path(r"C:\Users\tonio\Projects\myapp\blueprints\raster_r9")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")
PDF_NATIVE_DPI = 72.0
OCR_DPI = 150.0  # REVERTED: 100dpi regressed FOLLOW 95.8%→79.2%, GAVNN 6/6→4/6
# Phase 1.1.1 attempt 2 findings (2026-05-25):
#   - 100dpi parallel: avg/p50 improved (4645→3707, 4894→3571) but p95 7072ms (REGRESSED 390ms)
#   - Quality regression: SPLIT_CLUSTER 4.2%→16.7%, GAVNN 6/6→4/6 follows
#   - Sheet ID drift: D.2 disappeared, novel X.00/X12/A.1 appeared (partial OCR)
#   - OCR-only parallelism savings: ~700ms typical (negligible vs OCR 3-7s pole)
# Phase 1.1.1 status: p95 gate REMAINS UNCLOSED. Options for next iteration:
#   A. Azure prebuilt-layout vs prebuilt-read (could be slower OR faster, unknown)
#   B. Per-page DPI selection (100 if simple, 150 if dense — needs page classifier)
#   C. Azure batch async (queue multiple pages, parallel cloud-side)
#   D. Accept Phase 1.2 8/9 ship and proceed to Phase 1.3 governance
TARGET_PRIMITIVES = 1000

# Single shared thread pool — initialize once
_OCR_GEOM_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="srr-parallel")


def process_page_parallel(pdf_path, page_idx):
    """v3: OCR + geometry extraction run concurrently."""
    t0 = time.time()
    img, pw_pt, ph_pt = render_page_gray(pdf_path, page_idx, OCR_DPI)
    ph_px, pw_px = img.shape
    render_ms = (time.time() - t0) * 1000

    # === Parallel launch ===
    extractor = get_text_extractor()
    cache_key = compute_cache_key(pdf_path, page_idx, OCR_DPI)

    t_parallel = time.time()
    ocr_future = _OCR_GEOM_POOL.submit(
        extractor.extract, img,
        dpi=OCR_DPI, page_idx=page_idx, cache_key=cache_key,
    )
    geom_future = _OCR_GEOM_POOL.submit(
        extract_full_page_primitives, img, OCR_DPI, pw_pt, ph_pt,
    )

    # Wait for both
    ocr_result = ocr_future.result()
    raw_primitives = geom_future.result()
    parallel_ms = (time.time() - t_parallel) * 1000

    # Failure check
    if ocr_result.failure_mode:
        return {"page_idx": page_idx, "error": ocr_result.failure_mode,
                "elapsed_ms": (time.time() - t0) * 1000}

    ocr_spans = ocr_result.spans
    n_raw = len(raw_primitives)

    # === Sequential: prune + PRG (both depend on the parallel outputs) ===
    t_prune = time.time()
    pruned, prune_stats = semantic_prune(
        raw_primitives, ocr_spans, pw_px, ph_px,
        target_count=TARGET_PRIMITIVES,
    )
    prune_ms = (time.time() - t_prune) * 1000

    sheet_id = None
    for s in ocr_spans:
        m = SHEET_ID_RE.search((s.text or "").upper())
        if m:
            sheet_id = m.group(0); break

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
            "render_ms": round(render_ms, 1),
            "parallel_ms": round(parallel_ms, 1),
            "ocr_ms": round(ocr_result.runtime_ms, 1),
            "prune_ms": round(prune_ms, 1),
            "elapsed_ms": (time.time() - t0) * 1000,
        }

    for i, p in enumerate(pruned):
        p["primitive_id"] = i

    t_prg = time.time()
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
            "render_ms": round(render_ms, 1),
            "parallel_ms": round(parallel_ms, 1),
            "ocr_ms": round(ocr_result.runtime_ms, 1),
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
        proj = [bbox[0]+(dx0-sx0), bbox[1]+(dy0-sy0),
                 bbox[2]+(dx0-sx0), bbox[3]+(dy0-sy0)]
        iou_f = bbox_iou(post["bbox"], proj)
    else:
        recall = precision = iou_f = 0.0

    sg = {"src": list(swap_src), "dst": list(swap_dst)}
    traj = classify_trajectory(baseline, post, sg, recall)
    uniq = cluster_uniqueness(pruned, (pw_pt, ph_pt))
    tier = confidence_tier(uniq, iou_f, top_score, recall, traj)
    prg_ms = (time.time() - t_prg) * 1000

    return {
        "page_idx": page_idx,
        "engine": ocr_result.engine,
        "cost_usd": ocr_result.cost_usd_est,
        "render_ms": round(render_ms, 1),
        "parallel_ms": round(parallel_ms, 1),
        "ocr_ms": round(ocr_result.runtime_ms, 1),
        "ocr_cached": ocr_result.cached,
        "prune_ms": round(prune_ms, 1),
        "prg_ms": round(prg_ms, 1),
        "n_ocr_spans": len(ocr_spans),
        "n_raw_primitives": n_raw,
        "n_pruned": len(pruned),
        "sheet_id": sheet_id,
        "trajectory_class": traj,
        "follows_content": traj == "FOLLOW",
        "stays_at_position": traj == "STAY",
        "primitive_recall": round(recall, 3),
        "primitive_precision": round(precision, 3),
        "iou_follow": round(iou_f, 3),
        "cluster_uniqueness": round(uniq, 3),
        "ablation_survives": ablated is not None,
        "confidence_tier": tier,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
    }


def main():
    print(f"=== SRR + Azure DI v3 — Phase 1.1.1 (parallel OCR + geom) ===\n", flush=True)
    print(f"OCR_DPI={OCR_DPI}  target_primitives={TARGET_PRIMITIVES}")
    print(f"Architecture: parallel (OCR || geometry) -> prune -> PRG\n", flush=True)

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
    parallel_savings_sum = 0  # ocr_ms vs (parallel_ms - geom-only-time-estimate)

    for pdf in raster_pdfs:
        print(f"=== {pdf.name} ===", flush=True)
        doc = fitz.open(str(pdf))
        n_pages = doc.page_count; doc.close()
        n_test = min(n_pages, 6)
        for pg in range(n_test):
            try:
                r = process_page_parallel(pdf, pg)
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
                # Parallel efficiency: parallel_ms should be ≈ max(ocr_ms, geom_ms),
                # not sum. Savings vs v2 = ocr_ms - (parallel_ms - render_ms)
                print(f"  page {pg}: spans={r.get('n_ocr_spans',0):3d} "
                      f"raw={r.get('n_raw_primitives',0):5d}->kept={r.get('n_pruned',0):4d} "
                      f"sheet={r.get('sheet_id')}  "
                      f"traj={traj:<14s} r={r.get('primitive_recall',0):.2f} "
                      f"p={r.get('primitive_precision',0):.2f} iou={r.get('iou_follow',0):.2f} "
                      f"u={r.get('cluster_uniqueness',0):.2f}  {tier}  "
                      f"{runtime:.0f}ms (render={r.get('render_ms',0):.0f} "
                      f"parallel(OCR||geom)={r.get('parallel_ms',0):.0f} "
                      f"ocr={r.get('ocr_ms',0):.0f} prune={r.get('prune_ms',0):.0f})",
                      flush=True)
            except Exception as exc:
                import traceback; traceback.print_exc()
                print(f"  page {pg}: EXC {type(exc).__name__}: {str(exc)[:100]}", flush=True)

    total = sum(1 for r in all_results if "error" not in r)
    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies)//2]
        p95 = latencies[min(int(len(latencies)*0.95), len(latencies)-1)]
    else:
        p50 = p95 = 0

    print(f"\n=== Phase 1.1.1 — Parallel OCR+Geom Benchmark ===")
    print(f"\ntotal pages: {total}")
    print(f"primitives PRUNED avg: {primitives_pruned_sum/max(total,1):.0f}/page")
    print(f"runtime: avg={total_runtime/max(total,1):.0f}ms  p50={p50:.0f}ms  p95={p95:.0f}ms")
    print(f"cost: ${total_cost:.4f}  ({total_cost/max(total,1)*100:.2f}c/page)")
    print(f"\nReference (Phase 1.2): avg=4645ms  p50=4894ms  p95=6682ms")
    p95_v2 = 6682
    p95_delta = p95 - p95_v2
    p95_pct = (p95_v2 - p95) / p95_v2 * 100 if p95 < p95_v2 else 0
    print(f"p95 change: {int(p95_delta):+d}ms ({p95_pct:+.0f}% faster)" if p95_delta < 0 else f"p95 change: {int(p95_delta):+d}ms (REGRESSED)")

    print(f"\n--- TRAJECTORY ---")
    for cls in ("FOLLOW","STAY","DRIFT_NEAR","SPLIT_CLUSTER","THIRD_REGION",
                 "NULL_COLLAPSE","NO_BASELINE","NO_PRIMITIVES_AFTER_PRUNE"):
        c = traj_counts.get(cls, 0)
        if c > 0:
            print(f"  {cls:<30s} {c:3d}  ({c/max(total,1)*100:.1f}%)")

    avg_recall = recall_sum / max(n_baselines, 1)
    avg_precision = precision_sum / max(n_baselines, 1)
    avg_iou = iou_sum / max(n_baselines, 1)
    follows_rate = follows / max(n_baselines, 1)

    prims_avg = primitives_pruned_sum / max(total, 1)
    gates = {
        "p95<5000": p95 < 5000,
        "prims_500_1500": 500 <= prims_avg <= 1500,
        "follows>=70%": follows_rate >= 0.70,
        "recall>=0.7": avg_recall >= 0.70,
        "precision>=0.7": avg_precision >= 0.70,
        "IoU>=0.5": avg_iou >= 0.50,
        "GREEN_present": tier_counts.get("GREEN", 0) > 0,
        "RED<50%": tier_counts.get("RED", 0) / max(total, 1) < 0.50,
        "ablation_100%": ablation_survives == n_baselines and n_baselines > 0,
    }
    print(f"\n--- ACCEPTANCE GATES (Phase 1.1.1) ---")
    for name, ok in gates.items():
        print(f"  {name:<22s} {'PASS' if ok else 'FAIL'}")
    n_pass = sum(1 for v in gates.values() if v)
    print(f"\n{n_pass} of {len(gates)} gates pass")

    print(f"\n--- CONFIDENCE TIERS ---")
    for t in ("GREEN","YELLOW","RED"):
        c = tier_counts.get(t, 0)
        print(f"  {t:<8s} {c:3d}  ({c/max(total,1)*100:.1f}%)")

    print(f"\n--- PER-PDF ---")
    for pdf_name, stats in sorted(per_pdf.items()):
        ids = ", ".join(set(stats["sheet_ids"]))[:60]
        avg_rt = stats["total_runtime_ms"] / max(stats["baseline_found"], 1)
        marker = " <-- FOLLOWS" if stats["follows"] > 0 else ""
        print(f"  {pdf_name[:42]:<42s}  base={stats['baseline_found']}  "
              f"follows={stats['follows']}  avg={avg_rt:.0f}ms  "
              f"${stats['total_cost']:.4f}  sheets=[{ids}]{marker}")

    out_path = OUT_DIR / "srr_azure_v3_phase1.1.1_2026-05-25.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "srr_azure_v3_phase1.1.1",
            "total_pages": total,
            "avg_pruned_primitives": int(prims_avg),
            "follows_rate": round(follows_rate, 4),
            "avg_recall": round(avg_recall, 4),
            "avg_precision": round(avg_precision, 4),
            "avg_iou": round(avg_iou, 4),
            "avg_runtime_ms": round(total_runtime/max(total,1), 1),
            "p50_runtime_ms": p50, "p95_runtime_ms": p95,
            "total_cost_usd": round(total_cost, 4),
            "trajectory": dict(traj_counts),
            "confidence_tiers": dict(tier_counts),
            "gates_passed": n_pass,
            "gates_total": len(gates),
            "gates": gates,
            "per_pdf": {k: {**v, "sheet_ids": list(set(v["sheet_ids"])),
                              "tiers": dict(v["tiers"])} for k, v in per_pdf.items()},
            "per_page": all_results,
        }, f, indent=2)
    print(f"\nsaved: {out_path}", flush=True)
    _OCR_GEOM_POOL.shutdown(wait=False)


if __name__ == "__main__":
    main()
