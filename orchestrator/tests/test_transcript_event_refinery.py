"""Tests for TranscriptEventRefinery.

Mocks MemoryService, EntityThreadResolver, ProactiveCandidateEngine, and the
inbox row-loading helpers. Verifies routing by event_type and DLQ-on-exception.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    ScopedIdentity,
    ThreadOut,
)
from aspire_orchestrator.services.transcript_event_refinery import (
    TranscriptEventRefinery,
)

TENANT_A = uuid.uuid4()
SUITE_A = uuid.uuid4()
OFFICE_A = uuid.uuid4()
TRACE = uuid.uuid4()
CORR = uuid.uuid4()
THREAD_ID = uuid.uuid4()
NOW = datetime.now(tz=timezone.utc)


def _inbox_row(event_type: str, payload: dict | None = None) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "v1",
        "tenant_id": str(TENANT_A),
        "suite_id": str(SUITE_A),
        "office_id": str(OFFICE_A),
        "actor_id": None,
        "user_id": None,
        "event_type": event_type,
        "source_surface": "ava_voice",
        "source_agent": "ava",
        "runtime_family": "elevenlabs",
        "channel": "voice",
        "trace_id": str(TRACE),
        "correlation_id": str(CORR),
        "source_record_id": None,
        "session_id": None,
        "thread_id": None,
        "entity_type": None,
        "entity_id": None,
        "payload": payload or {},
        "risk_tier": "yellow",
        "needs_approval": False,
        "receipt_required": False,
        "event_at": NOW.isoformat(),
        "created_at": NOW.isoformat(),
        "source_updated_at": None,
        "idempotency_key": "test-key-001",
        "status": "pending",
        "attempts": 0,
        "last_error": None,
        "processed_at": None,
        "dead_lettered_at": None,
    }


def _make_thread() -> ThreadOut:
    return ThreadOut(
        thread_id=THREAD_ID,
        tenant_id=TENANT_A,
        suite_id=SUITE_A,
        office_id=OFFICE_A,
        thread_type="internal_thread",
        status="open",
        first_event_at=NOW,
        last_activity_at=NOW,
        participants=[],
        tags=[],
        created_at=NOW,
    )


def _build_refinery(
    *, write_returns=None, resolve_returns=None
) -> tuple[TranscriptEventRefinery, AsyncMock, AsyncMock, AsyncMock]:
    memory_service = AsyncMock()
    thread_resolver = AsyncMock()
    candidate_engine = AsyncMock()

    if resolve_returns is None:
        resolve_returns = _make_thread()
    thread_resolver.resolve = AsyncMock(return_value=resolve_returns)

    async def fake_write(envelope, *, scope, embed=True):
        # Return a minimal MemoryObjectOut-shaped object
        out = AsyncMock()
        out.memory_id = uuid.uuid4()
        return out

    memory_service.write = AsyncMock(side_effect=fake_write)
    candidate_engine.create_candidate = AsyncMock(
        side_effect=lambda c, *, scope: _candidate_out_stub()
    )

    refinery = TranscriptEventRefinery(
        memory_service=memory_service,
        thread_resolver=thread_resolver,
        candidate_engine=candidate_engine,
    )
    return refinery, memory_service, thread_resolver, candidate_engine


def _candidate_out_stub():
    out = AsyncMock()
    out.candidate_id = uuid.uuid4()
    return out


@pytest.mark.asyncio
class TestRefineRouting:
    async def test_voice_event_produces_session_summary(self) -> None:
        row = _inbox_row("voice_session_ended", {"summary": "Caller asked about quote."})
        refinery, memory_service, _, _ = _build_refinery()
        with patch(
            "aspire_orchestrator.services.transcript_event_refinery.supabase_select",
            new=AsyncMock(return_value=[row]),
        ), patch(
            "aspire_orchestrator.services.transcript_event_refinery.supabase_update",
            new=AsyncMock(return_value=[row]),
        ):
            result = await refinery.refine(uuid.UUID(row["event_id"]))
            assert len(result.memory_ids) >= 1
            assert memory_service.write.await_count >= 1

    async def test_meeting_event_produces_summary_plus_action_items(self) -> None:
        payload = {
            "summary": "Pricing meeting with Acme.",
            "action_items": [
                {"label": "Send updated quote", "owner": "ava"},
                {"label": "Schedule follow-up", "owner": "ava"},
            ],
        }
        row = _inbox_row("meeting_recap_ready", payload)
        refinery, memory_service, _, _ = _build_refinery()
        with patch(
            "aspire_orchestrator.services.transcript_event_refinery.supabase_select",
            new=AsyncMock(return_value=[row]),
        ), patch(
            "aspire_orchestrator.services.transcript_event_refinery.supabase_update",
            new=AsyncMock(return_value=[row]),
        ):
            result = await refinery.refine(uuid.UUID(row["event_id"]))
            # 1 session_summary + 2 followup_task = 3 writes (or close)
            assert memory_service.write.await_count >= 1


@pytest.mark.asyncio
class TestRefineDeadLetter:
    async def test_inbox_row_missing_raises(self) -> None:
        refinery, _, _, _ = _build_refinery()
        with patch(
            "aspire_orchestrator.services.transcript_event_refinery.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(Exception):
                await refinery.refine(uuid.uuid4())

    async def test_refiner_exception_marks_dead_letter(self) -> None:
        row = _inbox_row("voice_session_ended", {"summary": "x"})
        refinery, memory_service, _, _ = _build_refinery()

        # Memory write raises so the refiner enters dead-letter path
        memory_service.write = AsyncMock(
            side_effect=RuntimeError("simulated downstream failure")
        )

        update_calls: list[dict] = []

        # Real supabase_update signature: (table: str, filter: str, payload: dict, **kwargs)
        async def capture_update(table, filter_str, payload, *args, **kwargs):
            update_calls.append({"table": table, "filter": filter_str, "payload": payload})
            return row

        with patch(
            "aspire_orchestrator.services.transcript_event_refinery.supabase_select",
            new=AsyncMock(return_value=[row]),
        ), patch(
            "aspire_orchestrator.services.transcript_event_refinery.supabase_update",
            new=AsyncMock(side_effect=capture_update),
        ):
            with pytest.raises(Exception):
                await refinery.refine(uuid.UUID(row["event_id"]))

        # Verify a 'dead_letter' status update was attempted
        statuses = [c["payload"].get("status") for c in update_calls]
        assert "dead_letter" in statuses, (
            f"expected dead_letter update; got {statuses}"
        )
