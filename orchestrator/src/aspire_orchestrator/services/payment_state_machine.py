"""Payment State Machine — 6-state payment lifecycle with dual sequential approval.

States: DRAFT -> OWNER_APPROVED -> ACCOUNTANT_APPROVED -> EXECUTING -> RECONCILED -> FAILED

Governance guarantees:
  - Every transition produces a TransitionReceipt (Law #2)
  - Invalid transitions raise InvalidTransitionError with denial receipt (Law #3)
  - DRAFT->OWNER_APPROVED requires RED (owner approval + presence_token, Law #4/8)
  - OWNER_APPROVED->ACCOUNTANT_APPROVED requires RED (accountant approval, Law #4)
  - Dual SEQUENTIAL approval: owner first, then accountant (order enforced)
  - ACCOUNTANT_APPROVED->EXECUTING triggers provider call (externally — Law #7)
  - EXECUTING->RECONCILED or EXECUTING->FAILED based on provider response
  - Terminal states: RECONCILED, FAILED
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
    entity_type: str = "payment"
    from_state: str = ""
    to_state: str = ""
    actor_id: str = ""
    actor_type: str = "system"
    risk_tier: str = "green"
    outcome: str = "success"
    reason_code: str | None = None
    approval_evidence: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed (Law #3: fail-closed)."""

    def __init__(self, message: str, *, denial_receipt: TransitionReceipt) -> None:
        self.denial_receipt = denial_receipt
        super().__init__(message)


# Risk tier per target state
RISK_TIERS: dict[str, str] = {
    "draft": "green",
    "owner_approved": "red",
    "accountant_approved": "red",
    "executing": "green",
    "reconciled": "green",
    "failed": "green",
}

# Valid state transitions — adjacency list (sequential dual-approval enforced by order)
TRANSITIONS: dict[str, list[str]] = {
    "draft": ["owner_approved"],
    "owner_approved": ["accountant_approved"],
    "accountant_approved": ["executing"],
    "executing": ["reconciled", "failed"],
    "reconciled": [],   # terminal
    "failed": [],       # terminal
}

VALID_STATES: frozenset[str] = frozenset(TRANSITIONS.keys())

TERMINAL_STATES: frozenset[str] = frozenset(
    s for s, targets in TRANSITIONS.items() if len(targets) == 0
)


def _validate_approval_role(
    approval_evidence: dict[str, Any],
    expected_role: str,
) -> str | None:
    """Validate that approval_evidence contains the expected approver role.

    Returns an error message if validation fails, None if valid.
    """
    approver_role = approval_evidence.get("approver_role", "")
    if approver_role.lower() != expected_role.lower():
        return (
            f"Expected approver_role={expected_role!r}, "
            f"got {approver_role!r}"
        )
    return None


class PaymentStateMachine:
    """6-state payment lifecycle engine with dual sequential approval.

    Pure logic — no I/O. All transitions produce immutable receipts.

    Dual sequential approval flow:
    1. Owner approves (RED + presence required)
    2. Accountant approves (RED)
    3. Only then can execution proceed
    """

    def __init__(
        self,
        entity_id: str,
        suite_id: str,
        office_id: str,
        *,
        initial_state: str = "draft",
    ) -> None:
        if initial_state not in VALID_STATES:
            raise ValueError(f"Invalid initial state: {initial_state!r}")

        self._entity_id = entity_id
        self._suite_id = suite_id
        self._office_id = office_id
        self._current_state = initial_state
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
            entity_id: The payment being transitioned (must match self).
            from_state: Expected current state (safety check).
            to_state: Target state.
            suite_id: Tenant suite (must match self, Law #6).
            office_id: Tenant office (must match self, Law #6).
            correlation_id: Trace ID for the request chain.
            actor_id: Who is initiating the transition.
            approval_evidence: Required for RED tiers (Law #4).
                For owner_approved: must contain approver_role="owner".
                For accountant_approved: must contain approver_role="accountant".
            presence_token: Required for DRAFT->OWNER_APPROVED (RED + presence, Law #4/8).
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

        # CHECK: DRAFT->OWNER_APPROVED requires presence_token (RED + presence, Law #4/8)
        if to_state == "owner_approved" and not presence_token:
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
                "DRAFT->OWNER_APPROVED requires presence_token "
                "(owner must be present for payment approval, Law #4/8)",
                denial_receipt=denial,
            )

        # CHECK: OWNER_APPROVED role validation
        if to_state == "owner_approved" and approval_evidence:
            role_error = _validate_approval_role(approval_evidence, "owner")
            if role_error:
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
                    reason_code="invalid_approver_role",
                    approval_evidence=approval_evidence,
                )
                self._history.append(denial)
                raise InvalidTransitionError(
                    f"Owner approval role validation failed: {role_error}",
                    denial_receipt=denial,
                )

        # CHECK: ACCOUNTANT_APPROVED role validation
        if to_state == "accountant_approved" and approval_evidence:
            role_error = _validate_approval_role(approval_evidence, "accountant")
            if role_error:
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
                    reason_code="invalid_approver_role",
                    approval_evidence=approval_evidence,
                )
                self._history.append(denial)
                raise InvalidTransitionError(
                    f"Accountant approval role validation failed: {role_error}",
                    denial_receipt=denial,
                )

            # CHECK: Accountant must be DIFFERENT person than owner (dual-approval integrity)
            accountant_id = approval_evidence.get("approver_id", "")
            owner_receipts = [
                r for r in self._history
                if r.to_state == "owner_approved" and r.outcome == "success"
            ]
            if owner_receipts:
                owner_evidence = owner_receipts[-1].approval_evidence or {}
                owner_id = owner_evidence.get("approver_id", "")
                if owner_id and accountant_id and owner_id == accountant_id:
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
                        f"Accountant approver ({accountant_id}) cannot be the same "
                        f"person as owner approver ({owner_id}). Dual approval "
                        f"requires distinct approvers.",
                        denial_receipt=denial,
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
        )
        self._history.append(receipt)

        logger.info(
            "Payment %s transitioned %s -> %s (actor=%s, corr=%s, tier=%s)",
            entity_id[:8] if len(entity_id) > 8 else entity_id,
            old_state,
            to_state,
            actor_id,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            risk_tier,
        )

        return receipt
