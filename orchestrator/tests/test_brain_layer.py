"""Brain Layer Tests — Phase 2 Wave 1 (40 tests).

Tests cover:
  1. IntentClassifier (10 tests) — classification, fail-closed, risk tier authority
  2. SkillRouter (8 tests) — routing, compounds, delegation, risk escalation
  3. QALoop (8 tests) — governance verification, PII detection, retries
  4. POST /v1/intents/classify endpoint (8 tests) — HTTP contract, auth, receipts
  5. Pipeline integration (6 tests) — 11-node graph with Brain Layer nodes

All LLM calls are mocked — NEVER calls a real API.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import openai
import pytest

from aspire_orchestrator.models import Outcome, RiskTier
from aspire_orchestrator.services.intent_classifier import (
    CONFIDENCE_AUTO_ROUTE,
    CONFIDENCE_CLARIFY,
    IntentClassifier,
    IntentResult,
    _resolve_skill_pack,
    get_intent_classifier,
)
from aspire_orchestrator.services.qa_loop import QALoop, QAResult, QAViolation
from aspire_orchestrator.services.skill_router import (
    ExecutionStrategy,
    RoutingPlan,
    RoutingStep,
    SkillRouter,
    get_skill_router,
)


# =====================================================================
# Helpers
# =====================================================================


def _make_llm_response(
    action_type: str = "receipts.search",
    skill_pack: str = "internal",
    confidence: float = 0.95,
    entities: dict[str, Any] | None = None,
    clarification_prompt: str | None = None,
) -> dict[str, Any]:
    """Build a mock LLM JSON response for the intent classifier."""
    return {
        "action_type": action_type,
        "skill_pack": skill_pack,
        "confidence": confidence,
        "entities": entities or {},
        "clarification_prompt": clarification_prompt,
    }


def _mock_openai_completion(
    llm_response: dict[str, Any],
) -> MagicMock:
    """Build a mock OpenAI ChatCompletion wrapping a classification result."""
    choice = MagicMock()
    choice.message.content = json.dumps(llm_response)
    choice.finish_reason = "stop"
    completion = MagicMock()
    completion.choices = [choice]
    return completion


def _make_intent_result(
    action_type: str = "receipts.search",
    skill_pack: str = "internal",
    confidence: float = 0.95,
    risk_tier: RiskTier = RiskTier.GREEN,
    requires_clarification: bool = False,
) -> IntentResult:
    """Build an IntentResult for router/QA tests (no LLM needed)."""
    return IntentResult(
        action_type=action_type,
        skill_pack=skill_pack,
        confidence=confidence,
        entities={},
        risk_tier=risk_tier,
        requires_clarification=requires_clarification,
    )


def _make_valid_receipt(
    action_type: str = "receipts.search",
    outcome: str = "success",
    suite_id: str = "STE-0001",
    **overrides: Any,
) -> dict[str, Any]:
    """Build a valid receipt dict for QA tests."""
    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "suite_id": suite_id,
        "office_id": "OFF-0001",
        "actor_type": "user",
        "actor_id": "test_user",
        "action_type": action_type,
        "risk_tier": "green",
        "tool_used": "test_tool",
        "capability_token_id": str(uuid.uuid4()),
        "outcome": outcome,
        "reason_code": None,
        "receipt_hash": "abc123",
        "previous_receipt_hash": "0" * 64,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "redacted_inputs": None,
        "redacted_outputs": None,
    }
    receipt.update(overrides)
    return receipt


def _make_classify_request(
    utterance: str = "Show my calendar",
    suite_id: str = "STE-0001",
    office_id: str = "OFF-0001",
) -> dict[str, Any]:
    """Build a valid IntentRequest body for endpoint tests."""
    return {
        "schema_version": "1.0",
        "suite_id": suite_id,
        "office_id": office_id,
        "request_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_type": "intent.classify",
        "payload": {"utterance": utterance},
    }


# =====================================================================
# 1. Intent Classification Tests (10 tests)
# =====================================================================


class TestIntentClassification:
    """IntentClassifier — classify user utterances into Aspire actions."""

    @pytest.fixture(autouse=True)
    def _setup_env(self, monkeypatch):
        """Set required env vars for classifier init."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-for-ci")
        # Reset singleton between tests
        import aspire_orchestrator.services.intent_classifier as ic_mod
        ic_mod._cached_classifier = None

    @pytest.mark.asyncio
    async def test_classify_invoice_create(self, monkeypatch) -> None:
        """'Create an invoice for John' -> invoice.create, GREEN (draft creation)."""
        llm_resp = _make_llm_response(
            action_type="invoice.create",
            skill_pack="quinn_invoicing",
            confidence=0.92,
            entities={"customer_name": "John"},
        )
        mock_completion = _mock_openai_completion(llm_resp)
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            classifier = IntentClassifier()
            result = await classifier.classify("Create an invoice for John")

        assert result.action_type == "invoice.create"
        assert result.risk_tier == RiskTier.GREEN
        assert result.confidence == 0.92
        assert result.skill_pack == "quinn_invoicing"

    @pytest.mark.asyncio
    async def test_classify_payment_send(self, monkeypatch) -> None:
        """'Send $500 to supplier' -> payment.send, RED."""
        llm_resp = _make_llm_response(
            action_type="payment.send",
            skill_pack="finn_money_desk",
            confidence=0.91,
            entities={"amount_cents": 50000, "recipient": "supplier"},
        )
        mock_completion = _mock_openai_completion(llm_resp)
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            classifier = IntentClassifier()
            result = await classifier.classify("Send $500 to supplier")

        assert result.action_type == "payment.send"
        assert result.risk_tier == RiskTier.RED
        assert result.confidence == 0.91

    @pytest.mark.asyncio
    async def test_classify_calendar_read(self, monkeypatch) -> None:
        """'What's on my calendar today' -> calendar.read, GREEN."""
        llm_resp = _make_llm_response(
            action_type="calendar.read",
            skill_pack="nora_conference",
            confidence=0.97,
        )
        mock_completion = _mock_openai_completion(llm_resp)
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            classifier = IntentClassifier()
            result = await classifier.classify("What's on my calendar today")

        assert result.action_type == "calendar.read"
        assert result.risk_tier == RiskTier.GREEN
        assert result.confidence >= CONFIDENCE_AUTO_ROUTE

    @pytest.mark.asyncio
    async def test_classify_research_search(self, monkeypatch) -> None:
        """'Find plumbers near me' -> research.places, GREEN."""
        llm_resp = _make_llm_response(
            action_type="research.places",
            skill_pack="adam_research",
            confidence=0.89,
        )
        mock_completion = _mock_openai_completion(llm_resp)
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            classifier = IntentClassifier()
            result = await classifier.classify("Find plumbers near me")

        assert result.action_type == "research.places"
        assert result.risk_tier == RiskTier.GREEN
        assert result.skill_pack == "adam_research"

    @pytest.mark.asyncio
    async def test_classify_email_send(self, monkeypatch) -> None:
        """'Send email to client' -> email.send, YELLOW."""
        llm_resp = _make_llm_response(
            action_type="email.send",
            skill_pack="eli_inbox",
            confidence=0.88,
        )
        mock_completion = _mock_openai_completion(llm_resp)
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            classifier = IntentClassifier()
            result = await classifier.classify("Send email to client")

        assert result.action_type == "email.send"
        assert result.risk_tier == RiskTier.YELLOW
        assert result.skill_pack == "eli_inbox"

    @pytest.mark.asyncio
    async def test_classify_unknown_intent(self, monkeypatch) -> None:
        """'What's the meaning of life' -> unknown, confidence < 0.5."""
        llm_resp = _make_llm_response(
            action_type="unknown",
            skill_pack="internal",
            confidence=0.1,
        )
        mock_completion = _mock_openai_completion(llm_resp)
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            classifier = IntentClassifier()
            result = await classifier.classify("What's the meaning of life")

        assert result.action_type == "unknown"
        assert result.confidence == 0.0  # Unknown gets clamped to 0.0
        assert result.risk_tier == RiskTier.YELLOW  # Unknown defaults to YELLOW

    @pytest.mark.asyncio
    async def test_classify_ambiguous_intent(self, monkeypatch) -> None:
        """'Process that thing' -> requires_clarification (confidence 0.5-0.85)."""
        llm_resp = _make_llm_response(
            action_type="invoice.create",
            skill_pack="quinn_invoicing",
            confidence=0.65,
            clarification_prompt="Did you mean create an invoice or process a payment?",
        )
        mock_completion = _mock_openai_completion(llm_resp)
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            classifier = IntentClassifier()
            result = await classifier.classify("Process that thing")

        assert result.requires_clarification is True
        assert CONFIDENCE_CLARIFY <= result.confidence < CONFIDENCE_AUTO_ROUTE
        assert result.clarification_prompt is not None

    @pytest.mark.asyncio
    async def test_classify_no_api_key(self, monkeypatch) -> None:
        """Missing OPENAI_API_KEY -> fail-closed, confidence 0.0 (Law #3)."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ASPIRE_OPENAI_API_KEY", raising=False)

        import aspire_orchestrator.services.intent_classifier as ic_mod
        import aspire_orchestrator.services.llm_router as router_mod
        ic_mod._cached_classifier = None
        router_mod._cached_router = None

        classifier = IntentClassifier()
        result = await classifier.classify("Create an invoice")

        assert result.confidence == 0.0
        assert result.action_type == "unknown"
        assert result.entities.get("_fail_reason") == "intent_classifier_no_api_key"

    @pytest.mark.asyncio
    async def test_classify_llm_timeout(self, monkeypatch) -> None:
        """Simulated LLM timeout -> fail-closed (Law #3)."""
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=openai.APITimeoutError(request=MagicMock()),
        )

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            classifier = IntentClassifier()
            result = await classifier.classify("Create an invoice")

        assert result.confidence == 0.0
        assert result.action_type == "unknown"
        assert result.entities.get("_fail_reason") == "intent_classifier_timeout"

    @pytest.mark.asyncio
    async def test_classify_risk_tier_from_policy(self, monkeypatch) -> None:
        """LLM cannot override risk tier — always comes from policy YAML (Law #4)."""
        # LLM returns GREEN-tier payment.send (wrong), but policy says RED
        llm_resp = _make_llm_response(
            action_type="payment.send",
            skill_pack="finn_money_desk",
            confidence=0.95,
        )
        mock_completion = _mock_openai_completion(llm_resp)
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            classifier = IntentClassifier()
            result = await classifier.classify("Send payment")

        # Risk tier MUST come from policy_matrix.yaml, not LLM
        assert result.risk_tier == RiskTier.RED
        assert result.action_type == "payment.send"


# =====================================================================
# 2. Skill Routing Tests (8 tests)
# =====================================================================


class TestSkillRouting:
    """SkillRouter — maps intents to routing plans with governance."""

    @pytest.mark.asyncio
    async def test_route_single_green(self) -> None:
        """research.search -> single step, GREEN, sequential."""
        intent = _make_intent_result(
            action_type="research.search",
            skill_pack="adam_research",
            risk_tier=RiskTier.GREEN,
        )
        router = SkillRouter()
        plan = await router.route(intent)

        assert len(plan.steps) == 1
        assert plan.steps[0].action_type == "research.search"
        assert plan.estimated_risk_tier == RiskTier.GREEN
        assert plan.execution_strategy == ExecutionStrategy.SEQUENTIAL
        assert plan.deny_reason is None

    @pytest.mark.asyncio
    async def test_route_single_yellow(self) -> None:
        """email.send -> single step, YELLOW, approval required.

        entities include draft_id to prevent lifecycle reroute
        (email.send → email.draft happens when no existing draft).
        """
        intent = IntentResult(
            action_type="email.send",
            skill_pack="eli_inbox",
            confidence=0.95,
            entities={"draft_id": "existing-draft-123"},
            risk_tier=RiskTier.YELLOW,
            requires_clarification=False,
        )
        router = SkillRouter()
        plan = await router.route(intent)

        assert len(plan.steps) == 1
        assert plan.steps[0].action_type == "email.send"
        assert plan.estimated_risk_tier == RiskTier.YELLOW
        assert plan.steps[0].approval_required is True

    @pytest.mark.asyncio
    async def test_route_single_red(self) -> None:
        """payment.send -> single step, RED, presence required."""
        intent = _make_intent_result(
            action_type="payment.send",
            skill_pack="finn_money_desk",
            risk_tier=RiskTier.RED,
        )
        router = SkillRouter()
        plan = await router.route(intent)

        assert len(plan.steps) == 1
        assert plan.steps[0].action_type == "payment.send"
        assert plan.estimated_risk_tier == RiskTier.RED
        assert plan.steps[0].presence_required is True

    @pytest.mark.asyncio
    async def test_route_compound_sequential(self) -> None:
        """invoice.create + invoice.send -> sequential (dependency).

        invoice.send intent includes invoice_id to prevent lifecycle reroute.
        """
        intents = [
            _make_intent_result(action_type="invoice.create", risk_tier=RiskTier.GREEN),
            IntentResult(
                action_type="invoice.send",
                skill_pack="quinn_invoicing",
                confidence=0.95,
                entities={"invoice_id": "existing-invoice-123"},
                risk_tier=RiskTier.YELLOW,
                requires_clarification=False,
            ),
        ]
        router = SkillRouter()
        plan = await router.route_multi(intents)

        assert len(plan.steps) == 2
        # invoice.send depends on invoice.create
        send_step = next(s for s in plan.steps if s.action_type == "invoice.send")
        assert send_step.depends_on is not None
        assert len(send_step.depends_on) > 0
        # Strategy should be SEQUENTIAL or MIXED (has dependencies)
        assert plan.execution_strategy in (ExecutionStrategy.SEQUENTIAL, ExecutionStrategy.MIXED)

    @pytest.mark.asyncio
    async def test_route_compound_parallel(self) -> None:
        """research.search + research.places -> parallel (independent GREEN actions)."""
        intents = [
            _make_intent_result(action_type="research.search", skill_pack="adam_research", risk_tier=RiskTier.GREEN),
            _make_intent_result(action_type="research.places", skill_pack="adam_research", risk_tier=RiskTier.GREEN),
        ]
        router = SkillRouter()
        plan = await router.route_multi(intents)

        assert len(plan.steps) == 2
        assert plan.execution_strategy == ExecutionStrategy.PARALLEL
        assert plan.estimated_risk_tier == RiskTier.GREEN

    @pytest.mark.asyncio
    async def test_route_unknown_action(self) -> None:
        """unknown.action -> deny_reason set (fail-closed, Law #3)."""
        intent = _make_intent_result(
            action_type="unknown",
            skill_pack="internal",
            confidence=0.0,
        )
        router = SkillRouter()
        plan = await router.route(intent)

        assert plan.deny_reason is not None
        assert len(plan.steps) == 0

    @pytest.mark.asyncio
    async def test_route_delegation(self) -> None:
        """Cross-pack routing (invoice + email = Quinn + Eli) -> delegation_required."""
        intents = [
            _make_intent_result(action_type="invoice.create", skill_pack="quinn_invoicing", risk_tier=RiskTier.GREEN),
            _make_intent_result(action_type="email.send", skill_pack="eli_inbox", risk_tier=RiskTier.YELLOW),
        ]
        router = SkillRouter()
        plan = await router.route_multi(intents)

        assert plan.delegation_required is True
        packs = {s.skill_pack for s in plan.steps}
        assert len(packs) >= 2

    @pytest.mark.asyncio
    async def test_route_risk_escalation(self) -> None:
        """Mixed GREEN + RED -> plan risk = RED (Law #4 — max escalation)."""
        intents = [
            _make_intent_result(action_type="research.search", skill_pack="adam_research", risk_tier=RiskTier.GREEN),
            _make_intent_result(action_type="payment.send", skill_pack="finn_money_desk", risk_tier=RiskTier.RED),
        ]
        router = SkillRouter()
        plan = await router.route_multi(intents)

        assert plan.estimated_risk_tier == RiskTier.RED


# =====================================================================
# 3. QA Loop Tests (8 tests)
# =====================================================================


class TestQALoop:
    """QALoop — verify governance invariants before respond."""

    @pytest.mark.asyncio
    async def test_qa_all_checks_pass(self) -> None:
        """Valid state with complete receipts -> passed=True."""
        receipt = _make_valid_receipt()
        state = {
            "action_type": "receipts.search",
            "risk_tier": "green",
            "outcome": "success",
            "tool_used": "test_tool",
            "receipts": [receipt],
        }

        qa = QALoop()
        result = qa.verify(state)

        assert result.passed is True
        assert len(result.violations) == 0

    @pytest.mark.asyncio
    async def test_qa_missing_receipt(self) -> None:
        """No receipts -> critical violation: receipt_exists (Law #2)."""
        state = {
            "action_type": "invoice.create",
            "risk_tier": "yellow",
            "outcome": "success",
            "receipts": [],
        }

        qa = QALoop()
        result = qa.verify(state)

        assert result.passed is False
        check_names = [v.check_name for v in result.violations]
        assert "receipt_exists" in check_names
        critical_violations = [v for v in result.violations if v.severity == "critical"]
        assert len(critical_violations) > 0

    @pytest.mark.asyncio
    async def test_qa_missing_fields(self) -> None:
        """Receipt without correlation_id -> critical violation (Law #2)."""
        receipt = _make_valid_receipt()
        del receipt["correlation_id"]
        state = {
            "action_type": "receipts.search",
            "risk_tier": "green",
            "outcome": "success",
            "receipts": [receipt],
        }

        qa = QALoop()
        result = qa.verify(state)

        assert result.passed is False
        check_names = [v.check_name for v in result.violations]
        assert "receipt_has_required_fields" in check_names

    @pytest.mark.asyncio
    async def test_qa_risk_tier_no_approval(self) -> None:
        """YELLOW action without approval_evidence -> critical (Law #4)."""
        receipt = _make_valid_receipt(action_type="invoice.create", outcome="success")
        receipt["risk_tier"] = "yellow"
        state = {
            "action_type": "invoice.create",
            "risk_tier": "yellow",
            "outcome": "success",
            "receipts": [receipt],
            "approval_evidence": None,
        }

        qa = QALoop()
        result = qa.verify(state)

        assert result.passed is False
        check_names = [v.check_name for v in result.violations]
        assert "risk_tier_honored" in check_names

    @pytest.mark.asyncio
    async def test_qa_approval_missing_approver(self) -> None:
        """approval_evidence without approver_id -> critical (Law #4)."""
        receipt = _make_valid_receipt(action_type="invoice.create", outcome="success")
        state = {
            "action_type": "invoice.create",
            "risk_tier": "yellow",
            "outcome": "success",
            "receipts": [receipt],
            "approval_evidence": {
                "approval_method": "ui_button",
                # Missing approver_id
            },
        }

        qa = QALoop()
        result = qa.verify(state)

        assert result.passed is False
        check_names = [v.check_name for v in result.violations]
        assert "approval_evidence_valid" in check_names

    @pytest.mark.asyncio
    async def test_qa_outcome_inconsistent(self) -> None:
        """state=success but receipt=denied -> critical violation."""
        receipt = _make_valid_receipt(outcome="denied")
        state = {
            "action_type": "receipts.search",
            "risk_tier": "green",
            "outcome": "success",
            "receipts": [receipt],
        }

        qa = QALoop()
        result = qa.verify(state)

        assert result.passed is False
        check_names = [v.check_name for v in result.violations]
        assert "outcome_consistent" in check_names

    @pytest.mark.asyncio
    async def test_qa_pii_leak_ssn(self) -> None:
        """SSN in receipt redacted_outputs -> warning (Law #9)."""
        receipt = _make_valid_receipt()
        receipt["redacted_outputs"] = {"customer_ssn": "123-45-6789"}
        state = {
            "action_type": "receipts.search",
            "risk_tier": "green",
            "outcome": "success",
            "receipts": [receipt],
        }

        qa = QALoop()
        result = qa.verify(state)

        # SSN detection is a warning, not critical
        pii_violations = [v for v in result.violations if v.check_name == "no_pii_leak"]
        assert len(pii_violations) > 0
        assert pii_violations[0].severity == "warning"

    @pytest.mark.asyncio
    async def test_qa_capability_token_missing(self) -> None:
        """Successful execution without capability_token_id -> critical (Law #5)."""
        receipt = _make_valid_receipt()
        del receipt["capability_token_id"]
        receipt["receipt_type"] = "tool_execution"
        state = {
            "action_type": "receipts.search",
            "risk_tier": "green",
            "outcome": "success",
            "tool_used": "test_tool",
            "receipts": [receipt],
        }

        qa = QALoop()
        result = qa.verify(state)

        assert result.passed is False
        check_names = [v.check_name for v in result.violations]
        assert "capability_token_used" in check_names


# =====================================================================
# 4. Intents Endpoint Tests (8 tests)
# =====================================================================


class TestIntentsEndpoint:
    """POST /v1/intents/classify — HTTP contract, auth, receipts."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from aspire_orchestrator.server import app
        return TestClient(app)

    def _auth_headers(
        self,
        suite_id: str = "STE-0001",
        office_id: str = "OFF-0001",
    ) -> dict[str, str]:
        return {
            "x-suite-id": suite_id,
            "x-office-id": office_id,
            "x-actor-id": "test_user",
        }

    def test_classify_endpoint_success(self, client, monkeypatch) -> None:
        """Valid request -> 200 with routing plan."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        # Reset classifier singleton
        import aspire_orchestrator.services.intent_classifier as ic_mod
        ic_mod._cached_classifier = None

        llm_resp = _make_llm_response(
            action_type="research.search",
            confidence=0.95,
        )
        mock_completion = _mock_openai_completion(llm_resp)
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            resp = client.post(
                "/v1/intents/classify",
                json=_make_classify_request("Find plumbers near me"),
                headers=self._auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["route"]["action"] == "execute"
        assert data["route"]["classified_action"] == "research.search"
        assert data["plan"]["status"] == "ready"

    def test_classify_endpoint_missing_auth(self, client) -> None:
        """No X-Suite-Id -> 401 (Law #3: fail-closed)."""
        resp = client.post(
            "/v1/intents/classify",
            json=_make_classify_request(),
            headers={},  # No auth headers
        )

        assert resp.status_code == 401
        data = resp.json()
        assert data["error"] == "AUTH_REQUIRED"

    def test_classify_endpoint_invalid_body(self, client) -> None:
        """Bad JSON -> 400."""
        resp = client.post(
            "/v1/intents/classify",
            content="not json",
            headers={
                **self._auth_headers(),
                "content-type": "application/json",
            },
        )

        assert resp.status_code == 400
        data = resp.json()
        assert data["error"] == "SCHEMA_VALIDATION_FAILED"

    def test_classify_endpoint_no_utterance(self, client) -> None:
        """Missing payload.utterance -> 400."""
        request_body = _make_classify_request()
        request_body["payload"] = {}  # No utterance

        resp = client.post(
            "/v1/intents/classify",
            json=request_body,
            headers=self._auth_headers(),
        )

        assert resp.status_code == 400
        data = resp.json()
        assert data["error"] == "SCHEMA_VALIDATION_FAILED"
        assert "utterance" in data["message"]

    def test_classify_endpoint_low_confidence(self, client, monkeypatch) -> None:
        """Low confidence (<0.5) -> escalation response."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        import aspire_orchestrator.services.intent_classifier as ic_mod
        ic_mod._cached_classifier = None

        llm_resp = _make_llm_response(
            action_type="unknown",
            confidence=0.1,
        )
        mock_completion = _mock_openai_completion(llm_resp)
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            resp = client.post(
                "/v1/intents/classify",
                json=_make_classify_request("blargblargblarg"),
                headers=self._auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["route"]["action"] == "escalate"
        assert data["route"]["reason"] == "low_confidence"
        assert data["plan"]["status"] == "escalated"

    def test_classify_endpoint_clarification(self, client, monkeypatch) -> None:
        """Ambiguous intent -> clarification response."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        import aspire_orchestrator.services.intent_classifier as ic_mod
        ic_mod._cached_classifier = None

        llm_resp = _make_llm_response(
            action_type="invoice.create",
            confidence=0.65,
            clarification_prompt="Did you mean create or edit an invoice?",
        )
        mock_completion = _mock_openai_completion(llm_resp)
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            resp = client.post(
                "/v1/intents/classify",
                json=_make_classify_request("Process that thing"),
                headers=self._auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["route"]["action"] == "clarify"
        assert data["plan"]["status"] == "awaiting_clarification"
        assert data["route"]["clarification_prompt"] is not None

    def test_classify_endpoint_routing_denied(self, client, monkeypatch) -> None:
        """Action unknown to router -> denied response."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        import aspire_orchestrator.services.intent_classifier as ic_mod
        ic_mod._cached_classifier = None

        # Return a valid-looking action that actually doesn't exist in policy
        llm_resp = _make_llm_response(
            action_type="nonexistent.action",
            confidence=0.95,
        )
        mock_completion = _mock_openai_completion(llm_resp)
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            resp = client.post(
                "/v1/intents/classify",
                json=_make_classify_request("Do something weird"),
                headers=self._auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        # nonexistent.action is not in policy, so classifier maps to unknown
        # Unknown action -> confidence 0.0 -> escalation
        assert data["route"]["action"] in ("escalate", "denied")

    def test_classify_endpoint_receipt_generated(self, client, monkeypatch) -> None:
        """Every classify path generates a receipt (Law #2)."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        import aspire_orchestrator.services.intent_classifier as ic_mod
        ic_mod._cached_classifier = None

        llm_resp = _make_llm_response(
            action_type="research.search",
            confidence=0.95,
        )
        mock_completion = _mock_openai_completion(llm_resp)
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch("aspire_orchestrator.services.intent_classifier.openai.AsyncOpenAI", return_value=mock_client):
            resp = client.post(
                "/v1/intents/classify",
                json=_make_classify_request("Find plumbers near me"),
                headers=self._auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        # Every response should have receipt_ids in governance
        assert "governance" in data
        assert "receipt_ids" in data["governance"]
        assert len(data["governance"]["receipt_ids"]) > 0


# =====================================================================
# 5. Pipeline Integration Tests (6 tests)
# =====================================================================


class TestPipelineIntegration:
    """11-node graph with Brain Layer nodes — integration tests."""

    @pytest.fixture(autouse=True)
    def _no_real_llm(self, monkeypatch):
        """Remove LLM API keys so param_extract uses graceful degradation.

        server.py's load_dotenv() runs at import time (triggered by other test
        modules like test_admin_api.py), which puts OPENAI_API_KEY into
        os.environ.  Without this cleanup, param_extract makes a real LLM call,
        required-field validation fails, and the pipeline short-circuits before
        reaching approval_check — causing KeyError: 'approval_status'.
        """
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ASPIRE_OPENAI_API_KEY", raising=False)

    @pytest.fixture
    def graph(self):
        from aspire_orchestrator.graph import build_orchestrator_graph
        return build_orchestrator_graph()

    def _make_request_with_utterance(
        self,
        utterance: str,
        task_type: str = "intent.classify",
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "suite_id": "STE-0001",
            "office_id": "OFF-0001",
            "request_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": task_type,
            "payload": {"utterance": utterance},
        }

    def _mock_classifier(self, intent_result: IntentResult):
        """Create a mock IntentClassifier that returns a predictable result."""
        mock = AsyncMock()
        mock.classify = AsyncMock(return_value=intent_result)
        return mock

    @pytest.mark.asyncio
    async def test_pipeline_green_with_brain(self, graph) -> None:
        """Full 11-node GREEN path with utterance (Brain Layer active)."""
        intent = _make_intent_result(
            action_type="research.search",
            skill_pack="adam_research",
            confidence=0.95,
            risk_tier=RiskTier.GREEN,
        )
        mock_classifier = self._mock_classifier(intent)

        with patch(
            "aspire_orchestrator.services.intent_classifier.get_intent_classifier",
            return_value=mock_classifier,
        ):
            request = self._make_request_with_utterance("Find plumbers near me")
            result = await graph.ainvoke({
                "request": request,
                "actor_id": "test_user",
                "utterance": "Find plumbers near me",
            })

        assert result["safety_passed"] is True
        # Brain Layer nodes ran: intent_result and routing_plan should be populated
        assert result.get("intent_result") is not None
        assert result.get("routing_plan") is not None
        # GREEN tier auto-approves
        assert result["policy_allowed"] is True
        assert result["approval_status"] == "approved"
        # QA node should have run (qa_result in state)
        assert result.get("qa_result") is not None

    @pytest.mark.asyncio
    async def test_pipeline_yellow_with_brain(self, graph) -> None:
        """Full 11-node YELLOW path — stops at approval (Brain Layer active)."""
        intent = _make_intent_result(
            action_type="email.send",
            skill_pack="eli_inbox",
            confidence=0.92,
            risk_tier=RiskTier.YELLOW,
        )
        mock_classifier = self._mock_classifier(intent)

        with patch(
            "aspire_orchestrator.services.intent_classifier.get_intent_classifier",
            return_value=mock_classifier,
        ):
            request = self._make_request_with_utterance("Send an email to John")
            result = await graph.ainvoke({
                "request": request,
                "actor_id": "test_user",
                "utterance": "Send an email to John",
            })

        assert result["safety_passed"] is True
        assert result.get("intent_result") is not None
        # YELLOW tier requires approval
        assert result["approval_status"] == "pending"
        response = result["response"]
        assert response["error"] == "APPROVAL_REQUIRED"

    @pytest.mark.asyncio
    async def test_pipeline_backwards_compat(self, graph) -> None:
        """No utterance -> skips classify/route -> 8-node path (backwards compat)."""
        request = {
            "schema_version": "1.0",
            "suite_id": "STE-0001",
            "office_id": "OFF-0001",
            "request_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": "receipts.search",
            "payload": {"query": "recent invoices"},
        }
        # No utterance = old-style direct request
        result = await graph.ainvoke({"request": request, "actor_id": "test_user"})

        assert result["safety_passed"] is True
        # Brain Layer nodes should NOT have run
        assert result.get("intent_result") is None
        assert result.get("routing_plan") is None
        # Should still complete successfully via the 8-node path
        assert result["policy_allowed"] is True
        assert result["outcome"].value == "success"

    @pytest.mark.asyncio
    async def test_pipeline_classify_low_confidence(self, graph) -> None:
        """Utterance -> classify -> low confidence -> respond (escalation)."""
        intent = _make_intent_result(
            action_type="unknown",
            confidence=0.2,
            risk_tier=RiskTier.YELLOW,
        )
        mock_classifier = self._mock_classifier(intent)

        with patch(
            "aspire_orchestrator.services.intent_classifier.get_intent_classifier",
            return_value=mock_classifier,
        ):
            request = self._make_request_with_utterance("blarg blarg blarg")
            result = await graph.ainvoke({
                "request": request,
                "actor_id": "test_user",
                "utterance": "blarg blarg blarg",
            })

        assert result["safety_passed"] is True
        assert result.get("intent_result") is not None
        # Low confidence should route directly to respond (no routing/policy/execution)
        assert result.get("routing_plan") is None
        # Phantom execution guard: respond must NOT claim success
        response = result["response"]
        assert response is not None
        assert response["error"] == "CLASSIFICATION_UNCLEAR"
        # Must not contain execution claim language
        assert "success" not in response.get("text", "").lower() or "rephrase" in response.get("text", "").lower()

    @pytest.mark.asyncio
    async def test_pipeline_classify_preserves_explicit_task_type(self, graph) -> None:
        """When LLM returns unknown but explicit task_type is valid, preserve it.

        The mock classifier returns unknown/low-confidence, but the request has
        explicit task_type='invoice.send'. Classify should boost it.
        entities include invoice_id to prevent lifecycle reroute in route_node.
        """
        intent = IntentResult(
            action_type="unknown",
            skill_pack="internal",
            confidence=0.3,
            entities={"invoice_id": "existing-inv-123"},
            risk_tier=RiskTier.YELLOW,
            requires_clarification=False,
        )
        mock_classifier = self._mock_classifier(intent)

        with patch(
            "aspire_orchestrator.services.intent_classifier.get_intent_classifier",
            return_value=mock_classifier,
        ):
            # Explicit task_type="invoice.send" is in the policy matrix — should be preserved
            request = self._make_request_with_utterance(
                "Resend the invoice to Scott Consultants",
                task_type="invoice.send",
            )
            result = await graph.ainvoke({
                "request": request,
                "actor_id": "test_user",
                "utterance": "Resend the invoice to Scott Consultants",
            })

        assert result["safety_passed"] is True
        assert result.get("intent_result") is not None
        # Classify should have preserved the explicit task_type
        intent_data = result["intent_result"]
        assert intent_data["action_type"] == "invoice.send"
        assert intent_data["confidence"] == 0.9
        # Pipeline should continue to routing, not short-circuit to respond
        assert result.get("routing_plan") is not None
        # YELLOW tier invoice.send requires approval
        assert result["approval_status"] == "pending"
        response = result["response"]
        assert response["error"] == "APPROVAL_REQUIRED"

    @pytest.mark.asyncio
    async def test_pipeline_qa_retry(self, graph) -> None:
        """QA retry mechanism — retries once then escalates on persistent violations."""
        request = {
            "schema_version": "1.0",
            "suite_id": "STE-0001",
            "office_id": "OFF-0001",
            "request_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": "receipts.search",
            "payload": {"query": "invoices"},
        }
        result = await graph.ainvoke({"request": request, "actor_id": "test_user"})

        qa_result = result.get("qa_result", {})
        # QA node ran and produced a result
        assert qa_result is not None
        # QA retry mechanism works: retry_count tracks attempts
        assert "retry_count" in qa_result
        assert qa_result.get("max_retries") == 1
        # After retry exhaustion, retry_suggested is False
        assert qa_result.get("retry_suggested") is False

    @pytest.mark.asyncio
    async def test_pipeline_qa_escalation(self, graph) -> None:
        """QA produces meta-receipt on every path (Law #2: QA itself generates a receipt)."""
        request = {
            "schema_version": "1.0",
            "suite_id": "STE-0001",
            "office_id": "OFF-0001",
            "request_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": "receipts.search",
            "payload": {"query": "test"},
        }
        result = await graph.ainvoke({"request": request, "actor_id": "test_user"})

        # Verify QA meta-receipt was generated (Law #2: QA itself produces a receipt)
        # QA meta-receipt is stored separately to avoid breaking chain integrity
        qa_meta = result.get("qa_meta_receipt")
        assert qa_meta is not None, "QA meta-receipt must be generated (Law #2)"
        assert qa_meta["actor_id"] == "qa_loop"
        assert qa_meta["receipt_type"] == "qa_verification"
        assert "redacted_outputs" in qa_meta
        assert "violation_count" in qa_meta["redacted_outputs"]


# =====================================================================
# QA Loop Meta-Receipt Test (bonus: validates Law #2 for QA itself)
# =====================================================================


class TestQAMetaReceipt:
    """QALoop.build_meta_receipt generates a receipt for the QA verification itself."""

    def test_meta_receipt_structure(self) -> None:
        """Meta-receipt has all required fields for a valid receipt."""
        qa = QALoop()
        state = {
            "correlation_id": str(uuid.uuid4()),
            "suite_id": "STE-0001",
            "office_id": "OFF-0001",
            "action_type": "receipts.search",
        }
        qa_result = QAResult(passed=True)

        meta = qa.build_meta_receipt(state, qa_result)

        assert meta["action_type"] == "qa.verify"
        assert meta["actor_id"] == "qa_loop"
        assert meta["outcome"] == "success"
        assert meta["suite_id"] == state["suite_id"]
        assert meta["correlation_id"] == state["correlation_id"]
        assert meta["receipt_type"] == "qa_verification"
        assert meta["redacted_outputs"]["passed"] is True

    def test_meta_receipt_failed(self) -> None:
        """Meta-receipt reflects QA failure with violation details."""
        qa = QALoop()
        state = {
            "correlation_id": str(uuid.uuid4()),
            "suite_id": "STE-0001",
            "action_type": "invoice.create",
        }
        violations = [
            QAViolation(
                check_name="receipt_exists",
                severity="critical",
                message="No receipts found",
            ),
        ]
        qa_result = QAResult(
            passed=False,
            violations=violations,
            escalation_required=True,
        )

        meta = qa.build_meta_receipt(state, qa_result)

        assert meta["outcome"] == "failed"
        assert meta["reason_code"] == "qa_violations_found"
        assert meta["redacted_outputs"]["violation_count"] == 1
        assert meta["redacted_outputs"]["escalation_required"] is True
