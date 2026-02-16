"""Mail State Machine — 13-state email lifecycle for Eli Phase B.

Implements the complete email state machine as PURE logic.
No HTTP calls — providers are separate (Law #7: tools are hands).

States (13):
  RECEIVED -> TRIAGED -> CLASSIFIED -> DRAFT_GENERATED -> DRAFT_REVIEWED ->
  APPROVED -> SENDING -> SENT -> DELIVERED -> BOUNCED -> FAILED -> ARCHIVED -> DELETED

Governance guarantees:
  - Every transition produces a MailTransitionReceipt (Law #2)
  - Invalid transitions raise InvalidTransitionError with denial receipt (Law #3)
  - History is append-only (immutable, no UPDATE/DELETE)
  - Terminal state (DELETED) allows no further transitions
"""

from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.services.mail_receipt_types import (
    MailReceiptType,
    MailTransitionReceipt,
    receipt_type_for_state,
)

logger = logging.getLogger(__name__)


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed (Law #3: fail-closed).

    Includes the denial receipt for the attempted transition.
    """

    def __init__(
        self,
        message: str,
        *,
        denial_receipt: MailTransitionReceipt,
    ) -> None:
        self.denial_receipt = denial_receipt
        super().__init__(message)


# Valid state transitions — adjacency list
TRANSITIONS: dict[str, list[str]] = {
    "received": ["triaged"],
    "triaged": ["classified"],
    "classified": ["draft_generated", "archived"],
    "draft_generated": ["draft_reviewed"],
    "draft_reviewed": ["approved", "draft_generated"],
    "approved": ["sending"],
    "sending": ["sent", "failed"],
    "sent": ["delivered", "bounced", "failed"],
    "delivered": ["archived"],
    "bounced": ["draft_generated", "archived"],
    "failed": ["sending", "archived"],
    "archived": ["deleted"],
    "deleted": [],  # terminal
}

# All valid state names
VALID_STATES: frozenset[str] = frozenset(TRANSITIONS.keys())


class MailStateMachine:
    """13-state mail lifecycle engine.

    Pure logic — no I/O. All transitions produce immutable receipts.

    Usage:
        sm = MailStateMachine(mail_id="...", suite_id="...", office_id="...")
        receipt = sm.transition("triaged", actor="ava", correlation_id="...")
        assert sm.current_state == "triaged"
        assert len(sm.history) == 1
    """

    def __init__(
        self,
        mail_id: str,
        suite_id: str,
        office_id: str,
        *,
        initial_state: str = "received",
    ) -> None:
        if initial_state not in VALID_STATES:
            raise ValueError(f"Invalid initial state: {initial_state!r}")

        self._mail_id = mail_id
        self._suite_id = suite_id
        self._office_id = office_id
        self._current_state = initial_state
        self._history: list[MailTransitionReceipt] = []

    @property
    def mail_id(self) -> str:
        return self._mail_id

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
    def history(self) -> list[MailTransitionReceipt]:
        """Return a copy of the transition history (immutable access)."""
        return list(self._history)

    def can_transition(self, target: str) -> bool:
        """Check if a transition to the target state is valid from current state."""
        if target not in VALID_STATES:
            return False
        allowed = TRANSITIONS.get(self._current_state, [])
        return target in allowed

    def transition(
        self,
        new_state: str,
        *,
        actor: str,
        correlation_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> MailTransitionReceipt:
        """Execute a state transition with full receipt emission.

        Args:
            new_state: Target state to transition to.
            actor: Who is initiating the transition (user/system/agent ID).
            correlation_id: Trace ID for the request chain.
            metadata: Optional additional data for the receipt.

        Returns:
            MailTransitionReceipt with the transition details.

        Raises:
            InvalidTransitionError: If the transition is not allowed.
                The error includes a denial receipt (Law #3).
        """
        if metadata is None:
            metadata = {}

        now = MailTransitionReceipt.now()

        # Validate target is a known state
        if new_state not in VALID_STATES:
            denial = MailTransitionReceipt(
                mail_id=self._mail_id,
                suite_id=self._suite_id,
                office_id=self._office_id,
                from_state=self._current_state,
                to_state=new_state,
                receipt_type=MailReceiptType.MAIL_TRANSITION_DENIED,
                actor=actor,
                correlation_id=correlation_id,
                timestamp=now,
                metadata={
                    **metadata,
                    "reason": f"Unknown state: {new_state!r}",
                },
            )
            self._history.append(denial)
            raise InvalidTransitionError(
                f"Unknown state: {new_state!r}",
                denial_receipt=denial,
            )

        # Validate transition is allowed from current state
        allowed = TRANSITIONS.get(self._current_state, [])
        if new_state not in allowed:
            denial = MailTransitionReceipt(
                mail_id=self._mail_id,
                suite_id=self._suite_id,
                office_id=self._office_id,
                from_state=self._current_state,
                to_state=new_state,
                receipt_type=MailReceiptType.MAIL_TRANSITION_DENIED,
                actor=actor,
                correlation_id=correlation_id,
                timestamp=now,
                metadata={
                    **metadata,
                    "reason": f"Transition {self._current_state!r} -> {new_state!r} not allowed. "
                    f"Valid targets: {allowed}",
                },
            )
            self._history.append(denial)
            raise InvalidTransitionError(
                f"Cannot transition from {self._current_state!r} to {new_state!r}. "
                f"Allowed transitions: {allowed}",
                denial_receipt=denial,
            )

        # Execute transition
        receipt = MailTransitionReceipt(
            mail_id=self._mail_id,
            suite_id=self._suite_id,
            office_id=self._office_id,
            from_state=self._current_state,
            to_state=new_state,
            receipt_type=receipt_type_for_state(new_state),
            actor=actor,
            correlation_id=correlation_id,
            timestamp=now,
            metadata=metadata,
        )

        old_state = self._current_state
        self._current_state = new_state
        self._history.append(receipt)

        logger.info(
            "Mail %s transitioned %s -> %s (actor=%s, corr=%s)",
            self._mail_id[:8] if len(self._mail_id) > 8 else self._mail_id,
            old_state,
            new_state,
            actor,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )

        return receipt

    @property
    def is_terminal(self) -> bool:
        """Check if the current state is terminal (no further transitions)."""
        return len(TRANSITIONS.get(self._current_state, [])) == 0

    def get_valid_transitions(self) -> list[str]:
        """Return list of valid target states from the current state."""
        return list(TRANSITIONS.get(self._current_state, []))
