"""Receipt Chain Service — Hash Chain Computation + Verification (Law #2).

Per receipt_chain_spec.md:
  - chain_id = suite_id (one chain per suite), "global" for admin receipts
  - sequence = monotonically increasing integer within a chain
  - genesis_prev_hash = "0" * 64
  - receipt_hash = sha256_hex(prev_hash + "\\n" + canonical_receipt)
  - Canonical JSON: UTF-8, keys sorted, no whitespace, exclude derived fields
  - Verifier job: recompute hashes, detect tampering, emit OpsExceptionCard on mismatch

Per receipt_emission_rules.md:
  - If receipts cannot be written: fail closed and degrade to draft-only mode
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

GENESIS_PREV_HASH = "0" * 64

# Derived fields excluded from canonical JSON per receipt_chain_spec.md
_EXCLUDE_FROM_CANONICAL = frozenset({
    "receipt_hash",
    "previous_receipt_hash",
    "computed_fields",
})


def canonicalize_receipt(receipt: dict[str, Any]) -> str:
    """Serialize receipt to canonical JSON for hashing.

    Per receipt_chain_spec.md:
    - UTF-8, keys sorted lexicographically
    - No insignificant whitespace
    - Arrays preserved in order
    - Exclude derived fields (receipt_hash, previous_receipt_hash, computed_fields)
    """
    canonical_dict = {
        k: v for k, v in receipt.items()
        if k not in _EXCLUDE_FROM_CANONICAL
    }
    return json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"), default=str)


def compute_receipt_hash(prev_hash: str, canonical_receipt: str) -> str:
    """Compute receipt hash per chain spec.

    receipt_hash = sha256_hex(prev_hash + "\\n" + canonical_receipt)
    """
    data = f"{prev_hash}\n{canonical_receipt}"
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def assign_chain_metadata(
    receipts: list[dict[str, Any]],
    chain_id: str,
    starting_sequence: int = 1,
    starting_prev_hash: str = GENESIS_PREV_HASH,
) -> list[dict[str, Any]]:
    """Assign chain_id, sequence, and compute hash chain for a list of receipts.

    Mutates receipts in-place and returns them.
    Starting_sequence and starting_prev_hash allow appending to existing chains.
    """
    prev_hash = starting_prev_hash
    for i, receipt in enumerate(receipts):
        receipt["chain_id"] = chain_id
        receipt["sequence"] = starting_sequence + i

        canonical = canonicalize_receipt(receipt)
        receipt_hash = compute_receipt_hash(prev_hash, canonical)
        receipt["receipt_hash"] = receipt_hash
        receipt["previous_receipt_hash"] = prev_hash
        prev_hash = receipt_hash

    return receipts


# ===========================================================================
# Chain Verification
# ===========================================================================


class ChainIntegrityError(Exception):
    """Raised when receipt chain integrity verification fails."""

    def __init__(self, message: str, *, chain_id: str, sequence: int, details: str):
        super().__init__(message)
        self.chain_id = chain_id
        self.sequence = sequence
        self.details = details


@dataclass
class VerificationResult:
    """Result of chain verification."""

    valid: bool
    chain_id: str
    receipts_verified: int
    errors: list[ChainIntegrityError] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len(self.errors)


def verify_chain(
    receipts: list[dict[str, Any]],
    *,
    chain_id: str | None = None,
    expected_prev_hash: str = GENESIS_PREV_HASH,
) -> VerificationResult:
    """Verify receipt chain integrity by recomputing all hashes.

    Per receipt_chain_spec.md verifier job:
    1. For each receipt, recompute receipt_hash
    2. Ensure prev_hash matches prior recomputed hash
    3. Ensure recomputed receipt_hash equals stored
    4. On mismatch: report error (no auto-repair)

    Args:
        receipts: List of receipt dicts (ordered by sequence)
        chain_id: Expected chain_id (optional, verifies if provided)
        expected_prev_hash: The prev_hash for the first receipt

    Returns:
        VerificationResult with valid flag and any errors found
    """
    if not receipts:
        return VerificationResult(
            valid=True,
            chain_id=chain_id or "empty",
            receipts_verified=0,
        )

    detected_chain_id = chain_id or receipts[0].get("chain_id", "unknown")
    errors: list[ChainIntegrityError] = []
    prev_hash = expected_prev_hash

    for receipt in receipts:
        seq = receipt.get("sequence", -1)

        # Verify chain_id consistency
        if chain_id and receipt.get("chain_id") != chain_id:
            errors.append(ChainIntegrityError(
                f"Chain ID mismatch at sequence {seq}",
                chain_id=detected_chain_id,
                sequence=seq,
                details=f"Expected chain_id={chain_id}, got {receipt.get('chain_id')}",
            ))

        # Verify prev_hash linkage
        stored_prev_hash = receipt.get("previous_receipt_hash", "")
        if stored_prev_hash != prev_hash:
            errors.append(ChainIntegrityError(
                f"Previous hash mismatch at sequence {seq}",
                chain_id=detected_chain_id,
                sequence=seq,
                details=f"Expected prev_hash={prev_hash[:16]}..., stored={stored_prev_hash[:16]}...",
            ))

        # Recompute receipt_hash
        canonical = canonicalize_receipt(receipt)
        recomputed_hash = compute_receipt_hash(prev_hash, canonical)

        stored_hash = receipt.get("receipt_hash", "")
        if recomputed_hash != stored_hash:
            errors.append(ChainIntegrityError(
                f"Receipt hash mismatch at sequence {seq}",
                chain_id=detected_chain_id,
                sequence=seq,
                details=f"Recomputed={recomputed_hash[:16]}..., stored={stored_hash[:16]}...",
            ))

        # Use recomputed hash as prev for next receipt (not stored — detect cascading corruption)
        prev_hash = recomputed_hash

    is_valid = len(errors) == 0

    if is_valid:
        logger.info(
            "Chain verification PASSED: chain_id=%s, receipts=%d",
            detected_chain_id, len(receipts),
        )
    else:
        logger.error(
            "Chain verification FAILED: chain_id=%s, receipts=%d, errors=%d",
            detected_chain_id, len(receipts), len(errors),
        )
        for err in errors:
            logger.error(
                "  Chain error at seq %d: %s — %s",
                err.sequence, err, err.details,
            )

    return VerificationResult(
        valid=is_valid,
        chain_id=detected_chain_id,
        receipts_verified=len(receipts),
        errors=errors,
    )


def generate_ops_exception_card(
    verification_result: VerificationResult,
) -> dict[str, Any] | None:
    """Generate an OpsExceptionCard for chain integrity failures.

    Per receipt_chain_spec.md:
    - emit OpsExceptionCard with severity=sev1, class=receipt_chain_integrity
    - open an incident (no auto-repair)
    - require approval for any remediation steps

    Returns None if chain is valid.
    """
    if verification_result.valid:
        return None

    return {
        "type": "OpsExceptionCard",
        "severity": "sev1",
        "class": "receipt_chain_integrity",
        "chain_id": verification_result.chain_id,
        "receipts_verified": verification_result.receipts_verified,
        "error_count": verification_result.error_count,
        "errors": [
            {
                "sequence": err.sequence,
                "message": str(err),
                "details": err.details,
            }
            for err in verification_result.errors
        ],
        "action_required": "Manual investigation required. No auto-repair. Approval needed for remediation.",
    }
