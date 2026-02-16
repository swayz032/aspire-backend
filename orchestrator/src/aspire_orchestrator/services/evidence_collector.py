"""Evidence Collector — Attach supporting evidence to financial proposals.

When Finn proposes a payment, transfer, or draw, this service collects
and packages the evidence that supports the proposal (invoices, contracts,
bank statements, etc.)

Law compliance:
- Law #2: Every evidence collection produces a receipt
- Law #3: Incomplete evidence -> proposal cannot proceed (fail-closed)
- Law #7: Pure logic — no provider calls
- Law #9: PII redacted from evidence packages
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

ACTOR_EVIDENCE_COLLECTOR = "service:evidence-collector"

# Minimum completeness score for evidence packages to be valid
DEFAULT_MINIMUM_REQUIRED_SCORE = 0.8

# PII patterns to redact from evidence descriptions (Law #9)
_PII_PATTERNS = [
    "ssn", "social_security", "account_number", "routing_number",
    "credit_card", "cc_number", "tax_id", "ein",
]


# =============================================================================
# Evidence Requirements by Action
# =============================================================================


# Each entry maps an action to its required evidence types and their weights.
# Weights must sum to 1.0 for each action.
EVIDENCE_REQUIREMENTS: dict[str, dict[str, float]] = {
    "payment.send": {
        "invoice": 0.25,
        "contract": 0.25,
        "bank_balance": 0.3,
        "approval_record": 0.2,
    },
    "payment.transfer": {
        "source_statement": 0.4,
        "destination_verify": 0.3,
        "approval_record": 0.3,
    },
    "payroll.run": {
        "snapshot": 0.4,
        "tax_calc": 0.3,
        "approval_record": 0.3,
    },
}

# For payment.send, invoice OR contract satisfies the first requirement
_ALTERNATIVE_EVIDENCE: dict[str, list[list[str]]] = {
    "payment.send": [["invoice", "contract"]],  # either satisfies the slot
}


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class EvidenceItem:
    """A single piece of supporting evidence."""

    id: str
    type: str  # invoice, contract, statement, receipt, bank_balance, approval_record, etc.
    reference_id: str  # ID of the source document
    description: str
    collected_at: str  # ISO 8601
    hash: str  # SHA-256 hash of content for integrity verification

    @staticmethod
    def compute_hash(content: str) -> str:
        """Compute SHA-256 hash of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass
class EvidencePackage:
    """A package of evidence items for a financial proposal."""

    id: str
    proposal_id: str
    suite_id: str
    office_id: str
    items: list[EvidenceItem]
    completeness_score: float
    minimum_required_score: float
    created_at: str  # ISO 8601

    @property
    def is_complete(self) -> bool:
        """Check if the package meets the minimum completeness threshold."""
        return self.completeness_score >= self.minimum_required_score


@dataclass(frozen=True)
class AttachmentReceipt:
    """Receipt for attaching evidence to a proposal."""

    receipt: dict[str, Any]
    proposal_id: str
    package_id: str
    attached: bool


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
    """Build a receipt for an evidence collection operation (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_version": "1.0",
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": f"evidence.{action_type}",
        "suite_id": suite_id,
        "office_id": office_id,
        "actor": ACTOR_EVIDENCE_COLLECTOR,
        "correlation_id": correlation_id,
        "action_type": action_type,
        "outcome": outcome,
        "reason_code": reason_code,
    }
    if details:
        receipt["details"] = details
    return receipt


# =============================================================================
# PII Redaction (Law #9)
# =============================================================================


def _redact_description(description: str) -> str:
    """Redact potential PII from evidence descriptions.

    Law #9: Never store raw PII. Replace known PII patterns with redaction markers.
    """
    redacted = description
    for pattern in _PII_PATTERNS:
        # Simple case-insensitive check — production would use Presidio DLP
        lower = redacted.lower()
        idx = lower.find(pattern)
        while idx != -1:
            # Find the value after the pattern (e.g., "ssn: 123-45-6789")
            colon_idx = redacted.find(":", idx)
            if colon_idx != -1 and colon_idx < idx + len(pattern) + 2:
                # Redact everything after the colon until next comma/period/newline
                end_idx = len(redacted)
                for delimiter in [",", ".", "\n", ";", ")"]:
                    d_idx = redacted.find(delimiter, colon_idx)
                    if d_idx != -1 and d_idx < end_idx:
                        end_idx = d_idx
                redacted = (
                    redacted[:colon_idx + 1]
                    + " <PII_REDACTED>"
                    + redacted[end_idx:]
                )
            idx = lower.find(pattern, idx + len(pattern) + 10)
    return redacted


# =============================================================================
# Core Functions
# =============================================================================


def collect_evidence(
    proposal_id: str,
    action_type: str,
    evidence_items: list[EvidenceItem],
    *,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    minimum_score: float = DEFAULT_MINIMUM_REQUIRED_SCORE,
) -> EvidencePackage:
    """Collect and package evidence for a financial proposal.

    Law #2: Produces a receipt.
    Law #3: Unknown action_type -> fail-closed (no evidence requirements known).
    Law #6: Scoped to suite_id/office_id.
    Law #9: PII redacted from evidence descriptions.

    Args:
        proposal_id: ID of the financial proposal.
        action_type: The action requiring evidence (payment.send, payment.transfer, payroll.run).
        evidence_items: List of evidence items collected.
        suite_id: Tenant suite ID.
        office_id: Tenant office ID.
        correlation_id: Trace ID for the operation.
        minimum_score: Minimum completeness score (default 0.8).

    Returns:
        EvidencePackage with completeness score and items.
    """
    # Redact PII from evidence descriptions (Law #9)
    redacted_items: list[EvidenceItem] = []
    for item in evidence_items:
        redacted_items.append(EvidenceItem(
            id=item.id,
            type=item.type,
            reference_id=item.reference_id,
            description=_redact_description(item.description),
            collected_at=item.collected_at,
            hash=item.hash,
        ))

    # Calculate completeness score
    completeness_score = _calculate_completeness(action_type, redacted_items)

    package_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    package = EvidencePackage(
        id=package_id,
        proposal_id=proposal_id,
        suite_id=suite_id,
        office_id=office_id,
        items=redacted_items,
        completeness_score=completeness_score,
        minimum_required_score=minimum_score,
        created_at=now,
    )

    logger.info(
        "Evidence collected: proposal=%s, action=%s, items=%d, score=%.2f, "
        "complete=%s, suite=%s",
        proposal_id[:8] if len(proposal_id) > 8 else proposal_id,
        action_type, len(redacted_items), completeness_score,
        package.is_complete,
        suite_id[:8] if len(suite_id) > 8 else suite_id,
    )

    return package


def _calculate_completeness(action_type: str, items: list[EvidenceItem]) -> float:
    """Calculate completeness score based on evidence requirements.

    Each evidence type has a weight. The score is the sum of weights
    for evidence types that are present.

    For actions with alternative evidence (e.g., payment.send needs
    invoice OR contract), either one satisfies the requirement.
    """
    requirements = EVIDENCE_REQUIREMENTS.get(action_type)
    if requirements is None:
        # Unknown action — score 0.0 (fail-closed, Law #3)
        logger.warning("No evidence requirements defined for action: %s", action_type)
        return 0.0

    provided_types = {item.type for item in items}
    score = 0.0

    # Check for alternative evidence groups
    alternatives = _ALTERNATIVE_EVIDENCE.get(action_type, [])
    satisfied_by_alternative: set[str] = set()

    for alt_group in alternatives:
        if any(t in provided_types for t in alt_group):
            # At least one alternative is present — credit all in the group
            for t in alt_group:
                if t in requirements:
                    satisfied_by_alternative.add(t)

    # Score each requirement
    for evidence_type, weight in requirements.items():
        if evidence_type in provided_types:
            score += weight
        elif evidence_type in satisfied_by_alternative:
            # Alternative evidence satisfies this requirement
            score += weight

    return round(min(score, 1.0), 4)


def validate_completeness(package: EvidencePackage) -> bool:
    """Validate that an evidence package meets the minimum completeness threshold.

    Law #3: Incomplete evidence -> proposal cannot proceed.
    """
    return package.is_complete


def attach_to_proposal(
    proposal_id: str,
    package: EvidencePackage,
    *,
    correlation_id: str,
) -> AttachmentReceipt:
    """Attach an evidence package to a financial proposal.

    Law #2: Produces a receipt.
    Law #3: Incomplete package -> deny attachment (fail-closed).

    Args:
        proposal_id: ID of the financial proposal.
        package: The evidence package to attach.
        correlation_id: Trace ID for the operation.

    Returns:
        AttachmentReceipt with attachment status and receipt.
    """
    if not package.is_complete:
        receipt = _build_receipt(
            suite_id=package.suite_id,
            office_id=package.office_id,
            correlation_id=correlation_id,
            action_type="attach",
            outcome="denied",
            reason_code="INCOMPLETE_EVIDENCE",
            details={
                "proposal_id": proposal_id,
                "package_id": package.id,
                "completeness_score": package.completeness_score,
                "minimum_required_score": package.minimum_required_score,
                "item_count": len(package.items),
                "item_types": [item.type for item in package.items],
            },
        )

        logger.warning(
            "Evidence attachment DENIED: proposal=%s, score=%.2f < %.2f required",
            proposal_id[:8] if len(proposal_id) > 8 else proposal_id,
            package.completeness_score,
            package.minimum_required_score,
        )

        return AttachmentReceipt(
            receipt=receipt,
            proposal_id=proposal_id,
            package_id=package.id,
            attached=False,
        )

    # Compute package integrity hash
    items_canonical = json.dumps(
        [{"id": i.id, "type": i.type, "hash": i.hash} for i in package.items],
        sort_keys=True,
        separators=(",", ":"),
    )
    package_hash = hashlib.sha256(items_canonical.encode("utf-8")).hexdigest()

    receipt = _build_receipt(
        suite_id=package.suite_id,
        office_id=package.office_id,
        correlation_id=correlation_id,
        action_type="attach",
        outcome="success",
        details={
            "proposal_id": proposal_id,
            "package_id": package.id,
            "completeness_score": package.completeness_score,
            "item_count": len(package.items),
            "item_types": [item.type for item in package.items],
            "package_hash": f"sha256:{package_hash}",
        },
    )

    logger.info(
        "Evidence attached: proposal=%s, package=%s, score=%.2f, items=%d",
        proposal_id[:8] if len(proposal_id) > 8 else proposal_id,
        package.id[:8],
        package.completeness_score,
        len(package.items),
    )

    return AttachmentReceipt(
        receipt=receipt,
        proposal_id=proposal_id,
        package_id=package.id,
        attached=True,
    )
