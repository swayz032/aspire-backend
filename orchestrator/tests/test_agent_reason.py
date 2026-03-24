"""Tests for agent_reason_node conversational intelligence."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.nodes.agent_reason import (
    _build_channel_context,
    _build_user_context,
    _guard_output,
    _load_persona,
    _load_prompt_contract,
    _make_conversation_receipt,
    agent_reason_node,
)


def _make_state(**overrides) -> dict:
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


class TestLoadPersona:
    def test_known_agent_loads(self):
        persona = _load_persona("finn")
        assert len(persona) > 0
        assert "Finn" in persona or "finn" in persona.lower()

    def test_unknown_agent_falls_back_to_ava(self):
        persona = _load_persona("nonexistent_agent")
        assert len(persona) > 0


class TestLoadPromptContract:
    def test_missing_prompt_contract_is_safe(self):
        assert isinstance(_load_prompt_contract("nonexistent_agent"), str)


class TestBuildUserContext:
    def test_no_profile(self):
        assert _build_user_context({"user_profile": None}) == ""

    def test_full_profile(self):
        ctx = _build_user_context(
            {
                "user_profile": {
                    "display_name": "John",
                    "business_name": "Pallet Co",
                    "industry": "manufacturing",
                }
            }
        )
        assert "John" in ctx
        assert "Pallet Co" in ctx
        assert "manufacturing" in ctx

    def test_partial_profile(self):
        ctx = _build_user_context({"user_profile": {"display_name": "Jane"}})
        assert "Jane" in ctx


class TestBuildChannelContext:
    def test_voice_channel(self):
        ctx = _build_channel_context({"user_profile": {"channel": "voice"}})
        assert "text-to-speech" in ctx.lower()

    def test_avatar_channel(self):
        ctx = _build_channel_context({"user_profile": {"channel": "avatar"}})
        assert "anam avatar" in ctx.lower()

    def test_chat_channel(self):
        ctx = _build_channel_context({"user_profile": {"channel": "chat"}})
        assert "formatting" in ctx.lower()

    def test_default_is_chat(self):
        # RC0 fix: default is now "chat" (not "voice")
        ctx = _build_channel_context({})
        assert "text-to-speech" not in ctx.lower()
        assert "substantive" in ctx.lower() or "formatting" in ctx.lower()


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
        for marker in (
            "I've created your invoice",
            "Payment processed successfully",
            "Meeting scheduled for tomorrow",
        ):
            result = _guard_output(marker, "ava")
            assert "approval process" in result

    def test_case_insensitive(self):
        result = _guard_output("i've scheduled the meeting for you", "nora")
        assert "approval process" in result


class TestMakeConversationReceipt:
    def test_receipt_has_required_fields(self):
        receipt = _make_conversation_receipt(_make_state(), "finn", "knowledge", 150)
        assert receipt["action_type"] == "agent.conversation"
        assert receipt["risk_tier"] == "green"
        assert receipt["agent_id"] == "finn"
        assert receipt["intent_type"] == "knowledge"
        assert receipt["outcome"] == "success"
        assert receipt["response_length"] == 150
        assert "id" in receipt
        assert "created_at" in receipt
        assert receipt["suite_id"] == "suite-aaa"


class TestAgentReasonNode:
    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.working_memory.get_working_memory")
    @patch("aspire_orchestrator.services.episodic_memory.get_episodic_memory")
    @patch("aspire_orchestrator.services.semantic_memory.get_semantic_memory")
    @patch("aspire_orchestrator.services.retrieval_router.get_retrieval_router")
    async def test_successful_conversation(
        self, mock_router_fn, mock_sm_fn, mock_em_fn, mock_wm_fn,
    ):
        mock_router = AsyncMock()
        mock_router.retrieve.return_value = MagicMock(
            context="",
            receipt_id="rcpt-1",
            status="not_applicable",
            degraded_reason="",
            grounding_score=1.0,
            conflict_flags=[],
        )
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

        with patch(
            "aspire_orchestrator.nodes.agent_reason.generate_text_async",
            new=AsyncMock(
                return_value="A tax write-off is a business expense that reduces your taxable income."
            ),
        ):
            result = await agent_reason_node(_make_state())

        assert "conversation_response" in result
        assert "tax" in result["conversation_response"].lower() or "write-off" in result["conversation_response"].lower()
        assert len(result["pipeline_receipts"]) >= 1
        assert mock_wm.add_turn.call_count == 2
        assert result["pipeline_receipts"][-1]["quality_report"]["passed"] is True

    @pytest.mark.asyncio
    async def test_llm_failure_returns_persona_fallback(self):
        with patch("aspire_orchestrator.services.retrieval_router.get_retrieval_router") as mock_rr, \
             patch("aspire_orchestrator.services.working_memory.get_working_memory") as mock_wm_fn, \
             patch("aspire_orchestrator.services.episodic_memory.get_episodic_memory") as mock_em_fn, \
             patch("aspire_orchestrator.services.semantic_memory.get_semantic_memory") as mock_sm_fn:
            mock_router = AsyncMock()
            mock_router.retrieve.return_value = MagicMock(
                context="",
                receipt_id="",
                status="not_applicable",
                degraded_reason="",
                grounding_score=1.0,
                conflict_flags=[],
            )
            mock_rr.return_value = mock_router
            mock_wm = AsyncMock()
            mock_wm.get_recent_turns.return_value = []
            mock_wm.add_turn.return_value = None
            mock_wm_fn.return_value = mock_wm
            mock_em_fn.return_value = AsyncMock(search_relevant_episodes=AsyncMock(return_value=[]))
            mock_sm_fn.return_value = AsyncMock(get_user_facts=AsyncMock(return_value=[]))

            with patch(
                "aspire_orchestrator.nodes.agent_reason.generate_text_async",
                new=AsyncMock(side_effect=Exception("API down")),
            ):
                result = await agent_reason_node(_make_state())

        response = result["conversation_response"]
        # Persona-specific fallback for Finn (personality pass v1.1.0-p7)
        assert "I hit a bump" in response
        assert "numbers right" in response

    @pytest.mark.asyncio
    async def test_ava_fallback_for_unknown_agent(self):
        with patch("aspire_orchestrator.services.retrieval_router.get_retrieval_router") as mock_rr, \
             patch("aspire_orchestrator.services.working_memory.get_working_memory") as mock_wm_fn, \
             patch("aspire_orchestrator.services.episodic_memory.get_episodic_memory") as mock_em_fn, \
             patch("aspire_orchestrator.services.semantic_memory.get_semantic_memory") as mock_sm_fn:
            mock_router = AsyncMock()
            mock_router.retrieve.return_value = MagicMock(
                context="",
                receipt_id="",
                status="not_applicable",
                degraded_reason="",
                grounding_score=1.0,
                conflict_flags=[],
            )
            mock_rr.return_value = mock_router
            mock_wm = AsyncMock()
            mock_wm.get_recent_turns.return_value = []
            mock_wm.add_turn.return_value = None
            mock_wm_fn.return_value = mock_wm
            mock_em_fn.return_value = AsyncMock(search_relevant_episodes=AsyncMock(return_value=[]))
            mock_sm_fn.return_value = AsyncMock(get_user_facts=AsyncMock(return_value=[]))

            with patch(
                "aspire_orchestrator.nodes.agent_reason.generate_text_async",
                new=AsyncMock(side_effect=Exception("fail")),
            ):
                result = await agent_reason_node(_make_state(agent_target="nonexistent"))

        # Unknown agent falls back to Ava's persona fallback (personality pass v1.1.0-p7)
        assert "I hit a snag" in result["conversation_response"]

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.working_memory.get_working_memory")
    @patch("aspire_orchestrator.services.episodic_memory.get_episodic_memory")
    @patch("aspire_orchestrator.services.semantic_memory.get_semantic_memory")
    @patch("aspire_orchestrator.services.retrieval_router.get_retrieval_router")
    async def test_phantom_action_stripped_in_response(
        self, mock_router_fn, mock_sm_fn, mock_em_fn, mock_wm_fn,
    ):
        mock_router = AsyncMock()
        mock_router.retrieve.return_value = MagicMock(
            context="",
            receipt_id="",
            status="not_applicable",
            degraded_reason="",
            grounding_score=1.0,
            conflict_flags=[],
        )
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

        with patch(
            "aspire_orchestrator.nodes.agent_reason.generate_text_async",
            new=AsyncMock(return_value="I've sent the invoice to your client."),
        ):
            result = await agent_reason_node(_make_state())

        assert "approval process" in result["conversation_response"]
        assert "I've sent" not in result["conversation_response"]

    @pytest.mark.asyncio
    async def test_receipt_always_generated(self):
        with patch("aspire_orchestrator.services.retrieval_router.get_retrieval_router") as mock_rr, \
             patch("aspire_orchestrator.services.working_memory.get_working_memory") as mock_wm_fn, \
             patch("aspire_orchestrator.services.episodic_memory.get_episodic_memory") as mock_em_fn, \
             patch("aspire_orchestrator.services.semantic_memory.get_semantic_memory") as mock_sm_fn:
            mock_router = AsyncMock()
            mock_router.retrieve.return_value = MagicMock(
                context="",
                receipt_id="",
                status="not_applicable",
                degraded_reason="",
                grounding_score=1.0,
                conflict_flags=[],
            )
            mock_rr.return_value = mock_router
            mock_wm = AsyncMock()
            mock_wm.get_recent_turns.return_value = []
            mock_wm.add_turn.return_value = None
            mock_wm_fn.return_value = mock_wm
            mock_em_fn.return_value = AsyncMock(search_relevant_episodes=AsyncMock(return_value=[]))
            mock_sm_fn.return_value = AsyncMock(get_user_facts=AsyncMock(return_value=[]))

            with patch(
                "aspire_orchestrator.nodes.agent_reason.generate_text_async",
                new=AsyncMock(side_effect=Exception("fail")),
            ):
                result = await agent_reason_node(_make_state())

        assert len(result["pipeline_receipts"]) >= 1
        receipt = result["pipeline_receipts"][-1]
        assert receipt["action_type"] == "agent.conversation"
        assert receipt["outcome"] == "success"
        assert "quality_report" in receipt

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.working_memory.get_working_memory")
    @patch("aspire_orchestrator.services.episodic_memory.get_episodic_memory")
    @patch("aspire_orchestrator.services.semantic_memory.get_semantic_memory")
    @patch("aspire_orchestrator.services.retrieval_router.get_retrieval_router")
    async def test_quality_guard_rewrites_ai_style_output(
        self, mock_router_fn, mock_sm_fn, mock_em_fn, mock_wm_fn,
    ):
        mock_router = AsyncMock()
        mock_router.retrieve.return_value = MagicMock(
            context="",
            receipt_id="",
            status="not_applicable",
            degraded_reason="",
            grounding_score=1.0,
            conflict_flags=[],
        )
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

        with patch(
            "aspire_orchestrator.nodes.agent_reason.generate_text_async",
            new=AsyncMock(return_value="As an AI, I'd be happy to help with that."),
        ):
            result = await agent_reason_node(
                _make_state(agent_target="eli", user_profile={"channel": "chat"})
            )

        assert "as an ai" not in result["conversation_response"].lower()
        # Quality guard replaces AI-style output with agent-specific fallback
        assert "draft" in result["conversation_response"].lower() or "eli" in result["conversation_response"].lower()
        receipt = result["pipeline_receipts"][-1]
        assert receipt["quality_report"]["passed"] is False
        assert receipt["quality_report"]["violations"]

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.services.working_memory.get_working_memory")
    @patch("aspire_orchestrator.services.episodic_memory.get_episodic_memory")
    @patch("aspire_orchestrator.services.semantic_memory.get_semantic_memory")
    @patch("aspire_orchestrator.services.retrieval_router.get_retrieval_router")
    async def test_weak_grounding_forces_grounded_fallback(
        self, mock_router_fn, mock_sm_fn, mock_em_fn, mock_wm_fn,
    ):
        mock_router = AsyncMock()
        mock_router.retrieve.return_value = MagicMock(
            context="Weak context",
            receipt_id="",
            status="degraded",
            degraded_reason="no_chunks_retrieved",
            grounding_score=0.12,
            conflict_flags=[],
        )
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

        with patch(
            "aspire_orchestrator.nodes.agent_reason.generate_text_async",
            new=AsyncMock(return_value="Here is the exact legal answer."),
        ):
            result = await agent_reason_node(
                _make_state(agent_target="clara", intent_type="knowledge", user_profile={"channel": "chat"})
            )

        assert "cautious legal read" in result["conversation_response"].lower()
        receipt = result["pipeline_receipts"][-1]
        assert receipt["retrieval_verification"]["passed"] is False
        assert receipt["retrieval_verification"]["mode"] in {"degraded", "weak_grounding"}
