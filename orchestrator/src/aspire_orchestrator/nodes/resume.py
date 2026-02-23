"""Resume Node — Execute previously drafted operations after user approval.

Mini-pipeline (5 steps):
1. Validate — Fetch approval_request from Supabase, verify 5 conditions
2. Retrieve — Get execution_payload, tool, operation, assigned_agent
3. Token Mint — Mint fresh capability token for the tool (Law #5)
4. Execute — Call execute_tool() with real params
5. Receipt + Narration — Generate receipt linked to original run_id
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome, ReceiptType

logger = logging.getLogger(__name__)


async def resume_after_approval(
    approval_id: str,
    suite_id: str,
    office_id: str,
    actor_id: str,
) -> dict[str, Any]:
    """Execute a previously drafted operation after user approval.

    Returns:
        {"success": True, "narration_text": str, "receipt_id": str, "execution_result": dict}
        OR {"success": False, "error_code": str, "error_message": str, "receipt_id": str}
    """
    from aspire_orchestrator.services.supabase_client import supabase_select, supabase_update
    from aspire_orchestrator.services.narration import compose_narration
    from aspire_orchestrator.services.receipt_store import store_receipts
    from aspire_orchestrator.services.receipt_chain import assign_chain_metadata

    # Pre-fetch correlation_id — will be replaced with original_run_id after we fetch the approval record
    temp_correlation_id = str(uuid.uuid4())
    receipts: list[dict[str, Any]] = []

    # Defense-in-depth: validate UUID format before filter interpolation (prevent injection)
    try:
        uuid.UUID(approval_id)
        uuid.UUID(suite_id)
    except (ValueError, AttributeError):
        return _error("INVALID_INPUT", "Invalid identifier format", temp_correlation_id, suite_id or "unknown", office_id, receipts)

    # --- Step 1: Validate ---
    try:
        rows = await supabase_select(
            "approval_requests",
            f"approval_id=eq.{approval_id}&select=*"
        )
    except Exception as e:
        logger.error("Resume fetch failed: %s", e)
        return _error("RESUME_FETCH_FAILED", "Unable to retrieve approval request", temp_correlation_id, suite_id, office_id, receipts)

    if not rows:
        return _error("RESUME_NOT_FOUND", "Approval request not found", temp_correlation_id, suite_id, office_id, receipts)

    approval = rows[0]

    # Bind trace to original run for receipt chain integrity (Law #2)
    raw_run_id = approval.get("run_id")
    if raw_run_id:
        try:
            uuid.UUID(raw_run_id)
            correlation_id = raw_run_id
        except (ValueError, AttributeError):
            logger.warning("Resume: invalid run_id format in approval record, using temp ID")
            correlation_id = temp_correlation_id
    else:
        correlation_id = temp_correlation_id

    # Check 1: status == 'approved'
    if approval.get("status") != "approved":
        return _error("RESUME_NOT_APPROVED", "Approval has not been granted", correlation_id, suite_id, office_id, receipts)

    # Check 2: tenant_id == suite_id (Law #6: tenant isolation)
    if approval.get("tenant_id") != suite_id:
        logger.warning("Resume DENIED: tenant mismatch. Expected %s, got %s", suite_id[:8], approval.get("tenant_id", "?")[:8])
        return _error("TENANT_ISOLATION_VIOLATION", "Access denied", correlation_id, suite_id, office_id, receipts)

    # Check 3: not expired
    expires_at = approval.get("expires_at")
    if expires_at:
        from datetime import datetime as dt
        try:
            exp = dt.fromisoformat(expires_at.replace("Z", "+00:00"))
            if exp < datetime.now(timezone.utc):
                return _error("RESUME_EXPIRED", "Approval has expired", correlation_id, suite_id, office_id, receipts)
        except (ValueError, TypeError) as e:
            # Fail closed: invalid expiry format = treat as expired (Law #3)
            logger.warning("Resume DENIED: invalid expiry format (%s), treating as expired", e)
            return _error("RESUME_EXPIRED", "Approval has expired or is invalid", correlation_id, suite_id, office_id, receipts)

    # Check 4: execution_params_hash matches execution_payload (approve-then-swap defense)
    execution_payload = approval.get("execution_payload")
    stored_hash = approval.get("execution_params_hash")
    if execution_payload and stored_hash:
        computed_hash = hashlib.sha256(
            json.dumps(execution_payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        # Timing-safe comparison to prevent side-channel brute-force
        if not secrets.compare_digest(computed_hash, stored_hash):
            logger.warning("Resume DENIED: payload hash mismatch (approve-then-swap detected)")
            return _error("PAYLOAD_HASH_MISMATCH", "Payload integrity check failed", correlation_id, suite_id, office_id, receipts)

    # --- Step 2: Retrieve ---
    tool_used = approval.get("tool", "unknown")
    task_type = approval.get("operation", "unknown")
    assigned_agent = approval.get("assigned_agent", "ava")
    risk_tier = approval.get("risk_tier", "yellow")
    original_run_id = correlation_id  # Already bound above

    # Revalidate risk tier against current policy matrix (defend against DB downgrade attack)
    try:
        from aspire_orchestrator.services.policy_engine import get_policy_matrix
        matrix = get_policy_matrix()
        policy_result = matrix.evaluate(task_type)
        raw_tier = policy_result.risk_tier.value if hasattr(policy_result.risk_tier, "value") else str(policy_result.risk_tier)
        current_tier = raw_tier.lower()
        stored_tier = risk_tier.lower()
        if current_tier != stored_tier:
            logger.warning(
                "Resume DENIED: risk_tier mismatch (DB=%s, policy=%s), possible downgrade attack",
                risk_tier, current_tier,
            )
            return _error("RISK_TIER_MISMATCH", "Risk classification has changed — re-evaluation required", correlation_id, suite_id, office_id, receipts)
    except Exception as e:
        # Fail closed: if we can't verify, deny (Law #3)
        logger.warning("Resume risk tier revalidation failed: %s — denying", e)
        return _error("RISK_TIER_VALIDATION_FAILED", "Unable to verify risk classification", correlation_id, suite_id, office_id, receipts)

    if not execution_payload:
        return _error("RESUME_NO_PAYLOAD", "No execution payload found in approval request", correlation_id, suite_id, office_id, receipts)

    # --- Step 2b: Dual Approval Verification (defense-in-depth) ---
    # If the policy requires dual approval (e.g. contract.sign), verify via DualApprovalService
    # that BOTH approvers have signed off — even if Supabase status says 'approved'.
    try:
        if policy_result and policy_result.dual_approval:
            from aspire_orchestrator.services.dual_approval_service import get_dual_approval_service
            dual_svc = get_dual_approval_service()
            dual_status = dual_svc.check_status(
                request_id=approval_id, suite_id=suite_id,
            )
            if not dual_status.fully_approved:
                logger.warning(
                    "Resume DENIED: dual approval required but not FULLY_APPROVED (status=%s, remaining=%s)",
                    dual_status.status.value if hasattr(dual_status.status, "value") else dual_status.status,
                    dual_status.remaining_roles,
                )
                return _error(
                    "DUAL_APPROVAL_INCOMPLETE",
                    "This action requires dual approval (legal + business_owner) — not all approvers have signed off",
                    correlation_id, suite_id, office_id, receipts,
                )
    except Exception as e:
        # Fail closed: if we can't verify dual approval, deny (Law #3)
        logger.warning("Resume dual approval verification failed: %s — denying", e)
        return _error("DUAL_APPROVAL_CHECK_FAILED", "Unable to verify dual approval status", correlation_id, suite_id, office_id, receipts)

    # --- Step 3: Token Mint ---
    try:
        from aspire_orchestrator.services.token_service import mint_token
        token = mint_token(
            suite_id=suite_id,
            office_id=office_id,
            tool=tool_used,
            scopes=[f"{task_type.split('.')[0]}.write"] if "." in task_type else ["execute"],
            correlation_id=original_run_id,
        )
        capability_token_id = token.get("token_id")
    except Exception as e:
        logger.warning("Token mint failed for resume: %s", e)
        capability_token_id = None

    # --- Step 4: Execute ---
    try:
        from aspire_orchestrator.services.tool_executor import execute_tool
        tool_result = await execute_tool(
            tool_id=tool_used,
            payload=execution_payload,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            risk_tier=risk_tier,
            capability_token_id=capability_token_id,
        )
        # ToolExecutionResult uses .outcome (Outcome enum), not .success
        execution_success = (
            tool_result.outcome == Outcome.SUCCESS
            if hasattr(tool_result, "outcome")
            else bool(tool_result)
        )
        execution_data = tool_result.data if hasattr(tool_result, "data") else {}
        execution_error = tool_result.error if hasattr(tool_result, "error") else None
        if execution_error:
            logger.warning("Resume execution error for %s: %s", tool_used, execution_error)
    except Exception as e:
        logger.error("Resume execution failed for approval=%s: %s", approval_id[:8], e)
        execution_success = False
        # Never expose raw exception details to client (enterprise security)
        execution_data = {"error": "Tool execution failed — please retry or contact support"}

    # --- Step 5: Receipt + Narration ---
    receipt_id = str(uuid.uuid4())
    receipt = {
        "id": receipt_id,
        "correlation_id": original_run_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "agent",
        "actor_id": assigned_agent,
        "action_type": f"resume.{task_type}",
        "risk_tier": risk_tier,
        "tool_used": tool_used,
        "capability_token_id": capability_token_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "outcome": Outcome.SUCCESS.value if execution_success else Outcome.FAILED.value,
        "reason_code": "RESUME_EXECUTED" if execution_success else "RESUME_EXECUTION_FAILED",
        "receipt_type": ReceiptType.TOOL_EXECUTION.value,
        "receipt_hash": "",
        "approval_id": approval_id,
    }
    receipts.append(receipt)

    # Persist receipts
    try:
        assign_chain_metadata(receipts, chain_id=suite_id)
        store_receipts(receipts)
    except Exception as e:
        logger.error("Resume receipt persistence failed: %s", e)

    # Update approval status to 'executed'
    try:
        await supabase_update(
            "approval_requests",
            f"approval_id=eq.{approval_id}",
            {"status": "executed"},
        )
    except Exception as e:
        logger.warning("Failed to update approval status to executed: %s", e)

    # Narration
    narration = compose_narration(
        outcome="success" if execution_success else "failed",
        task_type=task_type,
        tool_used=tool_used,
        execution_params=execution_payload,
        execution_result=execution_data if isinstance(execution_data, dict) else {"data": execution_data},
        draft_id=approval_id,
        risk_tier=risk_tier,
    )

    if execution_success:
        return {
            "success": True,
            "narration_text": narration,
            "receipt_id": receipt_id,
            "execution_result": execution_data if isinstance(execution_data, dict) else {"data": execution_data},
        }
    else:
        return {
            "success": False,
            "error_code": "RESUME_EXECUTION_FAILED",
            "error_message": narration,
            "receipt_id": receipt_id,
        }


def _error(
    code: str,
    message: str,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build error response with receipt."""
    from aspire_orchestrator.services.receipt_store import store_receipts
    from aspire_orchestrator.services.receipt_chain import assign_chain_metadata

    receipt_id = str(uuid.uuid4())
    receipt = {
        "id": receipt_id,
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "orchestrator.resume",
        "action_type": "resume.validation",
        "risk_tier": "yellow",
        "tool_used": "unknown",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "outcome": Outcome.DENIED.value,
        "reason_code": code,
        "receipt_type": ReceiptType.TOOL_EXECUTION.value,
        "receipt_hash": "",
    }
    receipts.append(receipt)

    try:
        assign_chain_metadata(receipts, chain_id=suite_id)
        store_receipts(receipts)
    except Exception as e:
        logger.error("Resume error receipt persistence failed: %s", e)

    return {
        "success": False,
        "error_code": code,
        "error_message": message,
        "receipt_id": receipt_id,
    }
