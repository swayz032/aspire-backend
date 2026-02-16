"""Dual Approval Service — RED-tier operations require two independent approvals.

For RED-tier operations (payments, payroll, contract signing), a single
approval is insufficient. Two independent approvals are required:
  - No same-approver (two different humans must approve)
  - Timeout: 24h (approvals expire if not both completed within window)
  - Receipt for every approval/denial/expiry event

Used by: Finn Money Desk (transfer_funds), Milo Payroll (run_payroll).

Law compliance:
  - Law #2: Every approval event emits a receipt
  - Law #3: Fail closed on expired, same-approver, or missing approval
  - Law #4: Only enforced for RED-tier operations
  - Law #6: Suite-scoped — approvals are tenant-isolated
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

APPROVAL_TIMEOUT_HOURS = 24
RECEIPT_VERSION = "1.0"


class ApprovalStatus(str, Enum):
    """Status of a dual approval request."""

    PENDING = "pending"
    FIRST_APPROVED = "first_approved"
    FULLY_APPROVED = "fully_approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass
class ApprovalRecord:
    """Single approval within a dual approval flow."""

    approver_id: str
    approver_role: str  # e.g., "owner", "accountant", "hr", "finance"
    approved: bool
    timestamp: str
    evidence_hash: str


@dataclass
class DualApprovalRequest:
    """A request requiring two independent approvals."""

    request_id: str
    suite_id: str
    office_id: str
    correlation_id: str
    action_type: str
    risk_tier: str
    binding_fields: dict[str, Any]
    binding_hash: str
    required_roles: list[str]  # e.g., ["owner", "accountant"]
    status: ApprovalStatus = ApprovalStatus.PENDING
    approvals: list[ApprovalRecord] = field(default_factory=list)
    denials: list[ApprovalRecord] = field(default_factory=list)
    created_at: str = ""
    expires_at: str = ""


@dataclass
class DualApprovalResult:
    """Result of a dual approval operation."""

    success: bool
    status: ApprovalStatus
    request_id: str
    error: str | None = None
    receipt: dict[str, Any] = field(default_factory=dict)
    fully_approved: bool = False
    remaining_roles: list[str] = field(default_factory=list)


def _compute_hash(data: dict[str, Any]) -> str:
    """Compute SHA256 hash for binding verification."""
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _build_receipt(
    *,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    event_type: str,
    status: str,
    inputs: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for dual approval events (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor": "service:dual-approval",
        "correlation_id": correlation_id,
        "status": status,
        "inputs_hash": _compute_hash(inputs),
        "policy": {
            "decision": "allow" if status == "ok" else "deny",
            "policy_id": "dual-approval-v1",
            "reasons": [],
        },
        "redactions": [],
    }
    if metadata:
        receipt["metadata"] = metadata
    return receipt


class DualApprovalService:
    """Thread-safe dual approval service for RED-tier operations.

    Lifecycle:
      1. create_request() — initiates a dual approval flow
      2. submit_approval() — records one approval (need two from different roles)
      3. check_status() — returns current approval state
      4. submit_denial() — records denial (terminates the flow)

    Constraints:
      - No same-approver: two different approver_ids required
      - No same-role: approvals from two different roles required
      - Timeout: request expires after APPROVAL_TIMEOUT_HOURS
      - Binding hash: binding fields are locked at creation, verified at approval
    """

    def __init__(self) -> None:
        self._requests: dict[str, DualApprovalRequest] = {}
        self._lock = threading.Lock()

    def clear_store(self) -> None:
        """Clear all approval requests. Testing only."""
        with self._lock:
            self._requests.clear()

    def create_request(
        self,
        *,
        suite_id: str,
        office_id: str,
        correlation_id: str,
        action_type: str,
        binding_fields: dict[str, Any],
        required_roles: list[str],
    ) -> DualApprovalResult:
        """Create a new dual approval request.

        Args:
            suite_id: Tenant suite ID (Law #6)
            office_id: Office ID within the suite
            correlation_id: Trace correlation ID
            action_type: The action requiring approval (e.g., "payment.transfer")
            binding_fields: Fields that are locked for approval verification
            required_roles: Roles that must approve (e.g., ["owner", "accountant"])

        Returns:
            DualApprovalResult with the created request_id
        """
        if len(required_roles) < 2:
            receipt = _build_receipt(
                suite_id=suite_id, office_id=office_id,
                correlation_id=correlation_id,
                event_type="approval.dual.create",
                status="denied",
                inputs={"action": action_type, "roles": required_roles},
            )
            receipt["policy"]["reasons"] = ["INSUFFICIENT_REQUIRED_ROLES"]
            return DualApprovalResult(
                success=False,
                status=ApprovalStatus.DENIED,
                request_id="",
                error="Dual approval requires at least 2 required roles",
                receipt=receipt,
            )

        now = datetime.now(timezone.utc)
        request_id = str(uuid.uuid4())
        binding_hash = _compute_hash(binding_fields)

        request = DualApprovalRequest(
            request_id=request_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type=action_type,
            risk_tier="red",
            binding_fields=binding_fields,
            binding_hash=binding_hash,
            required_roles=required_roles,
            status=ApprovalStatus.PENDING,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(hours=APPROVAL_TIMEOUT_HOURS)).isoformat(),
        )

        with self._lock:
            self._requests[request_id] = request

        receipt = _build_receipt(
            suite_id=suite_id, office_id=office_id,
            correlation_id=correlation_id,
            event_type="approval.dual.create",
            status="ok",
            inputs={"action": action_type, "roles": required_roles},
            metadata={
                "request_id": request_id,
                "binding_hash": binding_hash,
                "expires_at": request.expires_at,
                "required_roles": required_roles,
            },
        )

        return DualApprovalResult(
            success=True,
            status=ApprovalStatus.PENDING,
            request_id=request_id,
            receipt=receipt,
            remaining_roles=list(required_roles),
        )

    def submit_approval(
        self,
        *,
        request_id: str,
        approver_id: str,
        approver_role: str,
        binding_fields: dict[str, Any],
        suite_id: str,
    ) -> DualApprovalResult:
        """Submit an approval for a dual approval request.

        Enforces:
          - Request exists and belongs to suite (Law #6)
          - Request is not expired (Law #3)
          - No same-approver (different approver_id required)
          - No same-role (different role required)
          - Binding hash matches (approve-then-swap defense)

        Returns:
            DualApprovalResult with updated status
        """
        with self._lock:
            request = self._requests.get(request_id)

            if not request:
                receipt = _build_receipt(
                    suite_id=suite_id, office_id="",
                    correlation_id="",
                    event_type="approval.dual.submit",
                    status="denied",
                    inputs={"request_id": request_id, "approver_id": approver_id},
                )
                receipt["policy"]["reasons"] = ["REQUEST_NOT_FOUND"]
                return DualApprovalResult(
                    success=False,
                    status=ApprovalStatus.DENIED,
                    request_id=request_id,
                    error="Approval request not found",
                    receipt=receipt,
                )

            # Tenant isolation check (Law #6)
            if request.suite_id != suite_id:
                receipt = _build_receipt(
                    suite_id=suite_id, office_id="",
                    correlation_id=request.correlation_id,
                    event_type="approval.dual.submit",
                    status="denied",
                    inputs={"request_id": request_id, "approver_id": approver_id},
                )
                receipt["policy"]["reasons"] = ["CROSS_TENANT_VIOLATION"]
                return DualApprovalResult(
                    success=False,
                    status=ApprovalStatus.DENIED,
                    request_id=request_id,
                    error="Cross-tenant approval attempt denied (Law #6)",
                    receipt=receipt,
                )

            # Check expiration (Law #3: fail closed)
            now = datetime.now(timezone.utc)
            expires_at = datetime.fromisoformat(request.expires_at)
            if now > expires_at:
                request.status = ApprovalStatus.EXPIRED
                receipt = _build_receipt(
                    suite_id=request.suite_id, office_id=request.office_id,
                    correlation_id=request.correlation_id,
                    event_type="approval.dual.expired",
                    status="denied",
                    inputs={"request_id": request_id, "approver_id": approver_id},
                    metadata={"expired_at": request.expires_at},
                )
                receipt["policy"]["reasons"] = ["REQUEST_EXPIRED"]
                return DualApprovalResult(
                    success=False,
                    status=ApprovalStatus.EXPIRED,
                    request_id=request_id,
                    error=f"Approval request expired at {request.expires_at}",
                    receipt=receipt,
                )

            # Check if already fully approved or denied
            if request.status in (ApprovalStatus.FULLY_APPROVED, ApprovalStatus.DENIED):
                receipt = _build_receipt(
                    suite_id=request.suite_id, office_id=request.office_id,
                    correlation_id=request.correlation_id,
                    event_type="approval.dual.submit",
                    status="denied",
                    inputs={"request_id": request_id, "approver_id": approver_id},
                )
                receipt["policy"]["reasons"] = ["REQUEST_ALREADY_RESOLVED"]
                return DualApprovalResult(
                    success=False,
                    status=request.status,
                    request_id=request_id,
                    error=f"Request already {request.status.value}",
                    receipt=receipt,
                )

            # No same-approver check
            existing_approver_ids = {a.approver_id for a in request.approvals}
            if approver_id in existing_approver_ids:
                receipt = _build_receipt(
                    suite_id=request.suite_id, office_id=request.office_id,
                    correlation_id=request.correlation_id,
                    event_type="approval.dual.submit",
                    status="denied",
                    inputs={"request_id": request_id, "approver_id": approver_id},
                )
                receipt["policy"]["reasons"] = ["SAME_APPROVER_VIOLATION"]
                return DualApprovalResult(
                    success=False,
                    status=request.status,
                    request_id=request_id,
                    error="Same approver cannot approve twice (dual approval requires two different approvers)",
                    receipt=receipt,
                )

            # No same-role check
            existing_roles = {a.approver_role for a in request.approvals}
            if approver_role in existing_roles:
                receipt = _build_receipt(
                    suite_id=request.suite_id, office_id=request.office_id,
                    correlation_id=request.correlation_id,
                    event_type="approval.dual.submit",
                    status="denied",
                    inputs={"request_id": request_id, "approver_id": approver_id, "role": approver_role},
                )
                receipt["policy"]["reasons"] = ["SAME_ROLE_VIOLATION"]
                return DualApprovalResult(
                    success=False,
                    status=request.status,
                    request_id=request_id,
                    error=f"Role '{approver_role}' has already approved. A different role is required.",
                    receipt=receipt,
                )

            # Role must be in required_roles
            if approver_role not in request.required_roles:
                receipt = _build_receipt(
                    suite_id=request.suite_id, office_id=request.office_id,
                    correlation_id=request.correlation_id,
                    event_type="approval.dual.submit",
                    status="denied",
                    inputs={"request_id": request_id, "approver_id": approver_id, "role": approver_role},
                )
                receipt["policy"]["reasons"] = ["UNAUTHORIZED_ROLE"]
                return DualApprovalResult(
                    success=False,
                    status=request.status,
                    request_id=request_id,
                    error=f"Role '{approver_role}' not authorized. Required: {request.required_roles}",
                    receipt=receipt,
                )

            # Verify binding hash (approve-then-swap defense)
            submitted_hash = _compute_hash(binding_fields)
            if submitted_hash != request.binding_hash:
                receipt = _build_receipt(
                    suite_id=request.suite_id, office_id=request.office_id,
                    correlation_id=request.correlation_id,
                    event_type="approval.dual.submit",
                    status="denied",
                    inputs={"request_id": request_id, "approver_id": approver_id},
                    metadata={"expected_hash": request.binding_hash, "submitted_hash": submitted_hash},
                )
                receipt["policy"]["reasons"] = ["BINDING_HASH_MISMATCH"]
                return DualApprovalResult(
                    success=False,
                    status=request.status,
                    request_id=request_id,
                    error="Binding fields have changed since request creation (approve-then-swap detected)",
                    receipt=receipt,
                )

            # Record the approval
            evidence_hash = _compute_hash({
                "approver_id": approver_id,
                "approver_role": approver_role,
                "request_id": request_id,
                "binding_hash": request.binding_hash,
                "timestamp": now.isoformat(),
            })

            record = ApprovalRecord(
                approver_id=approver_id,
                approver_role=approver_role,
                approved=True,
                timestamp=now.isoformat(),
                evidence_hash=evidence_hash,
            )
            request.approvals.append(record)

            # Check if fully approved
            approved_roles = {a.approver_role for a in request.approvals}
            remaining = [r for r in request.required_roles if r not in approved_roles]

            if len(remaining) == 0:
                request.status = ApprovalStatus.FULLY_APPROVED
                event_type = "approval.dual.fully_approved"
                status_str = "ok"
            else:
                request.status = ApprovalStatus.FIRST_APPROVED
                event_type = "approval.dual.first_approved"
                status_str = "ok"

            receipt = _build_receipt(
                suite_id=request.suite_id, office_id=request.office_id,
                correlation_id=request.correlation_id,
                event_type=event_type,
                status=status_str,
                inputs={
                    "request_id": request_id,
                    "approver_id": approver_id,
                    "approver_role": approver_role,
                },
                metadata={
                    "request_id": request_id,
                    "evidence_hash": evidence_hash,
                    "approval_count": len(request.approvals),
                    "remaining_roles": remaining,
                    "fully_approved": request.status == ApprovalStatus.FULLY_APPROVED,
                },
            )

            return DualApprovalResult(
                success=True,
                status=request.status,
                request_id=request_id,
                receipt=receipt,
                fully_approved=request.status == ApprovalStatus.FULLY_APPROVED,
                remaining_roles=remaining,
            )

    def submit_denial(
        self,
        *,
        request_id: str,
        denier_id: str,
        denier_role: str,
        reason: str,
        suite_id: str,
    ) -> DualApprovalResult:
        """Deny a dual approval request. Terminates the flow immediately."""
        with self._lock:
            request = self._requests.get(request_id)

            if not request:
                receipt = _build_receipt(
                    suite_id=suite_id, office_id="",
                    correlation_id="",
                    event_type="approval.dual.deny",
                    status="denied",
                    inputs={"request_id": request_id, "denier_id": denier_id},
                )
                receipt["policy"]["reasons"] = ["REQUEST_NOT_FOUND"]
                return DualApprovalResult(
                    success=False,
                    status=ApprovalStatus.DENIED,
                    request_id=request_id,
                    error="Approval request not found",
                    receipt=receipt,
                )

            if request.suite_id != suite_id:
                receipt = _build_receipt(
                    suite_id=suite_id, office_id="",
                    correlation_id=request.correlation_id,
                    event_type="approval.dual.deny",
                    status="denied",
                    inputs={"request_id": request_id, "denier_id": denier_id},
                )
                receipt["policy"]["reasons"] = ["CROSS_TENANT_VIOLATION"]
                return DualApprovalResult(
                    success=False,
                    status=ApprovalStatus.DENIED,
                    request_id=request_id,
                    error="Cross-tenant denial attempt denied (Law #6)",
                    receipt=receipt,
                )

            record = ApprovalRecord(
                approver_id=denier_id,
                approver_role=denier_role,
                approved=False,
                timestamp=datetime.now(timezone.utc).isoformat(),
                evidence_hash=_compute_hash({"denier_id": denier_id, "reason": reason}),
            )
            request.denials.append(record)
            request.status = ApprovalStatus.DENIED

            receipt = _build_receipt(
                suite_id=request.suite_id, office_id=request.office_id,
                correlation_id=request.correlation_id,
                event_type="approval.dual.denied",
                status="denied",
                inputs={
                    "request_id": request_id,
                    "denier_id": denier_id,
                    "denier_role": denier_role,
                    "reason": reason,
                },
                metadata={
                    "request_id": request_id,
                    "action_type": request.action_type,
                },
            )
            receipt["policy"]["reasons"] = ["EXPLICITLY_DENIED"]

            return DualApprovalResult(
                success=True,
                status=ApprovalStatus.DENIED,
                request_id=request_id,
                receipt=receipt,
            )

    def check_status(self, request_id: str, suite_id: str) -> DualApprovalResult:
        """Check the current status of a dual approval request."""
        with self._lock:
            request = self._requests.get(request_id)

            if not request or request.suite_id != suite_id:
                return DualApprovalResult(
                    success=False,
                    status=ApprovalStatus.DENIED,
                    request_id=request_id,
                    error="Request not found or access denied",
                )

            approved_roles = {a.approver_role for a in request.approvals}
            remaining = [r for r in request.required_roles if r not in approved_roles]

            return DualApprovalResult(
                success=True,
                status=request.status,
                request_id=request_id,
                fully_approved=request.status == ApprovalStatus.FULLY_APPROVED,
                remaining_roles=remaining,
            )


# Singleton
_dual_approval_service: DualApprovalService | None = None
_singleton_lock = threading.Lock()


def get_dual_approval_service() -> DualApprovalService:
    """Get the singleton DualApprovalService instance."""
    global _dual_approval_service
    if _dual_approval_service is None:
        with _singleton_lock:
            if _dual_approval_service is None:
                _dual_approval_service = DualApprovalService()
    return _dual_approval_service
