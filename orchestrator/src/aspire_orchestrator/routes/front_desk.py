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
# _resolve_scope extracted to routes/_scope.py (architect R5).
# Kept as a local re-export here so existing callers within this module and
# any external importers that do `from routes.front_desk import _resolve_scope`
# continue to work without change.
from aspire_orchestrator.routes._scope import _resolve_scope  # noqa: F401
# MARK: persona-imports
from aspire_orchestrator.services.elevenlabs_phone import (
    ElevenLabsPhoneError,
    attach_to_agent,
)
from aspire_orchestrator.services.receptionist_personas import (
    DEFAULT_PERSONA_SLUG,
    get_persona,
    is_valid_slug,
    list_personas,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_delete,
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


def _format_e164_us(e164: str) -> str:
    """Format a US E.164 number ('+14155550198') as '+1 (415) 555-0198'.

    Non-US numbers and invalid inputs are returned unchanged so the FE can
    still render something. Callers should treat this as best-effort UI text.
    """
    if not e164 or not isinstance(e164, str):
        return e164 or ""
    s = e164.strip()
    if s.startswith("+1") and len(s) == 12 and s[2:].isdigit():
        return f"+1 ({s[2:5]}) {s[5:8]}-{s[8:]}"
    return s


def _invalidate_personalization_cache_safe(office_id: str) -> None:
    try:
        from aspire_orchestrator.routes.sarah import (
            invalidate_personalization_cache_for_office,
        )
        invalidate_personalization_cache_for_office(office_id)
    except Exception as cache_exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "personalization_cache_invalidation_failed office_id=%s: %s",
            office_id,
            cache_exc,
        )


# MARK: persona-swap-helper
async def _apply_persona_swap(
    *,
    office_id: str,
    suite_id: str,
    tenant_id: str,
    from_slug: str,
    to_slug: str,
    cap_token_id: str | None,
) -> None:
    new_persona = get_persona(to_slug)
    new_agent_id = new_persona.agent_id
    phone_rows = await supabase_select(
        "tenant_phone_numbers",
        f"office_id=eq.{office_id}&status=eq.active",
        order_by="purchased_at.desc",
        limit=1,
    )
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    if not phone_rows:
        receipt_store.store_receipts([{
            "id": receipt_id, "receipt_type": "receptionist_persona_changed",
            "suite_id": suite_id, "office_id": office_id, "tenant_id": tenant_id,
            "outcome": "deferred_no_number",
            "action_type": "receptionist_persona_changed",
            "tool_used": "elevenlabs_phone_attach", "risk_tier": "yellow",
            "redacted_outputs": {
                "from_persona": from_slug, "to_persona": to_slug,
                "agent_id": new_agent_id,
                "deferred_reason": "office has no purchased number yet",
            },
            "trace_id": get_trace_id(), "correlation_id": get_correlation_id(),
            "capability_token_id": cap_token_id, "created_at": now,
        }])
        logger.info("persona_swap deferred office_id=%s from=%s to=%s reason=no_number",
                    office_id, from_slug, to_slug)
        return
    phone_row = phone_rows[0]
    el_phone_number_id = phone_row.get("elevenlabs_phone_number_id")
    phone_pk = phone_row.get("id")
    if not el_phone_number_id:
        receipt_store.store_receipts([{
            "id": receipt_id, "receipt_type": "receptionist_persona_changed",
            "suite_id": suite_id, "office_id": office_id, "tenant_id": tenant_id,
            "outcome": "deferred_no_el_id",
            "action_type": "receptionist_persona_changed",
            "tool_used": "elevenlabs_phone_attach", "risk_tier": "yellow",
            "redacted_outputs": {
                "from_persona": from_slug, "to_persona": to_slug,
                "agent_id": new_agent_id,
                "deferred_reason": "tenant_phone_numbers row missing elevenlabs_phone_number_id",
            },
            "trace_id": get_trace_id(), "correlation_id": get_correlation_id(),
            "capability_token_id": cap_token_id, "created_at": now,
        }])
        logger.warning("persona_swap deferred office_id=%s from=%s to=%s reason=no_el_id phone_pk=%s",
                       office_id, from_slug, to_slug, phone_pk)
        return
    try:
        await attach_to_agent(el_phone_number_id, agent_id=new_agent_id)
    except ElevenLabsPhoneError as exc:
        receipt_store.store_receipts([{
            "id": receipt_id, "receipt_type": "receptionist_persona_changed",
            "suite_id": suite_id, "office_id": office_id, "tenant_id": tenant_id,
            "outcome": "failed",
            "action_type": "receptionist_persona_changed",
            "tool_used": "elevenlabs_phone_attach", "risk_tier": "yellow",
            "reason_code": exc.code,
            "redacted_outputs": {
                "from_persona": from_slug, "to_persona": to_slug,
                "agent_id": new_agent_id, "el_status": exc.status_code,
                "el_message": str(exc)[:200],
            },
            "trace_id": get_trace_id(), "correlation_id": get_correlation_id(),
            "capability_token_id": cap_token_id, "created_at": now,
        }])
        logger.error("persona_swap el_attach_failed office_id=%s from=%s to=%s code=%s",
                     office_id, from_slug, to_slug, exc.code)
        return
    try:
        await supabase_update(
            "tenant_phone_numbers",
            f"id=eq.{phone_pk}",
            {"attached_to_agent_id": new_agent_id, "updated_at": now},
        )
    except SupabaseClientError as exc:
        logger.warning("persona_swap db_mirror_update_failed office_id=%s phone_pk=%s: %s",
                       office_id, phone_pk, exc)
    receipt_store.store_receipts([{
        "id": receipt_id, "receipt_type": "receptionist_persona_changed",
        "suite_id": suite_id, "office_id": office_id, "tenant_id": tenant_id,
        "outcome": "success",
        "action_type": "receptionist_persona_changed",
        "tool_used": "elevenlabs_phone_attach", "risk_tier": "yellow",
        "redacted_outputs": {
            "from_persona": from_slug, "to_persona": to_slug,
            "agent_id": new_agent_id,
            "el_phone_number_id_prefix": str(el_phone_number_id)[:12],
        },
        "trace_id": get_trace_id(), "correlation_id": get_correlation_id(),
        "capability_token_id": cap_token_id, "created_at": now,
    }])
    logger.info("persona_swap success office_id=%s from=%s to=%s",
                office_id, from_slug, to_slug)


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
    phone_number_id: str | None = None
    public_number_mode: str | None = None
    catch_mode: str | None = None
    after_hours_mode: str | None = None
    busy_mode: str | None = None
    greeting_name_override: str | None = None
    pronunciation_override: str | None = None
    business_hours: dict[str, Any] | None = None
    timezone: str | None = None
    voicemail_email: str | None = None
    # MARK: persona-field — migration 109
    receptionist_persona: str | None = Field(None, pattern=r"^[a-z]{2,32}$")
    capability_token: dict[str, Any] | None = None


class RoutingContactCreate(BaseModel):
    role: str = Field(..., description="owner|sales|support|billing|scheduling|custom")
    name: str | None = None
    label: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(None, pattern=r"^\+\d{7,15}$")
    sip_uri: str | None = None
    email: str | None = None
    transfer_allowed: bool | None = None
    fallback_mode: str | None = None
    sort_order: int | None = None
    capability_token: dict[str, Any] | None = None

    @property
    def display_name(self) -> str:
        return (self.name or self.label or "").strip()


class RoutingContactPatch(BaseModel):
    role: str | None = None
    name: str | None = None
    label: str | None = None
    phone: str | None = Field(None, pattern=r"^\+\d{7,15}$")
    sip_uri: str | None = None
    email: str | None = None
    transfer_allowed: bool | None = None
    fallback_mode: str | None = None
    sort_order: int | None = None
    capability_token: dict[str, Any] | None = None

    @property
    def display_name(self) -> str | None:
        if self.name is not None:
            return self.name
        if self.label is not None:
            return self.label
        return None


def _normalize_mode_value(value: str | None, *, field_name: str) -> str:
    normalized = (value or "").strip().lower()
    if field_name == "public_number_mode":
        if normalized in {"", "aspire_number", "aspire_new_number"}:
            return "ASPIRE_NEW_NUMBER"
        if normalized in {"keep_current_number", "forward_existing"}:
            return "FORWARD_EXISTING"
        if normalized == "port_in":
            return "PORT_IN"
        return value or "ASPIRE_NEW_NUMBER"
    if field_name in {"after_hours_mode", "busy_mode"}:
        if normalized in {"", "take_message"}:
            return "take_message"
        if normalized in {"callback_window", "ask_callback_window"}:
            return "ask_callback_window"
        if normalized == "try_transfer_then_message":
            return "try_transfer_then_message"
        return normalized
    return normalized


def _normalize_routing_role(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized == "operations":
        return "custom"
    return normalized or "custom"


def _normalize_fallback_mode(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"", "transfer_allowed"}:
        return "transfer_allowed"
    if normalized in {"message_only", "message_fallback"}:
        return "message_only"
    return normalized


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
        f"office_id=eq.{oid}",
        order_by="version_no.desc",
        limit=1,
    )
    config = config_rows[0] if config_rows else {}

    routing_rows = await supabase_select(
        "front_desk_routing_contacts",
        f"office_id=eq.{oid}",
    )

    suite_id_str = str(scope.suite_id)
    voicemail_email = ""
    try:
        suite_rows = await supabase_select(
            "suite_profiles",
            f"suite_id=eq.{suite_id_str}",
            limit=1,
        )
        if suite_rows:
            voicemail_email = suite_rows[0].get("voicemail_email") or ""
    except Exception as vm_exc:  # noqa: BLE001
        logger.warning("voicemail_email_fetch_failed suite_id=%s: %s", suite_id_str, vm_exc)

    aspire_number: dict[str, Any] | None = None
    try:
        phone_row: dict[str, Any] | None = None
        config_phone_id = config.get("phone_number_id")
        if config_phone_id:
            rows = await supabase_select("tenant_phone_numbers", f"id=eq.{config_phone_id}", limit=1)
            if rows:
                phone_row = rows[0]
        if not phone_row:
            rows = await supabase_select(
                "tenant_phone_numbers",
                f"office_id=eq.{oid}&status=eq.active",
                order_by="purchased_at.desc",
                limit=1,
            )
            if rows:
                phone_row = rows[0]
        if phone_row:
            e164 = phone_row.get("phone_number") or ""
            aspire_number = {
                "e164": e164,
                "formatted": _format_e164_us(e164),
                "capabilities": phone_row.get("capabilities") or {},
                "status": phone_row.get("status") or "active",
                "purchased_at": phone_row.get("purchased_at"),
            }
    except Exception as ap_exc:  # noqa: BLE001
        logger.warning("aspire_number_fetch_failed office_id=%s: %s", oid, ap_exc)

    return {
        "success": True,
        "config": config,
        "routing_contacts": routing_rows,
        "voicemail_email": voicemail_email,
        "aspire_number": aspire_number,
    }


# MARK: personas-route
@router.get("/personas")
async def list_receptionist_personas() -> dict[str, Any]:
    """Return the receptionist persona registry. Green tier — read-only."""
    return {
        "success": True,
        "default_persona": DEFAULT_PERSONA_SLUG,
        "personas": [p.to_dict() for p in list_personas()],
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

    # MARK: persona-validation
    if req.receptionist_persona is not None and not is_valid_slug(req.receptionist_persona):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "UNKNOWN_PERSONA",
                "message": (
                    f"Unknown receptionist persona \'{req.receptionist_persona}\'. "
                    "Allowed: sarah, tiffany."
                ),
            },
        )

    current_rows = await supabase_select(
        "front_desk_configs", f"office_id=eq.{office_id}", order_by="version_no.desc", limit=1,
    )
    current = current_rows[0] if current_rows else {}
    current_version = int(current.get("version_no", 0))

    new_row: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id, "suite_id": suite_id, "office_id": office_id,
        "version_no": current_version + 1, "is_current": True,
        "public_number_mode": _normalize_mode_value(
            req.public_number_mode if req.public_number_mode is not None else current.get("public_number_mode", "ASPIRE_NUMBER"),
            field_name="public_number_mode",
        ),
        "phone_number_id": req.phone_number_id if req.phone_number_id is not None else current.get("phone_number_id"),
        "catch_mode": req.catch_mode or current.get("catch_mode", "APP_AND_PHONE_SIMUL_RING"),
        "after_hours_mode": _normalize_mode_value(
            req.after_hours_mode if req.after_hours_mode is not None else current.get("after_hours_mode", "take_message"),
            field_name="after_hours_mode",
        ),
        "busy_mode": _normalize_mode_value(
            req.busy_mode if req.busy_mode is not None else current.get("busy_mode", "take_message"),
            field_name="busy_mode",
        ),
        "greeting_name_override": req.greeting_name_override
            if req.greeting_name_override is not None
            else current.get("greeting_name_override") or "",
        "pronunciation_override": req.pronunciation_override
            if req.pronunciation_override is not None
            else current.get("pronunciation_override") or "",
        "business_hours": req.business_hours if req.business_hours is not None else current.get("business_hours"),
        # NOT NULL on front_desk_configs.timezone — fall back to America/New_York when the
        # tenant's first save omits it (FE timezone detection failed) and no prior row exists.
        # Without this, first-save crashes with 23502 and the FE silently swallows the 500.
        "timezone": req.timezone if req.timezone is not None else (current.get("timezone") or "America/New_York"),
        "receptionist_persona": (
            req.receptionist_persona.strip().lower()
            if req.receptionist_persona is not None
            else (current.get("receptionist_persona") or DEFAULT_PERSONA_SLUG)
        ),
        "created_at": now,
    }

    inserted = await supabase_insert("front_desk_configs", new_row)

    _prev_persona = (current.get("receptionist_persona") or DEFAULT_PERSONA_SLUG)
    _next_persona = new_row.get("receptionist_persona")
    if _next_persona and _prev_persona != _next_persona:
        await _apply_persona_swap(
            office_id=office_id, suite_id=suite_id, tenant_id=tenant_id,
            from_slug=_prev_persona, to_slug=_next_persona,
            cap_token_id=_cap_token_id(req.capability_token) or None,
        )

    if req.voicemail_email is not None:
        try:
            await supabase_update(
                "suite_profiles", f"suite_id=eq.{suite_id}",
                {"voicemail_email": req.voicemail_email or None},
            )
        except Exception as vm_exc:  # noqa: BLE001
            logger.warning("voicemail_email_update_failed suite_id=%s: %s", suite_id, vm_exc)

    _invalidate_personalization_cache_safe(office_id)

    receipt_store.store_receipts([{
        "id": receipt_id, "receipt_type": "front_desk_config_save",
        "suite_id": suite_id, "office_id": office_id, "tenant_id": tenant_id,
        "outcome": "success", "action_type": "front_desk_config_save",
        "tool_used": "front_desk_config", "risk_tier": "yellow",
        "redacted_outputs": {"version_no": new_row["version_no"], "config_id": new_row["id"]},
        "trace_id": get_trace_id(), "correlation_id": get_correlation_id(),
        "capability_token_id": _cap_token_id(req.capability_token) or None, "created_at": now,
    }])

    return {"success": True, "config": inserted, "receipt_id": receipt_id}


@router.post("/config/test-call")
async def test_call(
    capability_token: dict[str, Any] | None = None,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Fire a test inbound call to the office's purchased number (Yellow tier)."""
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

    twiml_url = f"{_ASPIRE_ORCHESTRATOR_URL}/v1/sarah/personalization"
    call_payload = {"To": to_number, "From": to_number, "Url": twiml_url, "Method": "POST"}
    twilio_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"

    test_result = "success"
    twilio_call_sid = ""
    try:
        async with httpx.AsyncClient(auth=(account_sid, auth_token), timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(twilio_url, data=call_payload)
        if resp.status_code >= 400:
            test_result = "failed"
            logger.error("test_call twilio error status=%d", resp.status_code)
        else:
            twilio_call_sid = resp.json().get("sid", "")
    except Exception as exc:
        test_result = "failed"
        logger.error("test_call failed: %s", exc)

    latest_config_id = None
    latest_config_rows = await supabase_select(
        "front_desk_configs",
        f"office_id=eq.{office_id}",
        order_by="version_no.desc",
        limit=1,
    )
    if latest_config_rows:
        latest_config_id = latest_config_rows[0].get("id")

    try:
        if not latest_config_id:
            raise SupabaseClientError("No front_desk_configs row found for forwarding test update")
        await supabase_update(
            "front_desk_configs",
            f"id=eq.{latest_config_id}",
            {
                "last_forwarding_test_at": now,
                "last_forwarding_test_result": test_result,
                "forwarding_status": "VERIFIED" if test_result == "success" else "LAST_TEST_FAILED",
            },
        )
    except SupabaseClientError as exc:
        logger.error("test_call update failed: %s", exc)

    receipt_store.store_receipts([{
        "id": receipt_id, "receipt_type": "front_desk_test_call",
        "suite_id": suite_id, "office_id": office_id, "tenant_id": tenant_id,
        "outcome": test_result, "action_type": "front_desk_test_call",
        "tool_used": "front_desk_config", "risk_tier": "yellow",
        "redacted_inputs": {"to_number": to_number},
        "redacted_outputs": {"call_sid": twilio_call_sid},
        "trace_id": get_trace_id(), "correlation_id": get_correlation_id(),
        "capability_token_id": _cap_token_id(capability_token) or None, "created_at": now,
    }])

    return {"success": test_result == "success", "test_result": test_result,
            "call_sid": twilio_call_sid, "receipt_id": receipt_id}


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
        "id": str(uuid.uuid4()), "tenant_id": tenant_id, "suite_id": suite_id, "office_id": office_id,
        "role": _normalize_routing_role(req.role), "name": req.display_name, "phone": req.phone or "",
        "sip_uri": req.sip_uri or "", "email": req.email or "",
        "transfer_allowed": True if req.transfer_allowed is None else req.transfer_allowed,
        "fallback_mode": _normalize_fallback_mode(req.fallback_mode),
        "sort_order": 0 if req.sort_order is None else req.sort_order,
        "created_at": now,
    }
    inserted = await supabase_insert("front_desk_routing_contacts", row)
    _invalidate_personalization_cache_safe(office_id)

    receipt_store.store_receipts([{
        "id": receipt_id, "receipt_type": "routing_contact_create",
        "suite_id": suite_id, "office_id": office_id, "tenant_id": tenant_id,
        "outcome": "success", "action_type": "routing_contact_create",
        "tool_used": "front_desk_routing", "risk_tier": "yellow",
        "redacted_outputs": {"contact_id": row["id"], "role": req.role},
        "trace_id": get_trace_id(), "correlation_id": get_correlation_id(),
        "capability_token_id": _cap_token_id(req.capability_token) or None, "created_at": now,
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
    new_name = req.display_name
    if new_name is not None:
        update_data["name"] = new_name
    if req.role is not None:
        update_data["role"] = _normalize_routing_role(req.role)
    if req.phone is not None:
        update_data["phone"] = req.phone
    if req.sip_uri is not None:
        update_data["sip_uri"] = req.sip_uri
    if req.email is not None:
        update_data["email"] = req.email
    if req.transfer_allowed is not None:
        update_data["transfer_allowed"] = req.transfer_allowed
    if req.fallback_mode is not None:
        update_data["fallback_mode"] = _normalize_fallback_mode(req.fallback_mode)
    if req.sort_order is not None:
        update_data["sort_order"] = req.sort_order

    updated = await supabase_update(
        "front_desk_routing_contacts", f"id=eq.{contact_id}&office_id=eq.{office_id}", update_data,
    )
    _invalidate_personalization_cache_safe(office_id)

    receipt_store.store_receipts([{
        "id": receipt_id, "receipt_type": "routing_contact_update",
        "suite_id": suite_id, "office_id": office_id, "tenant_id": tenant_id,
        "outcome": "success", "action_type": "routing_contact_update",
        "tool_used": "front_desk_routing", "risk_tier": "yellow",
        "redacted_inputs": {"contact_id": contact_id},
        "trace_id": get_trace_id(), "correlation_id": get_correlation_id(),
        "capability_token_id": _cap_token_id(req.capability_token) or None, "created_at": now,
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
    """Delete a routing contact (Yellow tier)."""
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(capability_token, scope, "front_desk:routing_write")

    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    await supabase_delete("front_desk_routing_contacts", f"id=eq.{contact_id}&office_id=eq.{office_id}")
    _invalidate_personalization_cache_safe(office_id)

    receipt_store.store_receipts([{
        "id": receipt_id, "receipt_type": "routing_contact_delete",
        "suite_id": suite_id, "office_id": office_id, "tenant_id": tenant_id,
        "outcome": "success", "action_type": "routing_contact_delete",
        "tool_used": "front_desk_routing", "risk_tier": "yellow",
        "redacted_inputs": {"contact_id": contact_id},
        "trace_id": get_trace_id(), "correlation_id": get_correlation_id(),
        "capability_token_id": _cap_token_id(capability_token) or None, "created_at": now,
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
    """Resolve carrier-specific conditional-forwarding instructions for a phone number."""
    import re as _re
    from aspire_orchestrator.services.forwarding_instructions import resolve_forwarding_instructions
    from aspire_orchestrator.services.twilio_provisioning import (
        TwilioProvisioningError,
        lookup_carrier,
    )

    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)

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

    if not isinstance(phone, str) or not _re.match(r"^\+\d{7,15}$", phone):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_PHONE_FORMAT", "message": "phone must be E.164 format"},
        )

    phone_prefix = phone[:6] + "..."  # Law #9

    carrier_name = ""
    carrier_type = ""
    try:
        carrier_info = await lookup_carrier(phone)
        if carrier_info:
            carrier_name = carrier_info.carrier_name or ""
            carrier_type = carrier_info.type or ""
    except TwilioProvisioningError as exc:
        logger.warning("forwarding_instructions carrier_lookup_failed phone_prefix=%s err=%s", phone_prefix, exc)
        carrier_name = ""

    fwd_rows = await supabase_select("tenant_phone_numbers", f"office_id=eq.{office_id}&status=eq.active", limit=1)
    aspire_forward_target = fwd_rows[0]["phone_number"] if fwd_rows else ""
    instructions = resolve_forwarding_instructions(carrier_name, aspire_forward_target)

    receipt_store.store_receipts([{
        "id": receipt_id, "receipt_type": "forwarding_instructions_resolve",
        "suite_id": suite_id, "office_id": office_id, "tenant_id": tenant_id,
        "outcome": "success", "action_type": "forwarding_instructions_resolve",
        "tool_used": "front_desk_forwarding", "risk_tier": "green",
        "redacted_inputs": {"phone_prefix": phone_prefix, "carrier_name": carrier_name},
        "redacted_outputs": {
            "instruction_count": len(instructions),
            "aspire_forward_target_prefix": (aspire_forward_target[:6] + "...") if aspire_forward_target else "",
        },
        "trace_id": get_trace_id(), "correlation_id": get_correlation_id(),
        "capability_token_id": _cap_token_id(cap_token_dict) or None, "created_at": now,
    }])

    return {
        "success": True, "phone_prefix": phone_prefix,
        "carrier_name": carrier_name, "carrier_type": carrier_type,
        "instructions": instructions, "aspire_forward_target": aspire_forward_target,
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# GET /v1/front-desk/inbox  — Unified chronological feed (Pass G)
# ---------------------------------------------------------------------------


@router.get("/inbox")
async def get_front_desk_inbox(
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> dict[str, Any]:
    """Chronological merged feed of front-desk activity."""
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    time_filter = ""
    if since:
        time_filter += f"&created_at=gte.{since}"
    if until:
        time_filter += f"&created_at=lte.{until}"

    capped_limit = min(limit, 200)
    items: list[dict[str, Any]] = []

    try:
        call_rows = await supabase_select(
            "call_sessions", f"suite_id=eq.{suite_id}{time_filter}",
            order_by="created_at.desc", limit=capped_limit,
        )
        for r in (call_rows or []):
            items.append({
                "kind": "call", "id": r.get("id"),
                "event_at": r.get("started_at") or r.get("created_at"),
                "contact_phone": r.get("caller_id") or r.get("phone"),
                "contact_name": r.get("contact_name"),
                "direction": r.get("direction", "inbound"),
                "duration_seconds": r.get("duration_seconds"),
                "status": r.get("status"), "suite_id": suite_id, "raw": r,
            })
    except SupabaseClientError as exc:
        logger.warning("inbox_calls_fetch_failed suite_id=%s: %s", suite_id, exc)

    try:
        vm_rows = await supabase_select(
            "frontdesk_voicemails", f"suite_id=eq.{suite_id}{time_filter}",
            order_by="created_at.desc", limit=capped_limit,
        )
        for r in (vm_rows or []):
            items.append({
                "kind": "voicemail", "id": r.get("id"),
                "event_at": r.get("created_at"),
                "contact_phone": r.get("caller_phone") or r.get("from_number"),
                "contact_name": r.get("contact_name"),
                "duration_seconds": r.get("duration_seconds"),
                "reviewed": r.get("reviewed", False),
                "transcription": r.get("transcription"), "suite_id": suite_id, "raw": r,
            })
    except SupabaseClientError as exc:
        logger.warning("inbox_voicemails_fetch_failed suite_id=%s: %s", suite_id, exc)

    try:
        sms_rows = await supabase_select(
            "sms_messages", f"suite_id=eq.{suite_id}{time_filter}",
            order_by="created_at.desc", limit=capped_limit,
        )
        for r in (sms_rows or []):
            items.append({
                "kind": "sms", "id": r.get("id"),
                "event_at": r.get("created_at"),
                "contact_phone": r.get("from_number") or r.get("to_number"),
                "contact_name": r.get("contact_name"),
                "body_preview": (r.get("body") or "")[:120],
                "direction": r.get("direction", "inbound"),
                "status": r.get("status"), "suite_id": suite_id, "raw": r,
            })
    except SupabaseClientError as exc:
        logger.warning("inbox_sms_fetch_failed suite_id=%s: %s", suite_id, exc)

    try:
        cb_rows = await supabase_select(
            "callback_promises", f"suite_id=eq.{suite_id}{time_filter}&status=neq.completed",
            order_by="due_at.asc", limit=capped_limit,
        )
        for r in (cb_rows or []):
            items.append({
                "kind": "callback", "id": r.get("id"),
                "event_at": r.get("due_at") or r.get("created_at"),
                "contact_phone": r.get("contact_phone"), "contact_name": r.get("contact_name"),
                "promise_context": r.get("promise_context"), "status": r.get("status"),
                "due_at": r.get("due_at"), "suite_id": suite_id, "raw": r,
            })
    except SupabaseClientError as exc:
        logger.warning("inbox_callbacks_fetch_failed suite_id=%s: %s", suite_id, exc)

    try:
        cap_rows = await supabase_select(
            "memory_objects", f"suite_id=eq.{suite_id}&memory_type=eq.call{time_filter}",
            order_by="created_at.desc", limit=capped_limit,
        )
        for r in (cap_rows or []):
            detail = r.get("detail") or {}
            items.append({
                "kind": "capture", "id": str(r.get("memory_id") or r.get("id") or ""),
                "event_at": r.get("created_at"),
                "contact_phone": detail.get("caller_phone") or "",
                "contact_name": detail.get("caller_name") or "",
                "body_preview": (r.get("summary") or "")[:120],
                "urgency": detail.get("urgency"), "reason_category": detail.get("reason_category"),
                "category": detail.get("category"), "suite_id": suite_id,
            })
    except SupabaseClientError as exc:
        logger.warning("inbox_captures_fetch_failed suite_id=%s: %s", suite_id, exc)

    items.sort(key=lambda x: x.get("event_at") or "", reverse=True)
    items = items[:capped_limit]

    return {"items": items, "count": len(items), "suite_id": suite_id, "since": since, "until": until}
