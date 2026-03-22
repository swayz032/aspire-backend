"""RBAC Receipt Emission Service — rbac.* receipt types (Law #2).

Emits receipts for role-based access control events:
  - rbac.role.granted
  - rbac.role.revoked
  - rbac.permission.escalated

RBAC receipts are YELLOW risk tier (permission changes are state-changing).
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
        "actor_id": "rbac_system",
        "risk_tier": "yellow",
        "outcome": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "action_type": receipt_type,
        "tool_used": "rbac_system",
        "receipt_hash": "",
        "reason_code": "EXECUTED",
        "capability_token_id": None,
        "redacted_inputs": {},
    }


def emit_role_granted(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    target_office_id: str,
    role: str,
    granted_by: str,
    approval_receipt_id: str | None = None,
) -> dict[str, Any]:
    """Emit an rbac.role.granted receipt."""
    receipt = _base_receipt("rbac.role.granted", suite_id, office_id, trace_id)
    receipt["target_office_id"] = target_office_id
    receipt["role"] = role
    receipt["granted_by"] = granted_by
    receipt["action_type"] = "rbac.role.granted"
    if approval_receipt_id:
        receipt["approval_receipt_id"] = approval_receipt_id

    receipt_store.store_receipts([receipt])
    logger.info("Emitted rbac.role.granted receipt: %s (role=%s, target=%s)", receipt["id"], role, target_office_id)
    return receipt


def emit_role_revoked(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    target_office_id: str,
    role: str,
    revoked_by: str,
    approval_receipt_id: str | None = None,
) -> dict[str, Any]:
    """Emit an rbac.role.revoked receipt."""
    receipt = _base_receipt("rbac.role.revoked", suite_id, office_id, trace_id)
    receipt["target_office_id"] = target_office_id
    receipt["role"] = role
    receipt["revoked_by"] = revoked_by
    receipt["action_type"] = "rbac.role.revoked"
    if approval_receipt_id:
        receipt["approval_receipt_id"] = approval_receipt_id

    receipt_store.store_receipts([receipt])
    logger.info("Emitted rbac.role.revoked receipt: %s (role=%s, target=%s)", receipt["id"], role, target_office_id)
    return receipt


def emit_permission_escalated(
    *,
    suite_id: str,
    office_id: str,
    trace_id: str,
    target_office_id: str,
    from_role: str,
    to_role: str,
    reason: str,
    approval_receipt_id: str | None = None,
    requires_ava_video: bool = False,
) -> dict[str, Any]:
    """Emit an rbac.permission.escalated receipt."""
    receipt = _base_receipt("rbac.permission.escalated", suite_id, office_id, trace_id)
    receipt["target_office_id"] = target_office_id
    receipt["from_role"] = from_role
    receipt["to_role"] = to_role
    receipt["reason"] = reason
    receipt["action_type"] = "rbac.permission.escalated"
    if approval_receipt_id:
        receipt["approval_receipt_id"] = approval_receipt_id
    receipt["requires_ava_video"] = requires_ava_video

    receipt_store.store_receipts([receipt])
    logger.info(
        "Emitted rbac.permission.escalated receipt: %s (%s -> %s, target=%s)",
        receipt["id"], from_role, to_role, target_office_id,
    )
    return receipt
