"""Entitlement Receipt Emission Service — entitlement.* receipt types (Law #2).

Emits receipts for billing/entitlement lifecycle events:
  - entitlement.plan.changed
  - entitlement.seat.added
  - entitlement.seat.removed
  - entitlement.usage.capped
  - entitlement.grace.started
  - entitlement.grace.ended

All entitlement receipts are GREEN risk tier (ops events, no user-facing actions).
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
        "actor_id": "entitlement_system",
        "risk_tier": "green",
        "outcome": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def emit_plan_changed(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    from_plan: str,
    to_plan: str,
    effective_at: str,
    billing_provider_ref: str | None = None,
    requires_approval: bool = True,
) -> dict[str, Any]:
    """Emit an entitlement.plan.changed receipt."""
    receipt = _base_receipt("entitlement.plan.changed", suite_id, office_id, trace_id)
    receipt["from_plan"] = from_plan
    receipt["to_plan"] = to_plan
    receipt["effective_at"] = effective_at
    receipt["action_type"] = "entitlement.plan.changed"
    if billing_provider_ref:
        receipt["billing_provider_ref"] = billing_provider_ref
    receipt["requires_approval"] = requires_approval

    receipt_store.store_receipts([receipt])
    logger.info("Emitted entitlement.plan.changed receipt: %s (%s -> %s)", receipt["id"], from_plan, to_plan)
    return receipt


def emit_seat_added(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    office_added: str,
    new_seat_count: int,
    billing_provider_ref: str | None = None,
) -> dict[str, Any]:
    """Emit an entitlement.seat.added receipt."""
    receipt = _base_receipt("entitlement.seat.added", suite_id, office_id, trace_id)
    receipt["office_added"] = office_added
    receipt["new_seat_count"] = new_seat_count
    receipt["action_type"] = "entitlement.seat.added"
    if billing_provider_ref:
        receipt["billing_provider_ref"] = billing_provider_ref

    receipt_store.store_receipts([receipt])
    logger.info("Emitted entitlement.seat.added receipt: %s (seats=%d)", receipt["id"], new_seat_count)
    return receipt


def emit_seat_removed(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    office_removed: str,
    new_seat_count: int,
    billing_provider_ref: str | None = None,
) -> dict[str, Any]:
    """Emit an entitlement.seat.removed receipt."""
    receipt = _base_receipt("entitlement.seat.removed", suite_id, office_id, trace_id)
    receipt["office_removed"] = office_removed
    receipt["new_seat_count"] = new_seat_count
    receipt["action_type"] = "entitlement.seat.removed"
    if billing_provider_ref:
        receipt["billing_provider_ref"] = billing_provider_ref

    receipt_store.store_receipts([receipt])
    logger.info("Emitted entitlement.seat.removed receipt: %s (seats=%d)", receipt["id"], new_seat_count)
    return receipt


def emit_usage_capped(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    cap_name: str,
    cap_value: float,
    period: str | None = None,
) -> dict[str, Any]:
    """Emit an entitlement.usage.capped receipt."""
    receipt = _base_receipt("entitlement.usage.capped", suite_id, office_id, trace_id)
    receipt["cap_name"] = cap_name
    receipt["cap_value"] = cap_value
    receipt["action_type"] = "entitlement.usage.capped"
    if period:
        receipt["period"] = period

    receipt_store.store_receipts([receipt])
    logger.info("Emitted entitlement.usage.capped receipt: %s (cap=%s)", receipt["id"], cap_name)
    return receipt


def emit_grace_started(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    reason: str,
    ends_at: str,
) -> dict[str, Any]:
    """Emit an entitlement.grace.started receipt."""
    receipt = _base_receipt("entitlement.grace.started", suite_id, office_id, trace_id)
    receipt["reason"] = reason
    receipt["ends_at"] = ends_at
    receipt["action_type"] = "entitlement.grace.started"

    receipt_store.store_receipts([receipt])
    logger.info("Emitted entitlement.grace.started receipt: %s (reason=%s)", receipt["id"], reason)
    return receipt


def emit_grace_ended(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    ended_at: str,
) -> dict[str, Any]:
    """Emit an entitlement.grace.ended receipt."""
    receipt = _base_receipt("entitlement.grace.ended", suite_id, office_id, trace_id)
    receipt["ended_at"] = ended_at
    receipt["action_type"] = "entitlement.grace.ended"

    receipt_store.store_receipts([receipt])
    logger.info("Emitted entitlement.grace.ended receipt: %s", receipt["id"])
    return receipt
