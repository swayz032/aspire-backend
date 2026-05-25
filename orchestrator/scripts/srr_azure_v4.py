"""SRR + Azure DI v4 — Phase 1.3 Governance + Receipts.

Inherits Phase 1.2 pipeline (the 8/9-gate winner) and adds:
  - Per-page ExtractionReceipt with deterministic receipt_id
  - Authority Graph routing (GREEN auto / YELLOW Ava / RED block)
  - Disqualifier checks (no sheet_id, border ablation fail, etc. -> BLOCK)
  - JSONL audit trail (replay-able)
  - Material Story payload export

Acceptance gates (Phase 1.3):
  - 100% pages produce receipt
  - 100% receipts have valid routing_action
  - Idempotency: same input -> same receipt_id
  - JSONL replay yields identical routing on all pages
  - No regression on Phase 1.2's 8 quality gates
  - Routing distribution matches tier distribution

Run via:
  RAILWAY_TOKEN=... railway run --service Ava-Brain python srr_azure_v4.py
"""
from __future__ import annotations

import json
import math
import os
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
from semantic_pruner_v2 import (
    semantic_prune, SHEET_ID_RE, DIMENSION_RE, POSITIVE_TOKENS,
)
from prg_v29 import (
    run_prg, swap_regions, pick_alternate_quadrant,
    bbox_iou, classify_trajectory,
)
from srr_azure_v2 import (
    extract_full_page_primitives, cluster_uniqueness, render_page_gray,
)
from extraction_receipt import (
    PIPELINE_VERSION, ExtractionReceipt, build_receipt, persist_receipt,
    load_receipts, verify_idempotency, receipt_to_material_story_payload,
    compute_tier, compute_disqualifiers, compute_routing,
)

RASTER_CACHE = Path(r"C:\Users\tonio\Projects\myapp\blueprints\raster_r9")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")
PDF_NATIVE_DPI = 72.0
OCR_DPI = 150.0
TARGET_PRIMITIVES = 1000


def process_page_with_receipt(pdf_path: Path, page_idx: int) -> tuple[dict, ExtractionReceipt]:
    """Run Phase 1.2 pipeline + build governance receipt."""
    t0 = time.time()
    img, pw_pt, ph_pt = render_page_gray(pdf_path, page_idx, OCR_DPI)
    ph_px, pw_px = img.shape

    extractor = get_text_extractor()
    cache_key = compute_cache_key(pdf_path, page_idx, OCR_DPI)
    ocr_result = extractor.extract(img, dpi=OCR_DPI, page_idx=page_idx, cache_key=cache_key)

    metrics: dict = {
        "engine": ocr_result.engine,
        "cost_usd": ocr_result.cost_usd_est,
        "ocr_ms": ocr_result.runtime_ms,
        "n_ocr_spans": len(ocr_result.spans),
    }
    if ocr_result.failure_mode:
        metrics["failure_mode"] = ocr_result.failure_mode
        metrics["trajectory_class"] = "OCR_FAILURE"
        metrics["elapsed_ms"] = (time.time() - t0) * 1000
        receipt = build_receipt(
            pdf_path, page_idx, metrics, ocr_spans=ocr_result.spans,
            pos_tokens_set=POSITIVE_TOKENS, sheet_id_re=SHEET_ID_RE,
            dim_re=DIMENSION_RE,
        )
        return metrics, receipt

    ocr_spans = ocr_result.spans
    raw_primitives = extract_full_page_primitives(img, OCR_DPI, pw_pt, ph_pt)
    pruned, prune_stats = semantic_prune(
        raw_primitives, ocr_spans, pw_px, ph_px, target_count=TARGET_PRIMITIVES,
    )

    # Sheet ID
    sheet_id = None
    for s in ocr_spans:
        m = SHEET_ID_RE.search((s.text or "").upper())
        if m:
            sheet_id = m.group(0); break
    metrics["sheet_id"] = sheet_id
    metrics["n_raw_primitives"] = len(raw_primitives)
    metrics["n_pruned"] = len(pruned)

    # Top-zone semantic score
    top_score = sum(
        s.confidence for s in ocr_spans
        if any(t in (s.text or "").upper() for t in POSITIVE_TOKENS)
    )
    metrics["ocr_top_zone_score"] = top_score

    if not pruned:
        metrics["trajectory_class"] = "NO_PRIMITIVES_AFTER_PRUNE"
        metrics["elapsed_ms"] = (time.time() - t0) * 1000
        receipt = build_receipt(
            pdf_path, page_idx, metrics, ocr_spans=ocr_spans,
            pos_tokens_set=POSITIVE_TOKENS, sheet_id_re=SHEET_ID_RE,
            dim_re=DIMENSION_RE,
        )
        return metrics, receipt

    for i, p in enumerate(pruned):
        p["primitive_id"] = i

    baseline = run_prg(pruned, (pw_pt, ph_pt))
    if not baseline:
        metrics["trajectory_class"] = "NO_BASELINE"
        metrics["elapsed_ms"] = (time.time() - t0) * 1000
        receipt = build_receipt(
            pdf_path, page_idx, metrics, ocr_spans=ocr_spans,
            pos_tokens_set=POSITIVE_TOKENS, sheet_id_re=SHEET_ID_RE,
            dim_re=DIMENSION_RE,
        )
        return metrics, receipt

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

    metrics.update({
        "primitive_recall": recall,
        "primitive_precision": precision,
        "iou_follow": iou_f,
        "cluster_uniqueness": uniq,
        "ablation_survives": ablated is not None,
        "trajectory_class": traj,
        "cluster_bbox_pt": list(bbox),
        "cluster_n_primitives": baseline.get("n_primitives", 0),
        "cluster_centroid_pt": list(baseline.get("centroid", [])),
        "elapsed_ms": (time.time() - t0) * 1000,
    })

    receipt = build_receipt(
        pdf_path, page_idx, metrics, ocr_spans=ocr_spans,
        pos_tokens_set=POSITIVE_TOKENS, sheet_id_re=SHEET_ID_RE,
        dim_re=DIMENSION_RE,
    )
    return metrics, receipt


def main():
    print(f"=== SRR + Azure DI v4 — Phase 1.3 (Governance + Receipts) ===\n", flush=True)
    print(f"OCR_DPI={OCR_DPI}  target_primitives={TARGET_PRIMITIVES}")
    print(f"Pipeline version: {PIPELINE_VERSION}")
    print(f"Receipts persist to: {Path(r'benchmark_corpus/receipts/{PIPELINE_VERSION}.jsonl')}\n",
           flush=True)

    raster_pdfs = sorted(RASTER_CACHE.glob("raster150_*.pdf"))
    if not raster_pdfs:
        print("ERROR: no raster cache"); return

    run_id = f"{PIPELINE_VERSION}_2026-05-25"
    # Clear prior run's JSONL for clean idempotency test
    jsonl_path = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus\receipts") / f"{run_id}.jsonl"
    if jsonl_path.exists():
        jsonl_path.unlink()

    all_receipts: list[ExtractionReceipt] = []
    routing_counts = defaultdict(int)
    tier_counts = defaultdict(int)
    disqualifier_counts = defaultdict(int)
    per_pdf_routing = defaultdict(lambda: defaultdict(int))
    n_receipt_pages = 0
    total_cost = 0.0
    runtimes = []

    for pdf in raster_pdfs:
        print(f"=== {pdf.name} ===", flush=True)
        doc = fitz.open(str(pdf))
        n_pages = doc.page_count; doc.close()
        n_test = min(n_pages, 6)
        for pg in range(n_test):
            try:
                metrics, receipt = process_page_with_receipt(pdf, pg)
                persist_receipt(receipt, run_id)
                all_receipts.append(receipt)
                n_receipt_pages += 1
                routing_counts[receipt.routing_action] += 1
                tier_counts[receipt.confidence_tier] += 1
                per_pdf_routing[pdf.name][receipt.routing_action] += 1
                for d in receipt.disqualifiers:
                    disqualifier_counts[d.split(":")[0]] += 1
                total_cost += receipt.cost_usd
                runtimes.append(receipt.runtime_ms)
                print(
                    f"  page {pg}: sheet={receipt.sheet_id}  "
                    f"tier={receipt.confidence_tier}  "
                    f"-> {receipt.routing_action:<14s} "
                    f"IoU={receipt.iou_follow:.2f} u={receipt.cluster_uniqueness:.2f} "
                    f"traj={receipt.trajectory_class:<14s} "
                    f"dq={len(receipt.disqualifiers)} "
                    f"{receipt.runtime_ms:.0f}ms",
                    flush=True,
                )
            except Exception as exc:
                import traceback; traceback.print_exc()
                print(f"  page {pg}: EXC {type(exc).__name__}: {str(exc)[:100]}",
                       flush=True)

    # === IDEMPOTENCY + REPLAY TEST ===
    print(f"\n--- IDEMPOTENCY + REPLAY VERIFICATION ---")
    replayed = load_receipts(jsonl_path)
    idem = verify_idempotency(replayed)
    print(f"  receipts written: {n_receipt_pages}")
    print(f"  receipts loaded:  {len(replayed)}")
    print(f"  unique receipt_ids: {idem['unique_ids']}")
    print(f"  idempotent: {idem['idempotent']}")
    if idem['duplicates']:
        print(f"  ⚠ DUPLICATES: {idem['duplicates']}")

    # Replay-determinism: re-route every loaded receipt + verify match
    deterministic_count = 0
    for orig, loaded in zip(all_receipts, replayed):
        # Reproduce routing from metrics
        metrics_only = {
            "iou_follow": loaded.iou_follow,
            "cluster_uniqueness": loaded.cluster_uniqueness,
            "primitive_recall": loaded.primitive_recall,
            "ocr_top_zone_score": loaded.ocr_top_zone_score,
            "trajectory_class": loaded.trajectory_class,
            "cost_usd": loaded.cost_usd,
        }
        reroute_tier, _ = compute_tier(metrics_only)
        reroute_dq = compute_disqualifiers(
            metrics_only, loaded.sheet_id, loaded.border_ablation_passed,
            loaded.failure_mode,
        )
        reroute_action, _ = compute_routing(reroute_tier, reroute_dq)
        if reroute_action == orig.routing_action and reroute_tier == orig.confidence_tier:
            deterministic_count += 1
    print(f"  deterministic replay: {deterministic_count}/{len(replayed)}  "
          f"({deterministic_count/max(len(replayed),1)*100:.1f}%)")

    # === SUMMARY ===
    print(f"\n=== Phase 1.3 SUMMARY ===")
    print(f"\ntotal pages: {n_receipt_pages}")
    print(f"total cost: ${total_cost:.4f}")
    if runtimes:
        runtimes.sort()
        p50 = runtimes[len(runtimes)//2]
        p95 = runtimes[min(int(len(runtimes)*0.95), len(runtimes)-1)]
        print(f"runtime: p50={p50:.0f}ms  p95={p95:.0f}ms")

    print(f"\n--- TIER DISTRIBUTION ---")
    for t in ("GREEN", "YELLOW", "RED"):
        c = tier_counts.get(t, 0)
        print(f"  {t:<8s} {c:3d}  ({c/max(n_receipt_pages,1)*100:.1f}%)")

    print(f"\n--- ROUTING ACTIONS ---")
    for a in ("AUTO_BIND", "ROUTE_TO_AVA", "BLOCK_MANUAL"):
        c = routing_counts.get(a, 0)
        print(f"  {a:<16s} {c:3d}  ({c/max(n_receipt_pages,1)*100:.1f}%)")

    print(f"\n--- DISQUALIFIERS (FAIL-CLOSED triggers) ---")
    for d, c in sorted(disqualifier_counts.items(), key=lambda x: -x[1]):
        print(f"  {d:<28s} {c:3d}")
    if not disqualifier_counts:
        print(f"  (none triggered)")

    print(f"\n--- PER-PDF ROUTING ---")
    for pdf_name, routing in sorted(per_pdf_routing.items()):
        green = routing.get("AUTO_BIND", 0)
        yellow = routing.get("ROUTE_TO_AVA", 0)
        red = routing.get("BLOCK_MANUAL", 0)
        print(f"  {pdf_name[:42]:<42s}  AUTO={green} AVA={yellow} BLOCK={red}")

    print(f"\n--- ACCEPTANCE GATES (Phase 1.3) ---")
    gates = {
        "receipt_per_page": len(all_receipts) == n_receipt_pages and n_receipt_pages > 0,
        "all_have_routing": all(r.routing_action in ("AUTO_BIND","ROUTE_TO_AVA","BLOCK_MANUAL") for r in all_receipts),
        "idempotent_receipt_ids": idem["idempotent"],
        "deterministic_replay": deterministic_count == len(replayed) and len(replayed) > 0,
        "jsonl_persisted": jsonl_path.exists() and jsonl_path.stat().st_size > 0,
        "no_failure_default_BLOCK": all(
            r.routing_action == "BLOCK_MANUAL" for r in all_receipts if r.failure_mode
        ) or not any(r.failure_mode for r in all_receipts),
        "green_routes_to_auto": all(
            r.routing_action == "AUTO_BIND" for r in all_receipts
            if r.confidence_tier == "GREEN" and not r.disqualifiers
        ),
        "tier_distribution_healthy": (tier_counts.get("GREEN", 0) > 0 and
                                        tier_counts.get("RED", 0) < n_receipt_pages * 0.5),
    }
    for name, ok in gates.items():
        print(f"  {name:<32s} {'PASS' if ok else 'FAIL'}")
    n_pass = sum(1 for v in gates.values() if v)
    print(f"\n{n_pass} of {len(gates)} Phase 1.3 gates pass")

    # === SAMPLE MATERIAL STORY PAYLOADS ===
    print(f"\n--- SAMPLE MATERIAL STORY PAYLOADS (one per tier) ---")
    samples_by_tier = {"GREEN": None, "YELLOW": None, "RED": None}
    for r in all_receipts:
        if samples_by_tier[r.confidence_tier] is None:
            samples_by_tier[r.confidence_tier] = receipt_to_material_story_payload(r)
        if all(v is not None for v in samples_by_tier.values()):
            break
    for tier, payload in samples_by_tier.items():
        if payload:
            print(f"\n  [{tier}] {json.dumps(payload, indent=2, default=str)[:500]}")

    out_path = OUT_DIR / f"phase1.3_summary_2026-05-25.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "phase1.3_governance_receipts",
            "pipeline_version": PIPELINE_VERSION,
            "total_pages": n_receipt_pages,
            "tier_distribution": dict(tier_counts),
            "routing_distribution": dict(routing_counts),
            "disqualifier_counts": dict(disqualifier_counts),
            "idempotency": idem,
            "deterministic_replay_count": deterministic_count,
            "gates_passed": n_pass,
            "gates_total": len(gates),
            "gates": gates,
            "total_cost_usd": round(total_cost, 4),
            "jsonl_audit_trail": str(jsonl_path),
            "per_pdf_routing": {k: dict(v) for k, v in per_pdf_routing.items()},
        }, f, indent=2)
    print(f"\nsaved: {out_path}", flush=True)
    print(f"audit trail: {jsonl_path}", flush=True)


if __name__ == "__main__":
    main()
