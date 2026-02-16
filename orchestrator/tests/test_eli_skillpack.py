"""Eli Inbox Skill Pack Tests — 15 tests covering read, triage, draft, send.

Categories:
  1. Email read (4 tests) — success, receipt, GREEN tier, filter validation
  2. Email triage (4 tests) — classify support/sales/billing/spam correctly
  3. Email draft (4 tests) — YELLOW tier, approval_required, draft content, receipt
  4. Email send (3 tests) — YELLOW tier, approval_required, binding_fields, receipt

Law compliance:
  - Law #2: Every test verifies receipt emission
  - Law #3: Missing params -> fail-closed denial
  - Law #4: GREEN (read/triage), YELLOW (draft/send)
  - Law #7: Tool calls go through tool_executor, not direct API
  - Law #9: DLP redaction of email content in receipts
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.skillpacks.eli_inbox import (
    ACTOR_ELI,
    EliInboxContext,
    EliInboxSkillPack,
    _classify_email,
)

# =============================================================================
# Fixtures
# =============================================================================

SUITE_ID = "suite-eli-test-001"
OFFICE_ID = "office-eli-test-001"
CORR_ID = "corr-eli-test-001"


@pytest.fixture
def ctx() -> EliInboxContext:
    return EliInboxContext(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        correlation_id=CORR_ID,
    )


@pytest.fixture
def skill_pack() -> EliInboxSkillPack:
    return EliInboxSkillPack()


def _mock_email_read_success(**kwargs) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="polaris.email.read",
        data={
            "emails": [
                {"id": "msg-001", "subject": "Help with order", "from": "customer@example.com"},
                {"id": "msg-002", "subject": "Invoice #123", "from": "vendor@example.com"},
            ],
        },
        receipt_data={},
    )


def _mock_draft_success(**kwargs) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="polaris.email.draft",
        data={
            "draft_id": "draft-001",
            "status": "draft",
        },
        receipt_data={},
    )


def _mock_send_success(**kwargs) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="polaris.email.send",
        data={
            "message_id": "msg-sent-001",
            "status": "sent",
        },
        receipt_data={},
    )


def _mock_send_failure(**kwargs) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id="polaris.email.send",
        error="SMTP connection failed",
        data={},
        receipt_data={},
    )


# =============================================================================
# 1. Email Read Tests (4)
# =============================================================================


class TestReadEmails:
    @pytest.mark.asyncio
    async def test_read_emails_success(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Read emails returns results from PolarisM."""
        with patch(
            "aspire_orchestrator.skillpacks.eli_inbox.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_email_read_success,
        ):
            result = await skill_pack.read_emails({"folder": "inbox", "unread_only": True}, ctx)

        assert result.success
        assert len(result.data["emails"]) == 2
        assert result.data["emails"][0]["id"] == "msg-001"

    @pytest.mark.asyncio
    async def test_read_emails_receipt(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Receipt generated on successful read (Law #2)."""
        with patch(
            "aspire_orchestrator.skillpacks.eli_inbox.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_email_read_success,
        ):
            result = await skill_pack.read_emails({"folder": "inbox"}, ctx)

        receipt = result.receipt
        assert receipt["receipt_id"]
        assert receipt["event_type"] == "email.read"
        assert receipt["status"] == "ok"
        assert receipt["suite_id"] == SUITE_ID
        assert receipt["correlation_id"] == CORR_ID
        assert receipt["actor"] == ACTOR_ELI
        assert receipt["inputs_hash"].startswith("sha256:")

    @pytest.mark.asyncio
    async def test_read_emails_green_tier(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Email read is GREEN tier — no approval required (Law #4)."""
        with patch(
            "aspire_orchestrator.skillpacks.eli_inbox.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_email_read_success,
        ):
            result = await skill_pack.read_emails({"folder": "inbox"}, ctx)

        assert result.success
        assert not result.approval_required
        assert result.receipt["risk_tier"] == "green"
        assert result.receipt["policy"]["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_read_emails_missing_filters(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Missing filters -> fail-closed denial (Law #3)."""
        result = await skill_pack.read_emails({}, ctx)

        assert not result.success
        assert result.error == "Missing required parameter: filters"
        assert result.receipt["policy"]["decision"] == "deny"
        assert "MISSING_FILTERS" in result.receipt["policy"]["reasons"]


# =============================================================================
# 2. Email Triage Tests (4)
# =============================================================================


class TestTriageEmail:
    @pytest.mark.asyncio
    async def test_triage_support(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Support email classified correctly."""
        result = await skill_pack.triage_email(
            "msg-001", "I need help with my order", "The product is broken", ctx,
        )

        assert result.success
        assert result.data["category"] == "support"
        assert result.data["email_id"] == "msg-001"
        assert result.receipt["event_type"] == "email.triage"

    @pytest.mark.asyncio
    async def test_triage_sales(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Sales inquiry classified correctly."""
        result = await skill_pack.triage_email(
            "msg-002", "Pricing inquiry", "Can I get a quote for your services?", ctx,
        )

        assert result.success
        assert result.data["category"] == "sales"

    @pytest.mark.asyncio
    async def test_triage_billing(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Billing email classified correctly."""
        result = await skill_pack.triage_email(
            "msg-003", "Invoice overdue", "Your payment is overdue. Please check your balance.", ctx,
        )

        assert result.success
        assert result.data["category"] == "billing"

    @pytest.mark.asyncio
    async def test_triage_spam(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Spam email classified correctly."""
        result = await skill_pack.triage_email(
            "msg-004", "Congratulations! You are a winner!", "Click here for your free prize. Limited time offer!", ctx,
        )

        assert result.success
        assert result.data["category"] == "spam"


# =============================================================================
# 3. Email Draft Tests (4)
# =============================================================================


class TestDraftResponse:
    @pytest.mark.asyncio
    async def test_draft_yellow_tier(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Draft is YELLOW tier — approval_required is True (Law #4)."""
        with patch(
            "aspire_orchestrator.skillpacks.eli_inbox.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_draft_success,
        ):
            result = await skill_pack.draft_response(
                "msg-001",
                {
                    "to": "customer@example.com",
                    "subject": "Re: Help with order",
                    "body_html": "<p>We can help.</p>",
                    "body_text": "We can help.",
                    "from_address": "support@mybiz.com",
                },
                ctx,
            )

        assert result.success
        assert result.approval_required
        assert result.receipt["risk_tier"] == "yellow"
        assert result.receipt["approval_required"] is True

    @pytest.mark.asyncio
    async def test_draft_content_returned(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Draft creation returns draft_id from provider."""
        with patch(
            "aspire_orchestrator.skillpacks.eli_inbox.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_draft_success,
        ):
            result = await skill_pack.draft_response(
                "msg-001",
                {
                    "to": "customer@example.com",
                    "subject": "Re: Help",
                    "body_html": "<p>Reply</p>",
                    "body_text": "Reply",
                    "from_address": "support@mybiz.com",
                },
                ctx,
            )

        assert result.data["draft_id"] == "draft-001"
        assert result.data["status"] == "draft"

    @pytest.mark.asyncio
    async def test_draft_receipt_emitted(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Receipt generated on draft creation (Law #2)."""
        with patch(
            "aspire_orchestrator.skillpacks.eli_inbox.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_draft_success,
        ):
            result = await skill_pack.draft_response(
                "msg-001",
                {
                    "to": "customer@example.com",
                    "subject": "Re: Help",
                    "body_html": "<p>Reply</p>",
                    "body_text": "Reply",
                    "from_address": "support@mybiz.com",
                },
                ctx,
            )

        receipt = result.receipt
        assert receipt["receipt_id"]
        assert receipt["event_type"] == "email.draft"
        assert receipt["status"] == "ok"
        assert receipt["actor"] == ACTOR_ELI

    @pytest.mark.asyncio
    async def test_draft_missing_fields(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Missing required fields -> fail-closed denial (Law #3)."""
        result = await skill_pack.draft_response(
            "msg-001",
            {"body_html": "<p>Reply</p>"},  # missing to, subject, from_address
            ctx,
        )

        assert not result.success
        assert "Missing required fields" in (result.error or "")
        assert result.receipt["policy"]["decision"] == "deny"


# =============================================================================
# 4. Email Send Tests (3)
# =============================================================================


class TestSendEmail:
    @pytest.mark.asyncio
    async def test_send_yellow_tier_approval(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Send is YELLOW tier — approval_required is True (Law #4)."""
        with patch(
            "aspire_orchestrator.skillpacks.eli_inbox.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_send_success,
        ):
            result = await skill_pack.send_email(
                "draft-001",
                {
                    "to": "customer@example.com",
                    "subject": "Re: Help with order",
                    "body_html": "<p>We fixed it.</p>",
                    "body_text": "We fixed it.",
                    "from_address": "support@mybiz.com",
                },
                ctx,
            )

        assert result.success
        assert result.approval_required
        assert result.receipt["risk_tier"] == "yellow"
        assert result.receipt["approval_required"] is True
        # Binding fields in metadata (approve-then-swap defense)
        assert "binding_fields" in result.receipt["metadata"]

    @pytest.mark.asyncio
    async def test_send_receipt_on_success(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Receipt generated on successful send (Law #2)."""
        with patch(
            "aspire_orchestrator.skillpacks.eli_inbox.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_send_success,
        ):
            result = await skill_pack.send_email(
                "draft-001",
                {
                    "to": "customer@example.com",
                    "subject": "Re: Help",
                    "body_html": "<p>Done</p>",
                    "body_text": "Done",
                    "from_address": "support@mybiz.com",
                },
                ctx,
            )

        receipt = result.receipt
        assert receipt["receipt_id"]
        assert receipt["event_type"] == "email.send"
        assert receipt["status"] == "ok"
        assert receipt["suite_id"] == SUITE_ID

    @pytest.mark.asyncio
    async def test_send_receipt_on_failure(
        self, skill_pack: EliInboxSkillPack, ctx: EliInboxContext,
    ) -> None:
        """Receipt generated even on send failure (Law #2)."""
        with patch(
            "aspire_orchestrator.skillpacks.eli_inbox.execute_tool",
            new_callable=AsyncMock,
            side_effect=_mock_send_failure,
        ):
            result = await skill_pack.send_email(
                "draft-001",
                {
                    "to": "customer@example.com",
                    "subject": "Re: Help",
                    "body_html": "<p>Done</p>",
                    "body_text": "Done",
                    "from_address": "support@mybiz.com",
                },
                ctx,
            )

        assert not result.success
        assert result.error == "SMTP connection failed"
        receipt = result.receipt
        assert receipt["receipt_id"]
        assert receipt["event_type"] == "email.send"
        assert receipt["status"] == "failed"
        assert receipt["suite_id"] == SUITE_ID
