"""Accountant Mode — Read-only view for external auditors/accountants.

Provides a restricted interface that grants read access to financial data
without any write/execute permissions. Used by Teressa Books to share
data with external accounting firms.

Law compliance:
- Law #2: Every access produces a receipt (audit trail of who saw what)
- Law #3: Write attempts -> deny (fail-closed, read-only enforced)
- Law #5: Capability tokens scoped to read-only
- Law #6: Tenant isolation enforced
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

ACTOR_ACCOUNTANT_MODE = "service:accountant-mode"

# Session constraints
DEFAULT_SESSION_TTL_HOURS = 8
MAX_SESSION_TTL_HOURS = 24


# =============================================================================
# Enums and Constants
# =============================================================================


class AccountantScope(str, Enum):
    """Allowed read-only scopes for accountant sessions."""

    RECEIPT_READ = "RECEIPT_READ"
    INVOICE_READ = "INVOICE_READ"
    PAYMENT_READ = "PAYMENT_READ"
    TRANSACTION_READ = "TRANSACTION_READ"
    TAX_READ = "TAX_READ"


# Map data_type request strings to required scopes
_DATA_TYPE_SCOPE_MAP: dict[str, AccountantScope] = {
    "receipts": AccountantScope.RECEIPT_READ,
    "invoices": AccountantScope.INVOICE_READ,
    "payments": AccountantScope.PAYMENT_READ,
    "transactions": AccountantScope.TRANSACTION_READ,
    "tax_summaries": AccountantScope.TAX_READ,
}

# Valid data types
VALID_DATA_TYPES = frozenset(_DATA_TYPE_SCOPE_MAP.keys())


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class AccountantSession:
    """An active accountant session with read-only access."""

    session_id: str
    suite_id: str
    office_id: str
    accountant_id: str
    firm_name: str
    scopes: list[AccountantScope]
    created_at: str  # ISO 8601
    expires_at: str  # ISO 8601
    active: bool = True


@dataclass(frozen=True)
class AccountantAccessResult:
    """Result of an accountant data access request."""

    allowed: bool
    data_type: str
    data: dict[str, Any]
    receipt: dict[str, Any]
    deny_reason: str = ""


@dataclass(frozen=True)
class TransitionReceipt:
    """Receipt for session lifecycle transitions (create/end)."""

    receipt: dict[str, Any]
    session_id: str
    action: str  # create, end


# =============================================================================
# In-Memory Session Store (Phase 2 — moves to DB later)
# =============================================================================

_active_sessions: dict[str, AccountantSession] = {}


def _clear_sessions() -> None:
    """Clear all sessions. For testing only."""
    _active_sessions.clear()


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
    """Build a receipt for an accountant mode operation (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_version": "1.0",
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": f"accountant_mode.{action_type}",
        "suite_id": suite_id,
        "office_id": office_id,
        "actor": ACTOR_ACCOUNTANT_MODE,
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


def create_session(
    *,
    suite_id: str,
    office_id: str,
    accountant_id: str,
    firm_name: str,
    scopes: list[str],
    ttl_hours: int = DEFAULT_SESSION_TTL_HOURS,
    correlation_id: str,
) -> TransitionReceipt:
    """Create a new accountant session with read-only access.

    Law #2: Session creation produces a receipt.
    Law #3: Invalid scopes -> deny (fail-closed).
    Law #5: Session is scoped to specific read operations.
    Law #6: Scoped to suite_id/office_id.

    Args:
        suite_id: Tenant suite ID.
        office_id: Tenant office ID.
        accountant_id: Identifier for the external accountant.
        firm_name: Name of the accounting firm.
        scopes: List of scope strings (must be valid AccountantScope values).
        ttl_hours: Session duration in hours (default 8, max 24).
        correlation_id: Trace ID for the operation.

    Returns:
        TransitionReceipt with session details and receipt.
    """
    # Validate TTL bounds
    if ttl_hours <= 0 or ttl_hours > MAX_SESSION_TTL_HOURS:
        receipt = _build_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="session.create",
            outcome="denied",
            reason_code="INVALID_TTL",
            details={
                "requested_ttl_hours": ttl_hours,
                "max_ttl_hours": MAX_SESSION_TTL_HOURS,
            },
        )
        return TransitionReceipt(
            receipt=receipt,
            session_id="",
            action="create",
        )

    # Validate scopes — fail-closed on invalid scopes (Law #3)
    validated_scopes: list[AccountantScope] = []
    for scope_str in scopes:
        try:
            validated_scopes.append(AccountantScope(scope_str))
        except ValueError:
            receipt = _build_receipt(
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                action_type="session.create",
                outcome="denied",
                reason_code="INVALID_SCOPE",
                details={"invalid_scope": scope_str, "valid_scopes": [s.value for s in AccountantScope]},
            )
            return TransitionReceipt(
                receipt=receipt,
                session_id="",
                action="create",
            )

    if not validated_scopes:
        receipt = _build_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="session.create",
            outcome="denied",
            reason_code="EMPTY_SCOPES",
            details={"error": "At least one scope is required"},
        )
        return TransitionReceipt(
            receipt=receipt,
            session_id="",
            action="create",
        )

    # Create session
    now = datetime.now(timezone.utc)
    session_id = str(uuid.uuid4())
    expires_at = now + timedelta(hours=ttl_hours)

    session = AccountantSession(
        session_id=session_id,
        suite_id=suite_id,
        office_id=office_id,
        accountant_id=accountant_id,
        firm_name=firm_name,
        scopes=validated_scopes,
        created_at=now.isoformat(),
        expires_at=expires_at.isoformat(),
        active=True,
    )

    _active_sessions[session_id] = session

    receipt = _build_receipt(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        action_type="session.create",
        outcome="success",
        details={
            "session_id": session_id,
            "accountant_id": accountant_id,
            "firm_name": firm_name,
            "scopes": [s.value for s in validated_scopes],
            "ttl_hours": ttl_hours,
            "expires_at": expires_at.isoformat(),
        },
    )

    logger.info(
        "Accountant session created: session=%s, firm=%s, scopes=%d, ttl=%dh, suite=%s",
        session_id[:8], firm_name, len(validated_scopes), ttl_hours,
        suite_id[:8] if len(suite_id) > 8 else suite_id,
    )

    return TransitionReceipt(
        receipt=receipt,
        session_id=session_id,
        action="create",
    )


def validate_session(
    session_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Check whether a session is valid and not expired.

    Args:
        session_id: The session to validate.
        now: Override current time (for testing).

    Returns:
        True if session is active and not expired.
    """
    session = _active_sessions.get(session_id)
    if session is None:
        return False
    if not session.active:
        return False

    check_time = now or datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(session.expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if check_time > expires_at:
        session.active = False
        return False

    return True


def access_data(
    session_id: str,
    data_type: str,
    *,
    correlation_id: str,
    now: datetime | None = None,
    data_provider: Any | None = None,
) -> AccountantAccessResult:
    """Access financial data through an accountant session.

    Law #2: Every access produces a receipt (who saw what).
    Law #3: Write attempts denied. Invalid session denied. Invalid data_type denied.
    Law #6: Data scoped to session's suite_id/office_id.

    Args:
        session_id: The accountant session ID.
        data_type: Type of data to access (receipts, invoices, payments, transactions, tax_summaries).
        correlation_id: Trace ID for the operation.
        now: Override current time (for testing).
        data_provider: Optional callable/object that provides actual data. If None, returns placeholder.

    Returns:
        AccountantAccessResult with data and receipt.
    """
    session = _active_sessions.get(session_id)

    # Session not found — fail-closed (Law #3)
    if session is None:
        receipt = _build_receipt(
            suite_id="unknown",
            office_id="unknown",
            correlation_id=correlation_id,
            action_type="data.access",
            outcome="denied",
            reason_code="SESSION_NOT_FOUND",
            details={"session_id": session_id, "data_type": data_type},
        )
        return AccountantAccessResult(
            allowed=False,
            data_type=data_type,
            data={},
            receipt=receipt,
            deny_reason="Session not found",
        )

    # Session expired — fail-closed (Law #3)
    if not validate_session(session_id, now=now):
        receipt = _build_receipt(
            suite_id=session.suite_id,
            office_id=session.office_id,
            correlation_id=correlation_id,
            action_type="data.access",
            outcome="denied",
            reason_code="SESSION_EXPIRED",
            details={"session_id": session_id, "data_type": data_type},
        )
        return AccountantAccessResult(
            allowed=False,
            data_type=data_type,
            data={},
            receipt=receipt,
            deny_reason="Session expired",
        )

    # Invalid data_type — fail-closed (Law #3)
    if data_type not in VALID_DATA_TYPES:
        receipt = _build_receipt(
            suite_id=session.suite_id,
            office_id=session.office_id,
            correlation_id=correlation_id,
            action_type="data.access",
            outcome="denied",
            reason_code="INVALID_DATA_TYPE",
            details={
                "session_id": session_id,
                "data_type": data_type,
                "valid_types": sorted(VALID_DATA_TYPES),
            },
        )
        return AccountantAccessResult(
            allowed=False,
            data_type=data_type,
            data={},
            receipt=receipt,
            deny_reason=f"Invalid data type: {data_type}",
        )

    # Scope check — fail-closed (Law #3)
    required_scope = _DATA_TYPE_SCOPE_MAP[data_type]
    if required_scope not in session.scopes:
        receipt = _build_receipt(
            suite_id=session.suite_id,
            office_id=session.office_id,
            correlation_id=correlation_id,
            action_type="data.access",
            outcome="denied",
            reason_code="SCOPE_INSUFFICIENT",
            details={
                "session_id": session_id,
                "data_type": data_type,
                "required_scope": required_scope.value,
                "session_scopes": [s.value for s in session.scopes],
            },
        )
        return AccountantAccessResult(
            allowed=False,
            data_type=data_type,
            data={},
            receipt=receipt,
            deny_reason=f"Scope {required_scope.value} not granted",
        )

    # Access granted — produce data + receipt (Law #2)
    data = {
        "data_type": data_type,
        "suite_id": session.suite_id,
        "office_id": session.office_id,
        "accessed_at": datetime.now(timezone.utc).isoformat(),
        "records": [],  # Placeholder — real data from provider
    }

    receipt = _build_receipt(
        suite_id=session.suite_id,
        office_id=session.office_id,
        correlation_id=correlation_id,
        action_type="data.access",
        outcome="success",
        details={
            "session_id": session_id,
            "data_type": data_type,
            "accountant_id": session.accountant_id,
            "firm_name": session.firm_name,
        },
    )

    logger.info(
        "Accountant data access: session=%s, type=%s, firm=%s, suite=%s",
        session_id[:8], data_type, session.firm_name,
        session.suite_id[:8] if len(session.suite_id) > 8 else session.suite_id,
    )

    return AccountantAccessResult(
        allowed=True,
        data_type=data_type,
        data=data,
        receipt=receipt,
    )


def attempt_write(
    session_id: str,
    *,
    correlation_id: str,
    operation: str = "write",
) -> AccountantAccessResult:
    """Explicitly deny any write attempt through an accountant session.

    Law #3: Write operations are always denied (fail-closed, read-only enforced).
    Law #2: Denial produces a receipt.

    Args:
        session_id: The accountant session ID.
        correlation_id: Trace ID for the operation.
        operation: Description of the attempted write operation.

    Returns:
        AccountantAccessResult with denied=True and receipt.
    """
    session = _active_sessions.get(session_id)
    suite_id = session.suite_id if session else "unknown"
    office_id = session.office_id if session else "unknown"

    receipt = _build_receipt(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        action_type="data.write_attempt",
        outcome="denied",
        reason_code="WRITE_DENIED",
        details={
            "session_id": session_id,
            "attempted_operation": operation,
            "message": "Accountant mode is read-only. Write operations are not permitted.",
        },
    )

    logger.warning(
        "Accountant write DENIED: session=%s, operation=%s, suite=%s",
        session_id[:8] if session_id else "none", operation,
        suite_id[:8] if len(suite_id) > 8 else suite_id,
    )

    return AccountantAccessResult(
        allowed=False,
        data_type="write",
        data={},
        receipt=receipt,
        deny_reason="Accountant mode is read-only. Write operations are not permitted.",
    )


def end_session(
    session_id: str,
    *,
    correlation_id: str,
) -> TransitionReceipt:
    """End an accountant session.

    Law #2: Session termination produces a receipt.

    Args:
        session_id: The session to end.
        correlation_id: Trace ID for the operation.

    Returns:
        TransitionReceipt with session end details.
    """
    session = _active_sessions.get(session_id)

    if session is None:
        receipt = _build_receipt(
            suite_id="unknown",
            office_id="unknown",
            correlation_id=correlation_id,
            action_type="session.end",
            outcome="denied",
            reason_code="SESSION_NOT_FOUND",
            details={"session_id": session_id},
        )
        return TransitionReceipt(
            receipt=receipt,
            session_id=session_id,
            action="end",
        )

    session.active = False

    receipt = _build_receipt(
        suite_id=session.suite_id,
        office_id=session.office_id,
        correlation_id=correlation_id,
        action_type="session.end",
        outcome="success",
        details={
            "session_id": session_id,
            "accountant_id": session.accountant_id,
            "firm_name": session.firm_name,
            "created_at": session.created_at,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    logger.info(
        "Accountant session ended: session=%s, firm=%s, suite=%s",
        session_id[:8], session.firm_name,
        session.suite_id[:8] if len(session.suite_id) > 8 else session.suite_id,
    )

    return TransitionReceipt(
        receipt=receipt,
        session_id=session_id,
        action="end",
    )
