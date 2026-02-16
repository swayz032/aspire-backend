"""SLO Receipt Emission Service — slo.*/alert.* receipt types (Law #2).

Emits receipts for observability events:
  - slo.metric.rollup
  - slo.breach.detected
  - alert.triggered

All SLO receipts are GREEN risk tier (ops events, no user-facing actions).
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
        "actor_id": "slo_monitor",
        "risk_tier": "green",
        "outcome": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def emit_slo_metric_rollup(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    service: str,
    window: str,
    metrics: dict[str, Any],
    suite_scope: str = "global",
) -> dict[str, Any]:
    """Emit a slo.metric.rollup receipt."""
    receipt = _base_receipt("slo.metric.rollup", suite_id, office_id, trace_id)
    receipt["service"] = service
    receipt["window"] = window
    receipt["metrics"] = metrics
    receipt["suite_scope"] = suite_scope
    receipt["action_type"] = "slo.metric.rollup"

    receipt_store.store_receipts([receipt])
    logger.info("Emitted slo.metric.rollup receipt: %s (service=%s, window=%s)", receipt["id"], service, window)
    return receipt


def emit_slo_breach_detected(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    service: str,
    slo_name: str,
    window: str,
    threshold: float,
    observed: float,
    error_budget_remaining: float | None = None,
    incident_receipt_id: str | None = None,
) -> dict[str, Any]:
    """Emit a slo.breach.detected receipt."""
    receipt = _base_receipt("slo.breach.detected", suite_id, office_id, trace_id)
    receipt["service"] = service
    receipt["slo_name"] = slo_name
    receipt["window"] = window
    receipt["threshold"] = threshold
    receipt["observed"] = observed
    receipt["action_type"] = "slo.breach.detected"
    if error_budget_remaining is not None:
        receipt["error_budget_remaining"] = error_budget_remaining
    if incident_receipt_id:
        receipt["incident_receipt_id"] = incident_receipt_id

    receipt_store.store_receipts([receipt])
    logger.info("Emitted slo.breach.detected receipt: %s (slo=%s)", receipt["id"], slo_name)
    return receipt


def emit_alert_triggered(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    alert_name: str,
    severity: str,
    service: str,
    signal_ref: str | None = None,
    incident_receipt_id: str | None = None,
) -> dict[str, Any]:
    """Emit an alert.triggered receipt."""
    receipt = _base_receipt("alert.triggered", suite_id, office_id, trace_id)
    receipt["alert_name"] = alert_name
    receipt["severity"] = severity
    receipt["service"] = service
    receipt["action_type"] = "alert.triggered"
    if signal_ref:
        receipt["signal_ref"] = signal_ref
    if incident_receipt_id:
        receipt["incident_receipt_id"] = incident_receipt_id

    receipt_store.store_receipts([receipt])
    logger.info("Emitted alert.triggered receipt: %s (alert=%s, severity=%s)", receipt["id"], alert_name, severity)
    return receipt
