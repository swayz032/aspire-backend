"""SRR + Azure DI v1 — production OCR backend transition.

Same SRR architecture as srr_pipeline_v1.py but OCR runs via ITextExtractor
abstraction. Default backend: Azure Document Intelligence. PaddleOCR available
via ASPIRE_OCR_BACKEND=paddle_ppocrv4 feature flag.

Acceptance gates (Phase 1.1):
  - Runtime: <5s/page p95
  - Sheet ID recovery: preserved or improved vs PaddleOCR 150dpi baseline
  - Per-page cost: ~$0.0015 Azure Read tier
  - PaddleOCR feature-flagged off, not deleted
  - Replay receipt emitted per page

Run via:
  RAILWAY_TOKEN=... railway run --service Ava-Brain python srr_azure_v1.py
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
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

warnings.filterwarnings("ignore")

try:
    import pymupdf as fitz
except ImportError:
    import fitz

sys.path.insert(0, str(Path(__file__).parent))
from text_extractor import get_text_extractor, compute_cache_key, OCRSpan
from prg_v29 import (
    run_prg, swap_regions, pick_alternate_quadrant,
    bbox_iou, classify_trajectory,
)

RASTER_CACHE = Path(r"C:\Users\tonio\Projects\myapp\blueprints\raster_r9")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")

PDF_NATIVE_DPI = 72.0
OCR_DPI = 150.0  # Azure handles full-res cleanly; matches SRR v1 baseline for fair comparison

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


def build_semantic_heatmap(ocr_spans: list[OCRSpan], pw_px: int, ph_px: int,
                            grid_size: int = 80) -> np.ndarray:
    gw = max(1, pw_px // grid_size + 1)
    gh = max(1, ph_px // grid_size + 1)
    heat = np.zeros((gh, gw), dtype=np.float32)
    if not ocr_spans:
        return heat
    for s in ocr_spans:
        x0, y0, x1, y1 = s.bbox_px
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        gx = max(0, min(gw - 1, (x0 + x1) // 2 // grid_size))
        gy = max(0, min(gh - 1, (y0 + y1) // 2 // grid_size))
        text = (s.text or "").upper()
        w = s.confidence
        for kw, b in POSITIVE_TOKENS.items():
            if kw in text:
                w += b * s.confidence; break
        for kw, p in NEGATIVE_TOKENS.items():
            if kw in text:
                w += p * s.confidence; break
        if SHEET_ID_RE.search(text):
            w += 2.5 * s.confidence
        th = y1 - y0
        if 8 <= th <= 35:
            w *= 1.4
        heat[gy, gx] += w
    # BR prior
    for gy in range(gh):
        for gx in range(gw):
            rx = (gx + 0.5) / gw
            ry = (gy + 0.5) / gh
            d = math.hypot(1 - rx, 1 - ry)
            heat[gy, gx] *= (1.0 + math.exp(-d * 2.0) * 0.6)
    return heat


def select_top_zones(heat: np.ndarray, pw_px: int, ph_px: int,
                       grid_size: int = 80, n_zones: int = 3) -> list[tuple]:
    h, w = heat.shape
    score = heat.copy()
    zones = []
    for _ in range(n_zones):
        idx = int(np.argmax(score))
        gy, gx = divmod(idx, w)
        peak = float(score[gy, gx])
        if peak < 0.5:
            break
        gy0 = max(0, gy - 2); gy1 = min(h, gy + 3)
        gx0 = max(0, gx - 2); gx1 = min(w, gx + 3)
        zones.append((
            gx0 * grid_size, gy0 * grid_size,
            min(pw_px, gx1 * grid_size), min(ph_px, gy1 * grid_size),
        ))
        score[gy0:gy1, gx0:gx1] = 0
    return zones


def extract_zone_primitives(page_gray, zones_px, dpi, pw_pt, ph_pt):
    scale_to_pt = PDF_NATIVE_DPI / dpi
    primitives = []
    for x0, y0, x1, y1 in zones_px:
        if x1 - x0 < 20 or y1 - y0 < 20:
            continue
        crop = page_gray[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        normed = cv2.normalize(crop, None, 0, 255, cv2.NORM_MINMAX)
        binary = cv2.adaptiveThreshold(normed, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                          cv2.THRESH_BINARY_INV, 25, 10)
        kernel = np.ones((2, 2), np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        min_len = max(8, int(0.05 * dpi))
        max_gap = max(3, int(0.02 * dpi))
        thresh_h = max(30, int(0.04 * dpi))
        lines = cv2.HoughLinesP(binary, rho=1, theta=np.pi / 180,
                                 threshold=thresh_h, minLineLength=min_len,
                                 maxLineGap=max_gap)
        zone_lines = []
        if lines is not None:
            for line in lines:
                lx0, ly0, lx1, ly1 = line[0]
                zone_lines.append((float(x0+lx0), float(y0+ly0),
                                     float(x0+lx1), float(y0+ly1)))
        contours, _ = cv2.findContours(binary, cv2.RETR_LIST,
                                          cv2.CHAIN_APPROX_TC89_L1)
        min_len_sq = (0.04 * dpi) ** 2
        for c in contours:
            if len(c) < 2:
                continue
            approx = cv2.approxPolyDP(c, 1.5, False)
            pts = approx.reshape(-1, 2)
            for i in range(len(pts) - 1):
                lx0, ly0 = float(pts[i, 0]), float(pts[i, 1])
                lx1, ly1 = float(pts[i+1, 0]), float(pts[i+1, 1])
                if (lx1-lx0)**2 + (ly1-ly0)**2 < min_len_sq:
                    continue
                zone_lines.append((float(x0+lx0), float(y0+ly0),
                                     float(x0+lx1), float(y0+ly1)))
        # Snap
        snap_r = max(3.0, 6.0 * dpi / PDF_NATIVE_DPI)
        eps = []
        for x0_,y0_,x1_,y1_ in zone_lines:
            eps += [(x0_,y0_),(x1_,y1_)]
        bins = defaultdict(list)
        for i,(x,y) in enumerate(eps):
            bins[(round(x/snap_r), round(y/snap_r))].append(i)
        canon = {k:(sum(eps[i][0] for i in v)/len(v),
                     sum(eps[i][1] for i in v)/len(v)) for k,v in bins.items()}
        snapped_eps = [canon[(round(x/snap_r), round(y/snap_r))] for x,y in eps]
        for i in range(len(zone_lines)):
            px0,py0 = snapped_eps[2*i]
            px1,py1 = snapped_eps[2*i+1]
            if math.hypot(px1-px0, py1-py0) < 1.0:
                continue
            ppx0 = px0 * scale_to_pt; ppy0 = py0 * scale_to_pt
            ppx1 = px1 * scale_to_pt; ppy1 = py1 * scale_to_pt
            length = math.hypot(ppx1-ppx0, ppy1-ppy0)
            if length < 2:
                continue
            primitives.append({
                "primitive_type": "line",
                "x0": ppx0, "y0": ppy0, "x1": ppx1, "y1": ppy1,
                "cx": (ppx0+ppx1)/2, "cy": (ppy0+ppy1)/2,
                "length": length, "source": "srr_zone_azure",
                "confidence": 0.85,
            })
    return primitives


def cluster_uniqueness(primitives, page_extent):
    from prg_v29 import (type_primitives, build_relational_graph, mine_admin_motifs)
    types_per, grid = type_primitives(primitives, page_extent)
    edges, micro, etypes = build_relational_graph(primitives, types_per, grid)
    motifs = mine_admin_motifs(edges, micro, primitives, types_per)
    if len(motifs) < 2:
        return 1.0
    motifs.sort(key=lambda m: -m["size"]*(0.3+m["alignment_score"]))
    top = motifs[0]["size"]*(0.3+motifs[0]["alignment_score"])
    sec = motifs[1]["size"]*(0.3+motifs[1]["alignment_score"])
    return min(top/sec if sec > 0 else 10.0, 10.0)


def confidence_tier(uniqueness, iou, top_score, traj):
    if traj == "FOLLOW" and iou >= 0.5 and uniqueness >= 1.5 and top_score >= 1.5:
        return "GREEN"
    if traj in ("FOLLOW","DRIFT_NEAR") and iou >= 0.3 and uniqueness >= 1.1:
        return "YELLOW"
    return "RED"


def render_page_gray(pdf_path, page_idx, dpi):
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_idx]
        pw_pt = page.rect.width
        ph_pt = page.rect.height
        mat = fitz.Matrix(dpi/PDF_NATIVE_DPI, dpi/PDF_NATIVE_DPI)
        pix = page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        return img, pw_pt, ph_pt
    finally:
        doc.close()


def process_page(pdf_path, page_idx):
    t0 = time.time()
    img, pw_pt, ph_pt = render_page_gray(pdf_path, page_idx, OCR_DPI)
    ph_px, pw_px = img.shape

    # Stage 1: OCR full page (cached via extractor)
    extractor = get_text_extractor()
    cache_key = compute_cache_key(pdf_path, page_idx, OCR_DPI)
    ocr_result = extractor.extract(img, dpi=OCR_DPI, page_idx=page_idx, cache_key=cache_key)
    if ocr_result.failure_mode:
        return {"page_idx": page_idx, "error": ocr_result.failure_mode,
                "engine": ocr_result.engine, "ocr_ms": ocr_result.runtime_ms,
                "elapsed_ms": (time.time() - t0) * 1000}
    ocr_spans = ocr_result.spans

    # Stage 2: semantic heatmap + top zones
    heat = build_semantic_heatmap(ocr_spans, pw_px, ph_px)
    zones_px = select_top_zones(heat, pw_px, ph_px, n_zones=3)

    # Top-zone semantic score
    top_score = 0.0
    if zones_px:
        x0,y0,x1,y1 = zones_px[0]
        for s in ocr_spans:
            sx0,sy0,sx1,sy1 = s.bbox_px
            if not (sx0>=x0 and sx1<=x1 and sy0>=y0 and sy1<=y1):
                continue
            text = (s.text or "").upper()
            for kw,b in POSITIVE_TOKENS.items():
                if kw in text:
                    top_score += b * s.confidence; break
            if SHEET_ID_RE.search(text):
                top_score += 2.0 * s.confidence

    # Sheet ID from all spans
    sheet_id = None
    for s in ocr_spans:
        m = SHEET_ID_RE.search((s.text or "").upper())
        if m:
            sheet_id = m.group(0); break

    # Stage 3: zone-only primitive extraction
    primitives = extract_zone_primitives(img, zones_px, OCR_DPI, pw_pt, ph_pt)
    if not primitives:
        return {"page_idx": page_idx, "n_ocr_spans": len(ocr_spans),
                "n_zones": len(zones_px), "n_primitives": 0,
                "sheet_id": sheet_id, "trajectory_class": "NO_PRIMITIVES",
                "engine": ocr_result.engine, "cost_usd": ocr_result.cost_usd_est,
                "ocr_ms": ocr_result.runtime_ms,
                "elapsed_ms": (time.time() - t0) * 1000}

    for i,p in enumerate(primitives):
        p["primitive_id"] = i

    # PRG + swap
    baseline = run_prg(primitives, (pw_pt, ph_pt))
    if not baseline:
        return {"page_idx": page_idx, "n_ocr_spans": len(ocr_spans),
                "n_zones": len(zones_px), "n_primitives": len(primitives),
                "sheet_id": sheet_id, "trajectory_class": "NO_BASELINE",
                "engine": ocr_result.engine, "cost_usd": ocr_result.cost_usd_est,
                "ocr_ms": ocr_result.runtime_ms,
                "elapsed_ms": (time.time() - t0) * 1000}

    ablated = run_prg(primitives, (pw_pt, ph_pt), exclude_edge_frame=True)
    bbox = baseline["bbox"]; pad = 20.0
    swap_src = (max(0,bbox[0]-pad), max(0,bbox[1]-pad),
                 min(pw_pt,bbox[2]+pad), min(ph_pt,bbox[3]+pad))
    swap_dst = pick_alternate_quadrant(bbox, pw_pt, ph_pt)
    swapped = swap_regions(primitives, pw_pt, ph_pt, swap_src, swap_dst)
    post = run_prg(swapped, (pw_pt, ph_pt))

    bpids = set(baseline.get("primitive_ids", []))
    if post:
        ppids = set(post.get("primitive_ids", []))
        ov = bpids & ppids
        recall = len(ov)/max(len(bpids),1)
        precision = len(ov)/max(len(ppids),1)
        sx0,sy0,sx1,sy1 = swap_src
        dx0,dy0,dx1,dy1 = swap_dst
        proj = [bbox[0]+(dx0-sx0), bbox[1]+(dy0-sy0),
                bbox[2]+(dx0-sx0), bbox[3]+(dy0-sy0)]
        iou_f = bbox_iou(post["bbox"], proj)
        iou_s = bbox_iou(post["bbox"], bbox)
    else:
        recall = precision = iou_f = iou_s = 0.0

    sg = {"src": list(swap_src), "dst": list(swap_dst)}
    traj = classify_trajectory(baseline, post, sg, recall)
    uniq = cluster_uniqueness(primitives, (pw_pt, ph_pt))
    tier = confidence_tier(uniq, iou_f, top_score, traj)

    return {
        "page_idx": page_idx,
        "engine": ocr_result.engine,
        "cost_usd": ocr_result.cost_usd_est,
        "ocr_ms": round(ocr_result.runtime_ms, 1),
        "ocr_cached": ocr_result.cached,
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
        "cluster_uniqueness": round(uniq, 3),
        "top_zone_semantic_score": round(top_score, 3),
        "ablation_survives": ablated is not None,
        "confidence_tier": tier,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
    }


def main():
    backend = os.environ.get("ASPIRE_OCR_BACKEND", "azure_di")
    print(f"=== SRR + Azure DI v1 (backend={backend}) ===\n", flush=True)
    print(f"OCR_DPI={OCR_DPI}  Azure endpoint={os.environ.get('ASPIRE_AZURE_DOC_INTEL_ENDPOINT','<NOT SET>')[:40]}...", flush=True)
    print(f"PaddleOCR feature-flagged via ASPIRE_OCR_BACKEND=paddle_ppocrv4 (default off)\n", flush=True)

    raster_pdfs = sorted(RASTER_CACHE.glob("raster150_*.pdf"))
    if not raster_pdfs:
        print("ERROR: no raster cache", flush=True); return

    all_results = []
    per_pdf = defaultdict(lambda: {"baseline_found": 0, "follows": 0,
                                     "sheet_ids": [], "tiers": defaultdict(int),
                                     "total_cost": 0.0, "total_runtime_ms": 0.0})
    traj_counts = defaultdict(int)
    tier_counts = defaultdict(int)
    follows = 0; n_baselines = 0; ablation_survives = 0
    total_cost = 0.0; total_runtime = 0.0
    primitives_sum = 0
    latencies = []

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
                    print(f"  page {pg}: ERROR {r['error']} ({r.get('elapsed_ms',0):.0f}ms)", flush=True)
                    continue
                traj = r.get("trajectory_class", "?")
                traj_counts[traj] += 1
                cost = r.get("cost_usd", 0)
                runtime = r.get("elapsed_ms", 0)
                total_cost += cost
                total_runtime += runtime
                latencies.append(runtime)
                primitives_sum += r.get("n_primitives", 0)
                tier = r.get("confidence_tier", "RED")
                tier_counts[tier] += 1
                per_pdf[pdf.name]["total_cost"] += cost
                per_pdf[pdf.name]["total_runtime_ms"] += runtime
                if traj not in ("NO_PRIMITIVES","NO_BASELINE"):
                    n_baselines += 1
                    per_pdf[pdf.name]["baseline_found"] += 1
                    if r.get("ablation_survives"):
                        ablation_survives += 1
                    if r.get("follows_content"):
                        follows += 1
                        per_pdf[pdf.name]["follows"] += 1
                if r.get("sheet_id"):
                    per_pdf[pdf.name]["sheet_ids"].append(r["sheet_id"])
                per_pdf[pdf.name]["tiers"][tier] += 1
                print(f"  page {pg}: spans={r.get('n_ocr_spans',0):3d} zones={r.get('n_zones',0)} "
                      f"prims={r.get('n_primitives',0):4d} sheet={r.get('sheet_id')}  "
                      f"traj={traj:<14s} r={r.get('primitive_recall',0):.2f} "
                      f"p={r.get('primitive_precision',0):.2f} iou={r.get('iou_follow',0):.2f} "
                      f"u={r.get('cluster_uniqueness',0):.2f}  {tier}  "
                      f"{runtime:.0f}ms (ocr={r.get('ocr_ms',0):.0f}ms ${cost:.4f})",
                      flush=True)
            except Exception as exc:
                import traceback; traceback.print_exc()
                print(f"  page {pg}: EXC {type(exc).__name__}: {str(exc)[:120]}", flush=True)

    # === VERDICT ===
    total = sum(1 for r in all_results if "error" not in r)
    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies)//2]
        p95 = latencies[min(int(len(latencies)*0.95), len(latencies)-1)]
    else:
        p50 = p95 = 0
    print(f"\n=== Phase 1.1 — Azure DI Benchmark ===")
    print(f"\ntotal pages: {total}")
    print(f"avg primitives/page: {primitives_sum/max(total,1):.0f}")
    print(f"avg runtime: {total_runtime/max(total,1):.0f}ms  p50={p50:.0f}ms  p95={p95:.0f}ms  (target <5000ms)")
    print(f"total cost: ${total_cost:.4f}  ({total_cost/max(total,1)*1000:.2f}c/page)")

    print(f"\n--- TRAJECTORY ---")
    for cls in ("FOLLOW","STAY","DRIFT_NEAR","SPLIT_CLUSTER","THIRD_REGION",
                 "NULL_COLLAPSE","NO_BASELINE","NO_PRIMITIVES"):
        c = traj_counts.get(cls, 0)
        if c > 0:
            print(f"  {cls:<18s} {c:3d}  ({c/max(total,1)*100:.1f}%)")

    follows_rate = follows / max(n_baselines, 1)
    print(f"\n--- ACCEPTANCE GATES ---")
    print(f"  runtime p95:   {p95:.0f}ms  (target <5000)  {'PASS' if p95 < 5000 else 'FAIL'}")
    print(f"  follows:       {follows_rate*100:5.1f}%  (SRR PaddleOCR: 50%)")
    print(f"  ablation:      {ablation_survives}/{n_baselines}  ({ablation_survives/max(n_baselines,1)*100:.0f}%)")

    print(f"\n--- CONFIDENCE TIERS ---")
    for t in ("GREEN","YELLOW","RED"):
        c = tier_counts.get(t, 0)
        print(f"  {t:<8s} {c:3d}  ({c/max(total,1)*100:.1f}%)")

    print(f"\n--- PER-PDF ---")
    for pdf_name, stats in sorted(per_pdf.items()):
        ids = ", ".join(set(stats["sheet_ids"]))[:60]
        avg_runtime = stats["total_runtime_ms"] / max(stats["baseline_found"], 1)
        marker = " <-- FOLLOWS" if stats["follows"] > 0 else ""
        print(f"  {pdf_name[:42]:<42s}  base={stats['baseline_found']}  follows={stats['follows']}  "
              f"avg={avg_runtime:.0f}ms  ${stats['total_cost']:.4f}  "
              f"sheets=[{ids}]{marker}")

    out_path = OUT_DIR / "srr_azure_v1_2026-05-25.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "srr_azure_v1_phase1.1",
            "backend": backend,
            "total_pages": total,
            "follows_rate": round(follows_rate, 4),
            "avg_runtime_ms": round(total_runtime/max(total,1), 1),
            "p50_runtime_ms": p50, "p95_runtime_ms": p95,
            "total_cost_usd": round(total_cost, 4),
            "trajectory": dict(traj_counts),
            "confidence_tiers": dict(tier_counts),
            "per_pdf": {k: {**v, "sheet_ids": list(set(v["sheet_ids"])),
                              "tiers": dict(v["tiers"])} for k,v in per_pdf.items()},
            "per_page": all_results,
        }, f, indent=2)
    print(f"\nsaved: {out_path}", flush=True)


if __name__ == "__main__":
    main()
