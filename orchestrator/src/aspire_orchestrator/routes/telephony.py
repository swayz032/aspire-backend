"""Telephony routes — Twilio number search, purchase, and release (Pass 16 — §16.B).

Routes:
  POST /v1/twilio/available-numbers  — search available US numbers (Green tier)
  POST /v1/twilio/purchase-number    — purchase + EL import + attach (Yellow tier)
  POST /v1/twilio/release-number/{phone_number_id} — detach + release (Yellow tier)

Tenant scope: resolved from X-Tenant-Id, X-Suite-Id, X-Office-Id headers.
These headers are injected by the Gateway from the authenticated JWT — never
trust them on webhook routes (Law #6), but they are safe on authenticated
API routes that come through the Gateway.

Capability token: validated server-side before execution (Law #5).
Required scope per action:
  - available-numbers: no capability token required (read-only, Green tier)
  - purchase-number:   telephony:purchase
  - release-number:    telephony:release

Law compliance:
  Law #2 — receipts cut inside service layer.
  Law #3 — fail closed on missing token (401).
  Law #4 — Yellow tier for state-changing ops.
  Law #5 — server-side capability token validation.
  Law #6 — scope from headers (Gateway-trusted path).
  Law #9 — no secrets in logs.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services.token_service import validate_token
from aspire_orchestrator.services.twilio_provisioning import (
    TwilioProvisioningError,
    purchase_number,
    release_number,
    search_available_numbers,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/twilio", tags=["telephony"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AvailableNumbersRequest(BaseModel):
    area_code: str = Field(..., min_length=3, max_length=3, pattern=r"^\d{3}$")
    contains: str | None = Field(None, max_length=10)
    limit: int = Field(20, ge=1, le=50)


class PurchaseNumberRequest(BaseModel):
    phone_number: str = Field(..., pattern=r"^\+1\d{10}$")
    idempotency_key: str = Field(..., min_length=10, max_length=128)
    # Capability token forwarded from orchestrator mint (Law #5)
    capability_token: dict[str, Any] | None = None


class PurchaseNumberResponse(BaseModel):
    success: bool
    phone_number: str
    twilio_sid: str
    elevenlabs_phone_number_id: str
    attached_to_agent_id: str
    receipt_id: str
    purchased_at: str


# ---------------------------------------------------------------------------
# Scope resolution helper
# ---------------------------------------------------------------------------


def _resolve_scope(
    x_tenant_id: str | None,
    x_suite_id: str | None,
    x_office_id: str | None,
) -> ScopedIdentity:
    """Resolve scope from Gateway-injected headers. Fail closed on missing fields."""
    from uuid import UUID
    if not x_tenant_id or not x_suite_id or not x_office_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "MISSING_SCOPE_HEADERS",
                "message": "X-Tenant-Id, X-Suite-Id, X-Office-Id are required",
            },
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


def _validate_capability_token_for(
    cap_token: dict[str, Any] | None,
    scope: ScopedIdentity,
    required_scope: str,
) -> None:
    """Validate capability token server-side (Law #5). Raises 401 on failure."""
    if cap_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "MISSING_CAPABILITY_TOKEN",
                "message": "capability_token required for this action (Law #5)",
            },
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
                "message": result.error_message or "Capability token validation failed",
            },
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/available-numbers")
async def available_numbers(
    req: AvailableNumbersRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Search available US Local phone numbers via Twilio (Green tier, read-only)."""
    # Scope validation (but no capability token required for read-only search)
    _resolve_scope(x_tenant_id, x_suite_id, x_office_id)

    try:
        numbers = await search_available_numbers(
            area_code=req.area_code,
            contains=req.contains,
            limit=req.limit,
        )
    except TwilioProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": exc.code, "message": str(exc)},
        ) from exc

    return {
        "success": True,
        "numbers": [n.model_dump() for n in numbers],
        "count": len(numbers),
    }


@router.post("/purchase-number")
async def purchase_number_route(
    req: PurchaseNumberRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Purchase a Twilio number + import to EL + attach to Sarah (Yellow tier).

    Validates capability token (scope=telephony:purchase) before execution.
    Idempotent on idempotency_key per suite_id.
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_capability_token_for(req.capability_token, scope, "telephony:purchase")

    try:
        purchased = await purchase_number(
            phone_number=req.phone_number,
            scope=scope,
            idempotency_key=req.idempotency_key,
        )
    except TwilioProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY if exc.status_code >= 500 else status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": exc.code, "message": str(exc)},
        ) from exc

    return {
        "success": True,
        "phone_number": purchased.phone_number,
        "twilio_sid": purchased.twilio_sid,
        "elevenlabs_phone_number_id": purchased.elevenlabs_phone_number_id,
        "attached_to_agent_id": purchased.attached_to_agent_id,
        "receipt_id": purchased.receipt_id,
        "purchased_at": purchased.purchased_at,
    }


@router.post("/release-number/{phone_number_id}")
async def release_number_route(
    phone_number_id: str,
    capability_token: dict[str, Any] | None = None,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Detach from EL + release from Twilio + mark released (Yellow tier)."""
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_capability_token_for(capability_token, scope, "telephony:release")

    try:
        # Pass 18 fix THREAT-015: pass scope so release_number binds the
        # phone_number_id lookup to the authenticated suite. Cross-tenant
        # release with a valid same-tenant token is now blocked with 404.
        await release_number(phone_number_id, scope=scope)
    except TwilioProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if exc.status_code == 404 else status.HTTP_502_BAD_GATEWAY,
            detail={"error": exc.code, "message": str(exc)},
        ) from exc

    return {"success": True, "phone_number_id": phone_number_id}
