"""Tests for ElevenLabs webhook tool handlers — get_owner_availability + hardened
request_callback_window.

Coverage:
  test_get_owner_availability_returns_slots
  test_get_owner_availability_respects_business_hours_closed
  test_get_owner_availability_filters_past_slots
  test_get_owner_availability_hmac_required
  test_request_callback_window_slot_conflict
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from aspire_orchestrator.config.settings import settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = str(uuid.uuid4())
SUITE_ID = str(uuid.uuid4())
OFFICE_ID = str(uuid.uuid4())
CALLED_NUMBER = "+14482885386"
FAKE_SCOPE = {"tenant_id": TENANT_ID, "suite_id": SUITE_ID, "office_id": OFFICE_ID}
VALID_SECRET = "test-hmac-secret-12345"

_BUSINESS_HOURS_MON_FRI = {
    "mon": {"open": True, "startTime": "09:00", "endTime": "17:00"},
    "tue": {"open": True, "startTime": "09:00", "endTime": "17:00"},
    "wed": {"open": True, "startTime": "09:00", "endTime": "17:00"},
    "thu": {"open": True, "startTime": "09:00", "endTime": "17:00"},
    "fri": {"open": True, "startTime": "09:00", "endTime": "17:00"},
    "sat": {"open": False},
    "sun": {"open": False},
}


def _auth_headers() -> dict[str, str]:
    return {"X-Aspire-Webhook-Secret": VALID_SECRET}


# ---------------------------------------------------------------------------
# Helper to build fake front_desk_configs rows
# ---------------------------------------------------------------------------


def _fake_config(biz_hours: dict | None = None, tz: str = "America/New_York") -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "suite_id": SUITE_ID,
        "is_current": True,
        "business_hours": biz_hours or _BUSINESS_HOURS_MON_FRI,
        "timezone": tz,
    }


# ---------------------------------------------------------------------------
# 1. test_get_owner_availability_returns_slots
#    Stub a calendar with 1 event tomorrow 10am-11am; expect 10am slot absent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_owner_availability_returns_slots():
    """Happy path: slots returned, 10am block excluded."""
    from aspire_orchestrator.routes.elevenlabs_tools import get_owner_availability, GetOwnerAvailabilityReq
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    tomorrow = (now_local + timedelta(days=1)).date()

    # Build a calendar event tomorrow 10am–11am in NYC TZ
    ev_start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 10, 0, tzinfo=tz)
    ev_end = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 11, 0, tzinfo=tz)

    fake_event = {
        "id": str(uuid.uuid4()),
        "suite_id": SUITE_ID,
        "start_time": ev_start.isoformat(),
        "end_time": ev_end.isoformat(),
        "is_all_day": False,
        "status": "confirmed",
    }

    async def mock_select(table, filters, *, order_by=None, limit=None):
        if table == "front_desk_configs":
            return [_fake_config()]
        if table == "calendar_events":
            return [fake_event]
        if table == "tenant_phone_numbers":
            return [
                {
                    "tenant_id": TENANT_ID,
                    "suite_id": SUITE_ID,
                    "office_id": OFFICE_ID,
                    "phone_number": CALLED_NUMBER,
                    "status": "active",
                }
            ]
        return []

    req = GetOwnerAvailabilityReq(
        called_number=CALLED_NUMBER,
        window_hint="tomorrow",
        duration_minutes=30,
        max_slots=5,
    )

    with (
        patch("aspire_orchestrator.routes.elevenlabs_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.elevenlabs_tools._cut_receipt", return_value=str(uuid.uuid4())),
        patch("aspire_orchestrator.routes.elevenlabs_tools.receipt_store.store_receipts"),
    ):
        resp = await get_owner_availability(req)

    assert resp["found_count"] > 0, "Expected at least one open slot tomorrow"
    slot_starts = [s["start_iso"] for s in resp["slots"]]

    # 10am slot must be excluded
    for start in slot_starts:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(tz)
        assert not (dt.hour == 10 and dt.minute == 0 and dt.date() == tomorrow), (
            "10:00 AM slot should be blocked by the calendar event"
        )
    # 10:30 may also be blocked (event ends 11am, 10:30 slot runs to 11am — no overlap)
    # 11:00 should be available
    eleven_am_starts = [
        s for s in slot_starts
        if datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(tz).hour == 11
        and datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(tz).date() == tomorrow
    ]
    assert len(eleven_am_starts) >= 1, "11:00 AM slot should be open"
    assert resp["timezone"] == "America/New_York"


# ---------------------------------------------------------------------------
# 2. test_get_owner_availability_respects_business_hours_closed
#    Saturday closed → no Saturday slots when window_hint is "this week"
#    (if today is Friday this might need a special handle; we force Sunday in mock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_owner_availability_respects_business_hours_closed():
    """Closed days produce zero slots."""
    from aspire_orchestrator.routes.elevenlabs_tools import (
        GetOwnerAvailabilityReq,
        _slots_on_day,
    )
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    now_utc = datetime.now(timezone.utc)
    # Find next Saturday
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    days_to_sat = (5 - today.weekday()) % 7
    if days_to_sat == 0:
        days_to_sat = 7
    saturday = today + timedelta(days=days_to_sat)

    slots = _slots_on_day(
        day=saturday,
        biz_config=_BUSINESS_HOURS_MON_FRI,
        events=[],
        duration_minutes=30,
        now_utc=now_utc,
    )

    assert slots == [], f"Saturday should produce no slots, got {slots}"


# ---------------------------------------------------------------------------
# 3. test_get_owner_availability_filters_past_slots
#    Slots within the next 1 hour must be excluded.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_owner_availability_filters_past_slots():
    """Slots less than 1 hour from now are excluded."""
    from aspire_orchestrator.routes.elevenlabs_tools import _slots_on_day
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    # Run for today; all slots within 1h of now_utc should be gone
    today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    slots = _slots_on_day(
        day=today,
        biz_config=_BUSINESS_HOURS_MON_FRI,
        events=[],
        duration_minutes=30,
        now_utc=now_utc,
        min_lead_hours=1.0,
    )

    cutoff = now_utc + timedelta(hours=1)
    for slot_dt in slots:
        slot_utc = slot_dt.astimezone(timezone.utc)
        assert slot_utc >= cutoff, (
            f"Slot {slot_dt} is within the 1-hour exclusion window (cutoff {cutoff})"
        )


# ---------------------------------------------------------------------------
# 4. test_get_owner_availability_hmac_required
#    The _verify_el_tool_secret dependency raises 401 when secret is absent/wrong.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_owner_availability_hmac_required():
    """_verify_el_tool_secret raises HTTP 401 when header is absent."""
    from fastapi import HTTPException
    from starlette.requests import Request
    from aspire_orchestrator.routes.elevenlabs_tools import _verify_el_tool_secret

    original_secret = settings.elevenlabs_tool_webhook_secret
    settings.elevenlabs_tool_webhook_secret = VALID_SECRET
    try:
        # Build a minimal mock Request with no X-Aspire-Webhook-Secret header
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/elevenlabs/tools/get_owner_availability",
            "headers": [],  # empty — no HMAC header
            "query_string": b"",
        }
        request = Request(scope)

        with pytest.raises(HTTPException) as exc_info:
            await _verify_el_tool_secret(request)

        assert exc_info.value.status_code == 401
    finally:
        settings.elevenlabs_tool_webhook_secret = original_secret


# ---------------------------------------------------------------------------
# 5. test_request_callback_window_slot_conflict
#    Simulate race: slot taken between offer and accept.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_callback_window_slot_conflict():
    """When a calendar event overlaps the selected_slot_iso, return slot_conflict + alternatives."""
    from aspire_orchestrator.routes.sarah_tools import callback_request, CallbackRequestReq
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    slot_start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 14, 0, tzinfo=tz)
    slot_end = slot_start + timedelta(minutes=30)

    # Simulate a conflicting event covering the 2pm slot
    conflicting_event = {
        "id": str(uuid.uuid4()),
        "suite_id": SUITE_ID,
        "start_time": slot_start.isoformat(),
        "end_time": slot_end.isoformat(),
        "is_all_day": False,
        "status": "confirmed",
    }
    # Alternatives window — empty events → free slots returned
    post_slot_events: list[dict] = []

    call_count = {"n": 0}

    async def mock_select(table, filters, *, order_by=None, limit=None):
        if table == "tenant_phone_numbers":
            return [
                {
                    "tenant_id": TENANT_ID,
                    "suite_id": SUITE_ID,
                    "office_id": OFFICE_ID,
                    "phone_number": CALLED_NUMBER,
                    "status": "active",
                }
            ]
        if table == "calendar_events":
            call_count["n"] += 1
            # First query: conflict check
            if call_count["n"] == 1:
                return [conflicting_event]
            # Second query: alternatives look-ahead
            return post_slot_events
        return []

    req = CallbackRequestReq(
        called_number=CALLED_NUMBER,
        caller_name="Mike Johnson",
        caller_phone="+19416818610",
        reason="Painting quote follow-up",
        selected_slot_iso=slot_start.isoformat(),
    )

    with (
        patch("aspire_orchestrator.routes.sarah_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.sarah_tools.supabase_insert", new=AsyncMock()),
        patch("aspire_orchestrator.routes.sarah_tools._cut_receipt", return_value=str(uuid.uuid4())),
        patch("aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts"),
    ):
        result = await callback_request(req)

    assert result["success"] is False, "Conflicted slot should not succeed"
    assert result.get("slot_conflict") is True, "Response must include slot_conflict=true"
    assert "alternatives" in result, "Response must include alternatives list"
    # Alternatives should have 3 free 30-min windows after the conflict
    assert len(result["alternatives"]) == 3, (
        f"Expected 3 alternatives, got {len(result['alternatives'])}"
    )
    # Each alternative must be after slot_end
    for alt in result["alternatives"]:
        alt_start = datetime.fromisoformat(alt["start_iso"].replace("Z", "+00:00"))
        assert alt_start >= slot_end, f"Alternative {alt['start_iso']} is before conflict end"
