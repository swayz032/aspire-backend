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
    """Best-effort LKG cache invalidation for the given office.

    Called after every Front Desk write that affects what the personalization
    webhook would return (config save + routing-contact CRUD). Without this,
    Sarah keeps serving the prior cached dyn_vars for up to 10 minutes.

    Lazy-imported to avoid a circular import (sarah.py also imports this
    module). Failures are logged but never bubble — cache will expire on its
    own TTL if invalidation fails.
    """
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
    """Re-attach the office's EL phone number to the new persona's agent.

    Cuts a receipt on every outcome (Law #2). Pre-purchase tenants get a
    deferred_no_number receipt; the eventual purchase route reads
    front_desk_configs.receptionist_persona and attaches at creation time.
    EL failures emit a 'failed' receipt but do NOT roll back the
    front_desk_configs insert.
    """
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
    public_number_mode: str | None = None
    catch_mode: str | None = None
    after_hours_mode: str | None = None
    busy_mode: str | None = None
    greeting_name_override: str | None = None
    pronunciation_override: str | None = None
    # Hours tab — JSONB column on front_desk_configs. The frontend sends a
    # canonical 7-day shape: { mon: {open:bool, startTime:'HH:MM',
    # endTime:'HH:MM'}, ... sun: {...} }. Server stores as-is and the
    # personalization webhook reads it to compute is_open_now / is_after_hours.
    business_hours: dict[str, Any] | None = None
    # IANA timezone string (e.g. "America/Los_Angeles"). Drives is_open_now
    # evaluation against business_hours at the office's wall-clock time.
    timezone: str | None = None
    # Tenant-level voicemail destination. Lives on suite_profiles (one inbox
    # per business, not versioned per front_desk_configs row). PATCH handler
    # relays this through to suite_profiles when present.
    voicemail_email: str | None = None
    # MARK: persona-field — migration 109
    receptionist_persona: str | None = Field(None, pattern=r"^[a-z]{2,32}$")
    capability_token: dict[str, Any] | None = None


class RoutingContactCreate(BaseModel):
    role: str = Field(..., description="owner|sales|support|billing|scheduling|custom")
    # Display label for the contact. Live DB column is `name`; we accept
    # either `name` (canonical) or `label` (legacy alias) from the wire so
    # existing clients keep working while the schema is honest.
    name: str | None = None
    label: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(None, pattern=r"^\+\d{7,15}$")
    sip_uri: str | None = None
    email: str | None = None
    capability_token: dict[str, Any] | None = None

    @property
    def display_name(self) -> str:
        return (self.name or self.label or "").strip()


class RoutingContactPatch(BaseModel):
    # Same `name`/`label` duality as RoutingContactCreate.
    name: str | None = None
    label: str | None = None
    phone: str | None = Field(None, pattern=r"^\+\d{7,15}$")
    sip_uri: str | None = None
    email: str | None = None
    capability_token: dict[str, Any] | None = None

    @property
    def display_name(self) -> str | None:
        # None means "no change". Empty string means "explicitly clear".
        if self.name is not None:
            return self.name
        if self.label is not None:
            return self.label
        return None


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

    # Note: live schema has no is_active column. Reads return all rows for
    # the office; deletion is handled by hard DELETE in delete_routing_contact.
    routing_rows = await supabase_select(
        "front_desk_routing_contacts",
        f"office_id=eq.{oid}",
    )

    # Tenant-level voicemail email (migration 108). Lives on suite_profiles
    # because it's one inbox per business, not per versioned config row.
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
        logger.warning(
            "voicemail_email_fetch_failed suite_id=%s: %s",
            suite_id_str,
            vm_exc,
        )

    # Aspire phone number (joined from tenant_phone_numbers).
    # This is the single source of truth for "what's the office's purchased
    # number" — surfaced on the Return Call page header, the Front Desk Setup
    # Sarah Status Rail, and the Call Room caller-id field. We resolve in this
    # order:
    #   1. front_desk_configs.phone_number_id  -> tenant_phone_numbers.id
    #   2. fall back to any active tenant_phone_numbers row for this office
    # so the badge appears even if the config row hasn't yet been re-saved
    # since the purchase landed.
    aspire_number: dict[str, Any] | None = None
    try:
        phone_row: dict[str, Any] | None = None
        config_phone_id = config.get("phone_number_id")
        if config_phone_id:
            rows = await supabase_select(
                "tenant_phone_numbers",
                f"id=eq.{config_phone_id}",
                limit=1,
            )
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
        logger.warning(
            "aspire_number_fetch_failed office_id=%s: %s",
            oid,
            ap_exc,
        )

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
        "business_hours": req.business_hours
            if req.business_hours is not None
            else current.get("business_hours"),
        "timezone": req.timezone
            if req.timezone is not None
            else current.get("timezone"),
        # MARK: persona-new-row
        "receptionist_persona": (
            req.receptionist_persona.strip().lower()
            if req.receptionist_persona is not None
            else (current.get("receptionist_persona") or DEFAULT_PERSONA_SLUG)
        ),
        "created_at": now,
    }

    inserted = await supabase_insert("front_desk_configs", new_row)

    # MARK: persona-swap-call
    # Only swap when the slug actually changes. Treat a missing column on
    # the prior row (predates migration 109) as DEFAULT_PERSONA_SLUG so the
    # first save after migration is a no-op for tenants on the default.
    _prev_persona = (current.get("receptionist_persona") or DEFAULT_PERSONA_SLUG)
    _next_persona = new_row.get("receptionist_persona")
    if _next_persona and _prev_persona != _next_persona:
        await _apply_persona_swap(
            office_id=office_id,
            suite_id=suite_id,
            tenant_id=tenant_id,
            from_slug=_prev_persona,
            to_slug=_next_persona,
            cap_token_id=_cap_token_id(req.capability_token) or None,
        )

    # Tenant-level fields persisted on suite_profiles (not on the versioned
    # config row). voicemail_email is the dedicated inbox added in
    # migration 108 — falls back to suite_profiles.email in
    # _fetch_profile when not set. Done as a best-effort sibling write so
    # it doesn't fail the whole save if RLS/permission blocks.
    if req.voicemail_email is not None:
        try:
            await supabase_update(
                "suite_profiles",
                f"suite_id=eq.{suite_id}",
                {"voicemail_email": req.voicemail_email or None},
            )
        except Exception as vm_exc:  # noqa: BLE001
            logger.warning(
                "voicemail_email_update_failed suite_id=%s: %s",
                suite_id,
                vm_exc,
            )

    # Pass 19 §3.5.5 — invalidate LKG personalization cache for this office.
    # Without this, calls within the next 10min would get stale routing phones
    # from the in-process LKG cache.
    _invalidate_personalization_cache_safe(office_id)

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
        "name": req.display_name,  # live DB column is `name`, not `label`
        "phone": req.phone or "",
        "sip_uri": req.sip_uri or "",
        "email": req.email or "",
        # Sensible defaults for the columns the UI doesn't yet expose —
        # transfer_allowed=true so the LLM can transfer immediately,
        # fallback_mode kept consistent with frontend RoutingFallbackMode.
        "transfer_allowed": True,
        "fallback_mode": "TRANSFER_ALLOWED",
        "sort_order": 0,
        "created_at": now,
    }
    inserted = await supabase_insert("front_desk_routing_contacts", row)

    # Sarah's transfer-to-number rules dereference {{ routing_*_phone }}
    # from the personalization webhook. The LKG cache holds the prior dyn_vars
    # for up to 10 min — invalidate so the next call sees the new contact.
    _invalidate_personalization_cache_safe(office_id)

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
    new_name = req.display_name
    if new_name is not None:
        update_data["name"] = new_name  # live DB column is `name`
    if req.phone is not None:
        update_data["phone"] = req.phone
    if req.sip_uri is not None:
        update_data["sip_uri"] = req.sip_uri
    if req.email is not None:
        update_data["email"] = req.email

    updated = await supabase_update(
        "front_desk_routing_contacts",
        f"id=eq.{contact_id}&office_id=eq.{office_id}",
        update_data,
    )

    # Sarah's cached dyn_vars for this office still hold the old phone/label.
    # Drop them so the next call rebuilds from the just-written row.
    _invalidate_personalization_cache_safe(office_id)

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
    """Delete a routing contact (Yellow tier).

    The live front_desk_routing_contacts schema has no soft-delete column,
    so this performs a hard DELETE. The action is captured in an immutable
    receipt (`routing_contact_delete`) which preserves the audit trail per
    Law #2 — receipts are append-only, the config table holds tenant state
    and is mutable by design.
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(capability_token, scope, "front_desk:routing_write")

    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    await supabase_delete(
        "front_desk_routing_contacts",
        f"id=eq.{contact_id}&office_id=eq.{office_id}",
    )

    # Drop the personalization cache so Sarah stops dereferencing the
    # deleted contact's phone via routing_*_phone dyn vars.
    _invalidate_personalization_cache_safe(office_id)

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


# ---------------------------------------------------------------------------
# GET /v1/front-desk/inbox  — Unified chronological feed (Pass G)
# ---------------------------------------------------------------------------
# Merges call_sessions + frontdesk_voicemails + sms_messages + callback_promises
# into a single chronological feed for the TodayFeed widget.
# Each item gets a `kind` discriminator: 'call' | 'voicemail' | 'sms' | 'callback'.
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
    """Chronological merged feed of front-desk activity for the caller's suite.

    Returns items sorted by event_at desc. Max 100 items per request.
    Use since/until (ISO 8601) to paginate by time window.
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    # Build time filter segments
    time_filter = ""
    if since:
        time_filter += f"&created_at=gte.{since}"
    if until:
        time_filter += f"&created_at=lte.{until}"

    capped_limit = min(limit, 200)
    items: list[dict[str, Any]] = []

    # ── Calls ────────────────────────────────────────────────────────────────
    try:
        call_rows = await supabase_select(
            "call_sessions",
            f"suite_id=eq.{suite_id}{time_filter}",
            order_by="created_at.desc",
            limit=capped_limit,
        )
        for r in (call_rows or []):
            items.append({
                "kind": "call",
                "id": r.get("id"),
                "event_at": r.get("started_at") or r.get("created_at"),
                "contact_phone": r.get("caller_id") or r.get("phone"),
                "contact_name": r.get("contact_name"),
                "direction": r.get("direction", "inbound"),
                "duration_seconds": r.get("duration_seconds"),
                "status": r.get("status"),
                "suite_id": suite_id,
                "raw": r,
            })
    except SupabaseClientError as exc:
        logger.warning("inbox_calls_fetch_failed suite_id=%s: %s", suite_id, exc)

    # ── Voicemails ────────────────────────────────────────────────────────────
    try:
        vm_filter = f"suite_id=eq.{suite_id}{time_filter}"
        vm_rows = await supabase_select(
            "frontdesk_voicemails",
            vm_filter,
            order_by="created_at.desc",
            limit=capped_limit,
        )
        for r in (vm_rows or []):
            items.append({
                "kind": "voicemail",
                "id": r.get("id"),
                "event_at": r.get("created_at"),
                "contact_phone": r.get("caller_phone") or r.get("from_number"),
                "contact_name": r.get("contact_name"),
                "duration_seconds": r.get("duration_seconds"),
                "reviewed": r.get("reviewed", False),
                "transcription": r.get("transcription"),
                "suite_id": suite_id,
                "raw": r,
            })
    except SupabaseClientError as exc:
        logger.warning("inbox_voicemails_fetch_failed suite_id=%s: %s", suite_id, exc)

    # ── SMS ───────────────────────────────────────────────────────────────────
    try:
        sms_filter = f"suite_id=eq.{suite_id}{time_filter}"
        sms_rows = await supabase_select(
            "sms_messages",
            sms_filter,
            order_by="created_at.desc",
            limit=capped_limit,
        )
        for r in (sms_rows or []):
            items.append({
                "kind": "sms",
                "id": r.get("id"),
                "event_at": r.get("created_at"),
                "contact_phone": r.get("from_number") or r.get("to_number"),
                "contact_name": r.get("contact_name"),
                "body_preview": (r.get("body") or "")[:120],
                "direction": r.get("direction", "inbound"),
                "status": r.get("status"),
                "suite_id": suite_id,
                "raw": r,
            })
    except SupabaseClientError as exc:
        logger.warning("inbox_sms_fetch_failed suite_id=%s: %s", suite_id, exc)

    # ── Callbacks ─────────────────────────────────────────────────────────────
    try:
        cb_filter = f"suite_id=eq.{suite_id}{time_filter}&status=neq.completed"
        cb_rows = await supabase_select(
            "callback_promises",
            cb_filter,
            order_by="due_at.asc",
            limit=capped_limit,
        )
        for r in (cb_rows or []):
            items.append({
                "kind": "callback",
                "id": r.get("id"),
                "event_at": r.get("due_at") or r.get("created_at"),
                "contact_phone": r.get("contact_phone"),
                "contact_name": r.get("contact_name"),
                "promise_context": r.get("promise_context"),
                "status": r.get("status"),
                "due_at": r.get("due_at"),
                "suite_id": suite_id,
                "raw": r,
            })
    except SupabaseClientError as exc:
        logger.warning("inbox_callbacks_fetch_failed suite_id=%s: %s", suite_id, exc)

    # ── Captured messages (memory_objects where memory_type='call') ──────────
    # These are Tiffany/Sarah capture_message tool outputs — text-like captures
    # that surface as 'sms' kind in the feed (text-message-like, no audio).
    try:
        cap_filter = f"suite_id=eq.{suite_id}&memory_type=eq.call{time_filter}"
        cap_rows = await supabase_select(
            "memory_objects",
            cap_filter,
            order_by="created_at.desc",
            limit=capped_limit,
        )
        for r in (cap_rows or []):
            detail = r.get("detail") or {}
            items.append({
                "kind": "capture",
                "id": str(r.get("memory_id") or r.get("id") or ""),
                "event_at": r.get("created_at"),
                "contact_phone": detail.get("caller_phone") or "",
                "contact_name": detail.get("caller_name") or "",
                "body_preview": (r.get("summary") or "")[:120],
                "urgency": detail.get("urgency"),
                "reason_category": detail.get("reason_category"),
                "category": detail.get("category"),
                "suite_id": suite_id,
            })
    except SupabaseClientError as exc:
        logger.warning("inbox_captures_fetch_failed suite_id=%s: %s", suite_id, exc)

    # Sort by event_at desc (most recent first); None sorts last
    items.sort(key=lambda x: x.get("event_at") or "", reverse=True)
    items = items[:capped_limit]

    return {
        "items": items,
        "count": len(items),
        "suite_id": suite_id,
        "since": since,
        "until": until,
    }
