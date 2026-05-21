"""Missing-input cascade orchestrator — Wave 19c Drew Self-Resolution.

Orchestrates parallel resolver dispatch for a single MissingInputCandidate.
Routes by category, runs all resolver methods concurrently, returns the
best result. If any method reaches confidence >= 0.85, the cascade
resolves the candidate and the caller marks the DB row resolved.

If ALL methods fail to reach 0.85, returns ResolutionOutcome(resolved=False)
and the missing_input row stays open for contractor.

Design:
  - 90s total cap (asyncio.wait_for on gather)
  - Receipt per cascade run: action_type "blueprint.cascade.{category}"
  - Idempotent: re-running on an already-resolved row is a no-op

Law compliance:
  Law #1 — Cascade decides nothing. Returns outcome; Drew decides persistence.
  Law #2 — Receipt emitted for every cascade run (success and failure).
  Law #3 — Never raises; all resolver errors are caught inside resolvers.
  Law #6 — suite_id/project_id scoped throughout.
  Law #9 — No raw OCR or PII in receipts.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.blueprint.resolvers.base import (
    RESOLVED_CONFIDENCE_THRESHOLD,
    MissingInputCandidate,
    MissingInputCategory,
    ResolverResult,
)

_log = logging.getLogger(__name__)

_CASCADE_TIMEOUT_S: float = 90.0


# ---------------------------------------------------------------------------
# Category routing — keyword → MissingInputCategory
# ---------------------------------------------------------------------------

_CATEGORY_PATTERNS: list[tuple[MissingInputCategory, re.Pattern[str]]] = [
    (MissingInputCategory.DISCIPLINE, re.compile(
        r"\bdiscipline\b|\bclassif\b|\btrade\s+type\b|\bsheet\s+type\b",
        re.IGNORECASE,
    )),
    (MissingInputCategory.SCALE, re.compile(
        r"\bscale\b|\bcalibrat\b|\bdrawing\s+scale\b",
        re.IGNORECASE,
    )),
    (MissingInputCategory.ADDENDA, re.compile(
        r"\baddend(?:um|a)?\b|\brevision\s+hierarch|\bsupersess|\brev(?:ision)?\s+block\b",
        re.IGNORECASE,
    )),
    (MissingInputCategory.DIMENSION, re.compile(
        r"\bdimension|\broom\s+size\b|\bfloor\s+plan\b|\bsquare\s+foot|\bsf\b",
        re.IGNORECASE,
    )),
    (MissingInputCategory.DETAIL, re.compile(
        r"\bdetail\b|\bpartition\b|\bdoor\s+schedule\b|\bcallout\b|\bkeyed\b",
        re.IGNORECASE,
    )),
    (MissingInputCategory.SCHEDULE, re.compile(
        r"\bschedule\b|\blegend\b|\bequipment\b|\bfixture\b|\bpanel\b|\bcircuit\b",
        re.IGNORECASE,
    )),
]


def categorize_description(description: str) -> MissingInputCategory:
    """Classify a missing_input description into a resolver category.

    Returns MissingInputCategory.UNKNOWN if no pattern matches.
    """
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(description):
            return category
    return MissingInputCategory.UNKNOWN


# ---------------------------------------------------------------------------
# Resolver registry — category → resolver instance (built once)
# ---------------------------------------------------------------------------

def _build_resolver_registry() -> dict[MissingInputCategory, Any]:
    """Build resolver instances. Import-safe (lazy import of resolvers)."""
    try:
        from aspire_orchestrator.services.blueprint.resolvers.discipline_resolver import DisciplineResolver
        from aspire_orchestrator.services.blueprint.resolvers.scale_resolver import ScaleResolver
        from aspire_orchestrator.services.blueprint.resolvers.addenda_resolver import AddendaResolver
        from aspire_orchestrator.services.blueprint.resolvers.dimension_resolver import DimensionResolver
        from aspire_orchestrator.services.blueprint.resolvers.detail_resolver import DetailResolver
        from aspire_orchestrator.services.blueprint.resolvers.schedule_resolver import ScheduleResolver
        return {
            MissingInputCategory.DISCIPLINE: DisciplineResolver(),
            MissingInputCategory.SCALE: ScaleResolver(),
            MissingInputCategory.ADDENDA: AddendaResolver(),
            MissingInputCategory.DIMENSION: DimensionResolver(),
            MissingInputCategory.DETAIL: DetailResolver(),
            MissingInputCategory.SCHEDULE: ScheduleResolver(),
        }
    except ImportError as exc:
        _log.warning("missing_input_cascade: resolver import failed: %s", exc)
        return {}


_RESOLVER_REGISTRY: dict[MissingInputCategory, Any] | None = None


def _get_registry() -> dict[MissingInputCategory, Any]:
    global _RESOLVER_REGISTRY
    if _RESOLVER_REGISTRY is None:
        _RESOLVER_REGISTRY = _build_resolver_registry()
    return _RESOLVER_REGISTRY


# ---------------------------------------------------------------------------
# Outcome dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolutionOutcome:
    """Result of a cascade run for one MissingInputCandidate."""
    missing_input_id: str
    project_id: str
    resolved: bool
    best_result: ResolverResult | None
    all_results: list[ResolverResult] = field(default_factory=list)
    category: MissingInputCategory = MissingInputCategory.UNKNOWN
    cascade_receipt_id: str = ""


# ---------------------------------------------------------------------------
# Core cascade entry point
# ---------------------------------------------------------------------------

async def resolve_or_emit(
    candidate: MissingInputCandidate,
    correlation_id: str,
) -> ResolutionOutcome:
    """Attempt to resolve a single missing_input candidate.

    Routes to the category's resolver, runs all methods in parallel with
    a 90s total cap. Emits a receipt regardless of outcome. The CALLER
    (drew_blueprint.py) handles DB persistence — this function is pure
    outcome computation + receipt.

    Args:
        candidate: Fully-populated MissingInputCandidate with sheets + OCR.
        correlation_id: Trace ID from the parent Drew stage (for receipt chain).

    Returns:
        ResolutionOutcome with resolved=True and best_result.confidence >= 0.85
        if resolved, else resolved=False.
    """
    receipt_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()

    registry = _get_registry()
    resolver = registry.get(candidate.category)

    if resolver is None:
        _log.info(
            "missing_input_cascade: no resolver for category=%s mi_id=%s",
            candidate.category.value,
            candidate.missing_input_id[:8],
        )
        _emit_receipt(
            receipt_id=receipt_id,
            correlation_id=correlation_id,
            candidate=candidate,
            outcome="skipped",
            resolved=False,
            best_result=None,
            all_results=[],
            now_iso=now_iso,
        )
        return ResolutionOutcome(
            missing_input_id=candidate.missing_input_id,
            project_id=candidate.project_id,
            resolved=False,
            best_result=None,
            all_results=[],
            category=candidate.category,
            cascade_receipt_id=receipt_id,
        )

    # Dispatch all resolver methods in parallel, bounded by 90s.
    try:
        all_results: list[ResolverResult] = await asyncio.wait_for(
            resolver.resolve(candidate),
            timeout=_CASCADE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        _log.warning(
            "missing_input_cascade: %s resolver timed out after %ss for mi_id=%s",
            candidate.category.value,
            _CASCADE_TIMEOUT_S,
            candidate.missing_input_id[:8],
        )
        all_results = []
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "missing_input_cascade: resolver raised unexpectedly mi_id=%s: %s",
            candidate.missing_input_id[:8],
            type(exc).__name__,
        )
        all_results = []

    # Pick the highest-confidence result.
    confident_results = [r for r in all_results if r.is_confident]
    best_result: ResolverResult | None = (
        max(confident_results, key=lambda r: r.confidence)
        if confident_results else None
    )
    resolved = best_result is not None

    outcome_str = "resolved" if resolved else "unresolved"
    _emit_receipt(
        receipt_id=receipt_id,
        correlation_id=correlation_id,
        candidate=candidate,
        outcome=outcome_str,
        resolved=resolved,
        best_result=best_result,
        all_results=all_results,
        now_iso=now_iso,
    )

    return ResolutionOutcome(
        missing_input_id=candidate.missing_input_id,
        project_id=candidate.project_id,
        resolved=resolved,
        best_result=best_result,
        all_results=all_results,
        category=candidate.category,
        cascade_receipt_id=receipt_id,
    )


# ---------------------------------------------------------------------------
# Receipt emission
# ---------------------------------------------------------------------------

# Allow-list of evidence keys that are safe to surface on the cascade receipt.
# Numeric counters (call counts, sheets processed, row counts) and short
# enum-like strings (skip reasons, OCR tier) only. Anything else is dropped
# to avoid leaking raw OCR, PII, or large payloads onto receipts.
_RECEIPT_EVIDENCE_KEY_ALLOWLIST: frozenset[str] = frozenset({
    # Numeric counters
    "azure_calls_made",
    "azure_calls_capped_at",
    "vision_tokens_used",
    "vision_calls_made",
    "sheets_processed",
    "sheets_tried",
    "schedule_sheets_found",
    "schedule_sheets_with_png",
    "table_rows_parsed",
    "total_rows_extracted",
    "tables_found",
    "best_confidence",
    "doors_detected",
    # Enum-like short strings
    "ocr_tier",
    "skip_reason",
    "png_source",
    "error",
    "timeout_s",
})


def _safe_evidence_for_receipt(evidence: dict[str, Any]) -> dict[str, Any]:
    """Return an allow-listed subset of resolver evidence for the cascade receipt.

    Drops any key not in :data:`_RECEIPT_EVIDENCE_KEY_ALLOWLIST` and any value
    that is not a primitive (int/float/bool/str/None). Strings longer than
    120 chars are truncated. This is a Law #9 guard: receipts never carry
    raw OCR text or table payloads.
    """
    if not isinstance(evidence, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in evidence.items():
        if k not in _RECEIPT_EVIDENCE_KEY_ALLOWLIST:
            continue
        if isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
            out[k] = v
        elif isinstance(v, str):
            out[k] = v if len(v) <= 120 else v[:117] + "..."
        # Anything else (lists, dicts, bytes) is dropped.
    return out


def _emit_receipt(
    *,
    receipt_id: str,
    correlation_id: str,
    candidate: MissingInputCandidate,
    outcome: str,
    resolved: bool,
    best_result: ResolverResult | None,
    all_results: list[ResolverResult],
    now_iso: str,
) -> None:
    """Emit an immutable receipt for this cascade run (Law #2)."""
    try:
        from aspire_orchestrator.services.receipt_store import store_receipts
        method_summary = {
            r.method: {
                "confidence": round(r.confidence, 3),
                "resolved": r.resolved,
                # Wave 19.1b — Surface a safe-typed subset of resolver
                # evidence on the cascade receipt so cost/cap counters
                # (e.g. ``azure_calls_made``, ``vision_tokens_used`` in
                # Wave 19.2) are auditable without per-resolver receipt
                # code. We allow only numeric counts and short string
                # skip reasons; raw OCR / row payloads are stripped.
                "evidence": _safe_evidence_for_receipt(r.evidence),
            }
            for r in all_results
        }
        store_receipts([{
            "id": receipt_id,
            "outcome": outcome,
            "action_type": f"blueprint.cascade.{candidate.category.value}",
            "receipt_type": f"blueprint.cascade.{candidate.category.value}",
            "created_at": now_iso,
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": "skillpack:drew-blueprint",
            "correlation_id": correlation_id,
            "suite_id": candidate.suite_id,
            "metadata": {
                "missing_input_id": candidate.missing_input_id,
                "project_id": candidate.project_id,
                "category": candidate.category.value,
                "resolved": resolved,
                "best_method": best_result.method if best_result else None,
                "best_confidence": round(best_result.confidence, 3) if best_result else None,
                "method_count": len(all_results),
                "method_summary": method_summary,
            },
        }])
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "missing_input_cascade: receipt emission failed mi_id=%s: %s",
            candidate.missing_input_id[:8],
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Bulk helper: build candidates from DB rows + project context
# ---------------------------------------------------------------------------

def build_candidate(
    row: dict[str, Any],
    sheets: list[dict[str, Any]],
    symbols_by_sheet: dict[str, list[dict[str, Any]]],
    ocr_by_sheet: dict[str, str],
    *,
    data_provider: Any | None = None,
) -> MissingInputCandidate:
    """Build a MissingInputCandidate from a blueprint_missing_inputs DB row.

    Attaches project context (sheets, symbols, OCR) from the caller's
    already-loaded data (avoid duplicate DB queries per candidate).

    Args:
        row: A blueprint_missing_inputs row dict.
        sheets: Active sheets for the project (already loaded by the caller).
        symbols_by_sheet: ``sheet_id -> list[blueprint_symbols row]`` index.
        ocr_by_sheet: ``sheet_id -> ocr_text`` index.
        data_provider: Wave 19.1a — Optional per-cascade-run
            ``SheetDataProvider`` instance. Pass to give resolvers access
            to PNG bytes (from Supabase Storage) and YOLO detections
            (from blueprint_symbols) without each resolver having to
            re-query. Default ``None`` keeps all pre-19.1 callers
            (existing tests, legacy code paths) working unchanged.
    """
    description = str(row.get("description") or "")
    category = categorize_description(description)
    return MissingInputCandidate(
        missing_input_id=str(row.get("id") or ""),
        project_id=str(row.get("project_id") or ""),
        suite_id=str(row.get("suite_id") or ""),
        office_id=row.get("office_id"),
        description=description,
        suggested_resolution=row.get("suggested_resolution"),
        category=category,
        sheets=sheets,
        symbols_by_sheet=symbols_by_sheet,
        ocr_by_sheet=ocr_by_sheet,
        data_provider=data_provider,
    )
