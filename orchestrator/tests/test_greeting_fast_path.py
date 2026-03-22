"""Tests for the greeting fast path node."""

import pytest

from aspire_orchestrator.nodes.greeting_fast_path import (
    is_greeting,
    greeting_response,
    greeting_fast_path_node,
)


class TestGreetingDetector:
    @pytest.mark.parametrize("utterance", [
        "hello", "Hi!", "hey", "good morning", "what's up",
        "how are you", "yo", "testing", "greetings",
        "Good afternoon!", "How are you doing?",
    ])
    def test_greetings_detected(self, utterance: str) -> None:
        assert is_greeting(utterance) is True

    @pytest.mark.parametrize("utterance", [
        "create an invoice for $500",
        "what's the status of my QuickBooks sync",
        "schedule a meeting with John tomorrow at 3pm",
        "hello can you send an email to the client about the project update",
        "research competitors in the painting industry",
        "",
    ])
    def test_non_greetings_rejected(self, utterance: str) -> None:
        assert is_greeting(utterance) is False

    def test_max_word_limit(self) -> None:
        long = "hello " * 11
        assert is_greeting(long.strip()) is False


class TestGreetingResponse:
    def test_known_agents(self) -> None:
        for agent in ["ava", "nora", "eli", "finn", "sarah", "adam"]:
            resp = greeting_response(agent)
            assert isinstance(resp, str)
            assert len(resp) > 5

    def test_unknown_agent_defaults_to_ava(self) -> None:
        resp = greeting_response("unknown_agent")
        assert isinstance(resp, str)


class TestGreetingFastPathNode:
    def test_greeting_sets_fast_path(self) -> None:
        state: dict = {"utterance": "hello", "requested_agent": "ava"}
        result = greeting_fast_path_node(state)
        assert result["_greeting_fast_path"] is True
        assert result["conversation_response"]
        assert result["governance"]["receipt_ids"]
        assert result["status"] == "success"
        assert result["assigned_agent"] == "ava"
        assert len(result["pipeline_receipts"]) == 1
        assert result["pipeline_receipts"][0]["action"] == "greeting.fast_path"

    def test_non_greeting_passes_through(self) -> None:
        state: dict = {"utterance": "create an invoice", "requested_agent": "ava"}
        result = greeting_fast_path_node(state)
        assert result["_greeting_fast_path"] is False
        assert "conversation_response" not in result

    def test_greeting_with_finn(self) -> None:
        state: dict = {"utterance": "hey", "requested_agent": "finn"}
        result = greeting_fast_path_node(state)
        assert result["_greeting_fast_path"] is True
        assert result["assigned_agent"] == "finn"

    def test_receipt_has_required_fields(self) -> None:
        state: dict = {"utterance": "hi", "requested_agent": "ava"}
        result = greeting_fast_path_node(state)
        receipt = result["_fast_path_receipt"]
        assert "id" in receipt
        assert "receipt_id" in receipt
        assert receipt["risk_tier"] == "green"
        assert receipt["result"] == "success"
        assert receipt["receipt_hash"] == ""  # Unpersisted marker for safety net

    def test_empty_utterance_not_greeting(self) -> None:
        state: dict = {"utterance": "", "requested_agent": "ava"}
        result = greeting_fast_path_node(state)
        assert result["_greeting_fast_path"] is False
