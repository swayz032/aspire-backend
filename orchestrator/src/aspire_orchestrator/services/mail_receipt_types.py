"""Mail Receipt Types — 16 receipt types for the Eli 13-state mail machine.

Each mail state transition produces an immutable receipt (Law #2).
Invalid transitions produce denial receipts (Law #3: fail-closed).

Receipt types:
  - 13 state receipts (one per state)
  - 3 meta receipts (transition_denied, dlp_redacted, retry)

No UPDATE/DELETE on mail receipts — corrections are new receipts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MailReceiptType(str, Enum):
    """Receipt types for the mail state machine.

    Each state transition maps to one receipt type.
    Meta types cover governance events (denial, DLP, retries).
    """

    # State transition receipts (one per state)
    MAIL_RECEIVED = "mail.received"
    MAIL_TRIAGED = "mail.triaged"
    MAIL_CLASSIFIED = "mail.classified"
    MAIL_DRAFT_GENERATED = "mail.draft_generated"
    MAIL_DRAFT_REVIEWED = "mail.draft_reviewed"
    MAIL_APPROVED = "mail.approved"
    MAIL_SENDING = "mail.sending"
    MAIL_SENT = "mail.sent"
    MAIL_DELIVERED = "mail.delivered"
    MAIL_BOUNCED = "mail.bounced"
    MAIL_FAILED = "mail.failed"
    MAIL_ARCHIVED = "mail.archived"
    MAIL_DELETED = "mail.deleted"

    # Meta types — governance events
    MAIL_TRANSITION_DENIED = "mail.transition_denied"
    MAIL_DLP_REDACTED = "mail.dlp_redacted"
    MAIL_RETRY = "mail.retry"


# State name → receipt type mapping
_STATE_TO_RECEIPT: dict[str, MailReceiptType] = {
    "received": MailReceiptType.MAIL_RECEIVED,
    "triaged": MailReceiptType.MAIL_TRIAGED,
    "classified": MailReceiptType.MAIL_CLASSIFIED,
    "draft_generated": MailReceiptType.MAIL_DRAFT_GENERATED,
    "draft_reviewed": MailReceiptType.MAIL_DRAFT_REVIEWED,
    "approved": MailReceiptType.MAIL_APPROVED,
    "sending": MailReceiptType.MAIL_SENDING,
    "sent": MailReceiptType.MAIL_SENT,
    "delivered": MailReceiptType.MAIL_DELIVERED,
    "bounced": MailReceiptType.MAIL_BOUNCED,
    "failed": MailReceiptType.MAIL_FAILED,
    "archived": MailReceiptType.MAIL_ARCHIVED,
    "deleted": MailReceiptType.MAIL_DELETED,
}


def receipt_type_for_state(state: str) -> MailReceiptType:
    """Map a state name to its corresponding receipt type.

    Raises KeyError if state is not a valid mail state.
    """
    return _STATE_TO_RECEIPT[state]


@dataclass(frozen=True)
class MailTransitionReceipt:
    """Immutable receipt for a single mail state transition (Law #2).

    These are append-only. No UPDATE/DELETE. Corrections are new receipts.

    Fields match the minimum receipt spec from CLAUDE.md Law #2:
      - correlation_id / trace_id
      - actor (user/system) + suite_id + office_id
      - action_type + risk_tier
      - timestamps
      - outcome + reason_code
    """

    mail_id: str
    suite_id: str
    office_id: str
    from_state: str
    to_state: str
    receipt_type: MailReceiptType
    actor: str
    correlation_id: str
    timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def now() -> datetime:
        """UTC timestamp factory for consistency."""
        return datetime.now(timezone.utc)
