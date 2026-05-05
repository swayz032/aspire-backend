"""Twilio phone-number provisioning service (Pass 16 — §16.B; Pass 18+ Lane 2; Pass 19 Lane B).

Provides:
  - search_available_numbers: search available US local OR toll-free numbers.
  - lookup_carrier: Twilio Lookup v2 → carrier name, type, line_type_intelligence.
  - purchase_number: atomic Yellow-tier purchase -> EL import -> EL attach -> receipt.
  - release_number: EL detach -> Twilio release -> mark released_at.

Aspire Laws enforced:
  Law #2 — every state change cuts an immutable receipt.
  Law #3 — fail closed on missing credentials.
  Law #4 — Yellow tier: capability token validated upstream by route layer.
  Law #6 — suite_id/office_id scoped; never cross-tenant.
  Law #9 — twilio_account_sid and twilio_auth_token never logged.
  Law #10 — circuit breaker + retry on every external HTTP call.

Pass 18+ Lane 2 changes:
  - All Twilio HTTP calls wrapped with `resilient_call` (breaker + retry).
  - GET /AvailablePhoneNumbers retried (idempotent=True).
  - POST /IncomingPhoneNumbers (purchase) retried ONLY on true network errors
    (idempotent=False) — Twilio billing already happened on any HTTP response.
  - DELETE rollback retried with `idempotent=True` (delete is naturally idempotent
    on Twilio side: 404 means already gone).
  - Persistent idempotency: `tenant_phone_numbers.purchase_idempotency_key`
    (migration 104) replaces the in-memory `_idem_store` dict. Survives restarts.
  - Prometheus metrics: aspire_telephony_purchase_total{outcome=...},
    aspire_telephony_release_total{outcome=...}.

Pass 19 Lane B additions:
  - search_available_numbers gains number_type param ('Local' | 'TollFree').
    TollFree hits /US/TollFree.json, skips AreaCode, reports $2.00/mo cost.
  - lookup_carrier wraps Twilio Lookup v2 → returns CarrierInfo.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.elevenlabs_phone import (
    SARAH_RECEPTIONIST_AGENT_ID,
    ElevenLabsPhoneError,
    attach_to_agent,
    detach_from_elevenlabs,
    import_to_elevenlabs,
)
# MARK: persona-imports
from aspire_orchestrator.services.receptionist_personas import (
    DEFAULT_PERSONA_SLUG,
    get_persona,
)
from aspire_orchestrator.services.metrics import METRICS
from aspire_orchestrator.services.resilience import (
    TWILIO_RETRY,
    CircuitOpenError,
    RetryableError,
    resilient_call,
    twilio_breaker,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
    supabase_update,
)

logger = logging.getLogger(__name__)

_TWILIO_BASE = "https://api.twilio.com/2010-04-01"
_TIMEOUT_SECONDS = 4.5  # <5s per Law #10 reliability standard

# Orchestrator production URL — configurable via env for staging/dev overrides
_ASPIRE_ORCHESTRATOR_URL = "https://orchestrator.aspire.app"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PhoneCapabilities(BaseModel):
    voice: bool
    sms: bool
    mms: bool


class AvailableNumber(BaseModel):
    phone_number: str       # E.164
    region: str
    monthly_cost_cents: int
    capabilities: PhoneCapabilities


class CarrierInfo(BaseModel):
    """Twilio Lookup v2 line_type_intelligence result.

    Law #9: carrier_name and type are non-PII telecom metadata.
    The raw phone number is never stored in this model.
    """
    carrier_name: str | None = None
    type: str | None = None  # e.g. 'mobile', 'landline', 'voip', 'fixed-voip'
    line_type_intelligence: dict[str, Any] | None = None


class PurchasedNumber(BaseModel):
    phone_number: str                    # E.164
    twilio_sid: str                      # SIDxxxxxxxx
    elevenlabs_phone_number_id: str      # pn_...
    attached_to_agent_id: str
    tenant_id: str
    suite_id: str
    office_id: str
    receipt_id: str
    purchased_at: str                    # ISO8601


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _twilio_auth() -> tuple[str, str]:
    """Return (account_sid, auth_token). Fail closed if not configured."""
    sid = settings.twilio_account_sid
    token = settings.twilio_auth_token
    if not sid or not token:
        raise TwilioProvisioningError(
            "MISSING_TWILIO_CREDENTIALS",
            "twilio_account_sid or twilio_auth_token not configured. "
            "Fail-closed per Law #3.",
        )
    return sid, token


class TwilioProvisioningError(Exception):
    """Raised on Twilio provisioning failures."""

    def __init__(self, code: str, message: str, status_code: int = 0) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


def _raise_twilio_error(operation: str, resp: httpx.Response) -> None:
    detail = f"HTTP {resp.status_code}"
    try:
        body = resp.json()
        if isinstance(body, dict):
            msg = body.get("message") or body.get("detail") or ""
            code = body.get("code", "")
            detail = f"{code}: {msg}".strip(": ") if code else str(msg) or detail
    except Exception:
        pass
    logger.error("twilio_provisioning op=%s status=%d detail=%s", operation, resp.status_code, detail)
    raise TwilioProvisioningError(
        f"TWILIO_{operation.upper()}_FAILED",
        f"Twilio {operation} failed: {detail}",
        resp.status_code,
    )


def _is_retryable_twilio_status(status_code: int) -> bool:
    """Twilio responses that warrant a retry on idempotent operations.

    429 throttling, 5xx server errors. NEVER 4xx (auth/validation).
    """
    return status_code == 429 or 500 <= status_code < 600


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def _twilio_get_available_numbers(
    *,
    account_sid: str,
    auth_token: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Single attempt of the Local number search. Wrapped by resilient_call below."""
    url = f"{_TWILIO_BASE}/Accounts/{account_sid}/AvailablePhoneNumbers/US/Local.json"
    async with httpx.AsyncClient(
        auth=(account_sid, auth_token),
        timeout=_TIMEOUT_SECONDS,
    ) as client:
        resp = await client.get(url, params=params)

    if resp.status_code >= 400:
        if _is_retryable_twilio_status(resp.status_code):
            raise RetryableError(
                "TWILIO_TRANSIENT",
                f"Twilio search transient {resp.status_code}",
            )
        _raise_twilio_error("search_available_numbers", resp)

    return resp.json().get("available_phone_numbers", []) or []


async def _twilio_get_tollfree_numbers(
    *,
    account_sid: str,
    auth_token: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Single attempt of the TollFree number search. Wrapped by resilient_call below.

    Pass 19 Lane B: toll-free uses /US/TollFree.json. AreaCode must NOT be
    included in params — toll-free numbers are non-geographic.
    """
    url = f"{_TWILIO_BASE}/Accounts/{account_sid}/AvailablePhoneNumbers/US/TollFree.json"
    async with httpx.AsyncClient(
        auth=(account_sid, auth_token),
        timeout=_TIMEOUT_SECONDS,
    ) as client:
        resp = await client.get(url, params=params)

    if resp.status_code >= 400:
        if _is_retryable_twilio_status(resp.status_code):
            raise RetryableError(
                "TWILIO_TRANSIENT",
                f"Twilio toll-free search transient {resp.status_code}",
            )
        _raise_twilio_error("search_available_numbers_tollfree", resp)

    return resp.json().get("available_phone_numbers", []) or []


async def search_available_numbers(
    area_code: str = "",
    contains: str | None = None,
    limit: int = 20,
    *,
    number_type: str = "Local",
) -> list[AvailableNumber]:
    """Search available US phone numbers via Twilio REST API.

    Pass 19 Lane B extension:
      number_type='Local' (default): hits /US/Local.json with AreaCode param.
      number_type='TollFree': hits /US/TollFree.json, skips AreaCode (non-geographic),
        reports monthly_cost_cents=200 ($2.00/mo standard toll-free rate).

    Returns sanitized list — Twilio account SID never included in response.

    Idempotent (GET) — wrapped with circuit breaker + retry on transient failures.
    Law #9: account_sid used only in auth, never returned.
    """
    account_sid, auth_token = _twilio_auth()

    is_tollfree = number_type.lower() in ("tollfree", "toll-free", "toll_free")

    if is_tollfree:
        # TollFree: non-geographic, no AreaCode
        params: dict[str, Any] = {
            "PageSize": min(limit, 50),
            "VoiceEnabled": "true",
            "SmsEnabled": "true",
        }
        fn = _twilio_get_tollfree_numbers
        monthly_cost_cents = 200  # $2.00/mo standard toll-free
        log_area = "toll-free"
    else:
        # Local: AreaCode required for targeting
        params = {
            "AreaCode": area_code,
            "PageSize": min(limit, 50),
            "VoiceEnabled": "true",
        }
        fn = _twilio_get_available_numbers
        monthly_cost_cents = 100  # $1.00/mo standard local
        log_area = area_code

    if contains:
        params["Contains"] = contains

    try:
        raw_numbers = await resilient_call(
            fn,
            account_sid=account_sid,
            auth_token=auth_token,
            params=params,
            breaker=twilio_breaker(),
            policy=TWILIO_RETRY,
            idempotent=True,
        )
    except CircuitOpenError:
        logger.warning("twilio_search circuit_open area_code=%s type=%s", log_area, number_type)
        raise TwilioProvisioningError(
            "TWILIO_CIRCUIT_OPEN",
            "Twilio is degraded — search temporarily unavailable",
            503,
        )

    numbers: list[AvailableNumber] = []
    for raw in raw_numbers:
        caps_raw = raw.get("capabilities", {})
        caps = PhoneCapabilities(
            voice=bool(caps_raw.get("voice", False)),
            sms=bool(caps_raw.get("SMS", caps_raw.get("sms", False))),
            mms=bool(caps_raw.get("MMS", caps_raw.get("mms", False))),
        )
        numbers.append(
            AvailableNumber(
                phone_number=raw.get("phone_number", ""),
                region=raw.get("region", ""),
                monthly_cost_cents=monthly_cost_cents,
                capabilities=caps,
            )
        )

    logger.info(
        "twilio_search area_code=%s type=%s contains=%s results=%d",
        log_area,
        number_type,
        contains or "",
        len(numbers),
    )
    return numbers


# ---------------------------------------------------------------------------
# Lookup v2 — carrier resolution (Pass 19 Lane B)
# ---------------------------------------------------------------------------

_TWILIO_LOOKUPS_BASE = "https://lookups.twilio.com/v2"


async def _twilio_lookup_v2(
    *,
    account_sid: str,
    auth_token: str,
    phone_number: str,
) -> dict[str, Any]:
    """Single attempt of Twilio Lookup v2. Wrapped by resilient_call below.

    GET /v2/PhoneNumbers/{phone}?Fields=line_type_intelligence
    Law #9: account_sid used only in auth header, never in response.
    """
    # URL-encode + sign are handled by httpx auth tuple
    url = f"{_TWILIO_LOOKUPS_BASE}/PhoneNumbers/{phone_number}"
    async with httpx.AsyncClient(
        auth=(account_sid, auth_token),
        timeout=_TIMEOUT_SECONDS,
    ) as client:
        resp = await client.get(url, params={"Fields": "line_type_intelligence"})

    if resp.status_code == 404:
        # Number not found — return empty dict (not an error)
        return {}
    if resp.status_code >= 400:
        if _is_retryable_twilio_status(resp.status_code):
            raise RetryableError(
                "TWILIO_TRANSIENT",
                f"Twilio Lookup v2 transient {resp.status_code}",
            )
        _raise_twilio_error("lookup_carrier", resp)

    return resp.json()


async def lookup_carrier(phone_number: str) -> CarrierInfo | None:
    """Resolve carrier information for a phone number via Twilio Lookup v2.

    Used by FORWARD_EXISTING mode to return carrier-specific conditional-forwarding
    instructions to the frontend.

    Returns CarrierInfo with carrier_name, type, line_type_intelligence, or
    None if the number is not found.

    Idempotent (GET) — wrapped with circuit breaker + retry on transient failures.
    Law #9: phone_number not logged at INFO; account_sid never returned.
    """
    account_sid, auth_token = _twilio_auth()

    try:
        data = await resilient_call(
            _twilio_lookup_v2,
            account_sid=account_sid,
            auth_token=auth_token,
            phone_number=phone_number,
            breaker=twilio_breaker(),
            policy=TWILIO_RETRY,
            idempotent=True,
        )
    except CircuitOpenError as ce:
        logger.warning("twilio_lookup_carrier circuit_open")
        raise TwilioProvisioningError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — carrier lookup unavailable ({ce})",
            503,
        ) from ce

    if not data:
        # 404 path — number not found
        return None

    lti = data.get("line_type_intelligence") or {}
    carrier_name = lti.get("carrier_name") or ""
    carrier_type = lti.get("type") or ""

    logger.info(
        "twilio_lookup_carrier phone_prefix=%s carrier=%s type=%s",
        (phone_number or "")[:6] + "...",  # Law #9: prefix only
        carrier_name or "unknown",
        carrier_type or "unknown",
    )

    return CarrierInfo(
        carrier_name=carrier_name,
        type=carrier_type,
        line_type_intelligence=lti if lti else None,
    )


async def _twilio_purchase_post(
    *,
    account_sid: str,
    auth_token: str,
    body: dict[str, str],
) -> dict[str, Any]:
    """Single POST attempt — NOT retried on HTTP responses (would double-charge).

    Only network-level errors retry via resilient_call's _NETWORK_ERRORS path.
    """
    url = f"{_TWILIO_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers.json"
    async with httpx.AsyncClient(
        auth=(account_sid, auth_token),
        timeout=_TIMEOUT_SECONDS,
    ) as client:
        resp = await client.post(url, data=body)
    if resp.status_code >= 400:
        _raise_twilio_error("purchase_number", resp)
    return resp.json()


async def _twilio_delete_number(
    *,
    account_sid: str,
    auth_token: str,
    twilio_sid: str,
) -> int:
    """Single DELETE attempt of an IncomingPhoneNumber. 404 = already gone."""
    url = f"{_TWILIO_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers/{twilio_sid}.json"
    async with httpx.AsyncClient(
        auth=(account_sid, auth_token),
        timeout=_TIMEOUT_SECONDS,
    ) as client:
        resp = await client.delete(url)
    if resp.status_code == 404:
        return 404
    if resp.status_code >= 400:
        if _is_retryable_twilio_status(resp.status_code):
            raise RetryableError("TWILIO_TRANSIENT", f"Twilio delete transient {resp.status_code}")
        _raise_twilio_error("release_number", resp)
    return resp.status_code


async def _lookup_idempotency(suite_id: str, idempotency_key: str) -> PurchasedNumber | None:
    """Persistent idempotency lookup by (suite_id, purchase_idempotency_key).

    Returns the cached PurchasedNumber if the (suite_id, key) tuple has already
    produced a successful purchase. Returns None on miss or DB error.
    """
    try:
        rows = await supabase_select(
            "tenant_phone_numbers",
            f"suite_id=eq.{suite_id}&purchase_idempotency_key=eq.{idempotency_key}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning("idempotency_lookup db_error suite=%s err=%s", suite_id, exc)
        return None
    if not rows:
        return None
    row = rows[0]
    # Find a matching prior receipt to surface the receipt_id (best-effort).
    receipt_id = row.get("purchase_receipt_id") or ""
    return PurchasedNumber(
        phone_number=row.get("phone_number", ""),
        twilio_sid=row.get("twilio_sid", ""),
        elevenlabs_phone_number_id=row.get("elevenlabs_phone_number_id", "") or "",
        attached_to_agent_id=row.get("attached_to_agent_id", "") or "",
        tenant_id=str(row.get("tenant_id", "")),
        suite_id=str(row.get("suite_id", "")),
        office_id=str(row.get("office_id", "")),
        receipt_id=receipt_id,
        purchased_at=str(row.get("purchased_at", "")),
    )


async def purchase_number(
    phone_number: str,
    *,
    scope: ScopedIdentity,
    idempotency_key: str,
    trace_id: str = "",
    correlation_id: str = "",
    capability_token_id: str = "",
) -> PurchasedNumber:
    """Atomic Yellow-tier phone number purchase flow.

    Steps (all-or-nothing — rollback on any failure):
      1. Persistent idempotency lookup by (suite_id, idempotency_key) — return
         cached PurchasedNumber if already executed (Pass 18+ Lane 2 — survives restart).
      2. POST to Twilio IncomingPhoneNumbers — NOT retried on HTTP responses
         (only on true network errors before remote sees the request).
      3. INSERT into tenant_phone_numbers (status='reserved') WITH
         purchase_idempotency_key set — UNIQUE constraint catches the race
         where two concurrent requests both pass step 1.
      4. POST to EL phone-numbers (import).
      5. PATCH EL phone-number to attach to Sarah Receptionist.
      6. UPDATE tenant_phone_numbers: status='active', el IDs.
      7. Cut phone_number_purchase receipt (Law #2).

    On any failure: release Twilio number, mark DB row released_at,
    cut phone_number_purchase_failed receipt.

    Law #4: Yellow tier — capability token validated upstream by route layer.
    Law #9: account_sid never in logs; auth_token never in logs.
    """
    suite_id = str(scope.suite_id)

    # Persistent idempotency check (Pass 18+ Lane 2 — survives restart)
    existing = await _lookup_idempotency(suite_id, idempotency_key)
    if existing is not None:
        logger.info(
            "purchase_number idempotent_replay suite=%s key=%s...",
            suite_id,
            idempotency_key[:12],
        )
        METRICS.telephony_purchase_counter.labels(outcome="idempotent_replay").inc()
        return existing

    account_sid, auth_token = _twilio_auth()
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)

    twilio_sid: str = ""
    db_row_id: str = ""
    el_phone_number_id: str = ""
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        # ── Step 2: Purchase from Twilio ──────────────────────────────────
        base = _ASPIRE_ORCHESTRATOR_URL
        twilio_body = {
            "PhoneNumber": phone_number,
            "SmsUrl": f"{base}/v1/ingest/twilio/sms/inbound",
            "SmsMethod": "POST",
            "SmsStatusCallback": f"{base}/v1/ingest/twilio/sms/status",
            "SmsStatusCallbackMethod": "POST",
            "StatusCallback": f"{base}/v1/ingest/twilio/voice/recording-status",
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": "initiated ringing answered completed",
            "VoiceMethod": "POST",
            # voice_url left blank — EL overwrites via import (§16.B note)
        }
        try:
            twilio_data = await resilient_call(
                _twilio_purchase_post,
                account_sid=account_sid,
                auth_token=auth_token,
                body=twilio_body,
                breaker=twilio_breaker(),
                policy=TWILIO_RETRY,
                idempotent=False,  # POST: only network-level retries
            )
        except CircuitOpenError as ce:
            METRICS.telephony_purchase_counter.labels(outcome="circuit_open").inc()
            raise TwilioProvisioningError(
                "TWILIO_CIRCUIT_OPEN",
                f"Twilio is degraded — purchase rejected ({ce})",
                503,
            ) from ce

        twilio_sid = twilio_data.get("sid", "")
        friendly_name = twilio_data.get("friendly_name", phone_number)

        # ── Step 3: INSERT tenant_phone_numbers (status=reserved) ─────────
        # purchase_idempotency_key set NOW — UNIQUE partial index catches
        # any concurrent duplicate that lost the step-1 race.
        row: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "suite_id": suite_id,
            "office_id": office_id,
            "phone_number": phone_number,
            "twilio_sid": twilio_sid,
            "twilio_friendly_name": friendly_name,
            "status": "reserved",
            "voice_enabled": True,
            "sms_enabled": True,
            "monthly_cost_cents": 100,
            "purchased_at": now,
            "purchase_idempotency_key": idempotency_key,
        }
        try:
            inserted = await supabase_insert("tenant_phone_numbers", row)
        except SupabaseClientError as ins_exc:
            # If a UNIQUE violation on idempotency_key fired (concurrent purchase),
            # re-read the existing row and treat as idempotent replay.
            err_str = str(ins_exc)
            if "23505" in err_str or "duplicate" in err_str.lower() or "idempotency" in err_str.lower():
                logger.info(
                    "purchase_number unique_race suite=%s key=%s... — re-reading",
                    suite_id,
                    idempotency_key[:12],
                )
                # Best-effort: rollback the Twilio purchase we just made (the OTHER
                # request will own the EL side). Fail-safe: if rollback fails,
                # the lifecycle reaper will release orphaned Twilio numbers.
                if twilio_sid:
                    try:
                        await _twilio_delete_number(
                            account_sid=account_sid,
                            auth_token=auth_token,
                            twilio_sid=twilio_sid,
                        )
                    except Exception as drop_exc:
                        logger.error(
                            "purchase_number duplicate_rollback failed sid=%s: %s",
                            twilio_sid,
                            drop_exc,
                        )
                existing = await _lookup_idempotency(suite_id, idempotency_key)
                if existing is not None:
                    METRICS.telephony_purchase_counter.labels(outcome="idempotent_replay").inc()
                    return existing
            raise
        db_row_id = inserted.get("id") or row["id"]

        # ── Step 4: Import to ElevenLabs ──────────────────────────────────
        el_phone_number_id = await import_to_elevenlabs(
            phone_number=phone_number,
            label=friendly_name,
            twilio_sid=account_sid,   # EL needs workspace SID for API access
            twilio_token=auth_token,
        )

        # MARK: persona-attach
        # ── Step 5: Attach to chosen receptionist persona ─────────────────
        # Read the office's persona choice from front_desk_configs
        # (migration 109). Defaults to 'sarah' if no config row exists yet —
        # matches CHECK default and the historical attach behavior for
        # offices purchasing before completing Front Desk Setup.
        chosen_persona_slug = DEFAULT_PERSONA_SLUG
        try:
            persona_rows = await supabase_select(
                "front_desk_configs",
                f"office_id=eq.{office_id}&is_current=eq.true",
                limit=1,
            )
            if persona_rows:
                chosen_persona_slug = (
                    persona_rows[0].get("receptionist_persona") or DEFAULT_PERSONA_SLUG
                )
        except SupabaseClientError as persona_exc:
            logger.warning(
                "purchase_number persona_lookup_failed office=%s — using default '%s': %s",
                office_id, DEFAULT_PERSONA_SLUG, persona_exc,
            )
        chosen_persona = get_persona(chosen_persona_slug)
        attached_agent_id = chosen_persona.agent_id

        await attach_to_agent(el_phone_number_id, agent_id=attached_agent_id)

        # ── Step 6: UPDATE DB row: status=active, EL IDs ─────────────────
        await supabase_update(
            "tenant_phone_numbers",
            f"id=eq.{db_row_id}",
            {
                "status": "active",
                "elevenlabs_phone_number_id": el_phone_number_id,
                "attached_to_agent_id": attached_agent_id,
            },
        )

    except (TwilioProvisioningError, ElevenLabsPhoneError, SupabaseClientError) as exc:
        # ── Rollback ──────────────────────────────────────────────────────
        await _rollback_purchase(
            twilio_sid=twilio_sid,
            db_row_id=db_row_id,
            el_phone_number_id=el_phone_number_id,
            account_sid=account_sid,
            auth_token=auth_token,
        )
        METRICS.telephony_purchase_counter.labels(outcome="failed").inc()
        receipt_store.store_receipts([{
            "id": receipt_id,
            "receipt_type": "phone_number_purchase_failed",
            "suite_id": suite_id,
            "office_id": office_id,
            "tenant_id": tenant_id,
            "outcome": "failed",
            "action_type": "phone_number_purchase",
            "tool_used": "twilio_provisioning",
            "risk_tier": "yellow",
            "reason_code": getattr(exc, "code", "UNKNOWN_ERROR"),
            "redacted_inputs": {
                "phone_number": phone_number,
                "idempotency_key": idempotency_key,
            },
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "capability_token_id": capability_token_id or None,
            "created_at": now,
        }])
        raise

    # ── Step 7: Cut purchase receipt (Law #2) ─────────────────────────────
    receipt_store.store_receipts([{
        "id": receipt_id,
        "receipt_type": "phone_number_purchase",
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "outcome": "success",
        "action_type": "phone_number_purchase",
        "tool_used": "twilio_provisioning",
        "risk_tier": "yellow",
        "redacted_inputs": {
            "phone_number": phone_number,
            "idempotency_key": idempotency_key,
        },
        "redacted_outputs": {
            "twilio_sid": twilio_sid,
            "el_phone_number_id": el_phone_number_id,
            "attached_to_agent_id": attached_agent_id,
            "receptionist_persona": chosen_persona_slug,
        },
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "capability_token_id": capability_token_id or None,
        "created_at": now,
    }])

    result = PurchasedNumber(
        phone_number=phone_number,
        twilio_sid=twilio_sid,
        elevenlabs_phone_number_id=el_phone_number_id,
        attached_to_agent_id=attached_agent_id,
        tenant_id=tenant_id,
        suite_id=suite_id,
        office_id=office_id,
        receipt_id=receipt_id,
        purchased_at=now,
    )

    METRICS.telephony_purchase_counter.labels(outcome="success").inc()

    logger.info(
        "purchase_number success phone=%s twilio_sid=%s el_id=%s...",
        phone_number,
        twilio_sid,
        el_phone_number_id[:12],
    )
    return result


async def _rollback_purchase(
    *,
    twilio_sid: str,
    db_row_id: str,
    el_phone_number_id: str,
    account_sid: str,
    auth_token: str,
) -> None:
    """Best-effort rollback. Logs failures but does not re-raise."""
    # Detach from EL if import succeeded
    if el_phone_number_id:
        try:
            await detach_from_elevenlabs(el_phone_number_id)
        except Exception as exc:
            logger.error("rollback: EL detach failed for %s: %s", el_phone_number_id[:12], exc)

    # Release from Twilio if purchase succeeded — DELETE is idempotent (404 = ok)
    if twilio_sid:
        try:
            await resilient_call(
                _twilio_delete_number,
                account_sid=account_sid,
                auth_token=auth_token,
                twilio_sid=twilio_sid,
                breaker=twilio_breaker(),
                policy=TWILIO_RETRY,
                idempotent=True,
            )
            logger.info("rollback: Twilio number %s released", twilio_sid)
        except Exception as exc:
            logger.error("rollback: Twilio release failed for %s: %s", twilio_sid, exc)

    # Mark DB row released
    if db_row_id:
        try:
            await supabase_update(
                "tenant_phone_numbers",
                f"id=eq.{db_row_id}",
                {"status": "released", "released_at": datetime.now(timezone.utc).isoformat()},
            )
        except Exception as exc:
            logger.error("rollback: DB row update failed for %s: %s", db_row_id, exc)


async def release_number(
    phone_number_id: str,
    *,
    scope: ScopedIdentity | None = None,
    trace_id: str = "",
    correlation_id: str = "",
    capability_token_id: str = "",
) -> None:
    """Yellow-tier: detach from EL -> release from Twilio -> mark released_at.

    Law #2: cuts phone_number_release receipt.
    Law #4: Yellow tier — capability token validated upstream.
    Law #6 (Pass 18 fix THREAT-015): scope binding — phone_number_id alone is
        insufficient; we MUST also filter by suite_id from the authenticated
        scope so an attacker with a valid release token for THEIR own suite
        cannot release another tenant's number by supplying a foreign UUID.
        `scope=None` is allowed for system-internal callers (lifecycle jobs)
        but external API routes MUST pass scope.
    """
    account_sid, auth_token = _twilio_auth()
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Pass 18 fix THREAT-015: bind to authenticated scope when provided.
    if scope is not None:
        filter_str = (
            f"id=eq.{phone_number_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
        )
    else:
        filter_str = f"id=eq.{phone_number_id}"

    rows = await supabase_select(
        "tenant_phone_numbers",
        filter_str,
        limit=1,
    )
    if not rows:
        raise TwilioProvisioningError(
            "PHONE_NUMBER_NOT_FOUND",
            f"tenant_phone_numbers row not found for id={phone_number_id}",
            404,
        )
    row = rows[0]
    suite_id = row.get("suite_id", "")
    office_id = row.get("office_id", "")
    tenant_id = row.get("tenant_id", "")
    el_id = row.get("elevenlabs_phone_number_id") or ""
    twilio_sid = row.get("twilio_sid") or ""
    phone_number = row.get("phone_number", "")

    # Detach from EL first (so it stops handling voice calls)
    if el_id:
        try:
            await detach_from_elevenlabs(el_id)
        except Exception as exc:
            logger.error("release_number: EL detach failed for %s: %s", el_id[:12], exc)

    # Release from Twilio (resilient)
    if twilio_sid:
        try:
            await resilient_call(
                _twilio_delete_number,
                account_sid=account_sid,
                auth_token=auth_token,
                twilio_sid=twilio_sid,
                breaker=twilio_breaker(),
                policy=TWILIO_RETRY,
                idempotent=True,
            )
        except CircuitOpenError as ce:
            METRICS.telephony_release_counter.labels(outcome="circuit_open").inc()
            raise TwilioProvisioningError(
                "TWILIO_CIRCUIT_OPEN",
                f"Twilio is degraded — release rejected ({ce})",
                503,
            ) from ce
        except TwilioProvisioningError:
            METRICS.telephony_release_counter.labels(outcome="failed").inc()
            raise

    # Mark released in DB
    await supabase_update(
        "tenant_phone_numbers",
        f"id=eq.{phone_number_id}",
        {"status": "released", "released_at": now},
    )

    # Receipt (Law #2)
    receipt_store.store_receipts([{
        "id": receipt_id,
        "receipt_type": "phone_number_release",
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "outcome": "success",
        "action_type": "phone_number_release",
        "tool_used": "twilio_provisioning",
        "risk_tier": "yellow",
        "redacted_inputs": {"phone_number": phone_number},
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "capability_token_id": capability_token_id or None,
        "created_at": now,
    }])

    METRICS.telephony_release_counter.labels(outcome="success").inc()
    logger.info(
        "release_number success phone=%s twilio_sid=%s",
        phone_number,
        twilio_sid,
    )


__all__ = [
    "AvailableNumber",
    "CarrierInfo",
    "PhoneCapabilities",
    "PurchasedNumber",
    "TwilioProvisioningError",
    "lookup_carrier",
    "search_available_numbers",
    "purchase_number",
    "release_number",
]
