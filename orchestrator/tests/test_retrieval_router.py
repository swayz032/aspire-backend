"""Tests for RetrievalRouter agentic cross-domain RAG routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.services.retrieval_router import (
    RetrievalRouter,
    RetrievalRouterResult,
    get_retrieval_router,
)


@pytest.fixture
def router():
    return RetrievalRouter()


SUITE_ID = "suite-aaa-111"


class TestDetermineDomains:
    def test_finn_gets_finance(self, router):
        domains = router._determine_domains("finn", "anything")
        assert "finance" in domains

    def test_clara_gets_legal(self, router):
        domains = router._determine_domains("clara", "anything")
        assert "legal" in domains

    def test_eli_gets_communication(self, router):
        domains = router._determine_domains("eli", "anything")
        assert "communication" in domains

    def test_ava_gets_general_by_default(self, router):
        domains = router._determine_domains("ava", "hello how are you")
        assert "general" in domains

    def test_ava_cross_domain_finance(self, router):
        domains = router._determine_domains("ava", "what tax deductions can I take?")
        assert "finance" in domains

    def test_ava_cross_domain_legal(self, router):
        domains = router._determine_domains("ava", "what does the indemnification clause mean?")
        assert "legal" in domains

    def test_ava_cross_domain_communication(self, router):
        domains = router._determine_domains("ava", "help me draft a follow-up email")
        assert "communication" in domains

    def test_ava_multi_domain(self, router):
        domains = router._determine_domains("ava", "what are the tax implications of this contract?")
        assert "finance" in domains
        assert "legal" in domains

    def test_nora_has_no_domains(self, router):
        assert router._determine_domains("nora", "anything") == []

    def test_sarah_has_no_domains(self, router):
        assert router._determine_domains("sarah", "anything") == []

    def test_tec_has_no_domains(self, router):
        assert router._determine_domains("tec", "anything") == []

    def test_unknown_agent_no_domains(self, router):
        assert router._determine_domains("nonexistent", "anything") == []

    def test_ava_domain_fanout_is_capped(self, router):
        domains = router._determine_domains(
            "ava",
            "help me draft an email about the tax impact of this contract for the business",
        )
        assert len(domains) <= router.MAX_DOMAINS


class TestAssembleContext:
    def test_empty_results(self, router):
        context, grounding_score, sources = router._assemble_context([])
        assert context == ""
        assert grounding_score == 0.0
        assert sources == []

    def test_empty_chunks(self, router):
        context, grounding_score, sources = router._assemble_context([("finance", [])])
        assert context == ""
        assert grounding_score == 0.0
        assert sources == []

    def test_single_domain_single_chunk(self, router):
        chunks = [{"content": "Tax deduction info", "combined_score": 0.9, "chunk_type": "rule"}]
        context, grounding_score, sources = router._assemble_context([("finance", chunks)])
        assert "RELEVANT KNOWLEDGE" in context
        assert "Tax deduction info" in context
        assert "Source: finance" in context
        assert grounding_score == pytest.approx(0.9)
        assert sources == ["finance"]

    def test_multi_domain_sorted_by_score(self, router):
        finance_chunks = [{"content": "Finance info", "combined_score": 0.7}]
        legal_chunks = [{"content": "Legal info", "combined_score": 0.9}]
        context, grounding_score, _ = router._assemble_context(
            [("finance", finance_chunks), ("legal", legal_chunks)]
        )
        assert context.index("Legal info") < context.index("Finance info")
        assert grounding_score == pytest.approx(0.8)

    def test_caps_at_10_chunks(self, router):
        chunks = [{"content": f"Chunk {i}", "combined_score": 0.5 + i * 0.01} for i in range(15)]
        context, _, _ = router._assemble_context([("finance", chunks)])
        assert "[10/10]" in context
        assert "[11/" not in context


class TestRetrieve:
    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self, router):
        result = await router.retrieve("", "ava", SUITE_ID)
        assert result.context == ""
        assert result.status == "not_applicable"
        assert result.receipt_id

    @pytest.mark.asyncio
    async def test_whitespace_query_returns_empty(self, router):
        result = await router.retrieve("   ", "ava", SUITE_ID)
        assert result.context == ""
        assert result.status == "not_applicable"

    @pytest.mark.asyncio
    async def test_no_domains_returns_empty(self, router):
        result = await router.retrieve("anything", "nora", SUITE_ID)
        assert result.context == ""
        assert result.domains_queried == []
        assert result.status == "not_applicable"

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.retrieval_router.store_receipts")
    async def test_domain_failure_graceful_degradation(self, mock_receipts, router):
        del mock_receipts
        with patch.object(router, "_retrieve_domain", new_callable=AsyncMock, return_value=("finance", [])):
            result = await router.retrieve("tax deductions", "finn", SUITE_ID)
        assert isinstance(result, RetrievalRouterResult)
        assert result.receipt_id
        assert result.status == "degraded"
        assert result.degraded_reason == "no_chunks_retrieved"

    @pytest.mark.asyncio
    async def test_result_has_timing(self, router):
        result = await router.retrieve("test", "nora", SUITE_ID)
        assert result.timing_ms >= 0

    @pytest.mark.asyncio
    async def test_success_result_has_grounding_and_sources(self, router):
        with patch.object(
            router,
            "_retrieve_domain",
            new_callable=AsyncMock,
            return_value=("finance", [{"content": "Tax guidance", "combined_score": 0.88, "source": "IRS memo"}]),
        ):
            result = await router.retrieve("tax deductions", "finn", SUITE_ID)
        assert result.status == "primary"
        assert result.grounding_score == pytest.approx(0.88)
        assert result.sources == ["IRS memo"]

    @pytest.mark.asyncio
    async def test_second_identical_request_hits_router_cache(self, router):
        with patch.object(
            router,
            "_retrieve_domain",
            new_callable=AsyncMock,
            return_value=("finance", [{"content": "Tax guidance", "combined_score": 0.88, "source": "IRS memo"}]),
        ) as mock_retrieve:
            first = await router.retrieve("tax deductions", "finn", SUITE_ID)
            second = await router.retrieve("tax deductions", "finn", SUITE_ID)
        assert first.status == "primary"
        assert second.status == "primary"
        assert mock_retrieve.await_count == 1


class TestRetrievalRouterSingleton:
    def test_singleton(self):
        import aspire_orchestrator.services.retrieval_router as mod

        mod._router = None
        r1 = get_retrieval_router()
        r2 = get_retrieval_router()
        assert r1 is r2
        mod._router = None
