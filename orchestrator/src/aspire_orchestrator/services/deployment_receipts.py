"""Deployment Receipt Emission Service — deploy.* receipt types (Law #2).

Emits receipts for deployment lifecycle events:
  - deploy.started
  - deploy.canary.deployed
  - deploy.promoted
  - deploy.rolled_back
  - deploy.failed

All deployment receipts are GREEN risk tier (ops events, no user-facing actions).
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
    actor: str = "system",
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
        "actor_id": actor,
        "risk_tier": "green",
        "outcome": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "action_type": receipt_type,
        "tool_used": "deployment_system",
        "receipt_hash": "",
        "reason_code": "EXECUTED",
        "capability_token_id": None,
        "redacted_inputs": {},
    }


def emit_deploy_started(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    release_id: str,
    environment: str,
    actor: str = "system",
    change_proposal_id: str | None = None,
    canary_percent: float | None = None,
) -> dict[str, Any]:
    """Emit a deploy.started receipt."""
    receipt = _base_receipt("deploy.started", suite_id, office_id, trace_id, actor)
    receipt["release_id"] = release_id
    receipt["environment"] = environment
    receipt["action_type"] = "deploy.started"
    if change_proposal_id:
        receipt["change_proposal_id"] = change_proposal_id
    if canary_percent is not None:
        receipt["canary_percent"] = canary_percent

    receipt_store.store_receipts([receipt])
    logger.info("Emitted deploy.started receipt: %s", receipt["id"])
    return receipt


def emit_deploy_canary_deployed(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    release_id: str,
    environment: str,
    canary_percent: float,
    metrics_snapshot_ref: str | None = None,
) -> dict[str, Any]:
    """Emit a deploy.canary.deployed receipt."""
    receipt = _base_receipt("deploy.canary.deployed", suite_id, office_id, trace_id)
    receipt["release_id"] = release_id
    receipt["environment"] = environment
    receipt["canary_percent"] = canary_percent
    receipt["action_type"] = "deploy.canary.deployed"
    if metrics_snapshot_ref:
        receipt["metrics_snapshot_ref"] = metrics_snapshot_ref

    receipt_store.store_receipts([receipt])
    logger.info("Emitted deploy.canary.deployed receipt: %s", receipt["id"])
    return receipt


def emit_deploy_promoted(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    release_id: str,
    environment: str,
    promotion_time: str | None = None,
    metrics_snapshot_ref: str | None = None,
) -> dict[str, Any]:
    """Emit a deploy.promoted receipt."""
    receipt = _base_receipt("deploy.promoted", suite_id, office_id, trace_id)
    receipt["release_id"] = release_id
    receipt["environment"] = environment
    receipt["action_type"] = "deploy.promoted"
    if promotion_time:
        receipt["promotion_time"] = promotion_time
    if metrics_snapshot_ref:
        receipt["metrics_snapshot_ref"] = metrics_snapshot_ref

    receipt_store.store_receipts([receipt])
    logger.info("Emitted deploy.promoted receipt: %s", receipt["id"])
    return receipt


def emit_deploy_rolled_back(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    release_id: str,
    environment: str,
    reason: str,
    rollback_release_id: str | None = None,
    metrics_snapshot_ref: str | None = None,
) -> dict[str, Any]:
    """Emit a deploy.rolled_back receipt."""
    receipt = _base_receipt("deploy.rolled_back", suite_id, office_id, trace_id)
    receipt["release_id"] = release_id
    receipt["environment"] = environment
    receipt["reason"] = reason
    receipt["action_type"] = "deploy.rolled_back"
    receipt["outcome"] = "failed"
    if rollback_release_id:
        receipt["rollback_release_id"] = rollback_release_id
    if metrics_snapshot_ref:
        receipt["metrics_snapshot_ref"] = metrics_snapshot_ref

    receipt_store.store_receipts([receipt])
    logger.info("Emitted deploy.rolled_back receipt: %s", receipt["id"])
    return receipt


def emit_deploy_failed(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    release_id: str,
    environment: str,
    error: str,
    artifacts: list[str] | None = None,
) -> dict[str, Any]:
    """Emit a deploy.failed receipt."""
    receipt = _base_receipt("deploy.failed", suite_id, office_id, trace_id)
    receipt["release_id"] = release_id
    receipt["environment"] = environment
    receipt["error"] = error
    receipt["action_type"] = "deploy.failed"
    receipt["outcome"] = "failed"
    if artifacts:
        receipt["artifacts"] = artifacts

    receipt_store.store_receipts([receipt])
    logger.info("Emitted deploy.failed receipt: %s", receipt["id"])
    return receipt
