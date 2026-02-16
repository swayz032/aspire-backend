"""Reconciliation Engine — Match bank transactions to internal records.

Matches payments, invoices, and transfers to bank statement entries.
Used by Finn Money Desk and Teressa Books for financial accuracy.

Law compliance:
- Law #2: Every reconciliation attempt produces a receipt
- Law #3: Ambiguous matches -> flag for human review (fail-closed)
- Law #7: Pure logic — no provider calls
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

ACTOR_RECONCILIATION = "service:reconciliation-engine"

# Confidence thresholds
CONFIDENCE_AUTO_MATCH = 0.95
CONFIDENCE_SUGGEST = 0.70
# Below CONFIDENCE_SUGGEST -> flag for manual review


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class InternalRecord:
    """An internal financial record (invoice, payment, transfer)."""

    record_id: str
    amount_cents: int
    date: str  # ISO 8601 date (YYYY-MM-DD)
    description: str
    record_type: str  # invoice, payment, transfer
    reference: str = ""  # optional external reference


@dataclass
class BankEntry:
    """A bank statement entry."""

    entry_id: str
    amount_cents: int
    date: str  # ISO 8601 date (YYYY-MM-DD)
    description: str
    reference: str = ""  # optional reference field


@dataclass(frozen=True)
class ReconciliationMatch:
    """A match between an internal record and a bank entry."""

    internal_record_id: str
    external_txn_id: str
    match_type: str  # exact, fuzzy, manual
    confidence: float  # 0.0 - 1.0
    amount_cents: int
    date_delta_days: int  # absolute difference in days


@dataclass
class ReconciliationResult:
    """Result of a reconciliation run."""

    matched: list[ReconciliationMatch]
    unmatched_internal: list[str]  # record_ids with no match
    unmatched_external: list[str]  # entry_ids with no match
    flagged_for_review: list[ReconciliationMatch]  # ambiguous matches
    receipt: dict[str, Any] = field(default_factory=dict)


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
    """Build a receipt for a reconciliation operation (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_version": "1.0",
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": f"reconciliation.{action_type}",
        "suite_id": suite_id,
        "office_id": office_id,
        "actor": ACTOR_RECONCILIATION,
        "correlation_id": correlation_id,
        "action_type": action_type,
        "outcome": outcome,
        "reason_code": reason_code,
    }
    if details:
        receipt["details"] = details
    return receipt


# =============================================================================
# Matching Logic
# =============================================================================


def _parse_date(date_str: str) -> datetime:
    """Parse an ISO date string to datetime. Handles YYYY-MM-DD format."""
    return datetime.strptime(date_str, "%Y-%m-%d")


def _date_delta_days(date1: str, date2: str) -> int:
    """Compute absolute difference in days between two date strings."""
    d1 = _parse_date(date1)
    d2 = _parse_date(date2)
    return abs((d1 - d2).days)


def _match_exact(record: InternalRecord, entry: BankEntry) -> ReconciliationMatch | None:
    """Attempt exact match: same amount AND same date."""
    if record.amount_cents == entry.amount_cents and record.date == entry.date:
        return ReconciliationMatch(
            internal_record_id=record.record_id,
            external_txn_id=entry.entry_id,
            match_type="exact",
            confidence=1.0,
            amount_cents=record.amount_cents,
            date_delta_days=0,
        )
    return None


def _match_fuzzy(record: InternalRecord, entry: BankEntry) -> ReconciliationMatch | None:
    """Attempt fuzzy match: amount within +/-1 cent AND date within +/-2 days."""
    amount_diff = abs(record.amount_cents - entry.amount_cents)
    date_delta = _date_delta_days(record.date, entry.date)

    if amount_diff <= 1 and date_delta <= 2:
        # Compute confidence based on closeness
        amount_penalty = 0.02 if amount_diff > 0 else 0.0
        date_penalty = date_delta * 0.015
        confidence = 1.0 - amount_penalty - date_penalty

        return ReconciliationMatch(
            internal_record_id=record.record_id,
            external_txn_id=entry.entry_id,
            match_type="fuzzy",
            confidence=round(confidence, 4),
            amount_cents=record.amount_cents,
            date_delta_days=date_delta,
        )
    return None


def _match_reference(record: InternalRecord, entry: BankEntry) -> ReconciliationMatch | None:
    """Attempt reference-based match: matching reference fields."""
    if (
        record.reference
        and entry.reference
        and record.reference.strip().lower() == entry.reference.strip().lower()
    ):
        date_delta = _date_delta_days(record.date, entry.date)
        amount_match = record.amount_cents == entry.amount_cents
        confidence = 0.90 if amount_match else 0.75
        return ReconciliationMatch(
            internal_record_id=record.record_id,
            external_txn_id=entry.entry_id,
            match_type="fuzzy",
            confidence=confidence,
            amount_cents=record.amount_cents,
            date_delta_days=date_delta,
        )
    return None


# =============================================================================
# Core Reconciliation
# =============================================================================


def reconcile(
    internal_records: list[InternalRecord],
    bank_entries: list[BankEntry],
    *,
    suite_id: str,
    office_id: str,
    correlation_id: str,
) -> ReconciliationResult:
    """Reconcile internal records against bank entries.

    Matching strategies (applied in order):
    1. Exact: amount matches exactly AND date matches exactly -> confidence 1.0
    2. Reference: matching reference fields -> confidence 0.75-0.90
    3. Fuzzy: amount within +/-$0.01, date within +/-2 days -> confidence varies

    Confidence thresholds:
    - >0.95: auto-match (included in matched list)
    - 0.70-0.95: suggest (included in flagged_for_review — Law #3 fail-closed)
    - <0.70: no match (unmatched)

    Law #2: Always produces a receipt.
    Law #3: Ambiguous matches flagged for human review.
    Law #6: All operations scoped to suite_id/office_id.

    Args:
        internal_records: Internal financial records to match.
        bank_entries: Bank statement entries to match against.
        suite_id: Tenant suite ID.
        office_id: Tenant office ID.
        correlation_id: Trace ID for the operation.

    Returns:
        ReconciliationResult with matched, unmatched, flagged, and receipt.
    """
    matched: list[ReconciliationMatch] = []
    flagged: list[ReconciliationMatch] = []
    matched_internal_ids: set[str] = set()
    matched_external_ids: set[str] = set()

    # Pass 1: Exact matches
    for record in internal_records:
        if record.record_id in matched_internal_ids:
            continue
        for entry in bank_entries:
            if entry.entry_id in matched_external_ids:
                continue
            match = _match_exact(record, entry)
            if match:
                matched.append(match)
                matched_internal_ids.add(record.record_id)
                matched_external_ids.add(entry.entry_id)
                break

    # Pass 2: Reference matches (for unmatched records)
    for record in internal_records:
        if record.record_id in matched_internal_ids:
            continue
        for entry in bank_entries:
            if entry.entry_id in matched_external_ids:
                continue
            match = _match_reference(record, entry)
            if match:
                if match.confidence >= CONFIDENCE_AUTO_MATCH:
                    matched.append(match)
                    matched_internal_ids.add(record.record_id)
                    matched_external_ids.add(entry.entry_id)
                elif match.confidence >= CONFIDENCE_SUGGEST:
                    flagged.append(match)
                    # Don't mark as matched — still unmatched until human review
                break

    # Pass 3: Fuzzy matches (for remaining unmatched)
    for record in internal_records:
        if record.record_id in matched_internal_ids:
            continue
        best_match: ReconciliationMatch | None = None
        for entry in bank_entries:
            if entry.entry_id in matched_external_ids:
                continue
            match = _match_fuzzy(record, entry)
            if match and (best_match is None or match.confidence > best_match.confidence):
                best_match = match

        if best_match:
            if best_match.confidence >= CONFIDENCE_AUTO_MATCH:
                matched.append(best_match)
                matched_internal_ids.add(record.record_id)
                matched_external_ids.add(best_match.external_txn_id)
            elif best_match.confidence >= CONFIDENCE_SUGGEST:
                # Law #3: Ambiguous -> flag for human review (fail-closed)
                flagged.append(best_match)

    # Compute unmatched
    # For unmatched_internal, exclude records that were flagged for review
    flagged_internal_ids = {f.internal_record_id for f in flagged}
    unmatched_internal = [
        r.record_id for r in internal_records
        if r.record_id not in matched_internal_ids
        and r.record_id not in flagged_internal_ids
    ]

    flagged_external_ids = {f.external_txn_id for f in flagged}
    unmatched_external = [
        e.entry_id for e in bank_entries
        if e.entry_id not in matched_external_ids
        and e.entry_id not in flagged_external_ids
    ]

    receipt = _build_receipt(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        action_type="reconcile",
        outcome="success",
        details={
            "internal_record_count": len(internal_records),
            "bank_entry_count": len(bank_entries),
            "matched_count": len(matched),
            "flagged_for_review_count": len(flagged),
            "unmatched_internal_count": len(unmatched_internal),
            "unmatched_external_count": len(unmatched_external),
        },
    )

    logger.info(
        "Reconciliation complete: suite=%s, matched=%d, flagged=%d, "
        "unmatched_int=%d, unmatched_ext=%d, corr=%s",
        suite_id[:8] if len(suite_id) > 8 else suite_id,
        len(matched),
        len(flagged),
        len(unmatched_internal),
        len(unmatched_external),
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    return ReconciliationResult(
        matched=matched,
        unmatched_internal=unmatched_internal,
        unmatched_external=unmatched_external,
        flagged_for_review=flagged,
        receipt=receipt,
    )
