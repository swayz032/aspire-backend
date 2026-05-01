"""Front Desk configuration CRUD routes (Pass 16 — §16.F).

Routes:
  GET  /v1/front-desk/config                       — get current config
  PATCH /v1/front-desk/config                      — versioned write (Yellow)
  POST /v1/front-desk/config/test-call             — trigger test inbound (Yellow)
  POST /v1/front-desk/routing-contacts             — create contact (Yellow)
  PATCH /v1/front-desk/routing-contacts/{id}       — update contact (Yellow)
  DELETE /v1/front-desk/routing-contacts/{id}      — delete contact (Yellow)

Versioning strategy: PATCH inserts a NEW row with version_no = max+1.
The is_current trigger in migration 102 marks only the highest version current.
No UPDATE on existing rows — append-only per Law #2.

Law compliance:
  Law #2 — receipts cut on every state change.
  Law #3 — fail closed on missing capability token.
  Law #4 — Yellow tier for all writes.
  Law #5 — server-side capability token validation.
  Law #6 — scope from X- headers (Gateway-trusted).

Table assumptions (migration 102):
  - front_desk_configs: id, tenant_id, suite_id, office_id, version_no, is_current,
    public_number_mode, catch_mode, after_hours_mode, busy_mode,
    greeting_name_override, pronunciation_override,
    last_forwarding_test_at, last_forwarding_test_result, forwarding_status
  - front_desk_routing_contacts: id, office_id, suite_id, tenant_id, role,
    label, phone, sip_uri, email, is_active
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
    supabase_update,
)
from aspire_orchestrator.services.token_service import validate_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/front-desk", tags=["front-desk"])

_ASPIRE_ORCHESTRATOR_URL = "https://orchestrator.aspire.app"
_TIMEOUT_SECONDS = 4.5


# ---------------------------------------------------------------------------
# Shared helpers (same pattern as telephony.py)
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


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class FrontDeskConfigPatch(BaseModel):
    public_number_mode: str | None = None
    catch_mode: str | None = None
    after_hours_mode: str | None = None
    busy_mode: str | None = None
    greeting_name_override: str | None = None
    pronunciation_override: str | None = None
    capability_token: dict[str, Any] | None = None


class RoutingContactCreate(BaseModel):
    role: str = Field(..., description="owner|sales|support|billing|scheduling|custom")
    label: str = Field(..., min_length=1, max_length=100)
    phone: str | None = Field(None, pattern=r"^\+\d{7,15}$")
    sip_uri: str | None = None
    email: str | None = None
    capability_token: dict[str, Any] | None = None


class RoutingContactPatch(BaseModel):
    label: str | None = None
    phone: str | None = Field(None, pattern=r"^\+\d{7,15}$")
    sip_uri: str | None = None
    email: str | None = None
    is_active: bool | None = None
    capability_token: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/config")
async def get_config(
    office_id: str | None = None,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Return the current front_desk_configs row + routing contacts."""
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    oid = office_id or str(scope.office_id)

    config_rows = await supabase_select(
        "front_desk_configs",
        f"office_id=eq.{oid}&is_current=eq.true",
        order_by="version_no.desc",
        limit=1,
    )
    config = config_rows[0] if config_rows else {}

    routing_rows = await supabase_select(
        "front_desk_routing_contacts",
        f"office_id=eq.{oid}&is_active=eq.true",
    )

    return {
        "success": True,
        "config": config,
        "routing_contacts": routing_rows,
    }


@router.patch("/config")
async def patch_config(
    req: FrontDeskConfigPatch,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Versioned write — inserts NEW row with version_no = max+1 (Yellow tier)."""
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(req.capability_token, scope, "front_desk:config_save")

    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Fetch current max version
    current_rows = await supabase_select(
        "front_desk_configs",
        f"office_id=eq.{office_id}",
        order_by="version_no.desc",
        limit=1,
    )
    current = current_rows[0] if current_rows else {}
    current_version = int(current.get("version_no", 0))

    # Build new row merging current fields with patch fields
    new_row: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "version_no": current_version + 1,
        "is_current": True,
        "public_number_mode": req.public_number_mode or current.get("public_number_mode", "ASPIRE_NUMBER"),
        "catch_mode": req.catch_mode or current.get("catch_mode", "APP_AND_PHONE_SIMUL_RING"),
        "after_hours_mode": req.after_hours_mode or current.get("after_hours_mode", "take_message"),
        "busy_mode": req.busy_mode or current.get("busy_mode", "take_message"),
        "greeting_name_override": req.greeting_name_override
            if req.greeting_name_override is not None
            else current.get("greeting_name_override") or "",
        "pronunciation_override": req.pronunciation_override
            if req.pronunciation_override is not None
            else current.get("pronunciation_override") or "",
        "created_at": now,
    }

    inserted = await supabase_insert("front_desk_configs", new_row)

    # Pass 19 §3.5.5 — invalidate LKG personalization cache for this office.
    # Without this, calls within the next 10min would get stale routing phones
    # from the in-process LKG cache.
    try:
        from aspire_orchestrator.routes.sarah import invalidate_personalization_cache_for_office
        invalidate_personalization_cache_for_office(office_id)
    except Exception as cache_exc:
        # Non-fatal: log and continue. Cache will expire on its own TTL.
        logger.warning(
            "patch_config cache_invalidation_failed office_id=%s: %s",
            office_id,
            cache_exc,
        )

    receipt_store.store_receipts([{
        "id": receipt_id,
        "receipt_type": "front_desk_config_save",
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "outcome": "success",
        "action_type": "front_desk_config_save",
        "tool_used": "front_desk_config",
        "risk_tier": "yellow",
        "redacted_outputs": {"version_no": new_row["version_no"], "config_id": new_row["id"]},
        "trace_id": get_trace_id(),
        "correlation_id": get_correlation_id(),
        "capability_token_id": _cap_token_id(req.capability_token) or None,
        "created_at": now,
    }])

    return {
        "success": True,
        "config": inserted,
        "receipt_id": receipt_id,
    }


@router.post("/config/test-call")
async def test_call(
    capability_token: dict[str, Any] | None = None,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Fire a test inbound call to the office's purchased number (Yellow tier).

    Uses Twilio Calls.create to trigger an inbound call via TwiML that
    immediately connects to the EL agent (exercises the personalization webhook
    end-to-end).
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(capability_token, scope, "front_desk:test_call")

    office_id = str(scope.office_id)
    suite_id = str(scope.suite_id)
    tenant_id = str(scope.tenant_id)
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    account_sid = settings.twilio_account_sid
    auth_token = settings.twilio_auth_token
    if not account_sid or not auth_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "MISSING_TWILIO_CREDENTIALS"},
        )

    # Resolve the office's purchased number
    number_rows = await supabase_select(
        "tenant_phone_numbers",
        f"office_id=eq.{office_id}&status=eq.active&voice_enabled=eq.true",
        limit=1,
    )
    if not number_rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "NO_ACTIVE_NUMBER", "message": "No active voice-enabled number for this office"},
        )
    number_row = number_rows[0]
    to_number = number_row["phone_number"]

    # Build TwiML that triggers a call to our number (exercises personalization webhook)
    twiml_url = f"{_ASPIRE_ORCHESTRATOR_URL}/v1/sarah/personalization"
    call_payload = {
        "To": to_number,
        "From": to_number,   # Test call: call the number from itself
        "Url": twiml_url,
        "Method": "POST",
    }
    twilio_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"

    test_result = "success"
    twilio_call_sid = ""
    try:
        async with httpx.AsyncClient(
            auth=(account_sid, auth_token),
            timeout=_TIMEOUT_SECONDS,
        ) as client:
            resp = await client.post(twilio_url, data=call_payload)
        if resp.status_code >= 400:
            test_result = "failed"
            logger.error("test_call twilio error status=%d", resp.status_code)
        else:
            twilio_call_sid = resp.json().get("sid", "")
    except Exception as exc:
        test_result = "failed"
        logger.error("test_call failed: %s", exc)

    # Update last_forwarding_test_at + result on current config
    try:
        await supabase_update(
            "front_desk_configs",
            f"office_id=eq.{office_id}&is_current=eq.true",
            {
                "last_forwarding_test_at": now,
                "last_forwarding_test_result": test_result,
                "forwarding_status": "VERIFIED" if test_result == "success" else "LAST_TEST_FAILED",
            },
        )
    except SupabaseClientError as exc:
        logger.error("test_call update failed: %s", exc)

    receipt_store.store_receipts([{
        "id": receipt_id,
        "receipt_type": "front_desk_test_call",
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "outcome": test_result,
        "action_type": "front_desk_test_call",
        "tool_used": "front_desk_config",
        "risk_tier": "yellow",
        "redacted_inputs": {"to_number": to_number},
        "redacted_outputs": {"call_sid": twilio_call_sid},
        "trace_id": get_trace_id(),
        "correlation_id": get_correlation_id(),
        "capability_token_id": _cap_token_id(capability_token) or None,
        "created_at": now,
    }])

    return {
        "success": test_result == "success",
        "test_result": test_result,
        "call_sid": twilio_call_sid,
        "receipt_id": receipt_id,
    }


@router.post("/routing-contacts")
async def create_routing_contact(
    req: RoutingContactCreate,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Create a routing contact (Yellow tier)."""
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(req.capability_token, scope, "front_desk:routing_write")

    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    row: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "role": req.role,
        "label": req.label,
        "phone": req.phone or "",
        "sip_uri": req.sip_uri or "",
        "email": req.email or "",
        "is_active": True,
        "created_at": now,
    }
    inserted = await supabase_insert("front_desk_routing_contacts", row)

    receipt_store.store_receipts([{
        "id": receipt_id,
        "receipt_type": "routing_contact_create",
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "outcome": "success",
        "action_type": "routing_contact_create",
        "tool_used": "front_desk_routing",
        "risk_tier": "yellow",
        "redacted_outputs": {"contact_id": row["id"], "role": req.role},
        "trace_id": get_trace_id(),
        "correlation_id": get_correlation_id(),
        "capability_token_id": _cap_token_id(req.capability_token) or None,
        "created_at": now,
    }])

    return {"success": True, "contact": inserted, "receipt_id": receipt_id}


@router.patch("/routing-contacts/{contact_id}")
async def update_routing_contact(
    contact_id: str,
    req: RoutingContactPatch,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Update a routing contact (Yellow tier)."""
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(req.capability_token, scope, "front_desk:routing_write")

    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    update_data: dict[str, Any] = {"updated_at": now}
    if req.label is not None:
        update_data["label"] = req.label
    if req.phone is not None:
        update_data["phone"] = req.phone
    if req.sip_uri is not None:
        update_data["sip_uri"] = req.sip_uri
    if req.email is not None:
        update_data["email"] = req.email
    if req.is_active is not None:
        update_data["is_active"] = req.is_active

    updated = await supabase_update(
        "front_desk_routing_contacts",
        f"id=eq.{contact_id}&office_id=eq.{office_id}",
        update_data,
    )

    receipt_store.store_receipts([{
        "id": receipt_id,
        "receipt_type": "routing_contact_update",
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "outcome": "success",
        "action_type": "routing_contact_update",
        "tool_used": "front_desk_routing",
        "risk_tier": "yellow",
        "redacted_inputs": {"contact_id": contact_id},
        "trace_id": get_trace_id(),
        "correlation_id": get_correlation_id(),
        "capability_token_id": _cap_token_id(req.capability_token) or None,
        "created_at": now,
    }])

    return {"success": True, "contact": updated, "receipt_id": receipt_id}


@router.delete("/routing-contacts/{contact_id}")
async def delete_routing_contact(
    contact_id: str,
    capability_token: dict[str, Any] | None = None,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Soft-delete a routing contact (set is_active=false, Yellow tier)."""
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(capability_token, scope, "front_desk:routing_write")

    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Soft-delete: set is_active=false (Law #2 — no hard DELETE)
    await supabase_update(
        "front_desk_routing_contacts",
        f"id=eq.{contact_id}&office_id=eq.{office_id}",
        {"is_active": False, "updated_at": now},
    )

    receipt_store.store_receipts([{
        "id": receipt_id,
        "receipt_type": "routing_contact_delete",
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "outcome": "success",
        "action_type": "routing_contact_delete",
        "tool_used": "front_desk_routing",
        "risk_tier": "yellow",
        "redacted_inputs": {"contact_id": contact_id},
        "trace_id": get_trace_id(),
        "correlation_id": get_correlation_id(),
        "capability_token_id": _cap_token_id(capability_token) or None,
        "created_at": now,
    }])

    return {"success": True, "contact_id": contact_id, "receipt_id": receipt_id}


# ---------------------------------------------------------------------------
# GET /v1/front-desk/forwarding-instructions (Pass 19 Lane B §3.1)
# ---------------------------------------------------------------------------


@router.get("/forwarding-instructions")
async def get_forwarding_instructions(
    phone: str,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
    x_capability_token: str | None = Header(None, alias="X-Aspire-Capability-Token"),
) -> dict[str, Any]:
    """Resolve carrier-specific conditional-forwarding instructions for a phone number.

    Used by FORWARD_EXISTING mode on the Front Desk Setup page. The frontend
    calls this after the owner enters their existing number. We resolve the
    carrier via Twilio Lookup v2 and return the appropriate forwarding codes.

    Capability scope: front_desk:read (GREEN tier — read-only).
    Law #9: phone number prefix only logged; full number never in logs/receipts.
    """
    import re as _re
    from aspire_orchestrator.services.forwarding_instructions import resolve_forwarding_instructions
    from aspire_orchestrator.services.twilio_provisioning import (
        TwilioProvisioningError,
        lookup_carrier,
    )

    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)

    # Capability token (parse from header string if provided)
    cap_token_dict: dict[str, Any] | None = None
    if x_capability_token:
        try:
            import json as _json
            cap_token_dict = _json.loads(x_capability_token)
        except Exception:
            cap_token_dict = None
    _validate_cap_token(cap_token_dict, scope, "front_desk:read")

    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Validate E.164 format
    if not isinstance(phone, str) or not _re.match(r"^\+\d{7,15}$", phone):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_PHONE_FORMAT", "message": "phone must be E.164 format"},
        )

    phone_prefix = phone[:6] + "..."  # Law #9

    # Resolve carrier via Twilio Lookup v2
    carrier_name = ""
    carrier_type = ""
    try:
        carrier_info = await lookup_carrier(phone)
        if carrier_info:
            carrier_name = carrier_info.carrier_name or ""
            carrier_type = carrier_info.type or ""
    except TwilioProvisioningError as exc:
        logger.warning(
            "forwarding_instructions carrier_lookup_failed phone_prefix=%s err=%s",
            phone_prefix,
            exc,
        )
        # Fail open — return generic instructions even if lookup fails
        carrier_name = ""

    # Resolve forwarding codes
    # aspire_forward_target: tenant's Aspire forward-target number from tenant_phone_numbers
    fwd_rows = await supabase_select(
        "tenant_phone_numbers",
        f"office_id=eq.{office_id}&status=eq.active",
        limit=1,
    )
    aspire_forward_target = fwd_rows[0]["phone_number"] if fwd_rows else ""

    instructions = resolve_forwarding_instructions(carrier_name, aspire_forward_target)

    # Cut receipt (Law #2 — green tier read)
    receipt_store.store_receipts([{
        "id": receipt_id,
        "receipt_type": "forwarding_instructions_resolve",
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "outcome": "success",
        "action_type": "forwarding_instructions_resolve",
        "tool_used": "front_desk_forwarding",
        "risk_tier": "green",
        "redacted_inputs": {
            "phone_prefix": phone_prefix,
            "carrier_name": carrier_name,
        },
        "redacted_outputs": {
            "instruction_count": len(instructions),
            "aspire_forward_target_prefix": (aspire_forward_target[:6] + "...") if aspire_forward_target else "",
        },
        "trace_id": get_trace_id(),
        "correlation_id": get_correlation_id(),
        "capability_token_id": _cap_token_id(cap_token_dict) or None,
        "created_at": now,
    }])

    logger.info(
        "forwarding_instructions phone_prefix=%s carrier=%s instructions=%d",
        phone_prefix,
        carrier_name or "unknown",
        len(instructions),
    )

    return {
        "success": True,
        "phone_prefix": phone_prefix,
        "carrier_name": carrier_name,
        "carrier_type": carrier_type,
        "instructions": instructions,
        "aspire_forward_target": aspire_forward_target,
        "receipt_id": receipt_id,
    }
