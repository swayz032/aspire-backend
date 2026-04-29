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
        assert receipt["receipt_type"] == "memory.episode_stored"
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


# ---------------------------------------------------------------------------
# Pass 7 dual-read shadow mode (memory_objects parity)
# ---------------------------------------------------------------------------

class TestDualReadShadowMode:
    """Verify the shadow read against memory_objects runs alongside the legacy
    path, logs divergence at WARNING when results disagree, logs nothing
    extraordinary when they match, and is no-op when the feature flag is off.
    The shadow path must NEVER affect the value returned to the caller.
    """

    @pytest.mark.asyncio
    async def test_parity_no_divergence_warning(self, em, caplog):
        """Same IDs in legacy + shadow -> no DIVERGENCE warning, returns legacy."""
        import logging as _logging

        rpc_rows = [
            {
                "id": "11111111-1111-4111-1111-111111111111",
                "agent_id": "finn",
                "session_id": "sess-a",
                "summary": "summary a",
                "key_topics": [],
                "key_entities": {},
                "turn_count": 3,
                "created_at": "2026-04-01T10:00:00Z",
                "similarity": 0.9,
            }
        ]
        shadow_rows = [
            # Same memory_id as legacy id -> sets match -> parity OK
            {
                "memory_id": "11111111-1111-4111-1111-111111111111",
                "memory_type": "session_summary",
                "last_activity_at": "2026-04-01T10:00:00Z",
            }
        ]

        with patch(
            "aspire_orchestrator.services.legal_embedding_service.embed_text",
            new_callable=AsyncMock, return_value=[0.1] * 3072,
        ), patch(
            "aspire_orchestrator.services.supabase_client.supabase_rpc",
            new_callable=AsyncMock, return_value=rpc_rows,
        ), patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new_callable=AsyncMock, return_value=shadow_rows,
        ), patch(
            "aspire_orchestrator.services.memory_dual_read.is_dual_read_enabled",
            return_value=True,
        ):
            with caplog.at_level(_logging.DEBUG, logger="aspire_orchestrator.services.memory_dual_read"):
                episodes = await em.search_relevant_episodes("query", SUITE_ID, AGENT_ID)

        assert len(episodes) == 1
        assert episodes[0].episode_id == "11111111-1111-4111-1111-111111111111"
        # Parity should produce DEBUG log, not WARNING
        warnings = [r for r in caplog.records
                    if r.levelno >= _logging.WARNING and "DIVERGENCE" in r.getMessage()]
        assert warnings == [], f"Unexpected DIVERGENCE warnings on parity: {warnings}"

    @pytest.mark.asyncio
    async def test_divergence_logs_warning(self, em, caplog):
        """Different IDs between legacy + shadow -> WARNING with DIVERGENCE."""
        import logging as _logging

        rpc_rows = [
            {
                "id": "11111111-1111-4111-1111-111111111111",
                "agent_id": "finn",
                "session_id": "sess-a",
                "summary": "in legacy only",
                "key_topics": [],
                "key_entities": {},
                "turn_count": 3,
                "created_at": "2026-04-01T10:00:00Z",
                "similarity": 0.9,
            }
        ]
        shadow_rows = [
            {
                "memory_id": "22222222-2222-4222-2222-222222222222",
                "memory_type": "session_summary",
                "last_activity_at": "2026-04-01T10:00:00Z",
            }
        ]

        with patch(
            "aspire_orchestrator.services.legal_embedding_service.embed_text",
            new_callable=AsyncMock, return_value=[0.1] * 3072,
        ), patch(
            "aspire_orchestrator.services.supabase_client.supabase_rpc",
            new_callable=AsyncMock, return_value=rpc_rows,
        ), patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new_callable=AsyncMock, return_value=shadow_rows,
        ), patch(
            "aspire_orchestrator.services.memory_dual_read.is_dual_read_enabled",
            return_value=True,
        ):
            with caplog.at_level(_logging.WARNING, logger="aspire_orchestrator.services.memory_dual_read"):
                episodes = await em.search_relevant_episodes("query", SUITE_ID, AGENT_ID)

        # Legacy result still returned unchanged
        assert len(episodes) == 1
        assert episodes[0].episode_id == "11111111-1111-4111-1111-111111111111"
        warnings = [r for r in caplog.records
                    if r.levelno >= _logging.WARNING and "DIVERGENCE" in r.getMessage()]
        assert len(warnings) == 1, f"Expected exactly 1 DIVERGENCE warning, got {warnings}"
        assert "episodic_memory.search_relevant_episodes" in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_dual_read_disabled_skips_shadow(self, em):
        """When ASPIRE_MEMORY_DUAL_READ_ENABLED=0 the shadow read is a no-op."""
        rpc_rows = [
            {
                "id": "33333333-3333-4333-3333-333333333333",
                "agent_id": "finn",
                "session_id": "sess-c",
                "summary": "legacy only",
                "key_topics": [],
                "key_entities": {},
                "turn_count": 1,
                "created_at": "2026-04-01T10:00:00Z",
                "similarity": 0.9,
            }
        ]
        shadow_select = AsyncMock(return_value=[])

        with patch(
            "aspire_orchestrator.services.legal_embedding_service.embed_text",
            new_callable=AsyncMock, return_value=[0.1] * 3072,
        ), patch(
            "aspire_orchestrator.services.supabase_client.supabase_rpc",
            new_callable=AsyncMock, return_value=rpc_rows,
        ), patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            shadow_select,
        ), patch(
            "aspire_orchestrator.services.memory_dual_read.is_dual_read_enabled",
            return_value=False,
        ):
            episodes = await em.search_relevant_episodes("query", SUITE_ID, AGENT_ID)

        assert len(episodes) == 1
        # supabase_select must NOT be called when dual-read is disabled
        # (the legacy RPC path doesn't use supabase_select on the happy path)
        assert shadow_select.call_count == 0

    @pytest.mark.asyncio
    async def test_shadow_read_failure_does_not_break_legacy(self, em, caplog):
        """If the shadow path raises, the legacy result must still be returned."""
        import logging as _logging

        rpc_rows = [
            {
                "id": "44444444-4444-4444-4444-444444444444",
                "agent_id": "finn",
                "session_id": "sess-d",
                "summary": "legacy",
                "key_topics": [],
                "key_entities": {},
                "turn_count": 1,
                "created_at": "2026-04-01T10:00:00Z",
                "similarity": 0.9,
            }
        ]

        with patch(
            "aspire_orchestrator.services.legal_embedding_service.embed_text",
            new_callable=AsyncMock, return_value=[0.1] * 3072,
        ), patch(
            "aspire_orchestrator.services.supabase_client.supabase_rpc",
            new_callable=AsyncMock, return_value=rpc_rows,
        ), patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new_callable=AsyncMock, side_effect=Exception("simulated shadow failure"),
        ), patch(
            "aspire_orchestrator.services.memory_dual_read.is_dual_read_enabled",
            return_value=True,
        ):
            with caplog.at_level(_logging.WARNING, logger="aspire_orchestrator.services.memory_dual_read"):
                episodes = await em.search_relevant_episodes("query", SUITE_ID, AGENT_ID)

        # Legacy path still succeeds
        assert len(episodes) == 1
        assert episodes[0].episode_id == "44444444-4444-4444-4444-444444444444"
        # Shadow failure is logged at WARNING via memory_dual_read.log_shadow_error
        shadow_errors = [r for r in caplog.records
                         if "shadow_error" in r.getMessage()]
        assert len(shadow_errors) == 1
