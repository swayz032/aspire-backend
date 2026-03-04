"""Aspire LangGraph Orchestrator Graph — the Single Brain (Law #1).

Dual-path flow (13 nodes):
  ACTION PATH (12 nodes):
    Intake → Safety → Classify → Route → ParamExtract → Policy → Approval → TokenMint → Execute → ReceiptWrite → QA → Respond

  CONVERSATION PATH (4 nodes):
    Intake → Safety → Classify → AgentReason → Respond

Backwards-compatible flow (8 nodes, when utterance is not set):
  Intake → Safety → Policy → Approval → TokenMint → Execute → ReceiptWrite → QA → Respond

Conditional routing:
  - safety_gate: BLOCKED → respond (with safety denial receipt)
  - classify: action + high confidence → route (ACTION PATH)
  - classify: conversation/knowledge/advice → agent_reason (CONVERSATION PATH)
  - classify: requires_clarification → respond (clarification prompt)
  - classify: unknown + no intent_type → agent_reason (default, don't dead-end)
  - route: deny_reason set → respond (routing denied)
  - param_extract: PARAM_EXTRACTION_FAILED → respond (missing fields prompt)
  - policy_eval: DENIED → respond (with policy denial receipt)
  - approval_check: APPROVAL_REQUIRED → respond (with approval request)
  - approval_check: PRESENCE_REQUIRED → respond (with presence request)
  - qa: retry_suggested → execute (retry loop)
  - All other paths flow through the full pipeline

This graph is the ONLY decision authority. No other component decides or executes.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from aspire_orchestrator.config.settings import settings

from aspire_orchestrator.nodes.intake import intake_node
from aspire_orchestrator.nodes.safety_gate import safety_gate_node
from aspire_orchestrator.nodes.param_extract import param_extract_node
from aspire_orchestrator.nodes.policy_eval import policy_eval_node
from aspire_orchestrator.nodes.approval_check import approval_check_node
from aspire_orchestrator.nodes.token_mint import token_mint_node
from aspire_orchestrator.nodes.execute import execute_node
from aspire_orchestrator.nodes.receipt_write import receipt_write_node
from aspire_orchestrator.nodes.agent_reason import agent_reason_node
from aspire_orchestrator.nodes.respond import respond_node
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)

# Maximum QA retries before escalation (matches QALoop._DEFAULT_MAX_RETRIES)
_QA_MAX_RETRIES = 1
_CHECKPOINTER_RUNTIME: dict[str, str] = {
    "mode": "memory",
    "backend": "MemorySaver",
}
_CHECKPOINTER_CTX: Any | None = None


# =============================================================================
# Brain Layer Nodes (Phase 2)
# =============================================================================


async def classify_node(state: OrchestratorState) -> dict[str, Any]:
    """Classify user utterance into an Aspire action (Brain Layer).

    Calls IntentClassifier.classify() and stores the result in state.
    If confidence is too low or clarification is needed, routes to respond.

    Trust-but-verify: If the request already carries an explicit task_type that
    exists in the policy matrix AND the LLM returns unknown/low-confidence,
    preserve the original task_type. The client (Desktop/Ava) knows what action
    the user requested; the LLM classifier refines but must not erase it.
    """
    from aspire_orchestrator.services.intent_classifier import get_intent_classifier

    utterance = state.get("utterance", "")
    context = state.get("context") if isinstance(state.get("context"), dict) else {}
    if not isinstance(context, dict):
        context = {}
    explicit_task_type = state.get("task_type", "")
    request_obj = state.get("request")
    payload = request_obj.get("payload", {}) if isinstance(request_obj, dict) else {}
    requested_agent = (
        state.get("requested_agent")
        or payload.get("requested_agent")
        or payload.get("agent")
        or "ava"
    )
    if isinstance(requested_agent, str):
        requested_agent = requested_agent.strip().lower() or "ava"
    context = {
        **context,
        "current_agent": requested_agent,
    }

    classifier = get_intent_classifier()
    intent_result = await classifier.classify(utterance, context)

    result: dict[str, Any] = {
        "intent_result": intent_result.model_dump(),
        "action_type": intent_result.action_type,
    }
    requested_agent = requested_agent if isinstance(requested_agent, str) else "ava"

    # Update task_type so downstream policy_eval uses the classified action
    if intent_result.action_type and intent_result.action_type != "unknown":
        result["task_type"] = intent_result.action_type
    elif explicit_task_type and explicit_task_type != "unknown":
        # LLM returned unknown/low-confidence but request has explicit task_type.
        # Preserve it — the client knows what action the user wants.
        from aspire_orchestrator.services.policy_engine import get_policy_matrix
        matrix = get_policy_matrix()
        policy_result = matrix.evaluate(explicit_task_type)
        if policy_result.allowed:
            logger.info(
                "Classify: LLM returned unknown but explicit task_type=%s is valid — preserving",
                explicit_task_type,
            )
            result["task_type"] = explicit_task_type
            # Boost intent_result so routing doesn't bail out
            result["intent_result"]["action_type"] = explicit_task_type
            result["intent_result"]["confidence"] = 0.9
            result["action_type"] = explicit_task_type

    logger.info(
        "Classify: action=%s, confidence=%.2f, clarify=%s, explicit_task=%s",
        intent_result.action_type,
        intent_result.confidence,
        intent_result.requires_clarification,
        explicit_task_type,
    )

    # If caller requested a specific specialist desk (Finn/Eli/etc),
    # preserve that persona target for conversational path handling.
    if (
        isinstance(requested_agent, str)
        and requested_agent
        and requested_agent != "ava"
        and (
            result.get("action_type") == "unknown"
            or result["intent_result"].get("intent_type") in ("conversation", "knowledge", "advice", "hybrid")
        )
    ):
        result["agent_target"] = requested_agent
        result["intent_result"]["agent_target"] = requested_agent

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

    request = state.get("request")
    current_agent = "ava"
    if hasattr(request, "payload") and isinstance(request.payload, dict):
        current_agent = (
            request.payload.get("requested_agent")
            or request.payload.get("agent")
            or "ava"
        )
    elif isinstance(request, dict):
        current_agent = request.get("requested_agent") or request.get("agent") or "ava"
    if isinstance(current_agent, str):
        current_agent = current_agent.strip().lower() or "ava"

    context = {
        "suite_id": state.get("suite_id"),
        "office_id": state.get("office_id"),
        "current_agent": current_agent,
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

        # Lifecycle reroute may have changed the action (e.g. contract.send → contract.generate).
        # Propagate the ACTUAL routed action to task_type so policy_eval evaluates the
        # correct risk tier.  Without this, policy sees the original classified action
        # (YELLOW) instead of the rerouted one (GREEN) and blocks execution.
        routed_action = routing_plan.steps[0].action_type
        current_task = state.get("task_type", "")
        if routed_action != current_task:
            logger.info(
                "Route: lifecycle reroute propagated task_type %s → %s",
                current_task, routed_action,
            )
            result["task_type"] = routed_action
            result["action_type"] = routed_action

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
    """Route after classification — dual-path: ACTION vs CONVERSATION.

    1. Action with good confidence → route (ACTION PATH)
    2. Requires clarification → respond (clarification prompt)
    3. Conversation/knowledge/advice intent → agent_reason (CONVERSATION PATH)
    4. Default unknown → agent_reason (don't dead-end)
    """
    intent_result = state.get("intent_result", {})
    confidence = intent_result.get("confidence", 0.0)
    requires_clarification = intent_result.get("requires_clarification", False)
    action_type = intent_result.get("action_type", "unknown")
    intent_type = intent_result.get("intent_type", "")

    # ACTION PATH: known action with sufficient confidence
    if action_type != "unknown" and confidence >= 0.5:
        if requires_clarification:
            return "respond"
        return "route"

    # CONVERSATION PATH: classifier identified conversational intent
    if intent_type in ("conversation", "knowledge", "advice", "hybrid"):
        return "agent_reason"

    # Default: route to agent_reason rather than dead-ending
    return "agent_reason"


def _route_after_route(state: OrchestratorState) -> str:
    """Route after skill router.

    If routing denied → respond. Otherwise → param_extract.
    """
    routing_plan = state.get("routing_plan", {})
    if routing_plan.get("deny_reason"):
        return "respond"
    return "param_extract"


def _route_after_param_extract(state: OrchestratorState) -> str:
    """Route after parameter extraction.

    If extraction failed (missing required fields) → respond.
    Otherwise → policy_eval.
    """
    if state.get("error_code") == "PARAM_EXTRACTION_FAILED":
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


async def build_orchestrator_graph() -> StateGraph:
    """Build the Aspire orchestrator StateGraph with 13 nodes.

    Dual-path architecture:
    - ACTION PATH: classify → route → param_extract → policy → approval → execute
    - CONVERSATION PATH: classify → agent_reason → respond

    Backwards compatible: if utterance is not set, classify/route/param_extract are skipped.

    Returns a compiled graph ready for invocation.
    """
    graph = StateGraph(OrchestratorState)

    # Add all 13 nodes (8 existing + 3 Brain Layer + 1 param_extract + 1 agent_reason)
    graph.add_node("intake", intake_node)
    graph.add_node("safety_gate", safety_gate_node)
    graph.add_node("classify", classify_node)
    graph.add_node("route", route_node)
    graph.add_node("param_extract", param_extract_node)
    graph.add_node("policy_eval", policy_eval_node)
    graph.add_node("approval_check", approval_check_node)
    graph.add_node("token_mint", token_mint_node)
    graph.add_node("execute", execute_node)
    graph.add_node("receipt_write", receipt_write_node)
    graph.add_node("qa", qa_node)
    graph.add_node("agent_reason", agent_reason_node)
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

    # classify → route (ACTION) OR agent_reason (CONVERSATION) OR respond (clarification)
    graph.add_conditional_edges("classify", _route_after_classify, {
        "route": "route",
        "agent_reason": "agent_reason",
        "respond": "respond",
    })

    # agent_reason → respond (conversation path always ends at respond)
    graph.add_edge("agent_reason", "respond")

    # route → param_extract OR respond (routing denied)
    graph.add_conditional_edges("route", _route_after_route, {
        "param_extract": "param_extract",
        "respond": "respond",
    })

    # param_extract → policy_eval OR respond (extraction failed)
    graph.add_conditional_edges("param_extract", _route_after_param_extract, {
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

    # Enable thread-scoped checkpointing so HITL/resume and multi-turn continuity
    # can use LangGraph's configurable.thread_id contract.
    checkpointer = await _build_checkpointer()
    return graph.compile(checkpointer=checkpointer)


def get_checkpointer_runtime() -> dict[str, str]:
    """Expose checkpointer runtime metadata for readiness diagnostics."""
    return dict(_CHECKPOINTER_RUNTIME)


async def close_checkpointer_runtime() -> None:
    """Close persistent checkpointer resources on shutdown."""
    global _CHECKPOINTER_CTX
    if _CHECKPOINTER_CTX is None:
        return
    try:
        if hasattr(_CHECKPOINTER_CTX, "__aexit__"):
            await _CHECKPOINTER_CTX.__aexit__(None, None, None)
        else:
            _CHECKPOINTER_CTX.__exit__(None, None, None)
    except Exception:
        # Best-effort shutdown cleanup; service is terminating anyway.
        pass
    finally:
        _CHECKPOINTER_CTX = None


async def _build_checkpointer() -> Any:
    """Build checkpointer from environment (memory|postgres)."""
    global _CHECKPOINTER_CTX
    mode = (settings.langgraph_checkpointer or "memory").strip().lower()
    aspire_env = os.environ.get("ASPIRE_ENV", "").strip().lower()
    allow_memory_failover_in_prod = os.environ.get("ASPIRE_ALLOW_MEMORY_CHECKPOINTER_IN_PROD", "").strip() == "1"
    if aspire_env == "production" and mode != "postgres" and not allow_memory_failover_in_prod:
        raise RuntimeError(
            "Production requires ASPIRE_LANGGRAPH_CHECKPOINTER=postgres (memory is dev-only).",
        )
    if mode == "postgres":
        dsn = (settings.langgraph_postgres_dsn or "").strip()
        if not dsn:
            raise RuntimeError(
                "ASPIRE_LANGGRAPH_POSTGRES_DSN is required when ASPIRE_LANGGRAPH_CHECKPOINTER=postgres",
            )
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            # AsyncPostgresSaver is async-context-manager based.
            # Initialize it once at startup and keep it open for process lifetime.
            _CHECKPOINTER_CTX = AsyncPostgresSaver.from_conn_string(dsn)
            saver = await _CHECKPOINTER_CTX.__aenter__()
            try:
                await saver.setup()
            except Exception as setup_err:
                # Supabase/PgBouncer can surface duplicate prepared-statement errors
                # even when migrations are already applied. Treat this as non-fatal.
                if "DuplicatePreparedStatement" not in str(type(setup_err)) and "prepared statement" not in str(setup_err):
                    raise
            _CHECKPOINTER_RUNTIME["mode"] = "postgres"
            _CHECKPOINTER_RUNTIME["backend"] = "AsyncPostgresSaver"
            return saver
        except Exception as e:
            try:
                if _CHECKPOINTER_CTX is not None and hasattr(_CHECKPOINTER_CTX, "__aexit__"):
                    await _CHECKPOINTER_CTX.__aexit__(None, None, None)
            except Exception:
                pass
            _CHECKPOINTER_CTX = None
            logger.exception("Postgres checkpointer init failed, falling back to MemorySaver: %s", e)
            _CHECKPOINTER_RUNTIME["mode"] = "memory-fallback"
            _CHECKPOINTER_RUNTIME["backend"] = "MemorySaver"
            return MemorySaver()

    _CHECKPOINTER_RUNTIME["mode"] = "memory"
    _CHECKPOINTER_RUNTIME["backend"] = "MemorySaver"
    return MemorySaver()
