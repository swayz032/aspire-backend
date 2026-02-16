"""Aspire LangGraph Orchestrator Graph — the Single Brain (Law #1).

Phase 2 canonical flow (11 nodes):
  Intake → Safety → Classify → Route → Policy → Approval → TokenMint → Execute → ReceiptWrite → QA → Respond

Backwards-compatible flow (8 nodes, when utterance is not set):
  Intake → Safety → Policy → Approval → TokenMint → Execute → ReceiptWrite → QA → Respond

Conditional routing:
  - safety_gate: BLOCKED → respond (with safety denial receipt)
  - classify: low confidence (<0.5) → respond (escalation)
  - classify: requires_clarification → respond (clarification prompt)
  - route: deny_reason set → respond (routing denied)
  - policy_eval: DENIED → respond (with policy denial receipt)
  - approval_check: APPROVAL_REQUIRED → respond (with approval request)
  - approval_check: PRESENCE_REQUIRED → respond (with presence request)
  - qa: retry_suggested → execute (retry loop)
  - All other paths flow through the full pipeline

This graph is the ONLY decision authority. No other component decides or executes.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from aspire_orchestrator.nodes.intake import intake_node
from aspire_orchestrator.nodes.safety_gate import safety_gate_node
from aspire_orchestrator.nodes.policy_eval import policy_eval_node
from aspire_orchestrator.nodes.approval_check import approval_check_node
from aspire_orchestrator.nodes.token_mint import token_mint_node
from aspire_orchestrator.nodes.execute import execute_node
from aspire_orchestrator.nodes.receipt_write import receipt_write_node
from aspire_orchestrator.nodes.respond import respond_node
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)

# Maximum QA retries before escalation (matches QALoop._DEFAULT_MAX_RETRIES)
_QA_MAX_RETRIES = 1


# =============================================================================
# Brain Layer Nodes (Phase 2)
# =============================================================================


async def classify_node(state: OrchestratorState) -> dict[str, Any]:
    """Classify user utterance into an Aspire action (Brain Layer).

    Calls IntentClassifier.classify() and stores the result in state.
    If confidence is too low or clarification is needed, routes to respond.
    """
    from aspire_orchestrator.services.intent_classifier import get_intent_classifier

    utterance = state.get("utterance", "")
    context = state.get("context") if isinstance(state.get("context"), dict) else None

    classifier = get_intent_classifier()
    intent_result = await classifier.classify(utterance, context)

    result: dict[str, Any] = {
        "intent_result": intent_result.model_dump(),
        "action_type": intent_result.action_type,
    }

    # Update task_type so downstream policy_eval uses the classified action
    if intent_result.action_type and intent_result.action_type != "unknown":
        result["task_type"] = intent_result.action_type

    logger.info(
        "Classify: action=%s, confidence=%.2f, clarify=%s",
        intent_result.action_type,
        intent_result.confidence,
        intent_result.requires_clarification,
    )

    return result


async def route_node(state: OrchestratorState) -> dict[str, Any]:
    """Route classified intent to skill pack(s) (Brain Layer).

    Calls SkillRouter.route() and stores the RoutingPlan in state.
    If routing is denied, sets error state for respond node.
    """
    from aspire_orchestrator.services.intent_classifier import IntentResult
    from aspire_orchestrator.services.skill_router import get_skill_router

    intent_data = state.get("intent_result", {})
    intent_result = IntentResult(**intent_data)

    context = {
        "suite_id": state.get("suite_id"),
        "office_id": state.get("office_id"),
        "current_agent": "ava",
    }

    router = get_skill_router()
    routing_plan = await router.route(intent_result, context=context)

    result: dict[str, Any] = {
        "routing_plan": routing_plan.model_dump(),
    }

    if routing_plan.deny_reason:
        logger.warning("Route DENIED: %s", routing_plan.deny_reason)
        result["error_code"] = "ROUTING_DENIED"
        result["error_message"] = f"Routing denied: {routing_plan.deny_reason}"
        result["outcome"] = "denied"
        return result

    # Set state fields used by downstream nodes
    result["risk_tier"] = routing_plan.estimated_risk_tier
    if routing_plan.steps:
        result["tool_used"] = routing_plan.steps[0].tools[0] if routing_plan.steps[0].tools else None

    logger.info(
        "Route: steps=%d, risk=%s, strategy=%s",
        len(routing_plan.steps),
        routing_plan.estimated_risk_tier.value,
        routing_plan.execution_strategy.value,
    )

    return result


def qa_node(state: OrchestratorState) -> dict[str, Any]:
    """QA verification gate — verify governance before respond (Brain Layer).

    Runs after receipt_write, before respond. Checks all governance invariants.
    Can suggest retry (routes back to execute) or escalation.
    """
    from aspire_orchestrator.services.qa_loop import QALoop

    qa = QALoop()

    # Build the state dict that QA expects (receipts come from pipeline_receipts)
    qa_state: dict[str, Any] = dict(state)
    qa_state["receipts"] = list(state.get("pipeline_receipts", []))
    qa_state["qa_retry_count"] = state.get("qa_retry_count", 0)

    qa_result = qa.verify(qa_state)

    # Build meta-receipt (Law #2: QA loop itself generates a receipt)
    meta_receipt = qa.build_meta_receipt(qa_state, qa_result)

    # Store QA meta-receipt separately — pipeline_receipts are already chain-hashed
    # by receipt_write_node. Appending an unhashed receipt would break chain integrity.
    # The respond node can include qa_meta_receipt in the response if needed.
    result: dict[str, Any] = {
        "qa_result": qa_result.model_dump(),
        "qa_meta_receipt": meta_receipt,
    }

    if qa_result.escalation_required:
        logger.warning(
            "QA ESCALATION: violations=%d, retries exhausted",
            len(qa_result.violations),
        )
        result["error_code"] = "QA_ESCALATION"
        result["error_message"] = (
            f"QA verification failed with {len(qa_result.violations)} violation(s) "
            f"after {qa_result.retry_count} retries"
        )

    if qa_result.retry_suggested:
        logger.info(
            "QA retry suggested: retry_count=%d, max=%d",
            qa_result.retry_count,
            qa_result.max_retries,
        )
        result["qa_retry_count"] = state.get("qa_retry_count", 0) + 1

    return result


# =============================================================================
# Routing Functions
# =============================================================================


def _route_after_safety(state: OrchestratorState) -> str:
    """Route after safety gate.

    If blocked → respond. If utterance set → classify. Otherwise → policy (backwards compat).
    """
    if not state.get("safety_passed", False):
        return "respond"

    # Phase 2: if utterance is set, go through Brain Layer (classify → route)
    if state.get("utterance"):
        return "classify"

    # Backwards compat: old-style requests go directly to policy
    return "policy_eval"


def _route_after_classify(state: OrchestratorState) -> str:
    """Route after classification.

    Low confidence (<0.5) or unknown → respond (escalation).
    Requires clarification → respond (with clarification prompt).
    Otherwise → route node.
    """
    intent_result = state.get("intent_result", {})
    confidence = intent_result.get("confidence", 0.0)
    requires_clarification = intent_result.get("requires_clarification", False)
    action_type = intent_result.get("action_type", "unknown")

    if action_type == "unknown" or confidence < 0.5:
        return "respond"

    if requires_clarification:
        return "respond"

    return "route"


def _route_after_route(state: OrchestratorState) -> str:
    """Route after skill router.

    If routing denied → respond. Otherwise → policy_eval.
    """
    routing_plan = state.get("routing_plan", {})
    if routing_plan.get("deny_reason"):
        return "respond"
    return "policy_eval"


def _route_after_policy(state: OrchestratorState) -> str:
    """Route after policy evaluation: if denied, go to respond."""
    if not state.get("policy_allowed", False):
        return "respond"
    return "approval_check"


def _route_after_approval(state: OrchestratorState) -> str:
    """Route after approval check.

    - If approval is needed but not provided: respond with ApprovalRequest
    - If presence is needed but not provided: respond with PresenceRequest
    - If approved: proceed to token minting
    """
    approval_status = state.get("approval_status", "pending")
    if approval_status == "pending":
        # Approval still needed — return ApprovalRequest to client
        return "respond"
    if state.get("presence_required", False) and approval_status == "approved":
        # Red tier: check if presence token is valid
        # If presence_required is still True after approval_check, it means
        # presence was not yet verified — respond with PresenceRequest
        error_code = state.get("error_code")
        if error_code == "PRESENCE_REQUIRED":
            return "respond"
    if approval_status in ("rejected", "expired"):
        return "respond"
    return "token_mint"


def _route_after_qa(state: OrchestratorState) -> str:
    """Route after QA verification.

    If retry suggested and under limit → execute (retry loop).
    Otherwise → respond.
    """
    qa_result = state.get("qa_result", {})
    retry_suggested = qa_result.get("retry_suggested", False)
    retry_count = state.get("qa_retry_count", 0)

    if retry_suggested and retry_count <= _QA_MAX_RETRIES:
        logger.info("QA routing to execute for retry (attempt %d)", retry_count)
        return "execute"

    return "respond"


# =============================================================================
# Graph Builder
# =============================================================================


def build_orchestrator_graph() -> StateGraph:
    """Build the Aspire orchestrator StateGraph with 11 nodes.

    Phase 2 adds 3 Brain Layer nodes: classify, route, qa.
    Backwards compatible: if utterance is not set, classify/route are skipped.

    Returns a compiled graph ready for invocation.
    """
    graph = StateGraph(OrchestratorState)

    # Add all 11 nodes (8 existing + 3 Brain Layer)
    graph.add_node("intake", intake_node)
    graph.add_node("safety_gate", safety_gate_node)
    graph.add_node("classify", classify_node)
    graph.add_node("route", route_node)
    graph.add_node("policy_eval", policy_eval_node)
    graph.add_node("approval_check", approval_check_node)
    graph.add_node("token_mint", token_mint_node)
    graph.add_node("execute", execute_node)
    graph.add_node("receipt_write", receipt_write_node)
    graph.add_node("qa", qa_node)
    graph.add_node("respond", respond_node)

    # Set entry point
    graph.set_entry_point("intake")

    # Linear edges (unconditional)
    graph.add_edge("intake", "safety_gate")
    graph.add_edge("token_mint", "execute")
    graph.add_edge("execute", "receipt_write")
    graph.add_edge("receipt_write", "qa")  # Phase 2: receipt_write → qa (was → respond)
    graph.add_edge("respond", END)

    # Conditional edges (branching)

    # safety_gate → classify (utterance) OR policy_eval (backwards compat) OR respond (blocked)
    graph.add_conditional_edges("safety_gate", _route_after_safety, {
        "classify": "classify",
        "policy_eval": "policy_eval",
        "respond": "respond",
    })

    # classify → route OR respond (low confidence / clarification needed)
    graph.add_conditional_edges("classify", _route_after_classify, {
        "route": "route",
        "respond": "respond",
    })

    # route → policy_eval OR respond (routing denied)
    graph.add_conditional_edges("route", _route_after_route, {
        "policy_eval": "policy_eval",
        "respond": "respond",
    })

    graph.add_conditional_edges("policy_eval", _route_after_policy, {
        "approval_check": "approval_check",
        "respond": "respond",
    })
    graph.add_conditional_edges("approval_check", _route_after_approval, {
        "token_mint": "token_mint",
        "respond": "respond",
    })

    # qa → respond OR execute (retry)
    graph.add_conditional_edges("qa", _route_after_qa, {
        "respond": "respond",
        "execute": "execute",
    })

    return graph.compile()
