"""Tests for EpisodicMemory — cross-session episode summaries.

Covers: summarize_and_store, search_relevant_episodes, _parse_summary_output,
        receipt generation (Law #2), fail-closed behavior (Law #3).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.episodic_memory import (
    Episode,
    EpisodicMemory,
    get_episodic_memory,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def em():
    return EpisodicMemory()


SUITE_ID = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
USER_ID = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"
AGENT_ID = "finn"
SESSION_ID = "sess-001"


# ---------------------------------------------------------------------------
# _parse_summary_output
# ---------------------------------------------------------------------------

class TestParseSummaryOutput:
    def test_parses_structured_output(self, em):
        raw = (
            "SUMMARY: Discussed tax deductions for wooden pallet business.\n"
            "TOPICS: tax deductions, Section 179, home office\n"
            'ENTITIES: {"industry": "wooden pallet manufacturing", "amounts": ["$5000"]}'
        )
        summary, topics, entities = em._parse_summary_output(raw)
        assert "tax deductions" in summary
        assert "Section 179" in topics
        assert entities["industry"] == "wooden pallet manufacturing"

    def test_handles_no_entities(self, em):
        raw = "SUMMARY: Quick greeting.\nTOPICS: hello\nENTITIES: {}"
        summary, topics, entities = em._parse_summary_output(raw)
        assert summary == "Quick greeting."
        assert topics == ["hello"]
        assert entities == {}

    def test_handles_malformed_json(self, em):
        raw = "SUMMARY: Chat about invoicing.\nTOPICS: invoicing\nENTITIES: not-json"
        summary, topics, entities = em._parse_summary_output(raw)
        assert summary == "Chat about invoicing."
        assert entities == {}

    def test_handles_raw_text_fallback(self, em):
        raw = "Just a plain text response without structure."
        summary, topics, entities = em._parse_summary_output(raw)
        assert summary == raw
        assert topics == []
        assert entities == {}


# ---------------------------------------------------------------------------
# summarize_and_store
# ---------------------------------------------------------------------------

class TestSummarizeAndStore:
    @pytest.mark.asyncio
    async def test_skips_too_few_turns(self, em):
        result = await em.summarize_and_store(
            turns=[{"role": "user", "content": "hi"}],
            session_id=SESSION_ID, suite_id=SUITE_ID,
            user_id=USER_ID, agent_id=AGENT_ID,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_empty_turns(self, em):
        result = await em.summarize_and_store(
            turns=[], session_id=SESSION_ID, suite_id=SUITE_ID,
            user_id=USER_ID, agent_id=AGENT_ID,
        )
        assert result is None

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.episodic_memory.store_receipts")
    @patch("aspire_orchestrator.services.supabase_client.supabase_insert", new_callable=AsyncMock)
    @patch("aspire_orchestrator.services.legal_embedding_service.embed_text", new_callable=AsyncMock)
    async def test_successful_store(self, mock_embed, mock_insert, mock_receipts, em):
        mock_embed.return_value = [0.1] * 3072

        llm_response = (
            "SUMMARY: Discussed tax strategies.\n"
            "TOPICS: taxes, strategy\n"
            "ENTITIES: {}"
        )

        with patch(
            "aspire_orchestrator.services.episodic_memory.generate_text_async",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            with patch("aspire_orchestrator.services.episodic_memory.resolve_openai_api_key", return_value="test"):
                result = await em.summarize_and_store(
                    turns=[
                        {"role": "user", "content": "What tax deductions can I take?"},
                        {"role": "agent", "content": "Common deductions include..."},
                    ],
                    session_id=SESSION_ID, suite_id=SUITE_ID,
                    user_id=USER_ID, agent_id=AGENT_ID,
                )

        assert result is not None  # Returns episode_id
        mock_insert.assert_called_once()
        mock_receipts.assert_called_once()  # Law #2

        # Verify receipt has required fields
        receipt = mock_receipts.call_args[0][0][0]
        assert receipt["event_type"] == "memory.episode_stored"
        assert receipt["suite_id"] == SUITE_ID
        assert receipt["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none(self, em):
        """Fail-closed: LLM failure → None, not crash (Law #3)."""
        with patch(
            "aspire_orchestrator.services.episodic_memory.generate_text_async",
            new_callable=AsyncMock,
            side_effect=Exception("API down"),
        ):
            with patch("aspire_orchestrator.services.episodic_memory.resolve_openai_api_key", return_value="test"):
                result = await em.summarize_and_store(
                    turns=[
                        {"role": "user", "content": "hello"},
                        {"role": "agent", "content": "hi"},
                    ],
                    session_id=SESSION_ID, suite_id=SUITE_ID,
                    user_id=USER_ID, agent_id=AGENT_ID,
                )
        assert result is None


# ---------------------------------------------------------------------------
# search_relevant_episodes
# ---------------------------------------------------------------------------

class TestSearchRelevantEpisodes:
    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self, em):
        result = await em.search_relevant_episodes("", SUITE_ID, AGENT_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_whitespace_query_returns_empty(self, em):
        result = await em.search_relevant_episodes("   ", SUITE_ID, AGENT_ID)
        assert result == []

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.supabase_client.supabase_rpc", new_callable=AsyncMock)
    @patch("aspire_orchestrator.services.legal_embedding_service.embed_text", new_callable=AsyncMock)
    async def test_successful_search(self, mock_embed, mock_rpc, em):
        mock_embed.return_value = [0.1] * 3072
        mock_rpc.return_value = [
            {
                "id": "ep-001",
                "agent_id": "finn",
                "session_id": "sess-old",
                "summary": "Discussed tax deductions",
                "key_topics": ["taxes"],
                "key_entities": {},
                "turn_count": 5,
                "created_at": "2026-02-20T10:00:00Z",
                "similarity": 0.85,
            }
        ]

        episodes = await em.search_relevant_episodes("tax deductions", SUITE_ID, AGENT_ID)
        assert len(episodes) == 1
        assert episodes[0].summary == "Discussed tax deductions"
        assert episodes[0].similarity == 0.85

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.legal_embedding_service.embed_text", new_callable=AsyncMock)
    async def test_embed_failure_returns_empty(self, mock_embed, em):
        """Fail-closed: embedding failure → empty list (Law #3)."""
        mock_embed.return_value = None
        result = await em.search_relevant_episodes("query", SUITE_ID, AGENT_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_rpc_failure_returns_empty(self, em):
        """Fail-closed: Supabase failure → empty list (Law #3)."""
        with patch("aspire_orchestrator.services.legal_embedding_service.embed_text", new_callable=AsyncMock, return_value=[0.1] * 3072):
            with patch("aspire_orchestrator.services.supabase_client.supabase_rpc", new_callable=AsyncMock, side_effect=Exception("DB down")):
                result = await em.search_relevant_episodes("query", SUITE_ID, AGENT_ID)
        assert result == []


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestEpisodicMemorySingleton:
    def test_singleton(self):
        import aspire_orchestrator.services.episodic_memory as mod
        mod._memory = None
        m1 = get_episodic_memory()
        m2 = get_episodic_memory()
        assert m1 is m2
        mod._memory = None
