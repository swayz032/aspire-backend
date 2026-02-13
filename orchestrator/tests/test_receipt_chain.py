"""Tests for Receipt Hash Chain — Verification + Tamper Detection (Law #2, W3-09).

Covers:
- Chain computation correctness
- Genesis hash (64 zeros)
- Chain linkage (each receipt links to previous)
- 100-receipt chain verification
- Tamper detection (modified receipt hash, modified content, inserted receipt)
- OpsExceptionCard generation on failure
- Canonicalization correctness
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

import pytest

from aspire_orchestrator.services.receipt_chain import (
    GENESIS_PREV_HASH,
    ChainIntegrityError,
    assign_chain_metadata,
    canonicalize_receipt,
    compute_receipt_hash,
    generate_ops_exception_card,
    verify_chain,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_receipt(
    *,
    receipt_id: str | None = None,
    suite_id: str = "00000000-0000-0000-0000-000000000001",
    office_id: str = "00000000-0000-0000-0000-000000000011",
    action_type: str = "test.action",
    outcome: str = "success",
) -> dict:
    return {
        "id": receipt_id or str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "test",
        "action_type": action_type,
        "risk_tier": "green",
        "tool_used": "test.tool",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "receipt_type": "tool_execution",
    }


def _make_chain(n: int, chain_id: str = "test-suite") -> list[dict]:
    """Create a chain of N receipts with computed hashes."""
    receipts = [_make_receipt() for _ in range(n)]
    assign_chain_metadata(receipts, chain_id=chain_id)
    return receipts


# ===========================================================================
# Canonicalization Tests
# ===========================================================================


class TestCanonicalization:
    def test_excludes_derived_fields(self) -> None:
        receipt = {
            "id": "abc",
            "outcome": "success",
            "receipt_hash": "should_be_excluded",
            "previous_receipt_hash": "should_be_excluded",
            "computed_fields": {"foo": "bar"},
        }
        canonical = canonicalize_receipt(receipt)
        assert "receipt_hash" not in canonical
        assert "previous_receipt_hash" not in canonical
        assert "computed_fields" not in canonical
        assert '"id":"abc"' in canonical
        assert '"outcome":"success"' in canonical

    def test_keys_sorted(self) -> None:
        receipt = {"z_field": "z", "a_field": "a", "m_field": "m"}
        canonical = canonicalize_receipt(receipt)
        # Keys should be in alphabetical order
        z_pos = canonical.index("z_field")
        m_pos = canonical.index("m_field")
        a_pos = canonical.index("a_field")
        assert a_pos < m_pos < z_pos

    def test_no_whitespace(self) -> None:
        receipt = {"key": "value", "num": 42}
        canonical = canonicalize_receipt(receipt)
        assert " " not in canonical
        assert "\n" not in canonical

    def test_deterministic(self) -> None:
        receipt = _make_receipt()
        c1 = canonicalize_receipt(receipt)
        c2 = canonicalize_receipt(receipt)
        assert c1 == c2


# ===========================================================================
# Hash Computation Tests
# ===========================================================================


class TestHashComputation:
    def test_genesis_hash_format(self) -> None:
        assert len(GENESIS_PREV_HASH) == 64
        assert GENESIS_PREV_HASH == "0" * 64

    def test_hash_is_sha256_hex(self) -> None:
        h = compute_receipt_hash("prev", "canonical")
        assert len(h) == 64
        int(h, 16)  # Must be valid hex

    def test_hash_deterministic(self) -> None:
        h1 = compute_receipt_hash("prev", "canonical")
        h2 = compute_receipt_hash("prev", "canonical")
        assert h1 == h2

    def test_different_prev_hash_different_result(self) -> None:
        h1 = compute_receipt_hash("aaaa", "same_canonical")
        h2 = compute_receipt_hash("bbbb", "same_canonical")
        assert h1 != h2

    def test_different_content_different_result(self) -> None:
        h1 = compute_receipt_hash("same_prev", "content_a")
        h2 = compute_receipt_hash("same_prev", "content_b")
        assert h1 != h2


# ===========================================================================
# Chain Assignment Tests
# ===========================================================================


class TestAssignChainMetadata:
    def test_assigns_chain_id(self) -> None:
        receipts = [_make_receipt() for _ in range(3)]
        assign_chain_metadata(receipts, chain_id="suite-abc")
        for r in receipts:
            assert r["chain_id"] == "suite-abc"

    def test_assigns_sequence_starting_at_1(self) -> None:
        receipts = [_make_receipt() for _ in range(3)]
        assign_chain_metadata(receipts, chain_id="test")
        assert [r["sequence"] for r in receipts] == [1, 2, 3]

    def test_custom_starting_sequence(self) -> None:
        receipts = [_make_receipt() for _ in range(3)]
        assign_chain_metadata(receipts, chain_id="test", starting_sequence=10)
        assert [r["sequence"] for r in receipts] == [10, 11, 12]

    def test_genesis_prev_hash(self) -> None:
        receipts = [_make_receipt()]
        assign_chain_metadata(receipts, chain_id="test")
        assert receipts[0]["previous_receipt_hash"] == GENESIS_PREV_HASH

    def test_chain_linkage(self) -> None:
        receipts = _make_chain(5)
        for i in range(1, len(receipts)):
            assert receipts[i]["previous_receipt_hash"] == receipts[i - 1]["receipt_hash"]

    def test_all_hashes_populated(self) -> None:
        receipts = _make_chain(3)
        for r in receipts:
            assert r["receipt_hash"] != ""
            assert len(r["receipt_hash"]) == 64

    def test_custom_starting_prev_hash(self) -> None:
        custom_prev = "a" * 64
        receipts = [_make_receipt()]
        assign_chain_metadata(receipts, chain_id="test", starting_prev_hash=custom_prev)
        assert receipts[0]["previous_receipt_hash"] == custom_prev


# ===========================================================================
# Chain Verification Tests
# ===========================================================================


class TestVerifyChain:
    def test_valid_chain_passes(self) -> None:
        receipts = _make_chain(5)
        result = verify_chain(receipts, chain_id="test-suite")
        assert result.valid is True
        assert result.receipts_verified == 5
        assert result.error_count == 0

    def test_empty_chain_valid(self) -> None:
        result = verify_chain([], chain_id="empty")
        assert result.valid is True
        assert result.receipts_verified == 0

    def test_single_receipt_valid(self) -> None:
        receipts = _make_chain(1)
        result = verify_chain(receipts, chain_id="test-suite")
        assert result.valid is True
        assert result.receipts_verified == 1

    def test_100_receipt_chain(self) -> None:
        """W3-09: 100 receipt chain verification."""
        receipts = _make_chain(100, chain_id="large-suite")
        result = verify_chain(receipts, chain_id="large-suite")
        assert result.valid is True
        assert result.receipts_verified == 100

    def test_chain_id_mismatch_detected(self) -> None:
        receipts = _make_chain(3, chain_id="suite-a")
        # Verify expecting suite-b
        result = verify_chain(receipts, chain_id="suite-b")
        assert result.valid is False
        assert result.error_count == 3  # All 3 receipts have wrong chain_id


# ===========================================================================
# Tamper Detection Tests
# ===========================================================================


class TestTamperDetection:
    def test_tampered_receipt_hash_detected(self) -> None:
        """Tamper: modify a receipt_hash after computation."""
        receipts = _make_chain(5)
        receipts[2]["receipt_hash"] = "f" * 64
        result = verify_chain(receipts, chain_id="test-suite")
        assert result.valid is False
        # Error at seq 2 (hash mismatch) + seq 3 (prev_hash mismatch from cascading)
        assert result.error_count >= 1

    def test_tampered_content_detected(self) -> None:
        """Tamper: modify receipt content after hash computation."""
        receipts = _make_chain(5)
        receipts[1]["outcome"] = "TAMPERED"
        result = verify_chain(receipts, chain_id="test-suite")
        assert result.valid is False
        assert any(
            "hash mismatch" in str(e).lower()
            for e in result.errors
        )

    def test_tampered_prev_hash_detected(self) -> None:
        """Tamper: modify previous_receipt_hash to break linkage."""
        receipts = _make_chain(5)
        receipts[3]["previous_receipt_hash"] = "b" * 64
        result = verify_chain(receipts, chain_id="test-suite")
        assert result.valid is False

    def test_deleted_receipt_detected(self) -> None:
        """Tamper: delete a receipt from the middle of the chain."""
        receipts = _make_chain(5)
        del receipts[2]  # Remove receipt at index 2
        result = verify_chain(receipts, chain_id="test-suite")
        assert result.valid is False
        # Receipt 3 now has wrong prev_hash (it pointed to deleted receipt 2)

    def test_inserted_receipt_detected(self) -> None:
        """Tamper: insert a receipt into the middle of an existing chain."""
        receipts = _make_chain(5)
        # Insert a rogue receipt at position 2
        rogue = _make_receipt(action_type="evil.insert")
        rogue["chain_id"] = "test-suite"
        rogue["sequence"] = 999
        rogue["receipt_hash"] = "c" * 64
        rogue["previous_receipt_hash"] = receipts[1]["receipt_hash"]
        receipts.insert(2, rogue)
        result = verify_chain(receipts, chain_id="test-suite")
        assert result.valid is False

    def test_reordered_receipts_detected(self) -> None:
        """Tamper: swap order of two receipts."""
        receipts = _make_chain(5)
        receipts[1], receipts[2] = receipts[2], receipts[1]
        result = verify_chain(receipts, chain_id="test-suite")
        assert result.valid is False

    def test_100_receipt_tamper_at_position_50(self) -> None:
        """W3-09: Tamper receipt #50 in a 100-receipt chain — must detect."""
        receipts = _make_chain(100)
        receipts[49]["outcome"] = "TAMPERED_AT_50"
        result = verify_chain(receipts, chain_id="test-suite")
        assert result.valid is False
        # Find the first error — should be at sequence 50
        first_error = result.errors[0]
        assert first_error.sequence == 50

    def test_genesis_hash_tampered(self) -> None:
        """Tamper: first receipt has wrong prev_hash (not genesis)."""
        receipts = _make_chain(3)
        receipts[0]["previous_receipt_hash"] = "e" * 64
        result = verify_chain(receipts, chain_id="test-suite")
        assert result.valid is False
        assert result.errors[0].sequence == 1


# ===========================================================================
# OpsExceptionCard Tests
# ===========================================================================


class TestOpsExceptionCard:
    def test_no_card_on_valid_chain(self) -> None:
        receipts = _make_chain(3)
        result = verify_chain(receipts, chain_id="test-suite")
        card = generate_ops_exception_card(result)
        assert card is None

    def test_card_generated_on_failure(self) -> None:
        receipts = _make_chain(3)
        receipts[1]["receipt_hash"] = "x" * 64
        result = verify_chain(receipts, chain_id="test-suite")
        card = generate_ops_exception_card(result)
        assert card is not None
        assert card["type"] == "OpsExceptionCard"
        assert card["severity"] == "sev1"
        assert card["class"] == "receipt_chain_integrity"
        assert card["error_count"] >= 1
        assert "Manual investigation" in card["action_required"]

    def test_card_contains_chain_details(self) -> None:
        receipts = _make_chain(5, chain_id="suite-abc")
        receipts[2]["outcome"] = "TAMPERED"
        result = verify_chain(receipts, chain_id="suite-abc")
        card = generate_ops_exception_card(result)
        assert card is not None
        assert card["chain_id"] == "suite-abc"
        assert card["receipts_verified"] == 5


# ===========================================================================
# Append to Existing Chain Tests
# ===========================================================================


class TestAppendToChain:
    def test_append_continues_chain(self) -> None:
        """New receipts can be appended to an existing chain."""
        first_batch = _make_chain(3, chain_id="suite-x")
        last_hash = first_batch[-1]["receipt_hash"]
        last_seq = first_batch[-1]["sequence"]

        second_batch = [_make_receipt() for _ in range(2)]
        assign_chain_metadata(
            second_batch,
            chain_id="suite-x",
            starting_sequence=last_seq + 1,
            starting_prev_hash=last_hash,
        )

        # Verify full chain
        full_chain = first_batch + second_batch
        result = verify_chain(full_chain, chain_id="suite-x")
        assert result.valid is True
        assert result.receipts_verified == 5

    def test_sequences_continuous(self) -> None:
        first_batch = _make_chain(3, chain_id="suite-x")
        second_batch = [_make_receipt() for _ in range(2)]
        assign_chain_metadata(
            second_batch,
            chain_id="suite-x",
            starting_sequence=4,
            starting_prev_hash=first_batch[-1]["receipt_hash"],
        )
        full_chain = first_batch + second_batch
        sequences = [r["sequence"] for r in full_chain]
        assert sequences == [1, 2, 3, 4, 5]
