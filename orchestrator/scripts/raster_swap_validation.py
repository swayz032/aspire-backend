"""Build 1 — Raster Swap Validation.

CRITICAL DISCIPLINE:
  Last night Raster Bridge v1 produced 100% baseline_found on raster pages.
  That number alone does NOT prove semantic transport — PRG could be finding
  noise clusters that happen to pass min_motif_size.

  This script runs the SAME adversarial swap-content test that earned the
  vector breakthrough (r8: 89.1% follows, 0% stays) against synthetic
  primitives from Raster Bridge v1.

What we measure:
  trajectory taxonomy (FOLLOW / STAY / DRIFT_NEAR / SPLIT_CLUSTER / THIRD_REGION / NULL_COLLAPSE)
  primitive_recall (causal — did the same primitives end up in winning cluster after swap?)
  primitive_precision (cluster purity)
  IoU (cluster boundary fidelity)
  border ablation survival (semantic vs position-prior test)

Three honest outcomes:
  follows >= 40%  -> Raster bridge produces real semantic transport (proceed Build 2-5)
  follows 10-40%  -> Noisy but signal present (Build 2 pruning critical before claim)
  follows <  10%  -> Noise clusters (OCR anchors required before semantic claim)

No PRG modification. Bridge unchanged. Just running validation that was deferred.
"""
from __future__ import annotations

import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2  # noqa: F401 (used by raster_bridge_v1)

sys.path.insert(0, str(Path(__file__).parent))
from raster_bridge_v1 import reconstruct_page, RASTER_CACHE  # noqa: E402
from prg_v29 import (  # noqa: E402
    run_prg, swap_regions, pick_alternate_quadrant,
    bbox_iou, classify_trajectory, in_canonical_zone,
)

OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")


def raster_swap_test(pdf_path: Path, page_idx: int, dpi: float = 150) -> dict:
    """Run the full adversarial swap-content protocol against raster-reconstructed primitives.
    Mirrors prg_v29.process_page exactly, but uses Raster Bridge primitives instead of vector.
    """
    t0 = time.time()
    primitives, (pw, ph), bridge_telem = reconstruct_page(pdf_path, page_idx, dpi=dpi)
    if not primitives:
        return {
            "page_idx": page_idx,
            "error": "no synthetic primitives",
            "bridge_failure": bridge_telem.failure_mode,
        }
    # Assign primitive_ids before any processing (mirrors prg_v29.process_page)
    for i, p in enumerate(primitives):
        p["primitive_id"] = i

    result = {"page_idx": page_idx,
              "n_primitives": len(primitives),
              "bridge_elapsed_ms": round(bridge_telem.elapsed_ms, 1)}

    # Baseline detection
    baseline = run_prg(primitives, (pw, ph))
    if not baseline:
        result["baseline_found"] = False
        result["trajectory_class"] = "NO_BASELINE"
        return result
    result["baseline_found"] = True
    bc = baseline["centroid"]
    result["baseline_centroid"] = [round(c, 1) for c in bc]
    result["baseline_in_canonical"] = in_canonical_zone(bc[0], bc[1], pw, ph)
    result["baseline_size"] = baseline["n_primitives"]
    result["baseline_emergence"] = round(baseline["emergence_score"], 2)

    # Border ablation
    ablated = run_prg(primitives, (pw, ph), exclude_edge_frame=True)
    if ablated:
        result["ablation_survives"] = True
        d = math.hypot(
            ablated["centroid"][0] - bc[0],
            ablated["centroid"][1] - bc[1],
        )
        result["ablation_stable"] = d < 60
    else:
        result["ablation_survives"] = False
        result["ablation_stable"] = False

    # Adaptive swap test (same as vector path)
    bbox = baseline["bbox"]
    pad = 20.0
    swap_src = (max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                 min(pw, bbox[2] + pad), min(ph, bbox[3] + pad))
    swap_dst = pick_alternate_quadrant(bbox, pw, ph)
    swap_geometry = {"src": [round(x, 1) for x in swap_src],
                      "dst": [round(x, 1) for x in swap_dst]}
    swapped = swap_regions(primitives, pw, ph, swap_src, swap_dst)
    post_swap = run_prg(swapped, (pw, ph))

    # Provenance: primitive_recall + primitive_precision
    baseline_pids = set(baseline.get("primitive_ids", []))
    if post_swap:
        post_swap_pids = set(post_swap.get("primitive_ids", []))
        overlap_pids = baseline_pids & post_swap_pids
        recall = len(overlap_pids) / max(len(baseline_pids), 1)
        precision = len(overlap_pids) / max(len(post_swap_pids), 1)
        result["primitive_recall"] = round(recall, 3)
        result["primitive_precision"] = round(precision, 3)
        result["contamination_share"] = round(
            len(post_swap_pids - baseline_pids) / max(len(post_swap_pids), 1), 3
        )
    else:
        result["primitive_recall"] = 0.0
        result["primitive_precision"] = 0.0
        result["contamination_share"] = 0.0

    # IoU with TL-to-TL projected destination
    if post_swap:
        sx0, sy0, sx1, sy1 = swap_geometry["src"]
        dx0, dy0, dx1, dy1 = swap_geometry["dst"]
        dxo = dx0 - sx0
        dyo = dy0 - sy0
        projected_bbox = [bbox[0] + dxo, bbox[1] + dyo, bbox[2] + dxo, bbox[3] + dyo]
        result["iou_follow"] = round(bbox_iou(post_swap["bbox"], projected_bbox), 3)
        result["iou_stay"] = round(bbox_iou(post_swap["bbox"], bbox), 3)
    else:
        result["iou_follow"] = 0.0
        result["iou_stay"] = 0.0

    # Trajectory classification
    traj = classify_trajectory(baseline, post_swap, swap_geometry,
                                 result["primitive_recall"])
    result["trajectory_class"] = traj
    result["follows_content"] = traj == "FOLLOW"
    result["stays_at_position"] = traj == "STAY"
    result["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
    return result


def main():
    print("=== Build 1: Raster Swap Validation ===\n", flush=True)
    print("Testing whether Raster Bridge v1's synthetic primitives produce\n"
          "real semantic transport under adversarial swap (the test that earned vector r8).\n", flush=True)
    print("Vector r8 reference: 89.1% follows, 2.3% stays, recall=0.84, precision=0.81, IoU=0.79\n", flush=True)

    raster_pdfs = sorted(RASTER_CACHE.glob("raster150_*.pdf"))
    if not raster_pdfs:
        print("ERROR: no rasterized cache. Run prg_r9_raster_falsification.py first.", flush=True)
        return

    all_results = []
    per_pdf_follows = defaultdict(lambda: {"valid": 0, "follows": 0, "stays": 0, "baseline_found": 0})

    for pdf in raster_pdfs:
        print(f"=== {pdf.name} ===", flush=True)
        import pymupdf as fitz
        doc = fitz.open(str(pdf))
        n_pages = doc.page_count
        doc.close()
        n_test = min(n_pages, 6)  # cap per PDF
        for pg in range(n_test):
            try:
                r = raster_swap_test(pdf, pg, dpi=150)
                all_results.append({"pdf_name": pdf.name, **r})
                if "error" in r:
                    print(f"  page {pg}: ERROR {r['error']}", flush=True)
                    continue
                if r.get("baseline_found"):
                    per_pdf_follows[pdf.name]["baseline_found"] += 1
                    per_pdf_follows[pdf.name]["valid"] += 1
                    if r.get("follows_content"):
                        per_pdf_follows[pdf.name]["follows"] += 1
                    if r.get("stays_at_position"):
                        per_pdf_follows[pdf.name]["stays"] += 1
                print(
                    f"  page {pg}: prims={r.get('n_primitives', 0):5d}  "
                    f"base={r.get('baseline_found', False)}  "
                    f"traj={r.get('trajectory_class', '?'):<14s} "
                    f"recall={r.get('primitive_recall', 0):.2f}  "
                    f"prec={r.get('primitive_precision', 0):.2f}  "
                    f"iou_F={r.get('iou_follow', 0):.2f}  "
                    f"ablate={r.get('ablation_survives', False)}  "
                    f"{r.get('elapsed_ms', 0):.0f}ms",
                    flush=True,
                )
            except Exception as exc:
                print(f"  page {pg}: EXCEPTION {type(exc).__name__}: {str(exc)[:80]}",
                       flush=True)
                all_results.append({"pdf_name": pdf.name, "page_idx": pg,
                                     "error": str(exc)[:200]})

    # === AGGREGATE ANALYSIS ===
    print("\n=== Build 1 VERDICT — RASTER SWAP VALIDATION ===", flush=True)
    traj_counts = defaultdict(int)
    base_found = 0
    ablation_survives = 0
    follows = 0
    stays = 0
    recall_sum = 0.0
    precision_sum = 0.0
    iou_sum = 0.0
    n_baselines = 0
    for r in all_results:
        if "error" in r:
            continue
        if r.get("baseline_found"):
            base_found += 1
            n_baselines += 1
            if r.get("ablation_survives"):
                ablation_survives += 1
            traj = r.get("trajectory_class")
            if traj:
                traj_counts[traj] += 1
            if r.get("follows_content"):
                follows += 1
            if r.get("stays_at_position"):
                stays += 1
            recall_sum += r.get("primitive_recall", 0)
            precision_sum += r.get("primitive_precision", 0)
            iou_sum += r.get("iou_follow", 0)
    total_pages = sum(1 for r in all_results if "error" not in r)

    print(f"\ntotal pages tested:    {total_pages}")
    print(f"baseline_found:        {base_found}  ({base_found/max(total_pages,1)*100:.1f}%)")
    print(f"ablation_survives:     {ablation_survives}  ({ablation_survives/max(base_found,1)*100:.1f}% of found)")

    print(f"\n--- TRAJECTORY TAXONOMY ---")
    for cls in ("FOLLOW", "STAY", "DRIFT_NEAR", "SPLIT_CLUSTER", "THIRD_REGION",
                 "NULL_COLLAPSE", "NO_BASELINE"):
        c = traj_counts.get(cls, 0)
        pct = c / max(base_found, 1) * 100 if cls != "NO_BASELINE" else c / max(total_pages, 1) * 100
        print(f"  {cls:<18s} {c:3d}  ({pct:.1f}%)")

    print(f"\n--- IoU-based GATES ---")
    follows_rate = follows / max(base_found, 1)
    stays_rate = stays / max(base_found, 1)
    avg_recall = recall_sum / max(n_baselines, 1)
    avg_precision = precision_sum / max(n_baselines, 1)
    avg_iou = iou_sum / max(n_baselines, 1)
    print(f"  follows_content:     {follows_rate*100:.1f}%  (vector r8: 89.1%)")
    print(f"  stays_at_position:   {stays_rate*100:.1f}%  (vector r8: 2.3%)")
    print(f"  avg recall:          {avg_recall:.3f}  (vector r8: 0.84)")
    print(f"  avg precision:       {avg_precision:.3f}  (vector r8: 0.81)")
    print(f"  avg IoU:             {avg_iou:.3f}  (vector r8: 0.79)")

    print(f"\n--- PER-PDF ---")
    pdfs_with_follows = 0
    for pdf_name, stats in sorted(per_pdf_follows.items()):
        marker = " <-- FOLLOWS" if stats["follows"] > 0 else ""
        if stats["follows"] > 0:
            pdfs_with_follows += 1
        print(f"  {pdf_name[:45]:<45s}  base={stats['baseline_found']}  "
              f"follows={stats['follows']}  stays={stats['stays']}{marker}")
    print(f"\nMISSION GATE: {pdfs_with_follows} distinct raster PDFs follow content  "
          f"(target >=3, available =4)")

    print("\n=== VERDICT ===")
    if follows_rate >= 0.40:
        print(f"REAL semantic transport on raster path ({follows_rate*100:.1f}% follows).")
        print(f"Raster Bridge v1 produces primitives that PRG can semantically follow.")
        print(f"Proceed to Build 2 (primitive pruner) to optimize, then Build 3 (OCR).")
    elif follows_rate >= 0.10:
        print(f"PARTIAL semantic transport on raster ({follows_rate*100:.1f}% follows).")
        print(f"Signal present but noise dominates. Build 2 pruning is now critical.")
        print(f"Cannot claim raster production-readiness until Build 2 raises follow rate.")
    else:
        print(f"FALSIFIED — raster bridge produces noise clusters, not semantic transport.")
        print(f"baseline_found of 100% was misleading. PRG finds A cluster, but it's not the title block.")
        print(f"Re-order: install OCR anchors (Build 3) BEFORE re-running swap test.")
    print(f"\nDistinct PDFs with follows>0: {pdfs_with_follows} / 4")

    out_path = OUT_DIR / "build1_raster_swap_validation_2026-05-25.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "build": "1_raster_swap_validation",
            "total_pages": total_pages,
            "baseline_found": base_found,
            "follows_content": follows,
            "stays_at_position": stays,
            "ablation_survives": ablation_survives,
            "avg_recall": round(avg_recall, 4),
            "avg_precision": round(avg_precision, 4),
            "avg_iou_follow": round(avg_iou, 4),
            "trajectory_counts": dict(traj_counts),
            "per_pdf": dict(per_pdf_follows),
            "pdfs_with_follows": pdfs_with_follows,
            "per_page": all_results,
        }, f, indent=2)
    print(f"\nsaved: {out_path}", flush=True)


if __name__ == "__main__":
    main()
