"""Synthetic failure-injection tests for persistence assertions (Track B Pass 1.C).

PURPOSE
-------
Simulate the exact production bug that Track B was created to fix:
  A write-producing stage returns successfully (no exception raised) but
  wrote zero rows -- either because:
    (a) Supabase RLS silently rejected every insert
    (b) The insert API returned HTTP 200 with an empty data array
    (c) The Supabase client silently swallowed a constraint violation

BEFORE the fix: receipt status = SUCCEEDED  (silent data loss -- Bug #3)
AFTER the fix:  receipt status = FAILED     (fail-closed, Law #3)

Aspire Laws validated:
  Law #2 (Receipt for All Actions) -- receipt must truthfully reflect what
    was persisted, not just that the function ran without exception.
  Law #3 (Fail Closed) -- zero rows written when >0 expected must produce
    FAILED, not SUCCEEDED.
  Law #6 (Tenant Isolation) -- FAILED receipt must carry suite_id so the
    incident is scoped to the correct tenant.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUITE_ID = str(uuid.UUID("dd000000-0000-0000-0000-000000000001"))
OFFICE_ID = str(uuid.UUID("dd000000-0000-0000-0000-000000000002"))
PROJECT_ID = str(uuid.UUID("dd000000-0000-0000-0000-000000000003"))
CORRELATION_ID = "corr-synthetic-failure-injection-001"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_store(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch store_receipts to capture emitted receipts without real persistence."""
    mock = MagicMock()
    monkeypatch.setattr(
        "aspire_orchestrator.services.receipt_store.store_receipts",
        mock,
    )
    return mock


@pytest.fixture(autouse=True)
def _mock_cut_receipt(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch _cut_receipt to return a controlled stub."""
    def _stub(**kwargs: Any) -> dict[str, Any]:
        receipt = dict(kwargs)
        receipt.setdefault("id", str(uuid.uuid4()))
        return receipt

    monkeypatch.setattr(
        "aspire_orchestrator.services.receipt_store._cut_receipt",
        _stub,
        raising=False,
    )
    return MagicMock(side_effect=_stub)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _stored_receipt(mock_store: MagicMock) -> dict[str, Any]:
    assert mock_store.called, "store_receipts was never called"
    receipts: list[dict[str, Any]] = mock_store.call_args[0][0]
    assert receipts, "store_receipts called with empty list"
    return receipts[0]


def _invoke_assertion(
    *,
    stage: str,
    expected_table: str,
    expected_min: int,
    count_before: int,
    count_after: int,
) -> None:
    from aspire_orchestrator.services.receipt_store import emit_persistence_asserted_receipt  # noqa: PLC0415

    emit_persistence_asserted_receipt(
        stage=stage,
        expected_table=expected_table,
        expected_min=expected_min,
        count_before=count_before,
        count_after=count_after,
        correlation_id=CORRELATION_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        project_id=PROJECT_ID,
        redacted_outputs={},
    )


# ---------------------------------------------------------------------------
# Scenario 1: Supabase returns HTTP 200 with empty insert response
# ---------------------------------------------------------------------------

class TestRLSSilentRejectionScenario:
    """Simulate Supabase RLS silently rejecting all inserts.

    The insert API call succeeds (HTTP 200) but data=[].
    Row count before == row count after.  delta == 0.
    expected_min > 0 because the stage was a write-producing stage.

    BEFORE fix: emit_persistence_asserted_receipt did not exist -- caller
                emitted SUCCEEDED unconditionally.
    AFTER fix:  emit_persistence_asserted_receipt detects delta=0 < expected_min=1
                and emits FAILED.
    """

    def test_rls_silent_rejection_emits_failed(self, _mock_store: MagicMock) -> None:
        """When RLS silently rejects all inserts, receipt must be FAILED not SUCCEEDED.

        Scenario:
          - Stage: drew.see.11g2.page  (expected_min=1)
          - count_before = 0 (no primitives for this project yet)
          - count_after  = 0 (Supabase insert returned 200 but wrote nothing)
          - delta        = 0
          -> assert status == FAILED
        """
        _invoke_assertion(
            stage="drew.see.11g2.page",
            expected_table="blueprint_primitives",
            expected_min=1,
            count_before=0,
            count_after=0,
        )

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()

        assert status == "FAILED", (
            f"RLS silent rejection (delta=0 < expected_min=1) must emit FAILED, "
            f"not {status!r}. This is Bug #3 from Track B -- the fix must catch this."
        )

    def test_rls_silent_rejection_error_message_is_ascii_safe(
        self, _mock_store: MagicMock
    ) -> None:
        """error_message for RLS silent rejection must be ASCII-safe (Windows cp1252)."""
        _invoke_assertion(
            stage="drew.see.11g2.page",
            expected_table="blueprint_primitives",
            expected_min=1,
            count_before=0,
            count_after=0,
        )

        receipt = _stored_receipt(_mock_store)
        error_msg: str = receipt.get("error_message", "") or ""

        try:
            error_msg.encode("cp1252")
        except (UnicodeEncodeError, UnicodeDecodeError) as exc:
            pytest.fail(
                f"error_message is not cp1252-safe (breaks Windows logging): {exc!r}\n"
                f"message was: {error_msg!r}"
            )

    def test_rls_silent_rejection_error_contains_persistence_assertion(
        self, _mock_store: MagicMock
    ) -> None:
        """FAILED receipt from RLS scenario must have 'persistence_assertion' in error_message."""
        _invoke_assertion(
            stage="drew.see.11g2.page",
            expected_table="blueprint_primitives",
            expected_min=1,
            count_before=0,
            count_after=0,
        )

        receipt = _stored_receipt(_mock_store)
        error_msg: str = receipt.get("error_message", "") or ""

        assert "persistence_assertion" in error_msg, (
            f"error_message must contain 'persistence_assertion' tag for ops triage, "
            f"got: {error_msg!r}"
        )


# ---------------------------------------------------------------------------
# Scenario 2: Partial write -- some rows written but fewer than expected_min
# ---------------------------------------------------------------------------

class TestPartialWriteScenario:
    """Simulate a partial write: rows written but fewer than expected_min.

    This can occur when:
      - Supabase rate-limited some inserts mid-batch (HTTP 429)
      - Network partition dropped part of a batch insert
      - Row-level constraint violations on some rows caused partial rollback
    """

    def test_partial_write_emits_failed(self, _mock_store: MagicMock) -> None:
        """delta < expected_min due to partial batch failure -> FAILED.

        Scenario:
          - Stage: blueprint.see.completed  (sanity floor expected_min=100)
          - count_before = 0
          - count_after  = 50  (batch write partially succeeded)
          - delta        = 50 < 100
          -> assert status == FAILED
        """
        _invoke_assertion(
            stage="blueprint.see.completed",
            expected_table="blueprint_primitives",
            expected_min=100,
            count_before=0,
            count_after=50,
        )

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()

        assert status == "FAILED", (
            f"Partial write (delta=50 < expected_min=100) must emit FAILED, got {status!r}"
        )

    def test_partial_write_error_message_shows_actual_delta(
        self, _mock_store: MagicMock
    ) -> None:
        """Partial write FAILED receipt must show actual delta in error_message."""
        _invoke_assertion(
            stage="blueprint.see.completed",
            expected_table="blueprint_primitives",
            expected_min=100,
            count_before=0,
            count_after=50,
        )

        receipt = _stored_receipt(_mock_store)
        error_msg: str = receipt.get("error_message", "") or ""

        assert "50" in error_msg, (
            f"error_message must show actual delta=50 for ops diagnosis, got: {error_msg!r}"
        )


# ---------------------------------------------------------------------------
# Scenario 3: Azure Doc Intel stage writes zero intelligence rows
# ---------------------------------------------------------------------------

class TestAzureDocIntelZeroWriteScenario:
    """Simulate blueprint.ingest.azure_doc_intel_used writing zero rows.

    This corresponds to the stage taxonomy row:
      Stage: blueprint.ingest.azure_doc_intel_used
      Table: blueprint_sheet_intelligence
      expected_min: 1

    If Azure returns a success response but the orchestrator failed to
    persist the result to Supabase (e.g., schema mismatch), count_after
    stays at count_before.
    """

    def test_azure_zero_write_emits_failed(self, _mock_store: MagicMock) -> None:
        """Azure Doc Intel stage that persists 0 rows must emit FAILED."""
        _invoke_assertion(
            stage="blueprint.ingest.azure_doc_intel_used",
            expected_table="blueprint_sheet_intelligence",
            expected_min=1,
            count_before=5,
            count_after=5,  # no new rows written
        )

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()

        assert status == "FAILED", (
            f"Azure Doc Intel with delta=0 (expected_min=1) must emit FAILED, got {status!r}"
        )

    def test_azure_zero_write_receipt_has_suite_id(self, _mock_store: MagicMock) -> None:
        """FAILED Azure Doc Intel receipt must carry suite_id (Law #6)."""
        _invoke_assertion(
            stage="blueprint.ingest.azure_doc_intel_used",
            expected_table="blueprint_sheet_intelligence",
            expected_min=1,
            count_before=5,
            count_after=5,
        )

        receipt = _stored_receipt(_mock_store)
        assert receipt.get("suite_id") == SUITE_ID, (
            f"FAILED receipt must carry suite_id={SUITE_ID} for tenant isolation, "
            f"got {receipt.get('suite_id')!r}"
        )


# ---------------------------------------------------------------------------
# Scenario 4: Legend stage with expected_min=0 should NOT emit FAILED on zero rows
# ---------------------------------------------------------------------------

class TestLegendZeroOutputIsNotFailure:
    """Verify that legitimately-zero-output stages do NOT trip the failure path.

    drew.see.legend_extracted has expected_min=0.
    When a sheet has no legend markers, delta=0 is expected behavior.
    This must emit SUCCEEDED_WITH_ZERO_OUTPUT, NOT FAILED.

    This is the anti-regression for the failure-injection: the fix
    must distinguish between "zero rows when >0 expected" (FAILED) and
    "zero rows when 0 expected" (SUCCEEDED_WITH_ZERO_OUTPUT).
    """

    def test_legend_zero_output_is_not_failed(self, _mock_store: MagicMock) -> None:
        """Legend stage with expected_min=0 and delta=0 must NOT emit FAILED."""
        _invoke_assertion(
            stage="drew.see.legend_extracted",
            expected_table="blueprint_project_legends",
            expected_min=0,
            count_before=10,
            count_after=10,  # legitimate zero output
        )

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()

        assert status != "FAILED", (
            f"drew.see.legend_extracted with expected_min=0 delta=0 must NOT be FAILED, "
            f"got {status!r}. This is a legitimately zero-output stage."
        )

    def test_legend_zero_output_emits_succeeded_with_zero_output(
        self, _mock_store: MagicMock
    ) -> None:
        """Legend stage with expected_min=0 and delta=0 must emit SUCCEEDED_WITH_ZERO_OUTPUT."""
        _invoke_assertion(
            stage="drew.see.legend_extracted",
            expected_table="blueprint_project_legends",
            expected_min=0,
            count_before=10,
            count_after=10,
        )

        receipt = _stored_receipt(_mock_store)
        status = (receipt.get("outcome") or receipt.get("status") or "").upper()

        assert status == "SUCCEEDED_WITH_ZERO_OUTPUT", (
            f"drew.see.legend_extracted with expected_min=0 delta=0 must emit "
            f"SUCCEEDED_WITH_ZERO_OUTPUT, got {status!r}"
        )
