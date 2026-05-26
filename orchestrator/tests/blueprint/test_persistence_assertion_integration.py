"""Integration test scaffold for persistence assertion regression (Track B Pass 1.C).

PURPOSE
-------
Verify that adding emit_persistence_asserted_receipt to all write-producing stages
does NOT break the Gulfstream ingest.  Every stage that previously emitted
SUCCEEDED should still emit SUCCEEDED after the change (count deltas are unchanged).

GULFSTREAM BASELINE (Phase5_v1_validated @ 9ad438e)
----------------------------------------------------
Project:        Gulfstream GAVNN_ADDENDUM_1 + associated PDFs
Total pages:    ~39 (GAVNN_ADDENDUM_1 alone)
Primitives:     ~1,280,000 (1.28M target -- allow +/- 5%)
Sheet count:    varies by run, but > 0 for every bundle

EXPECTED RECEIPT OUTCOMES AFTER CHANGE
---------------------------------------
Stage                               Table                           expected_min  Expected status
-----------------------------       ------------------------------- ------------- ---------------
blueprint.ingest.azure_doc_intel    blueprint_sheet_intelligence    1             SUCCEEDED
drew.see.11g2.page (per page)       blueprint_primitives            1             SUCCEEDED
drew.see.11g2.completed             blueprint_primitives            1             SUCCEEDED
blueprint.see.completed             blueprint_primitives (total)    100           SUCCEEDED
drew.see.legend_extracted           blueprint_project_legends       0             SUCCEEDED or
                                                                                  SUCCEEDED_WITH_ZERO_OUTPUT
drew.see.spatial_binding            symbol_memory / composed_syms   0             SUCCEEDED or
                                                                                  SUCCEEDED_WITH_ZERO_OUTPUT

HOW TO RUN (requires Railway + live Supabase)
---------------------------------------------
    pytest backend/orchestrator/tests/blueprint/ \
        -v -m integration \
        --tb=short

Prerequisites:
    - ASPIRE_SUPABASE_URL set
    - ASPIRE_SUPABASE_SERVICE_ROLE_KEY set
    - Valid Gulfstream project_id in GULFSTREAM_PROJECT_ID env var
    - OR re-trigger ingest via the Drew desktop UI and capture project_id

SPEC AMBIGUITIES (architect to resolve before running live)
------------------------------------------------------------
1. Is count_table_rows async or sync?  The plan shows async in the helper
   signature, but existing receipt_store.py uses sync Supabase calls.
   Tests below assume sync; if async, all callers need `await`.

2. Does emit_persistence_asserted_receipt return a receipt id (str) or
   a ReceiptStatus enum value?  The plan says -> ReceiptStatus, but the
   surrounding store helpers return str.  Tests mock this away but the
   integration test assertions depend on the return type.

3. What is the count_before/count_after unit?  Rows in the table scoped
   to (project_id, suite_id).  Confirm the WHERE clause includes both
   dimensions for Law #6 compliance (covered in test_receipt_count_table_rows.py).
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ASPIRE_SUPABASE_URL"),
        reason="Integration test requires ASPIRE_SUPABASE_URL (live Supabase)",
    ),
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Override with a real project_id from a completed Gulfstream ingest
GULFSTREAM_PROJECT_ID: str = os.environ.get(
    "GULFSTREAM_PROJECT_ID",
    "00000000-0000-0000-0000-000000000000",  # placeholder -- must override
)

GULFSTREAM_SUITE_ID: str = os.environ.get(
    "GULFSTREAM_SUITE_ID",
    "00000000-0000-0000-0000-000000000000",  # placeholder -- must override
)

# Gulfstream known-good primitive count (1.28M ± 5%)
PRIMITIVES_EXPECTED_MIN = 1_216_000   # 1.28M * 0.95
PRIMITIVES_EXPECTED_MAX = 1_344_000   # 1.28M * 1.05


# ---------------------------------------------------------------------------
# Helper: fetch row count from Supabase
# ---------------------------------------------------------------------------

def _count_rows(table: str, project_id: str, suite_id: str) -> int:
    """Fetch row count for (project_id, suite_id) from the live Supabase instance."""
    from aspire_orchestrator.services.receipt_store import count_table_rows  # noqa: PLC0415

    return count_table_rows(table=table, project_id=project_id, suite_id=suite_id)


# ---------------------------------------------------------------------------
# Helper: fetch receipts for project
# ---------------------------------------------------------------------------

def _get_receipts_for_stage(project_id: str, suite_id: str, stage: str) -> list[dict[str, Any]]:
    """Query in-memory receipt store for receipts matching project_id and stage."""
    from aspire_orchestrator.services.receipt_store import query_receipts  # noqa: PLC0415

    return query_receipts(suite_id=suite_id, filters={"project_id": project_id, "action_type": stage})


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

class TestGulfstreamPersistenceRegressionClean:
    """Verify Gulfstream ingest receipts all remain SUCCEEDED after adding assertions.

    These tests REPLAY the receipt audit -- they do not re-ingest the project.
    Pre-condition: the Gulfstream project has already been ingested with the
    new emit_persistence_asserted_receipt helper in place.
    """

    @pytest.mark.integration
    def test_gulfstream_primitives_count_in_expected_range(self) -> None:
        """Gulfstream blueprint_primitives count must be within 5% of 1.28M baseline.

        Asserts the persistence assertions did not change what was written --
        only whether the receipt accurately reports what was written.
        """
        if GULFSTREAM_PROJECT_ID == "00000000-0000-0000-0000-000000000000":
            pytest.skip(
                "Set GULFSTREAM_PROJECT_ID env var to a real completed Gulfstream project"
            )

        count = _count_rows(
            table="blueprint_primitives",
            project_id=GULFSTREAM_PROJECT_ID,
            suite_id=GULFSTREAM_SUITE_ID,
        )

        assert PRIMITIVES_EXPECTED_MIN <= count <= PRIMITIVES_EXPECTED_MAX, (
            f"Gulfstream primitive count {count} outside expected range "
            f"[{PRIMITIVES_EXPECTED_MIN}, {PRIMITIVES_EXPECTED_MAX}]. "
            f"Regression in persistence layer or count helper."
        )

    @pytest.mark.integration
    def test_gulfstream_sheet_intelligence_count_nonzero(self) -> None:
        """blueprint_sheet_intelligence must have rows for every ingested sheet."""
        if GULFSTREAM_PROJECT_ID == "00000000-0000-0000-0000-000000000000":
            pytest.skip("Set GULFSTREAM_PROJECT_ID env var")

        count = _count_rows(
            table="blueprint_sheet_intelligence",
            project_id=GULFSTREAM_PROJECT_ID,
            suite_id=GULFSTREAM_SUITE_ID,
        )

        assert count > 0, (
            f"blueprint_sheet_intelligence count=0 for Gulfstream project -- "
            f"ingest regression or persistence assertion incorrectly failing the stage."
        )

    @pytest.mark.integration
    def test_gulfstream_all_see_page_receipts_are_succeeded(self) -> None:
        """Every drew.see.11g2.page receipt for Gulfstream must be SUCCEEDED.

        After adding persistence assertions, no page receipt that previously
        succeeded should now be FAILED.
        """
        if GULFSTREAM_PROJECT_ID == "00000000-0000-0000-0000-000000000000":
            pytest.skip("Set GULFSTREAM_PROJECT_ID env var")

        receipts = _get_receipts_for_stage(
            project_id=GULFSTREAM_PROJECT_ID,
            suite_id=GULFSTREAM_SUITE_ID,
            stage="drew.see.11g2.page",
        )

        assert len(receipts) > 0, (
            "No drew.see.11g2.page receipts found for Gulfstream -- "
            "ingest may not have run with persistence assertions."
        )

        failed = [
            r for r in receipts
            if (r.get("outcome") or r.get("status") or "").upper() == "FAILED"
        ]
        assert not failed, (
            f"{len(failed)} drew.see.11g2.page receipts are FAILED after assertion "
            f"change -- expected all to remain SUCCEEDED. "
            f"First failure: {failed[0]!r}"
        )

    @pytest.mark.integration
    def test_gulfstream_legend_extracted_receipts_are_succeeded_or_zero_output(self) -> None:
        """drew.see.legend_extracted receipts must be SUCCEEDED or SUCCEEDED_WITH_ZERO_OUTPUT.

        Sheets with no legend markers legitimately emit SUCCEEDED_WITH_ZERO_OUTPUT.
        Neither should be FAILED for a healthy Gulfstream replay.
        """
        if GULFSTREAM_PROJECT_ID == "00000000-0000-0000-0000-000000000000":
            pytest.skip("Set GULFSTREAM_PROJECT_ID env var")

        receipts = _get_receipts_for_stage(
            project_id=GULFSTREAM_PROJECT_ID,
            suite_id=GULFSTREAM_SUITE_ID,
            stage="drew.see.legend_extracted",
        )

        if not receipts:
            pytest.skip(
                "No drew.see.legend_extracted receipts found -- "
                "stage may not have been wired in yet."
            )

        allowed_statuses = {"SUCCEEDED", "SUCCEEDED_WITH_ZERO_OUTPUT"}
        failed = [
            r for r in receipts
            if (r.get("outcome") or r.get("status") or "").upper() not in allowed_statuses
        ]
        assert not failed, (
            f"{len(failed)} drew.see.legend_extracted receipts have unexpected status. "
            f"Expected only: {allowed_statuses}. "
            f"First unexpected: {failed[0]!r}"
        )
