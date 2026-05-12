"""ElevenLabs webhook tool handlers — `/v1/elevenlabs/tools/*` (Pass G).

These are the orchestrator-side HTTP handlers backing the 3 EL workspace
webhook tools created in Pass G:

  POST /v1/elevenlabs/tools/lookup_contact          — resolve caller by phone or email
  POST /v1/elevenlabs/tools/create_appointment_request — propose appointment (Yellow)
  POST /v1/elevenlabs/tools/verify_caller_identity  — returns 501 (OTP not implemented)

Design contract (Law #7 — Tools Are Hands):
  - Each handler executes ONE bounded query and returns a stable JSON envelope.
  - Zero autonomous decisions. All retries, escalation, fallback: orchestrator.
  - No secrets or PII in logs (Law #9).
  - Receipt cut on every state-changing call (Law #2).
  - Tenant scope resolved from `called_number`.
  - Auth: X-Aspire-Webhook-Secret header verified against
    ASPIRE_ELEVENLABS_TOOL_WEBHOOK_SECRET env var (constant-time comparison).
    Missing env var or wrong/absent header → 401 DENY (Law #3 fail-closed).

Receipt types emitted:
  - appointment_proposal_created   (create_appointment_request)
  - caller_identity_verification_initiated  (verify_caller_identity — outcome=degraded)
  - lookup_contact                 (lookup_contact — informational, outcome=success)
  - el_tool_denied                 (auth failures — outcome=denied)
"""

from __future__ import annotations

import hmac
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/elevenlabs/tools", tags=["elevenlabs-tools"])


# ---------------------------------------------------------------------------
# HMAC authentication dependency (P0 #1 — Law #3 fail-closed)
# ---------------------------------------------------------------------------


async def _verify_el_tool_secret(request: Request) -> None:
    """Verify X-Aspire-Webhook-Secret against ASPIRE_ELEVENLABS_TOOL_WEBHOOK_SECRET.

    Fails closed when:
      - env var is unset or empty
      - header is missing
      - header does not match (constant-time compare — no timing oracle)

    Emits an el_tool_denied receipt on every denial so denials are auditable.
    """
    expected = settings.elevenlabs_tool_webhook_secret
    if not expected:
        _cut_deny_receipt("MISSING_TOOL_WEBHOOK_SECRET")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "MISCONFIGURED",
                "message": "ElevenLabs tool webhook secret not configured on this service.",
            },
        )

    provided = request.headers.get("X-Aspire-Webhook-Secret", "")
    if not provided or not hmac.compare_digest(provided, expected):
        _cut_deny_receipt("INVALID_TOOL_WEBHOOK_SECRET")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "UNAUTHORIZED", "message": "Invalid or missing webhook secret"},
        )


def _cut_deny_receipt(reason_code: str) -> None:
    """Emit an immutable denial receipt (Law #2 — receipts for ALL outcomes)."""
    rid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    receipt_store.store_receipts(
        [
            {
                "id": rid,
                "receipt_type": "el_tool_denied",
                "action_type": "el_tool_auth",
                "suite_id": "",
                "office_id": "",
                "tenant_id": "",
                "outcome": "denied",
                "tool_used": "elevenlabs_tools",
                "risk_tier": "green",
                "reason_code": reason_code,
                "trace_id": get_trace_id(),
                "correlation_id": get_correlation_id(),
                "created_at": now,
            }
        ]
    )

_E164 = re.compile(r"^\+\d{7,15}$")
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _redact_phone(phone: str | None) -> str:
    """Truncate phone for logs/receipts (Law #9)."""
    if not phone:
        return ""
    return phone[:6] + "..." if len(phone) > 6 else phone


async def _resolve_tenant_from_called_number(
    called_number: str,
) -> dict[str, str] | None:
    """Look up tenant scope from the called number (mirrors sarah_tools pattern)."""
    if not called_number or not _E164.match(called_number):
        return None
    try:
        rows = await supabase_select(
            "tenant_phone_numbers",
            {"phone_number": called_number, "status": "active"},
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning("el_tools tenant_lookup_failed: %s", exc)
        return None
    if not rows:
        return None
    row = rows[0]
    return {
        "tenant_id": str(row.get("tenant_id", "")),
        "suite_id": str(row.get("suite_id", "")),
        "office_id": str(row.get("office_id", "")),
    }


def _cut_receipt(
    *,
    receipt_type: str,
    scope: dict[str, str],
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
                "suite_id": scope.get("suite_id", ""),
                "office_id": scope.get("office_id", ""),
                "tenant_id": scope.get("tenant_id", ""),
                "outcome": outcome,
                "tool_used": "elevenlabs_tools",
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
# Request models
# ---------------------------------------------------------------------------


class LookupContactRequest(BaseModel):
    phone: str | None = Field(None, description="E.164 phone number to look up")
    email: str | None = Field(
        None,
        description="Email address to look up (basic RFC 5322 validated)",
    )
    called_number: str = Field(..., description="The Aspire number that was called (tenant resolution)")

    def validated_email(self) -> str | None:
        """Return email if it passes basic RFC 5322 regex; None otherwise."""
        if not self.email:
            return None
        if _EMAIL_RE.match(self.email):
            return self.email
        return None


class AppointmentWindow(BaseModel):
    start: str = Field(..., description="ISO 8601 start of proposed window")
    end: str = Field(..., description="ISO 8601 end of proposed window")


class AppointmentContact(BaseModel):
    phone: str = Field(..., description="Caller's E.164 phone number")
    name: str | None = None


class CreateAppointmentRequest(BaseModel):
    window: AppointmentWindow
    intent: str = Field(..., max_length=500, description="What the appointment is for")
    contact: AppointmentContact
    called_number: str = Field(..., description="The Aspire number that was called (tenant resolution)")


class VerifyCallerRequest(BaseModel):
    method: str = Field(..., description="'otp' or 'factual'")
    target: str = Field(..., description="Phone or email to deliver OTP / identity challenge")
    called_number: str = Field(..., description="The Aspire number that was called (tenant resolution)")


# ---------------------------------------------------------------------------
# POST /v1/elevenlabs/tools/lookup_contact
# ---------------------------------------------------------------------------


@router.post("/lookup_contact", dependencies=[Depends(_verify_el_tool_secret)])
async def lookup_contact(body: LookupContactRequest) -> dict[str, Any]:
    """Resolve a caller by phone or email.

    Returns the top matching front_desk_routing_contacts row + recent interaction
    summary. Scope resolved from called_number. Auth: HMAC shared secret.

    Receipt type: lookup_contact (informational, Green)
    """
    if not body.phone and not body.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_INPUT", "message": "phone or email required"},
        )

    scope = await _resolve_tenant_from_called_number(body.called_number) or {}
    suite_id = scope.get("suite_id", "")

    contact: dict[str, Any] | None = None

    if body.phone and _E164.match(body.phone) and suite_id:
        try:
            # P0 #7: dict-form filters — supabase_client URL-encodes each value
            # (no raw string concatenation of user-supplied data).
            rows = await supabase_select(
                "front_desk_routing_contacts",
                {"suite_id": suite_id, "phone": body.phone, "is_active": "true"},
                limit=1,
            )
            if rows:
                contact = rows[0]
        except SupabaseClientError as exc:
            logger.warning("lookup_contact db_error phone=%s: %s", _redact_phone(body.phone), exc)

    validated_email = body.validated_email()
    if not contact and validated_email and suite_id:
        try:
            # P0 #7: dict-form filter for email too
            rows = await supabase_select(
                "front_desk_routing_contacts",
                {"suite_id": suite_id, "email": validated_email, "is_active": "true"},
                limit=1,
            )
            if rows:
                contact = rows[0]
        except SupabaseClientError as exc:
            logger.warning("lookup_contact db_error email: %s", exc)

    # Fetch last 3 SMS threads as quick interaction summary
    last_interactions: list[str] = []
    if contact and suite_id:
        phone_val = contact.get("phone") or body.phone
        if phone_val:
            try:
                sms_rows = await supabase_select(
                    "sms_messages",
                    {"suite_id": suite_id, "from_number": phone_val},
                    order_by="created_at.desc",
                    limit=3,
                )
                for r in (sms_rows or []):
                    preview = (r.get("body") or "")[:80]
                    if preview:
                        last_interactions.append(preview)
            except SupabaseClientError:
                pass

    _cut_receipt(
        receipt_type="lookup_contact",
        scope=scope,
        outcome="success",
        risk_tier="green",
        redacted_inputs={"phone": _redact_phone(body.phone), "has_email": bool(body.email)},
        redacted_outputs={"found": contact is not None},
    )

    if contact:
        return {
            "found": True,
            "contact": {
                "name": contact.get("label") or contact.get("name"),
                "entity": contact.get("role"),
                "phone": contact.get("phone"),
                "email": contact.get("email"),
                "tags": contact.get("tags") or [],
                "last_interaction_summary": "; ".join(last_interactions) or None,
            },
        }
    return {"found": False, "contact": None}


# ---------------------------------------------------------------------------
# POST /v1/elevenlabs/tools/create_appointment_request
# ---------------------------------------------------------------------------


@router.post("/create_appointment_request", dependencies=[Depends(_verify_el_tool_secret)])
async def create_appointment_request(body: CreateAppointmentRequest) -> dict[str, Any]:
    """Create a YELLOW-tier appointment proposal awaiting owner approval.

    Writes to approval_requests table (existing). Returns proposal_id + receipt.
    Receipt type: appointment_proposal_created
    """
    scope = await _resolve_tenant_from_called_number(body.called_number) or {}
    suite_id = scope.get("suite_id", "")

    # Validate window ISO 8601
    try:
        datetime.fromisoformat(body.window.start.replace("Z", "+00:00"))
        datetime.fromisoformat(body.window.end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_INPUT", "message": "window.start/end must be ISO 8601"},
        ) from exc

    proposal_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    approval_row: dict[str, Any] = {
        "id": proposal_id,
        "suite_id": suite_id,
        "office_id": scope.get("office_id", "") or None,
        "action_type": "appointment_request",
        "risk_tier": "yellow",
        "status": "pending",
        "requested_by": "elevenlabs_agent",
        "payload": {
            "window": {"start": body.window.start, "end": body.window.end},
            "intent": body.intent,
            "contact_phone": _redact_phone(body.contact.phone),
            "contact_name": body.contact.name,
        },
        "created_at": now,
        "updated_at": now,
    }

    try:
        await supabase_insert("approval_requests", approval_row)
    except SupabaseClientError as exc:
        logger.warning("appointment_proposal_insert_failed suite_id=%s: %s", suite_id, exc)
        receipt_id = _cut_receipt(
            receipt_type="appointment_proposal_created",
            scope=scope,
            outcome="failed",
            redacted_inputs={
                "phone": _redact_phone(body.contact.phone),
                "intent_len": len(body.intent),
            },
        )
        return {
            "proposed": False,
            "proposal_id": None,
            "awaiting_approval": False,
            "receipt_id": receipt_id,
            "error": "DB write failed",
        }

    receipt_id = _cut_receipt(
        receipt_type="appointment_proposal_created",
        scope=scope,
        outcome="success",
        risk_tier="yellow",
        redacted_inputs={
            "phone": _redact_phone(body.contact.phone),
            "window_start": body.window.start,
            "intent_len": len(body.intent),
        },
        redacted_outputs={"proposal_id": proposal_id},
    )
    logger.info(
        "appointment_proposal_created id=%s suite_id=%s receipt=%s",
        proposal_id, suite_id, receipt_id,
    )
    return {
        "proposed": True,
        "proposal_id": proposal_id,
        "awaiting_approval": True,
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# POST /v1/elevenlabs/tools/verify_caller_identity
# ---------------------------------------------------------------------------


@router.post("/verify_caller_identity", dependencies=[Depends(_verify_el_tool_secret)])
async def verify_caller_identity(body: VerifyCallerRequest) -> dict[str, Any]:
    """Caller identity verification — NOT YET IMPLEMENTED (P0 #2 fix).

    This endpoint previously claimed to initiate OTP verification but never
    actually sent any code, making every receipt a false positive. Per Pass I
    remediation (Option A), it now returns HTTP 501 with outcome=degraded so
    the EL agent knows NOT to proceed with identity-gated actions.

    Agent prompt guidance (Sarah-FrontDesk / Tiffany-FrontDesk):
      When this endpoint returns implemented=false, the agent MUST:
        1. Inform the caller that identity verification is unavailable right now.
        2. Skip any identity-gated actions (e.g. booking, data changes).
        3. Escalate to the owner / leave a callback note.

    Receipt type: caller_identity_verification_initiated (outcome=degraded)
    """
    if body.method not in ("otp", "factual"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_INPUT", "message": "method must be 'otp' or 'factual'"},
        )

    scope = await _resolve_tenant_from_called_number(body.called_number) or {}

    # Cut degraded receipt — NOT success. The ghost OTP previously cut success
    # for a verification that never happened (Law #2 violation). Now we are
    # honest: outcome=degraded, reason_code=OTP_NOT_IMPLEMENTED.
    receipt_id = _cut_receipt(
        receipt_type="caller_identity_verification_initiated",
        scope=scope,
        outcome="degraded",
        risk_tier="yellow",
        redacted_inputs={
            "method": body.method,
            "target_prefix": body.target[:6] + "...",
        },
        redacted_outputs={"implemented": False},
    )
    logger.warning(
        "verify_caller_identity not_implemented method=%s suite_id=%s receipt=%s",
        body.method, scope.get("suite_id", ""), receipt_id,
    )

    # HTTP 501 tells the EL agent this is a known-missing capability,
    # not a transient error. Agent must NOT treat this as a successful verification.
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "method": body.method,
            "implemented": False,
            "receipt_id": receipt_id,
            "instructions": (
                "OTP verification is not yet available. "
                "Do NOT proceed with identity-gated actions. "
                "Inform the caller that verification is unavailable and escalate to the owner."
            ),
        },
    )


# ---------------------------------------------------------------------------
# POST /v1/elevenlabs/tools/get_owner_availability
# ---------------------------------------------------------------------------

_DEFAULT_BUSINESS_HOURS: dict[str, dict[str, Any]] = {
    "mon": {"open": True, "startTime": "09:00", "endTime": "17:00"},
    "tue": {"open": True, "startTime": "09:00", "endTime": "17:00"},
    "wed": {"open": True, "startTime": "09:00", "endTime": "17:00"},
    "thu": {"open": True, "startTime": "09:00", "endTime": "17:00"},
    "fri": {"open": True, "startTime": "09:00", "endTime": "17:00"},
    "sat": {"open": False},
    "sun": {"open": False},
}
_DAY_ABBR = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MORNING_RANGE = (8, 12)
_AFTERNOON_RANGE = (12, 17)
_EVENING_RANGE = (17, 21)


def _parse_window_hint(hint: str, now_local: datetime) -> tuple[datetime, datetime]:
    """Parse a natural-language window_hint into (range_start, range_end) in the local TZ.

    Supported phrases:
      today / tomorrow / this week / next week
      this morning/afternoon/evening / tomorrow morning/afternoon/evening
      next <weekday> / <weekday> (nearest upcoming)

    Falls back to next 5 business days if parsing fails.
    """
    h = hint.lower().strip()
    today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    # Detect time-of-day qualifier
    morning = "morning" in h
    afternoon = "afternoon" in h
    evening = "evening" in h

    def _apply_tod(start: datetime, end: datetime) -> tuple[datetime, datetime]:
        if morning:
            return start.replace(hour=_MORNING_RANGE[0]), start.replace(hour=_MORNING_RANGE[1])
        if afternoon:
            return start.replace(hour=_AFTERNOON_RANGE[0]), start.replace(hour=_AFTERNOON_RANGE[1])
        if evening:
            return start.replace(hour=_EVENING_RANGE[0]), end.replace(hour=_EVENING_RANGE[1])
        return start, end

    if "today" in h:
        return _apply_tod(today, today + timedelta(days=1))

    if "tomorrow" in h:
        tomorrow = today + timedelta(days=1)
        return _apply_tod(tomorrow, tomorrow + timedelta(days=1))

    if "next week" in h:
        days_ahead = 7 - now_local.weekday()  # Monday of next week
        next_mon = today + timedelta(days=days_ahead)
        return next_mon, next_mon + timedelta(days=7)

    if "this week" in h:
        # Remainder of current week (today → next Sunday midnight)
        days_to_sunday = 6 - now_local.weekday()
        week_end = today + timedelta(days=days_to_sunday + 1)
        return _apply_tod(today, week_end)

    # Named weekday: "next Monday", "Friday afternoon", "Monday"
    for idx, full_name in enumerate(_DAY_FULL):
        if full_name.lower() in h:
            current_dow = now_local.weekday()  # 0=Monday
            days_ahead = (idx - current_dow) % 7
            if days_ahead == 0:
                days_ahead = 7  # never "today" by name; go to next occurrence
            target = today + timedelta(days=days_ahead)
            return _apply_tod(target, target + timedelta(days=1))

    # Fallback: next 5 business days
    return today + timedelta(days=1), today + timedelta(days=8)


def _time_str_to_minutes(t: str) -> int:
    """Convert 'HH:MM' to minutes since midnight."""
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _slots_on_day(
    day: datetime,
    biz_config: dict[str, Any],
    events: list[dict[str, Any]],
    duration_minutes: int,
    now_utc: datetime,
    min_lead_hours: float = 1.0,
) -> list[datetime]:
    """Return open slot start-times (naive local) on `day`.

    `day` must be a timezone-aware datetime at midnight in the office TZ.
    `events` are the raw calendar_events rows for the range.
    """
    abbr = _DAY_ABBR[day.weekday()]
    config = biz_config.get(abbr) or biz_config.get(abbr.capitalize()) or {}
    if not config.get("open", False):
        return []

    start_min = _time_str_to_minutes(config.get("startTime", "09:00"))
    end_min = _time_str_to_minutes(config.get("endTime", "17:00"))

    # Collect busy intervals for this day (in local minutes since midnight)
    busy: list[tuple[int, int]] = []
    day_date = day.date()
    for ev in events:
        # is_all_day blocks the entire day
        if ev.get("is_all_day"):
            try:
                ev_start_str = (ev.get("start_time") or "")[:10]  # YYYY-MM-DD
                if ev_start_str and ev_start_str == str(day_date):
                    busy.append((0, 24 * 60))
                    continue
            except Exception:
                pass

        try:
            ev_start = datetime.fromisoformat(
                (ev.get("start_time") or "").replace("Z", "+00:00")
            ).astimezone(day.tzinfo)
            ev_end = datetime.fromisoformat(
                (ev.get("end_time") or "").replace("Z", "+00:00")
            ).astimezone(day.tzinfo)
        except Exception:
            continue

        if ev_start.date() > day_date or ev_end.date() < day_date:
            continue

        busy_start = max(0, ev_start.hour * 60 + ev_start.minute)
        busy_end = min(24 * 60, ev_end.hour * 60 + ev_end.minute)
        busy.append((busy_start, busy_end))

    cutoff_utc = now_utc + timedelta(hours=min_lead_hours)
    slots: list[datetime] = []
    slot_min = start_min
    while slot_min + duration_minutes <= end_min:
        slot_dt = day.replace(hour=slot_min // 60, minute=slot_min % 60, second=0, microsecond=0)
        # Skip past slots and too-soon slots
        if slot_dt.astimezone(timezone.utc) >= cutoff_utc:
            # Check against busy intervals
            slot_end_min = slot_min + duration_minutes
            overlaps = any(bs < slot_end_min and be > slot_min for bs, be in busy)
            if not overlaps:
                slots.append(slot_dt)
        slot_min += 30  # 30-min grid

    return slots


def _build_label(slot_dt: datetime, now_local: datetime) -> str:
    """Generate a human-friendly label like 'Tomorrow 2:00 PM'."""
    today = now_local.date()
    tomorrow = today + timedelta(days=1)
    day_name: str
    if slot_dt.date() == today:
        day_name = "Today"
    elif slot_dt.date() == tomorrow:
        day_name = "Tomorrow"
    else:
        day_name = _DAY_FULL[slot_dt.weekday()]

    # Strip leading zero from hour; strftime %I always zero-pads, e.g. "02:00 PM"
    time_str = slot_dt.strftime("%I:%M %p").lstrip("0") or slot_dt.strftime("%I:%M %p")

    return f"{day_name} {time_str}"


class GetOwnerAvailabilityReq(BaseModel):
    called_number: str = Field(..., description="Tenant resolution — E.164 Aspire number")
    window_hint: str = Field(
        ...,
        description="Natural language time window, e.g. 'tomorrow afternoon', 'this week', 'next Tuesday morning'",
    )
    duration_minutes: int = Field(30, ge=15, le=480, description="Appointment duration in minutes")
    max_slots: int = Field(5, ge=1, le=10, description="Maximum slots to return")


class AvailabilitySlot(BaseModel):
    start_iso: str
    end_iso: str
    label: str
    day_of_week: str
    local_time: str


class GetOwnerAvailabilityResp(BaseModel):
    slots: list[AvailabilitySlot]
    timezone: str
    window_parsed: str
    found_count: int
    note: str | None = None


@router.post(
    "/get_owner_availability",
    dependencies=[Depends(_verify_el_tool_secret)],
    response_model=GetOwnerAvailabilityResp,
)
async def get_owner_availability(body: GetOwnerAvailabilityReq) -> dict[str, Any]:
    """Return open calendar slots from the owner's Aspire calendar.

    Flow:
      1. Resolve tenant scope from called_number.
      2. Load business_hours + timezone from front_desk_configs.
      3. Parse window_hint into a date range.
      4. Query calendar_events for the range (busy intervals).
      5. Generate open 30-min-boundary slots; filter past + too-soon.
      6. Return up to max_slots, evenly distributed.

    Receipt: owner_availability_queried (Green — read-only, no state change).
    """
    scope = await _resolve_tenant_from_called_number(body.called_number) or {}
    suite_id = scope.get("suite_id", "")

    # Step 2: load biz hours + timezone
    biz_hours: dict[str, Any] = _DEFAULT_BUSINESS_HOURS.copy()
    office_tz_str = "America/New_York"

    if suite_id:
        try:
            cfg_rows = await supabase_select(
                "front_desk_configs",
                {"suite_id": suite_id, "is_current": "true"},
                order_by="version_no.desc",
                limit=1,
            )
            if cfg_rows:
                cfg = cfg_rows[0]
                biz_hours = cfg.get("business_hours") or _DEFAULT_BUSINESS_HOURS.copy()
                office_tz_str = cfg.get("timezone") or "America/New_York"
        except SupabaseClientError as exc:
            logger.warning("get_owner_availability config_lookup_failed suite_id=%s: %s", suite_id, exc)

    # Resolve ZoneInfo (fall back to UTC on bad tz string)
    try:
        office_tz = ZoneInfo(office_tz_str)
    except (ZoneInfoNotFoundError, KeyError):
        logger.warning("get_owner_availability invalid_timezone tz=%s — falling back to UTC", office_tz_str)
        office_tz = ZoneInfo("America/New_York")

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(office_tz)

    # Step 3: parse window
    range_start, range_end = _parse_window_hint(body.window_hint, now_local)
    # Ensure tz-aware (they're naive midnight in local tz after parse — attach tz)
    if range_start.tzinfo is None:
        range_start = range_start.replace(tzinfo=office_tz)
    if range_end.tzinfo is None:
        range_end = range_end.replace(tzinfo=office_tz)

    def _fmt_date(dt: datetime) -> str:
        return dt.strftime("%A %b %d").replace(" 0", " ")

    window_parsed = (
        f"{_fmt_date(range_start)} to {_fmt_date(range_end)}"
        if range_start.date() != range_end.date()
        else _fmt_date(range_start)
    )

    # Step 4: query calendar_events
    events: list[dict[str, Any]] = []
    if suite_id:
        try:
            events = await supabase_select(
                "calendar_events",
                f"suite_id=eq.{suite_id}"
                f"&start_time=gte.{range_start.isoformat()}"
                f"&start_time=lt.{range_end.isoformat()}"
                f"&status=in.(pending,confirmed)",
                order_by="start_time.asc",
            ) or []
        except SupabaseClientError as exc:
            logger.warning("get_owner_availability events_lookup_failed suite_id=%s: %s", suite_id, exc)

    # Also capture all-day events (status may be NULL — no status filter covers those)
    if suite_id:
        try:
            all_day_events = await supabase_select(
                "calendar_events",
                f"suite_id=eq.{suite_id}"
                f"&start_time=gte.{range_start.isoformat()}"
                f"&start_time=lt.{range_end.isoformat()}"
                f"&is_all_day=eq.true",
                order_by="start_time.asc",
            ) or []
            # Merge, deduplicating by id
            seen_ids = {e.get("id") for e in events}
            for ev in all_day_events:
                if ev.get("id") not in seen_ids:
                    events.append(ev)
        except SupabaseClientError:
            pass  # Non-fatal — proceed with existing events list

    # Step 5: compute open slots day by day
    all_slots: list[datetime] = []
    current_day = range_start.replace(hour=0, minute=0, second=0, microsecond=0)
    # Ensure tz aware
    if current_day.tzinfo is None:
        current_day = current_day.replace(tzinfo=office_tz)

    while current_day < range_end:
        day_slots = _slots_on_day(
            day=current_day,
            biz_config=biz_hours,
            events=events,
            duration_minutes=body.duration_minutes,
            now_utc=now_utc,
        )
        all_slots.extend(day_slots)
        current_day += timedelta(days=1)

    found_count = len(all_slots)
    note: str | None = None

    # Step 6: select up to max_slots evenly distributed
    selected: list[datetime] = []
    if found_count == 0:
        note = "No open slots found in the stated window."
    elif found_count <= body.max_slots:
        selected = all_slots
    else:
        # Even distribution: take evenly-spaced indices
        step = found_count / body.max_slots
        selected = [all_slots[int(i * step)] for i in range(body.max_slots)]

    # Build response slots
    slots_out: list[dict[str, Any]] = []
    for slot_dt in selected:
        end_dt = slot_dt + timedelta(minutes=body.duration_minutes)
        # Format offset-aware ISO
        start_iso = slot_dt.isoformat()
        end_iso = end_dt.isoformat()
        label = _build_label(slot_dt, now_local)
        local_time = slot_dt.strftime("%I:%M %p").lstrip("0") or slot_dt.strftime("%I:%M %p")

        slots_out.append(
            {
                "start_iso": start_iso,
                "end_iso": end_iso,
                "label": label,
                "day_of_week": _DAY_FULL[slot_dt.weekday()],
                "local_time": local_time,
            }
        )

    receipt_id = _cut_receipt(
        receipt_type="owner_availability_queried",
        scope=scope,
        outcome="success",
        risk_tier="green",
        redacted_inputs={
            "window_hint": body.window_hint,
            "duration_minutes": body.duration_minutes,
            "max_slots": body.max_slots,
        },
        redacted_outputs={
            "slots_count": len(slots_out),
            "timezone": office_tz_str,
        },
    )
    logger.info(
        "get_owner_availability suite_id=%s slots=%d window=%s receipt=%s",
        suite_id, len(slots_out), body.window_hint[:40], receipt_id,
    )

    return {
        "slots": slots_out,
        "timezone": office_tz_str,
        "window_parsed": window_parsed,
        "found_count": found_count,
        "note": note,
    }
