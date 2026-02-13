"""Approval Check Node — Yellow/Red tier governance (Law #4).

Responsibilities:
1. GREEN tier: auto-approve (no user confirmation needed)
2. YELLOW tier: verify approval binding (payload_hash, replay defense)
3. RED tier: verify approval binding + presence token
4. If approval missing: return ApprovalRequest with payload_hash
5. If approval expired/rejected: deny with receipt
6. Emit approval_requested or approval_granted/denied receipt

Per approval_binding_spec.md:
  - payload_hash = SHA-256 of canonical JSON of execution payload
  - Binding: suite_id + request_id + payload_hash + policy_version
  - Reject mismatched payload_hash (approve-then-swap defense)
  - Reject expired approvals
  - Reject reused request_id

Per presence_sessions.md:
  - RED tier requires presence_token (TTL <=5min, nonce bound to payload_hash)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import (
    AspireErrorCode,
    Outcome,
    ReceiptType,
    RiskTier,
)
from aspire_orchestrator.services.approval_service import (
    ApprovalBinding,
    ApprovalBindingError,
    compute_payload_hash,
    verify_approval_binding,
)
from aspire_orchestrator.services.presence_service import (
    PresenceError,
    verify_presence_token,
)
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)


def _extract_execution_payload(state: OrchestratorState) -> dict[str, Any]:
    """Extract the execution payload for approval hash binding.

    The payload includes the exact parameters that will be executed,
    bound to the tenant context. This prevents approve-then-swap attacks.
    """
    request = state.get("request")
    payload: dict[str, Any] = {}

    if request is not None:
        if hasattr(request, "payload"):
            payload = request.payload if isinstance(request.payload, dict) else {}
        elif isinstance(request, dict):
            payload = request.get("payload", {})

    return {
        "task_type": state.get("task_type", "unknown"),
        "parameters": payload,
        "suite_id": state.get("suite_id", ""),
        "office_id": state.get("office_id", ""),
    }


def _make_receipt(
    *,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    actor_type: str,
    actor_id: str,
    action_type: str,
    risk_tier: str,
    outcome: str,
    reason_code: str,
    receipt_type: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a receipt dict with standard fields."""
    receipt: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action_type": action_type,
        "risk_tier": risk_tier,
        "tool_used": "orchestrator.approval_check",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "reason_code": reason_code,
        "receipt_type": receipt_type,
        "receipt_hash": "",
    }
    if details:
        receipt["details"] = details
    return receipt


def _map_binding_error_to_error_code(
    error: ApprovalBindingError,
) -> AspireErrorCode:
    """Map approval binding errors to Aspire error codes."""
    if error == ApprovalBindingError.APPROVAL_EXPIRED:
        return AspireErrorCode.APPROVAL_EXPIRED
    if error in (
        ApprovalBindingError.SUITE_MISMATCH,
        ApprovalBindingError.OFFICE_MISMATCH,
    ):
        return AspireErrorCode.TENANT_ISOLATION_VIOLATION
    # payload_hash mismatch, request_id reused, policy_version mismatch
    return AspireErrorCode.APPROVAL_BINDING_FAILED


def _map_presence_error_to_error_code(
    error: PresenceError,
) -> AspireErrorCode:
    """Map presence verification errors to Aspire error codes."""
    if error == PresenceError.TOKEN_MISSING:
        return AspireErrorCode.PRESENCE_REQUIRED
    if error in (
        PresenceError.SUITE_MISMATCH,
        PresenceError.OFFICE_MISMATCH,
    ):
        return AspireErrorCode.TENANT_ISOLATION_VIOLATION
    return AspireErrorCode.PRESENCE_INVALID


def approval_check_node(state: OrchestratorState) -> dict[str, Any]:
    """Check approval status for the current request.

    GREEN tier: auto-approve
    YELLOW tier: require user approval evidence with payload_hash binding
    RED tier: require user approval + presence token verification
    """
    if state.get("error_code"):
        return {"approval_status": "denied"}

    risk_tier = state.get("risk_tier", RiskTier.YELLOW)
    correlation_id = state.get("correlation_id", str(uuid.uuid4()))
    suite_id = state.get("suite_id", "unknown")
    office_id = state.get("office_id", "unknown")
    request_id = state.get("request_id", str(uuid.uuid4()))
    risk_tier_value = risk_tier.value if isinstance(risk_tier, RiskTier) else risk_tier

    # GREEN tier: auto-approve with receipt (Law #2)
    if risk_tier == RiskTier.GREEN:
        logger.info(
            "GREEN tier auto-approve: correlation=%s, suite=%s",
            correlation_id[:8], suite_id[:8],
        )
        receipt = _make_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_type="system",
            actor_id="orchestrator.approval_check",
            action_type="approval.auto_approve",
            risk_tier="green",
            outcome=Outcome.SUCCESS.value,
            reason_code="GREEN_AUTO_APPROVED",
            receipt_type=ReceiptType.APPROVAL_GRANTED.value,
        )
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(receipt)
        return {
            "approval_status": "approved",
            "approval_evidence": None,
            "pipeline_receipts": existing_receipts,
        }

    # --- YELLOW/RED tier: Compute payload hash for approval binding ---
    execution_payload = _extract_execution_payload(state)
    payload_hash = compute_payload_hash(execution_payload)

    # Check if approval evidence exists in the request
    approval_evidence = state.get("approval_evidence")

    if approval_evidence is None:
        # No approval yet — return ApprovalRequest to client
        logger.info(
            "Approval required: tier=%s, correlation=%s, suite=%s",
            risk_tier_value, correlation_id[:8], suite_id[:8],
        )

        receipt = _make_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_type="system",
            actor_id="orchestrator",
            action_type="approval.request",
            risk_tier=risk_tier_value,
            outcome=Outcome.PENDING.value,
            reason_code=AspireErrorCode.APPROVAL_REQUIRED.value,
            receipt_type=ReceiptType.APPROVAL_REQUESTED.value,
            details={"payload_hash": payload_hash},
        )
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(receipt)

        error_msg = f"{'Red' if risk_tier == RiskTier.RED else 'Yellow'}-tier action requires approval"
        if risk_tier == RiskTier.RED:
            error_msg += " with presence verification"

        return {
            "approval_status": "pending",
            "approval_payload_hash": payload_hash,
            "error_code": AspireErrorCode.APPROVAL_REQUIRED.value,
            "error_message": error_msg,
            "outcome": Outcome.PENDING,
            "pipeline_receipts": existing_receipts,
        }

    # --- Approval evidence exists — verify binding ---

    # Extract fields from ApprovalEvidence (Pydantic model or dict)
    if hasattr(approval_evidence, "approver_id"):
        approver_id = approval_evidence.approver_id
        approved_at_raw = approval_evidence.approved_at
    elif isinstance(approval_evidence, dict):
        approver_id = approval_evidence.get("approver_id", "unknown")
        approved_at_raw = approval_evidence.get("approved_at")
    else:
        # Fail closed: unrecognized evidence format
        logger.warning(
            "Approval binding REJECTED: unrecognized evidence format, correlation=%s",
            correlation_id[:8],
        )
        receipt = _make_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_type="system",
            actor_id="orchestrator",
            action_type="approval.deny",
            risk_tier=risk_tier_value,
            outcome=Outcome.DENIED.value,
            reason_code=AspireErrorCode.APPROVAL_BINDING_FAILED.value,
            receipt_type=ReceiptType.APPROVAL_DENIED.value,
            details={"reason": "Unrecognized approval evidence format"},
        )
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(receipt)
        return {
            "approval_status": "rejected",
            "error_code": AspireErrorCode.APPROVAL_BINDING_FAILED.value,
            "error_message": "Unrecognized approval evidence format",
            "outcome": Outcome.DENIED,
            "pipeline_receipts": existing_receipts,
        }

    # Parse approved_at to datetime
    if isinstance(approved_at_raw, str):
        approved_at = datetime.fromisoformat(approved_at_raw)
        if approved_at.tzinfo is None:
            approved_at = approved_at.replace(tzinfo=timezone.utc)
    elif isinstance(approved_at_raw, datetime):
        approved_at = approved_at_raw
        if approved_at.tzinfo is None:
            approved_at = approved_at.replace(tzinfo=timezone.utc)
    else:
        approved_at = datetime.now(timezone.utc)

    # Build ApprovalBinding from the evidence
    # The evidence should contain the payload_hash that was computed at approval time
    evidence_payload_hash = ""
    if hasattr(approval_evidence, "payload_hash"):
        evidence_payload_hash = approval_evidence.payload_hash
    elif isinstance(approval_evidence, dict):
        evidence_payload_hash = approval_evidence.get("payload_hash", payload_hash)
    else:
        evidence_payload_hash = payload_hash

    # Get policy_version from evidence or use current
    from aspire_orchestrator.services.approval_service import CURRENT_POLICY_VERSION

    evidence_policy_version = ""
    if hasattr(approval_evidence, "policy_version"):
        evidence_policy_version = approval_evidence.policy_version
    elif isinstance(approval_evidence, dict):
        evidence_policy_version = approval_evidence.get(
            "policy_version", CURRENT_POLICY_VERSION
        )
    else:
        evidence_policy_version = CURRENT_POLICY_VERSION

    # Get request_id from evidence or use current
    evidence_request_id = ""
    if hasattr(approval_evidence, "request_id"):
        evidence_request_id = approval_evidence.request_id
    elif isinstance(approval_evidence, dict):
        evidence_request_id = approval_evidence.get("request_id", request_id)
    else:
        evidence_request_id = request_id

    binding = ApprovalBinding(
        suite_id=suite_id,
        office_id=office_id,
        request_id=evidence_request_id,
        payload_hash=evidence_payload_hash,
        policy_version=evidence_policy_version,
        approved_at=approved_at,
        expires_at=approved_at,  # Will be replaced below
        approver_id=approver_id,
    )

    # Compute actual expiry from evidence or default (5 minutes)
    from datetime import timedelta

    from aspire_orchestrator.services.approval_service import (
        DEFAULT_APPROVAL_EXPIRY_SECONDS,
    )

    if hasattr(approval_evidence, "expires_at") and approval_evidence.expires_at:
        expires_at = approval_evidence.expires_at
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
    elif isinstance(approval_evidence, dict) and "expires_at" in approval_evidence:
        expires_at = approval_evidence["expires_at"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = approved_at + timedelta(seconds=DEFAULT_APPROVAL_EXPIRY_SECONDS)

    # Reconstruct with correct expires_at (frozen dataclass)
    binding = ApprovalBinding(
        suite_id=binding.suite_id,
        office_id=binding.office_id,
        request_id=binding.request_id,
        payload_hash=binding.payload_hash,
        policy_version=binding.policy_version,
        approved_at=binding.approved_at,
        expires_at=expires_at,
        approver_id=binding.approver_id,
    )

    # Verify the approval binding (7-check defense)
    binding_result = verify_approval_binding(
        binding,
        expected_suite_id=suite_id,
        expected_office_id=office_id,
        expected_request_id=request_id,
        expected_payload_hash=payload_hash,
    )

    if not binding_result.valid:
        # Approval binding verification FAILED — deny (Law #3: fail closed)
        error_code = _map_binding_error_to_error_code(binding_result.error)
        logger.warning(
            "Approval binding REJECTED: error=%s, msg=%s, correlation=%s",
            binding_result.error.value if binding_result.error else "unknown",
            binding_result.error_message,
            correlation_id[:8],
        )

        receipt = _make_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_type="user",
            actor_id=approver_id,
            action_type="approval.deny",
            risk_tier=risk_tier_value,
            outcome=Outcome.DENIED.value,
            reason_code=error_code.value,
            receipt_type=ReceiptType.APPROVAL_DENIED.value,
            details={
                "binding_error": binding_result.error.value if binding_result.error else "unknown",
                "binding_message": binding_result.error_message or "",
            },
        )
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(receipt)

        return {
            "approval_status": "rejected",
            "approval_payload_hash": payload_hash,
            "error_code": error_code.value,
            "error_message": binding_result.error_message or "Approval binding verification failed",
            "outcome": Outcome.DENIED,
            "pipeline_receipts": existing_receipts,
        }

    # --- Approval binding verified ---

    # RED tier: also verify presence token
    if risk_tier == RiskTier.RED:
        presence_token = state.get("presence_token")

        if presence_token is None:
            # No presence token — deny (Law #3: fail closed for RED tier)
            logger.warning(
                "Presence token MISSING for RED-tier action: correlation=%s, suite=%s",
                correlation_id[:8], suite_id[:8],
            )
            receipt = _make_receipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                actor_type="system",
                actor_id="orchestrator",
                action_type="presence.check",
                risk_tier="red",
                outcome=Outcome.DENIED.value,
                reason_code=AspireErrorCode.PRESENCE_REQUIRED.value,
                receipt_type=ReceiptType.PRESENCE_MISSING.value,
            )
            existing_receipts = list(state.get("pipeline_receipts", []))
            existing_receipts.append(receipt)

            return {
                "approval_status": "rejected",
                "approval_payload_hash": payload_hash,
                "presence_required": True,
                "error_code": AspireErrorCode.PRESENCE_REQUIRED.value,
                "error_message": "Red-tier action requires presence verification",
                "outcome": Outcome.DENIED,
                "pipeline_receipts": existing_receipts,
            }

        # Verify presence token (6-check)
        presence_result = verify_presence_token(
            presence_token,
            expected_suite_id=suite_id,
            expected_office_id=office_id,
            expected_payload_hash=payload_hash,
        )

        if not presence_result.valid:
            error_code = _map_presence_error_to_error_code(presence_result.error)
            logger.warning(
                "Presence token REJECTED: error=%s, msg=%s, correlation=%s",
                presence_result.error.value if presence_result.error else "unknown",
                presence_result.error_message,
                correlation_id[:8],
            )

            receipt = _make_receipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                actor_type="system",
                actor_id="orchestrator",
                action_type="presence.check",
                risk_tier="red",
                outcome=Outcome.DENIED.value,
                reason_code=error_code.value,
                receipt_type=ReceiptType.PRESENCE_MISSING.value,
                details={
                    "presence_error": presence_result.error.value if presence_result.error else "unknown",
                    "presence_message": presence_result.error_message or "",
                },
            )
            existing_receipts = list(state.get("pipeline_receipts", []))
            existing_receipts.append(receipt)

            return {
                "approval_status": "rejected",
                "approval_payload_hash": payload_hash,
                "error_code": error_code.value,
                "error_message": presence_result.error_message or "Presence verification failed",
                "outcome": Outcome.DENIED,
                "pipeline_receipts": existing_receipts,
            }

        # Presence verified
        logger.info(
            "Presence token VERIFIED for RED-tier action: correlation=%s, suite=%s",
            correlation_id[:8], suite_id[:8],
        )

    # --- Approval GRANTED ---
    logger.info(
        "Approval GRANTED: tier=%s, correlation=%s, suite=%s, approver=%s",
        risk_tier_value, correlation_id[:8], suite_id[:8], approver_id[:8],
    )

    receipt = _make_receipt(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        actor_type="user",
        actor_id=approver_id,
        action_type="approval.grant",
        risk_tier=risk_tier_value,
        outcome=Outcome.SUCCESS.value,
        reason_code="APPROVED",
        receipt_type=ReceiptType.APPROVAL_GRANTED.value,
        details={
            "payload_hash": payload_hash,
            "policy_version": binding.policy_version,
        },
    )
    existing_receipts = list(state.get("pipeline_receipts", []))
    existing_receipts.append(receipt)

    return {
        "approval_status": "approved",
        "approval_payload_hash": payload_hash,
        "approval_evidence": approval_evidence,
        "pipeline_receipts": existing_receipts,
    }
