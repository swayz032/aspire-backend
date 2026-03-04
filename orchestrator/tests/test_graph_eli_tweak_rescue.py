from __future__ import annotations

import sys
import types

import pytest

pytest.importorskip("langgraph")


class _FakeIntentResult:
    def __init__(self) -> None:
        self.action_type = "unknown"
        self.skill_pack = "ava_orchestrator"
        self.confidence = 0.4
        self.entities = {}
        self.risk_tier = "green"
        self.requires_clarification = False
        self.clarification_prompt = None
        self.raw_llm_response = None
        self.intent_type = "conversation"
        self.agent_target = None

    def model_dump(self) -> dict:
        return {
            "action_type": self.action_type,
            "skill_pack": self.skill_pack,
            "confidence": self.confidence,
            "entities": self.entities,
            "risk_tier": self.risk_tier,
            "requires_clarification": self.requires_clarification,
            "clarification_prompt": self.clarification_prompt,
            "raw_llm_response": self.raw_llm_response,
            "intent_type": self.intent_type,
            "agent_target": self.agent_target,
        }


class _FakeClassifier:
    async def classify(self, utterance: str, context: dict | None = None):  # noqa: ARG002
        return _FakeIntentResult()


class _FakeClarifyIntentResult(_FakeIntentResult):
    def __init__(self) -> None:
        super().__init__()
        self.intent_type = "unknown"
        self.requires_clarification = True


class _FakeClarifyClassifier:
    async def classify(self, utterance: str, context: dict | None = None):  # noqa: ARG002
        return _FakeClarifyIntentResult()


class _FakeOfficeIntentResult(_FakeIntentResult):
    def __init__(self) -> None:
        super().__init__()
        self.action_type = "internal.office.draft"
        self.intent_type = "action"
        self.confidence = 0.92


class _FakeOfficeClassifier:
    async def classify(self, utterance: str, context: dict | None = None):  # noqa: ARG002
        return _FakeOfficeIntentResult()


@pytest.mark.asyncio
async def test_graph_classify_rescues_eli_tweak_to_email_draft(monkeypatch) -> None:
    from aspire_orchestrator import graph as graph_mod

    fake_module = types.SimpleNamespace(
        get_intent_classifier=lambda: _FakeClassifier(),
    )
    monkeypatch.setitem(sys.modules, "aspire_orchestrator.services.intent_classifier", fake_module)

    state = {
        "utterance": "revise that draft and make it shorter",
        "task_type": "assistant.chat",
        "request": {
            "payload": {
                "requested_agent": "eli",
                "agent": "eli",
            },
        },
    }
    out = await graph_mod.classify_node(state)  # type: ignore[arg-type]
    assert out["action_type"] == "email.draft"
    assert out["task_type"] == "email.draft"
    assert out["intent_result"]["skill_pack"] == "eli_inbox"
    assert out["intent_result"]["intent_type"] == "action"


@pytest.mark.asyncio
async def test_graph_classify_rescues_eli_tweak_when_clarification_requested(monkeypatch) -> None:
    from aspire_orchestrator import graph as graph_mod

    fake_module = types.SimpleNamespace(
        get_intent_classifier=lambda: _FakeClarifyClassifier(),
    )
    monkeypatch.setitem(sys.modules, "aspire_orchestrator.services.intent_classifier", fake_module)

    class _Req:
        payload = {"requested_agent": "eli", "agent": "eli"}

    state = {
        "utterance": "make this draft warmer and shorter",
        "task_type": "assistant.chat",
        "request": _Req(),
    }
    out = await graph_mod.classify_node(state)  # type: ignore[arg-type]
    assert out["action_type"] == "email.draft"
    assert out["task_type"] == "email.draft"
    assert out["intent_result"]["skill_pack"] == "eli_inbox"
    assert out["intent_result"]["requires_clarification"] is False


@pytest.mark.asyncio
async def test_graph_classify_rescues_eli_tweak_when_office_draft_misfire(monkeypatch) -> None:
    from aspire_orchestrator import graph as graph_mod

    fake_module = types.SimpleNamespace(
        get_intent_classifier=lambda: _FakeOfficeClassifier(),
    )
    monkeypatch.setitem(sys.modules, "aspire_orchestrator.services.intent_classifier", fake_module)

    state = {
        "utterance": "make this draft warmer and shorter",
        "task_type": "assistant.chat",
        "request": {
            "payload": {
                "requested_agent": "eli",
                "agent": "eli",
            },
        },
    }
    out = await graph_mod.classify_node(state)  # type: ignore[arg-type]
    assert out["action_type"] == "email.draft"
    assert out["task_type"] == "email.draft"
    assert out["intent_result"]["agent_target"] == "eli"
