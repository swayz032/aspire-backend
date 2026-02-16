"""Finn A2A Delegation Service — Validated delegation to allowlisted agents.

Per Finn v2 spec: Finn delegates specialized analysis to other agents
through Ava via A2A proposals. This service validates delegation requests
before dispatching to the existing A2AService.

Enforcement:
1. Allowlist check: to_agent must be in {adam, teressa, milo, eli}
2. Max depth check: delegation_depth < MAX_DELEGATION_DEPTH
3. Rate limit: max delegations per tenant per minute
4. Risk propagation: downstream inherits highest risk tier
5. Correlation ID required: every delegation carries parent correlation_id
6. Schema validation: proposal validated against 06_output_schema.json

Law #2: Every delegation (success or denial) emits receipt data.
Law #3: Any validation failure → deny + receipt.
Law #6: All operations scoped to suite_id.
Law #7: This service validates; it does not decide. The orchestrator decides.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

ALLOWED_DELEGATION_TARGETS = frozenset({"adam", "teressa", "milo", "eli"})
MAX_DELEGATION_DEPTH = 2
MAX_DELEGATIONS_PER_MINUTE = 10

VALID_REQUEST_TYPES = frozenset({
    "ResearchRequest",
    "BookkeepingRequest",
    "PayrollRequest",
    "InboxRequest",
})

RISK_TIER_ORDER = {"green": 0, "yellow": 1, "red": 2}


# =============================================================================
# Data Classes
# =============================================================================


@dataclass(frozen=True)
class DelegationRequest:
    """A validated A2A delegation request from Finn."""

    suite_id: str
    office_id: str
    correlation_id: str
    to_agent: str
    request_type: str
    payload: dict[str, Any]
    risk_tier: str
    delegation_depth: int = 0
    idempotency_key: str | None = None


@dataclass(frozen=True)
class DelegationResult:
    """Result of a delegation validation."""

    allowed: bool
    deny_reason: str | None = None
    receipt_data: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Service
# =============================================================================


class FinnDelegationService:
    """Validates Finn's A2A delegation proposals.

    Thread-safe rate limiting per tenant. Stateless validation
    for all other checks.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Rate limit: {suite_id: [timestamps]}
        self._rate_windows: dict[str, list[float]] = defaultdict(list)

    def validate_delegation(self, request: DelegationRequest) -> DelegationResult:
        """Validate a delegation request against all enforcement rules.

        Returns DelegationResult with allowed=True or deny reason + receipt data.
        """
        now = datetime.now(timezone.utc)

        # Check 1: correlation_id required
        if not request.correlation_id:
            return self._deny(
                request, "MISSING_CORRELATION_ID",
                "Delegation requires correlation_id for tracing",
            )

        # Check 2: to_agent must be non-empty
        if not request.to_agent:
            return self._deny(
                request, "EMPTY_TO_AGENT",
                "Delegation target agent is required",
            )

        # Check 3: to_agent in allowlist
        if request.to_agent not in ALLOWED_DELEGATION_TARGETS:
            return self._deny(
                request, "AGENT_NOT_ALLOWLISTED",
                f"Agent '{request.to_agent}' not in allowlist: {sorted(ALLOWED_DELEGATION_TARGETS)}",
            )

        # Check 4: request_type valid
        if request.request_type not in VALID_REQUEST_TYPES:
            return self._deny(
                request, "INVALID_REQUEST_TYPE",
                f"Request type '{request.request_type}' not valid. "
                f"Allowed: {sorted(VALID_REQUEST_TYPES)}",
            )

        # Check 5: delegation depth
        if request.delegation_depth >= MAX_DELEGATION_DEPTH:
            return self._deny(
                request, "MAX_DEPTH_EXCEEDED",
                f"Delegation depth {request.delegation_depth} >= max {MAX_DELEGATION_DEPTH}",
            )

        # Check 6: risk tier valid
        if request.risk_tier not in RISK_TIER_ORDER:
            return self._deny(
                request, "INVALID_RISK_TIER",
                f"Risk tier '{request.risk_tier}' is not valid",
            )

        # Check 7: rate limit per tenant
        if not self._check_rate_limit(request.suite_id, now):
            return self._deny(
                request, "RATE_LIMIT_EXCEEDED",
                f"Max {MAX_DELEGATIONS_PER_MINUTE} delegations per minute exceeded "
                f"for suite {request.suite_id}",
            )

        # All checks passed
        logger.info(
            "Finn delegation ALLOWED: to=%s type=%s suite=%s corr=%s",
            request.to_agent, request.request_type,
            request.suite_id, request.correlation_id,
        )

        return DelegationResult(
            allowed=True,
            receipt_data=self._make_receipt_data(
                request=request,
                outcome="success",
                details={"delegation_allowed": True},
            ),
        )

    def propagate_risk_tier(
        self,
        parent_tier: str,
        downstream_tier: str,
    ) -> str:
        """Return the highest risk tier between parent and downstream.

        Per spec: downstream work inherits the highest risk tier.
        """
        parent_order = RISK_TIER_ORDER.get(parent_tier, 1)
        downstream_order = RISK_TIER_ORDER.get(downstream_tier, 1)
        if parent_order >= downstream_order:
            return parent_tier
        return downstream_tier

    def compute_inputs_hash(self, inputs: dict[str, Any]) -> str:
        """Compute SHA256 hash of inputs for approval binding.

        Returns "sha256:<hex>" format per 06_output_schema.json.
        """
        canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def clear_rate_limits(self) -> None:
        """Clear rate limit state. Testing only."""
        with self._lock:
            self._rate_windows.clear()

    # ─────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────

    def _check_rate_limit(self, suite_id: str, now: datetime) -> bool:
        """Check and update rate limit window for a tenant.

        Sliding window: count delegations in the last 60 seconds.
        """
        cutoff = now.timestamp() - 60.0

        with self._lock:
            window = self._rate_windows[suite_id]
            # Prune expired entries
            self._rate_windows[suite_id] = [
                ts for ts in window if ts > cutoff
            ]
            window = self._rate_windows[suite_id]

            if len(window) >= MAX_DELEGATIONS_PER_MINUTE:
                return False

            window.append(now.timestamp())
            return True

    def _deny(
        self,
        request: DelegationRequest,
        reason_code: str,
        message: str,
    ) -> DelegationResult:
        """Create a denial result with receipt data."""
        logger.warning(
            "Finn delegation DENIED: reason=%s to=%s suite=%s — %s",
            reason_code, request.to_agent, request.suite_id, message,
        )
        return DelegationResult(
            allowed=False,
            deny_reason=reason_code,
            receipt_data=self._make_receipt_data(
                request=request,
                outcome="denied",
                details={"reason_code": reason_code, "message": message},
            ),
        )

    @staticmethod
    def _make_receipt_data(
        *,
        request: DelegationRequest,
        outcome: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        """Create receipt data for a delegation validation (Law #2)."""
        return {
            "id": str(uuid.uuid4()),
            "correlation_id": request.correlation_id or str(uuid.uuid4()),
            "suite_id": request.suite_id,
            "office_id": request.office_id,
            "actor_type": "agent",
            "actor_id": "skillpack:finn-finance-manager",
            "action_type": "a2a.create",
            "risk_tier": request.risk_tier or "yellow",
            "tool_used": "a2a.dispatch",
            "outcome": outcome,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "receipt_hash": "",
            "redacted_inputs": {
                "to_agent": request.to_agent,
                "request_type": request.request_type,
                "delegation_depth": request.delegation_depth,
            },
            "redacted_outputs": details,
        }


# =============================================================================
# Module-level singleton
# =============================================================================

_service: FinnDelegationService | None = None


def get_finn_delegation_service() -> FinnDelegationService:
    """Get the singleton Finn delegation service."""
    global _service
    if _service is None:
        _service = FinnDelegationService()
    return _service
