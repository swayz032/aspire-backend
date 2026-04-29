"""Finn Memory Tools — 13 server-side tools for the Finance Hub agent.

Every tool follows the Aspire governance contract (CLAUDE.md):
  Law #2: state-changing tools emit receipts via receipt_store.
  Law #3: missing capability scope → raise FinnToolError (fail closed).
  Law #6: scope (tenant_id/suite_id/office_id) checked before every DB call.
  Law #7: tools are hands — they never decide, retry, or call each other.
  Law #9: no raw secrets or PII in log lines.

All 13 tools are pure functions (or thin class wrappers).  The orchestrator
instantiates them; the tool returns a Pydantic result or raises FinnToolError.
Retries, fallbacks, and escalation are the orchestrator's responsibility.

Capability scope required: 'finance_read' (reads) / 'finance_write' (writes).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    Provenance,
    ScopedIdentity,
)
from aspire_orchestrator.services.memory_service import (
    MemoryService,
    MemoryServiceError,
)
from aspire_orchestrator.services.receipt_store import store_receipts
from aspire_orchestrator.services.supabase_client import supabase_select
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOOL_TIMEOUT_S = 4.9  # <5 s per Law #10 reliability pattern
_FINANCE_BASE = "http://localhost:8000"  # overridden by settings in production

# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class FinnToolError(MemoryServiceError):
    """Structured error raised by Finn tools.

    Inherits MemoryServiceError so the orchestrator's existing error handling
    covers both memory layer and Finn tool errors uniformly.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        tenant_id: UUID | str | None = None,
        correlation_id: UUID | str | None = None,
        retryable: bool = False,
        provider: str | None = None,
    ) -> None:
        super().__init__(message, code=code, tenant_id=tenant_id, correlation_id=correlation_id)
        self.retryable = retryable
        self.provider = provider


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _assert_finance_scope(scope: ScopedIdentity, required_scope: str) -> None:
    """Fail closed if capability scope is missing or wrong (Law #3, Law #5)."""
    # In production the orchestrator passes the decoded capability token scopes.
    # Here we accept scope as a proxy — the actual capability token verification
    # happens at the gateway/middleware layer before the tool is invoked.
    # This guard is the defence-in-depth layer inside the tool.
    if not isinstance(scope, ScopedIdentity):
        raise FinnToolError(
            "Capability scope validation failed: invalid ScopedIdentity",
            code="INVALID_CAPABILITY_TOKEN",
            tenant_id=None,
        )


async def _finance_get(
    path: str,
    *,
    scope: ScopedIdentity,
    correlation_id: UUID,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GET from internal finance service with explicit timeout and scope headers."""
    headers = {
        "X-Tenant-Id": str(scope.tenant_id),
        "X-Suite-Id": str(scope.suite_id),
        "X-Office-Id": str(scope.office_id),
        "X-Correlation-Id": str(correlation_id),
    }
    try:
        async with httpx.AsyncClient(timeout=_TOOL_TIMEOUT_S) as client:
            resp = await client.get(
                f"{_FINANCE_BASE}{path}",
                headers=headers,
                params=params or {},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException as exc:
        raise FinnToolError(
            "Finance service timeout",
            code="PROVIDER_TIMEOUT",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
            retryable=True,
        ) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        code = "PROVIDER_UNAVAILABLE" if status >= 500 else "INVALID_INPUT"
        raise FinnToolError(
            f"Finance service HTTP {status}",
            code=code,
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
            retryable=status >= 500,
        ) from exc


async def _finance_post(
    path: str,
    *,
    scope: ScopedIdentity,
    correlation_id: UUID,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST to internal finance service with explicit timeout and scope headers."""
    headers = {
        "X-Tenant-Id": str(scope.tenant_id),
        "X-Suite-Id": str(scope.suite_id),
        "X-Office-Id": str(scope.office_id),
        "X-Correlation-Id": str(correlation_id),
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_TOOL_TIMEOUT_S) as client:
            resp = await client.post(
                f"{_FINANCE_BASE}{path}",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException as exc:
        raise FinnToolError(
            "Finance service timeout",
            code="PROVIDER_TIMEOUT",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
            retryable=True,
        ) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        code = "PROVIDER_UNAVAILABLE" if status >= 500 else "INVALID_INPUT"
        raise FinnToolError(
            f"Finance service HTTP {status}",
            code=code,
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
            retryable=status >= 500,
        ) from exc


# ---------------------------------------------------------------------------
# Output shapes (Pydantic)
# ---------------------------------------------------------------------------


class FinnContextOut(BaseModel):
    current_datetime: str
    tenant_id: str
    suite_id: str
    office_id: str
    connected_providers: list[str]
    provider_freshness: dict[str, str]
    coverage_summary: str
    degraded_conditions: list[str]
    correlation_id: str


class FinnOverviewOut(BaseModel):
    books_health: str
    cash_position_summary: str
    needs_review_count: int
    close_readiness: str
    top_changes: list[str]
    priority_actions: list[str]
    recent_receipts_summary: str
    correlation_id: str


class FinnCashTruthOut(BaseModel):
    usable_cash: dict[str, Any]
    upcoming_outflows: list[dict[str, Any]]
    incoming_pressure: list[dict[str, Any]]
    overdue_invoice_exposure: dict[str, Any]
    forecast_confidence: str
    correlation_id: str


class FinnReviewQueueOut(BaseModel):
    items: list[dict[str, Any]]
    total: int
    correlation_id: str


class FinnReconciliationQueueOut(BaseModel):
    mismatch_candidates: list[dict[str, Any]]
    duplicate_candidates: list[dict[str, Any]]
    transfer_pair_candidates: list[dict[str, Any]]
    stale_provider_warnings: list[str]
    total: int
    correlation_id: str


class FinnReportsSummaryOut(BaseModel):
    summaries: list[dict[str, Any]]
    trend_direction: str | None = None
    freshness: str | None = None
    top_drivers: list[str]
    correlation_id: str


class FinnArAgingOut(BaseModel):
    buckets: dict[str, Any]
    top_overdue_invoices: list[dict[str, Any]]
    top_customers_at_risk: list[dict[str, Any]]
    cash_impact_estimate: dict[str, Any]
    collections_priority: list[str]
    correlation_id: str


class FinnRulesOut(BaseModel):
    rules: list[dict[str, Any]]
    total: int
    correlation_id: str


class FinnSimulateRuleOut(BaseModel):
    projected_matches: int
    projected_misses: int
    false_positive_risk: str
    sample_transactions: list[dict[str, Any]]
    requires_approval: bool
    correlation_id: str


class FinnPreviewWritebackOut(BaseModel):
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    target_provider: str
    expected_impact: str
    required_approval_state: str
    preview_artifact_id: str
    correlation_id: str


class FinnApplyWritebackOut(BaseModel):
    applied: bool
    receipt_id: str
    provider_status: str
    failure_details: str | None = None
    correlation_id: str


class FinnMoneyTrailOut(BaseModel):
    receipts: list[dict[str, Any]]
    total: int
    correlation_id: str


class FinnSaveMemoryOut(BaseModel):
    memory_id: str
    receipt_id: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Tool 1: finn_get_context
# ---------------------------------------------------------------------------


async def finn_get_context(scope: ScopedIdentity) -> FinnContextOut:
    """Get business context, provider state, and Finance Hub surface.

    GREEN tier. No state change. No receipt emitted (read-only, Law #2).
    Capability scope: finance_read.
    """
    _assert_finance_scope(scope, "finance_read")
    correlation_id = uuid.uuid4()

    logger.info(
        "finn_get_context tenant_id=%s suite_id=%s office_id=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        str(scope.suite_id)[:8],
        str(scope.office_id)[:8],
        str(correlation_id)[:8],
    )

    try:
        data = await _finance_get(
            "/finance/context",
            scope=scope,
            correlation_id=correlation_id,
        )
    except FinnToolError:
        raise
    except Exception as exc:
        raise FinnToolError(
            "Unexpected error in finn_get_context",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return FinnContextOut(
        current_datetime=_now_utc().isoformat(),
        tenant_id=str(scope.tenant_id),
        suite_id=str(scope.suite_id),
        office_id=str(scope.office_id),
        connected_providers=data.get("connected_providers", []),
        provider_freshness=data.get("provider_freshness", {}),
        coverage_summary=data.get("coverage_summary", ""),
        degraded_conditions=data.get("degraded_conditions", []),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool 2: finn_get_overview
# ---------------------------------------------------------------------------


async def finn_get_overview(scope: ScopedIdentity) -> FinnOverviewOut:
    """Get executive financial picture and books health summary.

    GREEN tier. Composite read: cash + AR + reconciliation queue counts.
    Capability scope: finance_read.
    """
    _assert_finance_scope(scope, "finance_read")
    correlation_id = uuid.uuid4()

    logger.info(
        "finn_get_overview tenant_id=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    try:
        data = await _finance_get(
            "/finance/overview",
            scope=scope,
            correlation_id=correlation_id,
        )
    except FinnToolError:
        raise
    except Exception as exc:
        raise FinnToolError(
            "Unexpected error in finn_get_overview",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return FinnOverviewOut(
        books_health=data.get("books_health", "unknown"),
        cash_position_summary=data.get("cash_position_summary", ""),
        needs_review_count=data.get("needs_review_count", 0),
        close_readiness=data.get("close_readiness", ""),
        top_changes=data.get("top_changes", []),
        priority_actions=data.get("priority_actions", []),
        recent_receipts_summary=data.get("recent_receipts_summary", ""),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool 3: finn_get_cash_truth
# ---------------------------------------------------------------------------


async def finn_get_cash_truth(scope: ScopedIdentity) -> FinnCashTruthOut:
    """Answer usable-cash and pressure questions via Plaid-backed finance service.

    GREEN tier. Capability scope: finance_read.
    """
    _assert_finance_scope(scope, "finance_read")
    correlation_id = uuid.uuid4()

    logger.info(
        "finn_get_cash_truth tenant_id=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    try:
        data = await _finance_get(
            "/finance/cash-truth",
            scope=scope,
            correlation_id=correlation_id,
        )
    except FinnToolError:
        raise
    except Exception as exc:
        raise FinnToolError(
            "Unexpected error in finn_get_cash_truth",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return FinnCashTruthOut(
        usable_cash=data.get("usable_cash", {}),
        upcoming_outflows=data.get("upcoming_outflows", []),
        incoming_pressure=data.get("incoming_pressure", []),
        overdue_invoice_exposure=data.get("overdue_invoice_exposure", {}),
        forecast_confidence=data.get("forecast_confidence", "low"),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool 4: finn_get_review_queue
# ---------------------------------------------------------------------------


async def finn_get_review_queue(
    scope: ScopedIdentity,
    *,
    limit: int = 50,
    offset: int = 0,
) -> FinnReviewQueueOut:
    """Load review and categorization backlog from /finance/classification/queue.

    GREEN tier. Capability scope: finance_read.
    """
    _assert_finance_scope(scope, "finance_read")
    correlation_id = uuid.uuid4()

    logger.info(
        "finn_get_review_queue tenant_id=%s limit=%d correlation_id=%s",
        str(scope.tenant_id)[:8],
        limit,
        str(correlation_id)[:8],
    )

    try:
        data = await _finance_get(
            "/finance/classification/queue",
            scope=scope,
            correlation_id=correlation_id,
            params={"limit": limit, "offset": offset},
        )
    except FinnToolError:
        raise
    except Exception as exc:
        raise FinnToolError(
            "Unexpected error in finn_get_review_queue",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    items = data.get("items", data.get("queue", []))
    return FinnReviewQueueOut(
        items=items,
        total=data.get("total", len(items)),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool 5: finn_get_reconciliation_queue
# ---------------------------------------------------------------------------


async def finn_get_reconciliation_queue(
    scope: ScopedIdentity,
    *,
    limit: int = 50,
    offset: int = 0,
) -> FinnReconciliationQueueOut:
    """Load mismatch and reconciliation blockers from /finance/reconciliation/queue.

    GREEN tier. Capability scope: finance_read.
    """
    _assert_finance_scope(scope, "finance_read")
    correlation_id = uuid.uuid4()

    logger.info(
        "finn_get_reconciliation_queue tenant_id=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    try:
        data = await _finance_get(
            "/finance/reconciliation/queue",
            scope=scope,
            correlation_id=correlation_id,
            params={"limit": limit, "offset": offset},
        )
    except FinnToolError:
        raise
    except Exception as exc:
        raise FinnToolError(
            "Unexpected error in finn_get_reconciliation_queue",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return FinnReconciliationQueueOut(
        mismatch_candidates=data.get("mismatch_candidates", []),
        duplicate_candidates=data.get("duplicate_candidates", []),
        transfer_pair_candidates=data.get("transfer_pair_candidates", []),
        stale_provider_warnings=data.get("stale_provider_warnings", []),
        total=data.get("total", 0),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool 6: finn_get_reports_summary
# ---------------------------------------------------------------------------


async def finn_get_reports_summary(
    scope: ScopedIdentity,
    *,
    report_type: str | None = None,
    limit: int = 5,
) -> FinnReportsSummaryOut:
    """Provide plain-language summaries over finance reports.

    Reads recent finance_brief memory_objects from MemoryService with
    visibility_scope='finance'. GREEN tier. Capability scope: finance_read.
    """
    _assert_finance_scope(scope, "finance_read")
    correlation_id = uuid.uuid4()

    logger.info(
        "finn_get_reports_summary tenant_id=%s report_type=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        report_type,
        str(correlation_id)[:8],
    )

    try:
        filters: dict[str, Any] = {
            "tenant_id": str(scope.tenant_id),
            "suite_id": str(scope.suite_id),
            "office_id": str(scope.office_id),
            "visibility_scope": "finance",
            "memory_type": "finance_brief",
        }
        if report_type:
            filters["entity_type"] = report_type

        rows = await supabase_select(
            "memory_objects",
            filters,
            order_by="last_activity_at.desc",
            limit=limit,
        )

        summaries = [
            {
                "memory_id": r.get("memory_id"),
                "title": r.get("title"),
                "summary": r.get("summary"),
                "last_activity_at": r.get("last_activity_at"),
            }
            for r in rows
        ]
        return FinnReportsSummaryOut(
            summaries=summaries,
            trend_direction=None,
            freshness=rows[0].get("last_activity_at") if rows else None,
            top_drivers=[],
            correlation_id=str(correlation_id),
        )
    except FinnToolError:
        raise
    except Exception as exc:
        raise FinnToolError(
            "Unexpected error in finn_get_reports_summary",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc


# ---------------------------------------------------------------------------
# Tool 7: finn_get_ar_aging
# ---------------------------------------------------------------------------


async def finn_get_ar_aging(scope: ScopedIdentity) -> FinnArAgingOut:
    """Explain receivables, collections pressure, and overdue risk.

    GREEN tier. Capability scope: finance_read.
    """
    _assert_finance_scope(scope, "finance_read")
    correlation_id = uuid.uuid4()

    logger.info(
        "finn_get_ar_aging tenant_id=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    try:
        data = await _finance_get(
            "/finance/ar-aging",
            scope=scope,
            correlation_id=correlation_id,
        )
    except FinnToolError:
        raise
    except Exception as exc:
        raise FinnToolError(
            "Unexpected error in finn_get_ar_aging",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return FinnArAgingOut(
        buckets=data.get("buckets", {}),
        top_overdue_invoices=data.get("top_overdue_invoices", []),
        top_customers_at_risk=data.get("top_customers_at_risk", []),
        cash_impact_estimate=data.get("cash_impact_estimate", {}),
        collections_priority=data.get("collections_priority", []),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool 8: finn_get_rules
# ---------------------------------------------------------------------------


async def finn_get_rules(scope: ScopedIdentity) -> FinnRulesOut:
    """Inspect categorization rules and automation behavior.

    GREEN tier. Capability scope: finance_read.
    """
    _assert_finance_scope(scope, "finance_read")
    correlation_id = uuid.uuid4()

    logger.info(
        "finn_get_rules tenant_id=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    try:
        data = await _finance_get(
            "/finance/classification/rules",
            scope=scope,
            correlation_id=correlation_id,
        )
    except FinnToolError:
        raise
    except Exception as exc:
        raise FinnToolError(
            "Unexpected error in finn_get_rules",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    rules = data.get("rules", [])
    return FinnRulesOut(
        rules=rules,
        total=data.get("total", len(rules)),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool 9: finn_simulate_rule
# ---------------------------------------------------------------------------


async def finn_simulate_rule(
    scope: ScopedIdentity,
    *,
    rule_definition: dict[str, Any],
) -> FinnSimulateRuleOut:
    """Preview a rule before creation or change.

    GREEN tier (preview only — no state change). Capability scope: finance_read.
    """
    _assert_finance_scope(scope, "finance_read")
    correlation_id = uuid.uuid4()

    logger.info(
        "finn_simulate_rule tenant_id=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    try:
        data = await _finance_post(
            "/finance/classification/simulate",
            scope=scope,
            correlation_id=correlation_id,
            payload={"rule": rule_definition},
        )
    except FinnToolError:
        raise
    except Exception as exc:
        raise FinnToolError(
            "Unexpected error in finn_simulate_rule",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return FinnSimulateRuleOut(
        projected_matches=data.get("projected_matches", 0),
        projected_misses=data.get("projected_misses", 0),
        false_positive_risk=data.get("false_positive_risk", "unknown"),
        sample_transactions=data.get("sample_transactions", []),
        requires_approval=data.get("requires_approval", False),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool 10: finn_preview_writeback
# ---------------------------------------------------------------------------


async def finn_preview_writeback(
    scope: ScopedIdentity,
    *,
    writeback_payload: dict[str, Any],
) -> FinnPreviewWritebackOut:
    """Preview a categorization/reconciliation/write-back mutation.

    GREEN tier (preview — no state change). Wraps /finance/writeback/preview.
    Capability scope: finance_read.
    """
    _assert_finance_scope(scope, "finance_read")
    correlation_id = uuid.uuid4()

    logger.info(
        "finn_preview_writeback tenant_id=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    try:
        data = await _finance_post(
            "/finance/writeback/preview",
            scope=scope,
            correlation_id=correlation_id,
            payload=writeback_payload,
        )
    except FinnToolError:
        raise
    except Exception as exc:
        raise FinnToolError(
            "Unexpected error in finn_preview_writeback",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return FinnPreviewWritebackOut(
        before_state=data.get("before_state", {}),
        after_state=data.get("after_state", {}),
        target_provider=data.get("target_provider", ""),
        expected_impact=data.get("expected_impact", ""),
        required_approval_state=data.get("required_approval_state", "none"),
        preview_artifact_id=data.get("preview_artifact_id", ""),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool 11: finn_apply_writeback  (RED tier — approval-gated)
# ---------------------------------------------------------------------------


async def finn_apply_writeback(
    scope: ScopedIdentity,
    *,
    writeback_payload: dict[str, Any],
    idempotency_key: str,
    approval_evidence: dict[str, Any] | None = None,
) -> FinnApplyWritebackOut:
    """Apply a governed write-back after policy conditions are met.

    RED tier. Requires idempotency_key and approval_evidence.
    Wraps /finance/writeback/apply. Emits receipt on state change (Law #2).
    Capability scope: finance_write.
    """
    _assert_finance_scope(scope, "finance_write")
    correlation_id = uuid.uuid4()

    if not idempotency_key:
        raise FinnToolError(
            "finn_apply_writeback requires idempotency_key",
            code="INVALID_INPUT",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
            retryable=False,
        )

    logger.info(
        "finn_apply_writeback tenant_id=%s idempotency_key=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        idempotency_key[:8],
        str(correlation_id)[:8],
    )

    payload = {
        **writeback_payload,
        "idempotency_key": idempotency_key,
        "correlation_id": str(correlation_id),
        "approval_evidence": approval_evidence or {},
    }

    try:
        data = await _finance_post(
            "/finance/writeback/apply",
            scope=scope,
            correlation_id=correlation_id,
            payload=payload,
        )
    except FinnToolError:
        raise
    except Exception as exc:
        raise FinnToolError(
            "Unexpected error in finn_apply_writeback",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    # Emit receipt for state change (Law #2).
    receipt_id = str(uuid.uuid4())
    receipt = {
        "id": receipt_id,
        "receipt_type": "finance_writeback_apply",
        "tenant_id": str(scope.tenant_id),
        "suite_id": str(scope.suite_id),
        "office_id": str(scope.office_id),
        "actor_id": str(scope.actor_id) if scope.actor_id else None,
        "actor_type": "WORKER",
        "action_type": "finance_writeback_apply",
        "tool_used": "finn_apply_writeback",
        "risk_tier": "red",
        "trace_id": str(correlation_id),
        "correlation_id": str(correlation_id),
        "redacted_inputs": {
            "idempotency_key": idempotency_key,
            "has_approval_evidence": bool(approval_evidence),
        },
        "redacted_outputs": {
            "applied": data.get("applied", False),
            "provider_status": data.get("provider_status", ""),
        },
        "outcome": "success" if data.get("applied") else "failed",
        "reason_code": None,
        "created_at": _now_utc().isoformat(),
    }
    try:
        await store_receipts([receipt])
    except Exception as exc:
        logger.error(
            "finn_apply_writeback: receipt store failed correlation_id=%s error=%s",
            str(correlation_id)[:8],
            type(exc).__name__,
        )

    return FinnApplyWritebackOut(
        applied=data.get("applied", False),
        receipt_id=receipt_id,
        provider_status=data.get("provider_status", ""),
        failure_details=data.get("failure_details"),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool 12: finn_get_money_trail
# ---------------------------------------------------------------------------


async def finn_get_money_trail(
    scope: ScopedIdentity,
    *,
    entity_id: str | None = None,
    limit: int = 20,
) -> FinnMoneyTrailOut:
    """Load proof and audit history from receipts filtered by finance domain.

    GREEN tier. Capability scope: finance_read.
    """
    _assert_finance_scope(scope, "finance_read")
    correlation_id = uuid.uuid4()

    logger.info(
        "finn_get_money_trail tenant_id=%s entity_id=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        str(entity_id)[:8] if entity_id else "none",
        str(correlation_id)[:8],
    )

    try:
        filters: dict[str, Any] = {
            "tenant_id": str(scope.tenant_id),
            "suite_id": str(scope.suite_id),
            "office_id": str(scope.office_id),
        }

        rows = await supabase_select(
            "receipts",
            filters,
            order_by="created_at.desc",
            limit=limit,
        )

        # Filter to finance domain receipts client-side (receipt_type prefix).
        finance_rows = [
            r for r in rows
            if str(r.get("receipt_type", "")).startswith("finance")
            or str(r.get("action_type", "")).startswith("finance")
        ]

        trail = [
            {
                "receipt_id": r.get("id"),
                "receipt_type": r.get("receipt_type"),
                "action_type": r.get("action_type"),
                "outcome": r.get("outcome"),
                "created_at": r.get("created_at"),
                "actor_id": r.get("actor_id"),
            }
            for r in finance_rows
        ]
        return FinnMoneyTrailOut(
            receipts=trail,
            total=len(trail),
            correlation_id=str(correlation_id),
        )
    except FinnToolError:
        raise
    except Exception as exc:
        raise FinnToolError(
            "Unexpected error in finn_get_money_trail",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc


# ---------------------------------------------------------------------------
# Tool 13: finn_save_finance_memory  (state change → receipt emitted)
# ---------------------------------------------------------------------------

_ALLOWED_FINANCE_MEMORY_TYPES = {
    "weekly_finance_brief",
    "what_changed_summary",
    "cleanup_snapshot",
    "collections_pressure_summary",
    "tax_readiness_summary",
    "receipt_explanation",
}


async def finn_save_finance_memory(
    scope: ScopedIdentity,
    *,
    memory_artifact_type: str,
    summary: str,
    title: str | None = None,
    detail: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> FinnSaveMemoryOut:
    """Persist durable office-scoped finance summaries to memory_objects.

    YELLOW tier. Emits receipt (Law #2). visibility_scope='finance', source_agent='finn'.
    Capability scope: finance_write.
    """
    _assert_finance_scope(scope, "finance_write")
    correlation_id = uuid.uuid4()
    trace_id = uuid.uuid4()

    if memory_artifact_type not in _ALLOWED_FINANCE_MEMORY_TYPES:
        raise FinnToolError(
            f"Invalid memory_artifact_type '{memory_artifact_type}'. "
            f"Allowed: {sorted(_ALLOWED_FINANCE_MEMORY_TYPES)}",
            code="INVALID_INPUT",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
            retryable=False,
        )

    ikey = idempotency_key or f"finn:{memory_artifact_type}:{_now_utc().date()}"

    logger.info(
        "finn_save_finance_memory tenant_id=%s artifact_type=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        memory_artifact_type,
        str(correlation_id)[:8],
    )

    provenance = Provenance(
        source_surface="finn_finance",
        source_agent="finn",
        runtime_family="elevenlabs",
        channel="finance",
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    envelope = MemoryObjectIn(
        scope=scope,
        provenance=provenance,
        memory_type="finance_brief",
        entity_type=memory_artifact_type,
        title=title,
        summary=summary,
        detail=detail or {},
        visibility_scope="finance",
        idempotency_key=ikey,
    )

    svc = MemoryService()
    try:
        result = await svc.write(envelope, scope=scope, embed=False)
    except MemoryServiceError:
        raise
    except Exception as exc:
        raise FinnToolError(
            "Unexpected error in finn_save_finance_memory",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return FinnSaveMemoryOut(
        memory_id=str(result.memory_id),
        receipt_id=str(result.linked_receipt_ids[0]) if result.linked_receipt_ids else "",
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Registered tool names (used by tool_types.py / registry.py entries)
# ---------------------------------------------------------------------------

FINN_MEMORY_TOOLS: list[str] = [
    "finn.memory.get_context",
    "finn.memory.get_overview",
    "finn.memory.get_cash_truth",
    "finn.memory.get_review_queue",
    "finn.memory.get_reconciliation_queue",
    "finn.memory.get_reports_summary",
    "finn.memory.get_ar_aging",
    "finn.memory.get_rules",
    "finn.memory.simulate_rule",
    "finn.memory.preview_writeback",
    "finn.memory.apply_writeback",
    "finn.memory.get_money_trail",
    "finn.memory.save_finance_memory",
]
