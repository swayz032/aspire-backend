"""Safety Gate Node.

This node delegates request safety evaluation to the Safety Gateway service.
The gateway can be backed by local deterministic rules or an external NeMo
Guardrails-compatible sidecar. The orchestrator remains the caller and records
the safety decision as part of the immutable receipt chain.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import AspireErrorCode, Outcome, ReceiptType
from aspire_orchestrator.services.safety_gateway import SafetyGatewayError, evaluate_safety
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)


def _base_receipt(*, correlation_id: str, suite_id: str, office_id: str) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "safety_gate",
        "action_type": "safety.check",
        "risk_tier": "green",
        "tool_used": "orchestrator.safety_gate",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "receipt_type": ReceiptType.POLICY_DECISION.value,
        "receipt_hash": "",
    }


def safety_gate_node(state: OrchestratorState) -> dict[str, Any]:
    """Run request safety checks and emit a receipt for the decision."""
    if state.get("error_code"):
        return {"safety_passed": False}

    correlation_id = state.get("correlation_id", str(uuid.uuid4()))
    suite_id = state.get("suite_id", "unknown")
    office_id = state.get("office_id", "unknown")
    task_type = state.get("task_type", "unknown")

    request = state.get("request")
    payload = request.payload if hasattr(request, "payload") else (request.get("payload") if isinstance(request, dict) else {})

    try:
        decision = evaluate_safety(payload, task_type=task_type, suite_id=suite_id, office_id=office_id)
    except SafetyGatewayError as exc:
        logger.error(
            "Safety gateway ERROR: suite=%s task=%s correlation=%s error=%s",
            suite_id[:8] if len(suite_id) > 8 else suite_id,
            task_type,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            exc,
        )
        receipt = {
            **_base_receipt(correlation_id=correlation_id, suite_id=suite_id, office_id=office_id),
            "outcome": Outcome.DENIED.value,
            "reason_code": AspireErrorCode.SAFETY_BLOCKED.value,
            "result": {"blocked": True, "reason": str(exc), "source": "remote_error"},
        }
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(receipt)
        return {
            "safety_passed": False,
            "safety_block_reason": str(exc),
            "error_code": AspireErrorCode.SAFETY_BLOCKED.value,
            "error_message": str(exc),
            "outcome": Outcome.DENIED,
            "pipeline_receipts": existing_receipts,
        }

    existing_receipts = list(state.get("pipeline_receipts", []))

    if not decision.allowed:
        logger.warning(
            "Safety gate BLOCKED: suite=%s task=%s correlation=%s reason=%s source=%s",
            suite_id[:8] if len(suite_id) > 8 else suite_id,
            task_type,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            decision.reason,
            decision.source,
        )
        existing_receipts.append({
            **_base_receipt(correlation_id=correlation_id, suite_id=suite_id, office_id=office_id),
            "outcome": Outcome.DENIED.value,
            "reason_code": AspireErrorCode.SAFETY_BLOCKED.value,
            "result": {
                "blocked": True,
                "reason": decision.reason,
                "source": decision.source,
                "matched_rule": decision.matched_rule,
                "metadata": decision.metadata or {},
            },
        })
        return {
            "safety_passed": False,
            "safety_block_reason": decision.reason,
            "error_code": AspireErrorCode.SAFETY_BLOCKED.value,
            "error_message": decision.reason,
            "outcome": Outcome.DENIED,
            "pipeline_receipts": existing_receipts,
        }

    logger.info(
        "Safety gate PASSED: suite=%s task=%s correlation=%s source=%s",
        suite_id[:8] if len(suite_id) > 8 else suite_id,
        task_type,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        decision.source,
    )
    existing_receipts.append({
        **_base_receipt(correlation_id=correlation_id, suite_id=suite_id, office_id=office_id),
        "outcome": Outcome.SUCCESS.value,
        "reason_code": None,
        "result": {
            "passed": True,
            "source": decision.source,
            "matched_rule": decision.matched_rule,
            "metadata": decision.metadata or {},
        },
    })
    return {
        "safety_passed": True,
        "safety_block_reason": None,
        "pipeline_receipts": existing_receipts,
    }
