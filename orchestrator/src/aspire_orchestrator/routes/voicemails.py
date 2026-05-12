"""Voicemail management routes — `/v1/voicemails/*` (Pass I remediation).

Routes:
  POST /v1/voicemails/{id}/mark-reviewed — sets read_at on frontdesk_voicemails
  DELETE /v1/voicemails/{id}            — soft-delete (sets archived_at, no row removal)

Law compliance:
  Law #2 — receipts cut on every write (voicemail_marked_reviewed / voicemail_deleted).
  Law #3 — fail closed: missing scope headers → 401.
  Law #4 — Green tier for mark-reviewed; Yellow for soft-delete.
  Law #6 — all queries filter by suite_id resolved from X- headers.
  Law #9 — phone numbers truncated in receipts/logs.

Table: frontdesk_voicemails
  PK: voicemail_id (uuid)
  Soft-delete column: archived_at (timestamptz, nullable)
  Mark-reviewed column: read_at (timestamptz, nullable)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
    supabase_update,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/voicemails", tags=["voicemails"])


# ---------------------------------------------------------------------------
# Helpers — mirrors callbacks.py pattern
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
                "tool_used": "voicemails_route",
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
# POST /v1/voicemails/{id}/mark-reviewed  (Green tier)
# ---------------------------------------------------------------------------


@router.post("/{voicemail_id}/mark-reviewed")
async def mark_voicemail_reviewed(
    voicemail_id: str,
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> dict[str, Any]:
    """Set read_at on the voicemail row.

    Idempotent — if already marked reviewed, returns success with existing timestamp.
    Receipt type: voicemail_marked_reviewed (Green tier, informational state change)
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)

    # Tenant isolation: fetch row scoped to suite_id before writing
    try:
        rows = await supabase_select(
            "frontdesk_voicemails",
            f"voicemail_id=eq.{voicemail_id}&suite_id=eq.{scope.suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning("voicemail_fetch_failed id=%s: %s", voicemail_id, exc)
        raise HTTPException(status_code=502, detail={"error": "PROVIDER_ERROR"}) from exc

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "RESOURCE_NOT_FOUND"},
        )

    row = rows[0]

    # Idempotency: already reviewed → return success + existing timestamp
    if row.get("read_at"):
        return {
            "success": True,
            "voicemail_id": voicemail_id,
            "read_at": row["read_at"],
            "already_reviewed": True,
        }

    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        await supabase_update(
            "frontdesk_voicemails",
            f"voicemail_id=eq.{voicemail_id}&suite_id=eq.{scope.suite_id}",
            {"read_at": now_iso},
        )
    except SupabaseClientError as exc:
        logger.warning("voicemail_mark_reviewed_failed id=%s: %s", voicemail_id, exc)
        receipt_id = _cut_receipt(
            receipt_type="voicemail_marked_reviewed",
            scope=scope,
            outcome="failed",
            risk_tier="green",
            redacted_inputs={"voicemail_id": voicemail_id},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_ERROR", "receipt_id": receipt_id},
        ) from exc

    receipt_id = _cut_receipt(
        receipt_type="voicemail_marked_reviewed",
        scope=scope,
        outcome="success",
        risk_tier="green",
        redacted_inputs={"voicemail_id": voicemail_id},
        redacted_outputs={"read_at": now_iso},
    )
    logger.info("voicemail_marked_reviewed id=%s suite_id=%s receipt=%s", voicemail_id, scope.suite_id, receipt_id)
    return {
        "success": True,
        "voicemail_id": voicemail_id,
        "read_at": now_iso,
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# DELETE /v1/voicemails/{id}  (Yellow tier — soft delete)
# ---------------------------------------------------------------------------


@router.delete("/{voicemail_id}")
async def soft_delete_voicemail(
    voicemail_id: str,
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> dict[str, Any]:
    """Soft-delete a voicemail by setting archived_at.

    The row is NOT removed from the database (Law #2: immutable audit trail).
    Idempotent — already-archived voicemails return success.
    Receipt type: voicemail_deleted (Yellow tier)
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)

    # Tenant isolation: fetch row scoped to suite_id before writing
    try:
        rows = await supabase_select(
            "frontdesk_voicemails",
            f"voicemail_id=eq.{voicemail_id}&suite_id=eq.{scope.suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning("voicemail_fetch_failed id=%s: %s", voicemail_id, exc)
        raise HTTPException(status_code=502, detail={"error": "PROVIDER_ERROR"}) from exc

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "RESOURCE_NOT_FOUND"},
        )

    row = rows[0]

    # Idempotency: already archived → return success
    if row.get("archived_at"):
        return {
            "success": True,
            "voicemail_id": voicemail_id,
            "archived_at": row["archived_at"],
            "already_deleted": True,
        }

    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        await supabase_update(
            "frontdesk_voicemails",
            f"voicemail_id=eq.{voicemail_id}&suite_id=eq.{scope.suite_id}",
            {"archived_at": now_iso},
        )
    except SupabaseClientError as exc:
        logger.warning("voicemail_soft_delete_failed id=%s: %s", voicemail_id, exc)
        receipt_id = _cut_receipt(
            receipt_type="voicemail_deleted",
            scope=scope,
            outcome="failed",
            redacted_inputs={"voicemail_id": voicemail_id},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_ERROR", "receipt_id": receipt_id},
        ) from exc

    receipt_id = _cut_receipt(
        receipt_type="voicemail_deleted",
        scope=scope,
        outcome="success",
        risk_tier="yellow",
        redacted_inputs={"voicemail_id": voicemail_id},
        redacted_outputs={"archived_at": now_iso},
    )
    logger.info("voicemail_soft_deleted id=%s suite_id=%s receipt=%s", voicemail_id, scope.suite_id, receipt_id)
    return {
        "success": True,
        "voicemail_id": voicemail_id,
        "archived_at": now_iso,
        "receipt_id": receipt_id,
    }
