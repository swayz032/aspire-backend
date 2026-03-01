"""Tests for agent_reason_node — conversational intelligence.

Covers: full node execution with mocked LLM/memory/RAG,
        phantom action guard, persona loading, channel context,
        aspire awareness, receipt generation (Law #2),
        fail-closed behavior (Law #3).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.nodes.agent_reason import (
    _build_channel_context,
    _build_user_context,
    _guard_output,
    _load_persona,
    _make_conversation_receipt,
    agent_reason_node,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_state(**overrides) -> dict:
    """Build a minimal OrchestratorState dict."""
    state = {
        "utterance": "What is a tax write-off?",
        "agent_target": "finn",
        "intent_type": "knowledge",
        "suite_id": "suite-aaa",
        "actor_id": "user-001",
        "session_id": "sess-001",
        "correlation_id": "corr-001",
        "pipeline_receipts": [],
        "user_profile": None,
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# _load_persona
# ---------------------------------------------------------------------------

class TestLoadPersona:
    def test_known_agent_loads(self):
        # Should not raise — Finn persona file exists
        persona = _load_persona("finn")
        assert len(persona) > 0
        assert "Finn" in persona or "finn" in persona.lower()

    def test_unknown_agent_falls_back_to_ava(self):
        persona = _load_persona("nonexistent_agent")
        assert len(persona) > 0  # Ava fallback


# ---------------------------------------------------------------------------
# _build_user_context
# ---------------------------------------------------------------------------

class TestBuildUserContext:
    def test_no_profile(self):
        assert _build_user_context({"user_profile": None}) == ""

    def test_full_profile(self):
        ctx = _build_user_context({
            "user_profile": {
                "display_name": "John",
                "business_name": "Pallet Co",
                "industry": "manufacturing",
            }
        })
        assert "John" in ctx
        assert "Pallet Co" in ctx
        assert "manufacturing" in ctx

    def test_partial_profile(self):
        ctx = _build_user_context({
            "user_profile": {"display_name": "Jane"}
        })
        assert "Jane" in ctx


# ---------------------------------------------------------------------------
# _build_channel_context
# ---------------------------------------------------------------------------

class TestBuildChannelContext:
    def test_voice_channel(self):
        ctx = _build_channel_context({"user_profile": {"channel": "voice"}})
        assert "1-3 sentences" in ctx

    def test_avatar_channel(self):
        ctx = _build_channel_context({"user_profile": {"channel": "avatar"}})
        assert "1-3 sentences" in ctx

    def test_chat_channel(self):
        ctx = _build_channel_context({"user_profile": {"channel": "chat"}})
        assert "more detailed" in ctx

    def test_default_is_voice(self):
        ctx = _build_channel_context({})
        assert "1-3 sentences" in ctx


# ---------------------------------------------------------------------------
# _guard_output
# ---------------------------------------------------------------------------

class TestGuardOutput:
    def test_clean_text_passes_through(self):
        text = "Tax write-offs help reduce your taxable income."
        assert _guard_output(text, "finn") == text

    def test_phantom_action_stripped(self):
        text = "I've sent the invoice to your client."
        result = _guard_output(text, "finn")
        assert "approval process" in result
        assert "I've sent" not in result

    def test_multiple_phantom_markers(self):
        markers = [
            "I've created your invoice",
            "Payment processed successfully",
            "Meeting scheduled for tomorrow",
        ]
        for marker in markers:
            result = _guard_output(marker, "ava")
            assert "approval process" in result

    def test_case_insensitive(self):
        text = "i've scheduled the meeting for you"
        result = _guard_output(text, "nora")
        assert "approval process" in result


# ---------------------------------------------------------------------------
# _make_conversation_receipt
# ---------------------------------------------------------------------------

class TestMakeConversationReceipt:
    def test_receipt_has_required_fields(self):
        receipt = _make_conversation_receipt(
            _make_state(), "finn", "knowledge", 150,
        )
        assert receipt["action_type"] == "agent.conversation"
        assert receipt["risk_tier"] == "green"
        assert receipt["agent_id"] == "finn"
        assert receipt["intent_type"] == "knowledge"
        assert receipt["outcome"] == "success"
        assert receipt["response_length"] == 150
        assert "id" in receipt
        assert "created_at" in receipt
        assert receipt["suite_id"] == "suite-aaa"


# ---------------------------------------------------------------------------
# agent_reason_node (full integration)
# ---------------------------------------------------------------------------

class TestAgentReasonNode:
    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.working_memory.get_working_memory")
    @patch("aspire_orchestrator.services.episodic_memory.get_episodic_memory")
    @patch("aspire_orchestrator.services.semantic_memory.get_semantic_memory")
    @patch("aspire_orchestrator.services.retrieval_router.get_retrieval_router")
    async def test_successful_conversation(
        self, mock_router_fn, mock_sm_fn, mock_em_fn, mock_wm_fn,
    ):
        # Mock retrieval router
        mock_router = AsyncMock()
        mock_router.retrieve.return_value = MagicMock(context="", receipt_id="rcpt-1")
        mock_router_fn.return_value = mock_router

        # Mock memory services
        mock_wm = AsyncMock()
        mock_wm.get_recent_turns.return_value = []
        mock_wm.add_turn.return_value = None
        mock_wm_fn.return_value = mock_wm

        mock_em = AsyncMock()
        mock_em.search_relevant_episodes.return_value = []
        mock_em_fn.return_value = mock_em

        mock_sm = AsyncMock()
        mock_sm.get_user_facts.return_value = []
        mock_sm_fn.return_value = mock_sm

        # Mock OpenAI
        mock_choice = MagicMock()
        mock_choice.message.content = "A tax write-off is a business expense that reduces your taxable income."
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = AsyncMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            with patch("aspire_orchestrator.config.settings.settings", MagicMock(
                openai_api_key="test", ava_llm_model="gpt-5-mini",
            )):
                result = await agent_reason_node(_make_state())

        assert "conversation_response" in result
        assert "tax" in result["conversation_response"].lower() or "write-off" in result["conversation_response"].lower()
        assert len(result["pipeline_receipts"]) >= 1
        # Working memory should have been called to save turns
        assert mock_wm.add_turn.call_count == 2  # user + agent turn

    @pytest.mark.asyncio
    async def test_llm_failure_returns_persona_fallback(self):
        """LLM failure → persona-specific fallback, not generic error."""
        with patch("aspire_orchestrator.services.retrieval_router.get_retrieval_router") as mock_rr:
            mock_router = AsyncMock()
            mock_router.retrieve.return_value = MagicMock(context="", receipt_id="")
            mock_rr.return_value = mock_router

            with patch("openai.AsyncOpenAI", side_effect=Exception("API down")):
                with patch("aspire_orchestrator.config.settings.settings", MagicMock(
                    openai_api_key="test", ava_llm_model="gpt-5-mini",
                )):
                    result = await agent_reason_node(_make_state())

        response = result["conversation_response"]
        assert "Finn" in response  # Persona-specific fallback
        assert "I wasn't sure" not in response  # NOT the old generic fallback

    @pytest.mark.asyncio
    async def test_ava_fallback_for_unknown_agent(self):
        """Unknown agent → uses Ava persona as fallback."""
        with patch("aspire_orchestrator.services.retrieval_router.get_retrieval_router") as mock_rr:
            mock_router = AsyncMock()
            mock_router.retrieve.return_value = MagicMock(context="", receipt_id="")
            mock_rr.return_value = mock_router

            with patch("openai.AsyncOpenAI", side_effect=Exception("fail")):
                with patch("aspire_orchestrator.config.settings.settings", MagicMock(
                    openai_api_key="test", ava_llm_model="gpt-5-mini",
                )):
                    result = await agent_reason_node(_make_state(agent_target="nonexistent"))

        # Should get Ava's fallback (the default)
        assert "rephrase" in result["conversation_response"].lower()

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.working_memory.get_working_memory")
    @patch("aspire_orchestrator.services.episodic_memory.get_episodic_memory")
    @patch("aspire_orchestrator.services.semantic_memory.get_semantic_memory")
    @patch("aspire_orchestrator.services.retrieval_router.get_retrieval_router")
    async def test_phantom_action_stripped_in_response(
        self, mock_router_fn, mock_sm_fn, mock_em_fn, mock_wm_fn,
    ):
        """If LLM generates phantom action claim, it should be stripped."""
        mock_router = AsyncMock()
        mock_router.retrieve.return_value = MagicMock(context="", receipt_id="")
        mock_router_fn.return_value = mock_router

        mock_wm = AsyncMock()
        mock_wm.get_recent_turns.return_value = []
        mock_wm.add_turn.return_value = None
        mock_wm_fn.return_value = mock_wm

        mock_em = AsyncMock()
        mock_em.search_relevant_episodes.return_value = []
        mock_em_fn.return_value = mock_em

        mock_sm = AsyncMock()
        mock_sm.get_user_facts.return_value = []
        mock_sm_fn.return_value = mock_sm

        mock_choice = MagicMock()
        mock_choice.message.content = "I've sent the invoice to your client."
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = AsyncMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            with patch("aspire_orchestrator.config.settings.settings", MagicMock(
                openai_api_key="test", ava_llm_model="gpt-5-mini",
            )):
                result = await agent_reason_node(_make_state())

        assert "approval process" in result["conversation_response"]
        assert "I've sent" not in result["conversation_response"]

    @pytest.mark.asyncio
    async def test_receipt_always_generated(self):
        """Every conversation generates a receipt — even on LLM failure (Law #2)."""
        with patch("aspire_orchestrator.services.retrieval_router.get_retrieval_router") as mock_rr:
            mock_router = AsyncMock()
            mock_router.retrieve.return_value = MagicMock(context="", receipt_id="")
            mock_rr.return_value = mock_router

            with patch("openai.AsyncOpenAI", side_effect=Exception("fail")):
                with patch("aspire_orchestrator.config.settings.settings", MagicMock(
                    openai_api_key="test", ava_llm_model="gpt-5-mini",
                )):
                    result = await agent_reason_node(_make_state())

        assert len(result["pipeline_receipts"]) >= 1
        receipt = result["pipeline_receipts"][-1]
        assert receipt["action_type"] == "agent.conversation"
        assert receipt["outcome"] == "success"  # Fallback is still "success"
