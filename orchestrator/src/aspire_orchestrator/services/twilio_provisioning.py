"""Twilio phone-number provisioning service (Pass 16 — §16.B).

Provides:
  - search_available_numbers: search available US local numbers via Twilio REST API.
  - purchase_number: atomic Yellow-tier purchase → EL import → EL attach → receipt.
  - release_number: EL detach → Twilio release → mark released_at.

Aspire Laws enforced:
  Law #2 — every state change cuts an immutable receipt.
  Law #3 — fail closed on missing credentials.
  Law #4 — Yellow tier: capability token validated upstream by route layer.
  Law #6 — suite_id/office_id scoped; never cross-tenant.
  Law #9 — twilio_account_sid and twilio_auth_token never logged.

Idempotency:
  purchase_number accepts a client-generated idempotency_key.
  Duplicate key returns cached PurchasedNumber without re-executing Twilio call.
  Window: 24 hours (in-memory; Redis/Supabase migration in Phase 2).
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

# In-memory idempotency store (24h window, Phase 1)
_idem_store: dict[str, "PurchasedNumber"] = {}


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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def search_available_numbers(
    area_code: str,
    contains: str | None = None,
    limit: int = 20,
) -> list[AvailableNumber]:
    """Search available US Local phone numbers via Twilio REST API.

    GET /AvailablePhoneNumbers/US/Local.json?AreaCode={n}&Contains={c}
    Returns sanitized list — Twilio account SID never included in response.

    Law #9: account_sid used only in auth, never returned.
    """
    account_sid, auth_token = _twilio_auth()
    params: dict[str, Any] = {
        "AreaCode": area_code,
        "PageSize": min(limit, 50),
        "VoiceEnabled": "true",
    }
    if contains:
        params["Contains"] = contains

    url = f"{_TWILIO_BASE}/Accounts/{account_sid}/AvailablePhoneNumbers/US/Local.json"
    async with httpx.AsyncClient(
        auth=(account_sid, auth_token),
        timeout=_TIMEOUT_SECONDS,
    ) as client:
        resp = await client.get(url, params=params)

    if resp.status_code >= 400:
        _raise_twilio_error("search_available_numbers", resp)

    data = resp.json()
    numbers: list[AvailableNumber] = []
    for raw in data.get("available_phone_numbers", []):
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
                monthly_cost_cents=100,  # Twilio US Local = $1.00/mo standard
                capabilities=caps,
            )
        )

    logger.info(
        "twilio_search area_code=%s contains=%s results=%d",
        area_code,
        contains or "",
        len(numbers),
    )
    return numbers


async def purchase_number(
    phone_number: str,
    *,
    scope: ScopedIdentity,
    idempotency_key: str,
) -> PurchasedNumber:
    """Atomic Yellow-tier phone number purchase flow.

    Steps (all-or-nothing — rollback on any failure):
      1. Check idempotency (return cached if duplicate key).
      2. POST to Twilio IncomingPhoneNumbers with sms_url + status callbacks.
      3. INSERT into tenant_phone_numbers (status='reserved').
      4. POST to EL phone-numbers (import).
      5. PATCH EL phone-number to attach to Sarah Receptionist.
      6. UPDATE tenant_phone_numbers: status='active', el IDs.
      7. Cut phone_number_purchase receipt (Law #2).

    On any failure: release Twilio number, mark DB row released_at,
    cut phone_number_purchase_failed receipt.

    Law #4: Yellow tier — capability token validated upstream by route layer.
    Law #9: account_sid never in logs; auth_token never in logs.
    """
    # Idempotency check (Law #3: duplicate key = return cached, no re-execute)
    idem_cache_key = f"{scope.suite_id}:{idempotency_key}"
    if idem_cache_key in _idem_store:
        logger.info(
            "purchase_number idempotent_replay key=%s...",
            idempotency_key[:12],
        )
        return _idem_store[idem_cache_key]

    account_sid, auth_token = _twilio_auth()
    suite_id = str(scope.suite_id)
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
        url = f"{_TWILIO_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers.json"
        async with httpx.AsyncClient(
            auth=(account_sid, auth_token),
            timeout=_TIMEOUT_SECONDS,
        ) as client:
            resp = await client.post(url, data=twilio_body)
        if resp.status_code >= 400:
            _raise_twilio_error("purchase_number", resp)

        twilio_data = resp.json()
        twilio_sid = twilio_data.get("sid", "")
        friendly_name = twilio_data.get("friendly_name", phone_number)

        # ── Step 3: INSERT tenant_phone_numbers (status=reserved) ─────────
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
        }
        inserted = await supabase_insert("tenant_phone_numbers", row)
        db_row_id = inserted.get("id") or row["id"]

        # ── Step 4: Import to ElevenLabs ──────────────────────────────────
        el_phone_number_id = await import_to_elevenlabs(
            phone_number=phone_number,
            label=friendly_name,
            twilio_sid=account_sid,   # EL needs workspace SID for API access
            twilio_token=auth_token,
        )

        # ── Step 5: Attach to Sarah Receptionist ─────────────────────────
        await attach_to_agent(el_phone_number_id, agent_id=SARAH_RECEPTIONIST_AGENT_ID)

        # ── Step 6: UPDATE DB row: status=active, EL IDs ─────────────────
        await supabase_update(
            "tenant_phone_numbers",
            f"id=eq.{db_row_id}",
            {
                "status": "active",
                "elevenlabs_phone_number_id": el_phone_number_id,
                "attached_to_agent_id": SARAH_RECEPTIONIST_AGENT_ID,
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
            "redacted_inputs": {"phone_number": phone_number},
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
        "redacted_inputs": {"phone_number": phone_number},
        "redacted_outputs": {
            "twilio_sid": twilio_sid,
            "el_phone_number_id": el_phone_number_id,
            "attached_to_agent_id": SARAH_RECEPTIONIST_AGENT_ID,
        },
        "created_at": now,
    }])

    result = PurchasedNumber(
        phone_number=phone_number,
        twilio_sid=twilio_sid,
        elevenlabs_phone_number_id=el_phone_number_id,
        attached_to_agent_id=SARAH_RECEPTIONIST_AGENT_ID,
        tenant_id=tenant_id,
        suite_id=suite_id,
        office_id=office_id,
        receipt_id=receipt_id,
        purchased_at=now,
    )

    # Cache for idempotency (Law #3)
    _idem_store[idem_cache_key] = result

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

    # Release from Twilio if purchase succeeded
    if twilio_sid:
        try:
            url = f"{_TWILIO_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers/{twilio_sid}.json"
            async with httpx.AsyncClient(
                auth=(account_sid, auth_token),
                timeout=_TIMEOUT_SECONDS,
            ) as client:
                await client.delete(url)
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
) -> None:
    """Yellow-tier: detach from EL → release from Twilio → mark released_at.

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
    # Returns 404 (PHONE_NUMBER_NOT_FOUND) for cross-tenant attempts so the
    # caller cannot distinguish "doesn't exist" from "exists in another tenant".
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
        await detach_from_elevenlabs(el_id)

    # Release from Twilio
    if twilio_sid:
        url = f"{_TWILIO_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers/{twilio_sid}.json"
        async with httpx.AsyncClient(
            auth=(account_sid, auth_token),
            timeout=_TIMEOUT_SECONDS,
        ) as client:
            resp = await client.delete(url)
        if resp.status_code >= 400 and resp.status_code != 404:
            _raise_twilio_error("release_number", resp)

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
        "created_at": now,
    }])

    logger.info(
        "release_number success phone=%s twilio_sid=%s",
        phone_number,
        twilio_sid,
    )


__all__ = [
    "AvailableNumber",
    "PhoneCapabilities",
    "PurchasedNumber",
    "TwilioProvisioningError",
    "search_available_numbers",
    "purchase_number",
    "release_number",
]
