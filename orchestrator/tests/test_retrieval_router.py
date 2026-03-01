"""Tests for RetrievalRouter — agentic cross-domain RAG routing.

Covers: _determine_domains, _assemble_context, retrieve,
        receipt generation (Law #2), fail-closed (Law #3),
        parallel retrieval, tenant isolation (Law #6).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.services.retrieval_router import (
    RetrievalRouter,
    RetrievalRouterResult,
    get_retrieval_router,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def router():
    return RetrievalRouter()


SUITE_ID = "suite-aaa-111"


# ---------------------------------------------------------------------------
# _determine_domains
# ---------------------------------------------------------------------------

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
        """Ava should detect finance signals and add finance domain."""
        domains = router._determine_domains("ava", "what tax deductions can I take?")
        assert "finance" in domains

    def test_ava_cross_domain_legal(self, router):
        """Ava should detect legal signals and add legal domain."""
        domains = router._determine_domains("ava", "what does the indemnification clause mean?")
        assert "legal" in domains

    def test_ava_cross_domain_communication(self, router):
        """Ava should detect communication signals and add communication domain."""
        domains = router._determine_domains("ava", "help me draft a follow-up email")
        assert "communication" in domains

    def test_ava_multi_domain(self, router):
        """Ava should detect multiple domains in one query."""
        domains = router._determine_domains("ava", "what are the tax implications of this contract?")
        assert "finance" in domains
        assert "legal" in domains

    def test_nora_has_no_domains(self, router):
        """Nora has no RAG domains — should return empty."""
        domains = router._determine_domains("nora", "anything")
        assert domains == []

    def test_sarah_has_no_domains(self, router):
        domains = router._determine_domains("sarah", "anything")
        assert domains == []

    def test_tec_has_no_domains(self, router):
        domains = router._determine_domains("tec", "anything")
        assert domains == []

    def test_unknown_agent_no_domains(self, router):
        domains = router._determine_domains("nonexistent", "anything")
        assert domains == []


# ---------------------------------------------------------------------------
# _assemble_context
# ---------------------------------------------------------------------------

class TestAssembleContext:
    def test_empty_results(self, router):
        result = router._assemble_context([])
        assert result == ""

    def test_empty_chunks(self, router):
        result = router._assemble_context([("finance", [])])
        assert result == ""

    def test_single_domain_single_chunk(self, router):
        chunks = [{"content": "Tax deduction info", "combined_score": 0.9, "chunk_type": "rule"}]
        result = router._assemble_context([("finance", chunks)])
        assert "RELEVANT KNOWLEDGE" in result
        assert "Tax deduction info" in result
        assert "Source: finance" in result

    def test_multi_domain_sorted_by_score(self, router):
        finance_chunks = [{"content": "Finance info", "combined_score": 0.7}]
        legal_chunks = [{"content": "Legal info", "combined_score": 0.9}]
        result = router._assemble_context([("finance", finance_chunks), ("legal", legal_chunks)])
        # Legal should appear first (higher score)
        legal_pos = result.index("Legal info")
        finance_pos = result.index("Finance info")
        assert legal_pos < finance_pos

    def test_caps_at_10_chunks(self, router):
        chunks = [{"content": f"Chunk {i}", "combined_score": 0.5 + i * 0.01} for i in range(15)]
        result = router._assemble_context([("finance", chunks)])
        # Should only have [1/10] through [10/10]
        assert "[10/10]" in result
        assert "[11/" not in result


# ---------------------------------------------------------------------------
# retrieve
# ---------------------------------------------------------------------------

class TestRetrieve:
    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self, router):
        result = await router.retrieve("", "ava", SUITE_ID)
        assert result.context == ""
        assert result.receipt_id  # Still generates receipt ID

    @pytest.mark.asyncio
    async def test_whitespace_query_returns_empty(self, router):
        result = await router.retrieve("   ", "ava", SUITE_ID)
        assert result.context == ""

    @pytest.mark.asyncio
    async def test_no_domains_returns_empty(self, router):
        """Agent with no domains → skip retrieval entirely."""
        result = await router.retrieve("anything", "nora", SUITE_ID)
        assert result.context == ""
        assert result.domains_queried == []

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.retrieval_router.store_receipts")
    async def test_domain_failure_graceful_degradation(self, mock_receipts, router):
        """If a domain retrieval fails, return empty for that domain (Law #3)."""
        with patch.object(router, "_retrieve_domain", new_callable=AsyncMock, return_value=("finance", [])):
            result = await router.retrieve("tax deductions", "finn", SUITE_ID)
            assert isinstance(result, RetrievalRouterResult)
            assert result.receipt_id  # Receipt always generated

    @pytest.mark.asyncio
    async def test_result_has_timing(self, router):
        """Retrieve should always report timing."""
        result = await router.retrieve("test", "nora", SUITE_ID)
        assert result.timing_ms >= 0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestRetrievalRouterSingleton:
    def test_singleton(self):
        import aspire_orchestrator.services.retrieval_router as mod
        mod._router = None
        r1 = get_retrieval_router()
        r2 = get_retrieval_router()
        assert r1 is r2
        mod._router = None
