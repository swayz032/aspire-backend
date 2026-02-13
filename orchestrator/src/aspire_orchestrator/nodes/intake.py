"""Intake Node — First node in the orchestrator pipeline.

Responsibilities:
1. Validate AvaOrchestratorRequest against schema
2. Derive suite_id from auth context (NOT from client payload)
3. Generate correlation_id if not provided
4. Emit decision_intake receipt
5. On validation failure: set error_code = SCHEMA_VALIDATION_FAILED

Per architecture.md ingress rules:
  "Derive suite_id / office_id from auth context (do not trust client provided ids)"
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from aspire_orchestrator.models import (
    ActorType,
    AspireErrorCode,
    AvaOrchestratorRequest,
    Outcome,
    ReceiptType,
    RiskTier,
)
from aspire_orchestrator.state import OrchestratorState


def _make_receipt(
    *,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    actor_type: str,
    actor_id: str,
    action_type: str,
    outcome: str,
    reason_code: str | None = None,
    redacted_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a receipt dict for the pipeline receipts list."""
    return {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action_type": action_type,
        "risk_tier": "green",
        "tool_used": "orchestrator.intake",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "reason_code": reason_code,
        "receipt_type": ReceiptType.DECISION_INTAKE.value,
        "redacted_inputs": redacted_inputs,
        "receipt_hash": "",  # Computed by receipt_write node
    }


def intake_node(state: OrchestratorState) -> dict[str, Any]:
    """Validate incoming request and set up pipeline state.

    Returns partial state update to be merged into OrchestratorState.
    """
    raw_request = state.get("request")
    if raw_request is None:
        correlation_id = str(uuid.uuid4())
        receipt = _make_receipt(
            correlation_id=correlation_id,
            suite_id="unknown",
            office_id="unknown",
            actor_type=ActorType.SYSTEM.value,
            actor_id="orchestrator",
            action_type="intake.validate",
            outcome=Outcome.DENIED.value,
            reason_code=AspireErrorCode.SCHEMA_VALIDATION_FAILED.value,
        )
        return {
            "correlation_id": correlation_id,
            "error_code": AspireErrorCode.SCHEMA_VALIDATION_FAILED.value,
            "error_message": "No request provided",
            "outcome": Outcome.DENIED,
            "safety_passed": False,
            "pipeline_receipts": [receipt],
            "receipt_ids": [],
        }

    # Validate against Pydantic model
    try:
        if isinstance(raw_request, dict):
            request = AvaOrchestratorRequest(**raw_request)
        elif isinstance(raw_request, AvaOrchestratorRequest):
            request = raw_request
        else:
            raise ValidationError.from_exception_data(
                title="AvaOrchestratorRequest",
                line_errors=[],
            )
    except (ValidationError, Exception) as e:
        correlation_id = str(uuid.uuid4())
        receipt = _make_receipt(
            correlation_id=correlation_id,
            suite_id="unknown",
            office_id="unknown",
            actor_type=ActorType.SYSTEM.value,
            actor_id="orchestrator",
            action_type="intake.validate",
            outcome=Outcome.DENIED.value,
            reason_code=AspireErrorCode.SCHEMA_VALIDATION_FAILED.value,
            redacted_inputs={"error": str(e)[:200]},
        )
        return {
            "correlation_id": correlation_id,
            "error_code": AspireErrorCode.SCHEMA_VALIDATION_FAILED.value,
            "error_message": f"Schema validation failed: {e}",
            "outcome": Outcome.DENIED,
            "safety_passed": False,
            "pipeline_receipts": [receipt],
            "receipt_ids": [],
        }

    # CRITICAL: suite_id/office_id come from JWT auth context (gateway-provided),
    # NOT from client payload. Auth context overrides client values. (Law #6)
    # In dev mode (no auth context), fall back to request values with warning.
    auth_suite_id = state.get("auth_suite_id")
    auth_office_id = state.get("auth_office_id")

    if auth_suite_id:
        suite_id = auth_suite_id
        office_id = auth_office_id or request.office_id
    else:
        # Dev mode fallback — no JWT auth context provided
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "No auth context provided — using client-supplied suite_id. "
            "This is only acceptable in dev/test mode."
        )
        suite_id = request.suite_id
        office_id = request.office_id

    correlation_id = request.correlation_id or str(uuid.uuid4())

    # Derive actor_id from auth context (preferred) or state fallback
    actor_id = state.get("auth_actor_id") or state.get("actor_id", "unknown")

    # Emit decision_intake receipt
    receipt = _make_receipt(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        actor_type=ActorType.USER.value,
        actor_id=actor_id,
        action_type="intake.validate",
        outcome=Outcome.SUCCESS.value,
        redacted_inputs={"task_type": request.task_type},
    )

    return {
        "request": request,
        "correlation_id": correlation_id,
        "request_id": request.request_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": ActorType.USER,
        "actor_id": actor_id,
        "task_type": request.task_type,
        "timestamp": request.timestamp.isoformat() if hasattr(request.timestamp, "isoformat") else str(request.timestamp),
        "pipeline_receipts": [receipt],
        "receipt_ids": [],
    }
