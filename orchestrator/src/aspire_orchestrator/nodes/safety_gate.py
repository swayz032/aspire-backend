"""Safety Gate Node — NeMo Guardrails jailbreak/topic detection.

Responsibilities:
1. Run NeMo Guardrails check on the request (jailbreak detection)
2. Enforce topic steering (business operations only)
3. If blocked: set safety_passed=False, emit safety denial receipt
4. If passed: set safety_passed=True, continue to policy_eval

Uses local Ollama llama3:8b for NeMo classification to avoid external API costs.

NOTE: NeMo Guardrails integration will be fully wired in Wave 2 W2-03.
For now, this is a pass-through that sets safety_passed=True.
The structure is in place for NeMo integration.
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
)
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)


def safety_gate_node(state: OrchestratorState) -> dict[str, Any]:
    """Run safety checks on the incoming request.

    Returns partial state update with safety_passed flag.
    """
    # If previous node already failed, skip safety check
    if state.get("error_code"):
        return {"safety_passed": False}

    correlation_id = state.get("correlation_id", str(uuid.uuid4()))
    suite_id = state.get("suite_id", "unknown")
    office_id = state.get("office_id", "unknown")
    task_type = state.get("task_type", "unknown")

    # TODO (W2-03): Integrate NeMo Guardrails
    # For now, implement basic pattern matching for obvious jailbreak attempts
    blocked = False
    block_reason = None

    # Basic jailbreak detection patterns (NeMo will replace this)
    request = state.get("request")
    if request is not None:
        payload = request.payload if hasattr(request, "payload") else (request.get("payload") if isinstance(request, dict) else {})
        payload_str = str(payload).lower() if payload else ""

        jailbreak_patterns = [
            "ignore previous instructions",
            "ignore all instructions",
            "you are now",
            "pretend you are",
            "act as if",
            "disregard your rules",
            "bypass safety",
            "ignore your guidelines",
            "forget your instructions",
            "override your programming",
            "new system prompt",
            "system: you are",
            "ignore safety",
            "jailbreak",
            "dan mode",
            "developer mode",
            "do anything now",
            "sudo mode",
            "ignore all previous",
            "disregard all previous",
            "forget all previous",
            "you must obey",
            "roleplay as",
            "simulate being",
        ]

        for pattern in jailbreak_patterns:
            if pattern in payload_str:
                blocked = True
                block_reason = f"Safety gate blocked: jailbreak pattern detected ({pattern})"
                break

    if blocked:
        logger.warning(
            "Safety gate BLOCKED: reason='%s', suite=%s, task=%s, correlation=%s",
            block_reason, suite_id[:8] if len(suite_id) > 8 else suite_id,
            task_type, correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )
        receipt = {
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
            "outcome": Outcome.DENIED.value,
            "reason_code": AspireErrorCode.SAFETY_BLOCKED.value,
            "receipt_type": ReceiptType.POLICY_DECISION.value,
            "receipt_hash": "",
        }
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(receipt)

        return {
            "safety_passed": False,
            "safety_block_reason": block_reason,
            "error_code": AspireErrorCode.SAFETY_BLOCKED.value,
            "error_message": block_reason,
            "outcome": Outcome.DENIED,
            "pipeline_receipts": existing_receipts,
        }

    # Safety passed — emit pass receipt (Law #2: receipt for ALL actions) and continue
    logger.info(
        "Safety gate PASSED: suite=%s, task=%s, correlation=%s",
        suite_id[:8] if len(suite_id) > 8 else suite_id,
        task_type, correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )
    pass_receipt = {
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
        "outcome": Outcome.SUCCESS.value,
        "reason_code": None,
        "receipt_type": ReceiptType.POLICY_DECISION.value,
        "receipt_hash": "",
    }
    existing_receipts = list(state.get("pipeline_receipts", []))
    existing_receipts.append(pass_receipt)

    return {
        "safety_passed": True,
        "safety_block_reason": None,
        "pipeline_receipts": existing_receipts,
    }
