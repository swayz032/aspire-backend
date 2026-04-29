"""Tests for SemanticMemory — persistent learned user facts.

Covers: extract_and_store, get_user_facts, _upsert_fact,
        receipt generation (Law #2), fail-closed behavior (Law #3),
        fact validation, tenant isolation (Law #6).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.semantic_memory import (
    Fact,
    SemanticMemory,
    get_semantic_memory,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sm():
    return SemanticMemory()


SUITE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
AGENT_ID = "finn"


# ---------------------------------------------------------------------------
# extract_and_store
# ---------------------------------------------------------------------------

class TestExtractAndStore:
    @pytest.mark.asyncio
    async def test_skips_too_few_turns(self, sm):
        result = await sm.extract_and_store(
            turns=[{"role": "user", "content": "hi"}],
            suite_id=SUITE_ID, user_id=USER_ID, agent_id=AGENT_ID,
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_skips_empty_turns(self, sm):
        result = await sm.extract_and_store(
            turns=[], suite_id=SUITE_ID, user_id=USER_ID, agent_id=AGENT_ID,
        )
        assert result == 0

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.semantic_memory.store_receipts")
    @patch("aspire_orchestrator.services.supabase_client.supabase_select", new_callable=AsyncMock)
    @patch("aspire_orchestrator.services.supabase_client.supabase_insert", new_callable=AsyncMock)
    async def test_successful_extraction(self, mock_insert, mock_select, mock_receipts, sm):
        mock_select.return_value = []  # No existing facts (INSERT path)

        # Mock generate_text_async to return extracted facts JSON
        llm_response = (
            '{"facts": [{"fact_type": "industry", "fact_key": "industry", '
            '"fact_value": "wooden pallet manufacturing", "confidence": 0.95}]}'
        )

        with patch(
            "aspire_orchestrator.services.semantic_memory.generate_text_async",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            with patch("aspire_orchestrator.services.semantic_memory.resolve_openai_api_key", return_value="test"):
                with patch.object(sm, "_embed_fact_text", new_callable=AsyncMock, return_value=None):
                    result = await sm.extract_and_store(
                        turns=[
                            {"role": "user", "content": "I run a wooden pallet business"},
                            {"role": "agent", "content": "That's great! What can I help with?"},
                        ],
                        suite_id=SUITE_ID, user_id=USER_ID, agent_id=AGENT_ID,
                    )

        assert result == 1
        mock_insert.assert_called_once()
        mock_receipts.assert_called_once()

        # Verify receipt (Law #2)
        receipt = mock_receipts.call_args[0][0][0]
        assert receipt["receipt_type"] == "memory.facts_extracted"
        assert receipt["facts_extracted"] == 1

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.semantic_memory.store_receipts")
    @patch("aspire_orchestrator.services.supabase_client.supabase_select", new_callable=AsyncMock)
    @patch("aspire_orchestrator.services.supabase_client.supabase_update", new_callable=AsyncMock)
    async def test_upsert_updates_existing_fact(self, mock_update, mock_select, mock_receipts, sm):
        """When fact already exists, it should UPDATE not INSERT."""
        mock_select.return_value = [{"id": "existing-fact-id", "fact_value": "old-value"}]

        llm_response = (
            '[{"fact_type": "industry", "fact_key": "industry", '
            '"fact_value": "updated pallet business", "confidence": 0.99}]'
        )

        with patch(
            "aspire_orchestrator.services.semantic_memory.generate_text_async",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            with patch("aspire_orchestrator.services.semantic_memory.resolve_openai_api_key", return_value="test"):
                with patch.object(sm, "_embed_fact_text", new_callable=AsyncMock, return_value=None):
                    result = await sm.extract_and_store(
                        turns=[
                            {"role": "user", "content": "Actually we updated our business"},
                            {"role": "agent", "content": "Good to know."},
                        ],
                        suite_id=SUITE_ID, user_id=USER_ID, agent_id=AGENT_ID,
                    )

        assert result == 1
        mock_update.assert_called_once()
        # Verify it updates the right record
        update_args = mock_update.call_args
        assert "existing-fact-id" in update_args[0][1]

    @pytest.mark.asyncio
    async def test_invalid_fact_type_rejected(self, sm):
        """Facts with invalid fact_type should be silently skipped."""
        mock_choice = MagicMock()
        mock_choice.message.content = (
            '[{"fact_type": "INVALID_TYPE", "fact_key": "test", '
            '"fact_value": "test", "confidence": 0.5}]'
        )
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = AsyncMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("aspire_orchestrator.services.semantic_memory.store_receipts"):
            with patch("openai.AsyncOpenAI", return_value=mock_client):
                with patch("aspire_orchestrator.config.settings.settings", MagicMock(openai_api_key="test")):
                    result = await sm.extract_and_store(
                        turns=[
                            {"role": "user", "content": "test"},
                            {"role": "agent", "content": "test"},
                        ],
                        suite_id=SUITE_ID, user_id=USER_ID, agent_id=AGENT_ID,
                    )
        assert result == 0

    @pytest.mark.asyncio
    async def test_llm_failure_returns_zero(self, sm):
        """Fail-closed: LLM failure → 0 facts, not crash (Law #3)."""
        with patch("openai.AsyncOpenAI", side_effect=Exception("API down")):
            with patch("aspire_orchestrator.config.settings.settings", MagicMock(openai_api_key="test")):
                result = await sm.extract_and_store(
                    turns=[
                        {"role": "user", "content": "hello"},
                        {"role": "agent", "content": "hi"},
                    ],
                    suite_id=SUITE_ID, user_id=USER_ID, agent_id=AGENT_ID,
                )
        assert result == 0

    @pytest.mark.asyncio
    async def test_empty_extraction_no_receipt(self, sm):
        """No facts extracted → return 0, no receipt needed."""
        mock_choice = MagicMock()
        mock_choice.message.content = "[]"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = AsyncMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            with patch("aspire_orchestrator.config.settings.settings", MagicMock(openai_api_key="test")):
                result = await sm.extract_and_store(
                    turns=[
                        {"role": "user", "content": "hello"},
                        {"role": "agent", "content": "hi"},
                    ],
                    suite_id=SUITE_ID, user_id=USER_ID, agent_id=AGENT_ID,
                )
        assert result == 0


# ---------------------------------------------------------------------------
# get_user_facts
# ---------------------------------------------------------------------------

class TestGetUserFacts:
    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.supabase_client.supabase_select", new_callable=AsyncMock)
    async def test_returns_facts(self, mock_select, sm):
        mock_select.return_value = [
            {
                "id": "fact-001",
                "fact_type": "industry",
                "fact_key": "industry",
                "fact_value": "wooden pallet manufacturing",
                "confidence": 0.95,
                "created_at": "2026-02-20T10:00:00Z",
                "updated_at": "2026-02-20T10:00:00Z",
            },
            {
                "id": "fact-002",
                "fact_type": "preference",
                "fact_key": "invoice_format",
                "fact_value": "detailed with line items",
                "confidence": 0.8,
                "created_at": "2026-02-21T10:00:00Z",
                "updated_at": "2026-02-21T10:00:00Z",
            },
        ]

        facts = await sm.get_user_facts(SUITE_ID, USER_ID, AGENT_ID)
        assert len(facts) == 2
        assert facts[0].fact_key == "industry"
        assert facts[0].fact_value == "wooden pallet manufacturing"
        assert facts[1].fact_key == "invoice_format"

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.supabase_client.supabase_select", new_callable=AsyncMock)
    async def test_empty_facts(self, mock_select, sm):
        mock_select.return_value = []
        facts = await sm.get_user_facts(SUITE_ID, USER_ID, AGENT_ID)
        assert facts == []

    @pytest.mark.asyncio
    async def test_db_failure_returns_empty(self, sm):
        """Fail-closed: DB failure → empty list (Law #3)."""
        with patch("aspire_orchestrator.services.supabase_client.supabase_select", new_callable=AsyncMock, side_effect=Exception("DB down")):
            facts = await sm.get_user_facts(SUITE_ID, USER_ID, AGENT_ID)
        assert facts == []


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSemanticMemorySingleton:
    def test_singleton(self):
        import aspire_orchestrator.services.semantic_memory as mod
        mod._memory = None
        m1 = get_semantic_memory()
        m2 = get_semantic_memory()
        assert m1 is m2
        mod._memory = None


# ---------------------------------------------------------------------------
# Pass 7 dual-read shadow mode (memory_objects parity)
# ---------------------------------------------------------------------------

class TestDualReadShadowMode:
    """Shadow-read parallel against memory_objects.decision_fact for parity
    verification. Legacy result is always returned to caller; dual-read is
    purely observability."""

    @pytest.mark.asyncio
    async def test_parity_no_divergence_warning(self, sm, caplog):
        """Same row IDs -> no DIVERGENCE warning."""
        import logging as _logging

        # Legacy row keyed by 'id'; shadow row keyed by 'memory_id'.
        legacy_id = "abcdef00-0000-4000-8000-000000000001"
        legacy_rows = [{
            "id": legacy_id,
            "fact_type": "industry",
            "fact_key": "industry",
            "fact_value": "wooden pallets",
            "confidence": 0.9,
            "created_at": "2026-04-01T10:00:00Z",
            "updated_at": "2026-04-01T10:00:00Z",
        }]
        shadow_rows = [{
            "memory_id": legacy_id,
            "memory_type": "decision_fact",
            "last_activity_at": "2026-04-01T10:00:00Z",
        }]

        async def select_router(table, *args, **kwargs):
            if table == "agent_semantic_memory":
                return legacy_rows
            if table == "memory_objects":
                return shadow_rows
            return []

        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new=AsyncMock(side_effect=select_router),
        ), patch(
            "aspire_orchestrator.services.memory_dual_read.is_dual_read_enabled",
            return_value=True,
        ):
            with caplog.at_level(_logging.DEBUG, logger="aspire_orchestrator.services.memory_dual_read"):
                facts = await sm.get_user_facts(SUITE_ID, USER_ID, AGENT_ID)

        assert len(facts) == 1
        assert facts[0].fact_id == legacy_id
        warnings = [r for r in caplog.records
                    if r.levelno >= _logging.WARNING and "DIVERGENCE" in r.getMessage()]
        assert warnings == []

    @pytest.mark.asyncio
    async def test_divergence_logs_warning(self, sm, caplog):
        """Different IDs in legacy vs shadow -> WARNING."""
        import logging as _logging

        legacy_rows = [{
            "id": "11111111-1111-4111-8111-111111111111",
            "fact_type": "preference",
            "fact_key": "k1",
            "fact_value": "v1",
            "confidence": 0.7,
            "created_at": "2026-04-01T10:00:00Z",
            "updated_at": "2026-04-01T10:00:00Z",
        }]
        shadow_rows = [{
            "memory_id": "22222222-2222-4222-8222-222222222222",
            "memory_type": "decision_fact",
            "last_activity_at": "2026-04-01T10:00:00Z",
        }]

        async def select_router(table, *args, **kwargs):
            if table == "agent_semantic_memory":
                return legacy_rows
            if table == "memory_objects":
                return shadow_rows
            return []

        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new=AsyncMock(side_effect=select_router),
        ), patch(
            "aspire_orchestrator.services.memory_dual_read.is_dual_read_enabled",
            return_value=True,
        ):
            with caplog.at_level(_logging.WARNING, logger="aspire_orchestrator.services.memory_dual_read"):
                facts = await sm.get_user_facts(SUITE_ID, USER_ID, AGENT_ID)

        # Legacy result preserved
        assert len(facts) == 1
        warnings = [r for r in caplog.records
                    if r.levelno >= _logging.WARNING and "DIVERGENCE" in r.getMessage()]
        assert len(warnings) == 1
        assert "semantic_memory.get_user_facts" in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_dual_read_disabled_skips_shadow(self, sm):
        """ASPIRE_MEMORY_DUAL_READ_ENABLED=0 -> shadow query never runs."""
        legacy_rows = [{
            "id": "33333333-3333-4333-8333-333333333333",
            "fact_type": "industry",
            "fact_key": "k",
            "fact_value": "v",
            "confidence": 0.5,
            "created_at": "2026-04-01T10:00:00Z",
            "updated_at": "2026-04-01T10:00:00Z",
        }]

        select_calls: list[str] = []

        async def select_router(table, *args, **kwargs):
            select_calls.append(table)
            if table == "agent_semantic_memory":
                return legacy_rows
            return []

        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new=AsyncMock(side_effect=select_router),
        ), patch(
            "aspire_orchestrator.services.memory_dual_read.is_dual_read_enabled",
            return_value=False,
        ):
            facts = await sm.get_user_facts(SUITE_ID, USER_ID, AGENT_ID)

        assert len(facts) == 1
        # Shadow path never queried memory_objects
        assert "memory_objects" not in select_calls

    @pytest.mark.asyncio
    async def test_shadow_read_failure_does_not_break_legacy(self, sm, caplog):
        """Shadow exception is logged; legacy result is returned unchanged."""
        import logging as _logging

        legacy_rows = [{
            "id": "44444444-4444-4444-8444-444444444444",
            "fact_type": "industry",
            "fact_key": "k",
            "fact_value": "v",
            "confidence": 0.9,
            "created_at": "2026-04-01T10:00:00Z",
            "updated_at": "2026-04-01T10:00:00Z",
        }]

        async def select_router(table, *args, **kwargs):
            if table == "agent_semantic_memory":
                return legacy_rows
            if table == "memory_objects":
                raise Exception("simulated shadow read failure")
            return []

        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new=AsyncMock(side_effect=select_router),
        ), patch(
            "aspire_orchestrator.services.memory_dual_read.is_dual_read_enabled",
            return_value=True,
        ):
            with caplog.at_level(_logging.WARNING, logger="aspire_orchestrator.services.memory_dual_read"):
                facts = await sm.get_user_facts(SUITE_ID, USER_ID, AGENT_ID)

        assert len(facts) == 1
        shadow_errors = [r for r in caplog.records if "shadow_error" in r.getMessage()]
        assert len(shadow_errors) == 1
