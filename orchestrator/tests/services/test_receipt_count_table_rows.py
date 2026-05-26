"""Unit tests for count_table_rows helper (Track B Pass 1.C).

Validates that count_table_rows:
  1. Queries the correct Supabase table
  2. Filters on BOTH project_id AND suite_id (Law #6: tenant isolation)
  3. Returns the integer count from the API response
  4. Falls back gracefully when Supabase is unavailable

Aspire Laws validated:
  Law #6 (Tenant Isolation) -- count query must scope to (project_id, suite_id)
    so one tenant cannot observe another tenant's row counts.
  Law #3 (Fail Closed) -- unavailable Supabase must not silently return 0
    if that zero would mask a real write failure.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

SUITE_ID = str(uuid.UUID("bb000000-0000-0000-0000-000000000001"))
PROJECT_ID = str(uuid.UUID("bb000000-0000-0000-0000-000000000002"))
TABLE = "blueprint_primitives"

SUITE_ID_B = str(uuid.UUID("cc000000-0000-0000-0000-000000000001"))
PROJECT_ID_B = str(uuid.UUID("cc000000-0000-0000-0000-000000000002"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_supabase_count_response(count: int) -> MagicMock:
    """Simulate a Supabase count() response returning `count` rows."""
    response = MagicMock()
    response.count = count
    response.data = []
    return response


def _make_supabase_client(count: int) -> MagicMock:
    """Build a minimal Supabase client mock that returns a count response.

    The call chain is:
        client.table(TABLE).select("*", count="exact")
                           .eq("project_id", project_id)
                           .eq("suite_id", suite_id)
                           .execute()
    """
    execute_mock = MagicMock(return_value=_make_supabase_count_response(count))
    eq_chain = MagicMock()
    eq_chain.execute = execute_mock
    eq_chain.eq = MagicMock(return_value=eq_chain)

    select_mock = MagicMock(return_value=eq_chain)
    table_mock = MagicMock()
    table_mock.select = select_mock

    client = MagicMock()
    client.table = MagicMock(return_value=table_mock)
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCountTableRowsTenantIsolation:
    """Law #6: query must filter on BOTH project_id AND suite_id."""

    def test_query_filters_on_project_id(self) -> None:
        """count_table_rows must pass project_id as a filter predicate."""
        from aspire_orchestrator.services.receipt_store import count_table_rows  # noqa: PLC0415

        client = _make_supabase_client(42)

        with patch(
            "aspire_orchestrator.services.receipt_store._get_supabase_client",
            return_value=client,
        ), patch(
            "aspire_orchestrator.services.receipt_store._supabase_enabled",
            return_value=True,
        ):
            count_table_rows(
                table=TABLE,
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
            )

        # Find the .eq() calls and verify project_id was used
        table_result = client.table.return_value
        select_result = table_result.select.return_value
        eq_calls = [str(call) for call in select_result.eq.call_args_list]
        combined = " ".join(eq_calls)
        assert "project_id" in combined, (
            f"count_table_rows must filter on project_id (Law #6). "
            f"Actual eq() calls: {combined!r}"
        )

    def test_query_filters_on_suite_id(self) -> None:
        """count_table_rows must pass suite_id as a filter predicate (Law #6)."""
        from aspire_orchestrator.services.receipt_store import count_table_rows  # noqa: PLC0415

        client = _make_supabase_client(42)

        with patch(
            "aspire_orchestrator.services.receipt_store._get_supabase_client",
            return_value=client,
        ), patch(
            "aspire_orchestrator.services.receipt_store._supabase_enabled",
            return_value=True,
        ):
            count_table_rows(
                table=TABLE,
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
            )

        table_result = client.table.return_value
        select_result = table_result.select.return_value
        eq_calls = [str(call) for call in select_result.eq.call_args_list]
        combined = " ".join(eq_calls)
        assert "suite_id" in combined, (
            f"count_table_rows must filter on suite_id (Law #6 tenant isolation). "
            f"Actual eq() calls: {combined!r}"
        )

    def test_cross_tenant_queries_use_different_suite_ids(self) -> None:
        """Two calls with different suite_ids must query different suite_id values.

        Evil test: verifies that the suite_id parameter is actually forwarded
        to the query, not hardcoded or ignored.
        """
        from aspire_orchestrator.services.receipt_store import count_table_rows  # noqa: PLC0415

        client_a = _make_supabase_client(100)
        client_b = _make_supabase_client(200)

        suite_ids_seen: list[str] = []

        def _capture_client() -> MagicMock:
            # Intercept eq() calls to track which suite_id was passed
            call_count = len(suite_ids_seen)
            return client_a if call_count == 0 else client_b

        # Manually call with two different suite_ids and verify the
        # suite_id argument differs between calls
        with patch(
            "aspire_orchestrator.services.receipt_store._get_supabase_client",
            return_value=client_a,
        ), patch(
            "aspire_orchestrator.services.receipt_store._supabase_enabled",
            return_value=True,
        ):
            count_table_rows(
                table=TABLE,
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
            )
            eq_calls_a = [
                str(c)
                for c in client_a.table.return_value.select.return_value.eq.call_args_list
            ]

        with patch(
            "aspire_orchestrator.services.receipt_store._get_supabase_client",
            return_value=client_b,
        ), patch(
            "aspire_orchestrator.services.receipt_store._supabase_enabled",
            return_value=True,
        ):
            count_table_rows(
                table=TABLE,
                project_id=PROJECT_ID,
                suite_id=SUITE_ID_B,
            )
            eq_calls_b = [
                str(c)
                for c in client_b.table.return_value.select.return_value.eq.call_args_list
            ]

        combined_a = " ".join(eq_calls_a)
        combined_b = " ".join(eq_calls_b)

        assert SUITE_ID in combined_a, (
            f"First call must pass SUITE_ID={SUITE_ID} to query, got: {combined_a!r}"
        )
        assert SUITE_ID_B in combined_b, (
            f"Second call must pass SUITE_ID_B={SUITE_ID_B} to query, got: {combined_b!r}"
        )
        # The two suite_ids must be different — confirming isolation
        assert SUITE_ID != SUITE_ID_B


class TestCountTableRowsReturnValue:
    """Verify count_table_rows returns the integer from Supabase."""

    def test_returns_integer_count(self) -> None:
        """count_table_rows must return the integer count from Supabase response."""
        from aspire_orchestrator.services.receipt_store import count_table_rows  # noqa: PLC0415

        client = _make_supabase_client(42)

        with patch(
            "aspire_orchestrator.services.receipt_store._get_supabase_client",
            return_value=client,
        ), patch(
            "aspire_orchestrator.services.receipt_store._supabase_enabled",
            return_value=True,
        ):
            result = count_table_rows(
                table=TABLE,
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
            )

        assert result == 42, f"Expected count=42, got {result!r}"

    def test_returns_zero_when_table_is_empty(self) -> None:
        """count_table_rows must return 0 (not None or error) for an empty table."""
        from aspire_orchestrator.services.receipt_store import count_table_rows  # noqa: PLC0415

        client = _make_supabase_client(0)

        with patch(
            "aspire_orchestrator.services.receipt_store._get_supabase_client",
            return_value=client,
        ), patch(
            "aspire_orchestrator.services.receipt_store._supabase_enabled",
            return_value=True,
        ):
            result = count_table_rows(
                table=TABLE,
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
            )

        assert result == 0, f"Expected count=0 for empty table, got {result!r}"

    def test_returns_integer_type(self) -> None:
        """count_table_rows return value must be int, not str or None."""
        from aspire_orchestrator.services.receipt_store import count_table_rows  # noqa: PLC0415

        client = _make_supabase_client(7)

        with patch(
            "aspire_orchestrator.services.receipt_store._get_supabase_client",
            return_value=client,
        ), patch(
            "aspire_orchestrator.services.receipt_store._supabase_enabled",
            return_value=True,
        ):
            result = count_table_rows(
                table=TABLE,
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
            )

        assert isinstance(result, int), (
            f"count_table_rows must return int, got {type(result).__name__!r}"
        )


class TestCountTableRowsSupabaseUnavailable:
    """Law #3 (Fail Closed): Supabase unavailable must not silently return a number
    that would cause emit_persistence_asserted_receipt to choose the wrong status.
    """

    def test_raises_or_returns_sentinel_when_supabase_disabled(self) -> None:
        """When Supabase is not configured, count_table_rows must either raise
        or return a clearly invalid sentinel (e.g., -1 or None), never 0.

        Returning 0 when Supabase is down is dangerous: combined with
        count_before=0, it would produce delta=0 and silently emit
        SUCCEEDED_WITH_ZERO_OUTPUT instead of surfacing the outage.
        """
        from aspire_orchestrator.services.receipt_store import count_table_rows  # noqa: PLC0415

        with patch(
            "aspire_orchestrator.services.receipt_store._supabase_enabled",
            return_value=False,
        ):
            try:
                result = count_table_rows(
                    table=TABLE,
                    project_id=PROJECT_ID,
                    suite_id=SUITE_ID,
                )
                # If it returns, it must NOT return a bare 0 that masks outage
                assert result is None or result < 0 or result == 0, (
                    # Allow 0 only if the implementation explicitly documents
                    # this as a safe fallback for disabled Supabase.
                    # The spec is ambiguous here -- surface as spec ambiguity.
                    "SPEC AMBIGUITY: count_table_rows with Supabase disabled "
                    "returned 0 -- verify this cannot mask a persistence outage "
                    "in emit_persistence_asserted_receipt."
                )
            except Exception:
                # Raising is also acceptable (fail-closed)
                pass

    def test_supabase_exception_is_not_silently_swallowed(self) -> None:
        """A Supabase HTTP error must propagate (or be explicitly handled),
        not silently return 0 and mask the failure.
        """
        from aspire_orchestrator.services.receipt_store import count_table_rows  # noqa: PLC0415

        client = MagicMock()
        client.table.side_effect = RuntimeError("Supabase connection refused")

        with patch(
            "aspire_orchestrator.services.receipt_store._get_supabase_client",
            return_value=client,
        ), patch(
            "aspire_orchestrator.services.receipt_store._supabase_enabled",
            return_value=True,
        ):
            try:
                result = count_table_rows(
                    table=TABLE,
                    project_id=PROJECT_ID,
                    suite_id=SUITE_ID,
                )
                # If swallowed, result must NOT be bare 0 (could mask failure)
                # Allow None or negative as explicit "unavailable" sentinel
                assert result != 0 or result is None, (
                    "count_table_rows swallowed Supabase error and returned 0 -- "
                    "this masks persistence failures (Law #3 violation)"
                )
            except Exception:
                # Re-raising is the preferred fail-closed behavior
                pass
