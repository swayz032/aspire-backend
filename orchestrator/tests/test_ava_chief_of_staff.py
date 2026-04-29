"""Tests for ava_chief_of_staff.py — 10 Ava chief-of-staff tools.

Critical test: create_handoff_note — assert all 3 memory_objects share the
same correlation_id (= handoff_id), and that handoff_id is returned.

Evil tests:
  - Invalid scope → deny (INVALID_CAPABILITY_TOKEN)
  - Empty pending_intent → INVALID_INPUT, no write
  - Partial write failure → PROVIDER_INTERNAL_ERROR, no route proceeds
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity


TENANT = uuid.uuid4()
SUITE = uuid.uuid4()
OFFICE = uuid.uuid4()
MEMORY_ID_1 = uuid.uuid4()
MEMORY_ID_2 = uuid.uuid4()
MEMORY_ID_3 = uuid.uuid4()
CANDIDATE_ID = uuid.uuid4()
NOW_ISO = datetime.now(tz=timezone.utc).isoformat()


def _scope() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE)


def _fake_memory_out(memory_id: uuid.UUID | None = None) -> MagicMock:
    mo = MagicMock()
    mo.memory_id = memory_id or uuid.uuid4()
    mo.linked_receipt_ids = [uuid.uuid4()]
    return mo


# ---------------------------------------------------------------------------
# Tool 1: get_memory_brief
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_memory_brief_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import (
        AvaMemoryBriefOut,
        get_memory_brief,
    )

    fake_brief = MagicMock()
    fake_brief.brief_text = "Office is running smoothly."
    fake_brief.brief_json = {
        "due_now": [],
        "open_approvals": [],
        "recent_receipts": [],
        "risk_summary": "Low risk",
    }
    fake_brief.last_built_at = datetime.now(tz=timezone.utc)
    fake_brief.stale = False

    with patch(
        "aspire_orchestrator.services.skillpacks.ava_chief_of_staff.BriefMaterializer.build_office_brief",
        new=AsyncMock(return_value=fake_brief),
    ):
        result = await get_memory_brief(_scope())

    assert isinstance(result, AvaMemoryBriefOut)
    assert result.office_brief == "Office is running smoothly."
    assert result.stale is False
    assert result.correlation_id


@pytest.mark.asyncio
async def test_get_memory_brief_invalid_scope_raises() -> None:
    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import (
        AvaToolError,
        get_memory_brief,
    )

    with pytest.raises(AvaToolError) as exc_info:
        await get_memory_brief(None)  # type: ignore[arg-type]

    assert exc_info.value.code == "INVALID_CAPABILITY_TOKEN"


# ---------------------------------------------------------------------------
# Tool 2: search_memory
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_memory_returns_results() -> None:
    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import (
        AvaSearchMemoryOut,
        search_memory,
    )

    fake_resp = MagicMock()
    fake_item = MagicMock()
    fake_item.memory_id = uuid.uuid4()
    fake_item.memory_type = "session_summary"
    fake_item.title = "Last Tuesday"
    fake_item.summary = "Discussed finances."
    fake_item.entity_type = "meeting"
    fake_item.last_activity_at = None
    fake_item.confidence = 0.92
    fake_resp.items = [fake_item]
    fake_resp.total = 1

    with patch(
        "aspire_orchestrator.services.skillpacks.ava_chief_of_staff.MemorySearch.search",
        new=AsyncMock(return_value=fake_resp),
    ):
        result = await search_memory(_scope(), query="last Tuesday finances")

    assert isinstance(result, AvaSearchMemoryOut)
    assert result.total == 1
    assert result.results[0]["memory_type"] == "session_summary"


@pytest.mark.asyncio
async def test_search_memory_empty_query_raises() -> None:
    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import (
        AvaToolError,
        search_memory,
    )

    with pytest.raises(AvaToolError) as exc_info:
        await search_memory(_scope(), query="   ")

    assert exc_info.value.code == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Tool 3: get_thread_memory
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_thread_memory_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import (
        AvaThreadMemoryOut,
        get_thread_memory,
    )

    # entity_id not given → returns empty thread memory without error
    result = await get_thread_memory(
        _scope(),
        entity_type="company",
        entity_name="Acme Corp",
    )
    assert isinstance(result, AvaThreadMemoryOut)
    assert result.entity_type == "company"
    assert result.correlation_id


# ---------------------------------------------------------------------------
# Tool 4: create_handoff_note — CRITICAL TEST
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_handoff_note_three_objects_share_handoff_id() -> None:
    """All 3 memory objects must share the same correlation_id (= handoff_id)."""
    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import (
        AvaHandoffNoteOut,
        create_handoff_note,
    )

    written_envelopes: list[Any] = []
    call_count = 0
    memory_ids = [MEMORY_ID_1, MEMORY_ID_2, MEMORY_ID_3]

    async def mock_write(envelope: Any, *, scope: Any, embed: bool) -> Any:
        nonlocal call_count
        written_envelopes.append(envelope)
        mo = MagicMock()
        mo.memory_id = memory_ids[call_count]
        mo.linked_receipt_ids = [uuid.uuid4()]
        call_count += 1
        return mo

    with patch(
        "aspire_orchestrator.services.skillpacks.ava_chief_of_staff.MemoryService.write",
        side_effect=mock_write,
    ):
        result = await create_handoff_note(
            _scope(),
            pending_intent="Owner wants to send invoice to Acme.",
            authority_context="YELLOW tier — requires confirmation.",
            handoff_note="Owner is routing to Quinn for invoicing. Acme invoice due this week.",
            receiving_agent="quinn",
        )

    assert isinstance(result, AvaHandoffNoteOut)
    # All 3 objects must share the handoff_id
    assert result.handoff_id == result.correlation_id
    assert result.pending_intent_id == str(MEMORY_ID_1)
    assert result.authority_context_id == str(MEMORY_ID_2)
    assert result.handoff_note_id == str(MEMORY_ID_3)
    # All 3 envelopes must carry the same correlation_id (= handoff_id)
    handoff_uuid = uuid.UUID(result.handoff_id)
    for env in written_envelopes:
        assert env.provenance.correlation_id == handoff_uuid, (
            f"Envelope correlation_id {env.provenance.correlation_id} != handoff_id {handoff_uuid}"
        )
    # 3 receipt IDs
    assert len(result.receipt_ids) == 3


@pytest.mark.asyncio
async def test_create_handoff_note_empty_pending_intent_raises() -> None:
    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import (
        AvaToolError,
        create_handoff_note,
    )

    with pytest.raises(AvaToolError) as exc_info:
        await create_handoff_note(
            _scope(),
            pending_intent="",
            authority_context="some context",
            handoff_note="some note",
            receiving_agent="eli",
        )

    assert exc_info.value.code == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_create_handoff_note_partial_failure_raises_internal_error() -> None:
    """Partial write (2nd object fails) → PROVIDER_INTERNAL_ERROR — no route proceeds."""
    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import (
        AvaToolError,
        create_handoff_note,
    )
    from aspire_orchestrator.services.memory_service import MemoryServiceError

    call_count = 0

    async def mock_write_fail_on_second(envelope: Any, *, scope: Any, embed: bool) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise MemoryServiceError("DB error on second write", code="UNKNOWN_ERROR")
        mo = MagicMock()
        mo.memory_id = uuid.uuid4()
        mo.linked_receipt_ids = [uuid.uuid4()]
        return mo

    with patch(
        "aspire_orchestrator.services.skillpacks.ava_chief_of_staff.MemoryService.write",
        side_effect=mock_write_fail_on_second,
    ):
        with pytest.raises(AvaToolError) as exc_info:
            await create_handoff_note(
                _scope(),
                pending_intent="Owner wants to review contracts.",
                authority_context="RED tier review.",
                handoff_note="Routing to Clara.",
                receiving_agent="clara",
            )

    assert exc_info.value.code == "PROVIDER_INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Tool 5: save_session_summary — state change
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_session_summary_writes_session_summary() -> None:
    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import (
        AvaSessionSummaryOut,
        save_session_summary,
    )

    fake_out = _fake_memory_out(MEMORY_ID_1)
    fake_out.idempotency_key = "session:sess-123"

    captured: list[Any] = []

    async def mock_write(envelope: Any, *, scope: Any, embed: bool) -> Any:
        captured.append(envelope)
        return fake_out

    with patch(
        "aspire_orchestrator.services.skillpacks.ava_chief_of_staff.MemoryService.write",
        side_effect=mock_write,
    ):
        result = await save_session_summary(
            _scope(),
            session_id="sess-123",
            summary="Good session. Routed invoice to Quinn.",
            decisions=["Send invoice to Acme"],
            routed_to=["quinn"],
        )

    assert isinstance(result, AvaSessionSummaryOut)
    assert result.memory_id == str(MEMORY_ID_1)
    assert result.idempotency_replay is False
    # Verify idempotency key
    assert captured[0].idempotency_key == "session:sess-123"
    assert captured[0].provenance.source_agent == "ava"


# ---------------------------------------------------------------------------
# Tool 6: promote_artifact — state change
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_promote_artifact_updates_status() -> None:
    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import (
        AvaPromoteArtifactOut,
        promote_artifact,
    )

    fake_out = _fake_memory_out(MEMORY_ID_1)

    with patch(
        "aspire_orchestrator.services.skillpacks.ava_chief_of_staff.MemoryService.update_status",
        new=AsyncMock(return_value=fake_out),
    ):
        result = await promote_artifact(
            _scope(),
            memory_id=str(MEMORY_ID_1),
            reason="Signed contract — high value artifact",
        )

    assert isinstance(result, AvaPromoteArtifactOut)
    assert result.status == "promoted"
    assert result.memory_id == str(MEMORY_ID_1)


@pytest.mark.asyncio
async def test_promote_artifact_empty_reason_raises() -> None:
    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import (
        AvaToolError,
        promote_artifact,
    )

    with pytest.raises(AvaToolError) as exc_info:
        await promote_artifact(_scope(), memory_id=str(MEMORY_ID_1), reason="")

    assert exc_info.value.code == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Tools 7–10: route_to_* — creates proactive candidate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("target_agent,route_fn", [
    ("eli", "route_to_eli"),
    ("nora", "route_to_nora"),
    ("finn", "route_to_finn"),
    ("sarah", "route_to_sarah"),
])
async def test_route_to_agent_creates_candidate(target_agent: str, route_fn: str) -> None:
    import importlib
    mod = importlib.import_module("aspire_orchestrator.services.skillpacks.ava_chief_of_staff")
    route_func = getattr(mod, route_fn)

    fake_out = MagicMock()
    fake_out.candidate_id = CANDIDATE_ID
    fake_out.receipt_id = uuid.uuid4()

    captured_candidates: list[Any] = []

    async def mock_create(candidate_in: Any, *, scope: Any) -> Any:
        captured_candidates.append(candidate_in)
        return fake_out

    with patch(
        "aspire_orchestrator.services.skillpacks.ava_chief_of_staff.ProactiveCandidateEngine.create_candidate",
        side_effect=mock_create,
    ):
        result = await route_func(
            _scope(),
            handoff_id=str(uuid.uuid4()),
            intent_summary=f"Owner needs {target_agent} for their request",
        )

    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import AvaRouteOut
    assert isinstance(result, AvaRouteOut)
    assert result.candidate_id == str(CANDIDATE_ID)
    # The candidate's owner_agent must be the target
    assert captured_candidates[0].owner_agent == target_agent


# ---------------------------------------------------------------------------
# Evil tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evil_no_scope_all_tools_deny() -> None:
    """Every tool must deny when scope is None (Law #3, Law #5)."""
    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import (
        AvaToolError,
        get_memory_brief,
        search_memory,
        save_session_summary,
        promote_artifact,
        route_to_eli,
    )

    evil_calls = [
        (get_memory_brief, {"scope": None}),
        (search_memory, {"scope": None, "query": "anything"}),
        (save_session_summary, {"scope": None, "session_id": "s1", "summary": "x"}),
        (promote_artifact, {"scope": None, "memory_id": str(uuid.uuid4()), "reason": "r"}),
        (route_to_eli, {"scope": None, "handoff_id": str(uuid.uuid4()), "intent_summary": "x"}),
    ]

    for fn, kwargs in evil_calls:
        with pytest.raises(AvaToolError) as exc_info:
            await fn(**kwargs)  # type: ignore[arg-type]
        assert exc_info.value.code == "INVALID_CAPABILITY_TOKEN", (
            f"{fn.__name__} did not deny with INVALID_CAPABILITY_TOKEN"
        )


# ---------------------------------------------------------------------------
# Registered tool names
# ---------------------------------------------------------------------------

def test_ava_chief_of_staff_tools_has_10_entries() -> None:
    from aspire_orchestrator.services.skillpacks.ava_chief_of_staff import (
        AVA_CHIEF_OF_STAFF_TOOLS,
    )

    assert len(AVA_CHIEF_OF_STAFF_TOOLS) == 10
    assert "ava.memory.create_handoff_note" in AVA_CHIEF_OF_STAFF_TOOLS
    assert "ava.routing.route_to_sarah" in AVA_CHIEF_OF_STAFF_TOOLS
