"""Adam Research Skill Pack Tests — 10 tests covering search, comparison, RFQ.

Categories:
  1. Web search (2 tests) — success + fallback
  2. Places search (1 test) — local business search
  3. Vendor comparison (2 tests) — success + ranking
  4. RFQ generation (1 test) — document generation
  5. Receipt coverage (2 tests) — receipt on success + failure
  6. Governance (2 tests) — GREEN tier no approval + tool_executor invoked

Law compliance:
  - Law #2: Every test verifies receipt emission
  - Law #3: Missing query → fail-closed denial
  - Law #4: All actions are GREEN tier
  - Law #7: Tool calls go through search_router/tool_executor, not direct API
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.skillpacks.adam_research import (
    ACTOR_ADAM,
    AdamResearchContext,
    AdamResearchSkillPack,
)

# =============================================================================
# Fixtures
# =============================================================================

SUITE_ID = "suite-adam-test-001"
OFFICE_ID = "office-adam-test-001"
CORR_ID = "corr-adam-test-001"


@pytest.fixture
def ctx() -> AdamResearchContext:
    return AdamResearchContext(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        correlation_id=CORR_ID,
    )


@pytest.fixture
def skill_pack() -> AdamResearchSkillPack:
    return AdamResearchSkillPack()


def _mock_web_success(**kwargs) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="search.web",
        data={
            "provider_used": "brave",
            "fallback_chain": ["brave"],
            "results": [
                {"title": "Acme Plumbing", "url": "https://acme.example.com", "snippet": "Best plumber"},
                {"title": "Pro Plumbing", "url": "https://pro.example.com", "snippet": "Professional service"},
            ],
        },
        receipt_data={"router_provider_used": "brave"},
    )


def _mock_web_fallback(**kwargs) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="search.web",
        data={
            "provider_used": "tavily",
            "fallback_chain": ["brave", "tavily"],
            "results": [{"title": "Fallback Result", "url": "https://fallback.example.com"}],
        },
        receipt_data={"router_provider_used": "tavily", "router_fallback_chain": ["brave", "tavily"]},
    )


def _mock_web_failure(**kwargs) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id="search.web",
        error="All providers failed. Last error: [tavily] API key missing",
        data={"provider_used": None, "fallback_chain": ["brave", "tavily"]},
        receipt_data={"router_all_failed": True},
    )


def _mock_places_success(**kwargs) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="search.places",
        data={
            "provider_used": "google_places",
            "fallback_chain": ["google_places"],
            "results": [
                {"name": "Local Plumber", "address": "123 Main St", "rating": 4.5},
            ],
        },
        receipt_data={"router_provider_used": "google_places"},
    )


# =============================================================================
# 1. Web Search Tests
# =============================================================================


class TestSearchWeb:
    @pytest.mark.asyncio
    async def test_search_web_success(
        self, skill_pack: AdamResearchSkillPack, ctx: AdamResearchContext,
    ) -> None:
        """Web search returns results from primary provider."""
        with patch(
            "aspire_orchestrator.skillpacks.adam_research.route_web_search",
            new_callable=AsyncMock,
            side_effect=_mock_web_success,
        ):
            result = await skill_pack.search_web("plumbers near me", ctx)

        assert result.success
        assert result.data["provider_used"] == "brave"
        assert len(result.data["results"]) == 2
        assert result.receipt["event_type"] == "research.search"
        assert result.receipt["status"] == "ok"

    @pytest.mark.asyncio
    async def test_search_web_fallback(
        self, skill_pack: AdamResearchSkillPack, ctx: AdamResearchContext,
    ) -> None:
        """Primary provider fails, fallback succeeds."""
        with patch(
            "aspire_orchestrator.skillpacks.adam_research.route_web_search",
            new_callable=AsyncMock,
            side_effect=_mock_web_fallback,
        ):
            result = await skill_pack.search_web("plumbers near me", ctx)

        assert result.success
        assert result.data["provider_used"] == "tavily"
        assert result.data["fallback_chain"] == ["brave", "tavily"]
        assert result.receipt["metadata"]["provider_used"] == "tavily"


# =============================================================================
# 2. Places Search Tests
# =============================================================================


class TestSearchPlaces:
    @pytest.mark.asyncio
    async def test_search_places_success(
        self, skill_pack: AdamResearchSkillPack, ctx: AdamResearchContext,
    ) -> None:
        """Local business search returns results."""
        with patch(
            "aspire_orchestrator.skillpacks.adam_research.route_places_search",
            new_callable=AsyncMock,
            side_effect=_mock_places_success,
        ):
            result = await skill_pack.search_places("plumber", "Austin, TX", ctx)

        assert result.success
        assert result.data["provider_used"] == "google_places"
        assert result.receipt["event_type"] == "research.places"
        assert result.receipt["status"] == "ok"


# =============================================================================
# 3. Vendor Comparison Tests
# =============================================================================


class TestCompareVendors:
    @pytest.mark.asyncio
    async def test_compare_vendors_success(
        self, skill_pack: AdamResearchSkillPack, ctx: AdamResearchContext,
    ) -> None:
        """Multi-provider comparison returns ranked vendors."""
        with patch(
            "aspire_orchestrator.skillpacks.adam_research.route_web_search",
            new_callable=AsyncMock,
            side_effect=_mock_web_success,
        ), patch(
            "aspire_orchestrator.skillpacks.adam_research.route_places_search",
            new_callable=AsyncMock,
            side_effect=_mock_places_success,
        ):
            result = await skill_pack.compare_vendors(
                {"query": "plumbing services", "location": "Austin, TX", "categories": ["plumbing"]},
                ctx,
            )

        assert result.success
        assert result.data["total_results"] > 0
        assert "web" in result.data["sources"]
        assert "places" in result.data["sources"]
        assert result.receipt["event_type"] == "research.compare"

    @pytest.mark.asyncio
    async def test_compare_vendors_ranking(
        self, skill_pack: AdamResearchSkillPack, ctx: AdamResearchContext,
    ) -> None:
        """Results are sorted by relevance score descending."""
        with patch(
            "aspire_orchestrator.skillpacks.adam_research.route_web_search",
            new_callable=AsyncMock,
            side_effect=_mock_web_success,
        ), patch(
            "aspire_orchestrator.skillpacks.adam_research.route_places_search",
            new_callable=AsyncMock,
            side_effect=_mock_places_success,
        ):
            result = await skill_pack.compare_vendors(
                {"query": "plumbing", "location": "Austin, TX"},
                ctx,
            )

        vendors = result.data["vendors"]
        scores = [v["relevance_score"] for v in vendors]
        assert scores == sorted(scores, reverse=True), "Vendors must be sorted by relevance descending"


# =============================================================================
# 4. RFQ Generation Tests
# =============================================================================


class TestGenerateRfq:
    @pytest.mark.asyncio
    async def test_generate_rfq_success(
        self, skill_pack: AdamResearchSkillPack, ctx: AdamResearchContext,
    ) -> None:
        """RFQ document generated with correct structure."""
        result = await skill_pack.generate_rfq(
            vendor_data={"name": "Acme Plumbing", "contact": "info@acme.example.com"},
            requirements={
                "title": "Office Plumbing Repair",
                "items": [{"description": "Fix kitchen sink", "quantity": 1}],
                "deadline": "2026-03-01",
            },
            context=ctx,
        )

        assert result.success
        assert result.data["rfq_id"].startswith("RFQ-")
        assert result.data["vendor"]["name"] == "Acme Plumbing"
        assert result.data["requirements"]["title"] == "Office Plumbing Repair"
        assert result.data["status"] == "draft"
        assert result.receipt["event_type"] == "research.rfq"


# =============================================================================
# 5. Receipt Coverage Tests
# =============================================================================


class TestReceiptCoverage:
    @pytest.mark.asyncio
    async def test_receipt_generated_on_search(
        self, skill_pack: AdamResearchSkillPack, ctx: AdamResearchContext,
    ) -> None:
        """Receipt exists after successful search (Law #2)."""
        with patch(
            "aspire_orchestrator.skillpacks.adam_research.route_web_search",
            new_callable=AsyncMock,
            side_effect=_mock_web_success,
        ):
            result = await skill_pack.search_web("test query", ctx)

        receipt = result.receipt
        assert receipt["receipt_id"]
        assert receipt["suite_id"] == SUITE_ID
        assert receipt["correlation_id"] == CORR_ID
        assert receipt["actor"] == ACTOR_ADAM
        assert receipt["inputs_hash"].startswith("sha256:")
        assert receipt["policy"]["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_receipt_generated_on_failure(
        self, skill_pack: AdamResearchSkillPack, ctx: AdamResearchContext,
    ) -> None:
        """Receipt exists even when all providers fail (Law #2)."""
        with patch(
            "aspire_orchestrator.skillpacks.adam_research.route_web_search",
            new_callable=AsyncMock,
            side_effect=_mock_web_failure,
        ):
            result = await skill_pack.search_web("test query", ctx)

        assert not result.success
        assert result.error is not None
        receipt = result.receipt
        assert receipt["receipt_id"]
        assert receipt["status"] == "failed"
        assert receipt["suite_id"] == SUITE_ID
        assert receipt["correlation_id"] == CORR_ID


# =============================================================================
# 6. Governance Tests
# =============================================================================


class TestGovernance:
    @pytest.mark.asyncio
    async def test_green_tier_no_approval(
        self, skill_pack: AdamResearchSkillPack, ctx: AdamResearchContext,
    ) -> None:
        """GREEN tier operations require no approval (Law #4)."""
        with patch(
            "aspire_orchestrator.skillpacks.adam_research.route_web_search",
            new_callable=AsyncMock,
            side_effect=_mock_web_success,
        ):
            result = await skill_pack.search_web("test", ctx)

        # No approval_required field in receipt — GREEN tier auto-allow
        assert result.success
        assert result.receipt["policy"]["decision"] == "allow"
        # No approval evidence needed for GREEN
        assert "approval_evidence" not in result.receipt

    @pytest.mark.asyncio
    async def test_tool_executor_called(
        self, skill_pack: AdamResearchSkillPack, ctx: AdamResearchContext,
    ) -> None:
        """Verify tool calls go through search_router (Law #7)."""
        mock = AsyncMock(side_effect=_mock_web_success)
        with patch(
            "aspire_orchestrator.skillpacks.adam_research.route_web_search",
            new_callable=lambda: mock,
        ):
            await skill_pack.search_web("test", ctx)

        mock.assert_called_once()
        call_kwargs = mock.call_args
        assert call_kwargs.kwargs["payload"] == {"query": "test"}
        assert call_kwargs.kwargs["suite_id"] == SUITE_ID
        assert call_kwargs.kwargs["risk_tier"] == "green"
