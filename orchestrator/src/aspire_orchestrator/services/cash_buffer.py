"""Cash Buffer Service — Cash reserve forecasting and alerts for Finn Money Desk.

Responsibilities:
- Calculate current cash position from account balances
- Forecast cash needs for upcoming payroll/payments
- Alert when reserves drop below configurable threshold
- Validate sufficient funds before payment authorization

Law compliance:
- Law #2: Every forecast/alert check produces a receipt
- Law #3: Insufficient funds -> deny (fail-closed)
- Law #7: Pure logic — no provider calls
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Default buffer threshold: 20% of monthly operating costs
DEFAULT_BUFFER_THRESHOLD_PERCENT = 0.20

ACTOR_CASH_BUFFER = "service:cash-buffer"


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class CashPosition:
    """Current cash position for a suite/office."""

    suite_id: str
    office_id: str
    available_balance_cents: int
    reserved_cents: int = 0
    forecasted_outflow_cents: int = 0
    buffer_threshold_cents: int = 0

    @property
    def effective_balance_cents(self) -> int:
        """Available balance minus reserves."""
        return self.available_balance_cents - self.reserved_cents

    @property
    def buffer_healthy(self) -> bool:
        """True if effective balance exceeds threshold after forecasted outflow."""
        return (self.effective_balance_cents - self.forecasted_outflow_cents) >= self.buffer_threshold_cents


@dataclass(frozen=True)
class CashBufferResult:
    """Result of a cash buffer sufficiency check."""

    sufficient: bool
    available_cents: int
    requested_cents: int
    shortfall_cents: int
    buffer_threshold_cents: int
    receipt: dict[str, Any]


@dataclass(frozen=True)
class UpcomingObligation:
    """A single upcoming payment obligation."""

    obligation_id: str
    description: str
    amount_cents: int
    due_date: str  # ISO 8601
    category: str  # payroll, vendor, tax, loan, other


@dataclass
class CashForecast:
    """Forecast of cash needs over a period."""

    suite_id: str
    office_id: str
    days_ahead: int
    obligations: list[UpcomingObligation]
    total_forecasted_outflow_cents: int
    current_balance_cents: int
    buffer_status: str  # healthy, warning, critical
    receipt: dict[str, Any] = field(default_factory=dict)


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
    """Build a receipt for a cash buffer operation (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_version": "1.0",
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": f"cash_buffer.{action_type}",
        "suite_id": suite_id,
        "office_id": office_id,
        "actor": ACTOR_CASH_BUFFER,
        "correlation_id": correlation_id,
        "action_type": action_type,
        "outcome": outcome,
        "reason_code": reason_code,
    }
    if details:
        receipt["details"] = details
    return receipt


# =============================================================================
# Core Functions
# =============================================================================


def check_cash_buffer(
    position: CashPosition,
    amount_cents: int,
    *,
    correlation_id: str,
) -> CashBufferResult:
    """Check whether the cash buffer can support a given payment amount.

    Law #2: Produces a receipt regardless of outcome.
    Law #3: Insufficient funds -> deny (fail-closed).
    Law #6: Scoped to suite_id/office_id via CashPosition.

    Args:
        position: Current cash position for the suite/office.
        amount_cents: Amount in cents being requested.
        correlation_id: Trace ID for the operation.

    Returns:
        CashBufferResult with sufficiency verdict and receipt.
    """
    if amount_cents < 0:
        receipt = _build_receipt(
            suite_id=position.suite_id,
            office_id=position.office_id,
            correlation_id=correlation_id,
            action_type="check",
            outcome="denied",
            reason_code="INVALID_AMOUNT",
            details={"amount_cents": amount_cents, "error": "Amount cannot be negative"},
        )
        return CashBufferResult(
            sufficient=False,
            available_cents=position.effective_balance_cents,
            requested_cents=amount_cents,
            shortfall_cents=0,
            buffer_threshold_cents=position.buffer_threshold_cents,
            receipt=receipt,
        )

    remaining_after = position.effective_balance_cents - amount_cents
    shortfall = 0
    sufficient = True

    # Fail-closed: deny if payment would breach buffer threshold (Law #3)
    if remaining_after < position.buffer_threshold_cents:
        sufficient = False
        shortfall = position.buffer_threshold_cents - remaining_after

    outcome = "success" if sufficient else "denied"
    reason_code = "" if sufficient else "INSUFFICIENT_BUFFER"

    receipt = _build_receipt(
        suite_id=position.suite_id,
        office_id=position.office_id,
        correlation_id=correlation_id,
        action_type="check",
        outcome=outcome,
        reason_code=reason_code,
        details={
            "available_cents": position.effective_balance_cents,
            "requested_cents": amount_cents,
            "remaining_after_cents": remaining_after,
            "buffer_threshold_cents": position.buffer_threshold_cents,
            "shortfall_cents": shortfall,
        },
    )

    logger.info(
        "Cash buffer check: suite=%s, amount=%d, available=%d, sufficient=%s, corr=%s",
        position.suite_id[:8] if len(position.suite_id) > 8 else position.suite_id,
        amount_cents,
        position.effective_balance_cents,
        sufficient,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    return CashBufferResult(
        sufficient=sufficient,
        available_cents=position.effective_balance_cents,
        requested_cents=amount_cents,
        shortfall_cents=shortfall,
        buffer_threshold_cents=position.buffer_threshold_cents,
        receipt=receipt,
    )


def forecast_cash_needs(
    position: CashPosition,
    obligations: list[UpcomingObligation],
    *,
    days_ahead: int = 30,
    correlation_id: str,
) -> CashForecast:
    """Forecast cash needs for upcoming obligations.

    Law #2: Produces a receipt.
    Law #7: Pure logic, no provider calls.
    Law #6: Scoped to suite_id/office_id via CashPosition.

    Args:
        position: Current cash position for the suite/office.
        obligations: List of upcoming payment obligations.
        days_ahead: Number of days to look ahead (default 30).
        correlation_id: Trace ID for the operation.

    Returns:
        CashForecast with obligations, totals, and buffer status.
    """
    total_outflow = sum(o.amount_cents for o in obligations)

    # Determine buffer status
    remaining = position.effective_balance_cents - total_outflow
    if remaining >= position.buffer_threshold_cents:
        buffer_status = "healthy"
    elif remaining >= 0:
        buffer_status = "warning"
    else:
        buffer_status = "critical"

    receipt = _build_receipt(
        suite_id=position.suite_id,
        office_id=position.office_id,
        correlation_id=correlation_id,
        action_type="forecast",
        outcome="success",
        details={
            "days_ahead": days_ahead,
            "obligation_count": len(obligations),
            "total_forecasted_outflow_cents": total_outflow,
            "current_balance_cents": position.effective_balance_cents,
            "buffer_status": buffer_status,
        },
    )

    logger.info(
        "Cash forecast: suite=%s, days=%d, obligations=%d, outflow=%d, status=%s",
        position.suite_id[:8] if len(position.suite_id) > 8 else position.suite_id,
        days_ahead,
        len(obligations),
        total_outflow,
        buffer_status,
    )

    return CashForecast(
        suite_id=position.suite_id,
        office_id=position.office_id,
        days_ahead=days_ahead,
        obligations=obligations,
        total_forecasted_outflow_cents=total_outflow,
        current_balance_cents=position.effective_balance_cents,
        buffer_status=buffer_status,
        receipt=receipt,
    )


def compute_threshold_from_monthly_costs(
    monthly_operating_costs_cents: int,
    threshold_percent: float = DEFAULT_BUFFER_THRESHOLD_PERCENT,
) -> int:
    """Compute buffer threshold as a percentage of monthly operating costs.

    Default is 20% of monthly costs.
    """
    if monthly_operating_costs_cents < 0:
        raise ValueError("Monthly operating costs cannot be negative")
    if threshold_percent < 0 or threshold_percent > 1.0:
        raise ValueError("Threshold percent must be between 0.0 and 1.0")
    return int(monthly_operating_costs_cents * threshold_percent)
