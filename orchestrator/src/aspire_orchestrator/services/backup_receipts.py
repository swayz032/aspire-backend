"""Backup Receipt Emission Service — backup/restore/DR receipt types (Law #2).

Emits receipts for disaster recovery events:
  - backup.completed
  - restore.tested
  - dr.drill.completed

All backup receipts are GREEN risk tier (ops events, no user-facing actions).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services import receipt_store

logger = logging.getLogger(__name__)


def _base_receipt(
    receipt_type: str,
    suite_id: str,
    office_id: str,
    trace_id: str,
) -> dict[str, Any]:
    """Build base receipt dict with common fields."""
    return {
        "id": str(uuid.uuid4()),
        "receipt_type": receipt_type,
        "suite_id": suite_id,
        "office_id": office_id,
        "trace_id": trace_id,
        "correlation_id": str(uuid.uuid4()),
        "actor_type": "system",
        "actor_id": "backup_system",
        "risk_tier": "green",
        "outcome": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "action_type": receipt_type,
        "tool_used": "backup_system",
        "receipt_hash": "",
        "reason_code": "EXECUTED",
        "capability_token_id": None,
        "redacted_inputs": {},
    }


def emit_backup_completed(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    target: str,
    status: str,
    artifact_ref: str,
    error: str | None = None,
    rpo_minutes: int | None = None,
) -> dict[str, Any]:
    """Emit a backup.completed receipt."""
    receipt = _base_receipt("backup.completed", suite_id, office_id, trace_id)
    receipt["target"] = target
    receipt["status"] = status
    receipt["artifact_ref"] = artifact_ref
    receipt["action_type"] = "backup.completed"
    receipt["outcome"] = "success" if status == "success" else "failed"
    if error:
        receipt["error"] = error
    if rpo_minutes is not None:
        receipt["rpo_minutes"] = rpo_minutes

    receipt_store.store_receipts([receipt])
    logger.info("Emitted backup.completed receipt: %s (target=%s, status=%s)", receipt["id"], target, status)
    return receipt


def emit_restore_tested(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    target: str,
    status: str,
    artifact_ref: str,
    rto_minutes: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Emit a restore.tested receipt."""
    receipt = _base_receipt("restore.tested", suite_id, office_id, trace_id)
    receipt["target"] = target
    receipt["status"] = status
    receipt["artifact_ref"] = artifact_ref
    receipt["action_type"] = "restore.tested"
    receipt["outcome"] = "success" if status == "success" else "failed"
    if rto_minutes is not None:
        receipt["rto_minutes"] = rto_minutes
    if notes:
        receipt["notes"] = notes

    receipt_store.store_receipts([receipt])
    logger.info("Emitted restore.tested receipt: %s (target=%s, status=%s)", receipt["id"], target, status)
    return receipt


def emit_dr_drill_completed(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    scenario: str,
    status: str,
    rto_minutes: int,
    rpo_minutes: int,
    runbook_ref: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Emit a dr.drill.completed receipt."""
    receipt = _base_receipt("dr.drill.completed", suite_id, office_id, trace_id)
    receipt["scenario"] = scenario
    receipt["status"] = status
    receipt["rto_minutes"] = rto_minutes
    receipt["rpo_minutes"] = rpo_minutes
    receipt["action_type"] = "dr.drill.completed"
    receipt["outcome"] = "success" if status == "success" else "failed"
    if runbook_ref:
        receipt["runbook_ref"] = runbook_ref
    if notes:
        receipt["notes"] = notes

    receipt_store.store_receipts([receipt])
    logger.info("Emitted dr.drill.completed receipt: %s (scenario=%s, status=%s)", receipt["id"], scenario, status)
    return receipt
