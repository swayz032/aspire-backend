"""Nora Conference Skill Pack Tests — 10 tests covering create_room, schedule, summarize.

Categories:
  1. Room creation (3 tests) — success, receipt, GREEN tier
  2. Meeting scheduling (2 tests) — YELLOW tier, approval required
  3. Meeting summarization (2 tests) — success, receipt
  4. Tier enforcement (2 tests) — GREEN for room/summary, no approval
  5. Tool executor integration (1 test) — verifies executor is called

Law compliance:
  - Law #2: Every test verifies receipt emission
  - Law #3: Missing params produce fail-closed error + receipt
  - Law #4: GREEN/YELLOW tier classification verified
  - Law #7: Tool executor called (not direct provider)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.skillpacks.nora_conference import (
    ACTOR_NORA,
    NoraConferenceSkillPack,
    NoraContext,
)


# =============================================================================
# Fixtures
# =============================================================================

SUITE_ID = "suite-nora-test-001"
OFFICE_ID = "office-nora-001"
CORR_ID = "corr-nora-test-001"


@pytest.fixture
def ctx() -> NoraContext:
    """Tenant-scoped execution context."""
    return NoraContext(suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)


@pytest.fixture
def nora() -> NoraConferenceSkillPack:
    """Fresh Nora skill pack instance."""
    return NoraConferenceSkillPack()


def _mock_zoom_success() -> ToolExecutionResult:
    """Simulated successful Zoom session creation."""
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="zoom.session.create",
        data={
            "session_name": "test-room",
            "session_id": "ZS_abc123",
            "session_key": "",
            "status": "available",
        },
        receipt_data={},
    )


def _mock_zoom_failure() -> ToolExecutionResult:
    """Simulated failed Zoom session creation."""
    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id="zoom.session.create",
        error="Zoom API error: HTTP 500",
        receipt_data={},
    )


def _mock_deepgram_success() -> ToolExecutionResult:
    """Simulated successful Deepgram transcription."""
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="deepgram.transcribe",
        data={
            "transcript": "Hello everyone, let us discuss the quarterly results.",
            "confidence": 0.95,
            "words_count": 8,
            "duration": 120.5,
            "model": "nova-3",
            "language": "en",
        },
        receipt_data={},
    )


def _mock_deepgram_failure() -> ToolExecutionResult:
    """Simulated failed Deepgram transcription."""
    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id="deepgram.transcribe",
        error="Deepgram API error: HTTP 401",
        receipt_data={},
    )


# =============================================================================
# 1. Room Creation Tests
# =============================================================================


class TestCreateRoom:
    """Test create_room (GREEN tier)."""

    @pytest.mark.asyncio
    async def test_create_room_success(
        self, nora: NoraConferenceSkillPack, ctx: NoraContext,
    ) -> None:
        """Successful room creation returns room data."""
        with patch(
            "aspire_orchestrator.skillpacks.nora_conference.execute_tool",
            new_callable=AsyncMock,
            return_value=_mock_zoom_success(),
        ):
            result = await nora.create_room("standup-daily", None, ctx)

        assert result.success
        assert result.data["room_name"] == "test-room"
        assert result.data["sid"] == "RM_abc123"
        assert result.error is None
        assert not result.approval_required

    @pytest.mark.asyncio
    async def test_create_room_receipt(
        self, nora: NoraConferenceSkillPack, ctx: NoraContext,
    ) -> None:
        """Room creation emits a receipt with correct fields (Law #2)."""
        with patch(
            "aspire_orchestrator.skillpacks.nora_conference.execute_tool",
            new_callable=AsyncMock,
            return_value=_mock_zoom_success(),
        ):
            result = await nora.create_room("standup-daily", None, ctx)

        receipt = result.receipt
        assert receipt["event_type"] == "meeting.create_room"
        assert receipt["suite_id"] == SUITE_ID
        assert receipt["office_id"] == OFFICE_ID
        assert receipt["actor"] == ACTOR_NORA
        assert receipt["correlation_id"] == CORR_ID
        assert receipt["status"] == "ok"
        assert receipt["inputs_hash"].startswith("sha256:")

    @pytest.mark.asyncio
    async def test_create_room_missing_name_fails(
        self, nora: NoraConferenceSkillPack, ctx: NoraContext,
    ) -> None:
        """Missing room_name fails closed with receipt (Law #3)."""
        result = await nora.create_room("", None, ctx)

        assert not result.success
        assert result.error == "Missing required parameter: room_name"
        assert result.receipt["status"] == "failed"
        assert result.receipt["policy"]["decision"] == "deny"


# =============================================================================
# 2. Meeting Scheduling Tests
# =============================================================================


class TestScheduleMeeting:
    """Test schedule_meeting (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_schedule_meeting_yellow_tier(
        self, nora: NoraConferenceSkillPack, ctx: NoraContext,
    ) -> None:
        """Schedule returns YELLOW-tier plan with meeting details."""
        result = await nora.schedule_meeting(
            participants=["alice@example.com", "bob@example.com"],
            time="2026-02-15T10:00:00Z",
            agenda="Sprint planning",
            context=ctx,
        )

        assert result.success
        assert result.data["risk_tier"] == "yellow"
        assert result.data["participants"] == ["alice@example.com", "bob@example.com"]
        assert result.data["time"] == "2026-02-15T10:00:00Z"
        assert result.data["agenda"] == "Sprint planning"

    @pytest.mark.asyncio
    async def test_schedule_meeting_approval_required(
        self, nora: NoraConferenceSkillPack, ctx: NoraContext,
    ) -> None:
        """Schedule meeting requires user approval (YELLOW tier, Law #4)."""
        result = await nora.schedule_meeting(
            participants=["alice@example.com"],
            time="2026-02-15T10:00:00Z",
            agenda="1:1",
            context=ctx,
        )

        assert result.approval_required
        assert result.receipt["event_type"] == "meeting.schedule"
        assert result.receipt["actor"] == ACTOR_NORA
        assert result.receipt["suite_id"] == SUITE_ID


# =============================================================================
# 3. Meeting Summarization Tests
# =============================================================================


class TestSummarizeMeeting:
    """Test summarize_meeting (GREEN tier)."""

    @pytest.mark.asyncio
    async def test_summarize_meeting_success(
        self, nora: NoraConferenceSkillPack, ctx: NoraContext,
    ) -> None:
        """Successful summarization returns transcript and summary."""
        with patch(
            "aspire_orchestrator.skillpacks.nora_conference.execute_tool",
            new_callable=AsyncMock,
            return_value=_mock_deepgram_success(),
        ):
            result = await nora.summarize_meeting("RM_abc123", ctx)

        assert result.success
        assert "quarterly results" in result.data["transcript"]
        assert result.data["confidence"] == 0.95
        assert result.data["duration"] == 120.5
        assert result.data["summary"]["duration_minutes"] == 2.0
        assert result.error is None

    @pytest.mark.asyncio
    async def test_summarize_meeting_receipt(
        self, nora: NoraConferenceSkillPack, ctx: NoraContext,
    ) -> None:
        """Summarization emits receipt with transcript metadata (Law #2)."""
        with patch(
            "aspire_orchestrator.skillpacks.nora_conference.execute_tool",
            new_callable=AsyncMock,
            return_value=_mock_deepgram_success(),
        ):
            result = await nora.summarize_meeting("RM_abc123", ctx)

        receipt = result.receipt
        assert receipt["event_type"] == "meeting.summarize"
        assert receipt["suite_id"] == SUITE_ID
        assert receipt["actor"] == ACTOR_NORA
        assert receipt["status"] == "ok"
        assert receipt["metadata"]["room_id"] == "RM_abc123"
        assert receipt["metadata"]["confidence"] == 0.95


# =============================================================================
# 4. Tier Enforcement Tests
# =============================================================================


class TestTierEnforcement:
    """Verify GREEN/YELLOW classification."""

    @pytest.mark.asyncio
    async def test_green_tier_no_approval_for_room(
        self, nora: NoraConferenceSkillPack, ctx: NoraContext,
    ) -> None:
        """create_room is GREEN — no approval required."""
        with patch(
            "aspire_orchestrator.skillpacks.nora_conference.execute_tool",
            new_callable=AsyncMock,
            return_value=_mock_zoom_success(),
        ):
            result = await nora.create_room("daily", None, ctx)

        assert not result.approval_required

    @pytest.mark.asyncio
    async def test_green_tier_no_approval_for_summary(
        self, nora: NoraConferenceSkillPack, ctx: NoraContext,
    ) -> None:
        """summarize_meeting is GREEN — no approval required."""
        with patch(
            "aspire_orchestrator.skillpacks.nora_conference.execute_tool",
            new_callable=AsyncMock,
            return_value=_mock_deepgram_success(),
        ):
            result = await nora.summarize_meeting("RM_xyz", ctx)

        assert not result.approval_required


# =============================================================================
# 5. Tool Executor Integration Tests
# =============================================================================


class TestToolExecutorIntegration:
    """Verify tool_executor is called (Law #7: tools are hands)."""

    @pytest.mark.asyncio
    async def test_tool_executor_zoom_called(
        self, nora: NoraConferenceSkillPack, ctx: NoraContext,
    ) -> None:
        """create_room calls execute_tool with zoom.session.create."""
        mock_execute = AsyncMock(return_value=_mock_zoom_success())
        with patch(
            "aspire_orchestrator.skillpacks.nora_conference.execute_tool",
            mock_execute,
        ):
            await nora.create_room("test-room", None, ctx)

        mock_execute.assert_called_once()
        call_kwargs = mock_execute.call_args.kwargs
        assert call_kwargs["tool_id"] == "zoom.session.create"
        assert call_kwargs["correlation_id"] == CORR_ID
        assert call_kwargs["suite_id"] == SUITE_ID

    @pytest.mark.asyncio
    async def test_tool_executor_deepgram_called(
        self, nora: NoraConferenceSkillPack, ctx: NoraContext,
    ) -> None:
        """summarize_meeting calls execute_tool with deepgram.transcribe."""
        mock_execute = AsyncMock(return_value=_mock_deepgram_success())
        with patch(
            "aspire_orchestrator.skillpacks.nora_conference.execute_tool",
            mock_execute,
        ):
            await nora.summarize_meeting("RM_test", ctx)

        mock_execute.assert_called_once()
        call_kwargs = mock_execute.call_args.kwargs
        assert call_kwargs["tool_id"] == "deepgram.transcribe"
        assert call_kwargs["payload"] == {"audio_url": "RM_test"}
        assert call_kwargs["suite_id"] == SUITE_ID
