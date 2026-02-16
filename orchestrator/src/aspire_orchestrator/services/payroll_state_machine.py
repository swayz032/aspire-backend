"""Payroll State Machine — 6-state payroll run lifecycle.

States: SNAPSHOT -> REVIEWED -> APPROVED -> PROCESSING -> CONFIRMED -> FAILED

Governance guarantees:
  - Every transition produces a TransitionReceipt (Law #2)
  - Invalid transitions raise InvalidTransitionError with denial receipt (Law #3)
  - SNAPSHOT->REVIEWED is GREEN (auto after generation)
  - REVIEWED->APPROVED requires RED dual_approval (HR + Finance) with presence_token (Law #4/8)
  - APPROVED->PROCESSING triggers Gusto API call (externally, not here — Law #7)
  - PROCESSING->CONFIRMED or PROCESSING->FAILED based on provider response
  - Deadline enforcement: past-deadline transitions emit warning receipt
  - Terminal states: CONFIRMED, FAILED
  - History is append-only (immutable, no UPDATE/DELETE)
  - Pure logic only — no HTTP calls, no provider calls (Law #7)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransitionReceipt:
    """Immutable receipt for a single state transition (Law #2)."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = ""
    suite_id: str = ""
    office_id: str = ""
    entity_id: str = ""
    entity_type: str = "payroll"
    from_state: str = ""
    to_state: str = ""
    actor_id: str = ""
    actor_type: str = "system"
    risk_tier: str = "green"
    outcome: str = "success"
    reason_code: str | None = None
    approval_evidence: dict[str, Any] | None = None
    warning: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed (Law #3: fail-closed)."""

    def __init__(self, message: str, *, denial_receipt: TransitionReceipt) -> None:
        self.denial_receipt = denial_receipt
        super().__init__(message)


# Required dual-approval roles for REVIEWED->APPROVED
DUAL_APPROVAL_ROLES: frozenset[str] = frozenset({"hr", "finance"})

# Risk tier per target state
RISK_TIERS: dict[str, str] = {
    "snapshot": "green",
    "reviewed": "green",
    "approved": "red",
    "processing": "green",
    "confirmed": "green",
    "failed": "green",
}

# Valid state transitions — adjacency list
TRANSITIONS: dict[str, list[str]] = {
    "snapshot": ["reviewed"],
    "reviewed": ["approved"],
    "approved": ["processing"],
    "processing": ["confirmed", "failed"],
    "confirmed": [],    # terminal
    "failed": [],       # terminal
}

VALID_STATES: frozenset[str] = frozenset(TRANSITIONS.keys())

TERMINAL_STATES: frozenset[str] = frozenset(
    s for s, targets in TRANSITIONS.items() if len(targets) == 0
)


def _validate_dual_approval(approval_evidence: dict[str, Any]) -> tuple[set[str], str | None]:
    """Validate dual-approval evidence contains both HR and Finance approvals
    from DIFFERENT approvers (same person cannot approve both roles).

    Returns (missing_roles, error_message). Both empty/None if valid.
    """
    approvals = approval_evidence.get("approvals", [])
    role_to_approver: dict[str, str] = {}
    for approval in approvals:
        if isinstance(approval, dict) and "role" in approval and "approver_id" in approval:
            role = approval["role"].lower()
            approver_id = approval["approver_id"]
            role_to_approver[role] = approver_id

    missing_roles = DUAL_APPROVAL_ROLES - set(role_to_approver.keys())
    if missing_roles:
        return missing_roles, None

    # Enforce distinct approvers — same person CANNOT approve both roles
    approver_ids = list(role_to_approver.values())
    if len(set(approver_ids)) < len(approver_ids):
        return set(), "DUAL_APPROVAL_SAME_APPROVER: same approver_id used for multiple roles"

    return set(), None


class PayrollStateMachine:
    """6-state payroll run lifecycle engine.

    Pure logic — no I/O. All transitions produce immutable receipts.
    """

    def __init__(
        self,
        entity_id: str,
        suite_id: str,
        office_id: str,
        *,
        initial_state: str = "snapshot",
        deadline: datetime | None = None,
    ) -> None:
        if initial_state not in VALID_STATES:
            raise ValueError(f"Invalid initial state: {initial_state!r}")

        self._entity_id = entity_id
        self._suite_id = suite_id
        self._office_id = office_id
        self._current_state = initial_state
        self._deadline = deadline
        self._history: list[TransitionReceipt] = []

    @property
    def entity_id(self) -> str:
        return self._entity_id

    @property
    def suite_id(self) -> str:
        return self._suite_id

    @property
    def office_id(self) -> str:
        return self._office_id

    @property
    def current_state(self) -> str:
        return self._current_state

    @property
    def deadline(self) -> datetime | None:
        return self._deadline

    @property
    def history(self) -> list[TransitionReceipt]:
        return list(self._history)

    @property
    def is_terminal(self) -> bool:
        return self._current_state in TERMINAL_STATES

    def get_valid_transitions(self) -> list[str]:
        return list(TRANSITIONS.get(self._current_state, []))

    def transition(
        self,
        entity_id: str,
        from_state: str,
        to_state: str,
        *,
        suite_id: str,
        office_id: str,
        correlation_id: str,
        actor_id: str,
        approval_evidence: dict[str, Any] | None = None,
        presence_token: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> TransitionReceipt:
        """Execute a state transition with full receipt emission.

        Args:
            entity_id: The payroll run being transitioned (must match self).
            from_state: Expected current state (safety check).
            to_state: Target state.
            suite_id: Tenant suite (must match self, Law #6).
            office_id: Tenant office (must match self, Law #6).
            correlation_id: Trace ID for the request chain.
            actor_id: Who is initiating the transition.
            approval_evidence: Required for RED tiers (Law #4).
                For REVIEWED->APPROVED, must contain dual_approval with HR + Finance roles.
            presence_token: Required for RED tier (Law #4/8).
            extra: Additional metadata for the receipt.

        Returns:
            TransitionReceipt on success.

        Raises:
            InvalidTransitionError: With denial receipt on failure (Law #3).
        """
        risk_tier = RISK_TIERS.get(to_state, "red")

        # CHECK: entity_id matches
        if entity_id != self._entity_id:
            denial = TransitionReceipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                actor_id=actor_id,
                risk_tier=risk_tier,
                outcome="denied",
                reason_code="entity_id_mismatch",
            )
            self._history.append(denial)
            raise InvalidTransitionError(
                f"Entity ID mismatch: expected {self._entity_id!r}, got {entity_id!r}",
                denial_receipt=denial,
            )

        # CHECK: suite_id matches (Law #6)
        if suite_id != self._suite_id:
            denial = TransitionReceipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                actor_id=actor_id,
                risk_tier=risk_tier,
                outcome="denied",
                reason_code="suite_id_mismatch",
            )
            self._history.append(denial)
            raise InvalidTransitionError(
                f"Suite ID mismatch (Law #6): expected {self._suite_id!r}",
                denial_receipt=denial,
            )

        # CHECK: office_id matches (Law #6)
        if office_id != self._office_id:
            denial = TransitionReceipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                actor_id=actor_id,
                risk_tier=risk_tier,
                outcome="denied",
                reason_code="office_id_mismatch",
            )
            self._history.append(denial)
            raise InvalidTransitionError(
                f"Office ID mismatch (Law #6): expected {self._office_id!r}",
                denial_receipt=denial,
            )

        # CHECK: from_state matches current state
        if from_state != self._current_state:
            denial = TransitionReceipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                actor_id=actor_id,
                risk_tier=risk_tier,
                outcome="denied",
                reason_code="state_mismatch",
            )
            self._history.append(denial)
            raise InvalidTransitionError(
                f"State mismatch: expected current state {self._current_state!r}, "
                f"caller specified {from_state!r}",
                denial_receipt=denial,
            )

        # CHECK: to_state is a known state
        if to_state not in VALID_STATES:
            denial = TransitionReceipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                actor_id=actor_id,
                risk_tier=risk_tier,
                outcome="denied",
                reason_code="unknown_state",
            )
            self._history.append(denial)
            raise InvalidTransitionError(
                f"Unknown target state: {to_state!r}",
                denial_receipt=denial,
            )

        # CHECK: transition is valid
        allowed = TRANSITIONS.get(self._current_state, [])
        if to_state not in allowed:
            denial = TransitionReceipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                actor_id=actor_id,
                risk_tier=risk_tier,
                outcome="denied",
                reason_code="invalid_transition",
            )
            self._history.append(denial)
            raise InvalidTransitionError(
                f"Cannot transition from {self._current_state!r} to {to_state!r}. "
                f"Allowed: {allowed}",
                denial_receipt=denial,
            )

        # CHECK: RED tier requires approval_evidence (Law #4)
        if risk_tier == "red" and not approval_evidence:
            denial = TransitionReceipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                actor_id=actor_id,
                risk_tier=risk_tier,
                outcome="denied",
                reason_code="missing_approval_evidence",
            )
            self._history.append(denial)
            raise InvalidTransitionError(
                f"RED tier transition to {to_state!r} requires approval evidence (Law #4)",
                denial_receipt=denial,
            )

        # CHECK: RED tier requires presence_token (Law #4/8)
        if risk_tier == "red" and not presence_token:
            denial = TransitionReceipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                actor_id=actor_id,
                risk_tier=risk_tier,
                outcome="denied",
                reason_code="missing_presence_token",
                approval_evidence=approval_evidence,
            )
            self._history.append(denial)
            raise InvalidTransitionError(
                f"RED tier transition to {to_state!r} requires presence_token (Law #4/8)",
                denial_receipt=denial,
            )

        # CHECK: REVIEWED->APPROVED requires dual_approval (HR + Finance, different approvers)
        if from_state == "reviewed" and to_state == "approved" and approval_evidence:
            missing_roles, same_approver_error = _validate_dual_approval(approval_evidence)
            if missing_roles:
                denial = TransitionReceipt(
                    correlation_id=correlation_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    entity_id=entity_id,
                    from_state=from_state,
                    to_state=to_state,
                    actor_id=actor_id,
                    risk_tier=risk_tier,
                    outcome="denied",
                    reason_code="missing_dual_approval",
                    approval_evidence=approval_evidence,
                )
                self._history.append(denial)
                raise InvalidTransitionError(
                    f"REVIEWED->APPROVED requires dual approval from {DUAL_APPROVAL_ROLES}. "
                    f"Missing roles: {missing_roles}",
                    denial_receipt=denial,
                )
            if same_approver_error:
                denial = TransitionReceipt(
                    correlation_id=correlation_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    entity_id=entity_id,
                    from_state=from_state,
                    to_state=to_state,
                    actor_id=actor_id,
                    risk_tier=risk_tier,
                    outcome="denied",
                    reason_code="dual_approval_same_approver",
                    approval_evidence=approval_evidence,
                )
                self._history.append(denial)
                raise InvalidTransitionError(
                    same_approver_error,
                    denial_receipt=denial,
                )

        # Deadline enforcement: emit warning if past deadline
        warning = None
        if self._deadline is not None:
            now = datetime.now(timezone.utc)
            if now > self._deadline:
                warning = (
                    f"Payroll {entity_id} transition {from_state}->{to_state} "
                    f"occurred past deadline {self._deadline.isoformat()}. "
                    f"Current time: {now.isoformat()}"
                )
                logger.warning(
                    "Payroll %s: past-deadline transition %s -> %s (deadline=%s)",
                    entity_id[:8] if len(entity_id) > 8 else entity_id,
                    from_state,
                    to_state,
                    self._deadline.isoformat(),
                )

        # SUCCESS: Execute transition
        old_state = self._current_state
        self._current_state = to_state

        receipt = TransitionReceipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            entity_id=entity_id,
            from_state=old_state,
            to_state=to_state,
            actor_id=actor_id,
            risk_tier=risk_tier,
            outcome="success",
            approval_evidence=approval_evidence,
            warning=warning,
        )
        self._history.append(receipt)

        logger.info(
            "Payroll %s transitioned %s -> %s (actor=%s, corr=%s, tier=%s%s)",
            entity_id[:8] if len(entity_id) > 8 else entity_id,
            old_state,
            to_state,
            actor_id,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            risk_tier,
            ", WARNING: past-deadline" if warning else "",
        )

        return receipt
