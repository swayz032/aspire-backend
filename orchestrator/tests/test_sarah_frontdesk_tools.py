"""Tests for sarah_frontdesk_tools.py — 6 Sarah Front Desk tool wrappers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity


TENANT = uuid.uuid4()
SUITE = uuid.uuid4()
OFFICE = uuid.uuid4()
MEMORY_ID = uuid.uuid4()
CANDIDATE_ID = uuid.uuid4()
NOW_ISO = datetime.now(tz=timezone.utc).isoformat()


def _scope() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE)


def _fake_memory_out() -> MagicMock:
    mo = MagicMock()
    mo.memory_id = MEMORY_ID
    mo.linked_receipt_ids = [uuid.uuid4()]
    return mo


def _fake_candidate_out() -> MagicMock:
    mo = MagicMock()
    mo.candidate_id = CANDIDATE_ID
    mo.receipt_id = uuid.uuid4()
    return mo


# ---------------------------------------------------------------------------
# get_context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_context_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools import (
        SarahContextOut,
        get_context,
    )

    fake_calls = [{"id": "c1", "status": "missed"}]
    fake_vms: list[dict] = []
    fake_cbs: list[dict] = []

    with patch(
        "aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools.supabase_select",
        side_effect=[fake_calls, fake_vms, fake_cbs],
    ):
        result = await get_context(_scope())

    assert isinstance(result, SarahContextOut)
    assert result.missed_calls_summary["count"] == 1
    assert result.confidence in ("high", "low")


@pytest.mark.asyncio
async def test_get_context_invalid_scope_raises() -> None:
    from aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools import (
        SarahFrontDeskToolError,
        get_context,
    )

    with pytest.raises(SarahFrontDeskToolError) as exc_info:
        await get_context(None)  # type: ignore[arg-type]

    assert exc_info.value.code == "INVALID_CAPABILITY_TOKEN"


# ---------------------------------------------------------------------------
# search_memory
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_memory_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools import (
        SarahSearchOut,
        search_memory,
    )

    fake_resp = MagicMock()
    fake_result = MagicMock()
    fake_result.memory_id = uuid.uuid4()
    fake_result.memory_type = "timeline_event"
    fake_result.title = "Call from John"
    fake_result.summary = "John called about invoice."
    fake_result.last_activity_at = None
    fake_resp.items = [fake_result]
    fake_resp.total = 1

    with patch(
        "aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools.MemorySearch.search",
        new=AsyncMock(return_value=fake_resp),
    ):
        result = await search_memory(_scope(), query="John invoice")

    assert isinstance(result, SarahSearchOut)
    assert len(result.matches) == 1
    assert result.confidence == "high"


# ---------------------------------------------------------------------------
# get_thread_memory
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_thread_memory_returns_thread_items() -> None:
    from aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools import (
        get_thread_memory,
    )

    fake_mo = MagicMock()
    fake_mo.memory_id = uuid.uuid4()
    fake_mo.memory_type = "timeline_event"
    fake_mo.summary = "First call"
    fake_mo.last_activity_at = None

    with patch(
        "aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools.MemoryService.list_by_thread",
        new=AsyncMock(return_value=([fake_mo], None)),
    ):
        result = await get_thread_memory(_scope(), thread_id=str(uuid.uuid4()))

    assert result["total"] == 1
    assert result["correlation_id"]


# ---------------------------------------------------------------------------
# create_handoff_note — state change
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_handoff_note_writes_memory() -> None:
    from aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools import (
        SarahHandoffNoteOut,
        create_handoff_note,
    )

    fake_out = _fake_memory_out()

    with patch(
        "aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools.MemoryService.write",
        new=AsyncMock(return_value=fake_out),
    ):
        result = await create_handoff_note(
            _scope(),
            caller_name="Jane Smith",
            callback_number="555-0100",  # PII — must NOT appear in logs/receipts
            reason="Invoice dispute",
            urgency="high",
            recommended_next_step="Call back within 1h",
            supporting_summary="Jane called about invoice #123",
        )

    assert isinstance(result, SarahHandoffNoteOut)
    assert result.memory_id == str(MEMORY_ID)


@pytest.mark.asyncio
async def test_create_handoff_note_pii_not_in_receipt() -> None:
    """callback_number must never appear in log line or receipt (Law #9)."""
    from aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools import (
        create_handoff_note,
    )

    captured_envelopes: list[Any] = []

    async def mock_write(env: Any, *, scope: Any, embed: bool) -> Any:
        captured_envelopes.append(env)
        mo = MagicMock()
        mo.memory_id = MEMORY_ID
        mo.linked_receipt_ids = [uuid.uuid4()]
        return mo

    with patch(
        "aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools.MemoryService.write",
        side_effect=mock_write,
    ):
        await create_handoff_note(
            _scope(),
            caller_name="Jane Smith",
            callback_number="555-0100",
            reason="Payment",
            urgency="low",
            recommended_next_step="Call back",
            supporting_summary="Jane called",
        )

    # callback_number should NOT appear in the summary or title (it's in detail)
    for env in captured_envelopes:
        assert "555-0100" not in (env.summary or "")
        assert "555-0100" not in (env.title or "")


# ---------------------------------------------------------------------------
# triage_callback_queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_triage_callback_queue_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools import (
        SarahCallbackQueueOut,
        triage_callback_queue,
    )

    fake_callbacks = [{"id": "cb1", "caller": "Jane"}]
    fake_vms = [{"id": "vm1", "transcript": "left voicemail"}]

    with patch(
        "aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools.supabase_select",
        side_effect=[fake_callbacks, fake_vms],
    ):
        result = await triage_callback_queue(_scope())

    assert isinstance(result, SarahCallbackQueueOut)
    assert result.total == 2
    assert len(result.callbacks) == 1
    assert len(result.voicemails) == 1


# ---------------------------------------------------------------------------
# escalate_to_owner — state change, proactive candidate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_escalate_to_owner_creates_proactive_candidate() -> None:
    from aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools import (
        SarahEscalateOut,
        escalate_to_owner,
    )

    fake_out = _fake_candidate_out()

    with patch(
        "aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools.ProactiveCandidateEngine.create_candidate",
        new=AsyncMock(return_value=fake_out),
    ):
        result = await escalate_to_owner(
            _scope(),
            urgency="high",
            reason="Missed call from key client",
        )

    assert isinstance(result, SarahEscalateOut)
    assert result.candidate_id == str(CANDIDATE_ID)
    assert result.correlation_id


# ---------------------------------------------------------------------------
# Registered tool names
# ---------------------------------------------------------------------------

def test_sarah_frontdesk_tools_has_6_entries() -> None:
    from aspire_orchestrator.services.skillpacks.sarah_frontdesk_tools import (
        SARAH_FRONTDESK_TOOLS,
    )

    assert len(SARAH_FRONTDESK_TOOLS) == 6
    assert "sarah.frontdesk.escalate_to_owner" in SARAH_FRONTDESK_TOOLS
    assert "sarah.frontdesk.triage_callback_queue" in SARAH_FRONTDESK_TOOLS
