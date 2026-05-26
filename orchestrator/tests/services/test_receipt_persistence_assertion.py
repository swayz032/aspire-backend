"""Unit tests for emit_persistence_asserted_receipt (Track B Pass 1.C).

Validates the status decision state machine in receipt_store.py:

    delta = count_after - count_before
    expected_min > 0 AND delta >= expected_min  -> "SUCCEEDED"
    expected_min == 0 AND delta == 0            -> "SUCCEEDED_WITH_ZERO_OUTPUT"
    expected_min == 0 AND delta > 0             -> "SUCCEEDED"   (over-delivered)
    else                                        -> "FAILED"
        error_message = "persistence_assertion: {table} delta={delta} < expected_min={expected_min}"

Aspire Laws validated:
  Law #2 (Receipt for All Actions) -- every write-producing stage must emit a
    receipt whose status accurately reflects what was actually persisted.
  Law #3 (Fail Closed) -- zero rows written when >0 expected must produce
    FAILED, never silently SUCCEEDED.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

SUITE_ID = str(uuid.UUID("aa000000-0000-0000-0000-000000000001"))
OFFICE_ID = str(uuid.UUID("aa000000-0000-0000-0000-000000000002"))
PROJECT_ID = str(uuid.UUID("aa000000-0000-0000-0000-000000000003"))
CORRELATION_ID = "corr-test-persistence-assertion-001"
STAGE = "drew.see.11g2.page"
TABLE = "blueprint_primitives"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke(
    *,
    stage: str = STAGE,
    expected_table: str = TABLE,
    expected_min: int,
    count_before: int,
    count_after: int,
    correlation_id: str = CORRELATION_ID,
    suite_id: str = SUITE_ID,
    office_id: str = OFFICE_ID,
    project_id: str = PROJECT_ID,
    redacted_outputs: dict[str, Any] | None = None,
) -> Any:
    """Call emit_persistence_asserted_receipt with mocked store_receipts.

    Returns the raw return value from the helper (a receipt id string or
    raises, depending on implementation).  The important assertion is the
    receipt that was passed to store_receipts.
    """
    from aspire_orchestrator.services.receipt_store import emit_persistence_asserted_receipt  # noqa: PLC0415

    return emit_persistence_asserted_receipt(
        stage=stage,
        expected_table=expected_table,
        expected_min=expected_min,
        count_before=count_before,
        count_after=count_after,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        project_id=project_id,
        redacted_outputs=redacted_outputs or {},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_store(monkeypatch: pytest.MonkeyPatch):
    """Patch store_receipts so no in-memory or Supabase state is touched."""
    mock = MagicMock()
    monkeypatch.setattr(
        "aspire_orchestrator.services.receipt_store.store_receipts",
        mock,
    )
    return mock


@pytest.fixture(autouse=True)
def _mock_cut_receipt(monkeypatch: pytest.MonkeyPatch):
    """Patch _cut_receipt so tests receive a deterministic stub dict.

    The stub includes the fields the assertion helper is expected to set:
      - outcome / status
      - error_message (when FAILED)
    """
    captured: list[dict[str, Any]] = []

    def _stub(**kwargs: Any) -> dict[str, Any]:
        receipt = dict(kwargs)
        captured.append(receipt)
        receipt.setdefault("id", str(uuid.uuid4()))
        return receipt

    monkeypatch.setattr(
        "aspire_orchestrator.services.receipt_store._cut_receipt",
        _stub,
        raising=False,
    )
    return captured


# ---------------------------------------------------------------------------
# Helper to extract the receipt dict that was stored
# ---------------------------------------------------------------------------

def _stored_receipt(_mock_store: MagicMock) -> dict[str, Any]:
    """Return the first receipt from the most recent store_receipts call."""
    assert _mock_store.called, "store_receipts was never called"
    call_args = _mock_store.call_args
    receipts_arg: list[dict[str, Any]] = call_args[0][0]
    assert receipts_arg, "store_receipts called with empty list"
    return receipts_arg[0]


# ---------------------------------------------------------------------------
# Class: TestSucceededBranch
# ---------------------------------------------------------------------------

class TestSucceededBranch:
    """Law #2 + #3: expected_min > 0 AND delta >= expected_min -> SUCCEEDED."""

    def test_succeeded_delta_exceeds_expected_min(self, _mock_store: MagicMock) -> None:
        """expected_min=10, count_before=5, count_after=20 -> delta=15 -> SUCCEEDED."""
        _invoke(expected_min=10, count_before=5, count_after=20)

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()
        assert status == "SUCCEEDED", (
            f"Expected SUCCEEDED for delta=15 >= expected_min=10, got {status!r}"
        )
        assert "error_message" not in receipt or not receipt["error_message"], (
            "SUCCEEDED receipt must not carry error_message"
        )

    def test_succeeded_boundary_delta_equals_expected_min(self, _mock_store: MagicMock) -> None:
        """expected_min=1, count_before=0, count_after=1 -> delta=1 -> SUCCEEDED (boundary)."""
        _invoke(expected_min=1, count_before=0, count_after=1)

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()
        assert status == "SUCCEEDED", (
            f"Expected SUCCEEDED at boundary delta=1 == expected_min=1, got {status!r}"
        )

    def test_succeeded_large_delta_large_expected_min(self, _mock_store: MagicMock) -> None:
        """expected_min=1000, count_before=0, count_after=1280000 -> SUCCEEDED (Gulfstream scale)."""
        _invoke(expected_min=1000, count_before=0, count_after=1_280_000)

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()
        assert status == "SUCCEEDED", (
            f"Expected SUCCEEDED for Gulfstream-scale delta=1280000, got {status!r}"
        )

    def test_succeeded_receipt_contains_correlation_id(self, _mock_store: MagicMock) -> None:
        """SUCCEEDED receipt must include correlation_id (Law #2 traceability)."""
        _invoke(expected_min=5, count_before=0, count_after=10)

        receipt = _stored_receipt(_mock_store)
        assert receipt.get("correlation_id") == CORRELATION_ID, (
            "correlation_id missing or wrong in SUCCEEDED receipt"
        )

    def test_succeeded_receipt_contains_suite_id(self, _mock_store: MagicMock) -> None:
        """SUCCEEDED receipt must include suite_id (Law #6 tenant isolation)."""
        _invoke(expected_min=5, count_before=0, count_after=10)

        receipt = _stored_receipt(_mock_store)
        assert receipt.get("suite_id") == SUITE_ID, (
            "suite_id missing or wrong in SUCCEEDED receipt"
        )


# ---------------------------------------------------------------------------
# Class: TestSucceededWithZeroOutputBranch
# ---------------------------------------------------------------------------

class TestSucceededWithZeroOutputBranch:
    """expected_min == 0 AND delta == 0 -> SUCCEEDED_WITH_ZERO_OUTPUT.

    This represents stages that legitimately write zero rows
    (e.g., legend_extracted on a sheet with no legend elements).
    The distinct status preserves observability without forcing a false FAILED.
    """

    def test_zero_output_emits_distinct_status(self, _mock_store: MagicMock) -> None:
        """expected_min=0, delta=0 -> SUCCEEDED_WITH_ZERO_OUTPUT."""
        _invoke(expected_min=0, count_before=100, count_after=100)

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()
        assert status == "SUCCEEDED_WITH_ZERO_OUTPUT", (
            f"Expected SUCCEEDED_WITH_ZERO_OUTPUT for expected_min=0 delta=0, got {status!r}"
        )

    def test_zero_output_is_not_failed(self, _mock_store: MagicMock) -> None:
        """SUCCEEDED_WITH_ZERO_OUTPUT must not be classified as FAILED."""
        _invoke(expected_min=0, count_before=50, count_after=50)

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()
        assert "FAILED" not in status, (
            f"SUCCEEDED_WITH_ZERO_OUTPUT must not contain FAILED, got {status!r}"
        )

    def test_zero_output_no_error_message(self, _mock_store: MagicMock) -> None:
        """SUCCEEDED_WITH_ZERO_OUTPUT should not carry a failure error_message."""
        _invoke(expected_min=0, count_before=0, count_after=0)

        receipt = _stored_receipt(_mock_store)
        assert not receipt.get("error_message"), (
            "SUCCEEDED_WITH_ZERO_OUTPUT must not have error_message"
        )


# ---------------------------------------------------------------------------
# Class: TestOverDeliveredBranch
# ---------------------------------------------------------------------------

class TestOverDeliveredBranch:
    """expected_min == 0 AND delta > 0 -> SUCCEEDED (over-delivered).

    Stage was advisory (expected_min=0) but actually wrote rows.
    This is valid — emit SUCCEEDED, not SUCCEEDED_WITH_ZERO_OUTPUT.
    """

    def test_over_delivered_emits_succeeded(self, _mock_store: MagicMock) -> None:
        """expected_min=0, delta=5 -> SUCCEEDED (over-delivered)."""
        _invoke(expected_min=0, count_before=10, count_after=15)

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()
        assert status == "SUCCEEDED", (
            f"Expected SUCCEEDED for over-delivered delta=5 with expected_min=0, got {status!r}"
        )

    def test_over_delivered_is_not_zero_output(self, _mock_store: MagicMock) -> None:
        """SUCCEEDED (over-delivered) must NOT be SUCCEEDED_WITH_ZERO_OUTPUT."""
        _invoke(expected_min=0, count_before=0, count_after=3)

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()
        assert "ZERO_OUTPUT" not in status, (
            f"Over-delivered receipt must not be ZERO_OUTPUT, got {status!r}"
        )


# ---------------------------------------------------------------------------
# Class: TestFailedBranch
# ---------------------------------------------------------------------------

class TestFailedBranch:
    """Law #3 (Fail Closed): any case not matching SUCCEEDED or ZERO_OUTPUT -> FAILED."""

    def test_failed_delta_short_of_expected_min(self, _mock_store: MagicMock) -> None:
        """expected_min=10, delta=3 -> FAILED.

        This is the core silent-persistence-loss scenario from Bug #3 in
        Track B — stage returned successfully but persisted too few rows.
        """
        _invoke(expected_min=10, count_before=5, count_after=8)

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()
        assert status == "FAILED", (
            f"Expected FAILED for delta=3 < expected_min=10, got {status!r}"
        )

    def test_failed_error_message_contains_persistence_assertion_tag(
        self, _mock_store: MagicMock
    ) -> None:
        """FAILED receipt error_message must contain 'persistence_assertion' tag (ASCII-safe)."""
        _invoke(expected_min=10, count_before=5, count_after=8)

        receipt = _stored_receipt(_mock_store)
        error_msg: str = receipt.get("error_message", "") or ""
        assert "persistence_assertion" in error_msg, (
            f"error_message must contain 'persistence_assertion', got: {error_msg!r}"
        )

    def test_failed_error_message_contains_actual_delta(self, _mock_store: MagicMock) -> None:
        """FAILED error_message must surface the actual delta value."""
        _invoke(expected_min=10, count_before=5, count_after=8)

        receipt = _stored_receipt(_mock_store)
        error_msg: str = receipt.get("error_message", "") or ""
        # delta = 8 - 5 = 3
        assert "3" in error_msg, (
            f"error_message must contain delta=3, got: {error_msg!r}"
        )

    def test_failed_error_message_contains_expected_min(self, _mock_store: MagicMock) -> None:
        """FAILED error_message must surface expected_min so ops can diagnose."""
        _invoke(expected_min=10, count_before=5, count_after=8)

        receipt = _stored_receipt(_mock_store)
        error_msg: str = receipt.get("error_message", "") or ""
        assert "10" in error_msg, (
            f"error_message must contain expected_min=10, got: {error_msg!r}"
        )

    def test_failed_negative_delta_defensive(self, _mock_store: MagicMock) -> None:
        """expected_min=1, delta=-2 -> FAILED (defensive: rows deleted mid-stage).

        Negative delta should never occur in normal operation, but the helper
        must not crash or silently SUCCEED if it does.
        """
        _invoke(expected_min=1, count_before=10, count_after=8)

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()
        assert status == "FAILED", (
            f"Expected FAILED for negative delta=-2, got {status!r}"
        )

    def test_failed_negative_delta_error_message_ascii_safe(self, _mock_store: MagicMock) -> None:
        """error_message for negative delta must be ASCII-safe (Windows cp1252 encoding)."""
        _invoke(expected_min=1, count_before=10, count_after=8)

        receipt = _stored_receipt(_mock_store)
        error_msg: str = receipt.get("error_message", "") or ""
        # Encoding check — must not raise on cp1252
        try:
            error_msg.encode("cp1252")
        except (UnicodeEncodeError, UnicodeDecodeError) as exc:
            pytest.fail(
                f"error_message is not cp1252-safe (Windows encoding): {exc!r}\n"
                f"message was: {error_msg!r}"
            )

    def test_failed_zero_delta_when_expected_min_nonzero(self, _mock_store: MagicMock) -> None:
        """expected_min=5, delta=0 -> FAILED (RLS silent rejection scenario).

        This covers the case where Supabase's RLS silently rejected every insert,
        returning success from the API while writing zero rows.
        """
        _invoke(expected_min=5, count_before=100, count_after=100)

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()
        assert status == "FAILED", (
            f"Expected FAILED for expected_min=5 delta=0 (RLS silent rejection), got {status!r}"
        )

    def test_failed_zero_delta_error_message_mentions_table(self, _mock_store: MagicMock) -> None:
        """FAILED error_message should reference the expected_table for diagnostics."""
        _invoke(expected_min=5, count_before=100, count_after=100, expected_table=TABLE)

        receipt = _stored_receipt(_mock_store)
        error_msg: str = receipt.get("error_message", "") or ""
        assert TABLE in error_msg, (
            f"error_message must contain table name '{TABLE}', got: {error_msg!r}"
        )

    def test_failed_receipt_contains_suite_id_for_tenant_isolation(
        self, _mock_store: MagicMock
    ) -> None:
        """FAILED receipt must include suite_id (Law #6 — no orphan receipts)."""
        _invoke(expected_min=10, count_before=0, count_after=0)

        receipt = _stored_receipt(_mock_store)
        assert receipt.get("suite_id") == SUITE_ID, (
            "FAILED receipt must include suite_id for tenant isolation"
        )

    def test_failed_receipt_contains_project_id(self, _mock_store: MagicMock) -> None:
        """FAILED receipt must include project_id for traceability."""
        _invoke(expected_min=10, count_before=0, count_after=0)

        receipt = _stored_receipt(_mock_store)
        assert receipt.get("project_id") == PROJECT_ID or (
            receipt.get("redacted_outputs", {}) or {}
        ).get("project_id") == PROJECT_ID, (
            "FAILED receipt must surface project_id in receipt or redacted_outputs"
        )
