"""Aspire Pydantic v2 models — generated from canonical schemas.

Source schemas:
- plan/schemas/receipts.schema.v1.yaml
- plan/schemas/capability-token.schema.v1.yaml
- plan/schemas/risk-tiers.enum.yaml
- plan/schemas/outcome-status.enum.yaml
- plan/schemas/approval-status.enum.yaml
- plan/schemas/tenant-identity.yaml
- plan/contracts/ava-user/ava_orchestrator_request.schema.json
- plan/contracts/ava-user/ava_result.schema.json

DO NOT EDIT MANUALLY — regenerate from schemas when they change.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


# =============================================================================
# Enums (canonical values from YAML schemas)
# =============================================================================


class RiskTier(str, Enum):
    """Risk tier classification — Law #4. Use green/yellow/red, never low/medium/high."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class Outcome(str, Enum):
    """Receipt outcome status — Law #2. Every outcome generates a receipt."""

    SUCCESS = "success"
    DENIED = "denied"
    FAILED = "failed"
    TIMEOUT = "timeout"
    PENDING = "pending"


class ApprovalStatus(str, Enum):
    """Approval status — 'rejected' (approval) vs 'denied' (receipt outcome)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELED = "canceled"


class ActorType(str, Enum):
    """Who initiated the action."""

    USER = "user"
    SYSTEM = "system"
    AGENT = "agent"
    SCHEDULER = "scheduler"


class ApprovalMethod(str, Enum):
    """How approval was granted."""

    VOICE_CONFIRM = "voice_confirm"
    VIDEO_AUTHORITY = "video_authority"
    UI_BUTTON = "ui_button"
    DUAL_APPROVAL = "dual_approval"


class ReceiptType(str, Enum):
    """Receipt type categories per receipt_emission_rules.md."""

    DECISION_INTAKE = "decision_intake"
    POLICY_DECISION = "policy_decision"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    TOOL_EXECUTION = "tool_execution"
    RESEARCH_RUN = "research_run"
    EXCEPTION_CARD_GENERATED = "exception_card_generated"
    RITUAL_GENERATED = "ritual_generated"
    PRESENCE_VERIFIED = "presence_verified"
    PRESENCE_MISSING = "presence_missing"

    # Robot infrastructure receipt types (Wave 3)
    ROBOT_RUN_COMPLETED = "robot.run.completed"
    INCIDENT_OPENED = "incident.opened"

    # Ops receipt types (Phase 2.5 — Enterprise Coverage)
    DEPLOY_STARTED = "deploy.started"
    DEPLOY_CANARY_DEPLOYED = "deploy.canary.deployed"
    DEPLOY_PROMOTED = "deploy.promoted"
    DEPLOY_ROLLED_BACK = "deploy.rolled_back"
    DEPLOY_FAILED = "deploy.failed"
    SLO_METRIC_ROLLUP = "slo.metric.rollup"
    SLO_BREACH_DETECTED = "slo.breach.detected"
    ALERT_TRIGGERED = "alert.triggered"
    BACKUP_COMPLETED = "backup.completed"
    RESTORE_TESTED = "restore.tested"
    DR_DRILL_COMPLETED = "dr.drill.completed"
    ENTITLEMENT_PLAN_CHANGED = "entitlement.plan.changed"
    ENTITLEMENT_SEAT_ADDED = "entitlement.seat.added"
    ENTITLEMENT_SEAT_REMOVED = "entitlement.seat.removed"
    ENTITLEMENT_USAGE_CAPPED = "entitlement.usage.capped"
    ENTITLEMENT_GRACE_STARTED = "entitlement.grace.started"
    ENTITLEMENT_GRACE_ENDED = "entitlement.grace.ended"
    RBAC_ROLE_GRANTED = "rbac.role.granted"
    RBAC_ROLE_REVOKED = "rbac.role.revoked"
    RBAC_PERMISSION_ESCALATED = "rbac.permission.escalated"

    # Kill switch receipt types (Wave 4)
    KILL_SWITCH_ACTIVATED = "kill_switch.activated"
    KILL_SWITCH_MODE_CHANGED = "kill_switch.mode_changed"

    # Council service receipt types (Wave 5)
    COUNCIL_SESSION_CREATED = "council.session.created"
    COUNCIL_MEMBER_PROPOSAL = "council.member.proposal"
    COUNCIL_DECISION = "council.decision"

    # Learning loop receipt types (Wave 5)
    LEARNING_OBJECT_CREATED = "learning.object.created"
    EVAL_RUN_COMPLETED = "eval.run.completed"
    LEARNING_CHANGE_PROPOSED = "learning.change.proposed"
    LEARNING_CHANGE_APPROVED = "learning.change.approved"
    LEARNING_OBJECT_PROMOTED = "learning.object.promoted"


# =============================================================================
# Receipt (from receipts.schema.v1.yaml)
# =============================================================================


class ApprovalEvidence(BaseModel):
    """Evidence of approval for YELLOW/RED tier actions."""

    approver_id: str
    approval_method: ApprovalMethod
    session_id: UUID | None = None
    approved_at: datetime


class Receipt(BaseModel):
    """Immutable audit trail record — Law #2: No Action Without a Receipt.

    NO UPDATE/DELETE. Corrections are new receipts.
    """

    id: UUID
    correlation_id: UUID
    suite_id: UUID
    office_id: UUID
    actor_type: ActorType
    actor_id: str
    action_type: str
    risk_tier: RiskTier
    tool_used: str
    capability_token_id: UUID | None = None
    capability_token_hash: str | None = None
    created_at: datetime
    approved_at: datetime | None = None
    executed_at: datetime | None = None
    approval_evidence: ApprovalEvidence | None = None
    outcome: Outcome
    reason_code: str | None = None
    redacted_inputs: dict[str, Any] | None = None
    redacted_outputs: dict[str, Any] | None = None
    previous_receipt_hash: str | None = None
    receipt_hash: str


# =============================================================================
# Capability Token (from capability-token.schema.v1.yaml)
# =============================================================================


class CapabilityToken(BaseModel):
    """Capability token — Law #5: Short-lived (<60s), scoped, server-verified.

    Only the LangGraph orchestrator mints tokens.
    """

    token_id: UUID
    suite_id: UUID
    office_id: UUID
    tool: str
    scopes: list[str] = Field(min_length=1)
    issued_at: datetime
    expires_at: datetime
    signature: str
    revoked: bool = False
    correlation_id: UUID

    @model_validator(mode="after")
    def _validate_ttl(self) -> "CapabilityToken":
        """Law #5: Token TTL must be < 60 seconds."""
        ttl = (self.expires_at - self.issued_at).total_seconds()
        if ttl >= 60:
            raise ValueError(
                f"Capability token TTL {ttl}s >= 60s maximum (Law #5). "
                f"issued_at={self.issued_at.isoformat()}, expires_at={self.expires_at.isoformat()}"
            )
        if ttl <= 0:
            raise ValueError(
                f"Capability token TTL {ttl}s <= 0: token already expired at issue time"
            )
        return self


# =============================================================================
# AvaOrchestratorRequest (from ava_orchestrator_request.schema.json)
# =============================================================================


class AvaOrchestratorRequest(BaseModel):
    """Inbound request to the orchestrator — POST /v1/intents.

    Note: suite_id/office_id are validated against auth context.
    The orchestrator derives the authoritative suite_id from JWT, NOT from this payload.
    """

    schema_version: str = Field(pattern=r"^1\.0$")
    suite_id: str = Field(min_length=1)
    office_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    timestamp: datetime
    task_type: str = Field(min_length=1)
    payload: dict[str, Any]


# =============================================================================
# AvaResult (from ava_result.schema.json)
# =============================================================================


class AvaResultRisk(BaseModel):
    """Risk assessment included in the result."""

    tier: RiskTier


class AvaResultGovernance(BaseModel):
    """Governance metadata — approvals, tokens, receipt chain."""

    approvals_required: list[str]
    presence_required: bool
    capability_token_required: bool
    receipt_ids: list[str]


class AvaResult(BaseModel):
    """Response from the orchestrator — returned after processing an intent.

    Validated against schema before returning (egress validation).
    """

    schema_version: str = Field(pattern=r"^1\.0$")
    request_id: str
    correlation_id: str
    text: str = Field(
        description="Human-readable response text for voice/chat output. "
        "This is what Ava speaks to the user via ElevenLabs TTS.",
    )
    route: dict[str, Any]
    risk: AvaResultRisk
    governance: AvaResultGovernance
    plan: dict[str, Any]


# =============================================================================
# Error Codes (from architecture.md fail-closed error codes)
# =============================================================================


class AspireErrorCode(str, Enum):
    """Fail-closed error codes from architecture.md."""

    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_BINDING_FAILED = "APPROVAL_BINDING_FAILED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    PRESENCE_REQUIRED = "PRESENCE_REQUIRED"
    PRESENCE_INVALID = "PRESENCE_INVALID"
    CAPABILITY_TOKEN_REQUIRED = "CAPABILITY_TOKEN_REQUIRED"
    CAPABILITY_TOKEN_EXPIRED = "CAPABILITY_TOKEN_EXPIRED"
    TENANT_ISOLATION_VIOLATION = "TENANT_ISOLATION_VIOLATION"
    POLICY_DENIED = "POLICY_DENIED"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"
    RECEIPT_WRITE_FAILED = "RECEIPT_WRITE_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AspireError(BaseModel):
    """Structured error response from the orchestrator."""

    error: AspireErrorCode
    message: str
    correlation_id: str
    receipt_id: str | None = None
