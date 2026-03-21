"""Temporal workflow/activity I/O contracts — all dataclass models.

All types are plain dataclasses (Temporal requirement: must be serializable via
the default DataConverter). No Pydantic BaseModel here.

Enhancement #1: ApprovalEvidence / ApprovalUpdateResponse for Update validators.
Enhancement #11: OutboxStep / CompletedStep / CompensationAction for Saga.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# AvaIntentWorkflow I/O
# ---------------------------------------------------------------------------
@dataclass
class AvaIntentInput:
    """Input for AvaIntentWorkflow — wraps the full intent lifecycle."""

    suite_id: str
    office_id: str
    actor_id: str
    correlation_id: str
    thread_id: str
    initial_state: dict[str, Any]
    risk_tier: str = "green"
    requested_agent: str = "ava"


@dataclass
class AvaIntentOutput:
    """Output from AvaIntentWorkflow."""

    status: str  # "completed", "denied", "timed_out", "failed"
    response: dict[str, Any] | None = None
    error: str | None = None
    receipt_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# LangGraph Activity I/O
# ---------------------------------------------------------------------------
@dataclass
class RunLangGraphInput:
    """Input for run_langgraph_turn activity."""

    suite_id: str
    office_id: str
    actor_id: str
    thread_id: str
    correlation_id: str
    initial_state: dict[str, Any]
    approval_evidence: dict[str, Any] | None = None
    requested_agent: str = "ava"


@dataclass
class RunLangGraphOutput:
    """Output from run_langgraph_turn activity."""

    response: dict[str, Any]
    receipts: list[dict[str, Any]] = field(default_factory=list)
    requires_approval: bool = False
    approval_id: str | None = None
    approval_payload_hash: str | None = None
    requires_presence: bool = False
    presence_token: str | None = None
    current_agent: str | None = None


# ---------------------------------------------------------------------------
# Approval Evidence — Enhancement #1: Update Validator Contracts
# ---------------------------------------------------------------------------
@dataclass
class ApprovalEvidence:
    """Evidence submitted with an approval update (replaces fire-and-forget signal).

    The @update.validator checks all 8 ApprovalBindingError conditions:
    PAYLOAD_HASH_MISMATCH, APPROVAL_EXPIRED, REQUEST_ID_REUSED,
    SUITE_MISMATCH, OFFICE_MISMATCH, POLICY_VERSION_MISMATCH,
    MISSING_EVIDENCE, APPROVER_NOT_AUTHORIZED.
    """

    suite_id: str
    office_id: str
    approval_id: str
    approver_id: str
    approved: bool
    payload_hash: str
    policy_version: str
    evidence: dict[str, Any] = field(default_factory=dict)
    nonce: str = ""  # Single-use to prevent replay


@dataclass
class ApprovalUpdateResponse:
    """Response from the @workflow.update approve handler."""

    accepted: bool
    error_code: str | None = None
    message: str | None = None


# ---------------------------------------------------------------------------
# Presence Evidence
# ---------------------------------------------------------------------------
@dataclass
class PresenceEvidence:
    """Evidence for RED-tier presence confirmation."""

    suite_id: str
    office_id: str
    actor_id: str
    presence_token: str
    confirmed: bool


# ---------------------------------------------------------------------------
# Sync Activity I/O
# ---------------------------------------------------------------------------
@dataclass
class SyncWorkflowInput:
    """Input for sync_workflow_execution activity."""

    workflow_id: str
    temporal_run_id: str
    suite_id: str
    office_id: str
    correlation_id: str
    status: str
    workflow_kind: str = "intent"
    current_wait_type: str | None = None
    current_agent: str | None = None
    thread_id: str | None = None
    approval_id: str | None = None
    outbox_job_id: str | None = None
    parent_workflow_id: str | None = None
    latest_response: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Receipt Activity I/O
# ---------------------------------------------------------------------------
@dataclass
class PersistReceiptsInput:
    """Input for persist_receipts activity."""

    receipts: list[dict[str, Any]]
    suite_id: str
    correlation_id: str


@dataclass
class PersistReceiptsOutput:
    """Output from persist_receipts activity."""

    receipt_ids: list[str]
    count: int


# ---------------------------------------------------------------------------
# Event Activity I/O
# ---------------------------------------------------------------------------
@dataclass
class EmitClientEventInput:
    """Input for emit_client_event activity."""

    suite_id: str
    office_id: str
    correlation_id: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ApprovalWorkflow I/O
# ---------------------------------------------------------------------------
@dataclass
class ApprovalWorkflowInput:
    """Input for ApprovalWorkflow."""

    suite_id: str
    office_id: str
    correlation_id: str
    approval_id: str
    action_type: str
    risk_tier: str
    payload_hash: str
    policy_version: str
    parent_workflow_id: str | None = None
    max_reminders: int = 3
    reminder_interval_hours: float = 4.0
    timeout_hours: float = 24.0
    required_approvers: list[str] = field(default_factory=list)
    reminders_sent: int = 0  # Carried through continue-as-new (Enhancement #7)


@dataclass
class ApprovalWorkflowOutput:
    """Output from ApprovalWorkflow."""

    status: str  # "approved", "denied", "expired"
    approver_id: str | None = None
    evidence: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# OutboxExecutionWorkflow I/O
# ---------------------------------------------------------------------------
@dataclass
class OutboxJobInput:
    """Input for OutboxExecutionWorkflow."""

    job_id: str
    suite_id: str
    office_id: str
    correlation_id: str
    action_type: str
    risk_tier: str
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    capability_token_id: str | None = None
    provider: str | None = None


@dataclass
class OutboxJobOutput:
    """Output from OutboxExecutionWorkflow."""

    status: str  # "completed", "failed", "compensated"
    result: dict[str, Any] | None = None
    error: str | None = None
    compensation_results: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Saga — Enhancement #11: Compensation for Multi-Step Outbox
# ---------------------------------------------------------------------------
@dataclass
class OutboxStep:
    """A single step in a saga-style outbox execution."""

    step_name: str
    activity_name: str
    input: dict[str, Any]
    compensation_activity: str | None = None
    compensation_input: dict[str, Any] | None = None


@dataclass
class CompletedStep:
    """A step that completed successfully (for compensation tracking)."""

    step_name: str
    result: dict[str, Any]
    compensation_activity: str | None = None
    compensation_input: dict[str, Any] | None = None


@dataclass
class CompensationAction:
    """A compensation action to execute on saga rollback."""

    step_name: str
    activity_name: str
    input: dict[str, Any]


# ---------------------------------------------------------------------------
# ProviderCallbackWorkflow I/O
# ---------------------------------------------------------------------------
@dataclass
class CallbackInput:
    """Input for ProviderCallbackWorkflow."""

    suite_id: str
    office_id: str
    correlation_id: str
    provider: str
    ref_id: str
    parent_workflow_id: str | None = None
    timeout_hours: float = 72.0
    risk_tier: str = "green"
    agent_id: str | None = None


@dataclass
class CallbackOutput:
    """Output from ProviderCallbackWorkflow."""

    status: str  # "completed", "timed_out"
    data: dict[str, Any] | None = None


@dataclass
class CallbackData:
    """Data received from external webhook."""

    provider: str
    event_type: str
    ref_id: str
    payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# AgentFanOutWorkflow I/O
# ---------------------------------------------------------------------------
@dataclass
class AgentTask:
    """A single agent task within a fan-out."""

    agent_id: str
    skill_pack: str
    input: dict[str, Any]
    timeout_minutes: int = 5


@dataclass
class FanOutInput:
    """Input for AgentFanOutWorkflow."""

    suite_id: str
    office_id: str
    correlation_id: str
    agent_tasks: list[AgentTask] = field(default_factory=list)
    sla_timeout_minutes: int = 10
    risk_tier: str = "green"


@dataclass
class AgentResult:
    """Result from a single specialist agent."""

    agent_id: str
    status: str  # "completed", "failed", "cancelled"
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class FanOutOutput:
    """Output from AgentFanOutWorkflow."""

    results: list[AgentResult] = field(default_factory=list)
    partial: bool = False


@dataclass
class SpecialistInput:
    """Input for SpecialistAgentWorkflow (child)."""

    suite_id: str
    office_id: str
    correlation_id: str
    agent_id: str
    skill_pack: str
    input: dict[str, Any] = field(default_factory=dict)
    risk_tier: str = "green"


@dataclass
class SpecialistOutput:
    """Output from SpecialistAgentWorkflow (child)."""

    agent_id: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None
    receipt_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Provider Activity I/O
# ---------------------------------------------------------------------------
@dataclass
class ProviderCallInput:
    """Input for provider call activities."""

    suite_id: str
    office_id: str
    correlation_id: str
    provider: str
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    capability_token_id: str | None = None


@dataclass
class ProviderCallOutput:
    """Output from provider call activities."""

    success: bool
    provider: str
    action: str
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    receipt: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Async Activity Completion — Enhancement #8
# ---------------------------------------------------------------------------
@dataclass
class AsyncProviderCallInput:
    """Input for async provider call (webhook-based completion)."""

    suite_id: str
    office_id: str
    correlation_id: str
    provider: str
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    callback_url: str | None = None
    idempotency_key: str | None = None


# ---------------------------------------------------------------------------
# Outbox Activity I/O
# ---------------------------------------------------------------------------
@dataclass
class ClaimJobInput:
    """Input for claim_outbox_job activity."""

    job_id: str
    suite_id: str
    worker_id: str


@dataclass
class CompleteJobInput:
    """Input for complete_outbox_job activity."""

    job_id: str
    suite_id: str
    result: dict[str, Any]


@dataclass
class FailJobInput:
    """Input for fail_outbox_job activity."""

    job_id: str
    suite_id: str
    error: str
    retry_count: int = 0
