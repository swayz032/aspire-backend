"""Aspire LangGraph Orchestrator Graph — the Single Brain (Law #1).

Canonical flow from architecture.md:
  Intake → Safety(NeMo) → Policy → Approval → TokenMint → Execute → ReceiptWrite → Respond

Conditional routing:
  - safety_gate: BLOCKED → respond (with safety denial receipt)
  - policy_eval: DENIED → respond (with policy denial receipt)
  - approval_check: APPROVAL_REQUIRED → respond (with approval request)
  - approval_check: PRESENCE_REQUIRED → respond (with presence request)
  - All other paths flow through the full pipeline

This graph is the ONLY decision authority. No other component decides or executes.
"""

from __future__ import annotations

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


def _route_after_safety(state: OrchestratorState) -> str:
    """Route after safety gate: if blocked, go to respond with denial."""
    if not state.get("safety_passed", False):
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


def build_orchestrator_graph() -> StateGraph:
    """Build the Aspire orchestrator StateGraph with 8 nodes.

    Returns a compiled graph ready for invocation.
    """
    graph = StateGraph(OrchestratorState)

    # Add all 8 nodes
    graph.add_node("intake", intake_node)
    graph.add_node("safety_gate", safety_gate_node)
    graph.add_node("policy_eval", policy_eval_node)
    graph.add_node("approval_check", approval_check_node)
    graph.add_node("token_mint", token_mint_node)
    graph.add_node("execute", execute_node)
    graph.add_node("receipt_write", receipt_write_node)
    graph.add_node("respond", respond_node)

    # Set entry point
    graph.set_entry_point("intake")

    # Linear edges (unconditional)
    graph.add_edge("intake", "safety_gate")
    graph.add_edge("token_mint", "execute")
    graph.add_edge("execute", "receipt_write")
    graph.add_edge("receipt_write", "respond")
    graph.add_edge("respond", END)

    # Conditional edges (branching)
    graph.add_conditional_edges("safety_gate", _route_after_safety, {
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

    return graph.compile()
