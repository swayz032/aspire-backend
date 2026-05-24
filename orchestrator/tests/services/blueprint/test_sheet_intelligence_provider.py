"""Wave 20e: unit tests for SheetIntelligenceProvider.

Pure unit tests — no real Supabase. All DB calls patched.

Tests:
  1. load_all — batched query, rows indexed by sheet_id
  2. legends with discipline filter
  3. legends without discipline filter (all entries)
  4. schedules with schedule_type filter
  5. notes — all general-notes paragraphs
  6. tables_on_sheet — per-sheet accessor
  7. paragraphs_on_sheet — per-sheet accessor
  8. Suite isolation evil test — cross-tenant query returns 0 rows
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.services.blueprint.sheet_intelligence_provider import (
    SheetIntelligenceProvider,
)

_SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
_SUITE_OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_PROJECT = "5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2"
_SHEET_A = "11111111-0000-0000-0000-000000000001"
_SHEET_B = "22222222-0000-0000-0000-000000000002"


def _make_row(
    sheet_id: str,
    *,
    tables: list | None = None,
    paragraphs: list | None = None,
    regions: dict | None = None,
) -> dict:
    return {
        "id": "row-uuid",
        "sheet_id": sheet_id,
        "suite_id": _SUITE,
        "project_id": _PROJECT,
        "tables": tables or [],
        "paragraphs": paragraphs or [],
        "key_value_pairs": [],
        "regions": regions or {},
    }


# ---------------------------------------------------------------------------
# Test 1: load_all — batched query, rows indexed by sheet_id
# ---------------------------------------------------------------------------

class TestLoadAll:
    @pytest.mark.asyncio
    async def test_load_all_indexes_by_sheet_id(self) -> None:
        rows = [
            _make_row(_SHEET_A, tables=[{"headers": ["A"], "rows": [["1"]]}]),
            _make_row(_SHEET_B, tables=[{"headers": ["B"], "rows": [["2"], ["3"]]}]),
        ]
        with patch(
            "aspire_orchestrator.services.blueprint.sheet_intelligence_provider.supabase_select",
            new_callable=AsyncMock,
            return_value=rows,
        ):
            provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
            await provider.load_all()

        assert provider.cache_stats()["sheets_cached"] == 2
        assert _SHEET_A in provider._rows
        assert _SHEET_B in provider._rows

    @pytest.mark.asyncio
    async def test_load_all_idempotent(self) -> None:
        """Second load_all call is a no-op (no second DB hit)."""
        rows = [_make_row(_SHEET_A)]
        call_count = 0

        async def _fake_select(*a, **kw):
            nonlocal call_count
            call_count += 1
            return rows

        with patch(
            "aspire_orchestrator.services.blueprint.sheet_intelligence_provider.supabase_select",
            side_effect=_fake_select,
        ):
            provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
            await provider.load_all()
            await provider.load_all()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_load_all_degrades_on_db_failure(self) -> None:
        """DB failure logs a warning and leaves cache empty — no raise."""
        with patch(
            "aspire_orchestrator.services.blueprint.sheet_intelligence_provider.supabase_select",
            side_effect=RuntimeError("db down"),
        ):
            provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
            await provider.load_all()  # Must not raise

        assert provider.cache_stats()["sheets_cached"] == 0
        assert provider._loaded is True


# ---------------------------------------------------------------------------
# Test 2 & 3: legends with / without discipline filter
# ---------------------------------------------------------------------------

class TestLegends:
    def _provider_with_rows(self) -> SheetIntelligenceProvider:
        provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
        provider._rows = {
            _SHEET_A: _make_row(_SHEET_A, regions={
                "legend": [
                    {"discipline": "mechanical", "label": "AHU-1"},
                    {"discipline": "electrical", "label": "PANEL-A"},
                ]
            }),
            _SHEET_B: _make_row(_SHEET_B, regions={
                "legend": [
                    {"discipline": "mechanical", "label": "RTU-2"},
                ]
            }),
        }
        provider._loaded = True
        return provider

    @pytest.mark.asyncio
    async def test_legends_no_filter_returns_all(self) -> None:
        provider = self._provider_with_rows()
        results = await provider.legends()
        assert len(results) == 3
        # Every entry gets sheet_id injected.
        assert all("sheet_id" in r for r in results)

    @pytest.mark.asyncio
    async def test_legends_discipline_filter(self) -> None:
        provider = self._provider_with_rows()
        results = await provider.legends(discipline="mechanical")
        assert len(results) == 2
        assert all(r["discipline"] == "mechanical" for r in results)

    @pytest.mark.asyncio
    async def test_legends_discipline_filter_case_insensitive(self) -> None:
        provider = self._provider_with_rows()
        results = await provider.legends(discipline="ELECTRICAL")
        assert len(results) == 1
        assert results[0]["label"] == "PANEL-A"

    @pytest.mark.asyncio
    async def test_legends_empty_when_no_legend_region(self) -> None:
        provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
        provider._rows = {_SHEET_A: _make_row(_SHEET_A)}
        provider._loaded = True
        results = await provider.legends()
        assert results == []


# ---------------------------------------------------------------------------
# Test 4: schedules with schedule_type filter
# ---------------------------------------------------------------------------

class TestSchedules:
    def _provider_with_rows(self) -> SheetIntelligenceProvider:
        provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
        provider._rows = {
            _SHEET_A: _make_row(_SHEET_A, regions={
                "schedule": [
                    {"schedule_type": "DOOR", "count": 12},
                    {"schedule_type": "WINDOW", "count": 8},
                ]
            }),
        }
        provider._loaded = True
        return provider

    @pytest.mark.asyncio
    async def test_schedules_type_filter(self) -> None:
        provider = self._provider_with_rows()
        results = await provider.schedules(schedule_type="DOOR")
        assert len(results) == 1
        assert results[0]["count"] == 12

    @pytest.mark.asyncio
    async def test_schedules_no_filter_returns_all(self) -> None:
        provider = self._provider_with_rows()
        results = await provider.schedules()
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_schedules_type_filter_case_insensitive(self) -> None:
        provider = self._provider_with_rows()
        results = await provider.schedules(schedule_type="window")
        assert len(results) == 1
        assert results[0]["count"] == 8


# ---------------------------------------------------------------------------
# Test 5: notes
# ---------------------------------------------------------------------------

class TestNotes:
    @pytest.mark.asyncio
    async def test_notes_returned_across_sheets(self) -> None:
        provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
        provider._rows = {
            _SHEET_A: _make_row(_SHEET_A, regions={
                "notes": [{"text": "General note A", "role": "general"}]
            }),
            _SHEET_B: _make_row(_SHEET_B, regions={
                "notes": [
                    {"text": "Note B1", "role": "general"},
                    {"text": "Note B2", "role": "spec"},
                ]
            }),
        }
        provider._loaded = True
        results = await provider.notes()
        assert len(results) == 3
        assert all("sheet_id" in r for r in results)

    @pytest.mark.asyncio
    async def test_notes_empty_when_no_region(self) -> None:
        provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
        provider._rows = {_SHEET_A: _make_row(_SHEET_A)}
        provider._loaded = True
        results = await provider.notes()
        assert results == []


# ---------------------------------------------------------------------------
# Test 6: tables_on_sheet
# ---------------------------------------------------------------------------

class TestTablesOnSheet:
    @pytest.mark.asyncio
    async def test_tables_on_sheet_returns_cached(self) -> None:
        tables = [
            {"headers": ["Tag", "CFM"], "rows": [["AHU-1", "5000"]], "region_type": "schedule"},
        ]
        provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
        provider._rows = {_SHEET_A: _make_row(_SHEET_A, tables=tables)}
        provider._loaded = True

        result = await provider.tables_on_sheet(_SHEET_A)
        assert len(result) == 1
        assert result[0]["region_type"] == "schedule"

    @pytest.mark.asyncio
    async def test_tables_on_sheet_empty_on_miss(self) -> None:
        provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
        provider._rows = {}
        provider._loaded = True

        result = await provider.tables_on_sheet(_SHEET_A)
        assert result == []

    @pytest.mark.asyncio
    async def test_tables_on_sheet_empty_string_returns_empty(self) -> None:
        provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
        provider._loaded = True
        result = await provider.tables_on_sheet("")
        assert result == []


# ---------------------------------------------------------------------------
# Test 7: paragraphs_on_sheet
# ---------------------------------------------------------------------------

class TestParagraphsOnSheet:
    @pytest.mark.asyncio
    async def test_paragraphs_on_sheet_returns_cached(self) -> None:
        paragraphs = [
            {"text": "1. All work per IBC 2021.", "role": "spec"},
            {"text": "2. Contractor to verify dims.", "role": "general"},
        ]
        provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
        provider._rows = {_SHEET_B: _make_row(_SHEET_B, paragraphs=paragraphs)}
        provider._loaded = True

        result = await provider.paragraphs_on_sheet(_SHEET_B)
        assert len(result) == 2
        assert result[0]["role"] == "spec"

    @pytest.mark.asyncio
    async def test_paragraphs_on_sheet_miss_returns_empty(self) -> None:
        provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
        provider._rows = {}
        provider._loaded = True
        result = await provider.paragraphs_on_sheet(_SHEET_A)
        assert result == []


# ---------------------------------------------------------------------------
# Test 8: Suite isolation — evil test (cross-tenant returns 0 rows)
# ---------------------------------------------------------------------------

class TestSuiteIsolation:
    @pytest.mark.asyncio
    async def test_cross_tenant_query_returns_zero_rows(self) -> None:
        """Provider for tenant A must return only rows for suite A.

        Simulates a DB that returns the correct suite_id filter results
        (Postgres RLS + explicit filter). The filter string passed to
        supabase_select must include suite_id=eq.<suite_a>; a query
        with suite_b would return empty.
        """
        suite_a_rows = [_make_row(_SHEET_A)]

        captured_filters: list[str] = []

        async def _fake_select(table, filters, **kw):
            captured_filters.append(filters)
            # Only return rows when the correct suite is queried.
            if _SUITE in filters:
                return suite_a_rows
            return []  # Cross-tenant → empty

        with patch(
            "aspire_orchestrator.services.blueprint.sheet_intelligence_provider.supabase_select",
            side_effect=_fake_select,
        ):
            provider_a = SheetIntelligenceProvider(_PROJECT, _SUITE)
            await provider_a.load_all()

            provider_b = SheetIntelligenceProvider(_PROJECT, _SUITE_OTHER)
            await provider_b.load_all()

        # Suite A provider got its rows.
        assert provider_a.cache_stats()["sheets_cached"] == 1
        # Suite B provider got nothing (cross-tenant filter returned empty).
        assert provider_b.cache_stats()["sheets_cached"] == 0
        # Verify the filter included the correct suite_id each time.
        assert any(_SUITE in f for f in captured_filters)
        assert any(_SUITE_OTHER in f for f in captured_filters)

    def test_construction_fails_on_empty_suite_id(self) -> None:
        """Fail closed — empty suite_id raises at construction (Law #3)."""
        with pytest.raises(ValueError, match="suite_id"):
            SheetIntelligenceProvider(_PROJECT, "")

    def test_construction_fails_on_empty_project_id(self) -> None:
        with pytest.raises(ValueError, match="project_id"):
            SheetIntelligenceProvider("", _SUITE)
