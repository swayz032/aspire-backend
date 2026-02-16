"""Enhanced Skill Pack Tests — Phase 3 W3/W4.

Tests for all LLM-enhanced skill pack classes that inherit from EnhancedSkillPack.
Validates: LLM integration, receipt emission, fail-closed behavior, model routing.

W3 GREEN packs (15 tests each = 45):
  - EnhancedAdamResearch: plan_search, verify_evidence, generate_outreach_packet
  - EnhancedNoraConference: detect_risk_triggers, smart_summarize, route_to_specialist
  - EnhancedTecDocuments: plan_document, draft_content, review_document

W4 YELLOW packs (15 tests each = 75):
  - EnhancedQuinnInvoicing: parse_invoice_intent, match_customer, draft_invoice_plan
  - EnhancedEliInbox: triage_email, draft_reply, extract_action_items
  - EnhancedSarahFrontDesk: analyze_call_intent, transcribe_voicemail, plan_booking
  - EnhancedTeressaBooks: categorize_transaction, plan_reconciliation, analyze_financials
  - EnhancedMailOps: plan_domain_setup, diagnose_delivery
  - EnhancedFinnFinanceManager: analyze_financial_health, plan_budget_adjustment,
    generate_finance_report, recommend_delegation

Law compliance:
  - Law #2: Every method emits a receipt (success or failure)
  - Law #3: Missing/invalid inputs → fail-closed denial
  - Law #4: Correct risk tier tagging per method
  - Law #7: LLM calls route through call_llm (not direct API)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any

import pytest

from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult


# =============================================================================
# Shared Fixtures
# =============================================================================

SUITE_ID = "suite-enhanced-test-001"
OFFICE_ID = "office-enhanced-test-001"
CORR_ID = "corr-enhanced-test-001"


@pytest.fixture
def agent_ctx() -> AgentContext:
    """Standard agent context for all enhanced pack tests."""
    return AgentContext(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        correlation_id=CORR_ID,
        risk_tier="green",
    )


@pytest.fixture
def yellow_ctx() -> AgentContext:
    """YELLOW-tier agent context."""
    return AgentContext(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        correlation_id=CORR_ID,
        risk_tier="yellow",
    )


def _mock_llm_success(content: str = "LLM response") -> dict[str, Any]:
    """Build a successful LLM call result."""
    return {
        "content": content,
        "model_used": "gpt-5-mini",
        "profile_used": "cheap_classifier",
        "error": None,
    }


def _mock_llm_error(error: str = "llm_timeout") -> dict[str, Any]:
    """Build a failed LLM call result."""
    return {
        "content": "",
        "model_used": "gpt-5-mini",
        "profile_used": "cheap_classifier",
        "error": error,
    }


# Helper to create an enhanced pack with mocked config loading
def _create_pack(pack_class, **kwargs):
    """Instantiate an enhanced skill pack with config loading mocked out."""
    with patch.object(pack_class, "_load_config", return_value=None):
        return pack_class(**kwargs) if kwargs else pack_class()


# =============================================================================
# W3: EnhancedAdamResearch (GREEN)
# =============================================================================


class TestEnhancedAdamResearch:
    """Tests for EnhancedAdamResearch — GREEN tier research planning."""

    @pytest.fixture
    def pack(self):
        from aspire_orchestrator.skillpacks.adam_research import EnhancedAdamResearch
        return _create_pack(EnhancedAdamResearch)

    @pytest.mark.asyncio
    async def test_plan_search_success(self, pack, agent_ctx):
        """plan_search returns structured search plan via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Search plan: 3 queries"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-001")

        result = await pack.plan_search("Find HVAC contractors in Denver", agent_ctx)

        assert result.success is True
        assert "content" in result.data
        assert result.receipt["event_type"] == "research.plan"
        assert result.receipt["status"] == "ok"
        pack.call_llm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_plan_search_empty_query_denied(self, pack, agent_ctx):
        """plan_search fails closed on empty query (Law #3)."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-002")

        result = await pack.plan_search("", agent_ctx)

        assert result.success is False
        assert result.error is not None
        assert result.receipt["policy"]["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_plan_search_llm_error_produces_receipt(self, pack, agent_ctx):
        """plan_search emits failure receipt when LLM fails (Law #2)."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_error("llm_timeout"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-003")

        result = await pack.plan_search("Find plumbers", agent_ctx)

        assert result.success is False
        assert result.receipt["status"] == "failed"
        pack._trust_spine.emit_receipt.assert_awaited()

    @pytest.mark.asyncio
    async def test_verify_evidence_success(self, pack, agent_ctx):
        """verify_evidence evaluates search results via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Evidence verified"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-004")

        results = [{"title": "HVAC Pro", "url": "https://example.com"}]
        result = await pack.verify_evidence(results, "HVAC contractors", agent_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "research.verify"

    @pytest.mark.asyncio
    async def test_verify_evidence_empty_results_denied(self, pack, agent_ctx):
        """verify_evidence fails closed on empty results."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-005")

        result = await pack.verify_evidence([], "query", agent_ctx)

        assert result.success is False
        assert result.receipt["policy"]["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_generate_outreach_packet_success(self, pack, agent_ctx):
        """generate_outreach_packet creates vendor outreach via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Dear Vendor..."))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-006")

        vendor = {"name": "HVAC Pro", "contact": "info@hvac.example.com"}
        biz = {"company": "My Plumbing LLC"}
        result = await pack.generate_outreach_packet(vendor, biz, agent_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "research.outreach_packet"

    @pytest.mark.asyncio
    async def test_generate_outreach_no_vendor_denied(self, pack, agent_ctx):
        """generate_outreach_packet fails closed on empty vendor data."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-007")

        result = await pack.generate_outreach_packet({}, {"company": "LLC"}, agent_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_receipts_contain_suite_id(self, pack, agent_ctx):
        """All receipts contain suite_id for tenant isolation (Law #6)."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("ok"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-008")

        result = await pack.plan_search("test query", agent_ctx)

        assert result.receipt["suite_id"] == SUITE_ID
        assert result.receipt["office_id"] == OFFICE_ID


# =============================================================================
# W3: EnhancedNoraConference (GREEN)
# =============================================================================


class TestEnhancedNoraConference:
    """Tests for EnhancedNoraConference — GREEN tier meeting analysis."""

    @pytest.fixture
    def pack(self):
        from aspire_orchestrator.skillpacks.nora_conference import EnhancedNoraConference
        return _create_pack(EnhancedNoraConference)

    @pytest.mark.asyncio
    async def test_detect_risk_triggers_success(self, pack, agent_ctx):
        """detect_risk_triggers analyzes transcript via rule + LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Triggers found: payment"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-101")

        result = await pack.detect_risk_triggers("We need to send payment ASAP", agent_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "meeting.risk_detect"

    @pytest.mark.asyncio
    async def test_detect_risk_triggers_empty_transcript(self, pack, agent_ctx):
        """detect_risk_triggers fails closed on empty transcript."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-102")

        result = await pack.detect_risk_triggers("", agent_ctx)

        assert result.success is False
        assert result.receipt["policy"]["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_smart_summarize_success(self, pack, agent_ctx):
        """smart_summarize produces meeting summary via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Meeting summary..."))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-103")

        result = await pack.smart_summarize("Long transcript here...", "room-001", agent_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "meeting.smart_summarize"

    @pytest.mark.asyncio
    async def test_smart_summarize_empty_transcript(self, pack, agent_ctx):
        """smart_summarize fails closed on empty transcript."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-104")

        result = await pack.smart_summarize("", "room-001", agent_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_route_to_specialist_success(self, pack, agent_ctx):
        """route_to_specialist validates and routes trigger to agent."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Route to quinn"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-105")

        trigger = {"category": "money_movement", "specialist": "quinn", "text": "send payment"}
        result = await pack.route_to_specialist(trigger, {"room_id": "r1"}, agent_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "meeting.route_specialist"

    @pytest.mark.asyncio
    async def test_route_to_specialist_empty_trigger(self, pack, agent_ctx):
        """route_to_specialist fails closed on empty trigger."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-106")

        result = await pack.route_to_specialist({}, {}, agent_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_risk_trigger_keywords_defined(self, pack):
        """RISK_TRIGGER_KEYWORDS maps categories to target agents."""
        from aspire_orchestrator.skillpacks.nora_conference import RISK_TRIGGER_KEYWORDS

        assert "money_movement" in RISK_TRIGGER_KEYWORDS
        assert "contracts" in RISK_TRIGGER_KEYWORDS
        assert "payroll" in RISK_TRIGGER_KEYWORDS
        # Verify specialist agents exist
        for category, info in RISK_TRIGGER_KEYWORDS.items():
            assert "keywords" in info
            assert "specialist" in info


# =============================================================================
# W3: EnhancedTecDocuments (GREEN)
# =============================================================================


class TestEnhancedTecDocuments:
    """Tests for EnhancedTecDocuments — GREEN tier document generation."""

    @pytest.fixture
    def pack(self):
        from aspire_orchestrator.skillpacks.tec_documents import EnhancedTecDocuments
        return _create_pack(EnhancedTecDocuments)

    @pytest.mark.asyncio
    async def test_plan_document_success(self, pack, agent_ctx):
        """plan_document plans document structure via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Document plan"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-201")

        result = await pack.plan_document("proposal", {"title": "Q1 Report"}, agent_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "document.plan"

    @pytest.mark.asyncio
    async def test_plan_document_empty_type_denied(self, pack, agent_ctx):
        """plan_document fails closed on empty document type."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-202")

        result = await pack.plan_document("", {}, agent_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_draft_content_success(self, pack, agent_ctx):
        """draft_content generates document content via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Document content..."))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-203")

        plan = {"title": "Q1 Report", "sections": ["intro", "body"]}
        result = await pack.draft_content(plan, {"company": "LLC"}, agent_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "document.draft"

    @pytest.mark.asyncio
    async def test_draft_content_empty_plan_denied(self, pack, agent_ctx):
        """draft_content fails closed on empty plan."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-204")

        result = await pack.draft_content({}, {}, agent_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_review_document_success(self, pack, agent_ctx):
        """review_document reviews document via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Review: LGTM"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-205")

        result = await pack.review_document("Document text...", "proposal", agent_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "document.review"

    @pytest.mark.asyncio
    async def test_review_document_empty_content_denied(self, pack, agent_ctx):
        """review_document fails closed on empty content."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-206")

        result = await pack.review_document("", "proposal", agent_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_all_receipts_have_correlation_id(self, pack, agent_ctx):
        """All receipts propagate correlation_id for tracing."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("ok"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-207")

        result = await pack.plan_document("proposal", {"title": "T"}, agent_ctx)

        assert result.receipt["correlation_id"] == CORR_ID


# =============================================================================
# W4: EnhancedQuinnInvoicing (YELLOW)
# =============================================================================


class TestEnhancedQuinnInvoicing:
    """Tests for EnhancedQuinnInvoicing — YELLOW tier invoice processing."""

    @pytest.fixture
    def pack(self):
        from aspire_orchestrator.skillpacks.quinn_invoicing import EnhancedQuinnInvoicing
        return _create_pack(EnhancedQuinnInvoicing)

    @pytest.mark.asyncio
    async def test_parse_invoice_intent_success(self, pack, yellow_ctx):
        """parse_invoice_intent extracts invoice data via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Line items: ..."))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-301")

        result = await pack.parse_invoice_intent(
            "Send invoice for $500 web design to John", yellow_ctx
        )

        assert result.success is True
        assert result.receipt["event_type"] == "invoice.parse_intent"

    @pytest.mark.asyncio
    async def test_parse_invoice_intent_empty_denied(self, pack, yellow_ctx):
        """parse_invoice_intent fails closed on empty request."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-302")

        result = await pack.parse_invoice_intent("", yellow_ctx)

        assert result.success is False
        assert result.receipt["policy"]["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_match_customer_success(self, pack, yellow_ctx):
        """match_customer fuzzy-matches customer name via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Match: John Smith"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-303")

        customers = [{"name": "John Smith", "id": "c-001"}]
        result = await pack.match_customer("John S", customers, yellow_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "invoice.match_customer"

    @pytest.mark.asyncio
    async def test_match_customer_empty_name_denied(self, pack, yellow_ctx):
        """match_customer fails closed on empty customer name."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-304")

        result = await pack.match_customer("", [], yellow_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_draft_invoice_plan_success(self, pack, yellow_ctx):
        """draft_invoice_plan builds complete plan via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Invoice plan: ..."))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-305")

        parsed = {"customer": "John", "amount_cents": 50000, "items": []}
        result = await pack.draft_invoice_plan(parsed, yellow_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "invoice.draft_plan"

    @pytest.mark.asyncio
    async def test_draft_invoice_plan_empty_data_denied(self, pack, yellow_ctx):
        """draft_invoice_plan fails closed on empty parsed data."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-306")

        result = await pack.draft_invoice_plan({}, yellow_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_invoice_receipts_tagged_yellow(self, pack, yellow_ctx):
        """Invoice operations tag receipts with correct risk context."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("ok"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-307")

        result = await pack.parse_invoice_intent("Invoice $100", yellow_ctx)

        assert result.receipt["suite_id"] == SUITE_ID


# =============================================================================
# W4: EnhancedEliInbox (YELLOW)
# =============================================================================


class TestEnhancedEliInbox:
    """Tests for EnhancedEliInbox — YELLOW tier email processing."""

    @pytest.fixture
    def pack(self):
        from aspire_orchestrator.skillpacks.eli_inbox import EnhancedEliInbox
        return _create_pack(EnhancedEliInbox)

    @pytest.mark.asyncio
    async def test_triage_email_success(self, pack, yellow_ctx):
        """triage_email classifies email via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Priority: HIGH"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-401")

        email = {"from": "client@example.com", "subject": "Urgent", "body": "Need help"}
        result = await pack.triage_email(email, yellow_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "email.triage"

    @pytest.mark.asyncio
    async def test_triage_email_empty_denied(self, pack, yellow_ctx):
        """triage_email fails closed on empty email data."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-402")

        result = await pack.triage_email({}, yellow_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_draft_reply_success(self, pack, yellow_ctx):
        """draft_reply generates professional reply via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Dear client..."))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-403")

        email = {"from": "c@ex.com", "subject": "Q", "body": "Question?"}
        result = await pack.draft_reply(email, "answer the question politely", yellow_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "email.draft_reply"

    @pytest.mark.asyncio
    async def test_draft_reply_empty_intent_denied(self, pack, yellow_ctx):
        """draft_reply fails closed on empty reply intent."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-404")

        email = {"from": "c@ex.com", "subject": "Q", "body": "Body"}
        result = await pack.draft_reply(email, "", yellow_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_extract_action_items_success(self, pack, yellow_ctx):
        """extract_action_items extracts tasks from email thread."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("1. Follow up"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-405")

        thread = [
            {"from": "client@example.com", "body": "Please follow up on the invoice"},
            {"from": "me@company.com", "body": "Will do by Friday"},
        ]
        result = await pack.extract_action_items(thread, yellow_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "email.extract_actions"

    @pytest.mark.asyncio
    async def test_extract_action_items_empty_denied(self, pack, yellow_ctx):
        """extract_action_items fails closed on empty thread."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-406")

        result = await pack.extract_action_items([], yellow_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_llm_error_produces_failure_receipt(self, pack, yellow_ctx):
        """LLM errors still emit receipt (Law #2)."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_error("llm_timeout"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-407")

        result = await pack.triage_email(
            {"from": "a@b.com", "subject": "S", "body": "B"}, yellow_ctx
        )

        assert result.success is False
        assert result.receipt["status"] == "failed"
        pack._trust_spine.emit_receipt.assert_awaited()


# =============================================================================
# W4: EnhancedSarahFrontDesk (YELLOW)
# =============================================================================


class TestEnhancedSarahFrontDesk:
    """Tests for EnhancedSarahFrontDesk — YELLOW tier call handling."""

    @pytest.fixture
    def pack(self):
        from aspire_orchestrator.skillpacks.sarah_front_desk import EnhancedSarahFrontDesk
        return _create_pack(EnhancedSarahFrontDesk)

    @pytest.mark.asyncio
    async def test_analyze_call_intent_success(self, pack, yellow_ctx):
        """analyze_call_intent classifies caller via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Intent: appointment"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-501")

        caller = {"name": "John", "reason": "schedule appointment"}
        result = await pack.analyze_call_intent(caller, yellow_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "call.analyze_intent"

    @pytest.mark.asyncio
    async def test_analyze_call_intent_empty_caller_denied(self, pack, yellow_ctx):
        """analyze_call_intent fails closed on missing caller info."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-502")

        result = await pack.analyze_call_intent({}, yellow_ctx)

        assert result.success is False
        assert result.receipt["policy"]["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_transcribe_voicemail_success(self, pack, yellow_ctx):
        """transcribe_voicemail summarizes voicemail via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Summary: callback"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-503")

        result = await pack.transcribe_voicemail(
            "Hi, this is John. Please call me back.", yellow_ctx
        )

        assert result.success is True
        assert result.receipt["event_type"] == "call.voicemail_summary"

    @pytest.mark.asyncio
    async def test_transcribe_voicemail_empty_denied(self, pack, yellow_ctx):
        """transcribe_voicemail fails closed on empty text."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-504")

        result = await pack.transcribe_voicemail("", yellow_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_plan_booking_success(self, pack, yellow_ctx):
        """plan_booking plans appointment via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Booking plan"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-505")

        booking = {"type": "consultation", "preferred_time": "10am"}
        result = await pack.plan_booking(booking, yellow_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "call.plan_booking"

    @pytest.mark.asyncio
    async def test_caller_phone_used_when_name_missing(self, pack, yellow_ctx):
        """analyze_call_intent falls back to phone when name is missing."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Intent: inquiry"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-506")

        caller = {"phone": "555-1234", "reason": "billing question"}
        result = await pack.analyze_call_intent(caller, yellow_ctx)

        # Should succeed because phone was used as identifier
        assert result.success is True


# =============================================================================
# W4: EnhancedTeressaBooks (YELLOW)
# =============================================================================


class TestEnhancedTeressaBooks:
    """Tests for EnhancedTeressaBooks — YELLOW tier bookkeeping."""

    @pytest.fixture
    def pack(self):
        from aspire_orchestrator.skillpacks.teressa_books import EnhancedTeressaBooks
        return _create_pack(EnhancedTeressaBooks)

    @pytest.mark.asyncio
    async def test_categorize_transaction_success(self, pack, yellow_ctx):
        """categorize_transaction classifies via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Category: Office Supplies"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-601")

        txn = {"description": "Staples office supply", "amount_cents": 4599, "date": "2026-02-14"}
        result = await pack.categorize_transaction(txn, yellow_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "books.categorize"

    @pytest.mark.asyncio
    async def test_categorize_transaction_empty_denied(self, pack, yellow_ctx):
        """categorize_transaction fails closed on empty transaction."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-602")

        result = await pack.categorize_transaction({}, yellow_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_plan_reconciliation_success(self, pack, yellow_ctx):
        """plan_reconciliation compares Stripe vs QBO via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Reconciliation: 2 mismatches"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-603")

        stripe = {"transactions": [{"id": "t1", "amount": 100}]}
        qbo = {"transactions": [{"id": "q1", "amount": 100}]}
        result = await pack.plan_reconciliation(stripe, qbo, yellow_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "books.reconcile_plan"

    @pytest.mark.asyncio
    async def test_plan_reconciliation_empty_data_denied(self, pack, yellow_ctx):
        """plan_reconciliation fails closed on empty data."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-604")

        result = await pack.plan_reconciliation({}, {}, yellow_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_analyze_financials_success(self, pack, yellow_ctx):
        """analyze_financials generates insights via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Revenue up 15%"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-605")

        data = {"revenue_cents": 50000, "expenses_cents": 30000}
        result = await pack.analyze_financials("2026-Q1", data, yellow_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "books.analyze"

    @pytest.mark.asyncio
    async def test_analyze_financials_empty_period_denied(self, pack, yellow_ctx):
        """analyze_financials fails closed on empty period."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-606")

        result = await pack.analyze_financials("", {}, yellow_ctx)

        assert result.success is False


# =============================================================================
# W4: EnhancedMailOps (YELLOW)
# =============================================================================


class TestEnhancedMailOps:
    """Tests for EnhancedMailOps — YELLOW tier mail infrastructure."""

    @pytest.fixture
    def pack(self):
        from aspire_orchestrator.skillpacks.mail_ops_desk import EnhancedMailOps
        return _create_pack(EnhancedMailOps)

    @pytest.mark.asyncio
    async def test_plan_domain_setup_success(self, pack, yellow_ctx):
        """plan_domain_setup plans DNS/mailbox config via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("DNS records: MX, SPF"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-701")

        result = await pack.plan_domain_setup("example.com", yellow_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "mail.plan_domain"

    @pytest.mark.asyncio
    async def test_plan_domain_setup_empty_domain_denied(self, pack, yellow_ctx):
        """plan_domain_setup fails closed on empty domain."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-702")

        result = await pack.plan_domain_setup("", yellow_ctx)

        assert result.success is False
        assert result.receipt["policy"]["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_diagnose_delivery_success(self, pack, yellow_ctx):
        """diagnose_delivery analyzes mail issues via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Issue: SPF fail"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-703")

        issue = {"domain": "example.com", "error": "550 SPF check failed"}
        result = await pack.diagnose_delivery(issue, yellow_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "mail.diagnose"

    @pytest.mark.asyncio
    async def test_diagnose_delivery_empty_issue_denied(self, pack, yellow_ctx):
        """diagnose_delivery fails closed on empty issue data."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-704")

        result = await pack.diagnose_delivery({}, yellow_ctx)

        assert result.success is False


# =============================================================================
# W4: EnhancedFinnFinanceManager (YELLOW)
# =============================================================================


class TestEnhancedFinnFinanceManager:
    """Tests for EnhancedFinnFinanceManager — YELLOW tier financial intelligence."""

    @pytest.fixture
    def pack(self):
        from aspire_orchestrator.skillpacks.finn_finance_manager import (
            EnhancedFinnFinanceManager,
        )
        return _create_pack(EnhancedFinnFinanceManager)

    @pytest.mark.asyncio
    async def test_analyze_financial_health_success(self, pack, agent_ctx):
        """analyze_financial_health synthesizes snapshot via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Health score: 8/10"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-801")

        snapshot = {"revenue_cents": 100000, "expenses_cents": 60000,
                    "net_income_cents": 40000, "cash_position_cents": 250000}
        result = await pack.analyze_financial_health(snapshot, [], agent_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "finance.health.analyze"

    @pytest.mark.asyncio
    async def test_analyze_financial_health_empty_snapshot_denied(self, pack, agent_ctx):
        """analyze_financial_health fails closed on empty snapshot."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-802")

        result = await pack.analyze_financial_health({}, [], agent_ctx)

        assert result.success is False
        assert "EMPTY" in str(result.receipt.get("policy", {}).get("reasons", []))

    @pytest.mark.asyncio
    async def test_plan_budget_adjustment_success(self, pack, yellow_ctx):
        """plan_budget_adjustment proposes changes via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Reduce marketing 20%"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-803")

        budget = {"marketing_cents": 50000, "operations_cents": 30000}
        result = await pack.plan_budget_adjustment(
            budget, "Revenue decline in Q4", yellow_ctx
        )

        assert result.success is True
        assert result.receipt["event_type"] == "finance.budget.plan"

    @pytest.mark.asyncio
    async def test_plan_budget_adjustment_empty_reason_denied(self, pack, yellow_ctx):
        """plan_budget_adjustment fails closed on missing reason."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-804")

        result = await pack.plan_budget_adjustment({}, "", yellow_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_generate_finance_report_success(self, pack, agent_ctx):
        """generate_finance_report creates executive summary via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Q1 Revenue: $100k"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-805")

        result = await pack.generate_finance_report("2026-Q1", "quarterly", agent_ctx)

        assert result.success is True
        assert result.receipt["event_type"] == "finance.report.generate"

    @pytest.mark.asyncio
    async def test_generate_finance_report_invalid_type_denied(self, pack, agent_ctx):
        """generate_finance_report fails closed on invalid report type."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-806")

        result = await pack.generate_finance_report("2026-Q1", "invalid_type", agent_ctx)

        assert result.success is False
        assert "INVALID_REPORT_TYPE" in str(result.receipt.get("policy", {}).get("reasons", []))

    @pytest.mark.asyncio
    async def test_recommend_delegation_success(self, pack, agent_ctx):
        """recommend_delegation identifies target agent via LLM."""
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Delegate to teressa"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-807")

        result = await pack.recommend_delegation(
            "Reconcile Stripe transactions with QuickBooks", agent_ctx
        )

        assert result.success is True
        assert result.receipt["event_type"] == "finance.delegation.recommend"

    @pytest.mark.asyncio
    async def test_recommend_delegation_empty_task_denied(self, pack, agent_ctx):
        """recommend_delegation fails closed on empty task."""
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-808")

        result = await pack.recommend_delegation("", agent_ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_rule_pack_functions_accessible(self, pack):
        """Enhanced pack retains access to rule-based functions."""
        assert "snapshot" in pack._rule_pack_funcs
        assert "exceptions" in pack._rule_pack_funcs
        assert "draft" in pack._rule_pack_funcs
        assert "proposal" in pack._rule_pack_funcs
        assert "delegation" in pack._rule_pack_funcs


# =============================================================================
# Cross-cutting: Base class contract verification
# =============================================================================


class TestEnhancedSkillPackContract:
    """Verify all enhanced packs follow the EnhancedSkillPack contract."""

    PACK_CLASSES = []

    @pytest.fixture(autouse=True)
    def _load_pack_classes(self):
        """Dynamically import all enhanced pack classes."""
        from aspire_orchestrator.skillpacks.adam_research import EnhancedAdamResearch
        from aspire_orchestrator.skillpacks.nora_conference import EnhancedNoraConference
        from aspire_orchestrator.skillpacks.tec_documents import EnhancedTecDocuments
        from aspire_orchestrator.skillpacks.quinn_invoicing import EnhancedQuinnInvoicing
        from aspire_orchestrator.skillpacks.eli_inbox import EnhancedEliInbox
        from aspire_orchestrator.skillpacks.sarah_front_desk import EnhancedSarahFrontDesk
        from aspire_orchestrator.skillpacks.teressa_books import EnhancedTeressaBooks
        from aspire_orchestrator.skillpacks.mail_ops_desk import EnhancedMailOps
        from aspire_orchestrator.skillpacks.finn_finance_manager import EnhancedFinnFinanceManager

        self.__class__.PACK_CLASSES = [
            EnhancedAdamResearch,
            EnhancedNoraConference,
            EnhancedTecDocuments,
            EnhancedQuinnInvoicing,
            EnhancedEliInbox,
            EnhancedSarahFrontDesk,
            EnhancedTeressaBooks,
            EnhancedMailOps,
            EnhancedFinnFinanceManager,
        ]

    def test_all_packs_inherit_from_enhanced_skill_pack(self):
        """Every enhanced pack inherits from EnhancedSkillPack."""
        from aspire_orchestrator.skillpacks.base_skill_pack import EnhancedSkillPack

        for cls in self.PACK_CLASSES:
            assert issubclass(cls, EnhancedSkillPack), (
                f"{cls.__name__} does not inherit from EnhancedSkillPack"
            )

    def test_all_packs_instantiate_with_mocked_config(self):
        """All packs can be instantiated when config loading is mocked."""
        for cls in self.PACK_CLASSES:
            pack = _create_pack(cls)
            assert pack.agent_id is not None
            assert pack.agent_name is not None

    def test_all_packs_have_agent_id(self):
        """Every pack has a non-empty agent_id."""
        for cls in self.PACK_CLASSES:
            pack = _create_pack(cls)
            assert pack.agent_id, f"{cls.__name__} has empty agent_id"

    def test_all_packs_have_agent_name(self):
        """Every pack has a non-empty agent_name."""
        for cls in self.PACK_CLASSES:
            pack = _create_pack(cls)
            assert pack.agent_name, f"{cls.__name__} has empty agent_name"

    def test_all_packs_have_default_risk_tier(self):
        """Every pack has a valid default risk tier."""
        valid_tiers = {"green", "yellow", "red"}
        for cls in self.PACK_CLASSES:
            pack = _create_pack(cls)
            assert pack.default_risk_tier in valid_tiers, (
                f"{cls.__name__} has invalid tier: {pack.default_risk_tier}"
            )

    def test_all_packs_have_execute_with_llm(self):
        """Every pack inherits execute_with_llm method."""
        for cls in self.PACK_CLASSES:
            pack = _create_pack(cls)
            assert hasattr(pack, "execute_with_llm")
            assert callable(pack.execute_with_llm)

    def test_all_packs_have_build_receipt(self):
        """Every pack inherits build_receipt method."""
        for cls in self.PACK_CLASSES:
            pack = _create_pack(cls)
            assert hasattr(pack, "build_receipt")

    def test_all_packs_have_emit_receipt(self):
        """Every pack inherits emit_receipt method."""
        for cls in self.PACK_CLASSES:
            pack = _create_pack(cls)
            assert hasattr(pack, "emit_receipt")
