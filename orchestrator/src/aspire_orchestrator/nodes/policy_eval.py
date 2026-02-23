"""Policy Evaluation Node — Deterministic 9-step policy engine (Law #4).

Responsibilities:
1. Resolve tenant + actor context
2. Compute candidate tool set
3. Apply allowlist intersection
4. Classify risk tier per action
5. Determine approval requirements (yellow/red)
6. Determine presence requirements (red only)
7. Determine capability token requirements (all execution)
8. Emit policy_decision receipt
9. On denial: set policy_allowed=False with reason

Evaluation order per policy_engine_spec.md (9 steps):
  1. Validate ingress schema (done by intake)
  2. Resolve tenant + actor
  3. Compute candidate tool set
  4. Apply allowlist intersection
  5. Classify risk tier per action
  6. If yellow/red: require approvals
  7. If red: require presence proof
  8. For any execution: require valid capability token
  9. Emit policy_decision receipt

Driven by config/policy_matrix.yaml — single source of truth.
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
from aspire_orchestrator.services.kill_switch import check_kill_switch
from aspire_orchestrator.services.policy_engine import get_policy_matrix
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)


def policy_eval_node(state: OrchestratorState) -> dict[str, Any]:
    """Evaluate policy for the incoming request.

    Returns partial state update with risk tier, allowed tools, and approval requirements.

    Kill switch check runs BEFORE policy evaluation (Law #3: fail-closed).
    """
    if state.get("error_code"):
        return {"policy_allowed": False}

    correlation_id = state.get("correlation_id", str(uuid.uuid4()))
    suite_id = state.get("suite_id", "unknown")
    office_id = state.get("office_id", "unknown")
    task_type = state.get("task_type", "unknown")

    # Load policy matrix (cached singleton)
    matrix = get_policy_matrix()

    # Kill switch check — BEFORE policy evaluation
    # We need the risk tier from the matrix to check against kill switch
    eval_result_preview = matrix.evaluate(task_type)
    ks_result = check_kill_switch(
        action_type=task_type,
        risk_tier=eval_result_preview.risk_tier.value,
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
    )
    if not ks_result.allowed:
        logger.warning(
            "Kill switch BLOCKED: task=%s, tier=%s, mode=%s",
            task_type, eval_result_preview.risk_tier.value, ks_result.mode.value,
        )
        existing_receipts = list(state.get("pipeline_receipts", []))
        if ks_result.receipt:
            existing_receipts.append(ks_result.receipt)
        return {
            "risk_tier": eval_result_preview.risk_tier,
            "policy_allowed": False,
            "policy_deny_reason": ks_result.reason,
            "allowed_tools": [],
            "required_approvals": [],
            "presence_required": False,
            "error_code": "KILL_SWITCH_BLOCKED",
            "error_message": ks_result.reason,
            "outcome": Outcome.DENIED,
            "pipeline_receipts": existing_receipts,
        }

    # Steps 3-8: Evaluate policy
    eval_result = matrix.evaluate(task_type)

    if not eval_result.allowed:
        # Unknown action type → DENY (fail-closed, per policy_engine_spec.md)
        logger.warning(
            "Policy DENIED: task=%s, suite=%s, reason=%s",
            task_type, suite_id[:8] if len(suite_id) > 8 else suite_id,
            eval_result.deny_reason,
        )

        receipt = _make_policy_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            outcome=Outcome.DENIED.value,
            reason_code=AspireErrorCode.POLICY_DENIED.value,
            risk_tier=eval_result.risk_tier.value,
            task_type=task_type,
        )
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(receipt)

        return {
            "risk_tier": eval_result.risk_tier,
            "policy_allowed": False,
            "policy_deny_reason": eval_result.deny_reason,
            "allowed_tools": [],
            "required_approvals": [],
            "presence_required": False,
            "error_code": AspireErrorCode.POLICY_DENIED.value,
            "error_message": f"Policy denied: {eval_result.deny_reason}",
            "outcome": Outcome.DENIED,
            "pipeline_receipts": existing_receipts,
        }

    logger.info(
        "Policy ALLOWED: task=%s, suite=%s, risk=%s, tools=%s",
        task_type,
        suite_id[:8] if len(suite_id) > 8 else suite_id,
        eval_result.risk_tier.value,
        eval_result.tools,
    )

    # Step 6: If yellow/red, require approvals
    required_approvals: list[str] = []
    if eval_result.approval_required:
        required_approvals = ["owner_approval"]

    # Step 9: Emit policy_decision receipt
    receipt = _make_policy_receipt(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        outcome=Outcome.SUCCESS.value,
        reason_code="POLICY_ALLOWED",
        risk_tier=eval_result.risk_tier.value,
        task_type=task_type,
    )
    existing_receipts = list(state.get("pipeline_receipts", []))
    existing_receipts.append(receipt)

    result = {
        "risk_tier": eval_result.risk_tier,
        "policy_allowed": True,
        "policy_deny_reason": None,
        "allowed_tools": eval_result.tools,
        "required_approvals": required_approvals,
        "presence_required": eval_result.presence_required,
        "pipeline_receipts": existing_receipts,
    }

    # Backwards-compat: when Brain Layer is skipped (no utterance),
    # derive tool_used and execution_params from policy matrix + request payload.
    if not state.get("tool_used") and eval_result.tools:
        result["tool_used"] = eval_result.tools[0]
        logger.info("Backwards-compat: set tool_used=%s from policy matrix", eval_result.tools[0])

    if not state.get("execution_params"):
        request = state.get("request")
        if isinstance(request, dict):
            payload = request.get("payload", {})
        elif hasattr(request, "payload"):
            payload = request.payload if isinstance(request.payload, dict) else {}
        else:
            payload = {}
        if payload:
            result["execution_params"] = payload
            logger.info("Backwards-compat: set execution_params from request payload (%d keys)", len(payload))

    return result


def _make_policy_receipt(
    *,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    outcome: str,
    reason_code: str,
    risk_tier: str,
    task_type: str,
) -> dict[str, Any]:
    """Create a policy_decision receipt."""
    return {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "policy_engine",
        "action_type": f"policy.evaluate.{task_type}",
        "risk_tier": risk_tier,
        "tool_used": "orchestrator.policy_eval",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "reason_code": reason_code,
        "receipt_type": ReceiptType.POLICY_DECISION.value,
        "receipt_hash": "",
    }
