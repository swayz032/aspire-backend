"""SMS outbound route (Pass 16 — §16.E).

Route:
  POST /v1/sms/send — Yellow tier: send outbound SMS via Twilio.

Capability token required: scope = telephony:sms_send.
Tenant scope from X- headers (Gateway-trusted).

Law compliance:
  Law #2 — receipt cut inside sms_io.send_sms.
  Law #3 — fail closed on missing capability token.
  Law #4 — Yellow tier.
  Law #5 — server-side capability token validation.
  Law #9 — body content not logged verbatim.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services.sms_io import SmsIoError, send_sms
from aspire_orchestrator.services.token_service import validate_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sms", tags=["sms"])


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class SmsSendRequest(BaseModel):
    thread_memory_id: str = Field(..., min_length=10, max_length=128)
    body: str = Field(..., min_length=1, max_length=1600)
    idempotency_key: str = Field(..., min_length=10, max_length=128)
    capability_token: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Shared helpers (minimal — same pattern as other routes)
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


def _cap_token_id(cap_token: dict[str, Any] | None) -> str:
    """Extract deterministic capability_token_id for receipt tracing."""
    if not cap_token:
        return ""
    if cap_token.get("id"):
        return str(cap_token["id"])
    sig = cap_token.get("signature") or cap_token.get("token") or ""
    if sig:
        import hashlib
        return hashlib.sha256(str(sig).encode()).hexdigest()[:16]
    return ""


def _validate_cap_token(
    cap_token: dict[str, Any] | None,
    scope: ScopedIdentity,
    required_scope: str,
) -> None:
    if cap_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_CAPABILITY_TOKEN"},
        )
    result = validate_token(
        cap_token,
        expected_suite_id=str(scope.suite_id),
        expected_office_id=str(scope.office_id),
        required_scope=required_scope,
    )
    if not result.valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": result.error.value if result.error else "INVALID_TOKEN",
                "message": result.error_message or "",
            },
        )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/send")
async def sms_send(
    req: SmsSendRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Yellow tier: send an outbound SMS via Twilio.

    Requires capability token with scope=telephony:sms_send.
    Idempotent on idempotency_key (minute-bucketed SHA256).
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(req.capability_token, scope, "telephony:sms_send")

    try:
        result = await send_sms(
            thread_memory_id=req.thread_memory_id,
            body=req.body,
            scope=scope,
            capability_token=str(req.capability_token),
            idempotency_key=req.idempotency_key,
            trace_id=get_trace_id(),
            correlation_id=get_correlation_id(),
            capability_token_id=_cap_token_id(req.capability_token),
        )
    except SmsIoError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY
            if exc.status_code >= 500
            else status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": exc.code, "message": str(exc)},
        ) from exc

    return {
        "success": True,
        "message_sid": result["message_sid"],
        "status": result["status"],
        "receipt_id": result["receipt_id"],
    }
