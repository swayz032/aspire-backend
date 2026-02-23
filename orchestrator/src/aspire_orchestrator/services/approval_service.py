"""Approval Binding Service — Approve-then-swap defense (Law #4).

Per approval_binding_spec.md:
  - payload_hash = SHA-256 of canonical JSON of the exact execution payload
  - Approval must bind to: suite_id, office_id, request_id, payload_hash, policy_version
  - Replay defense:
    - Reject approvals with mismatched payload_hash
    - Reject expired approvals
    - Reject approvals reused across different request_id
  - Prevents the "approve then swap payload" attack

Per CLAUDE.md Law #4:
  - YELLOW: requires explicit user confirmation
  - RED: requires explicit authority + strong confirmation UX
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# Default approval expiry — 5 minutes
DEFAULT_APPROVAL_EXPIRY_SECONDS = 300

# Policy version for binding (incremented when policy matrix changes)
CURRENT_POLICY_VERSION = "1.0.0"

# In-memory used request_id set (Phase 1 — moves to DB in Phase 2)
_used_approval_request_ids: set[str] = set()


class ApprovalBindingError(Enum):
    """Enumeration of approval binding failure modes."""

    PAYLOAD_HASH_MISMATCH = "PAYLOAD_HASH_MISMATCH"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    REQUEST_ID_REUSED = "REQUEST_ID_REUSED"
    SUITE_MISMATCH = "SUITE_MISMATCH"
    OFFICE_MISMATCH = "OFFICE_MISMATCH"
    POLICY_VERSION_MISMATCH = "POLICY_VERSION_MISMATCH"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    APPROVER_NOT_AUTHORIZED = "APPROVER_NOT_AUTHORIZED"


@dataclass(frozen=True)
class ApprovalBindingResult:
    """Result of approval binding verification."""

    valid: bool
    error: ApprovalBindingError | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ApprovalBinding:
    """Represents a bound approval — ties approval to exact execution context.

    Per approval_binding_spec.md, approval must bind to:
    - suite_id, office_id (tenant context)
    - request_id (idempotency)
    - payload_hash (execution content)
    - policy_version (prevent policy downgrade attacks)
    """

    suite_id: str
    office_id: str
    request_id: str
    payload_hash: str
    policy_version: str
    approved_at: datetime
    expires_at: datetime
    approver_id: str


def compute_payload_hash(payload: dict[str, Any]) -> str:
    """Compute SHA-256 of canonical JSON for approval binding.

    Per approval_binding_spec.md:
    - SHA-256 of canonical JSON of the exact execution payload
    - Canonical JSON: UTF-8, keys sorted, no whitespace
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_approval_binding(
    *,
    suite_id: str,
    office_id: str,
    request_id: str,
    payload: dict[str, Any],
    approver_id: str,
    expiry_seconds: int = DEFAULT_APPROVAL_EXPIRY_SECONDS,
) -> ApprovalBinding:
    """Create an approval binding for a YELLOW/RED tier action.

    Returns an ApprovalBinding that ties the approval to the exact execution context.
    """
    now = datetime.now(timezone.utc)
    return ApprovalBinding(
        suite_id=suite_id,
        office_id=office_id,
        request_id=request_id,
        payload_hash=compute_payload_hash(payload),
        policy_version=CURRENT_POLICY_VERSION,
        approved_at=now,
        expires_at=now + timedelta(seconds=expiry_seconds),
        approver_id=approver_id,
    )


def verify_approval_binding(
    binding: ApprovalBinding,
    *,
    expected_suite_id: str,
    expected_office_id: str,
    expected_request_id: str,
    expected_payload_hash: str,
    now: datetime | None = None,
) -> ApprovalBindingResult:
    """Verify an approval binding against the execution context.

    Per approval_binding_spec.md replay defense:
    1. Reject approvals with mismatched payload_hash
    2. Reject expired approvals
    3. Reject approvals reused across different request_id
    4. Verify suite_id matches
    5. Verify office_id matches
    6. Verify policy_version matches
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # CHECK 1: payload_hash matches (approve-then-swap defense)
    # Use timing-safe comparison to prevent side-channel brute-force
    if not secrets.compare_digest(binding.payload_hash, expected_payload_hash):
        logger.warning(
            "Approval binding REJECTED: payload_hash mismatch, "
            "binding=%s, expected=%s, request_id=%s",
            binding.payload_hash[:16], expected_payload_hash[:16], expected_request_id[:8],
        )
        return ApprovalBindingResult(
            valid=False,
            error=ApprovalBindingError.PAYLOAD_HASH_MISMATCH,
            error_message="Approval payload_hash does not match execution payload. "
            "This may indicate an approve-then-swap attack.",
        )

    # CHECK 2: Not expired
    if now > binding.expires_at:
        logger.warning(
            "Approval binding REJECTED: expired at %s, now %s, request_id=%s",
            binding.expires_at.isoformat(), now.isoformat(), expected_request_id[:8],
        )
        return ApprovalBindingResult(
            valid=False,
            error=ApprovalBindingError.APPROVAL_EXPIRED,
            error_message=f"Approval expired at {binding.expires_at.isoformat()}",
        )

    # CHECK 3: request_id not reused (replay defense)
    approval_key = f"{binding.suite_id}:{binding.request_id}"
    if approval_key in _used_approval_request_ids:
        logger.warning(
            "Approval binding REJECTED: request_id reused, request_id=%s, suite=%s",
            binding.request_id[:8], binding.suite_id[:8],
        )
        return ApprovalBindingResult(
            valid=False,
            error=ApprovalBindingError.REQUEST_ID_REUSED,
            error_message="This request_id has already been used for an approval. "
            "Each approval is single-use.",
        )

    # CHECK 4: suite_id matches
    if binding.suite_id != expected_suite_id:
        logger.warning(
            "Approval binding REJECTED: suite_id mismatch, binding=%s, expected=%s",
            binding.suite_id[:8], expected_suite_id[:8],
        )
        return ApprovalBindingResult(
            valid=False,
            error=ApprovalBindingError.SUITE_MISMATCH,
            error_message="Approval suite_id does not match execution context",
        )

    # CHECK 5: office_id matches
    if binding.office_id != expected_office_id:
        logger.warning(
            "Approval binding REJECTED: office_id mismatch, binding=%s, expected=%s",
            binding.office_id[:8], expected_office_id[:8],
        )
        return ApprovalBindingResult(
            valid=False,
            error=ApprovalBindingError.OFFICE_MISMATCH,
            error_message="Approval office_id does not match execution context",
        )

    # CHECK 6: policy_version matches
    if binding.policy_version != CURRENT_POLICY_VERSION:
        logger.warning(
            "Approval binding REJECTED: policy_version mismatch, binding=%s, current=%s",
            binding.policy_version, CURRENT_POLICY_VERSION,
        )
        return ApprovalBindingResult(
            valid=False,
            error=ApprovalBindingError.POLICY_VERSION_MISMATCH,
            error_message="Approval was granted under a different policy version. "
            "Re-approval required.",
        )

    # CHECK 7: request_id matches
    if binding.request_id != expected_request_id:
        logger.warning(
            "Approval binding REJECTED: request_id mismatch, binding=%s, expected=%s",
            binding.request_id[:8], expected_request_id[:8],
        )
        return ApprovalBindingResult(
            valid=False,
            error=ApprovalBindingError.REQUEST_ID_REUSED,
            error_message="Approval request_id does not match execution request_id",
        )

    # All checks passed — mark request_id as used
    _used_approval_request_ids.add(approval_key)

    logger.info(
        "Approval binding VERIFIED: request_id=%s, suite=%s, approver=%s",
        binding.request_id[:8], binding.suite_id[:8], binding.approver_id[:8],
    )
    return ApprovalBindingResult(valid=True)


def clear_used_request_ids() -> None:
    """Clear the in-memory used request_id set. For testing only."""
    _used_approval_request_ids.clear()
