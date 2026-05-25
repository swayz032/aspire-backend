"""Phase 1.3 — Drew Extraction Receipt Schema + Story V2 Routing.

CORRECTED 2026-05-25: prior version routed to Ava (wrong namespace —
Ava lives in Service Hub front desk, not Estimate Studio). The actual
Estimate Studio architecture is:
  Drew (AI worker) -> populates BlueprintStoryV2 zones -> rendered in
  ChapterLeftPane + StoryPanelV2. Tim is the Service Hub Manager rail
  (right side) BUT TIM IS NOT BUILT YET. So YELLOW routing surfaces in
  the "review pending" UI state; RED routing creates OpenQuestion entries
  in the chapter's open_questions zone.

Every blueprint page extraction produces an ExtractionReceipt. Receipts are:
  - Deterministic (receipt_id = SHA-1 of doc_hash + page_idx + pipeline_version)
  - Replay-able (JSONL on disk; load + reproduce routing decision)
  - Audit-grade (every routing action traceable to evidence)
  - Fail-closed (any anomaly downgrades tier to OPEN_QUESTION)

Story V2 Routing (aligned with Aspire-desktop ChapterZones):
  GREEN  -> AUTO_POPULATE   (add to in_scope/tasks/buy_list autonomously)
  YELLOW -> REVIEW_PENDING  (add with confidence_low flag; Tim helps when built)
  RED    -> OPEN_QUESTION   (add to chapter.open_questions for user resolution)

Disqualifiers (override tier to OPEN_QUESTION regardless of cluster metrics):
  - No sheet_id extracted
  - Border ablation fails
  - Trajectory NULL_COLLAPSE / NO_BASELINE
  - OCR failure_mode set
  - Cost > budget cap (10x typical)

Discipline chapter classification: maps sheet code prefix to one of
  general | demolition | architectural | structural | plumbing |
  mechanical | electrical | fire | civil
matching the 9 discipline metas in blueprintsStoryV2.ts.
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

PIPELINE_VERSION = "srr_azure_v5_phase1.3_storyv2"


# ============================================================
# Story V2 classifiers (Python ports of Aspire-desktop frontend)
# ============================================================
# Source of truth: Aspire-desktop/lib/api/blueprintsStoryV2.ts (DISCIPLINE_MAP,
# disciplinePrefixFromCode) and Aspire-desktop/lib/scope/archetype.ts (TITLE_RULES,
# codeNumberToArchetype). Drew emits identity; frontend renders zones from these.

import re as _re

# 9 chapter disciplines (matches DisciplineMeta.key in blueprintsStoryV2.ts)
DISCIPLINE_KEYS = (
    "general", "demolition", "architectural", "structural",
    "plumbing", "mechanical", "electrical", "fire", "civil",
)

# Sheet code prefix -> discipline (mirrors DISCIPLINE_MAP in blueprintsStoryV2.ts)
DISCIPLINE_PREFIX_MAP = {
    "a": "architectural", "ad": "demolition", "d": "demolition", "ex": "demolition",
    "p": "plumbing", "m": "mechanical", "h": "mechanical",
    "e": "electrical", "ed": "electrical", "el": "electrical", "t": "electrical", "lv": "electrical",
    "fp": "fire", "f": "fire", "fa": "fire",
    "s": "structural",
    "c": "civil", "l": "civil",
    "g": "general", "cover": "general", "ls": "general",
}

# Same regex as disciplinePrefixFromCode() at blueprintsStoryV2.ts
_SHEET_PREFIX_RE = _re.compile(r"^([A-Za-z]{1,3})(?:[-_\s]*\d|\d)")


def classify_discipline(sheet_code: Optional[str]) -> str:
    """Map sheet_code (e.g., 'A-301', 'FP1.0') -> discipline key. Defaults 'general'."""
    if not sheet_code:
        return "general"
    code = sheet_code.strip()
    if not code:
        return "general"
    m = _SHEET_PREFIX_RE.match(code)
    if not m:
        return "general"
    prefix = m.group(1).lower()
    return DISCIPLINE_PREFIX_MAP.get(prefix, "general")


# 14 sheet archetypes (matches SheetArchetype in archetype.ts)
ARCHETYPES = (
    "cover", "index", "notes", "schedule", "plan", "rcp", "detail",
    "section", "elevation", "demo", "lifesafety", "riser", "legend", "sheet",
)

# Title-substring rules (order matches archetype.ts TITLE_RULES — specific first)
_TITLE_RULES = [
    ((_re.compile(r"cover\s*sheet", _re.I), _re.compile(r"title\s*sheet", _re.I)), "cover"),
    ((_re.compile(r"sheet\s*index", _re.I),), "index"),
    ((_re.compile(r"life\s*safety", _re.I), _re.compile(r"\begress\b", _re.I)), "lifesafety"),
    ((_re.compile(r"demolition", _re.I), _re.compile(r"existing\s*conditions", _re.I)), "demo"),
    ((_re.compile(r"reflected\s*ceiling", _re.I), _re.compile(r"\brcp\b", _re.I)), "rcp"),
    ((_re.compile(r"riser\s*diagram", _re.I), _re.compile(r"\briser\b", _re.I),
      _re.compile(r"one[-\s]*line\s*diagram", _re.I)), "riser"),
    ((_re.compile(r"\bschedule\b", _re.I),), "schedule"),
    ((_re.compile(r"general\s*notes", _re.I), _re.compile(r"\bnotes\b", _re.I)), "notes"),
    ((_re.compile(r"\blegend\b", _re.I), _re.compile(r"\bsymbols?\b", _re.I)), "legend"),
    ((_re.compile(r"\bdetails?\b", _re.I),), "detail"),
    ((_re.compile(r"\bsection\b", _re.I),), "section"),
    ((_re.compile(r"\belevation\b", _re.I),), "elevation"),
    ((_re.compile(r"\bplan\b", _re.I), _re.compile(r"floor\s*plan", _re.I)), "plan"),
]


def _code_number_to_archetype(sheet_code: Optional[str]) -> Optional[str]:
    """Mirrors codeNumberToArchetype in archetype.ts. Returns archetype or None."""
    if not sheet_code:
        return None
    m = _re.search(r"(\d+)", sheet_code)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    if n < 10:
        return "notes"
    if n < 100:
        return "schedule"
    if n < 200:
        return "plan"
    if n < 300:
        return "riser"
    if n < 400:
        return "detail"
    if n < 500:
        return "section"
    if n < 600:
        return "elevation"
    return None


def classify_archetype(sheet_code: Optional[str], sheet_title: Optional[str]) -> str:
    """Resolve archetype from title (priority) then code number. Defaults 'sheet'."""
    title = (sheet_title or "").strip()
    if title:
        for patterns, archetype in _TITLE_RULES:
            for pat in patterns:
                if pat.search(title):
                    return archetype
    from_code = _code_number_to_archetype(sheet_code)
    if from_code:
        return from_code
    return "sheet"


# Story V2 routing actions (aligned with ChapterZones in blueprintsStoryV2.ts)
ROUTING_AUTO_POPULATE = "AUTO_POPULATE"    # GREEN -> fills in_scope/tasks/buy_list autonomously
ROUTING_REVIEW_PENDING = "REVIEW_PENDING"  # YELLOW -> low-confidence flag; surfaces in review state
ROUTING_OPEN_QUESTION = "OPEN_QUESTION"    # RED / disqualified -> chapter.open_questions entry

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
    sheet_title: Optional[str] = None
    page_number: Optional[int] = None
    discipline_key: str = "general"
    sheet_archetype: str = "sheet"
    confidence_tier: str = "RED"
    routing_action: str = "OPEN_QUESTION"
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
    """Story V2 routing decision. Disqualifiers override tier to OPEN_QUESTION (fail-closed).

    GREEN -> AUTO_POPULATE: frontend fills chapter.in_scope / tasks / buy_list autonomously.
    YELLOW -> REVIEW_PENDING: surfaces with confidence_low flag; Tim rail helps when built.
    RED / disqualified -> OPEN_QUESTION: pushed to chapter.open_questions for user resolution.
    """
    if disqualifiers:
        return ROUTING_OPEN_QUESTION, f"Disqualifiers present: {','.join(disqualifiers)}"
    if tier == "GREEN":
        return ROUTING_AUTO_POPULATE, "GREEN tier — autonomous zone population"
    if tier == "YELLOW":
        return ROUTING_REVIEW_PENDING, "YELLOW tier — surfaces in review-pending state"
    return ROUTING_OPEN_QUESTION, "RED tier — routed to chapter.open_questions"


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
    sheet_title = metrics.get("sheet_title")
    border_ablation = bool(metrics.get("ablation_survives", False))
    failure_mode = metrics.get("failure_mode") or metrics.get("error")

    # Story V2 identity classification (drives chapter assignment + per-sheet story)
    discipline_key = classify_discipline(sheet_id)
    sheet_archetype = classify_archetype(sheet_id, sheet_title)

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
        sheet_title=sheet_title,
        page_number=int(metrics.get("page_number", page_idx + 1)),
        discipline_key=discipline_key,
        sheet_archetype=sheet_archetype,
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
            d.setdefault("sheet_title", None)
            d.setdefault("page_number", None)
            d.setdefault("discipline_key", "general")
            d.setdefault("sheet_archetype", "sheet")
            # Back-compat: legacy receipts used AVA-namespace routing actions.
            legacy_map = {
                "AUTO_BIND": ROUTING_AUTO_POPULATE,
                "ROUTE_TO_AVA": ROUTING_REVIEW_PENDING,
                "BLOCK_MANUAL": ROUTING_OPEN_QUESTION,
            }
            if d.get("routing_action") in legacy_map:
                d["routing_action"] = legacy_map[d["routing_action"]]
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
# Story V2 payload export (Phase 1.3 integration data)
# ============================================================

def receipt_to_story_v2_payload(receipt: ExtractionReceipt) -> dict:
    """Compact JSON payload for the BlueprintStoryV2 frontend contract.

    Frontend (Aspire-desktop/lib/api/blueprintsStoryV2.ts) uses this to:
      - Group the sheet into a chapter via discipline_key (one of 9 metas)
      - Pick per-sheet story template via sheet_archetype (one of 14 types)
      - Render confidence_tier dot (GREEN/YELLOW/RED) on chapter + sheet cards
      - Drive routing_action UI state:
          AUTO_POPULATE -> fills chapter.in_scope/tasks/buy_list autonomously
          REVIEW_PENDING -> surfaces with confidence_low flag
          OPEN_QUESTION -> entry into chapter.open_questions
    """
    return {
        "sheet_id": receipt.sheet_id,
        "sheet_title": receipt.sheet_title,
        "page_number": receipt.page_number,
        "discipline_key": receipt.discipline_key,
        "sheet_archetype": receipt.sheet_archetype,
        "tier": receipt.confidence_tier,
        "routing_action": receipt.routing_action,
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


# Backward-compat alias — callers using the legacy name continue working.
receipt_to_material_story_payload = receipt_to_story_v2_payload
