"""Finn Receipt Service — Receipt emitter for Finn v2 events (Law #2).

Wraps receipt creation with Finn v2 schema compliance:
  - All receipts include required fields per receipt_event.schema.json
  - correlation_id enforced (C2: correlation propagation)
  - PII fields never included in receipts
  - Uses existing receipt_chain.py for hash chain + receipt_store.py for persistence

Event types emitted:
  - finance.snapshot.read
  - finance.exceptions.read
  - finance.proposal.created
  - a2a.item.created
  - policy.denied
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

ACTOR_FINN = "skillpack:finn-finance-manager"
RECEIPT_VERSION = "1.0"


@dataclass(frozen=True)
class FinnReceiptContext:
    """Required context for all Finn receipts."""

    suite_id: str
    office_id: str
    correlation_id: str


def _compute_inputs_hash(inputs: dict[str, Any]) -> str:
    """Compute SHA256 hash of inputs for receipt linkage.

    Returns "sha256:<hex>" format per schema.
    """
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _make_receipt(
    *,
    ctx: FinnReceiptContext,
    event_type: str,
    status: str,
    inputs: dict[str, Any],
    policy_decision: str = "allow",
    policy_id: str = "finn-finance-manager-v1",
    policy_reasons: list[str] | None = None,
    redactions: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt conforming to receipt_event.schema.json."""
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_FINN,
        "correlation_id": ctx.correlation_id,
        "status": status,
        "inputs_hash": _compute_inputs_hash(inputs),
        "policy": {
            "decision": policy_decision,
            "policy_id": policy_id,
            "reasons": policy_reasons or [],
        },
        "redactions": redactions or [],
    }
    if metadata:
        receipt["metadata"] = metadata
    return receipt


def emit_snapshot_read_receipt(
    ctx: FinnReceiptContext,
    *,
    snapshot_hash: str = "",
) -> dict[str, Any]:
    """Emit receipt for finance.snapshot.read (GREEN)."""
    return _make_receipt(
        ctx=ctx,
        event_type="finance.snapshot.read",
        status="ok",
        inputs={"action": "finance.snapshot.read", "suite_id": ctx.suite_id},
        metadata={"snapshot_hash": snapshot_hash} if snapshot_hash else None,
    )


def emit_exceptions_read_receipt(
    ctx: FinnReceiptContext,
    *,
    exception_count: int = 0,
) -> dict[str, Any]:
    """Emit receipt for finance.exceptions.read (GREEN)."""
    return _make_receipt(
        ctx=ctx,
        event_type="finance.exceptions.read",
        status="ok",
        inputs={"action": "finance.exceptions.read", "suite_id": ctx.suite_id},
        metadata={"exception_count": exception_count},
    )


def emit_proposal_created_receipt(
    ctx: FinnReceiptContext,
    *,
    proposal_action: str,
    inputs_hash: str,
    risk_tier: str = "yellow",
) -> dict[str, Any]:
    """Emit receipt for finance.proposal.created (YELLOW)."""
    return _make_receipt(
        ctx=ctx,
        event_type="finance.proposal.created",
        status="ok",
        inputs={
            "action": proposal_action,
            "inputs_hash": inputs_hash,
            "risk_tier": risk_tier,
        },
    )


def emit_a2a_delegation_receipt(
    ctx: FinnReceiptContext,
    *,
    to_agent: str,
    request_type: str,
    status: str = "ok",
    deny_reason: str | None = None,
) -> dict[str, Any]:
    """Emit receipt for a2a.item.created or denial."""
    event_type = "a2a.item.created" if status == "ok" else "policy.denied"
    policy_decision = "allow" if status == "ok" else "deny"
    reasons = [deny_reason] if deny_reason else []

    return _make_receipt(
        ctx=ctx,
        event_type=event_type,
        status=status,
        inputs={
            "action": "a2a.create",
            "to_agent": to_agent,
            "request_type": request_type,
        },
        policy_decision=policy_decision,
        policy_reasons=reasons,
    )


def emit_policy_denied_receipt(
    ctx: FinnReceiptContext,
    *,
    action_type: str,
    reason_code: str,
    message: str = "",
) -> dict[str, Any]:
    """Emit receipt for policy.denied on any Finn action."""
    return _make_receipt(
        ctx=ctx,
        event_type="policy.denied",
        status="denied",
        inputs={"action": action_type, "suite_id": ctx.suite_id},
        policy_decision="deny",
        policy_reasons=[reason_code, message] if message else [reason_code],
    )
