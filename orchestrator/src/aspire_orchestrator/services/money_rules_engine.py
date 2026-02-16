"""Money Rules Engine — Transfer policy configuration and evaluation.

Loads transfer policy rules from YAML and evaluates whether a given
transfer is allowed, what approvals are required, and whether velocity
or pattern-based blocks apply.

Used by Finn Money Desk for payment authorization decisions.

Law compliance:
- Law #2: Every evaluation produces a receipt
- Law #3: Over-limit or blocked -> deny (fail-closed)
- Law #4: All transfers are RED tier (financial operations)
- Law #7: Pure logic — no provider calls
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

ACTOR_MONEY_RULES = "service:money-rules-engine"

_DEFAULT_RULES_PATH = Path(__file__).parent.parent / "config" / "money_rules.yaml"


# =============================================================================
# Data Classes
# =============================================================================


@dataclass(frozen=True)
class ApprovalThreshold:
    """An approval threshold from the money rules config."""

    max_cents: int
    approvers_required: int
    presence_required: bool
    cooling_period_minutes: int = 0


@dataclass(frozen=True)
class BlockedPattern:
    """A blocked/flagged transaction pattern."""

    pattern: str
    description: str
    threshold_cents: int = 0
    window_minutes: int = 0
    max_count: int = 0


@dataclass(frozen=True)
class VelocityLimits:
    """Velocity controls for transaction frequency."""

    max_transactions_per_hour: int
    max_transactions_per_day: int
    max_unique_recipients_per_day: int


@dataclass(frozen=True)
class TransactionLimits:
    """Per-transaction and aggregate limits."""

    single_transaction_max_cents: int
    daily_aggregate_max_cents: int
    weekly_aggregate_max_cents: int


@dataclass
class MoneyRulesConfig:
    """Loaded money rules configuration."""

    version: str
    default_currency: str
    transaction_limits: TransactionLimits
    approval_thresholds: list[ApprovalThreshold]
    velocity_limits: VelocityLimits
    blocked_patterns: list[BlockedPattern]


@dataclass(frozen=True)
class MoneyRuleResult:
    """Result of a transfer evaluation."""

    allowed: bool
    approvers_required: int
    presence_required: bool
    cooling_period_minutes: int
    flags: list[str]
    receipt: dict[str, Any]
    deny_reason: str = ""


@dataclass(frozen=True)
class VelocityResult:
    """Result of a velocity check."""

    within_limits: bool
    transactions_this_hour: int
    transactions_this_day: int
    unique_recipients_today: int
    flags: list[str]
    receipt: dict[str, Any]


@dataclass(frozen=True)
class PatternResult:
    """Result of a blocked pattern check."""

    blocked: bool
    triggered_patterns: list[str]
    receipt: dict[str, Any]


@dataclass
class RecentTransaction:
    """A recent transaction for velocity/pattern checking."""

    transaction_id: str
    recipient_id: str
    amount_cents: int
    timestamp: str  # ISO 8601


# =============================================================================
# Rules Loading
# =============================================================================


def load_money_rules(path: Path | str | None = None) -> MoneyRulesConfig:
    """Load money rules from YAML configuration.

    Fails closed if file is missing or malformed (Law #3).
    """
    rules_path = Path(path) if path else _DEFAULT_RULES_PATH

    if not rules_path.exists():
        raise FileNotFoundError(
            f"Money rules not found at {rules_path}. "
            "Fail-closed: cannot evaluate transfers without rules (Law #3)."
        )

    with open(rules_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError("Money rules YAML must be a mapping at top level")

    # Parse transaction limits
    txn_raw = raw.get("transaction_limits", {})
    transaction_limits = TransactionLimits(
        single_transaction_max_cents=txn_raw.get("single_transaction_max_cents", 50000000),
        daily_aggregate_max_cents=txn_raw.get("daily_aggregate_max_cents", 100000000),
        weekly_aggregate_max_cents=txn_raw.get("weekly_aggregate_max_cents", 500000000),
    )

    # Parse approval thresholds (sorted by max_cents ascending)
    thresholds_raw = raw.get("approval_thresholds", [])
    approval_thresholds = sorted(
        [
            ApprovalThreshold(
                max_cents=t.get("max_cents", 0),
                approvers_required=t.get("approvers_required", 1),
                presence_required=t.get("presence_required", True),
                cooling_period_minutes=t.get("cooling_period_minutes", 0),
            )
            for t in thresholds_raw
        ],
        key=lambda t: t.max_cents,
    )

    # Parse velocity limits
    vel_raw = raw.get("velocity_limits", {})
    velocity_limits = VelocityLimits(
        max_transactions_per_hour=vel_raw.get("max_transactions_per_hour", 10),
        max_transactions_per_day=vel_raw.get("max_transactions_per_day", 50),
        max_unique_recipients_per_day=vel_raw.get("max_unique_recipients_per_day", 20),
    )

    # Parse blocked patterns
    patterns_raw = raw.get("blocked_patterns", [])
    blocked_patterns = [
        BlockedPattern(
            pattern=p.get("pattern", ""),
            description=p.get("description", ""),
            threshold_cents=p.get("threshold_cents", 0),
            window_minutes=p.get("window_minutes", 0),
            max_count=p.get("max_count", 0),
        )
        for p in patterns_raw
    ]

    logger.info(
        "Money rules loaded: version=%s, thresholds=%d, patterns=%d",
        raw.get("version", "unknown"),
        len(approval_thresholds),
        len(blocked_patterns),
    )

    return MoneyRulesConfig(
        version=raw.get("version", "unknown"),
        default_currency=raw.get("default_currency", "USD"),
        transaction_limits=transaction_limits,
        approval_thresholds=approval_thresholds,
        velocity_limits=velocity_limits,
        blocked_patterns=blocked_patterns,
    )


# Module-level cached config
_cached_config: MoneyRulesConfig | None = None


def get_money_rules(*, reload: bool = False) -> MoneyRulesConfig:
    """Get the cached money rules, loading if needed."""
    global _cached_config
    if _cached_config is None or reload:
        _cached_config = load_money_rules()
    return _cached_config


# =============================================================================
# Receipt Builder
# =============================================================================


def _build_receipt(
    *,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    action_type: str,
    outcome: str,
    reason_code: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for a money rules operation (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_version": "1.0",
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": f"money_rules.{action_type}",
        "suite_id": suite_id,
        "office_id": office_id,
        "actor": ACTOR_MONEY_RULES,
        "correlation_id": correlation_id,
        "action_type": action_type,
        "outcome": outcome,
        "reason_code": reason_code,
    }
    if details:
        receipt["details"] = details
    return receipt


# =============================================================================
# Core Evaluation Functions
# =============================================================================


def evaluate_transfer(
    amount_cents: int,
    recipient_id: str,
    *,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    config: MoneyRulesConfig | None = None,
) -> MoneyRuleResult:
    """Evaluate a transfer against money rules.

    Law #2: Produces a receipt.
    Law #3: Over-limit -> deny (fail-closed).
    Law #4: All transfers are RED tier.
    Law #6: Scoped to suite_id/office_id.

    Args:
        amount_cents: Transfer amount in cents.
        recipient_id: Identifier for the transfer recipient.
        suite_id: Tenant suite ID.
        office_id: Tenant office ID.
        correlation_id: Trace ID for the operation.
        config: Optional pre-loaded config (uses cached if None).

    Returns:
        MoneyRuleResult with allowed flag, approval requirements, and receipt.
    """
    rules = config or get_money_rules()
    flags: list[str] = []

    # Check single transaction limit
    if amount_cents > rules.transaction_limits.single_transaction_max_cents:
        receipt = _build_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="evaluate",
            outcome="denied",
            reason_code="SINGLE_TXN_LIMIT_EXCEEDED",
            details={
                "amount_cents": amount_cents,
                "limit_cents": rules.transaction_limits.single_transaction_max_cents,
                "recipient_id": recipient_id,
            },
        )
        return MoneyRuleResult(
            allowed=False,
            approvers_required=0,
            presence_required=False,
            cooling_period_minutes=0,
            flags=["SINGLE_TXN_LIMIT_EXCEEDED"],
            receipt=receipt,
            deny_reason=f"Amount {amount_cents} exceeds single transaction limit "
                        f"{rules.transaction_limits.single_transaction_max_cents}",
        )

    # Determine approval threshold
    approvers_required = 1
    presence_required = True
    cooling_period_minutes = 0

    for threshold in rules.approval_thresholds:
        if amount_cents <= threshold.max_cents:
            approvers_required = threshold.approvers_required
            presence_required = threshold.presence_required
            cooling_period_minutes = threshold.cooling_period_minutes
            break
    else:
        # Amount exceeds all thresholds — use highest
        if rules.approval_thresholds:
            highest = rules.approval_thresholds[-1]
            approvers_required = highest.approvers_required
            presence_required = highest.presence_required
            cooling_period_minutes = highest.cooling_period_minutes
            flags.append("EXCEEDS_ALL_THRESHOLDS")

    # Check for round number pattern
    for pattern in rules.blocked_patterns:
        if pattern.pattern == "round_number":
            if (
                amount_cents >= pattern.threshold_cents
                and amount_cents % 100 == 0
            ):
                flags.append("ROUND_NUMBER_FLAG")

    receipt = _build_receipt(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        action_type="evaluate",
        outcome="success",
        details={
            "amount_cents": amount_cents,
            "recipient_id": recipient_id,
            "approvers_required": approvers_required,
            "presence_required": presence_required,
            "cooling_period_minutes": cooling_period_minutes,
            "flags": flags,
        },
    )

    logger.info(
        "Transfer evaluated: suite=%s, amount=%d, approvers=%d, presence=%s, "
        "cooling=%dmin, flags=%s",
        suite_id[:8] if len(suite_id) > 8 else suite_id,
        amount_cents, approvers_required, presence_required,
        cooling_period_minutes, flags,
    )

    return MoneyRuleResult(
        allowed=True,
        approvers_required=approvers_required,
        presence_required=presence_required,
        cooling_period_minutes=cooling_period_minutes,
        flags=flags,
        receipt=receipt,
    )


def check_velocity(
    recent_transactions: list[RecentTransaction],
    *,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    config: MoneyRulesConfig | None = None,
    now: datetime | None = None,
) -> VelocityResult:
    """Check whether velocity limits are exceeded.

    Law #2: Produces a receipt.
    Law #3: Exceeded velocity -> flag (fail-closed on hard limits).
    Law #6: Scoped to suite_id/office_id.

    Args:
        recent_transactions: List of recent transactions for this suite/office.
        suite_id: Tenant suite ID.
        office_id: Tenant office ID.
        correlation_id: Trace ID for the operation.
        config: Optional pre-loaded config.
        now: Override current time (for testing).

    Returns:
        VelocityResult with limit check details.
    """
    rules = config or get_money_rules()
    check_time = now or datetime.now(timezone.utc)
    flags: list[str] = []

    one_hour_ago = check_time - timedelta(hours=1)
    one_day_ago = check_time - timedelta(days=1)

    txns_this_hour = 0
    txns_this_day = 0
    unique_recipients: set[str] = set()

    for txn in recent_transactions:
        txn_time = datetime.fromisoformat(txn.timestamp)
        if txn_time.tzinfo is None:
            txn_time = txn_time.replace(tzinfo=timezone.utc)

        if txn_time >= one_day_ago:
            txns_this_day += 1
            unique_recipients.add(txn.recipient_id)

            if txn_time >= one_hour_ago:
                txns_this_hour += 1

    within_limits = True

    if txns_this_hour >= rules.velocity_limits.max_transactions_per_hour:
        within_limits = False
        flags.append("HOURLY_LIMIT_EXCEEDED")

    if txns_this_day >= rules.velocity_limits.max_transactions_per_day:
        within_limits = False
        flags.append("DAILY_LIMIT_EXCEEDED")

    if len(unique_recipients) >= rules.velocity_limits.max_unique_recipients_per_day:
        within_limits = False
        flags.append("UNIQUE_RECIPIENTS_LIMIT_EXCEEDED")

    outcome = "success" if within_limits else "denied"
    reason_code = "|".join(flags) if flags else ""

    receipt = _build_receipt(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        action_type="velocity_check",
        outcome=outcome,
        reason_code=reason_code,
        details={
            "transactions_this_hour": txns_this_hour,
            "transactions_this_day": txns_this_day,
            "unique_recipients_today": len(unique_recipients),
            "limits": {
                "max_per_hour": rules.velocity_limits.max_transactions_per_hour,
                "max_per_day": rules.velocity_limits.max_transactions_per_day,
                "max_recipients": rules.velocity_limits.max_unique_recipients_per_day,
            },
            "flags": flags,
        },
    )

    return VelocityResult(
        within_limits=within_limits,
        transactions_this_hour=txns_this_hour,
        transactions_this_day=txns_this_day,
        unique_recipients_today=len(unique_recipients),
        flags=flags,
        receipt=receipt,
    )


def check_blocked_patterns(
    amount_cents: int,
    recipient_id: str,
    recent_txns: list[RecentTransaction],
    *,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    config: MoneyRulesConfig | None = None,
    now: datetime | None = None,
) -> PatternResult:
    """Check whether a transaction triggers any blocked patterns.

    Law #2: Produces a receipt.
    Law #3: Blocked pattern -> deny (fail-closed).
    Law #6: Scoped to suite_id/office_id.

    Args:
        amount_cents: Transfer amount in cents.
        recipient_id: Identifier for the transfer recipient.
        recent_txns: Recent transactions for pattern analysis.
        suite_id: Tenant suite ID.
        office_id: Tenant office ID.
        correlation_id: Trace ID for the operation.
        config: Optional pre-loaded config.
        now: Override current time (for testing).

    Returns:
        PatternResult with blocked flag and triggered patterns.
    """
    rules = config or get_money_rules()
    check_time = now or datetime.now(timezone.utc)
    triggered: list[str] = []

    for pattern in rules.blocked_patterns:
        if pattern.pattern == "round_number":
            if (
                amount_cents >= pattern.threshold_cents
                and amount_cents % 100 == 0
            ):
                triggered.append(f"round_number: {pattern.description}")

        elif pattern.pattern == "split_transaction":
            window_start = check_time - timedelta(minutes=pattern.window_minutes)
            same_recipient_in_window = 0
            for txn in recent_txns:
                txn_time = datetime.fromisoformat(txn.timestamp)
                if txn_time.tzinfo is None:
                    txn_time = txn_time.replace(tzinfo=timezone.utc)
                if txn.recipient_id == recipient_id and txn_time >= window_start:
                    same_recipient_in_window += 1

            if same_recipient_in_window >= pattern.max_count:
                triggered.append(f"split_transaction: {pattern.description}")

    blocked = len(triggered) > 0
    outcome = "denied" if blocked else "success"

    receipt = _build_receipt(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        action_type="pattern_check",
        outcome=outcome,
        reason_code="BLOCKED_PATTERN" if blocked else "",
        details={
            "amount_cents": amount_cents,
            "recipient_id": recipient_id,
            "triggered_patterns": triggered,
            "patterns_checked": len(rules.blocked_patterns),
        },
    )

    return PatternResult(
        blocked=blocked,
        triggered_patterns=triggered,
        receipt=receipt,
    )
