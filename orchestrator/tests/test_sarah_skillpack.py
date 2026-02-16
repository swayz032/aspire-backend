"""Sarah Front Desk Skill Pack Tests — 15 tests covering call routing,
call transfer, and visitor logging.

Categories:
  1. Call routing (5 tests) — success, receipt, telephony policy, green tier, tool_executor
  2. Call transfer (5 tests) — yellow tier, approval_required, binding_fields, receipt, failure
  3. Visitor logging (5 tests) — success, receipt, green tier, data structure, no approval

Law compliance:
  - Law #2: Every test verifies receipt emission
  - Law #3: Missing params → fail-closed denial
  - Law #4: route_call/log_visitor=GREEN, transfer_call=YELLOW
  - Law #7: Tool calls go through tool_executor, not direct provider API
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.skillpacks.sarah_front_desk import (
    ACTOR_SARAH,
    SarahFrontDeskContext,
    SarahFrontDeskSkillPack,
)

# =============================================================================
# Fixtures
# =============================================================================

SUITE_ID = "suite-sarah-test-001"
OFFICE_ID = "office-sarah-test-001"
CORR_ID = "corr-sarah-test-001"


@pytest.fixture
def ctx() -> SarahFrontDeskContext:
    return SarahFrontDeskContext(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        correlation_id=CORR_ID,
    )


@pytest.fixture
def skill_pack() -> SarahFrontDeskSkillPack:
    return SarahFrontDeskSkillPack()


def _mock_tool_success(**kwargs) -> ToolExecutionResult:
    tool_id = kwargs.get("tool_id", "twilio.call.create")
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id=tool_id,
        data={
            "status": "success",
            "tool": tool_id,
            "routing_result": "reception",
        },
        receipt_data={"tool_id": tool_id},
    )


def _mock_tool_failure(**kwargs) -> ToolExecutionResult:
    tool_id = kwargs.get("tool_id", "twilio.call.create")
    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id=tool_id,
        error="Twilio API error: call not found",
        data={},
        receipt_data={"tool_id": tool_id},
    )


# =============================================================================
# 1. Call Routing Tests (5)
# =============================================================================


class TestRouteCall:
    @pytest.mark.asyncio
    async def test_route_call_success(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Call routing succeeds with valid caller info."""
        with patch(
            "aspire_orchestrator.skillpacks.sarah_front_desk.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_tool_success,
        ):
            result = await skill_pack.route_call(
                {"caller_number": "+15551234567", "caller_name": "John Doe"},
                ctx,
            )

        assert result.success
        assert result.receipt["event_type"] == "call.route"
        assert result.receipt["status"] == "ok"

    @pytest.mark.asyncio
    async def test_route_call_generates_receipt(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Receipt contains required fields (Law #2)."""
        with patch(
            "aspire_orchestrator.skillpacks.sarah_front_desk.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_tool_success,
        ):
            result = await skill_pack.route_call(
                {"caller_number": "+15551234567"},
                ctx,
            )

        receipt = result.receipt
        assert receipt["receipt_id"]
        assert receipt["suite_id"] == SUITE_ID
        assert receipt["office_id"] == OFFICE_ID
        assert receipt["correlation_id"] == CORR_ID
        assert receipt["actor"] == ACTOR_SARAH
        assert receipt["inputs_hash"].startswith("sha256:")
        assert receipt["policy"]["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_route_call_telephony_policy_enforced(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Forbidden topics in call context trigger deny (Law #3 + Law #9)."""
        result = await skill_pack.route_call(
            {
                "caller_number": "+15551234567",
                "context": "I need to give you my credit card number",
            },
            ctx,
        )

        assert not result.success
        assert result.error is not None
        assert "forbidden topic" in result.error.lower()
        assert result.receipt["policy"]["decision"] == "deny"
        assert "FORBIDDEN_TOPIC_DETECTED" in result.receipt["policy"]["reasons"]

    @pytest.mark.asyncio
    async def test_route_call_green_tier(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Call routing is GREEN tier — no approval required (Law #4)."""
        with patch(
            "aspire_orchestrator.skillpacks.sarah_front_desk.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_tool_success,
        ):
            result = await skill_pack.route_call(
                {"caller_number": "+15551234567"},
                ctx,
            )

        assert result.success
        assert result.receipt["risk_tier"] == "green"
        assert result.approval_required is False
        assert "approval_evidence" not in result.receipt

    @pytest.mark.asyncio
    async def test_route_call_tool_executor_called(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Call routing delegates to tool_executor (Law #7)."""
        mock = AsyncMock(side_effect=_mock_tool_success)
        with patch(
            "aspire_orchestrator.skillpacks.sarah_front_desk.execute_tool",
            mock,
        ):
            await skill_pack.route_call(
                {"caller_number": "+15551234567", "caller_name": "Jane"},
                ctx,
            )

        mock.assert_called_once()
        call_kwargs = mock.call_args.kwargs
        assert call_kwargs["tool_id"] == "twilio.call.create"
        assert call_kwargs["suite_id"] == SUITE_ID
        assert call_kwargs["risk_tier"] == "green"


# =============================================================================
# 2. Call Transfer Tests (5)
# =============================================================================


class TestTransferCall:
    @pytest.mark.asyncio
    async def test_transfer_call_yellow_tier(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Call transfer is YELLOW tier (Law #4)."""
        with patch(
            "aspire_orchestrator.skillpacks.sarah_front_desk.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_tool_success,
        ):
            result = await skill_pack.transfer_call(
                "call-123", "+15559876543", ctx,
            )

        assert result.receipt["risk_tier"] == "yellow"

    @pytest.mark.asyncio
    async def test_transfer_call_approval_required(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Call transfer requires user approval (YELLOW tier)."""
        with patch(
            "aspire_orchestrator.skillpacks.sarah_front_desk.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_tool_success,
        ):
            result = await skill_pack.transfer_call(
                "call-123", "+15559876543", ctx,
            )

        assert result.approval_required is True

    @pytest.mark.asyncio
    async def test_transfer_call_binding_fields(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Transfer receipt includes binding fields for approve-then-swap defense."""
        with patch(
            "aspire_orchestrator.skillpacks.sarah_front_desk.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_tool_success,
        ):
            result = await skill_pack.transfer_call(
                "call-123", "+15559876543", ctx,
            )

        receipt = result.receipt
        assert "approval_evidence" in receipt
        assert receipt["approval_evidence"]["binding_fields"]["call_id"] == "call-123"
        assert receipt["approval_evidence"]["binding_fields"]["destination"] == "+15559876543"
        assert receipt["approval_evidence"]["binding_hash"].startswith("sha256:")

    @pytest.mark.asyncio
    async def test_transfer_call_generates_receipt(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Transfer generates receipt with all required fields (Law #2)."""
        with patch(
            "aspire_orchestrator.skillpacks.sarah_front_desk.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_tool_success,
        ):
            result = await skill_pack.transfer_call(
                "call-456", "+15559876543", ctx,
            )

        receipt = result.receipt
        assert receipt["receipt_id"]
        assert receipt["event_type"] == "call.transfer"
        assert receipt["suite_id"] == SUITE_ID
        assert receipt["actor"] == ACTOR_SARAH

    @pytest.mark.asyncio
    async def test_transfer_call_failure(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Transfer failure still emits receipt with error status (Law #2)."""
        with patch(
            "aspire_orchestrator.skillpacks.sarah_front_desk.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_tool_failure,
        ):
            result = await skill_pack.transfer_call(
                "call-789", "+15559876543", ctx,
            )

        assert not result.success
        assert result.error is not None
        assert result.receipt["status"] == "failed"
        assert result.receipt["receipt_id"]


# =============================================================================
# 3. Visitor Logging Tests (5)
# =============================================================================


class TestLogVisitor:
    @pytest.mark.asyncio
    async def test_log_visitor_success(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Visitor logging succeeds with valid info."""
        with patch(
            "aspire_orchestrator.skillpacks.sarah_front_desk.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_tool_success,
        ):
            result = await skill_pack.log_visitor(
                {"name": "Alice Smith", "purpose": "Meeting", "company": "Acme Corp"},
                ctx,
            )

        assert result.success
        assert result.receipt["event_type"] == "visitor.log"
        assert result.receipt["status"] == "ok"

    @pytest.mark.asyncio
    async def test_log_visitor_generates_receipt(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Visitor logging generates receipt with required fields (Law #2)."""
        with patch(
            "aspire_orchestrator.skillpacks.sarah_front_desk.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_tool_success,
        ):
            result = await skill_pack.log_visitor(
                {"name": "Bob Jones"},
                ctx,
            )

        receipt = result.receipt
        assert receipt["receipt_id"]
        assert receipt["suite_id"] == SUITE_ID
        assert receipt["correlation_id"] == CORR_ID
        assert receipt["actor"] == ACTOR_SARAH
        assert receipt["inputs_hash"].startswith("sha256:")

    @pytest.mark.asyncio
    async def test_log_visitor_green_tier(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Visitor logging is GREEN tier — no approval required (Law #4)."""
        with patch(
            "aspire_orchestrator.skillpacks.sarah_front_desk.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_tool_success,
        ):
            result = await skill_pack.log_visitor(
                {"name": "Charlie Brown"},
                ctx,
            )

        assert result.receipt["risk_tier"] == "green"
        assert result.approval_required is False

    @pytest.mark.asyncio
    async def test_log_visitor_data_structure(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Visitor log entry contains expected data fields."""
        with patch(
            "aspire_orchestrator.skillpacks.sarah_front_desk.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_tool_success,
        ):
            result = await skill_pack.log_visitor(
                {
                    "name": "Dana White",
                    "purpose": "Delivery",
                    "phone": "+15551112222",
                    "company": "FedEx",
                },
                ctx,
            )

        data = result.data
        assert data["name"] == "Dana White"
        assert data["purpose"] == "Delivery"
        assert data["company"] == "FedEx"
        assert data["suite_id"] == SUITE_ID
        assert data["office_id"] == OFFICE_ID
        assert "visitor_id" in data
        assert "logged_at" in data

    @pytest.mark.asyncio
    async def test_log_visitor_missing_name_denied(
        self, skill_pack: SarahFrontDeskSkillPack, ctx: SarahFrontDeskContext,
    ) -> None:
        """Missing visitor name fails closed with receipt (Law #3)."""
        result = await skill_pack.log_visitor(
            {"purpose": "Meeting"},
            ctx,
        )

        assert not result.success
        assert result.error is not None
        assert result.receipt["policy"]["decision"] == "deny"
        assert "MISSING_VISITOR_NAME" in result.receipt["policy"]["reasons"]
        assert result.approval_required is False
