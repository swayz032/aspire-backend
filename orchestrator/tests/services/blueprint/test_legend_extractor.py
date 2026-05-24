"""Wave 20.6.1a: unit tests for legend_extractor.

Pure unit tests — no real Supabase. All DB calls patched.

Tests:
  1.  Happy path — architectural discipline from DB enum
  2.  Happy path — electrical discipline from sheet_number prefix
  3.  Happy path — fire_protection via FP prefix
  4.  Multiple legend regions on one sheet (3 regions → sum of symbols)
  5.  Empty legend regions on all sheets → 0 symbols, receipt still emitted
  6.  Idempotent upsert — second call with same rows → duplicates counted
  7.  Suite isolation evil test — cross-tenant provider sees 0 rows
  8.  Receipt emission — every run produces a receipt
  9.  Missing table_index in region → skipped gracefully
  10. table_index out of range → skipped gracefully (no IndexError)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from aspire_orchestrator.services.blueprint.legend_extractor import (
    _detect_discipline,
    _parse_legend_table,
    extract_project_legends,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------
_SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
_SUITE_OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_PROJECT = "7a93d144-5d53-47d3-8b97-067ebe249d88"
_SHEET_A = "11111111-0000-0000-0000-000000000001"
_SHEET_B = "22222222-0000-0000-0000-000000000002"
_CORR = "wave-20-6-1-test"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_table(rows: list[list[str]], *, bbox: list | None = None) -> dict:
    return {
        "bbox": bbox or [0, 0, 100, 0, 100, 50, 0, 50],
        "headers": ["MARK", "DESCRIPTION", "SPEC"],
        "rows": rows,
        "region_type": "legend",
    }


def _make_intel_row(
    sheet_id: str,
    *,
    tables: list | None = None,
    legend_regions: list | None = None,
) -> dict:
    regions: dict = {}
    if legend_regions is not None:
        regions["legend"] = legend_regions
    return {
        "id": "intel-uuid",
        "sheet_id": sheet_id,
        "suite_id": _SUITE,
        "project_id": _PROJECT,
        "tables": tables or [],
        "paragraphs": [],
        "regions": regions,
    }


def _make_sheet_row(sheet_id: str, *, sheet_number: str = "A-101", discipline: str | None = "A") -> dict:
    return {
        "id": sheet_id,
        "suite_id": _SUITE,
        "project_id": _PROJECT,
        "sheet_number": sheet_number,
        "discipline": discipline,
    }


async def _fake_select_factory(
    intel_rows: list[dict],
    sheet_rows: list[dict],
):
    """Return an async callable that routes by table name."""

    async def _fake_select(table: str, filters: str, **kw) -> list[dict]:
        if table == "blueprint_sheet_intelligence":
            # Honour suite_id isolation — only return rows matching suite in filter
            if _SUITE not in filters and _SUITE_OTHER not in filters:
                return intel_rows
            if _SUITE_OTHER in filters:
                return []
            return intel_rows
        if table == "blueprint_sheets":
            return sheet_rows
        return []

    return _fake_select


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------

class TestDetectDiscipline:
    def test_from_db_enum_architectural(self) -> None:
        assert _detect_discipline(None, "A") == "architectural"

    def test_from_db_enum_fire_protection(self) -> None:
        assert _detect_discipline(None, "FP") == "fire_protection"

    def test_from_sheet_number_prefix_electrical(self) -> None:
        assert _detect_discipline("E-101", None) == "electrical"

    def test_from_sheet_number_prefix_fp(self) -> None:
        assert _detect_discipline("FP-5", None) == "fire_protection"

    def test_fallback_unknown(self) -> None:
        assert _detect_discipline(None, None) == "unknown"

    def test_db_enum_takes_priority_over_sheet_number(self) -> None:
        # Sheet number says electrical but DB says plumbing — DB wins.
        assert _detect_discipline("E-101", "P") == "plumbing"


class TestParseLegendTable:
    def test_basic_three_column_row(self) -> None:
        tbl = _make_table([
            ["QR", "QR Pendant V2708", "120V", "Ceiling"],
        ])
        result = _parse_legend_table(tbl)
        assert len(result) == 1
        assert result[0]["symbol_id"] == "QR"
        assert result[0]["label"] == "QR Pendant V2708"
        assert "120V" in result[0]["specification"]

    def test_empty_symbol_id_row_skipped(self) -> None:
        tbl = _make_table([
            ["", "ignored row", ""],
            ["P-1", "Pump 1", "2 HP"],
        ])
        result = _parse_legend_table(tbl)
        assert len(result) == 1
        assert result[0]["symbol_id"] == "P-1"

    def test_header_row_with_only_whitespace_skipped(self) -> None:
        tbl = _make_table([
            ["   ", "DESCRIPTION", "SPEC"],
            ["AHU-1", "Air Handling Unit", "10 TON"],
        ])
        result = _parse_legend_table(tbl)
        assert len(result) == 1

    def test_single_column_row(self) -> None:
        tbl = _make_table([["SSP"]])
        result = _parse_legend_table(tbl)
        assert result[0]["symbol_id"] == "SSP"
        assert result[0]["label"] is None
        assert result[0]["specification"] is None

    def test_empty_table(self) -> None:
        tbl = _make_table([])
        assert _parse_legend_table(tbl) == []

    def test_bbox_carried_to_representation(self) -> None:
        bbox = [10, 20, 200, 20, 200, 80, 10, 80]
        tbl = _make_table([["X-1", "Symbol X", "Spec"]], bbox=bbox)
        result = _parse_legend_table(tbl)
        assert result[0]["representation_bbox"] == bbox


# ---------------------------------------------------------------------------
# Integration-style tests (Supabase calls patched)
# ---------------------------------------------------------------------------

class TestExtractProjectLegendsHappyPath:
    @pytest.mark.asyncio
    async def test_architectural_discipline_from_db_enum(self) -> None:
        """Single sheet, architectural, 3 legend symbols → 3 upsert rows."""
        table = _make_table([
            ["001", "MOTHER'S ROOM", "3'-0\"", "7'-0\""],
            ["002", "OFFICE", "3'-0\"", "7'-0\""],
            ["003", "CORRIDOR", "3'-0\"", "7'-0\""],
        ])
        intel_row = _make_intel_row(
            _SHEET_A,
            tables=[table],
            legend_regions=[{"bbox": [0, 0, 100, 100], "table_index": 0}],
        )
        sheet_row = _make_sheet_row(_SHEET_A, sheet_number="A-15", discipline="A")
        fake_select = await _fake_select_factory([intel_row], [sheet_row])

        inserted_rows = [{"id": str(i)} for i in range(3)]
        with (
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_select",
                side_effect=fake_select,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_insert_batch_ignore_conflicts",
                new_callable=AsyncMock,
                return_value=(inserted_rows, 0),
            ) as mock_upsert,
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.store_receipts",
            ),
        ):
            result = await extract_project_legends(
                project_id=_PROJECT,
                suite_id=_SUITE,
                office_id=None,
                correlation_id=_CORR,
            )

        assert result["legends_processed"] == 1
        assert result["symbols_extracted"] == 3
        assert "architectural" in result["disciplines_covered"]

        # Verify all 3 rows in upsert payload have discipline=architectural
        upsert_payload = mock_upsert.call_args[0][1]
        assert all(r["discipline"] == "architectural" for r in upsert_payload)

    @pytest.mark.asyncio
    async def test_electrical_discipline_from_sheet_number_prefix(self) -> None:
        """Sheet number E-101 → electrical discipline when DB discipline is None."""
        table = _make_table([
            ["L1", "DUPLEX RECEPTACLE", "120V 20A"],
            ["L2", "GFCI RECEPTACLE", "120V 20A"],
        ])
        intel_row = _make_intel_row(
            _SHEET_A,
            tables=[table],
            legend_regions=[{"bbox": [0, 0, 100, 100], "table_index": 0}],
        )
        sheet_row = _make_sheet_row(_SHEET_A, sheet_number="E-101", discipline=None)
        fake_select = await _fake_select_factory([intel_row], [sheet_row])

        with (
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_select",
                side_effect=fake_select,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_insert_batch_ignore_conflicts",
                new_callable=AsyncMock,
                return_value=([{"id": "x"}, {"id": "y"}], 0),
            ) as mock_upsert,
            patch("aspire_orchestrator.services.blueprint.legend_extractor.store_receipts"),
        ):
            result = await extract_project_legends(
                project_id=_PROJECT,
                suite_id=_SUITE,
                office_id=None,
                correlation_id=_CORR,
            )

        assert "electrical" in result["disciplines_covered"]
        upsert_payload = mock_upsert.call_args[0][1]
        assert all(r["discipline"] == "electrical" for r in upsert_payload)

    @pytest.mark.asyncio
    async def test_fire_protection_fp_prefix(self) -> None:
        """Sheet number FP-1 → fire_protection (prefix check: FP before F)."""
        table = _make_table([["SH-1", "SPRINKLER HEAD", "K=5.6"]])
        intel_row = _make_intel_row(
            _SHEET_A,
            tables=[table],
            legend_regions=[{"bbox": [], "table_index": 0}],
        )
        sheet_row = _make_sheet_row(_SHEET_A, sheet_number="FP-1", discipline=None)
        fake_select = await _fake_select_factory([intel_row], [sheet_row])

        with (
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_select",
                side_effect=fake_select,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_insert_batch_ignore_conflicts",
                new_callable=AsyncMock,
                return_value=([{"id": "z"}], 0),
            ) as mock_upsert,
            patch("aspire_orchestrator.services.blueprint.legend_extractor.store_receipts"),
        ):
            result = await extract_project_legends(
                project_id=_PROJECT,
                suite_id=_SUITE,
                office_id=None,
                correlation_id=_CORR,
            )

        assert "fire_protection" in result["disciplines_covered"]

    @pytest.mark.asyncio
    async def test_multiple_legend_regions_on_one_sheet(self) -> None:
        """Sheet with 3 legend regions each pointing to a separate table."""
        tables = [
            _make_table([["D1", "DOOR TYPE 1", "3'-0\""]]),
            _make_table([["W1", "WINDOW TYPE A", "2'-0\""], ["W2", "WINDOW TYPE B", "3'-0\""]]),
            _make_table([["F1", "FLOOR FINISH", "TILE"], ["F2", "CARPET", "LEVEL LOOP"]]),
        ]
        intel_row = _make_intel_row(
            _SHEET_A,
            tables=tables,
            legend_regions=[
                {"bbox": [], "table_index": 0},
                {"bbox": [], "table_index": 1},
                {"bbox": [], "table_index": 2},
            ],
        )
        sheet_row = _make_sheet_row(_SHEET_A, sheet_number="A-15", discipline="A")
        fake_select = await _fake_select_factory([intel_row], [sheet_row])

        all_rows = [{"id": str(i)} for i in range(5)]
        with (
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_select",
                side_effect=fake_select,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_insert_batch_ignore_conflicts",
                new_callable=AsyncMock,
                return_value=(all_rows, 0),
            ),
            patch("aspire_orchestrator.services.blueprint.legend_extractor.store_receipts"),
        ):
            result = await extract_project_legends(
                project_id=_PROJECT,
                suite_id=_SUITE,
                office_id=None,
                correlation_id=_CORR,
            )

        # 3 regions, 1+2+2 = 5 symbols total
        assert result["legends_processed"] == 3
        assert result["symbols_extracted"] == 5


class TestExtractProjectLegendsEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_legend_regions_returns_zero(self) -> None:
        """No legend regions on any sheet → 0 symbols, function returns cleanly."""
        intel_row = _make_intel_row(_SHEET_A)  # no legend_regions
        sheet_row = _make_sheet_row(_SHEET_A)
        fake_select = await _fake_select_factory([intel_row], [sheet_row])

        with (
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_select",
                side_effect=fake_select,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_insert_batch_ignore_conflicts",
                new_callable=AsyncMock,
            ) as mock_upsert,
            patch("aspire_orchestrator.services.blueprint.legend_extractor.store_receipts") as mock_receipt,
        ):
            result = await extract_project_legends(
                project_id=_PROJECT,
                suite_id=_SUITE,
                office_id=None,
                correlation_id=_CORR,
            )

        assert result["legends_processed"] == 0
        assert result["symbols_extracted"] == 0
        mock_upsert.assert_not_called()
        # Receipt must still be emitted (Law #2)
        mock_receipt.assert_called_once()

    @pytest.mark.asyncio
    async def test_idempotent_upsert_duplicates_counted(self) -> None:
        """Second call with same data → all rows counted as duplicates, none raised."""
        table = _make_table([["AHU-1", "Air Handler", "10 TON"]])
        intel_row = _make_intel_row(
            _SHEET_A,
            tables=[table],
            legend_regions=[{"bbox": [], "table_index": 0}],
        )
        sheet_row = _make_sheet_row(_SHEET_A, sheet_number="M-101", discipline="M")
        fake_select = await _fake_select_factory([intel_row], [sheet_row])

        # Simulate second run: 0 inserted, 1 duplicate
        with (
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_select",
                side_effect=fake_select,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_insert_batch_ignore_conflicts",
                new_callable=AsyncMock,
                return_value=([], 1),  # 0 inserted, 1 duplicate
            ),
            patch("aspire_orchestrator.services.blueprint.legend_extractor.store_receipts"),
        ):
            result = await extract_project_legends(
                project_id=_PROJECT,
                suite_id=_SUITE,
                office_id=None,
                correlation_id=_CORR,
            )

        assert result["symbols_extracted"] == 1
        assert result["symbols_inserted"] == 0
        assert result["symbols_duplicate"] == 1

    @pytest.mark.asyncio
    async def test_missing_table_index_in_region_skipped(self) -> None:
        """Region without table_index key → skipped, no crash, other regions processed."""
        table = _make_table([["V-1", "VAV BOX", "10x10"]])
        intel_row = _make_intel_row(
            _SHEET_A,
            tables=[table],
            legend_regions=[
                {"bbox": []},                           # missing table_index → skip
                {"bbox": [], "table_index": 0},         # valid
            ],
        )
        sheet_row = _make_sheet_row(_SHEET_A, sheet_number="M-5", discipline="M")
        fake_select = await _fake_select_factory([intel_row], [sheet_row])

        with (
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_select",
                side_effect=fake_select,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_insert_batch_ignore_conflicts",
                new_callable=AsyncMock,
                return_value=([{"id": "x"}], 0),
            ) as mock_upsert,
            patch("aspire_orchestrator.services.blueprint.legend_extractor.store_receipts"),
        ):
            result = await extract_project_legends(
                project_id=_PROJECT,
                suite_id=_SUITE,
                office_id=None,
                correlation_id=_CORR,
            )

        # Only 1 region was processable (the one with table_index)
        assert result["legends_processed"] == 1
        assert result["symbols_extracted"] == 1

    @pytest.mark.asyncio
    async def test_table_index_out_of_range_skipped(self) -> None:
        """Region with table_index pointing beyond tables list → skipped, no IndexError."""
        table = _make_table([["X-1", "Symbol", "Spec"]])
        intel_row = _make_intel_row(
            _SHEET_A,
            tables=[table],
            legend_regions=[
                {"bbox": [], "table_index": 99},  # out of range → skip
            ],
        )
        sheet_row = _make_sheet_row(_SHEET_A)
        fake_select = await _fake_select_factory([intel_row], [sheet_row])

        with (
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_select",
                side_effect=fake_select,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_insert_batch_ignore_conflicts",
                new_callable=AsyncMock,
            ) as mock_upsert,
            patch("aspire_orchestrator.services.blueprint.legend_extractor.store_receipts"),
        ):
            result = await extract_project_legends(
                project_id=_PROJECT,
                suite_id=_SUITE,
                office_id=None,
                correlation_id=_CORR,
            )

        assert result["symbols_extracted"] == 0
        mock_upsert.assert_not_called()


# ---------------------------------------------------------------------------
# Suite isolation evil test (Law #6)
# ---------------------------------------------------------------------------

class TestSuiteIsolation:
    @pytest.mark.asyncio
    async def test_cross_tenant_sees_zero_rows(self) -> None:
        """Provider for suite_other must return 0 rows — filter enforced at DB layer."""
        table = _make_table([["AHU-1", "Air Handler", "10 TON"]])
        suite_a_intel = _make_intel_row(
            _SHEET_A,
            tables=[table],
            legend_regions=[{"bbox": [], "table_index": 0}],
        )

        captured_filters: list[str] = []

        async def _tenant_aware_select(table_name: str, filters: str, **kw) -> list[dict]:
            captured_filters.append(filters)
            if _SUITE_OTHER in filters:
                return []  # Cross-tenant → empty (simulates RLS)
            if table_name == "blueprint_sheet_intelligence":
                return [suite_a_intel]
            return [_make_sheet_row(_SHEET_A)]

        with (
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_select",
                side_effect=_tenant_aware_select,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_insert_batch_ignore_conflicts",
                new_callable=AsyncMock,
                return_value=([], 0),
            ),
            patch("aspire_orchestrator.services.blueprint.legend_extractor.store_receipts"),
        ):
            # Suite A call → gets rows
            result_a = await extract_project_legends(
                project_id=_PROJECT,
                suite_id=_SUITE,
                office_id=None,
                correlation_id=_CORR,
            )
            # Suite B call → cross-tenant → gets nothing
            result_b = await extract_project_legends(
                project_id=_PROJECT,
                suite_id=_SUITE_OTHER,
                office_id=None,
                correlation_id=_CORR,
            )

        assert result_a["symbols_extracted"] == 1
        assert result_b["symbols_extracted"] == 0
        # Both queries must have included the correct suite_id
        assert any(_SUITE in f for f in captured_filters)
        assert any(_SUITE_OTHER in f for f in captured_filters)

    def test_empty_suite_id_raises_immediately(self) -> None:
        """extract_project_legends must fail closed on missing suite_id (Law #3)."""
        import asyncio
        with pytest.raises(ValueError, match="suite_id"):
            asyncio.get_event_loop().run_until_complete(
                extract_project_legends(
                    project_id=_PROJECT,
                    suite_id="",
                    office_id=None,
                    correlation_id=_CORR,
                )
            )

    def test_empty_project_id_raises_immediately(self) -> None:
        import asyncio
        with pytest.raises(ValueError, match="project_id"):
            asyncio.get_event_loop().run_until_complete(
                extract_project_legends(
                    project_id="",
                    suite_id=_SUITE,
                    office_id=None,
                    correlation_id=_CORR,
                )
            )


# ---------------------------------------------------------------------------
# Receipt emission test (Law #2)
# ---------------------------------------------------------------------------

class TestReceiptEmission:
    @pytest.mark.asyncio
    async def test_receipt_emitted_on_every_run(self) -> None:
        """Every call to extract_project_legends emits exactly one receipt."""
        intel_row = _make_intel_row(_SHEET_A)  # no legends → early exit path
        fake_select = await _fake_select_factory([intel_row], [])

        with (
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_select",
                side_effect=fake_select,
            ),
            patch("aspire_orchestrator.services.blueprint.legend_extractor.store_receipts") as mock_receipt,
        ):
            await extract_project_legends(
                project_id=_PROJECT,
                suite_id=_SUITE,
                office_id=None,
                correlation_id=_CORR,
            )

        mock_receipt.assert_called_once()
        receipt_payload = mock_receipt.call_args[0][0]
        assert isinstance(receipt_payload, list)
        assert len(receipt_payload) == 1
        r = receipt_payload[0]
        assert r["action_type"] == "blueprint.legend_extraction.summary"
        assert r["risk_tier"] == "GREEN"
        assert r["correlation_id"] == _CORR

    @pytest.mark.asyncio
    async def test_receipt_contains_correct_counts(self) -> None:
        """Receipt metadata must match the returned summary counts."""
        table = _make_table([
            ["P-1", "PUMP 1", "2 HP"],
            ["P-2", "PUMP 2", "5 HP"],
        ])
        intel_row = _make_intel_row(
            _SHEET_A,
            tables=[table],
            legend_regions=[{"bbox": [], "table_index": 0}],
        )
        sheet_row = _make_sheet_row(_SHEET_A, sheet_number="P-20", discipline="P")
        fake_select = await _fake_select_factory([intel_row], [sheet_row])

        with (
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_select",
                side_effect=fake_select,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.legend_extractor.supabase_insert_batch_ignore_conflicts",
                new_callable=AsyncMock,
                return_value=([{"id": "1"}, {"id": "2"}], 0),
            ),
            patch("aspire_orchestrator.services.blueprint.legend_extractor.store_receipts") as mock_receipt,
        ):
            result = await extract_project_legends(
                project_id=_PROJECT,
                suite_id=_SUITE,
                office_id=None,
                correlation_id=_CORR,
            )

        receipt_meta = mock_receipt.call_args[0][0][0]["metadata"]
        assert receipt_meta["legends_processed"] == result["legends_processed"]
        assert receipt_meta["symbols_extracted"] == result["symbols_extracted"]
        assert "plumbing" in receipt_meta["disciplines_covered"]
