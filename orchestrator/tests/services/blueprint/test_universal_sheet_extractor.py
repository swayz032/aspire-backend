"""Unit tests — Wave 20b: universal_sheet_extractor.

8 tests covering:
  1. Happy path — full Azure response in, structured row out
  2. Legend region detected via header pattern
  3. Schedule region detected via row numbering
  4. Notes region detected via Azure paragraph role
  5. Plan region detected via bbox area heuristic
  6. Detail region detected via dense annotation heuristic
  7. Receipt emitted on success path (action_type=provider.azure.sheet_layout, suite-bound)
  8. Receipt emitted on failure path (denial with reason_code=AZURE_LAYOUT_FAILED)

Law compliance tested:
  #2 — receipt emission on success + failure
  #3 — fail-closed on Azure error
  #6 — suite_id threaded through receipt on success path
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Env must be set before any app imports
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")
os.environ.setdefault("ASPIRE_AZURE_DOC_INTEL_ENDPOINT", "https://fake.cognitiveservices.azure.com")
os.environ.setdefault("ASPIRE_AZURE_DOC_INTEL_KEY", "fake-key-for-tests")


# ---------------------------------------------------------------------------
# Azure response fixtures
# ---------------------------------------------------------------------------

def _make_azure_response(
    tables: list[dict[str, Any]] | None = None,
    paragraphs: list[dict[str, Any]] | None = None,
    key_value_pairs: list[dict[str, Any]] | None = None,
    pages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "analyzeResult": {
            "tables": tables or [],
            "paragraphs": paragraphs or [],
            "keyValuePairs": key_value_pairs or [],
            "pages": pages or [{"pageNumber": 1}],
        }
    }


def _make_table(
    headers: list[str],
    rows: list[list[str]],
    polygon: list[float] | None = None,
) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    for col_idx, header in enumerate(headers):
        cells.append({
            "rowIndex": 0,
            "columnIndex": col_idx,
            "content": header,
            "kind": "columnHeader",
        })
    for row_idx, row in enumerate(rows):
        for col_idx, value in enumerate(row):
            cells.append({
                "rowIndex": row_idx + 1,
                "columnIndex": col_idx,
                "content": value,
                "kind": "content",
            })
    bounding_regions = [{"polygon": polygon}] if polygon else []
    return {
        "rowCount": len(rows) + 1,
        "columnCount": len(headers),
        "cells": cells,
        "boundingRegions": bounding_regions,
    }


def _make_para(text: str, role: str = "", polygon: list[float] | None = None) -> dict[str, Any]:
    bounding_regions = [{"polygon": polygon}] if polygon else []
    return {
        "content": text,
        "role": role,
        "boundingRegions": bounding_regions,
    }


SUITE_ID = str(uuid.uuid4())
PROJECT_ID = str(uuid.uuid4())
SHEET_ID = str(uuid.uuid4())
CORR_ID = str(uuid.uuid4())
PNG_BYTES = b"fake-png-bytes"


def _make_success_provider_response(body: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.success = True
    resp.body = body
    resp.error_code = None
    return resp


def _make_failed_provider_response() -> MagicMock:
    from aspire_orchestrator.providers.error_codes import InternalErrorCode
    resp = MagicMock()
    resp.success = False
    resp.body = {}
    resp.error_code = InternalErrorCode.SERVER_INTERNAL_ERROR
    resp.error_message = "Azure internal error"
    return resp


# ---------------------------------------------------------------------------
# Test 1: Happy path — full Azure response in, structured row out
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_returns_structured_row():
    from aspire_orchestrator.services.blueprint.universal_sheet_extractor import (
        extract_sheet_intelligence,
    )

    azure_body = _make_azure_response(
        tables=[_make_table(["Symbol", "Description"], [["FP-1", "Sprinkler head"]])],
        paragraphs=[_make_para("General Notes", role="sectionHeading")],
        key_value_pairs=[{"key": {"content": "Project"}, "value": {"content": "Gulfstream"}, "confidence": 0.95}],
        pages=[{"pageNumber": 1}, {"pageNumber": 2}],
    )

    mock_response = _make_success_provider_response(azure_body)

    with patch(
        "aspire_orchestrator.services.blueprint.universal_sheet_extractor.get_azure_doc_intel_client"
    ) as mock_client_factory, patch(
        "aspire_orchestrator.services.blueprint.universal_sheet_extractor.store_receipts"
    ):
        mock_client = AsyncMock()
        mock_client.analyze_layout = AsyncMock(return_value=mock_response)
        mock_client_factory.return_value = mock_client

        row = await extract_sheet_intelligence(
            suite_id=SUITE_ID,
            office_id=None,
            project_id=PROJECT_ID,
            sheet_id=SHEET_ID,
            sheet_png_bytes=PNG_BYTES,
            correlation_id=CORR_ID,
        )

    assert row["suite_id"] == SUITE_ID
    assert row["project_id"] == PROJECT_ID
    assert row["sheet_id"] == SHEET_ID
    assert row["azure_model"] == "prebuilt-layout"
    assert row["page_count"] == 2
    assert isinstance(row["tables"], list)
    assert isinstance(row["paragraphs"], list)
    assert isinstance(row["key_value_pairs"], list)
    assert isinstance(row["regions"], dict)
    assert "legend" in row["regions"]
    assert "extracted_at" in row
    assert row["azure_call_ms"] >= 0


# ---------------------------------------------------------------------------
# Test 2: Legend region detected via header pattern (first col = "symbol")
# ---------------------------------------------------------------------------

def test_legend_region_detected_via_header_pattern():
    from aspire_orchestrator.services.blueprint.universal_sheet_extractor import (
        _classify_regions,
        _extract_tables,
    )

    raw_analyze = _make_azure_response(
        tables=[_make_table(["Symbol", "Description"], [["FP-1", "Upright sprinkler"]])],
    )
    tables = _extract_tables(raw_analyze["analyzeResult"])
    regions = _classify_regions(tables=tables, paragraphs=[])

    assert len(regions["legend"]) == 1
    assert regions["schedule"] == []


# ---------------------------------------------------------------------------
# Test 3: Schedule region detected via row numbering + schedule header keyword
# ---------------------------------------------------------------------------

def test_schedule_region_detected_via_row_numbering():
    from aspire_orchestrator.services.blueprint.universal_sheet_extractor import (
        _classify_regions,
        _extract_tables,
    )

    raw_analyze = _make_azure_response(
        tables=[
            _make_table(
                ["No.", "Type", "Width", "Height"],
                [["1", "A", "3'-0\"", "7'-0\""], ["2", "B", "2'-8\"", "6'-8\""]],
            )
        ],
        paragraphs=[_make_para("DOOR SCHEDULE")],
    )
    tables = _extract_tables(raw_analyze["analyzeResult"])
    paragraphs_raw = raw_analyze["analyzeResult"]["paragraphs"]
    from aspire_orchestrator.services.blueprint.universal_sheet_extractor import _extract_paragraphs
    paragraphs = _extract_paragraphs({"paragraphs": paragraphs_raw})
    regions = _classify_regions(tables=tables, paragraphs=paragraphs)

    assert len(regions["schedule"]) >= 1
    assert regions["legend"] == []


# ---------------------------------------------------------------------------
# Test 4: Notes region detected via Azure paragraph role
# ---------------------------------------------------------------------------

def test_notes_region_detected_via_azure_paragraph_role():
    from aspire_orchestrator.services.blueprint.universal_sheet_extractor import (
        _classify_regions,
        _extract_paragraphs,
    )

    raw_para = _make_para("All work shall conform to IBC 2021.", role="generalNotes")
    paragraphs = _extract_paragraphs({"paragraphs": [raw_para]})
    regions = _classify_regions(tables=[], paragraphs=paragraphs)

    assert len(regions["notes"]) == 1
    assert regions["notes"][0]["text_snippet"].startswith("All work")


# ---------------------------------------------------------------------------
# Test 5: Plan region detected via bbox area heuristic (>40% of sheet area)
# ---------------------------------------------------------------------------

def test_plan_region_detected_via_bbox_area_heuristic():
    from aspire_orchestrator.services.blueprint.universal_sheet_extractor import (
        _classify_regions,
        _extract_paragraphs,
    )

    # Large polygon covering most of sheet (e.g., 100x80 = 8000 sq units)
    large_poly = [0.0, 0.0, 100.0, 0.0, 100.0, 80.0, 0.0, 80.0]
    # Small polygon (10x5 = 50 sq units, ratio ~0.006 of total)
    small_polys = [
        [0.0, 0.0, 5.0, 0.0, 5.0, 2.0, 0.0, 2.0],  # 10 sq units
        [0.0, 0.0, 3.0, 0.0, 3.0, 2.0, 0.0, 2.0],  # 6 sq units
    ]

    raw_paragraphs = [
        _make_para("Plan view text", polygon=large_poly),
    ] + [_make_para(f"Note {i}", polygon=p) for i, p in enumerate(small_polys)]

    paragraphs = _extract_paragraphs({"paragraphs": raw_paragraphs})
    regions = _classify_regions(tables=[], paragraphs=paragraphs)

    assert len(regions["plan"]) >= 1


# ---------------------------------------------------------------------------
# Test 6: Detail region detected via dense annotation heuristic (<15% area + ≥5 words)
# ---------------------------------------------------------------------------

def test_detail_region_detected_via_dense_annotation_heuristic():
    from aspire_orchestrator.services.blueprint.universal_sheet_extractor import (
        _classify_regions,
        _extract_paragraphs,
    )

    # Dominant paragraph creating large total area
    dominant = _make_para("Floorplan", polygon=[0.0, 0.0, 200.0, 0.0, 200.0, 150.0, 0.0, 150.0])
    # Small dense paragraph (many words, small bbox)
    detail_para = _make_para(
        "Provide 3/4 inch copper type L supply line with compression fittings per detail A",
        polygon=[1.0, 1.0, 5.0, 1.0, 5.0, 3.0, 1.0, 3.0],
    )

    paragraphs = _extract_paragraphs({"paragraphs": [dominant, detail_para]})
    regions = _classify_regions(tables=[], paragraphs=paragraphs)

    assert len(regions["detail"]) >= 1


# ---------------------------------------------------------------------------
# Test 7: Receipt emitted on success path (suite-bound, correct action_type)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receipt_emitted_on_success_path():
    from aspire_orchestrator.services.blueprint.universal_sheet_extractor import (
        extract_sheet_intelligence,
    )

    azure_body = _make_azure_response()
    mock_response = _make_success_provider_response(azure_body)

    captured_receipts: list[list[Any]] = []

    def capture_store(receipts: list[Any]) -> None:
        captured_receipts.extend(receipts)

    with patch(
        "aspire_orchestrator.services.blueprint.universal_sheet_extractor.get_azure_doc_intel_client"
    ) as mock_client_factory, patch(
        "aspire_orchestrator.services.blueprint.universal_sheet_extractor.store_receipts",
        side_effect=capture_store,
    ):
        mock_client = AsyncMock()
        mock_client.analyze_layout = AsyncMock(return_value=mock_response)
        mock_client_factory.return_value = mock_client

        await extract_sheet_intelligence(
            suite_id=SUITE_ID,
            office_id=None,
            project_id=PROJECT_ID,
            sheet_id=SHEET_ID,
            sheet_png_bytes=PNG_BYTES,
            correlation_id=CORR_ID,
        )

    assert len(captured_receipts) == 1
    receipt = captured_receipts[0]
    assert receipt["action_type"] == "provider.azure.sheet_layout"
    assert receipt["suite_id"] == SUITE_ID
    assert receipt["outcome"] == "success"
    assert receipt["risk_tier"] == "GREEN"


# ---------------------------------------------------------------------------
# Test 8: Receipt emitted on failure path (reason_code=AZURE_LAYOUT_FAILED)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receipt_emitted_on_failure_path():
    from aspire_orchestrator.providers.base_client import ProviderError
    from aspire_orchestrator.providers.error_codes import InternalErrorCode
    from aspire_orchestrator.services.blueprint.universal_sheet_extractor import (
        extract_sheet_intelligence,
    )

    captured_receipts: list[list[Any]] = []

    def capture_store(receipts: list[Any]) -> None:
        captured_receipts.extend(receipts)

    with patch(
        "aspire_orchestrator.services.blueprint.universal_sheet_extractor.get_azure_doc_intel_client"
    ) as mock_client_factory, patch(
        "aspire_orchestrator.services.blueprint.universal_sheet_extractor.store_receipts",
        side_effect=capture_store,
    ):
        mock_client = AsyncMock()
        mock_client.analyze_layout = AsyncMock(
            side_effect=ProviderError(
                code=InternalErrorCode.SERVER_INTERNAL_ERROR,
                message="Azure 503",
                provider_id="azure_doc_intel",
            )
        )
        mock_client_factory.return_value = mock_client

        with pytest.raises(ProviderError):
            await extract_sheet_intelligence(
                suite_id=SUITE_ID,
                office_id=None,
                project_id=PROJECT_ID,
                sheet_id=SHEET_ID,
                sheet_png_bytes=PNG_BYTES,
                correlation_id=CORR_ID,
            )

    assert len(captured_receipts) == 1
    receipt = captured_receipts[0]
    assert receipt["action_type"] == "provider.azure.sheet_layout"
    assert receipt["outcome"] == "failed"
    assert receipt["metadata"]["reason_code"] == "AZURE_LAYOUT_FAILED"


# ---------------------------------------------------------------------------
# Test 9: Per-cell bboxes are extracted alongside rows (Wave 20b-fix)
# ---------------------------------------------------------------------------

def test_cells_extracted_with_bboxes():
    """_extract_tables must emit cells list with bounding_regions on each cell."""
    from aspire_orchestrator.services.blueprint.universal_sheet_extractor import (
        _extract_tables,
    )

    # 2-row × 3-col table: header + 2 data rows = 6 cells total (excluding header row cells
    # from data region, but header cells are still captured in extracted_cells list)
    raw_cells = []
    polygons = {}
    cell_num = 0
    for r in range(3):  # row 0 = header, rows 1-2 = data
        for c in range(3):
            poly = [float(c * 100), float(r * 50), float(c * 100 + 100), float(r * 50),
                    float(c * 100 + 100), float(r * 50 + 50), float(c * 100), float(r * 50 + 50)]
            raw_cells.append({
                "rowIndex": r,
                "columnIndex": c,
                "content": f"cell-{r}-{c}",
                "kind": "columnHeader" if r == 0 else "content",
                "rowSpan": 1,
                "columnSpan": 1,
                "boundingRegions": [{"pageNumber": 1, "polygon": poly}],
            })
            polygons[(r, c)] = poly
            cell_num += 1

    azure_result = {
        "tables": [{
            "rowCount": 3,
            "columnCount": 3,
            "cells": raw_cells,
            "boundingRegions": [{"polygon": [0.0, 0.0, 300.0, 0.0, 300.0, 150.0, 0.0, 150.0]}],
        }]
    }

    tables = _extract_tables(azure_result)

    assert len(tables) == 1
    tbl = tables[0]
    assert "cells" in tbl, "cells key must be present in extracted table"
    assert len(tbl["cells"]) == 9  # 3 rows × 3 cols
    # Verify each cell has bounding_regions with polygon
    for cell in tbl["cells"]:
        assert "bounding_regions" in cell
        assert len(cell["bounding_regions"]) == 1
        assert cell["bounding_regions"][0]["polygon"] is not None
        assert "content" in cell
        assert "row_index" in cell
        assert "column_index" in cell


def test_cells_preserved_on_legend_table():
    """Legend-region table: cells must carry per-cell bounding_regions for symbol glyph bboxes."""
    from aspire_orchestrator.services.blueprint.universal_sheet_extractor import (
        _classify_regions,
        _extract_tables,
    )

    # Legend table: header row + 2 symbol rows, each cell has distinct polygon
    raw_cells = [
        # Header
        {
            "rowIndex": 0, "columnIndex": 0, "content": "Symbol",
            "kind": "columnHeader", "rowSpan": 1, "columnSpan": 1,
            "boundingRegions": [{"pageNumber": 1, "polygon": [0.0, 0.0, 80.0, 0.0, 80.0, 20.0, 0.0, 20.0]}],
        },
        {
            "rowIndex": 0, "columnIndex": 1, "content": "Description",
            "kind": "columnHeader", "rowSpan": 1, "columnSpan": 1,
            "boundingRegions": [{"pageNumber": 1, "polygon": [80.0, 0.0, 300.0, 0.0, 300.0, 20.0, 80.0, 20.0]}],
        },
        # Row 1 (symbol glyph cell + label cell)
        {
            "rowIndex": 1, "columnIndex": 0, "content": "FP-1",
            "kind": "content", "rowSpan": 1, "columnSpan": 1,
            "boundingRegions": [{"pageNumber": 1, "polygon": [0.0, 20.0, 80.0, 20.0, 80.0, 50.0, 0.0, 50.0]}],
        },
        {
            "rowIndex": 1, "columnIndex": 1, "content": "Upright Sprinkler",
            "kind": "content", "rowSpan": 1, "columnSpan": 1,
            "boundingRegions": [{"pageNumber": 1, "polygon": [80.0, 20.0, 300.0, 20.0, 300.0, 50.0, 80.0, 50.0]}],
        },
        # Row 2
        {
            "rowIndex": 2, "columnIndex": 0, "content": "FP-2",
            "kind": "content", "rowSpan": 1, "columnSpan": 1,
            "boundingRegions": [{"pageNumber": 1, "polygon": [0.0, 50.0, 80.0, 50.0, 80.0, 80.0, 0.0, 80.0]}],
        },
        {
            "rowIndex": 2, "columnIndex": 1, "content": "Pendant Sprinkler",
            "kind": "content", "rowSpan": 1, "columnSpan": 1,
            "boundingRegions": [{"pageNumber": 1, "polygon": [80.0, 50.0, 300.0, 50.0, 300.0, 80.0, 80.0, 80.0]}],
        },
    ]

    azure_result = {
        "tables": [{
            "rowCount": 3,
            "columnCount": 2,
            "cells": raw_cells,
            "boundingRegions": [{"polygon": [0.0, 0.0, 300.0, 0.0, 300.0, 80.0, 0.0, 80.0]}],
        }]
    }

    tables = _extract_tables(azure_result)
    regions = _classify_regions(tables=tables, paragraphs=[])

    assert len(regions["legend"]) == 1, "Legend region must be detected from 'Symbol' header"
    tbl = tables[0]
    assert "cells" in tbl
    # The glyph cells (col 0, rows 1-2) must have distinct polygons
    glyph_cells = [c for c in tbl["cells"] if c["column_index"] == 0 and c["row_index"] > 0]
    assert len(glyph_cells) == 2
    polys = [c["bounding_regions"][0]["polygon"] for c in glyph_cells]
    assert polys[0] != polys[1], "Each symbol glyph cell must have a distinct per-cell bbox"
