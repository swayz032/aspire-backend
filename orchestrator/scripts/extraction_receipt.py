"""Phase 1.3 — Drew Extraction Receipt Schema + Authority Graph Routing.

Every blueprint page extraction produces an ExtractionReceipt. Receipts are:
  - Deterministic (receipt_id = SHA-1 of doc_hash + page_idx + pipeline_version)
  - Replay-able (JSONL on disk; load + reproduce routing decision)
  - Audit-grade (every routing action traceable to evidence)
  - Fail-closed (any anomaly downgrades tier, BLOCK is the default)

Authority Graph Routing:
  GREEN  -> AUTO_BIND       (autonomous Material Story binding allowed)
  YELLOW -> ROUTE_TO_AVA    (Ava confirms with user before binding)
  RED    -> BLOCK_MANUAL    (no automation; explicit human review)

Disqualifiers (override tier to BLOCK regardless of cluster metrics):
  - No sheet_id extracted
  - Border ablation fails
  - Trajectory NULL_COLLAPSE / NO_BASELINE
  - OCR failure_mode set
  - Cost > budget cap (10x typical)
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PIPELINE_VERSION = "srr_azure_v4_phase1.3"
RECEIPT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus\receipts")
RECEIPT_DIR.mkdir(exist_ok=True, parents=True)


# ============================================================
# Receipt schema
# ============================================================

@dataclass
class OCRTokenEvidence:
    text: str
    confidence: float
    bbox_px: tuple[int, int, int, int]
    contribution: str  # "positive_keyword" | "sheet_id" | "negative_dimension" | "general"


@dataclass
class ExtractionReceipt:
    # ---- Identity ----
    receipt_id: str
    doc_hash: str
    pdf_filename: str
    page_idx: int
    pipeline_version: str
    timestamp: str
    suite_id: Optional[str] = None  # Aspire tenant scoping (set by caller in production)

    # ---- Extraction result ----
    sheet_id: Optional[str] = None
    confidence_tier: str = "RED"
    routing_action: str = "BLOCK_MANUAL"
    routing_reason: str = ""

    # ---- Confidence metrics (the evidence triangle) ----
    primitive_recall: float = 0.0
    primitive_precision: float = 0.0
    iou_follow: float = 0.0
    cluster_uniqueness: float = 0.0
    ocr_top_zone_score: float = 0.0

    # ---- Cluster evidence ----
    cluster_bbox_pt: list[float] = field(default_factory=list)
    cluster_n_primitives: int = 0
    cluster_centroid_pt: list[float] = field(default_factory=list)

    # ---- OCR evidence (top contributing tokens) ----
    evidence_ocr_tokens: list[dict] = field(default_factory=list)
    n_ocr_spans_total: int = 0

    # ---- Governance attestation ----
    border_ablation_passed: bool = False
    trajectory_class: str = "UNKNOWN"
    disqualifiers: list[str] = field(default_factory=list)

    # ---- Performance + cost ----
    runtime_ms: float = 0.0
    ocr_engine: str = "unknown"
    ocr_ms: float = 0.0
    cost_usd: float = 0.0

    # ---- Failure / errors ----
    failure_mode: Optional[str] = None
    error_detail: Optional[str] = None


# ============================================================
# Routing logic (Authority Graph)
# ============================================================

# Tier thresholds (locked from Phase 1.2 calibration)
GREEN_TIER_THRESHOLDS = {
    "iou_follow_min": 0.5,
    "cluster_uniqueness_min": 1.30,
    "or_recall_min": 0.80,
    "or_ocr_score_min": 1.0,
    "must_be_FOLLOW": True,
}
YELLOW_TIER_THRESHOLDS = {
    "iou_follow_min": 0.25,
    "cluster_uniqueness_min": 1.05,
    "trajectory_in": ("FOLLOW", "DRIFT_NEAR"),
}


def compute_tier(metrics: dict) -> tuple[str, str]:
    """Returns (tier, reason). Pure function of metrics + trajectory."""
    iou = metrics.get("iou_follow", 0.0)
    uniqueness = metrics.get("cluster_uniqueness", 0.0)
    recall = metrics.get("primitive_recall", 0.0)
    top_score = metrics.get("ocr_top_zone_score", 0.0)
    traj = metrics.get("trajectory_class", "UNKNOWN")

    # GREEN: must FOLLOW + strong IoU + unique cluster + (good recall OR strong OCR signal)
    if (traj == "FOLLOW"
            and iou >= GREEN_TIER_THRESHOLDS["iou_follow_min"]
            and uniqueness >= GREEN_TIER_THRESHOLDS["cluster_uniqueness_min"]
            and (recall >= GREEN_TIER_THRESHOLDS["or_recall_min"]
                  or top_score >= GREEN_TIER_THRESHOLDS["or_ocr_score_min"])):
        return "GREEN", (
            f"FOLLOW + IoU={iou:.2f}(≥0.5) + uniq={uniqueness:.2f}(≥1.30) + "
            f"recall={recall:.2f} or ocr_score={top_score:.2f}"
        )

    # YELLOW: FOLLOW or DRIFT + mid IoU + mild competing cluster
    if (traj in YELLOW_TIER_THRESHOLDS["trajectory_in"]
            and iou >= YELLOW_TIER_THRESHOLDS["iou_follow_min"]
            and uniqueness >= YELLOW_TIER_THRESHOLDS["cluster_uniqueness_min"]):
        return "YELLOW", (
            f"traj={traj} + IoU={iou:.2f}(≥0.25) + uniq={uniqueness:.2f}(≥1.05)"
        )

    return "RED", (
        f"traj={traj}, IoU={iou:.2f}, uniq={uniqueness:.2f}, "
        f"recall={recall:.2f}, ocr_score={top_score:.2f} — below thresholds"
    )


def compute_disqualifiers(metrics: dict, sheet_id: Optional[str],
                           border_ablation: bool, failure_mode: Optional[str]) -> list[str]:
    """Returns list of FAIL-CLOSED disqualifier strings. Empty list = no disqualifiers."""
    dq = []
    if failure_mode:
        dq.append(f"FAILURE_MODE:{failure_mode}")
    if not sheet_id:
        dq.append("NO_SHEET_ID")
    if not border_ablation:
        dq.append("BORDER_ABLATION_FAILED")
    traj = metrics.get("trajectory_class", "UNKNOWN")
    if traj in ("NO_BASELINE", "NULL_COLLAPSE", "NO_PRIMITIVES_AFTER_PRUNE",
                "NO_PRIMITIVES"):
        dq.append(f"TRAJECTORY_{traj}")
    cost = metrics.get("cost_usd", 0.0)
    if cost > 0.02:  # 10x typical Read tier
        dq.append(f"COST_EXCEEDED:${cost:.4f}")
    return dq


def compute_routing(tier: str, disqualifiers: list[str]) -> tuple[str, str]:
    """Authority Graph routing decision. Disqualifiers override tier to BLOCK."""
    if disqualifiers:
        return "BLOCK_MANUAL", f"Disqualifiers present: {','.join(disqualifiers)}"
    if tier == "GREEN":
        return "AUTO_BIND", "GREEN tier — autonomous Material Story binding"
    if tier == "YELLOW":
        return "ROUTE_TO_AVA", "YELLOW tier — Ava confirms with user before binding"
    return "BLOCK_MANUAL", "RED tier — explicit human review required"


# ============================================================
# Receipt builder
# ============================================================

def compute_doc_hash(pdf_path: Path) -> str:
    h = hashlib.sha1()
    with open(pdf_path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()[:16]


def compute_receipt_id(doc_hash: str, page_idx: int,
                        pipeline_version: str = PIPELINE_VERSION) -> str:
    return hashlib.sha1(
        f"{doc_hash}|{page_idx}|{pipeline_version}".encode()
    ).hexdigest()[:24]


def build_receipt(
    pdf_path: Path,
    page_idx: int,
    metrics: dict,
    ocr_spans=None,
    pos_tokens_set=None,
    sheet_id_re=None,
    dim_re=None,
    suite_id: Optional[str] = None,
) -> ExtractionReceipt:
    """Build a complete Phase 1.3 receipt from per-page extraction metrics + OCR spans."""
    doc_hash = compute_doc_hash(pdf_path)
    receipt_id = compute_receipt_id(doc_hash, page_idx)
    sheet_id = metrics.get("sheet_id")
    border_ablation = bool(metrics.get("ablation_survives", False))
    failure_mode = metrics.get("failure_mode") or metrics.get("error")

    # Classify and route
    disqualifiers = compute_disqualifiers(metrics, sheet_id, border_ablation, failure_mode)
    tier, tier_reason = compute_tier(metrics)
    routing_action, routing_reason = compute_routing(tier, disqualifiers)

    # Evidence — top 5 OCR tokens by contribution
    evidence_tokens = []
    if ocr_spans is not None and pos_tokens_set is not None:
        scored = []
        for s in ocr_spans:
            text = (s.text or "").upper().strip()
            conf = s.confidence
            contribution = "general"
            weight = conf * 0.1
            for t in pos_tokens_set:
                if t in text:
                    contribution = f"positive:{t}"
                    weight = conf * 2.0
                    break
            if sheet_id_re and sheet_id_re.search(text):
                contribution = "sheet_id_pattern"
                weight = conf * 2.5
            elif dim_re and dim_re.search(text):
                contribution = "negative:dimension"
                weight = -conf * 1.5
            scored.append((weight, s, contribution))
        scored.sort(reverse=True, key=lambda x: x[0])
        for weight, s, contribution in scored[:5]:
            evidence_tokens.append({
                "text": s.text,
                "confidence": round(s.confidence, 3),
                "bbox_px": list(s.bbox_px),
                "contribution": contribution,
                "weight": round(weight, 3),
            })

    return ExtractionReceipt(
        receipt_id=receipt_id,
        doc_hash=doc_hash,
        pdf_filename=pdf_path.name,
        page_idx=page_idx,
        pipeline_version=PIPELINE_VERSION,
        timestamp=datetime.now(timezone.utc).isoformat(),
        suite_id=suite_id,
        sheet_id=sheet_id,
        confidence_tier=tier,
        routing_action=routing_action,
        routing_reason=tier_reason + " | " + routing_reason,
        primitive_recall=round(metrics.get("primitive_recall", 0.0), 4),
        primitive_precision=round(metrics.get("primitive_precision", 0.0), 4),
        iou_follow=round(metrics.get("iou_follow", 0.0), 4),
        cluster_uniqueness=round(metrics.get("cluster_uniqueness", 0.0), 4),
        ocr_top_zone_score=round(metrics.get("ocr_top_zone_score", 0.0), 4),
        cluster_bbox_pt=list(metrics.get("cluster_bbox_pt", [])),
        cluster_n_primitives=int(metrics.get("cluster_n_primitives", 0)),
        cluster_centroid_pt=list(metrics.get("cluster_centroid_pt", [])),
        evidence_ocr_tokens=evidence_tokens,
        n_ocr_spans_total=int(metrics.get("n_ocr_spans", 0)),
        border_ablation_passed=border_ablation,
        trajectory_class=metrics.get("trajectory_class", "UNKNOWN"),
        disqualifiers=disqualifiers,
        runtime_ms=round(metrics.get("elapsed_ms", 0.0), 1),
        ocr_engine=metrics.get("engine", "unknown"),
        ocr_ms=round(metrics.get("ocr_ms", 0.0), 1),
        cost_usd=round(metrics.get("cost_usd", 0.0), 6),
        failure_mode=failure_mode,
    )


# ============================================================
# JSONL replay store
# ============================================================

def persist_receipt(receipt: ExtractionReceipt, run_id: str) -> Path:
    """Append receipt to JSONL store. One file per benchmark run.

    Idempotency: receipt_id includes pipeline_version, so re-running same input
    produces same receipt_id. Caller decides whether to skip-if-exists or replay-all.
    """
    out_path = RECEIPT_DIR / f"{run_id}.jsonl"
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(receipt), default=str) + "\n")
    return out_path


def load_receipts(jsonl_path: Path) -> list[ExtractionReceipt]:
    """Load receipts from JSONL for replay."""
    receipts = []
    if not jsonl_path.exists():
        return receipts
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            d.setdefault("disqualifiers", [])
            d.setdefault("evidence_ocr_tokens", [])
            d.setdefault("cluster_bbox_pt", [])
            d.setdefault("cluster_centroid_pt", [])
            receipts.append(ExtractionReceipt(**d))
    return receipts


def verify_idempotency(receipts: list[ExtractionReceipt]) -> dict:
    """Check that receipts can be replayed deterministically.
    Returns {"idempotent": bool, "n_receipts": int, "unique_ids": int, "duplicates": []}.
    """
    seen_ids = {}
    duplicates = []
    for r in receipts:
        if r.receipt_id in seen_ids:
            duplicates.append({
                "receipt_id": r.receipt_id,
                "first": seen_ids[r.receipt_id],
                "second": f"{r.pdf_filename}:{r.page_idx}",
            })
        else:
            seen_ids[r.receipt_id] = f"{r.pdf_filename}:{r.page_idx}"
    return {
        "idempotent": len(duplicates) == 0,
        "n_receipts": len(receipts),
        "unique_ids": len(seen_ids),
        "duplicates": duplicates,
    }


# ============================================================
# Material Story card export (Phase 1.3 integration data)
# ============================================================

def receipt_to_material_story_payload(receipt: ExtractionReceipt) -> dict:
    """Compact JSON payload for the Material Story confidence badge.
    Frontend consumes this to render:
      - GREEN -> green badge "verified A-101"
      - YELLOW -> yellow badge "ava confirm A-101?"
      - RED -> red badge "review required"
    """
    return {
        "sheet_id": receipt.sheet_id,
        "tier": receipt.confidence_tier,
        "routing": receipt.routing_action,
        "evidence_summary": {
            "ocr_confidence_avg": round(
                sum(t["confidence"] for t in receipt.evidence_ocr_tokens)
                / max(len(receipt.evidence_ocr_tokens), 1), 3,
            ),
            "iou": receipt.iou_follow,
            "cluster_uniqueness": receipt.cluster_uniqueness,
            "border_ablation": receipt.border_ablation_passed,
        },
        "disqualifiers": receipt.disqualifiers,
        "receipt_id": receipt.receipt_id,
        "audit_trail": f"receipts/{receipt.pipeline_version}.jsonl",
    }
