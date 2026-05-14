"""Voicemail Notifier — email + optional SMS after a voicemail is written (Wave 5/6).

Fires immediately after voicemail_writer.write_voicemail() returns.

Email path (always):
  Uses execute_polaris_email_send() from providers/polaris_email_client.py.
  The Domain Rail S2S HMAC signing is handled inside that function.
  Fail-closed: if Domain Rail is unreachable or returns an error we cut a denied
  receipt and raise HTTPException(503). We do NOT silently skip — the owner must
  receive their voicemail notification or the pipeline must visibly fail.

SMS path (conditional):
  Only fires when voicemail_data['urgency'] == 'high' AND the suite has a
  configured routing_owner_phone in office_profiles.
  Uses send_sms() from services/sms_io.py.
  A2P gate is enforced inside send_sms() — if the tenant is not registered we cut
  a denied receipt and continue (SMS is best-effort for high-urgency; email is the
  primary channel).

Rate limiting (in-memory token bucket):
  High-urgency SMS is rate-limited to 1 per 15 minutes per owner phone number.
  This is an in-memory bucket, which is safe for V1 (single-instance Railway).
  For multi-instance deployments this should be moved to Redis — see NOTE below.

NOTE on multi-instance Redis migration:
  The `_SmsRateLimiter` class uses a module-level dict. With Railway horizontal
  scaling (multiple orchestrator pods) each pod has its own dict, so the 15-minute
  window is per-pod rather than per-owner globally. For V1 (single Railway service)
  this is acceptable. When scaling to multiple instances, replace `_SmsRateLimiter`
  with a Redis `SET NX PX` lock on key `sms_rate:{owner_phone}` with TTL=900s.

Email template:
  Branded HTML + plaintext. Variables: caller_name, callback_number, call_reason,
  urgency_badge, summary, recording_link, transcript_preview (first 300 chars).
  DLP: the FROM address in Polaris receipts is domain-only (Law #9 is enforced
  inside execute_polaris_email_send — we don't need to redact here, but we also
  don't log raw email addresses in our own log lines).

Law compliance:
  Law #2 — Yellow receipts: voicemail_emailed, voicemail_sms_sent.
  Law #3 — Missing Polaris secret (domain_rail_url not configured) → deny + 503.
            Missing routing_owner_phone → skip SMS silently (not a failure).
            A2P gate inside send_sms → SmsIoError caught, denied receipt, continue.
  Law #6 — suite_id from resolved EL scope, never from payload.
  Law #9 — owner email address not logged; callback_number in receipts as prefix only.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from fastapi import HTTPException

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_send
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.sms_io import SmsIoError, send_sms
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
)
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

logger = logging.getLogger(__name__)

_TOOL_NAME = "post_call_enrichment"
_RISK_TIER = "yellow"

# Sender address for Aspire platform notifications (not tenant-specific)
_NOTIFICATION_FROM = "noreply@aspireos.app"

# High-urgency SMS rate limit: 1 per 15 minutes per owner phone
_SMS_RATE_LIMIT_SECONDS = 900


# ---------------------------------------------------------------------------
# In-memory SMS rate limiter (V1 — single instance)
# See module docstring NOTE for multi-instance Redis migration path.
# ---------------------------------------------------------------------------


class _SmsRateLimiter:
    """Thread-safe per-phone-number token bucket (1 token per window)."""

    def __init__(self, window_seconds: int = _SMS_RATE_LIMIT_SECONDS) -> None:
        self._window = window_seconds
        self._last_sent: dict[str, float] = {}
        self._lock = Lock()

    def allow(self, phone: str) -> bool:
        """Return True if an SMS to this phone is allowed now, and record the send."""
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(phone, 0.0)
            if now - last >= self._window:
                self._last_sent[phone] = now
                return True
        return False


_sms_rate_limiter = _SmsRateLimiter()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _phone_prefix(phone: str) -> str:
    """First-6-digit mask for PII-safe log lines (Law #9)."""
    digits = "".join(c for c in phone if c.isdigit() or c == "+")
    return digits[:6] + "..." if len(digits) >= 6 else digits + "..."


def _urgency_badge_html(urgency: str) -> str:
    """Render a coloured urgency badge for the email HTML body."""
    colours = {
        "high": ("#dc2626", "HIGH PRIORITY"),
        "medium": ("#d97706", "MEDIUM PRIORITY"),
        "low": ("#16a34a", "LOW PRIORITY"),
    }
    colour, label = colours.get(urgency.lower(), ("#6b7280", urgency.upper()))
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:4px;'
        f'background:{colour};color:#fff;font-size:11px;font-weight:700;'
        f'letter-spacing:0.05em;">{label}</span>'
    )


def _build_email_html(
    *,
    caller_name: str,
    callback_number: str,
    call_reason: str,
    urgency: str,
    call_summary: str,
    recording_uri: str,
    transcript_preview: str,
    business_name: str,
) -> str:
    """Render the branded HTML voicemail notification email body.

    Uses inline styles for maximum email-client compatibility.
    The recording link is only rendered when recording_uri is non-empty.
    """
    recording_block = ""
    if recording_uri:
        recording_block = (
            f'<p style="margin:12px 0;">'
            f'<a href="{recording_uri}" style="color:#4f46e5;text-decoration:none;'
            f'font-weight:600;">▶ Listen to Recording</a></p>'
        )

    transcript_block = ""
    if transcript_preview:
        safe_preview = transcript_preview.replace("<", "&lt;").replace(">", "&gt;")
        transcript_block = (
            f'<div style="margin-top:16px;padding:12px;background:#f9fafb;'
            f'border-left:3px solid #e5e7eb;border-radius:0 4px 4px 0;">'
            f'<p style="margin:0 0 4px;font-size:11px;color:#6b7280;'
            f'text-transform:uppercase;letter-spacing:0.05em;">Transcript Preview</p>'
            f'<p style="margin:0;font-size:13px;color:#374151;">{safe_preview}…</p>'
            f'</div>'
        )

    urgency_badge = _urgency_badge_html(urgency)

    safe_caller = (caller_name or "Unknown Caller").replace("<", "&lt;").replace(">", "&gt;")
    safe_callback = (callback_number or "Not provided").replace("<", "&lt;").replace(">", "&gt;")
    safe_reason = (call_reason or "Not specified").replace("<", "&lt;").replace(">", "&gt;")
    safe_summary = (call_summary or "").replace("<", "&lt;").replace(">", "&gt;")
    safe_business = (business_name or "your business").replace("<", "&lt;").replace(">", "&gt;")

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>New Voicemail — Aspire</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:32px 16px;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);">
      <!-- Header -->
      <tr><td style="background:#4f46e5;padding:24px 32px;">
        <p style="margin:0;font-size:22px;font-weight:700;color:#fff;">Aspire</p>
        <p style="margin:4px 0 0;font-size:13px;color:#c7d2fe;">New voicemail for {safe_business}</p>
      </td></tr>
      <!-- Body -->
      <tr><td style="padding:32px;">
        <p style="margin:0 0 4px;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Priority</p>
        <p style="margin:0 0 20px;">{urgency_badge}</p>

        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <td style="padding:10px 0;border-bottom:1px solid #f3f4f6;width:140px;">
              <span style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Caller</span>
            </td>
            <td style="padding:10px 0;border-bottom:1px solid #f3f4f6;">
              <span style="font-size:14px;font-weight:600;color:#111827;">{safe_caller}</span>
            </td>
          </tr>
          <tr>
            <td style="padding:10px 0;border-bottom:1px solid #f3f4f6;">
              <span style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Callback</span>
            </td>
            <td style="padding:10px 0;border-bottom:1px solid #f3f4f6;">
              <span style="font-size:14px;color:#374151;">{safe_callback}</span>
            </td>
          </tr>
          <tr>
            <td style="padding:10px 0;border-bottom:1px solid #f3f4f6;">
              <span style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Reason</span>
            </td>
            <td style="padding:10px 0;border-bottom:1px solid #f3f4f6;">
              <span style="font-size:14px;color:#374151;">{safe_reason}</span>
            </td>
          </tr>
        </table>

        {f'<p style="margin:20px 0 8px;font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Summary</p><p style="margin:0;font-size:14px;color:#374151;line-height:1.6;">{safe_summary}</p>' if safe_summary else ''}

        {recording_block}
        {transcript_block}

        <p style="margin:28px 0 0;font-size:12px;color:#9ca3af;border-top:1px solid #f3f4f6;padding-top:16px;">
          Sent by Aspire &middot; Tiffany answered this call on your behalf.
          <br>Manage your voicemail inbox in the Aspire desktop app.
        </p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""


def _build_email_text(
    *,
    caller_name: str,
    callback_number: str,
    call_reason: str,
    urgency: str,
    call_summary: str,
    recording_uri: str,
    transcript_preview: str,
) -> str:
    """Plain-text fallback for the voicemail notification email."""
    lines = [
        "NEW VOICEMAIL — Aspire",
        "=" * 40,
        f"Priority : {urgency.upper()}",
        f"Caller   : {caller_name or 'Unknown'}",
        f"Callback : {callback_number or 'Not provided'}",
        f"Reason   : {call_reason or 'Not specified'}",
    ]
    if call_summary:
        lines += ["", "Summary:", call_summary]
    if recording_uri:
        lines += ["", f"Recording: {recording_uri}"]
    if transcript_preview:
        lines += ["", "Transcript preview:", transcript_preview + "..."]
    lines += [
        "",
        "---",
        "Sent by Aspire. Tiffany answered this call on your behalf.",
        "Manage your voicemail inbox in the Aspire desktop app.",
    ]
    return "\n".join(lines)


def _build_receipt(
    *,
    receipt_id: str,
    suite_id: str,
    tenant_id: str,
    office_id: str,
    receipt_type: str,
    outcome: str,
    reason_code: str,
    voicemail_id: str,
    trace_id: str,
    correlation_id: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Yellow-tier notification receipt (Law #2)."""
    return {
        "id": receipt_id,
        "receipt_type": receipt_type,
        "suite_id": suite_id,
        "tenant_id": tenant_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": f"service.{_TOOL_NAME}",
        "action_type": f"notification.{receipt_type}",
        "tool_used": _TOOL_NAME,
        "risk_tier": _RISK_TIER,
        "outcome": outcome,
        "reason_code": reason_code,
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "redacted_inputs": {
            "voicemail_id": voicemail_id,
            # owner email/phone not included — PII, Law #9
            **(extra or {}),
        },
        "redacted_outputs": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def _fetch_owner_contact_info(
    suite_id: str,
    office_id: str,
) -> tuple[str, str, str]:
    """Fetch (voicemail_email, routing_owner_phone, business_name) from office_profiles.

    Returns empty strings for missing fields — callers check before using.
    Fail-closed on voicemail_email (required for email send).
    """
    try:
        rows = await supabase_select(
            "office_profiles",
            {"suite_id": suite_id, "office_id": office_id},
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error(
            "voicemail_notifier office_profiles_fetch_failed suite_id=%s error=%s",
            suite_id,
            exc.detail,
        )
        return "", "", ""

    if not rows:
        logger.warning(
            "voicemail_notifier no_office_profile suite_id=%s office_id=%s",
            suite_id,
            office_id,
        )
        return "", "", ""

    row = rows[0]
    email = str(row.get("voicemail_email") or row.get("email") or "").strip()
    phone = str(row.get("routing_owner_phone") or row.get("owner_phone") or "").strip()
    business_name = str(row.get("business_name") or row.get("name") or "").strip()
    return email, phone, business_name


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def notify_owner(
    *,
    suite_id: str,
    tenant_id: str,
    office_id: str,
    voicemail_id: str,
    voicemail_data: dict[str, Any],
    trace_id: str = "",
    correlation_id: str = "",
) -> None:
    """Surface a new voicemail in the desktop app's homepage notification dropdown.

    Architecture (per user-confirmed v1 design):
      - PRIMARY surface: Calls page Voicemails tab reads `frontdesk_voicemails`
        directly. The voicemail row was written by voicemail_writer.py before
        this function runs.
      - NOTIFICATION surface: a row in `inbox_items` with type='VOICEMAIL'
        appears instantly in the homepage notification dropdown via the
        existing useRealtimeInbox hook on the desktop app. Tap → navigates
        to Calls page Voicemails tab.

    NOT used in v1 (per user direction): email push, SMS push. Both are
    out of scope; the desktop app is the only surface. Email/SMS can be
    re-introduced as opt-in tenant settings later.

    Fail-closed: if the inbox_items insert fails this function raises
    HTTPException(503). The voicemail row in `frontdesk_voicemails` remains
    durable — the Calls page will still show it — but the webhook returns
    503 so ElevenLabs can retry delivery and reissue the inbox notification.
    """
    from aspire_orchestrator.services.supabase_client import supabase_insert

    # --- Extract voicemail fields --------------------------------------------
    caller_name = str(voicemail_data.get("caller_name") or "Unknown caller").strip()
    callback_number = str(voicemail_data.get("callback_number") or "").strip()
    call_reason = str(voicemail_data.get("call_reason") or "").strip()
    urgency_raw = str(voicemail_data.get("urgency") or "medium").strip().lower()
    urgency = urgency_raw if urgency_raw in {"low", "medium", "high"} else "medium"
    call_summary = str(voicemail_data.get("call_summary") or "").strip()
    recording_uri = str(voicemail_data.get("recording_uri") or "").strip()

    # Inbox preview: prefer the call_summary; fall back to call_reason.
    preview = call_summary or call_reason or "New voicemail"
    if len(preview) > 280:
        preview = preview[:277] + "..."

    title = f"New voicemail from {caller_name}"

    inbox_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    inbox_row: dict[str, Any] = {
        "id": inbox_id,
        "suite_id": suite_id,
        "tenant_id": tenant_id,
        "office_id": office_id,
        "type": "VOICEMAIL",
        "title": title,
        "preview": preview,
        "priority": urgency,
        "status": "NEW",
        "unread": True,
        "metadata": {
            "voicemail_id": voicemail_id,
            "callback_number": callback_number,
            "call_reason": call_reason,
            "recording_uri": recording_uri,
            "caller_name": caller_name,
            "deeplink": f"aspire://calls-messages?tab=voicemails&voicemail_id={voicemail_id}",
        },
        "created_at": now,
        "updated_at": now,
        "trace_id": trace_id or None,
    }

    receipt_id = str(uuid.uuid4())

    try:
        await supabase_insert("inbox_items", inbox_row)
    except Exception as exc:  # noqa: BLE001
        receipt_store.store_receipts_strict([
            _build_receipt(
                receipt_id=receipt_id,
                suite_id=suite_id,
                tenant_id=tenant_id,
                office_id=office_id,
                receipt_type="voicemail_inbox_notified",
                outcome="failed",
                reason_code="INBOX_INSERT_FAILED",
                voicemail_id=voicemail_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
        ])
        logger.error(
            "voicemail_notifier inbox_insert_failed voicemail_id=%s err=%s",
            voicemail_id,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail=f"voicemail_notifier: inbox_items insert failed: {exc}",
        ) from exc

    receipt_store.store_receipts_strict([
        _build_receipt(
            receipt_id=receipt_id,
            suite_id=suite_id,
            tenant_id=tenant_id,
            office_id=office_id,
            receipt_type="voicemail_inbox_notified",
            outcome="success",
            reason_code="EXECUTED",
            voicemail_id=voicemail_id,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
    ])
    logger.info(
        "voicemail_notifier inbox_notified voicemail_id=%s urgency=%s",
        voicemail_id,
        urgency,
    )
    return

# ---------------------------------------------------------------------------
# Legacy email + SMS path retained below for opt-in re-enablement (off in v1).
# ---------------------------------------------------------------------------
async def _notify_owner_email_legacy(
    *,
    suite_id: str,
    tenant_id: str,
    office_id: str,
    voicemail_id: str,
    voicemail_data: dict[str, Any],
    trace_id: str = "",
    correlation_id: str = "",
) -> None:
    """Legacy email+SMS notifier — kept dormant for future opt-in tenant setting.
    Not invoked in v1; the active path is notify_owner() above writing to inbox_items.
    """
    # --- Validate Domain Rail is configured (fail-closed, Law #3) ------------
    if not settings.domain_rail_url or not settings.s2s_hmac_secret or settings.s2s_hmac_secret == "UNCONFIGURED-FAIL-CLOSED":
        denied_id = str(uuid.uuid4())
        receipt_store.store_receipts_strict([
            _build_receipt(
                receipt_id=denied_id,
                suite_id=suite_id,
                tenant_id=tenant_id,
                office_id=office_id,
                receipt_type="voicemail_emailed",
                outcome="denied",
                reason_code="DOMAIN_RAIL_NOT_CONFIGURED",
                voicemail_id=voicemail_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
        ])
        raise HTTPException(
            status_code=503,
            detail=(
                "voicemail_notifier: Domain Rail (Polaris email) is not configured. "
                "Set ASPIRE_DOMAIN_RAIL_URL and ASPIRE_S2S_HMAC_SECRET. "
                "Fail-closed per Law #3."
            ),
        )

    # --- Fetch owner contact info --------------------------------------------
    owner_email, owner_phone, business_name = await _fetch_owner_contact_info(
        suite_id, office_id
    )

    if not owner_email:
        denied_id = str(uuid.uuid4())
        receipt_store.store_receipts_strict([
            _build_receipt(
                receipt_id=denied_id,
                suite_id=suite_id,
                tenant_id=tenant_id,
                office_id=office_id,
                receipt_type="voicemail_emailed",
                outcome="denied",
                reason_code="OWNER_EMAIL_NOT_CONFIGURED",
                voicemail_id=voicemail_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
        ])
        raise HTTPException(
            status_code=503,
            detail=(
                "voicemail_notifier: no voicemail_email configured for "
                f"suite_id={suite_id} office_id={office_id}. "
                "Set voicemail_email in office_profiles. Fail-closed per Law #3."
            ),
        )

    # --- Extract voicemail fields --------------------------------------------
    caller_name = str(voicemail_data.get("caller_name") or "Unknown Caller").strip()
    callback_number = str(voicemail_data.get("callback_number") or "").strip()
    call_reason = str(voicemail_data.get("call_reason") or "").strip()
    urgency = str(voicemail_data.get("urgency") or "medium").strip().lower()
    call_summary = str(voicemail_data.get("call_summary") or "").strip()
    recording_uri = str(voicemail_data.get("recording_uri") or "").strip()
    transcript_text = str(voicemail_data.get("transcript_text") or "").strip()
    transcript_preview = transcript_text[:300] if transcript_text else ""

    # --- Send email via Polaris ----------------------------------------------
    email_correlation_id = f"{correlation_id}:email"
    body_html = _build_email_html(
        caller_name=caller_name,
        callback_number=callback_number,
        call_reason=call_reason,
        urgency=urgency,
        call_summary=call_summary,
        recording_uri=recording_uri,
        transcript_preview=transcript_preview,
        business_name=business_name,
    )
    body_text = _build_email_text(
        caller_name=caller_name,
        callback_number=callback_number,
        call_reason=call_reason,
        urgency=urgency,
        call_summary=call_summary,
        recording_uri=recording_uri,
        transcript_preview=transcript_preview,
    )
    urgency_label = urgency.upper()
    email_subject = f"[{urgency_label}] New voicemail from {caller_name}"

    email_result = await execute_polaris_email_send(
        payload={
            "from_address": _NOTIFICATION_FROM,
            "to": owner_email,
            "subject": email_subject,
            "body_html": body_html,
            "body_text": body_text,
        },
        correlation_id=email_correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        risk_tier=_RISK_TIER,
    )

    email_receipt_id = str(uuid.uuid4())

    if email_result.outcome.value != "success":
        # Email failed — cut denied receipt and raise (fail-closed, Law #3)
        receipt_store.store_receipts_strict([
            _build_receipt(
                receipt_id=email_receipt_id,
                suite_id=suite_id,
                tenant_id=tenant_id,
                office_id=office_id,
                receipt_type="voicemail_emailed",
                outcome="failed",
                reason_code=getattr(email_result, "error", None) or "POLARIS_SEND_FAILED",
                voicemail_id=voicemail_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
        ])
        raise HTTPException(
            status_code=503,
            detail=(
                f"voicemail_notifier: Polaris email send failed: {email_result.error}. "
                "Fail-closed per Law #3."
            ),
        )

    # Email succeeded — cut Yellow receipt (voicemail_emailed)
    receipt_store.store_receipts_strict([
        _build_receipt(
            receipt_id=email_receipt_id,
            suite_id=suite_id,
            tenant_id=tenant_id,
            office_id=office_id,
            receipt_type="voicemail_emailed",
            outcome="success",
            reason_code="EXECUTED",
            voicemail_id=voicemail_id,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
    ])
    logger.info(
        "voicemail_notifier email_sent voicemail_id=%s urgency=%s",
        voicemail_id,
        urgency,
    )

    # --- Conditional high-urgency SMS ----------------------------------------
    # Requirements: urgency=='high' AND tenant has routing_owner_phone configured
    # AND rate limiter allows (1 per 15 min per owner phone).
    if urgency != "high" or not owner_phone:
        return  # SMS not applicable — return successfully

    owner_phone_prefix = _phone_prefix(owner_phone)

    if not _sms_rate_limiter.allow(owner_phone):
        logger.info(
            "voicemail_notifier sms_rate_limited owner_prefix=%s voicemail_id=%s",
            owner_phone_prefix,
            voicemail_id,
        )
        return  # Rate-limited — email already delivered, not a failure

    # Build a minimal SMS body (160 chars fits one segment)
    sms_body = (
        f"URGENT voicemail — {caller_name}: "
        f"{call_reason or call_summary or 'message left'}. "
        f"Callback: {callback_number or 'see app'}."
    )
    if len(sms_body) > 160:
        sms_body = sms_body[:157] + "..."

    sms_idempotency_key = f"voicemail-sms:{voicemail_id}"
    sms_receipt_id = str(uuid.uuid4())

    # send_sms() needs a thread_memory_id to resolve to_number from memory_objects.
    # For voicemail notifications the recipient IS the owner phone — a known direct
    # number, not a thread. sms_io.send_sms() resolves to_number from a thread row,
    # which doesn't apply here. We therefore use a lightweight direct Twilio POST
    # bypassing send_sms()'s thread-resolution logic. We reuse the auth + circuit-
    # breaker helpers from sms_io but call Twilio directly.
    #
    # Rationale for NOT calling send_sms(): send_sms() requires a thread_memory_id
    # and uses it to resolve the to_number. The owner notification has no thread —
    # we know the to_number directly (routing_owner_phone). Calling send_sms() with a
    # fake thread_memory_id would create a spurious memory_object and pollute the
    # SMS thread view. The correct path is a direct owner-notify POST.
    try:
        import httpx
        from aspire_orchestrator.services.sms_io import (
            _TIMEOUT_SECONDS,
            _TWILIO_BASE,
            _resolve_sms_from_row,
            _twilio_auth,
        )
        from aspire_orchestrator.services.supabase_client import supabase_select

        account_sid, auth_token = _twilio_auth()

        # Resolve the tenant's from_number for this office
        from_rows = await supabase_select(
            "tenant_phone_numbers",
            f"office_id=eq.{office_id}&sms_enabled=eq.true&status=eq.active",
            order_by="purchased_at.desc",
            limit=10,
        )
        if not from_rows:
            logger.warning(
                "voicemail_notifier no_sms_from_number office_id=%s — skipping SMS",
                office_id,
            )
            return

        try:
            from_number = _resolve_sms_from_row(from_rows, office_id)
        except Exception as exc:
            logger.warning(
                "voicemail_notifier invalid_sms_from_number office_id=%s err=%s",
                office_id,
                exc,
            )
            return
        url = f"{_TWILIO_BASE}/Accounts/{account_sid}/Messages.json"
        twilio_payload = {
            "From": from_number,
            "To": owner_phone,
            "Body": sms_body,
        }

        async with httpx.AsyncClient(
            auth=(account_sid, auth_token),
            timeout=_TIMEOUT_SECONDS,
        ) as client:
            resp = await client.post(
                url,
                data=twilio_payload,
                headers={"Idempotency-Key": sms_idempotency_key},
            )

        if resp.status_code >= 400:
            err_detail = ""
            try:
                err_detail = resp.json().get("message", f"HTTP {resp.status_code}")
            except Exception:
                err_detail = f"HTTP {resp.status_code}"
            logger.warning(
                "voicemail_notifier sms_send_failed owner_prefix=%s error=%s",
                owner_phone_prefix,
                err_detail,
            )
            receipt_store.store_receipts([
                _build_receipt(
                    receipt_id=sms_receipt_id,
                    suite_id=suite_id,
                    tenant_id=tenant_id,
                    office_id=office_id,
                    receipt_type="voicemail_sms_sent",
                    outcome="failed",
                    reason_code="TWILIO_SEND_FAILED",
                    voicemail_id=voicemail_id,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                    extra={"owner_phone_prefix": owner_phone_prefix},
                )
            ])
            return  # SMS failure is non-fatal — email already delivered

        # SMS success — cut Yellow receipt (voicemail_sms_sent)
        receipt_store.store_receipts_strict([
            _build_receipt(
                receipt_id=sms_receipt_id,
                suite_id=suite_id,
                tenant_id=tenant_id,
                office_id=office_id,
                receipt_type="voicemail_sms_sent",
                outcome="success",
                reason_code="EXECUTED",
                voicemail_id=voicemail_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
                extra={"owner_phone_prefix": owner_phone_prefix},
            )
        ])
        logger.info(
            "voicemail_notifier sms_sent owner_prefix=%s voicemail_id=%s",
            owner_phone_prefix,
            voicemail_id,
        )

    except SmsIoError as exc:
        # A2P gate or credential error — best-effort, continue after receipt
        logger.warning(
            "voicemail_notifier sms_blocked owner_prefix=%s code=%s",
            owner_phone_prefix,
            exc.code,
        )
        receipt_store.store_receipts([
            _build_receipt(
                receipt_id=sms_receipt_id,
                suite_id=suite_id,
                tenant_id=tenant_id,
                office_id=office_id,
                receipt_type="voicemail_sms_sent",
                outcome="denied",
                reason_code=exc.code,
                voicemail_id=voicemail_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
                extra={"owner_phone_prefix": owner_phone_prefix},
            )
        ])

    except Exception as exc:
        # Unexpected SMS error — log, cut receipt, continue (non-fatal)
        logger.error(
            "voicemail_notifier sms_unexpected_error owner_prefix=%s error=%s",
            owner_phone_prefix,
            exc,
        )
        receipt_store.store_receipts([
            _build_receipt(
                receipt_id=sms_receipt_id,
                suite_id=suite_id,
                tenant_id=tenant_id,
                office_id=office_id,
                receipt_type="voicemail_sms_sent",
                outcome="failed",
                reason_code="UNEXPECTED_ERROR",
                voicemail_id=voicemail_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
        ])


__all__ = ["notify_owner"]
