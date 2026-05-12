"""Callback Promises routes (Pass G — Front Desk Hub).

Routes:
  GET  /v1/callbacks                  — list callbacks scoped to caller's suite,
                                        filterable by bucket
  PATCH /v1/callbacks/{id}            — reschedule (receipt: callback_rescheduled)
  POST /v1/callbacks/{id}/complete    — mark complete (receipt: callback_completed)

Law compliance:
  Law #2 — receipts cut on every write (callback_rescheduled / callback_completed).
  Law #3 — fail closed: missing scope headers → 401.
  Law #4 — Yellow tier for writes; Green for reads.
  Law #6 — all queries filter by suite_id resolved from X- headers.
  Law #9 — phone numbers truncated in receipts/logs.

Table: callback_promises (migration: create_callback_promises_table)
  - status CHECK: scheduled | due_today | overdue | completed | cancelled
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
    supabase_update,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/callbacks", tags=["callbacks"])

# ---------------------------------------------------------------------------
# Bucket → status mapping
# ---------------------------------------------------------------------------

_BUCKET_STATUS_MAP: dict[str, list[str]] = {
    "due_today": ["due_today"],
    "overdue": ["overdue"],
    "scheduled": ["scheduled"],
    "completed": ["completed"],
    "all": ["scheduled", "due_today", "overdue", "completed", "cancelled"],
}


# ---------------------------------------------------------------------------
# Helpers — mirrors front_desk.py pattern
# ---------------------------------------------------------------------------


def _resolve_scope(
    x_tenant_id: str | None,
    x_suite_id: str | None,
    x_office_id: str | None,
) -> ScopedIdentity:
    from uuid import UUID

    if not x_tenant_id or not x_suite_id or not x_office_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_SCOPE_HEADERS"},
        )
    try:
        return ScopedIdentity(
            tenant_id=UUID(x_tenant_id),
            suite_id=UUID(x_suite_id),
            office_id=UUID(x_office_id),
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_SCOPE_HEADERS", "message": str(exc)},
        ) from exc


def _redact_phone(phone: str | None) -> str:
    """Truncate phone number for logs/receipts (Law #9)."""
    if not phone:
        return ""
    return phone[:6] + "..." if len(phone) > 6 else phone


def _cut_receipt(
    *,
    receipt_type: str,
    scope: ScopedIdentity,
    outcome: str = "success",
    risk_tier: str = "yellow",
    redacted_inputs: dict[str, Any] | None = None,
    redacted_outputs: dict[str, Any] | None = None,
) -> str:
    rid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    receipt_store.store_receipts(
        [
            {
                "id": rid,
                "receipt_type": receipt_type,
                "action_type": receipt_type,
                "suite_id": str(scope.suite_id),
                "office_id": str(scope.office_id),
                "tenant_id": str(scope.tenant_id),
                "outcome": outcome,
                "tool_used": "callbacks_route",
                "risk_tier": risk_tier,
                "redacted_inputs": redacted_inputs or {},
                "redacted_outputs": redacted_outputs or {},
                "trace_id": get_trace_id(),
                "correlation_id": get_correlation_id(),
                "created_at": now,
            }
        ]
    )
    return rid


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class CallbackRescheduleRequest(BaseModel):
    due_at: str = Field(..., description="New due timestamp in ISO 8601 format")


class CallbackResponse(BaseModel):
    success: bool
    receipt_id: str
    data: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# GET /v1/callbacks
# ---------------------------------------------------------------------------


@router.get("")
async def list_callbacks(
    bucket: str = "all",
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> dict[str, Any]:
    """Return callback_promises rows scoped to the caller's suite.

    bucket: due_today | overdue | scheduled | completed | all (default)
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)

    statuses = _BUCKET_STATUS_MAP.get(bucket, _BUCKET_STATUS_MAP["all"])

    try:
        rows = await supabase_select(
            "callback_promises",
            f"suite_id=eq.{scope.suite_id}&status=in.({','.join(statuses)})",
            order_by="due_at.asc",
            limit=200,
        )
    except SupabaseClientError as exc:
        logger.warning("callbacks_list_failed suite_id=%s: %s", scope.suite_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_ERROR", "message": "DB query failed"},
        ) from exc

    return {"items": rows or [], "count": len(rows or []), "bucket": bucket}


# ---------------------------------------------------------------------------
# PATCH /v1/callbacks/{id}  — reschedule
# ---------------------------------------------------------------------------


@router.patch("/{callback_id}")
async def reschedule_callback(
    callback_id: str,
    body: CallbackRescheduleRequest,
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> CallbackResponse:
    """Reschedule a callback. Emits receipt: callback_rescheduled."""
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)

    # Validate ISO 8601
    try:
        datetime.fromisoformat(body.due_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_INPUT", "message": "due_at must be ISO 8601"},
        ) from exc

    # Fetch row to verify it belongs to this suite (tenant isolation)
    try:
        rows = await supabase_select(
            "callback_promises",
            f"id=eq.{callback_id}&suite_id=eq.{scope.suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning("callbacks_fetch_failed id=%s: %s", callback_id, exc)
        raise HTTPException(status_code=502, detail={"error": "PROVIDER_ERROR"}) from exc

    if not rows:
        # Either not found OR belongs to a different suite — both return 404 (no cross-tenant hints)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "RESOURCE_NOT_FOUND"},
        )

    row = rows[0]

    try:
        updated = await supabase_update(
            "callback_promises",
            f"id=eq.{callback_id}&suite_id=eq.{scope.suite_id}",
            {"due_at": body.due_at, "status": "scheduled"},
        )
    except SupabaseClientError as exc:
        logger.warning("callbacks_reschedule_failed id=%s: %s", callback_id, exc)
        receipt_id = _cut_receipt(
            receipt_type="callback_rescheduled",
            scope=scope,
            outcome="failed",
            redacted_inputs={"callback_id": callback_id, "phone": _redact_phone(row.get("contact_phone"))},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_ERROR", "receipt_id": receipt_id},
        ) from exc

    receipt_id = _cut_receipt(
        receipt_type="callback_rescheduled",
        scope=scope,
        outcome="success",
        redacted_inputs={"callback_id": callback_id, "phone": _redact_phone(row.get("contact_phone"))},
        redacted_outputs={"new_due_at": body.due_at},
    )
    logger.info("callback_rescheduled id=%s suite_id=%s receipt=%s", callback_id, scope.suite_id, receipt_id)
    return CallbackResponse(success=True, receipt_id=receipt_id, data={"callback_id": callback_id, "due_at": body.due_at})


# ---------------------------------------------------------------------------
# POST /v1/callbacks/{id}/complete
# ---------------------------------------------------------------------------


@router.post("/{callback_id}/complete")
async def complete_callback(
    callback_id: str,
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> CallbackResponse:
    """Mark a callback as completed. Emits receipt: callback_completed."""
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)

    # Fetch row — enforce tenant isolation before writing
    try:
        rows = await supabase_select(
            "callback_promises",
            f"id=eq.{callback_id}&suite_id=eq.{scope.suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning("callbacks_fetch_failed id=%s: %s", callback_id, exc)
        raise HTTPException(status_code=502, detail={"error": "PROVIDER_ERROR"}) from exc

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "RESOURCE_NOT_FOUND"},
        )

    row = rows[0]
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        await supabase_update(
            "callback_promises",
            f"id=eq.{callback_id}&suite_id=eq.{scope.suite_id}",
            {"status": "completed", "completed_at": now_iso},
        )
    except SupabaseClientError as exc:
        logger.warning("callbacks_complete_failed id=%s: %s", callback_id, exc)
        receipt_id = _cut_receipt(
            receipt_type="callback_completed",
            scope=scope,
            outcome="failed",
            redacted_inputs={"callback_id": callback_id, "phone": _redact_phone(row.get("contact_phone"))},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_ERROR", "receipt_id": receipt_id},
        ) from exc

    receipt_id = _cut_receipt(
        receipt_type="callback_completed",
        scope=scope,
        outcome="success",
        redacted_inputs={"callback_id": callback_id, "phone": _redact_phone(row.get("contact_phone"))},
        redacted_outputs={"completed_at": now_iso},
    )
    logger.info("callback_completed id=%s suite_id=%s receipt=%s", callback_id, scope.suite_id, receipt_id)
    return CallbackResponse(success=True, receipt_id=receipt_id, data={"callback_id": callback_id, "completed_at": now_iso})
