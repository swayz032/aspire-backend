"""Unit tests for Google Calendar + Aspire Calendar ingestion adapters — Pass 14 expansion."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.services.ingestion.base import IngestionError
from aspire_orchestrator.services.ingestion.calendar_ingestion import (
    AspireCalendarIngestionAdapter,
    GoogleCalendarIngestionAdapter,
)
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

TENANT_A = UUID("ee000000-0000-0000-0000-000000000001")
SUITE_A = UUID("ee000000-0000-0000-0000-000000000002")
OFFICE_A = UUID("ee000000-0000-0000-0000-000000000003")

SCOPE_A = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)

CHANNEL_ROW_A = {
    "tenant_id": str(TENANT_A),
    "suite_id": str(SUITE_A),
    "office_id": str(OFFICE_A),
    "provider": "google_calendar",
    "external_account_id": "chan_gcal_001",
    "access_token": "ya29.fake_token",
    "calendar_id": "primary",
    "sync_token": "sync_abc123",
}

_GCAL_PAYLOAD = {
    "channel_id": "chan_gcal_001",
    "resource_id": "res_001",
    "resource_state": "exists",
}

_GCAL_EVENT = {
    "id": "event_gcal_001",
    "summary": "Quarterly Review",
    "description": "Q2 review with team",
    "start": {"dateTime": "2026-04-30T10:00:00Z", "timeZone": "UTC"},
    "end": {"dateTime": "2026-04-30T11:00:00Z", "timeZone": "UTC"},
    "location": "Conference Room A",
    "attendees": [
        {"email": "alice@example.com", "responseStatus": "accepted"},
        {"email": "bob@example.com", "responseStatus": "needsAction"},
    ],
    "organizer": {"email": "alice@example.com"},
    "status": "confirmed",
    "htmlLink": "https://calendar.google.com/event?eid=abc",
}

_ASPIRE_PAYLOAD = {
    "tenant_id": str(TENANT_A),
    "suite_id": str(SUITE_A),
    "office_id": str(OFFICE_A),
    "event_id": "aspire_evt_001",
    "action": "created",
    "title": "Team Standup",
    "description": "Daily standup",
    "start_at": "2026-04-30T09:00:00Z",
    "end_at": "2026-04-30T09:15:00Z",
    "timezone": "America/New_York",
    "location": "Zoom",
    "attendees": [
        {"email": "alice@example.com", "response_status": "accepted"},
    ],
    "created_by_user_id": "user_alice",
    "recurrence_rule": "RRULE:FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR",
}


# ===========================================================================
# Google Calendar adapter
# ===========================================================================

class TestGoogleCalendarVerifySignature:

    @pytest.mark.asyncio
    async def test_known_channel_id_returns_true(self) -> None:
        adapter = GoogleCalendarIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.calendar_ingestion.supabase_select",
            new=AsyncMock(return_value=[CHANNEL_ROW_A]),
        ):
            result = await adapter.verify_signature(
                body=b"",
                headers={"X-Goog-Channel-ID": "chan_gcal_001"},
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_unknown_channel_id_returns_false(self) -> None:
        adapter = GoogleCalendarIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.calendar_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            result = await adapter.verify_signature(
                body=b"",
                headers={"X-Goog-Channel-ID": "unknown_channel"},
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_missing_channel_id_returns_false(self) -> None:
        adapter = GoogleCalendarIngestionAdapter()
        result = await adapter.verify_signature(body=b"", headers={})
        assert result is False


class TestGoogleCalendarResolveScope:

    @pytest.mark.asyncio
    async def test_valid_channel_returns_scope(self) -> None:
        adapter = GoogleCalendarIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.calendar_ingestion.supabase_select",
            new=AsyncMock(return_value=[CHANNEL_ROW_A]),
        ):
            scope = await adapter.resolve_scope(_GCAL_PAYLOAD)
        assert scope.tenant_id == TENANT_A

    @pytest.mark.asyncio
    async def test_missing_channel_id_raises_422(self) -> None:
        adapter = GoogleCalendarIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.resolve_scope({})
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "MISSING_CHANNEL_ID"

    @pytest.mark.asyncio
    async def test_unknown_channel_raises_404(self) -> None:
        adapter = GoogleCalendarIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.calendar_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await adapter.resolve_scope(_GCAL_PAYLOAD)
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "UNKNOWN_CHANNEL"


class TestGoogleCalendarBuildEnvelope:

    @pytest.mark.asyncio
    async def test_happy_path_fields(self) -> None:
        adapter = GoogleCalendarIngestionAdapter()
        payload = {**_GCAL_PAYLOAD, "event": _GCAL_EVENT, "calendar_id": "primary"}
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env.memory_type == "calendar_event"
        assert env.title == "Quarterly Review"
        assert env.status == "executed"
        assert env.event_at is not None

    @pytest.mark.asyncio
    async def test_event_at_is_start_time(self) -> None:
        """event_at must be the event start (for idx_memory_objects_calendar_recent index)."""
        adapter = GoogleCalendarIngestionAdapter()
        payload = {**_GCAL_PAYLOAD, "event": _GCAL_EVENT}
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        # 2026-04-30T10:00:00Z
        assert env.event_at is not None
        assert env.event_at.month == 4
        assert env.event_at.day == 30

    @pytest.mark.asyncio
    async def test_idempotency_key_is_event_id(self) -> None:
        adapter = GoogleCalendarIngestionAdapter()
        payload = {**_GCAL_PAYLOAD, "event": _GCAL_EVENT}
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env.idempotency_key == "gcal-event-event_gcal_001"

    @pytest.mark.asyncio
    async def test_calendar_source_is_google(self) -> None:
        adapter = GoogleCalendarIngestionAdapter()
        payload = {**_GCAL_PAYLOAD, "event": _GCAL_EVENT}
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env.detail["calendar_source"] == "google"

    @pytest.mark.asyncio
    async def test_missing_event_raises_422(self) -> None:
        adapter = GoogleCalendarIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.build_envelope(_GCAL_PAYLOAD, scope=SCOPE_A, thread=None)
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_description_truncated_at_80_chars(self) -> None:
        """Law #9 — description capped at 80 chars in detail."""
        long_desc = "X" * 200
        event = {**_GCAL_EVENT, "description": long_desc}
        payload = {**_GCAL_PAYLOAD, "event": event}
        adapter = GoogleCalendarIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        desc = env.detail.get("description") or ""
        assert len(desc) <= 80

    @pytest.mark.asyncio
    async def test_attendee_emails_in_detail(self) -> None:
        adapter = GoogleCalendarIngestionAdapter()
        payload = {**_GCAL_PAYLOAD, "event": _GCAL_EVENT}
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        attendee_emails = [a["email"] for a in env.detail["attendees"]]
        assert "alice@example.com" in attendee_emails

    @pytest.mark.asyncio
    async def test_idempotency_is_deterministic(self) -> None:
        adapter = GoogleCalendarIngestionAdapter()
        payload = {**_GCAL_PAYLOAD, "event": _GCAL_EVENT}
        env1 = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        env2 = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env1.idempotency_key == env2.idempotency_key


class TestGoogleCalendarIngest:

    @pytest.mark.asyncio
    async def test_sync_ping_raises_200(self) -> None:
        """Initial sync confirmation → no memory write, 200 returned."""
        adapter = GoogleCalendarIngestionAdapter()
        payload = {**_GCAL_PAYLOAD, "resource_state": "sync"}
        with patch(
            "aspire_orchestrator.services.ingestion.calendar_ingestion.supabase_select",
            new=AsyncMock(return_value=[CHANNEL_ROW_A]),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await adapter.ingest(body=b"", headers={"X-Goog-Channel-ID": "chan_gcal_001"}, payload=payload)
        assert exc_info.value.status_code == 200
        assert exc_info.value.code == "SYNC_PING_NO_OP"

    @pytest.mark.asyncio
    async def test_invalid_channel_raises_401(self) -> None:
        adapter = GoogleCalendarIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.calendar_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await adapter.ingest(body=b"", headers={"X-Goog-Channel-ID": "bad"}, payload=_GCAL_PAYLOAD)
        assert exc_info.value.status_code == 401


# ===========================================================================
# Aspire Calendar adapter
# ===========================================================================

class TestAspireCalendarVerifySignature:

    @pytest.mark.asyncio
    async def test_always_returns_true(self) -> None:
        """Internal route — no HMAC; security is the route auth layer."""
        adapter = AspireCalendarIngestionAdapter()
        result = await adapter.verify_signature(body=b"anything", headers={})
        assert result is True


class TestAspireCalendarResolveScope:

    @pytest.mark.asyncio
    async def test_valid_payload_returns_scope(self) -> None:
        adapter = AspireCalendarIngestionAdapter()
        scope = await adapter.resolve_scope(_ASPIRE_PAYLOAD)
        assert scope.tenant_id == TENANT_A
        assert scope.suite_id == SUITE_A
        assert scope.office_id == OFFICE_A

    @pytest.mark.asyncio
    async def test_missing_tenant_id_raises_422(self) -> None:
        payload = {**_ASPIRE_PAYLOAD}
        del payload["tenant_id"]
        adapter = AspireCalendarIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.resolve_scope(payload)
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_uuid_raises_422(self) -> None:
        payload = {**_ASPIRE_PAYLOAD, "office_id": "not-a-uuid"}
        adapter = AspireCalendarIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.resolve_scope(payload)
        assert exc_info.value.status_code == 422


class TestAspireCalendarBuildEnvelope:

    @pytest.mark.asyncio
    async def test_created_action(self) -> None:
        adapter = AspireCalendarIngestionAdapter()
        env = await adapter.build_envelope(_ASPIRE_PAYLOAD, scope=SCOPE_A, thread=None)
        assert env.memory_type == "calendar_event"
        assert env.status == "executed"
        assert env.title == "Team Standup"
        assert env.idempotency_key == "aspire-cal-aspire_evt_001"

    @pytest.mark.asyncio
    async def test_updated_action_has_versioned_idempotency_key(self) -> None:
        """Each edit produces a unique row via versioned idempotency_key."""
        payload = {**_ASPIRE_PAYLOAD, "action": "updated"}
        adapter = AspireCalendarIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env.status == "executed"
        assert "aspire-cal-aspire_evt_001-v" in env.idempotency_key
        assert env.idempotency_key != "aspire-cal-aspire_evt_001"

    @pytest.mark.asyncio
    async def test_deleted_action_is_superseded(self) -> None:
        payload = {**_ASPIRE_PAYLOAD, "action": "deleted"}
        adapter = AspireCalendarIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env.status == "superseded"
        assert "deleted" in env.title.lower()
        assert env.idempotency_key == "aspire-cal-aspire_evt_001-deleted"

    @pytest.mark.asyncio
    async def test_invalid_action_raises_422(self) -> None:
        payload = {**_ASPIRE_PAYLOAD, "action": "mystery"}
        adapter = AspireCalendarIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "INVALID_ACTION"

    @pytest.mark.asyncio
    async def test_missing_event_id_raises_422(self) -> None:
        payload = {**_ASPIRE_PAYLOAD, "event_id": ""}
        adapter = AspireCalendarIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_calendar_source_is_aspire(self) -> None:
        adapter = AspireCalendarIngestionAdapter()
        env = await adapter.build_envelope(_ASPIRE_PAYLOAD, scope=SCOPE_A, thread=None)
        assert env.detail["calendar_source"] == "aspire"

    @pytest.mark.asyncio
    async def test_event_at_is_start_at(self) -> None:
        adapter = AspireCalendarIngestionAdapter()
        env = await adapter.build_envelope(_ASPIRE_PAYLOAD, scope=SCOPE_A, thread=None)
        assert env.event_at is not None
        assert env.event_at.day == 30
        assert env.event_at.month == 4

    @pytest.mark.asyncio
    async def test_client_idempotency_key_override(self) -> None:
        payload = {**_ASPIRE_PAYLOAD, "idempotency_key": "custom-key-xyz"}
        adapter = AspireCalendarIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env.idempotency_key == "custom-key-xyz"

    @pytest.mark.asyncio
    async def test_detail_fields_present(self) -> None:
        adapter = AspireCalendarIngestionAdapter()
        env = await adapter.build_envelope(_ASPIRE_PAYLOAD, scope=SCOPE_A, thread=None)
        required = {"event_id", "action", "title", "start_at", "end_at", "timezone",
                    "attendees", "organizer", "calendar_source"}
        for field in required:
            assert field in env.detail, f"Missing detail field: {field}"

    @pytest.mark.asyncio
    async def test_description_truncated_at_80_chars(self) -> None:
        payload = {**_ASPIRE_PAYLOAD, "description": "D" * 200}
        adapter = AspireCalendarIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        desc = env.detail.get("description") or ""
        assert len(desc) <= 80

    @pytest.mark.asyncio
    async def test_recurrence_rule_in_detail(self) -> None:
        adapter = AspireCalendarIngestionAdapter()
        env = await adapter.build_envelope(_ASPIRE_PAYLOAD, scope=SCOPE_A, thread=None)
        assert env.detail["recurrence_rule"] == "RRULE:FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR"

    @pytest.mark.asyncio
    async def test_cross_tenant_scope_enforced(self) -> None:
        """Different tenant IDs → different ScopedIdentity. No cross-tenant risk here
        since scope comes from the authenticated route, not from a lookup."""
        tenant_b = UUID("ff000000-0000-0000-0000-000000000001")
        suite_b = UUID("ff000000-0000-0000-0000-000000000002")
        office_b = UUID("ff000000-0000-0000-0000-000000000003")
        payload = {
            **_ASPIRE_PAYLOAD,
            "tenant_id": str(tenant_b),
            "suite_id": str(suite_b),
            "office_id": str(office_b),
        }
        adapter = AspireCalendarIngestionAdapter()
        scope_b = await adapter.resolve_scope(payload)
        assert scope_b.tenant_id == tenant_b
        assert scope_b.tenant_id != TENANT_A

    @pytest.mark.asyncio
    async def test_idempotency_is_deterministic_for_created(self) -> None:
        adapter = AspireCalendarIngestionAdapter()
        env1 = await adapter.build_envelope(_ASPIRE_PAYLOAD, scope=SCOPE_A, thread=None)
        env2 = await adapter.build_envelope(_ASPIRE_PAYLOAD, scope=SCOPE_A, thread=None)
        assert env1.idempotency_key == env2.idempotency_key
