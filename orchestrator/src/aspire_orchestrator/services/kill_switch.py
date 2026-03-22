"""Kill Switch Service — Emergency execution controls (Law #3: fail-closed).

Modes:
  - ENABLED: Normal operation, all actions processed normally
  - APPROVAL_ONLY: Only pre-approved actions execute; new YELLOW/RED actions blocked
  - DISABLED: ALL YELLOW and RED actions blocked; only GREEN actions pass

Reads mode from env var ASPIRE_KILL_SWITCH (default: ENABLED).
Wire into policy_eval node — check before action evaluation.

Law compliance:
  - Law #2: Mode changes and blocks produce receipts
  - Law #3: DISABLED mode = fail-closed for YELLOW/RED
  - Law #4: Respects risk tier classification
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from aspire_orchestrator.services.receipt_store import store_receipts

logger = logging.getLogger(__name__)


class KillSwitchMode(str, Enum):
    """Kill switch operational modes."""

    ENABLED = "ENABLED"
    APPROVAL_ONLY = "APPROVAL_ONLY"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class KillSwitchResult:
    """Result of kill switch check."""

    allowed: bool
    mode: KillSwitchMode
    reason: str | None = None
    receipt: dict[str, Any] | None = None


# In-memory mode override (can be set at runtime for emergency response)
_mode_override: KillSwitchMode | None = None


def get_kill_switch_mode() -> KillSwitchMode:
    """Get current kill switch mode.

    Priority: runtime override > env var > default (ENABLED).
    """
    if _mode_override is not None:
        return _mode_override

    env_mode = os.environ.get("ASPIRE_KILL_SWITCH", "ENABLED").upper()
    try:
        return KillSwitchMode(env_mode)
    except ValueError:
        logger.warning(
            "Invalid ASPIRE_KILL_SWITCH value: %s, defaulting to ENABLED",
            env_mode,
        )
        return KillSwitchMode.ENABLED


def set_kill_switch_mode(mode: KillSwitchMode) -> dict[str, Any]:
    """Set kill switch mode at runtime (emergency response).

    Returns a receipt for the mode change (Law #2).
    """
    global _mode_override
    old_mode = get_kill_switch_mode()
    _mode_override = mode

    logger.warning(
        "Kill switch mode changed: %s -> %s",
        old_mode.value, mode.value,
    )

    receipt = _build_mode_change_receipt(old_mode, mode)
    store_receipts([receipt])
    return receipt


def check_kill_switch(
    *,
    action_type: str,
    risk_tier: str,
    suite_id: str = "unknown",
    office_id: str = "unknown",
    correlation_id: str = "",
) -> KillSwitchResult:
    """Check if the kill switch allows an action to proceed.

    Args:
        action_type: The action being evaluated
        risk_tier: green/yellow/red
        suite_id: For receipt scoping
        office_id: For receipt scoping
        correlation_id: For traceability

    Returns:
        KillSwitchResult with allowed flag and optional receipt
    """
    mode = get_kill_switch_mode()
    tier = risk_tier.lower()

    # ENABLED: everything passes
    if mode == KillSwitchMode.ENABLED:
        return KillSwitchResult(allowed=True, mode=mode)

    # GREEN always passes regardless of mode
    if tier == "green":
        return KillSwitchResult(allowed=True, mode=mode)

    # DISABLED: block ALL yellow/red
    if mode == KillSwitchMode.DISABLED:
        receipt = _build_blocked_receipt(
            action_type=action_type,
            risk_tier=risk_tier,
            mode=mode,
            reason="kill_switch_disabled",
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
        )
        logger.warning(
            "Kill switch BLOCKED: action=%s, tier=%s, mode=%s",
            action_type, risk_tier, mode.value,
        )
        store_receipts([receipt])
        return KillSwitchResult(
            allowed=False,
            mode=mode,
            reason=f"Kill switch DISABLED — all {tier.upper()} actions blocked",
            receipt=receipt,
        )

    # APPROVAL_ONLY: block new yellow/red (only pre-approved pass)
    if mode == KillSwitchMode.APPROVAL_ONLY:
        receipt = _build_blocked_receipt(
            action_type=action_type,
            risk_tier=risk_tier,
            mode=mode,
            reason="kill_switch_approval_only",
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
        )
        logger.warning(
            "Kill switch APPROVAL_ONLY: action=%s, tier=%s — requires pre-approval",
            action_type, risk_tier,
        )
        store_receipts([receipt])
        return KillSwitchResult(
            allowed=False,
            mode=mode,
            reason=f"Kill switch APPROVAL_ONLY — new {tier.upper()} actions require pre-approval",
            receipt=receipt,
        )

    # Fallback: fail-closed (Law #3)
    return KillSwitchResult(
        allowed=False,
        mode=mode,
        reason="kill_switch_unknown_mode",
    )


def _build_blocked_receipt(
    *,
    action_type: str,
    risk_tier: str,
    mode: KillSwitchMode,
    reason: str,
    suite_id: str,
    office_id: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Build a receipt for a kill switch block (Law #2)."""
    return {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "kill_switch",
        "action_type": f"kill_switch.blocked.{action_type}",
        "risk_tier": risk_tier,
        "tool_used": "orchestrator.kill_switch",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "outcome": "denied",
        "reason_code": reason,
        "receipt_type": "kill_switch.activated",
        "receipt_hash": "",
        "details": {
            "kill_switch_mode": mode.value,
            "blocked_action": action_type,
            "blocked_risk_tier": risk_tier,
        },
    }


def _build_mode_change_receipt(
    old_mode: KillSwitchMode,
    new_mode: KillSwitchMode,
) -> dict[str, Any]:
    """Build a receipt for kill switch mode change (Law #2)."""
    return {
        "id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "suite_id": "00000000-0000-0000-0000-000000000000",
        "office_id": "00000000-0000-0000-0000-000000000000",
        "actor_type": "system",
        "actor_id": "kill_switch",
        "action_type": "kill_switch.mode_changed",
        "risk_tier": "red",
        "tool_used": "orchestrator.kill_switch",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "outcome": "success",
        "reason_code": "mode_changed",
        "receipt_type": "kill_switch.mode_changed",
        "receipt_hash": "",
        "details": {
            "old_mode": old_mode.value,
            "new_mode": new_mode.value,
        },
    }


def reset_kill_switch() -> None:
    """Reset kill switch to default. Testing only."""
    global _mode_override
    _mode_override = None
