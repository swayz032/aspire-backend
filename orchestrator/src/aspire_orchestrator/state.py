"""LangGraph state schema for the Aspire orchestrator.

This TypedDict defines the state that flows through all 8 graph nodes:
Intake → Safety → Policy → Approval → TokenMint → Execute → ReceiptWrite → Respond

Each node reads and writes to this shared state.
"""

from __future__ import annotations

from typing import Any, TypedDict

from aspire_orchestrator.models import (
    ActorType,
    ApprovalEvidence,
    AvaOrchestratorRequest,
    Outcome,
    RiskTier,
)


class OrchestratorState(TypedDict, total=False):
    """Shared state for the LangGraph orchestrator graph.

    Fields are populated progressively as the request flows through nodes.
    Required fields are set by the intake node; optional fields are set by
    subsequent nodes in the pipeline.
    """

    # --- Intake (set by intake node) ---
    request: AvaOrchestratorRequest
    correlation_id: str
    request_id: str
    suite_id: str  # Derived from auth, NOT from client payload
    office_id: str  # Derived from auth, NOT from client payload
    actor_type: ActorType
    actor_id: str
    task_type: str
    timestamp: str

    # --- Safety Gate (set by safety_gate node) ---
    safety_passed: bool
    safety_block_reason: str | None

    # --- Policy Evaluation (set by policy_eval node) ---
    risk_tier: RiskTier
    policy_allowed: bool
    policy_deny_reason: str | None
    allowed_tools: list[str]
    required_approvals: list[str]
    presence_required: bool

    # --- Approval Check (set by approval_check node) ---
    approval_status: str  # pending | approved | rejected | expired
    approval_evidence: ApprovalEvidence | None
    approval_payload_hash: str | None
    presence_token: dict[str, Any] | None  # RED tier presence token

    # --- Auth Context (set by gateway, used by intake for tenant derivation) ---
    auth_suite_id: str | None  # JWT-derived suite_id from gateway
    auth_office_id: str | None  # JWT-derived office_id from gateway
    auth_actor_id: str | None  # JWT-derived user ID from gateway

    # --- Token Mint (set by token_mint node) ---
    capability_token_id: str | None
    capability_token_hash: str | None
    capability_token: dict[str, Any] | None  # Full token for execute-node validation

    # --- Execute (set by execute node) ---
    tool_used: str | None
    execution_result: dict[str, Any] | None
    outcome: Outcome

    # --- Receipt Write (set by receipt_write node) ---
    receipt_ids: list[str]

    # --- Respond (set by respond node) ---
    response: dict[str, Any] | None

    # --- Error handling ---
    error_code: str | None
    error_message: str | None

    # --- Accumulated receipts for the full pipeline ---
    pipeline_receipts: list[dict[str, Any]]
