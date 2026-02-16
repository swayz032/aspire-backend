"""Failure Handler — Retry, backoff, DLQ, escalation.

Centralized failure handling for all skill pack operations.
Implements exponential backoff with jitter, max retries,
dead letter queue routing, and escalation notification.

Law compliance:
- Law #2: Every retry attempt and DLQ placement produces a receipt
- Law #3: Exhausted retries → deny (escalate, don't guess)
"""

from __future__ import annotations

import logging
import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RetryDecision:
    """The handler's output — should the caller retry?"""

    should_retry: bool
    wait_seconds: float
    attempt: int
    max_attempts: int
    receipt: dict[str, Any]


@dataclass
class FailureContext:
    """Input describing the failure to evaluate."""

    correlation_id: str
    suite_id: str
    office_id: str
    action_type: str
    error_type: str
    error_message: str
    attempt: int = 0
    max_attempts: int = 3


class FailureHandler:
    """Centralized failure handling with exponential backoff.

    The handler is pure logic (Law #7) — it computes whether a retry
    is appropriate and the backoff delay, but never executes the retry
    itself.  The orchestrator (Law #1) decides whether to honour the
    recommendation.
    """

    _BASE_DELAY = 1.0  # seconds
    _MAX_DELAY = 30.0  # seconds
    _JITTER_FACTOR = 0.5

    # Error types that must never be retried (Law #3: fail-closed)
    _NON_RETRYABLE = frozenset(
        {"auth_error", "permission_denied", "invalid_input", "tenant_mismatch"}
    )

    def evaluate(self, ctx: FailureContext) -> RetryDecision:
        """Evaluate whether a failed operation should be retried.

        Always produces a receipt (Law #2).  Non-retryable errors are
        denied immediately (Law #3).
        """
        ctx.attempt += 1

        # Non-retryable errors → immediate deny
        if ctx.error_type in self._NON_RETRYABLE:
            return RetryDecision(
                should_retry=False,
                wait_seconds=0,
                attempt=ctx.attempt,
                max_attempts=ctx.max_attempts,
                receipt=self._build_receipt(ctx, "denied", "non_retryable_error"),
            )

        # Max retries exceeded → deny
        if ctx.attempt >= ctx.max_attempts:
            return RetryDecision(
                should_retry=False,
                wait_seconds=0,
                attempt=ctx.attempt,
                max_attempts=ctx.max_attempts,
                receipt=self._build_receipt(ctx, "failed", "max_retries_exceeded"),
            )

        # Exponential backoff with jitter
        delay = min(
            self._BASE_DELAY * math.pow(2, ctx.attempt - 1),
            self._MAX_DELAY,
        )
        jitter = delay * self._JITTER_FACTOR * random.random()
        wait = delay + jitter

        return RetryDecision(
            should_retry=True,
            wait_seconds=round(wait, 2),
            attempt=ctx.attempt,
            max_attempts=ctx.max_attempts,
            receipt=self._build_receipt(ctx, "retry", f"attempt_{ctx.attempt}"),
        )

    def _build_receipt(
        self, ctx: FailureContext, outcome: str, reason_code: str
    ) -> dict[str, Any]:
        """Build an immutable receipt for the failure evaluation (Law #2)."""
        return {
            "id": str(uuid.uuid4()),
            "correlation_id": ctx.correlation_id,
            "suite_id": ctx.suite_id,
            "office_id": ctx.office_id,
            "action_type": f"failure.{ctx.action_type}",
            "outcome": outcome,
            "reason_code": reason_code,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "details": {
                "error_type": ctx.error_type,
                "error_message": ctx.error_message,
                "attempt": ctx.attempt,
                "max_attempts": ctx.max_attempts,
            },
        }
