"""Persona & Verbosity Verification Tests.

Tests that:
1. All 13 agent personas load correctly (no fallback to Ava)
2. Each persona contains the GPT-5.2 Output Discipline section
3. Channel-based verbosity instructions are injected correctly
4. Voice-channel responses use LOW verbosity, chat uses MEDIUM
5. The assigned_agent field flows through all response paths
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from aspire_orchestrator.nodes.respond import (
    _load_agent_persona,
    _resolve_agent_id,
    _call_openai_sync,
    _llm_conversational_reply,
    respond_node,
)


# ── Persona Loading Tests ──────────────────────────────────────────

PERSONA_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "aspire_orchestrator"
    / "config"
    / "pack_personas"
)

ALL_AGENT_IDS = [
    "ava",
    "ava_admin",
    "finn",
    "finn_fm",
    "eli",
    "quinn",
    "nora",
    "sarah",
    "adam",
    "tec",
    "teressa",
    "milo",
    "clara",
    "mail_ops",
]


class TestPersonaLoading:
    """Verify all agent personas load from their .md files."""

    @pytest.mark.parametrize("agent_id", ALL_AGENT_IDS)
    def test_persona_loads_not_fallback(self, agent_id: str) -> None:
        """Each agent must load its own persona, not fall back to Ava's built-in."""
        persona = _load_agent_persona(agent_id)
        assert persona, f"Persona for {agent_id} returned empty"
        # The built-in fallback contains this exact phrase
        builtin_fallback = "You are Ava, the AI executive assistant for Aspire."
        if agent_id not in ("ava",):
            assert persona != builtin_fallback, (
                f"Agent {agent_id} fell back to built-in persona"
            )

    @pytest.mark.parametrize("agent_id", ALL_AGENT_IDS)
    def test_persona_has_gpt52_discipline(self, agent_id: str) -> None:
        """Each persona must contain the GPT-5.2 Output Discipline section."""
        persona = _load_agent_persona(agent_id)
        assert "Output Discipline (GPT-5.2)" in persona, (
            f"Agent {agent_id} persona missing GPT-5.2 Output Discipline section"
        )
        assert "Never pad with filler" in persona
        assert "Stay within your skill pack domain" in persona
        assert "Do not volunteer information not explicitly asked for" in persona


# ── Verbosity Injection Tests ──────────────────────────────────────

class TestVerbosityInjection:
    """Verify channel-based verbosity is injected into OpenAI calls."""

    @patch("aspire_orchestrator.nodes.respond.openai")
    @patch("aspire_orchestrator.nodes.respond.os")
    def test_voice_channel_injects_low_verbosity(self, mock_os, mock_openai) -> None:
        """Voice channel must inject LOW verbosity instruction."""
        mock_os.environ.get.return_value = "test-key"
        mock_client = MagicMock()
        mock_openai.OpenAI.return_value = mock_client
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "test response"
        mock_client.chat.completions.create.return_value = mock_response

        messages = [{"role": "system", "content": "You are Ava."}]
        _call_openai_sync(messages, model="gpt-4o", channel="voice")

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        system_msg = call_kwargs["messages"][0]["content"]
        assert "Verbosity: LOW" in system_msg
        assert "1-2 sentences max" in system_msg

    @patch("aspire_orchestrator.nodes.respond.openai")
    @patch("aspire_orchestrator.nodes.respond.os")
    def test_chat_channel_injects_medium_verbosity(self, mock_os, mock_openai) -> None:
        """Chat channel must inject MEDIUM verbosity instruction."""
        mock_os.environ.get.return_value = "test-key"
        mock_client = MagicMock()
        mock_openai.OpenAI.return_value = mock_client
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "test response"
        mock_client.chat.completions.create.return_value = mock_response

        messages = [{"role": "system", "content": "You are Ava."}]
        _call_openai_sync(messages, model="gpt-4o", channel="chat")

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        system_msg = call_kwargs["messages"][0]["content"]
        assert "Verbosity: MEDIUM" in system_msg
        assert "3-5 sentences" in system_msg

    @patch("aspire_orchestrator.nodes.respond.openai")
    @patch("aspire_orchestrator.nodes.respond.os")
    def test_reasoning_model_uses_developer_role(self, mock_os, mock_openai) -> None:
        """GPT-5 models must use 'developer' role instead of 'system'."""
        mock_os.environ.get.return_value = "test-key"
        mock_client = MagicMock()
        mock_openai.OpenAI.return_value = mock_client
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "test response"
        mock_client.chat.completions.create.return_value = mock_response

        messages = [{"role": "system", "content": "You are Ava."}]
        _call_openai_sync(messages, model="gpt-5-mini", channel="voice")

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        # System should be rewritten to developer for reasoning models
        assert call_kwargs["messages"][0]["role"] == "developer"
        assert "Verbosity: LOW" in call_kwargs["messages"][0]["content"]

    @patch("aspire_orchestrator.nodes.respond.openai")
    @patch("aspire_orchestrator.nodes.respond.os")
    def test_no_system_message_still_injects_verbosity(self, mock_os, mock_openai) -> None:
        """When no system message exists, verbosity is prepended as new system message."""
        mock_os.environ.get.return_value = "test-key"
        mock_client = MagicMock()
        mock_openai.OpenAI.return_value = mock_client
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "test response"
        mock_client.chat.completions.create.return_value = mock_response

        messages = [{"role": "user", "content": "Hello"}]
        _call_openai_sync(messages, model="gpt-4o", channel="voice")

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        # A system message should be prepended
        assert call_kwargs["messages"][0]["role"] == "system"
        assert "Verbosity: LOW" in call_kwargs["messages"][0]["content"]


# ── Agent Resolution Tests ─────────────────────────────────────────

class TestAgentResolution:
    """Verify agent ID resolution from state."""

    def test_payment_domain_resolves_to_finn(self) -> None:
        state = {"task_type": "payment.send"}
        assert _resolve_agent_id(state) == "finn"

    def test_finance_domain_resolves_to_finn_fm(self) -> None:
        state = {"task_type": "finance.snapshot"}
        assert _resolve_agent_id(state) == "finn_fm"

    def test_explicit_agent_field_takes_priority(self) -> None:
        state = {"task_type": "unknown", "request": {"agent": "eli"}}
        assert _resolve_agent_id(state) == "eli"

    def test_unknown_domain_falls_back_to_ava(self) -> None:
        state = {"task_type": "unknown.something"}
        assert _resolve_agent_id(state) == "ava"


# ── Assigned Agent Pipeline Tests ──────────────────────────────────

class TestAssignedAgentPipeline:
    """Verify assigned_agent flows through all response paths."""

    def test_error_response_includes_assigned_agent(self) -> None:
        """Error responses must include assigned_agent."""
        state = {
            "correlation_id": "test-123",
            "request_id": "req-123",
            "error_code": "SAFETY_BLOCKED",
            "error_message": "blocked",
            "assigned_agent": "eli",
            "pipeline_receipts": [],
            "receipt_ids": [],
        }
        result = respond_node(state)
        assert result["response"]["assigned_agent"] == "eli"

    def test_greeting_response_includes_assigned_agent(self) -> None:
        """__greeting__ sentinel must include assigned_agent."""
        state = {
            "correlation_id": "test-123",
            "request_id": "req-123",
            "error_code": None,
            "utterance": "__greeting__",
            "assigned_agent": "ava",
            "pipeline_receipts": [],
            "receipt_ids": [],
            "outcome": "success",
            "risk_tier": "green",
        }
        result = respond_node(state)
        assert result["response"]["assigned_agent"] == "ava"
        assert "How can I help" in result["response"]["text"]


# ── Persona Behavior Verification Concept ──────────────────────────

class TestPersonaBehaviorConcept:
    """Conceptual test: how to verify persona behavior end-to-end.

    These tests document the verification approach. In a full integration
    test with a live LLM, you would:

    1. Send "who are you?" to each agent
    2. Verify the response contains the agent's name and role
    3. Compare voice-channel vs chat-channel response lengths

    Since we mock the LLM in unit tests, these tests verify the
    infrastructure that makes persona behavior work:
    - Correct persona loaded
    - Correct verbosity injected
    - Correct agent name in conversational prompt
    """

    AGENT_IDENTITY_EXPECTATIONS = {
        "ava": ("Ava", "Chief of Staff"),
        "finn_fm": ("Finn", "Finance Manager"),
        "eli": ("Eli", "Inbox"),
        "nora": ("Nora", "Conference"),
        "sarah": ("Sarah", "Front Desk"),
        "adam": ("Adam", "Research"),
        "quinn": ("Quinn", "Invoicing"),
        "tec": ("Tec", "Documents"),
        "teressa": ("Teressa", "Bookkeeping"),
        "milo": ("Milo", "Payroll"),
        "clara": ("Clara", "Legal"),
    }

    @pytest.mark.parametrize(
        "agent_id,expected",
        AGENT_IDENTITY_EXPECTATIONS.items(),
    )
    def test_persona_contains_identity(self, agent_id: str, expected: tuple[str, str]) -> None:
        """Each persona must contain the agent's name and role keyword."""
        name, role_keyword = expected
        persona = _load_agent_persona(agent_id)
        assert name in persona, f"Persona for {agent_id} doesn't mention name '{name}'"
        assert role_keyword.lower() in persona.lower(), (
            f"Persona for {agent_id} doesn't mention role '{role_keyword}'"
        )
