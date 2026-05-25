"""Build 2 — Primitive Noise Pruner.

Problem: Raster Bridge v1 emits 16,999 primitives/page (vs vector ~3,000).
Most of the excess is text-outline noise, duplicate Hough segments, tiny
contour fragments. Build 1 showed this drops PRG quality:
  recall   0.84 -> 0.48 on raster
  IoU      0.79 -> 0.48
  runtime  ~0.3s -> 75s on dense pages
  SPLIT_CLUSTER dominates (25%) because too many candidate motifs

Solution: score every primitive on structural quality, keep top-K per
spatial bucket. Drop noise floor. Preserve structural backbone.

This is NOT a PRG change. It's a pre-PRG filter.

Score components:
  + length_score          (long lines are more likely structural)
  + orthogonality_score   (axis-aligned lines are title-block-relevant)
  + intersection_score    (lines that meet other lines form structure)
  + alignment_score       (lines that share row/column with others = table)
  - tiny_segment_penalty  (lines < 6pt are usually noise)
  - duplicate_penalty     (near-identical line nearby = redundant)
  - isolated_penalty      (no neighbors within 50pt = orphan)

Output: filtered primitive list in same PRG schema.
Target: ~4,000 primitives/page (matches vector density PRG was tuned on).
"""
from __future__ import annotations

import math
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


# ============================================================
# Scoring components
# ============================================================

def angle_of(p):
    try:
        dx = float(p["x1"]) - float(p["x0"])
        dy = float(p["y1"]) - float(p["y0"])
    except (KeyError, TypeError, ValueError):
        return None
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return None
    return math.degrees(math.atan2(dy, dx)) % 180


def is_orthogonal(angle, tol=5.0):
    if angle is None:
        return False
    return angle <= tol or angle >= 180 - tol or abs(angle - 90) <= tol


def build_spatial_grid(prims, cell=30.0):
    g = defaultdict(list)
    for i, p in enumerate(prims):
        try:
            tx = int(float(p["cx"]) // cell)
            ty = int(float(p["cy"]) // cell)
            g[(tx, ty)].append(i)
        except (KeyError, TypeError, ValueError):
            pass
    return g


def neighbors_within(prim_idx, prims, grid, radius, cell=30.0):
    p = prims[prim_idx]
    try:
        cx, cy = float(p["cx"]), float(p["cy"])
    except (KeyError, TypeError, ValueError):
        return []
    n = int(math.ceil(radius / cell))
    tx, ty = int(cx // cell), int(cy // cell)
    out = []
    for dx in range(-n, n + 1):
        for dy in range(-n, n + 1):
            for ni in grid.get((tx + dx, ty + dy), []):
                if ni == prim_idx:
                    continue
                np_ = prims[ni]
                try:
                    if math.hypot(float(np_["cx"]) - cx, float(np_["cy"]) - cy) <= radius:
                        out.append(ni)
                except (KeyError, TypeError, ValueError):
                    pass
    return out


def score_primitive(idx, prims, grid):
    """Composite structural-quality score. Higher = more likely to be structural."""
    p = prims[idx]
    length = float(p.get("length", 0))
    angle = angle_of(p)
    score = 0.0

    # Length score: prefer 8-200pt
    if 8 < length < 200:
        score += min(length / 30.0, 2.5)  # caps at 2.5
    elif length >= 200:
        score += 2.0  # long structural lines (page borders, walls)

    # Orthogonality
    if is_orthogonal(angle):
        score += 1.5

    # Tiny segment penalty
    if length < 6:
        score -= 2.0

    # Neighborhood structure (within 40pt)
    nbrs = neighbors_within(idx, prims, grid, 40.0)

    # Isolation penalty
    if len(nbrs) < 2:
        score -= 1.5
    elif len(nbrs) >= 4:
        score += 0.5  # connected to structure

    # Intersection score — count orthogonal-direction neighbors (likely intersections)
    if angle is not None:
        ortho_partners = 0
        for ni in nbrs:
            na = angle_of(prims[ni])
            if na is None:
                continue
            angle_diff = min(abs(na - angle), 180 - abs(na - angle))
            if 80 <= angle_diff <= 100:  # perpendicular
                ortho_partners += 1
                if ortho_partners >= 3:
                    break
        score += min(ortho_partners, 3) * 0.6

    # Duplicate penalty — count near-identical lines (same angle, very close)
    if angle is not None:
        dupes = 0
        try:
            cx, cy = float(p["cx"]), float(p["cy"])
        except (KeyError, TypeError, ValueError):
            cx = cy = 0
        for ni in nbrs:
            np_ = prims[ni]
            na = angle_of(np_)
            if na is None:
                continue
            if abs(na - angle) > 3:
                continue
            try:
                ncx, ncy = float(np_["cx"]), float(np_["cy"])
                nlength = float(np_.get("length", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if math.hypot(ncx - cx, ncy - cy) < 4 and abs(nlength - length) < 4:
                dupes += 1
                if dupes >= 2:
                    break
        score -= dupes * 0.8

    # Alignment bonus — count lines sharing row/column baseline within 50pt
    aligned = 0
    try:
        cy_self = float(p["cy"])
        cx_self = float(p["cx"])
    except (KeyError, TypeError, ValueError):
        cy_self = cx_self = 0
    for ni in nbrs:
        np_ = prims[ni]
        try:
            ncy = float(np_["cy"])
            ncx = float(np_["cx"])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(ncy - cy_self) <= 4 or abs(ncx - cx_self) <= 4:
            aligned += 1
            if aligned >= 3:
                break
    score += min(aligned, 3) * 0.3

    return score


# ============================================================
# Spatial-bucket top-K pruner
# ============================================================

def prune_primitives(prims, target_count=4000, bucket_size=60.0):
    """Score every primitive, keep top-K per spatial bucket so coverage is uniform.

    bucket_size in PDF pt (60pt = ~0.83 inch). Page is typically 600x800pt for
    24x36 sheet at 72dpi, so ~80 buckets total. Per-bucket top-K = target / n_buckets.
    """
    if len(prims) <= target_count:
        return prims, {"original": len(prims), "kept": len(prims),
                        "method": "no_pruning_needed"}

    grid = build_spatial_grid(prims, cell=30.0)

    # Score everything
    scores = [score_primitive(i, prims, grid) for i in range(len(prims))]

    # Group by larger spatial buckets for top-K
    buckets = defaultdict(list)
    for i, p in enumerate(prims):
        try:
            bx = int(float(p["cx"]) // bucket_size)
            by = int(float(p["cy"]) // bucket_size)
            buckets[(bx, by)].append((scores[i], i))
        except (KeyError, TypeError, ValueError):
            buckets[(0, 0)].append((scores[i], i))

    n_buckets = len(buckets)
    per_bucket_keep = max(8, target_count // max(n_buckets, 1))

    kept_indices = []
    for key, candidates in buckets.items():
        candidates.sort(reverse=True)  # high score first
        for s, idx in candidates[:per_bucket_keep]:
            kept_indices.append(idx)

    # If still over target, drop globally lowest-scored
    if len(kept_indices) > target_count:
        kept_indices.sort(key=lambda i: -scores[i])
        kept_indices = kept_indices[:target_count]

    kept_indices.sort()  # preserve relative order
    pruned = [prims[i] for i in kept_indices]
    return pruned, {
        "original": len(prims),
        "kept": len(pruned),
        "n_buckets": n_buckets,
        "per_bucket_keep": per_bucket_keep,
        "score_min": round(min(scores), 2),
        "score_max": round(max(scores), 2),
        "score_mean": round(sum(scores) / len(scores), 2),
    }


# ============================================================
# Benchmark: re-run raster swap with pruning enabled
# ============================================================

def main():
    print("=== Build 2: Primitive Noise Pruner ===\n", flush=True)
    print("Goal: 16,999 prim/page -> ~4000  AND  lift follows >70%, IoU >0.50\n",
          flush=True)

    from raster_bridge_v1 import reconstruct_page, RASTER_CACHE
    from prg_v29 import (run_prg, swap_regions, pick_alternate_quadrant,
                         bbox_iou, classify_trajectory)

    raster_pdfs = sorted(RASTER_CACHE.glob("raster150_*.pdf"))
    import pymupdf as fitz

    all_results = []
    traj_counts = defaultdict(int)
    n_baselines = 0
    follows = 0
    stays = 0
    ablation_survives = 0
    recall_sum = 0.0
    precision_sum = 0.0
    iou_sum = 0.0
    runtime_sum = 0.0
    primitives_in_sum = 0
    primitives_out_sum = 0
    per_pdf = defaultdict(lambda: {"baseline_found": 0, "follows": 0, "stays": 0})

    for pdf in raster_pdfs:
        print(f"=== {pdf.name} ===", flush=True)
        doc = fitz.open(str(pdf))
        n_pages = doc.page_count
        doc.close()
        n_test = min(n_pages, 6)
        for pg in range(n_test):
            t0 = time.time()
            primitives, (pw, ph), bridge_telem = reconstruct_page(pdf, pg, dpi=150)
            if not primitives:
                print(f"  page {pg}: no primitives from bridge", flush=True)
                continue

            # === PRUNE ===
            pruned, prune_stats = prune_primitives(primitives, target_count=4000)
            for i, p in enumerate(pruned):
                p["primitive_id"] = i

            # Baseline + ablation + swap (full PRG protocol)
            baseline = run_prg(pruned, (pw, ph))
            if not baseline:
                print(f"  page {pg}: NO BASELINE after prune ({prune_stats['original']}->{prune_stats['kept']})",
                      flush=True)
                continue
            n_baselines += 1
            ablated = run_prg(pruned, (pw, ph), exclude_edge_frame=True)
            if ablated:
                ablation_survives += 1

            bc = baseline["centroid"]
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
            else:
                recall = precision = 0.0

            if post_swap:
                sx0, sy0, sx1, sy1 = swap_src
                dx0, dy0, dx1, dy1 = swap_dst
                projected = [bbox[0] + (dx0 - sx0), bbox[1] + (dy0 - sy0),
                             bbox[2] + (dx0 - sx0), bbox[3] + (dy0 - sy0)]
                iou_f = bbox_iou(post_swap["bbox"], projected)
                iou_s = bbox_iou(post_swap["bbox"], bbox)
            else:
                iou_f = iou_s = 0.0

            swap_geom = {"src": list(swap_src), "dst": list(swap_dst)}
            traj = classify_trajectory(baseline, post_swap, swap_geom, recall)
            traj_counts[traj] += 1
            if traj == "FOLLOW":
                follows += 1
                per_pdf[pdf.name]["follows"] += 1
            if traj == "STAY":
                stays += 1
                per_pdf[pdf.name]["stays"] += 1
            per_pdf[pdf.name]["baseline_found"] += 1
            recall_sum += recall
            precision_sum += precision
            iou_sum += iou_f
            elapsed = (time.time() - t0) * 1000
            runtime_sum += elapsed
            primitives_in_sum += prune_stats["original"]
            primitives_out_sum += prune_stats["kept"]

            all_results.append({
                "pdf_name": pdf.name,
                "page_idx": pg,
                "primitives_in": prune_stats["original"],
                "primitives_out": prune_stats["kept"],
                "trajectory_class": traj,
                "recall": round(recall, 3),
                "precision": round(precision, 3),
                "iou_follow": round(iou_f, 3),
                "ablation_survives": ablated is not None,
                "elapsed_ms": round(elapsed, 1),
            })
            print(f"  page {pg}: prune {prune_stats['original']:5d}->{prune_stats['kept']:4d}  "
                  f"traj={traj:<14s} recall={recall:.2f} prec={precision:.2f} iou={iou_f:.2f}  "
                  f"{elapsed:.0f}ms", flush=True)

    # === VERDICT ===
    print("\n=== Build 2 VERDICT ===", flush=True)
    total = sum(1 for r in all_results)
    print(f"\ntotal pages tested: {total}")
    print(f"primitives in:  avg {primitives_in_sum/max(total,1):.0f}/page")
    print(f"primitives out: avg {primitives_out_sum/max(total,1):.0f}/page")
    print(f"runtime: avg {runtime_sum/max(total,1):.0f}ms/page  "
          f"(vector ~50ms, Build 1 raster avg ~12s)")
    print(f"\n--- TRAJECTORY ---")
    for cls in ("FOLLOW", "STAY", "DRIFT_NEAR", "SPLIT_CLUSTER", "THIRD_REGION",
                 "NULL_COLLAPSE"):
        c = traj_counts.get(cls, 0)
        print(f"  {cls:<18s} {c:3d}  ({c/max(n_baselines,1)*100:.1f}%)")

    follows_rate = follows / max(n_baselines, 1)
    stays_rate = stays / max(n_baselines, 1)
    avg_recall = recall_sum / max(n_baselines, 1)
    avg_precision = precision_sum / max(n_baselines, 1)
    avg_iou = iou_sum / max(n_baselines, 1)
    print(f"\n--- QUALITY (raster pruned) vs Build 1 (raster unpruned) vs r8 (vector) ---")
    print(f"  follows:    {follows_rate*100:5.1f}%  (Build1: 50.0%, Vector r8: 89.1%)")
    print(f"  stays:      {stays_rate*100:5.1f}%  (Build1: 16.7%, Vector r8:  2.3%)")
    print(f"  recall:     {avg_recall:.3f}  (Build1: 0.484, Vector r8: 0.84)")
    print(f"  precision:  {avg_precision:.3f}  (Build1: 0.412, Vector r8: 0.81)")
    print(f"  IoU:        {avg_iou:.3f}  (Build1: 0.478, Vector r8: 0.79)")
    print(f"  ablation:   {ablation_survives}/{n_baselines}  ({ablation_survives/max(n_baselines,1)*100:.0f}% survive)")

    print(f"\n--- PER-PDF ---")
    for pdf_name, stats in sorted(per_pdf.items()):
        marker = " <-- FOLLOWS" if stats["follows"] > 0 else ""
        print(f"  {pdf_name[:45]:<45s}  base={stats['baseline_found']}  "
              f"follows={stats['follows']}  stays={stats['stays']}{marker}")

    print(f"\n=== Build 2 result ===")
    if follows_rate > 0.65 and avg_iou > 0.55:
        print(f"PRUNING SUCCEEDED. follow {follows_rate*100:.1f}%, IoU {avg_iou:.2f}.")
    elif follows_rate > 0.55:
        print(f"PARTIAL — pruning lifted follows but IoU still gappy.")
    else:
        print(f"PRUNING INSUFFICIENT — need OCR (Build 3) before raster is production-grade.")

    out_path = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus\build2_pruner_2026-05-25.json")
    import json
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "build": "2_primitive_pruner",
            "total_pages": total,
            "follows_rate": round(follows_rate, 4),
            "stays_rate": round(stays_rate, 4),
            "avg_recall": round(avg_recall, 4),
            "avg_precision": round(avg_precision, 4),
            "avg_iou": round(avg_iou, 4),
            "avg_runtime_ms": round(runtime_sum / max(total, 1), 1),
            "avg_primitives_in": int(primitives_in_sum / max(total, 1)),
            "avg_primitives_out": int(primitives_out_sum / max(total, 1)),
            "trajectory": dict(traj_counts),
            "per_pdf": dict(per_pdf),
            "per_page": all_results,
        }, f, indent=2)
    print(f"\nsaved: {out_path}", flush=True)


if __name__ == "__main__":
    main()
