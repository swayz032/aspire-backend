"""LangGraph state schema for the Aspire orchestrator.

This TypedDict defines the state that flows through all 11 graph nodes:
Intake → Safety → Classify → Route → Policy → Approval → TokenMint → Execute → ReceiptWrite → QA → Respond

Phase 2 added 3 Brain Layer nodes: Classify, Route, QA.
Backwards compat: if utterance is not set, classify/route are skipped (old-style direct requests).

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
    thread_id: str | None  # LangGraph thread identifier for checkpoint continuity
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

    # --- Idempotency + Outbox (Phase 3 W5) ---
    idempotency_key: str | None  # Unique key for state-changing ops (YELLOW/RED)

    # --- Execute (set by execute node) ---
    tool_used: str | None
    assigned_agent: str | None  # Agent that owns this execution (from A2A dispatch)
    execution_result: dict[str, Any] | None
    outcome: Outcome

    # --- Draft-First Execution (Phase 3 — param_extract / narration / approval bridge) ---
    execution_params: dict[str, Any] | None   # Extracted structured params from NL
    draft_id: str | None                       # approval_requests.approval_id for persisted drafts
    draft_persistence_status: str | None       # skipped | success | failed (enterprise observability)
    narration_text: str | None                 # Deterministic response text from narration layer
    advisor_context: dict[str, Any] | None     # Built by context_builder (v1.5 playbooks + staff)

    # --- Receipt Write (set by receipt_write node) ---
    receipt_ids: list[str]

    # --- Respond (set by respond node) ---
    response: dict[str, Any] | None

    # --- Error handling ---
    error_code: str | None
    error_message: str | None

    # --- Brain Layer (Phase 2 — classify / route / qa) ---
    utterance: str | None  # Raw user input for intent classification
    intent_result: dict[str, Any] | None  # From IntentClassifier
    routing_plan: dict[str, Any] | None  # From SkillRouter
    qa_result: dict[str, Any] | None  # From QALoop
    qa_meta_receipt: dict[str, Any] | None  # QA verification receipt (Law #2)
    qa_retry_count: int  # Retry counter for QA loop re-execution

    # --- Conversational Intelligence (Wave 1) ---
    intent_type: str | None          # "action" | "conversation" | "knowledge" | "advice" | "hybrid"
    agent_target: str | None         # suggested agent: "ava" | "finn" | "eli" | "quinn" | etc.
    conversation_response: str | None # output from agent_reason_node
    user_profile: dict[str, Any] | None  # injected from Desktop request
    session_id: str | None           # for memory scoping
    eli_deliverability_signals: dict[str, Any] | None  # optional postmaster/provider health inputs
    eli_rag_status: str | None       # primary | degraded | offline
    eli_fallback_mode: bool | None   # true when deterministic fallback was used
    eli_rag_sources: list[str] | None
    eli_iteration_count: int | None
    eli_agentic_plan: dict[str, Any] | None
    eli_quality_report: dict[str, Any] | None

    # --- Accumulated receipts for the full pipeline ---
    pipeline_receipts: list[dict[str, Any]]
