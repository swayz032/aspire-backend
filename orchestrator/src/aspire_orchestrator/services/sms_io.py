"""SMS outbound service + Twilio status callback handler (Pass 16 — §16.E).

EL's Twilio integration is VOICE-ONLY. SMS does NOT route through ElevenLabs.
Aspire owns the SMS path end-to-end:
  - Inbound: Twilio → /v1/ingest/twilio/sms/inbound → SMSIngestionAdapter (Pass 14)
  - Outbound: /v1/sms/send → send_sms() → Twilio Messages API
  - Status updates: Twilio → /v1/ingest/twilio/sms/status → update_sms_status()

Aspire Laws enforced:
  Law #2 — every state change cuts an immutable receipt.
  Law #3 — fail closed on missing credentials.
  Law #4 — Yellow tier; capability token validated upstream by route layer.
  Law #6 — scope resolved from thread_memory_id → tenant_phone_numbers (no header trust).
  Law #9 — phone number body content truncated to 80 chars in INFO logs.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.metrics import METRICS
from aspire_orchestrator.services.resilience import (
    TWILIO_RETRY,
    CircuitOpenError,
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
_ASPIRE_ORCHESTRATOR_URL = "https://orchestrator.aspire.app"

# Terminal Twilio message statuses — cut receipt on these
_TERMINAL_STATUSES = frozenset({"delivered", "failed", "undelivered"})


class SmsIoError(Exception):
    """Raised on SMS service failures."""

    def __init__(self, code: str, message: str, status_code: int = 0) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


def _twilio_auth() -> tuple[str, str]:
    """Return (account_sid, auth_token). Fail closed if not configured."""
    sid = settings.twilio_account_sid
    token = settings.twilio_auth_token
    if not sid or not token:
        raise SmsIoError(
            "MISSING_TWILIO_CREDENTIALS",
            "twilio_account_sid or twilio_auth_token not configured. "
            "Fail-closed per Law #3.",
        )
    return sid, token


def _make_idempotency_key(thread_memory_id: str, body: str) -> str:
    """Deterministic idempotency key: SHA256(thread_id||body||minute_bucket).

    Minute-bucketed to prevent sending identical SMS twice within the same
    minute (double-click guard) while allowing re-sends after the window.
    """
    now_minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    raw = f"{thread_memory_id}|{body}|{now_minute}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def send_sms(
    thread_memory_id: str,
    body: str,
    *,
    scope: ScopedIdentity,
    capability_token: str,
    idempotency_key: str,
    trace_id: str = "",
    correlation_id: str = "",
    capability_token_id: str = "",
) -> dict[str, Any]:
    """Yellow-tier: send an outbound SMS via Twilio Messages API.

    Steps:
      1. Resolve from_number via tenant_phone_numbers (same office, sms_enabled).
      2. Resolve to_number via thread_memory_id → memory_objects.detail.from.
      3. POST to Twilio /Messages.json.
      4. INSERT into sms_messages (direction='outbound').
      5. Append new memory_object referencing the thread (Law #2 — append-only).
      6. Cut sms_outbound receipt.
      7. Return {message_sid, status}.

    Law #4: Yellow tier — capability_token validated upstream (route layer).
    Law #9: body truncated to 80 chars in INFO logs; capability_token not logged.
    """
    account_sid, auth_token = _twilio_auth()
    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # ── A2P 10DLC gate (Pass 19 Law #3: fail closed) ──────────────────────
    # Gate checks tenant_a2p_registrations by scope.tenant_id (never from
    # payload — that would allow a cross-tenant tenant_id injection).
    a2p_rows = await supabase_select(
        "tenant_a2p_registrations",
        f"tenant_id=eq.{tenant_id}",
        limit=1,
    )
    # Verify that the returned row belongs to THIS tenant (defence-in-depth against
    # cross-tenant RLS bypass). RLS should enforce this at DB layer; we double-check
    # here at the service layer (Law #6).
    if a2p_rows:
        row_tenant = str(a2p_rows[0].get("tenant_id") or "")
        if row_tenant != tenant_id:
            logger.error(
                "sms_io a2p_row_tenant_mismatch scope_tenant=%s row_tenant=%s — denying",
                tenant_id,
                row_tenant,
            )
            a2p_rows = []  # Treat as no row — blocked
    a2p_status = (a2p_rows[0].get("status") if a2p_rows else None) or "unregistered"
    if a2p_status != "registered":
        # Block SMS — cut receipt then raise (Law #2 + Law #3)
        to_prefix_early = ""  # from_number not yet resolved; omit from receipt
        receipt_store.store_receipts([{
            "id": receipt_id,
            "receipt_type": "sms_send_blocked_a2p",
            "suite_id": suite_id,
            "office_id": office_id,
            "tenant_id": tenant_id,
            "outcome": "denied",
            "action_type": "sms_send",
            "tool_used": "sms_io",
            "risk_tier": "yellow",
            "reason_code": "a2p_not_registered",
            "redacted_inputs": {
                "thread_memory_id": thread_memory_id,
                "body_length": len(body),
                "a2p_status": a2p_status,
            },
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "capability_token_id": capability_token_id or None,
            "created_at": now,
        }])
        logger.warning(
            "sms_send blocked tenant=%s a2p_status=%s",
            tenant_id,
            a2p_status,
        )
        raise SmsIoError(
            "A2P_NOT_REGISTERED",
            f"Outbound SMS blocked: tenant A2P registration status is '{a2p_status}'. "
            "Complete A2P 10DLC registration to enable SMS.",
            403,
        )

    # ── Resolve from_number ───────────────────────────────────────────────
    from_rows = await supabase_select(
        "tenant_phone_numbers",
        f"office_id=eq.{office_id}&sms_enabled=eq.true&status=eq.active",
        limit=1,
    )
    if not from_rows:
        raise SmsIoError(
            "NO_SMS_NUMBER",
            f"No active SMS-enabled number found for office_id={office_id}",
            422,
        )
    from_number = (from_rows[0].get("phone_number") or "").strip()
    if not from_number:
        # Defensive: row exists but phone_number column is null/empty.
        # Never let an empty From reach Twilio (would return 21603).
        raise SmsIoError(
            "NO_SMS_NUMBER",
            f"tenant_phone_numbers row for office_id={office_id} has empty phone_number",
            422,
        )

    # ── Resolve to_number from thread ────────────────────────────────────
    thread_rows = await supabase_select(
        "memory_objects",
        f"memory_id=eq.{thread_memory_id}&suite_id=eq.{suite_id}",
        limit=1,
    )
    if not thread_rows:
        raise SmsIoError(
            "THREAD_NOT_FOUND",
            f"memory_object not found for thread_memory_id={thread_memory_id}",
            404,
        )
    thread_row = thread_rows[0]
    detail = thread_row.get("detail") or {}
    # direction is from original inbound: 'from' is the external contact number
    to_number = detail.get("from") or detail.get("to") or ""
    if not to_number:
        raise SmsIoError(
            "CANNOT_RESOLVE_TO_NUMBER",
            f"thread detail missing from/to fields for thread_memory_id={thread_memory_id}",
            422,
        )

    # ── POST to Twilio Messages API ───────────────────────────────────────
    msg_body: dict[str, str] = {
        "From": from_number,
        "To": to_number,
        "Body": body,
        "StatusCallback": f"{_ASPIRE_ORCHESTRATOR_URL}/v1/ingest/twilio/sms/status",
        "StatusCallbackMethod": "POST",
    }
    url = f"{_TWILIO_BASE}/Accounts/{account_sid}/Messages.json"

    # Pass 18+ Lane 2: forward our deterministic idempotency_key to Twilio
    # via the Idempotency-Key HTTP header. Twilio dedups identical (account, key)
    # tuples server-side, preventing duplicate sends from a retry storm.
    twilio_headers = {"Idempotency-Key": idempotency_key}

    async def _do_post_message() -> httpx.Response:
        async with httpx.AsyncClient(
            auth=(account_sid, auth_token),
            timeout=_TIMEOUT_SECONDS,
        ) as client:
            return await client.post(url, data=msg_body, headers=twilio_headers)

    start = time.monotonic()
    try:
        # SMS send is non-idempotent from our side: Twilio dedupes via the
        # header, but the response itself is a side effect (billing). Mark
        # idempotent=False so retries fire ONLY on true network errors before
        # Twilio sees the request.
        resp = await resilient_call(
            _do_post_message,
            breaker=twilio_breaker(),
            policy=TWILIO_RETRY,
            idempotent=False,
        )
    except CircuitOpenError as ce:
        METRICS.sms_send_counter.labels(outcome="circuit_open").inc()
        raise SmsIoError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — SMS send rejected ({ce})",
            503,
        ) from ce
    except Exception:
        METRICS.sms_send_counter.labels(outcome="timeout").inc()
        raise
    finally:
        METRICS.sms_outbound_latency.observe(time.monotonic() - start)

    if resp.status_code >= 400:
        METRICS.sms_send_counter.labels(outcome="failed").inc()
        detail_str = f"HTTP {resp.status_code}"
        try:
            err = resp.json()
            detail_str = err.get("message", detail_str)
        except Exception:
            pass
        # Pass I Law #2 fix: cut sms_failed receipt before re-raising so every
        # outbound attempt has a receipt regardless of outcome.
        to_prefix_fail = (to_number or "")[:6] + "..." if to_number else ""
        receipt_store.store_receipts([{
            "id": str(uuid.uuid4()),
            "receipt_type": "sms_failed",
            "suite_id": suite_id,
            "office_id": office_id,
            "tenant_id": tenant_id,
            "outcome": "failed",
            "action_type": "sms_send",
            "tool_used": "sms_io",
            "risk_tier": "yellow",
            "reason_code": "TWILIO_SEND_FAILED",
            "redacted_inputs": {
                "thread_memory_id": thread_memory_id,
                "to_prefix": to_prefix_fail,
                "body_length": len(body),
            },
            "redacted_outputs": {"twilio_status": resp.status_code},
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "capability_token_id": capability_token_id or None,
            "created_at": now,
        }])
        raise SmsIoError(
            "TWILIO_SEND_FAILED",
            f"Twilio Messages.create failed: {detail_str}",
            resp.status_code,
        )

    twilio_data = resp.json()
    message_sid = twilio_data.get("sid", "")
    message_status = twilio_data.get("status", "queued")

    # ── INSERT into sms_messages ──────────────────────────────────────────
    sms_row: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "thread_memory_id": thread_memory_id,
        "message_sid": message_sid,
        "direction": "outbound",
        "from_number": from_number,
        "to_number": to_number,
        "status": message_status,
        "idempotency_key": idempotency_key,
        "sent_at": now,
    }
    try:
        await supabase_insert("sms_messages", sms_row)
    except SupabaseClientError as exc:
        logger.error("sms_messages insert failed (non-blocking): %s", exc)

    # ── Append new memory_object (Law #2 — append-only) ───────────────────
    # Write a NEW memory row that links back to the thread. Never mutate the
    # thread row itself (immutable per Law #2).
    try:
        outbound_memory: dict[str, Any] = {
            "memory_id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "suite_id": suite_id,
            "office_id": office_id,
            "memory_type": "sms_thread",
            "title": f"SMS to {to_number}",
            "summary": (body[:140] + "…") if len(body) > 140 else body,
            "detail": {
                "direction": "outbound",
                "from": from_number,
                "to": to_number,
                "body": body,
                "message_sid": message_sid,
                "status": message_status,
            },
            "source_surface": "system",
            "runtime_family": "provider_webhook",
            "channel": "sms",
            "source_record_id": message_sid,
            "visibility_scope": "office",
            "idempotency_key": f"sms-outbound:{message_sid}",
            "thread_id": thread_memory_id,  # schema column for linking to thread row
            "created_at": now,
        }
        await supabase_insert("memory_objects", outbound_memory)
    except SupabaseClientError as exc:
        logger.error("outbound memory_object insert failed (non-blocking): %s", exc)

    # ── Cut receipt (Law #2) ──────────────────────────────────────────────
    body_preview = body[:80] + ("…" if len(body) > 80 else "")
    # Pass 18 fix THREAT-017: phone numbers are PII (Law #9). Receipts are
    # immutable, so any leakage is permanent. Mask `to` to first 6 digits +
    # ellipsis (matches caller_id_log pattern in sarah.py:210).
    to_prefix = (to_number or "")[:6] + "..." if to_number else ""
    from_prefix = (from_number or "")[:6] + "..." if from_number else ""

    receipt_store.store_receipts([{
        "id": receipt_id,
        "receipt_type": "sms_outbound",
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "outcome": "success",
        "action_type": "sms_send",
        "tool_used": "sms_io",
        "risk_tier": "yellow",
        "redacted_inputs": {
            "thread_memory_id": thread_memory_id,
            "to_prefix": to_prefix,
            "from_prefix": from_prefix,
            "body_preview": body_preview,
            "idempotency_key": idempotency_key,
        },
        "redacted_outputs": {
            "message_sid": message_sid,
            "status": message_status,
        },
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "capability_token_id": capability_token_id or None,
        "created_at": now,
    }])

    # Pass 18 fix PII-1: also mask phone numbers in INFO log lines.
    logger.info(
        "sms_send success from=%s to=%s sid=%s status=%s body=%.80s",
        from_prefix,
        to_prefix,
        message_sid,
        message_status,
        body,
    )
    METRICS.sms_send_counter.labels(outcome="success").inc()
    return {"message_sid": message_sid, "status": message_status, "receipt_id": receipt_id}


async def update_sms_status(
    twilio_message_sid: str,
    new_status: str,
    error_code: str | None = None,
) -> None:
    """Twilio status callback handler (idempotent on MessageSid).

    Updates sms_messages.status.
    Cuts sms_status_update receipt for terminal states
    (delivered/failed/undelivered).

    Law #2: receipt cut on every terminal status change.
    """
    now = datetime.now(timezone.utc).isoformat()
    receipt_id = str(uuid.uuid4())

    try:
        rows = await supabase_select(
            "sms_messages",
            f"message_sid=eq.{twilio_message_sid}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error("sms_status update select failed for sid=%s: %s", twilio_message_sid, exc)
        return

    if not rows:
        logger.warning("sms_status update: no sms_messages row for sid=%s", twilio_message_sid)
        return

    row = rows[0]
    suite_id = row.get("suite_id", "")
    office_id = row.get("office_id", "")
    tenant_id = row.get("tenant_id", "")

    update_data: dict[str, Any] = {"status": new_status, "updated_at": now}
    if error_code:
        update_data["error_code"] = error_code

    try:
        await supabase_update(
            "sms_messages",
            f"message_sid=eq.{twilio_message_sid}",
            update_data,
        )
    except SupabaseClientError as exc:
        logger.error("sms_status update write failed for sid=%s: %s", twilio_message_sid, exc)

    # Cut receipt only on terminal states (Law #2)
    if new_status.lower() in _TERMINAL_STATUSES:
        receipt_store.store_receipts([{
            "id": receipt_id,
            "receipt_type": "sms_status_update",
            "suite_id": suite_id,
            "office_id": office_id,
            "tenant_id": tenant_id,
            "outcome": "success" if new_status.lower() == "delivered" else "failed",
            "action_type": "sms_status_update",
            "tool_used": "sms_io",
            "risk_tier": "green",
            "redacted_inputs": {"message_sid": twilio_message_sid},
            "redacted_outputs": {
                "status": new_status,
                "error_code": error_code or "",
            },
            "created_at": now,
        }])
        logger.info(
            "sms_status terminal sid=%s status=%s error_code=%s",
            twilio_message_sid,
            new_status,
            error_code or "",
        )
    else:
        logger.debug("sms_status update sid=%s status=%s", twilio_message_sid, new_status)


_E164_RE_10 = re.compile(r"^\d{10}$")
_E164_RE_11 = re.compile(r"^1\d{10}$")


def _normalize_to_e164(raw: str) -> str:
    """Normalize an inbound phone number to E.164 (+1XXXXXXXXXX for US/CA).

    Accepts:
      - 10-digit strings (US local): '9175550200' -> '+19175550200'
      - 11-digit strings starting with '1': '19175550200' -> '+19175550200'
      - Already-formatted E.164: '+19175550200' -> '+19175550200' (pass-through)

    Raises SmsIoError(INVALID_TO_PHONE, ..., 422) for anything else.
    Law #3: fail closed — never guess or silently coerce an ambiguous number.
    """
    # Strip all non-digit characters except a leading '+' (which we handle separately)
    stripped = re.sub(r"[^\d+]", "", raw.strip())

    # Pass-through: already E.164 (must start with '+' and have digits only after)
    if stripped.startswith("+"):
        digits_only = stripped[1:]
        if digits_only.isdigit() and len(digits_only) >= 7:
            return stripped
        raise SmsIoError(
            "INVALID_TO_PHONE",
            f"Phone number '{raw[:20]}' has a '+' prefix but invalid digit sequence.",
            422,
        )

    digits = stripped
    if _E164_RE_10.match(digits):
        return f"+1{digits}"
    if _E164_RE_11.match(digits):
        return f"+{digits}"

    raise SmsIoError(
        "INVALID_TO_PHONE",
        f"Cannot normalize '{raw[:20]}' to E.164. "
        "Provide a 10-digit US number, 11-digit (1+10), or full E.164 (+1...).",
        422,
    )


async def send_sms_new(
    to_phone: str,
    body: str,
    *,
    scope: ScopedIdentity,
    capability_token: str,
    idempotency_key: str,
    trace_id: str = "",
    correlation_id: str = "",
    capability_token_id: str = "",
) -> dict[str, Any]:
    """Yellow-tier: send an outbound SMS to a NEW (no existing thread) recipient.

    Steps:
      1. Normalize to_phone to E.164 (fail closed on bad input).
      2. Run A2P 10DLC gate — same block as send_sms (Law #3).
      3. Resolve from_number via tenant_phone_numbers.
      4. INSERT a new memory_objects thread row (kind=sms_thread, origin=compose).
      5. Delegate to send_sms(thread_memory_id=<new_uuid>) — all downstream
         invariants (sms_messages insert, receipt, append-only memory log) are
         handled there exactly once.  We do NOT cut a second receipt.
      6. Return send_sms result PLUS thread_memory_id.

    Law #4: Yellow tier — capability_token validated upstream (route layer).
    Law #6: suite_id/office_id/tenant_id sourced ONLY from scope, never payload.
    Law #9: phone number truncated in logs; body_preview <= 80 chars.
    """
    import re as _re  # already imported at module level; local alias for readability

    # ── Normalize to_phone (fail closed) ──────────────────────────────────
    to_phone_e164 = _normalize_to_e164(to_phone)

    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)
    now = datetime.now(timezone.utc).isoformat()

    # Pass D perf fix 2026-05-13: removed the duplicate A2P gate + from_number
    # resolve that were originally done here AND again inside send_sms().
    # send_sms() runs the canonical A2P gate + from_number lookup exactly once.
    # Duplicates were adding 2–4s upstream and contributing to 504s on
    # /v1/sms/send-new.  We only need to: (1) create the thread row, (2)
    # delegate.  send_sms resolves to_number from thread detail.from.

    # ── Create new thread row in memory_objects ───────────────────────────
    # Law #6: all tenant fields from scope only.
    # detail.origin='compose' lets inbox feed distinguish compose-originated
    # threads from inbound-originated ones.
    # Note: detail.to (our from_number) is intentionally omitted here — send_sms
    # writes the outbound sms_messages row with the resolved from_number.
    thread_memory_id = str(uuid.uuid4())
    # Schema-correct memory_objects insert. Founder fix 2026-05-13: previous
    # version used 'kind' (does not exist — PGRST204) and 'updated_at' (does
    # not exist; use 'last_activity_at'). Column names verified against live
    # schema: memory_id, suite_id, office_id, tenant_id, memory_type, channel,
    # detail (jsonb), created_at, last_activity_at, status.
    thread_row: dict[str, Any] = {
        "memory_id": thread_memory_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "memory_type": "sms_thread",
        "channel": "sms",
        "status": "active",
        "detail": {
            "from": to_phone_e164,   # external contact (inbound convention)
            "channel": "sms",
            "origin": "compose",
        },
        "created_at": now,
        "last_activity_at": now,
    }
    try:
        await supabase_insert("memory_objects", thread_row)
    except SupabaseClientError as exc:
        logger.error("sms_send_new: thread memory_object insert failed: %s", exc)
        raise SmsIoError(
            "THREAD_CREATE_FAILED",
            f"Failed to create SMS thread record: {exc}",
            500,
        ) from exc

    # ── Delegate to send_sms ──────────────────────────────────────────────
    # send_sms resolves to_number from thread detail.from (which we just set
    # to to_phone_e164), sends via Twilio, inserts sms_messages, appends
    # outbound memory_object, and cuts the receipt.  No duplicate receipt here.
    result = await send_sms(
        thread_memory_id=thread_memory_id,
        body=body,
        scope=scope,
        capability_token=capability_token,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        correlation_id=correlation_id,
        capability_token_id=capability_token_id,
    )

    to_prefix = (to_phone_e164 or "")[:6] + "..."
    logger.info(
        "sms_send_new success to=%s thread_memory_id=%s sid=%s",
        to_prefix,
        thread_memory_id,
        result.get("message_sid", ""),
    )

    return {**result, "thread_memory_id": thread_memory_id}


__all__ = [
    "SmsIoError",
    "send_sms",
    "send_sms_new",
    "update_sms_status",
]
