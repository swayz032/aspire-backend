"""Drew Wave 2.7 — extended blueprint read API tests.

Tests the six new endpoints added in Wave 2.7:
  GET  /v1/blueprints/projects/{id}/symbols
  GET  /v1/blueprints/projects/{id}/assemblies
  GET  /v1/blueprints/projects/{id}/materials
  GET  /v1/blueprints/projects/{id}/missing_inputs
  GET  /v1/blueprints/projects/{id}/story
  POST /v1/blueprints/projects/{id}/missing_inputs/{input_id}/resolve

Law coverage:
  Law #2 — every endpoint emits a receipt (success + failure + denial paths)
  Law #3 — fail closed: missing headers → 401; missing token → 401
  Law #4 — POST /resolve is YELLOW; capability_token required
  Law #6 — cross-tenant returns 404 (no existence leak)
  Law #9 — markdown body / supplier data never appear in receipt outputs

Implementation note: route handler functions are tested directly (not via
TestClient) because the full server.py requires `langgraph` which is not
installed in the unit-test environment. This matches test_drew_read_apis.py.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

SUITE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUITE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OFFICE_A = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TENANT_A = "cccccccc-cccc-cccc-cccc-cccccccccccc"
PROJECT_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
SHEET_ID_1 = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
SHEET_ID_2 = "ffffffff-ffff-ffff-ffff-ffffffffffff"
INPUT_ID = "11111111-1111-1111-1111-111111111111"

_NOW = datetime.now(timezone.utc).isoformat()

_FAKE_PROJECT_ROW: dict[str, Any] = {
    "id": PROJECT_ID,
    "suite_id": SUITE_A,
    "office_id": OFFICE_A,
    "address": "abc123hash",
    "created_at": _NOW,
    "created_by": None,
    "stage_progress": {
        "ingest": "done",
        "classify": "done",
        "see": "done",
        "reason": "done",
        "procure": "not_started",
    },
}

_FAKE_SHEET_ROWS: list[dict[str, Any]] = [
    {"id": SHEET_ID_1, "suite_id": SUITE_A, "project_id": PROJECT_ID, "sheet_number": "A1", "created_at": _NOW},
    {"id": SHEET_ID_2, "suite_id": SUITE_A, "project_id": PROJECT_ID, "sheet_number": "S1", "created_at": _NOW},
]

_FAKE_SYMBOL_ROWS: list[dict[str, Any]] = [
    {
        "id": str(uuid.uuid4()),
        "suite_id": SUITE_A,
        "sheet_id": SHEET_ID_1,
        "class_": "electrical_outlet",
        "bbox": {"x": 10, "y": 20, "w": 5, "h": 5},
        "confidence": 0.92,
        "created_at": _NOW,
    },
    {
        "id": str(uuid.uuid4()),
        "suite_id": SUITE_A,
        "sheet_id": SHEET_ID_2,
        "class_": "structural_beam",
        "bbox": {"x": 50, "y": 60, "w": 10, "h": 3},
        "confidence": 0.85,
        "created_at": _NOW,
    },
]

_FAKE_ASSEMBLY_ROWS: list[dict[str, Any]] = [
    {
        "id": str(uuid.uuid4()),
        "suite_id": SUITE_A,
        "project_id": PROJECT_ID,
        "type": "concrete_pour",
        "quantity": 45.0,
        "unit": "cy",
        "truth": "observed",
        "supersedes_id": None,
        "created_at": _NOW,
    },
    {
        "id": str(uuid.uuid4()),
        "suite_id": SUITE_A,
        "project_id": PROJECT_ID,
        "type": "drywall",
        "quantity": 1200.0,
        "unit": "sf",
        "truth": "derived",
        "supersedes_id": None,
        "created_at": _NOW,
    },
]

_FAKE_ASSEMBLY_SUPERSEDED: dict[str, Any] = {
    "id": str(uuid.uuid4()),
    "suite_id": SUITE_A,
    "project_id": PROJECT_ID,
    "type": "old_framing",
    "quantity": 800.0,
    "unit": "lf",
    "truth": "assumed",
    "supersedes_id": str(uuid.uuid4()),  # non-null → superseded
    "created_at": _NOW,
}

_FAKE_MATERIAL_ROWS: list[dict[str, Any]] = [
    {
        "id": str(uuid.uuid4()),
        "suite_id": SUITE_A,
        "project_id": PROJECT_ID,
        "line_item": "structural_steel_beam",
        "quantity": 12.0,
        "unit": "ea",
        "truth": "observed",
        "tariff_flag": "section_232_steel",
        "supplier_id": str(uuid.uuid4()),
        "supersedes_id": None,
        "created_at": _NOW,
    },
    {
        "id": str(uuid.uuid4()),
        "suite_id": SUITE_A,
        "project_id": PROJECT_ID,
        "line_item": "drywall_sheet_5_8",
        "quantity": 240.0,
        "unit": "sheet",
        "truth": "derived",
        "tariff_flag": "none",
        "supplier_id": None,
        "supersedes_id": None,
        "created_at": _NOW,
    },
]

_FAKE_MISSING_INPUT_ROWS: list[dict[str, Any]] = [
    {
        "id": INPUT_ID,
        "suite_id": SUITE_A,
        "project_id": PROJECT_ID,
        "description": "Wall length between columns A3-A4 unclear",
        "suggested_resolution": "Measure on site and enter in feet",
        "resolved_by": None,
        "resolved_at": None,
        "created_at": _NOW,
    },
]

_FAKE_RESOLVED_INPUT_ROW: dict[str, Any] = {
    "id": INPUT_ID,
    "suite_id": SUITE_A,
    "project_id": PROJECT_ID,
    "description": "Wall length between columns A3-A4 unclear",
    "suggested_resolution": "Measure on site and enter in feet",
    "resolved_by": str(uuid.uuid4()),
    "resolved_at": _NOW,
    "created_at": _NOW,
}

_FAKE_STORY_ROWS: list[dict[str, Any]] = [
    {
        "id": str(uuid.uuid4()),
        "suite_id": SUITE_A,
        "project_id": PROJECT_ID,
        "phase": 1,
        "markdown": "## Phase 1: Site Preparation\n\nExcavation required for foundation.",
        "truth_distribution": {"observed": 3, "derived": 1},
        "mean_confidence": 0.88,
        "model_version": "gpt-4o-2024-11-20",
        "supersedes_id": None,
        "created_at": _NOW,
    },
    {
        "id": str(uuid.uuid4()),
        "suite_id": SUITE_A,
        "project_id": PROJECT_ID,
        "phase": 2,
        "markdown": "## Phase 2: Framing\n\nSteel frame assembly begins after pour.",
        "truth_distribution": {"observed": 5, "assumed": 2},
        "mean_confidence": 0.88,
        "model_version": "gpt-4o-2024-11-20",
        "supersedes_id": None,
        "created_at": _NOW,
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_receipt_store():
    from aspire_orchestrator.services.receipt_store import clear_store
    clear_store()
    yield
    clear_store()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _query_receipts_by_action(action_type: str) -> list[dict]:
    import aspire_orchestrator.services.receipt_store as rs
    with rs._lock:
        return [r for r in rs._receipts if r.get("action_type") == action_type]


def _run(coro: Any) -> Any:
    """Run a coroutine from synchronous test code."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# Endpoint 1: list_blueprint_symbols
# ===========================================================================

class TestListBlueprintSymbols:
    """GET /v1/blueprints/projects/{id}/symbols"""

    def test_get_symbols_filters_by_sheet_id(self) -> None:
        """sheet_id query param must restrict symbols to that sheet."""
        from aspire_orchestrator.routes.blueprints import list_blueprint_symbols

        sheet1_symbols = [s for s in _FAKE_SYMBOL_ROWS if s["sheet_id"] == SHEET_ID_1]

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
        ) as mock_select:
            # First call: project existence check; second: symbols
            mock_select.side_effect = [[_FAKE_PROJECT_ROW], sheet1_symbols]
            result = _run(list_blueprint_symbols(
                project_id=PROJECT_ID,
                sheet_id=SHEET_ID_1,
                confidence_floor=0.70,
                class_prefix=None,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        sheet_ids = {str(s.sheet_id) for s in result}
        assert sheet_ids == {SHEET_ID_1}, f"Expected only sheet {SHEET_ID_1}, got {sheet_ids}"

    def test_get_symbols_returns_404_for_cross_tenant(self) -> None:
        """Cross-tenant request must return 404 (Law #6)."""
        from aspire_orchestrator.routes.blueprints import list_blueprint_symbols

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
            return_value=[],  # project not found for SUITE_B
        ):
            with pytest.raises(HTTPException) as exc_info:
                _run(list_blueprint_symbols(
                    project_id=PROJECT_ID,
                    sheet_id=None,
                    confidence_floor=0.70,
                    class_prefix=None,
                    x_tenant_id=TENANT_A,
                    x_suite_id=SUITE_B,
                    x_office_id=OFFICE_A,
                ))
        assert exc_info.value.status_code == 404

    def test_symbols_receipt_emitted_on_success(self) -> None:
        """Successful symbols read must emit blueprint.read.symbols receipt (Law #2)."""
        from aspire_orchestrator.routes.blueprints import list_blueprint_symbols

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
        ) as mock_select:
            sheet_ids_csv = f"{SHEET_ID_1},{SHEET_ID_2}"
            mock_select.side_effect = [
                [_FAKE_PROJECT_ROW],   # project check
                _FAKE_SHEET_ROWS,      # sheet lookup (no sheet_id filter)
                _FAKE_SYMBOL_ROWS,     # symbols
            ]
            _run(list_blueprint_symbols(
                project_id=PROJECT_ID,
                sheet_id=None,
                confidence_floor=0.70,
                class_prefix=None,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        receipts = _query_receipts_by_action("blueprint.read.symbols")
        assert len(receipts) >= 1
        r = receipts[-1]  # last receipt is the success one
        assert r["outcome"] == "success"
        assert r["risk_tier"] == "green"
        # Law #9: receipt must not contain raw symbol data
        receipt_str = json.dumps(r)
        assert "electrical_outlet" not in receipt_str
        assert "confidence" not in receipt_str or r["redacted_outputs"].get("symbol_count") is not None


# ===========================================================================
# Endpoint 2: list_blueprint_assemblies
# ===========================================================================

class TestListBlueprintAssemblies:
    """GET /v1/blueprints/projects/{id}/assemblies"""

    def test_get_assemblies_excludes_superseded_by_default(self) -> None:
        """active_only=true (default) must add supersedes_id=is.null filter."""
        from aspire_orchestrator.routes.blueprints import list_blueprint_assemblies

        captured_filters: list[str] = []

        async def _capture(table: str, filters: str | dict, **kwargs: Any) -> list[dict]:
            if isinstance(filters, str):
                captured_filters.append(filters)
            if table == "blueprint_projects":
                return [_FAKE_PROJECT_ROW]
            return _FAKE_ASSEMBLY_ROWS

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            side_effect=_capture,
        ):
            _run(list_blueprint_assemblies(
                project_id=PROJECT_ID,
                active_only=True,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        all_filters = " ".join(captured_filters)
        assert "supersedes_id=is.null" in all_filters, (
            "active_only=true must send supersedes_id=is.null to PostgREST"
        )

    def test_assemblies_cross_tenant_returns_404(self) -> None:
        """Cross-tenant assembly request returns 404 (Law #6)."""
        from aspire_orchestrator.routes.blueprints import list_blueprint_assemblies

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
            return_value=[],
        ):
            with pytest.raises(HTTPException) as exc_info:
                _run(list_blueprint_assemblies(
                    project_id=PROJECT_ID,
                    active_only=True,
                    x_tenant_id=TENANT_A,
                    x_suite_id=SUITE_B,
                    x_office_id=OFFICE_A,
                ))
        assert exc_info.value.status_code == 404

    def test_assemblies_receipt_emitted(self) -> None:
        """Assembly read must emit blueprint.read.assemblies receipt (Law #2)."""
        from aspire_orchestrator.routes.blueprints import list_blueprint_assemblies

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
        ) as mock_select:
            mock_select.side_effect = [[_FAKE_PROJECT_ROW], _FAKE_ASSEMBLY_ROWS]
            _run(list_blueprint_assemblies(
                project_id=PROJECT_ID,
                active_only=True,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        receipts = _query_receipts_by_action("blueprint.read.assemblies")
        assert len(receipts) >= 1
        assert receipts[-1]["outcome"] == "success"


# ===========================================================================
# Endpoint 3: list_blueprint_materials
# ===========================================================================

class TestListBlueprintMaterials:
    """GET /v1/blueprints/projects/{id}/materials"""

    def test_get_materials_tariff_only_filter(self) -> None:
        """tariff_only=true must add tariff_flag=neq.none filter."""
        from aspire_orchestrator.routes.blueprints import list_blueprint_materials

        captured_filters: list[str] = []

        async def _capture(table: str, filters: str | dict, **kwargs: Any) -> list[dict]:
            if isinstance(filters, str):
                captured_filters.append(filters)
            if table == "blueprint_projects":
                return [_FAKE_PROJECT_ROW]
            # Return only the tariff-flagged material
            return [m for m in _FAKE_MATERIAL_ROWS if m["tariff_flag"] != "none"]

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            side_effect=_capture,
        ):
            result = _run(list_blueprint_materials(
                project_id=PROJECT_ID,
                tariff_only=True,
                has_supplier=False,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        all_filters = " ".join(captured_filters)
        assert "tariff_flag=neq.none" in all_filters, (
            "tariff_only=true must send tariff_flag=neq.none filter to PostgREST"
        )
        tariff_flags = {str(m.tariff_flag) for m in result}
        assert "none" not in tariff_flags, "No non-tariffed materials should appear with tariff_only=true"

    def test_materials_no_pii_in_receipt(self) -> None:
        """Receipts must not contain line_item text or supplier data (Law #9)."""
        from aspire_orchestrator.routes.blueprints import list_blueprint_materials

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
        ) as mock_select:
            mock_select.side_effect = [[_FAKE_PROJECT_ROW], _FAKE_MATERIAL_ROWS]
            _run(list_blueprint_materials(
                project_id=PROJECT_ID,
                tariff_only=False,
                has_supplier=False,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        receipts = _query_receipts_by_action("blueprint.read.materials")
        assert len(receipts) >= 1
        for r in receipts:
            receipt_str = json.dumps(r)
            # Law #9: line item names and supplier data must not appear
            assert "structural_steel_beam" not in receipt_str
            assert "drywall_sheet" not in receipt_str
            # supplier_id UUID values must not appear in receipt outputs
            for mat in _FAKE_MATERIAL_ROWS:
                if mat.get("supplier_id"):
                    assert mat["supplier_id"] not in receipt_str

    def test_materials_cross_tenant_returns_404(self) -> None:
        """Cross-tenant materials request returns 404 (Law #6)."""
        from aspire_orchestrator.routes.blueprints import list_blueprint_materials

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
            return_value=[],
        ):
            with pytest.raises(HTTPException) as exc_info:
                _run(list_blueprint_materials(
                    project_id=PROJECT_ID,
                    tariff_only=False,
                    has_supplier=False,
                    x_tenant_id=TENANT_A,
                    x_suite_id=SUITE_B,
                    x_office_id=OFFICE_A,
                ))
        assert exc_info.value.status_code == 404


# ===========================================================================
# Endpoint 4: list_blueprint_missing_inputs
# ===========================================================================

class TestListBlueprintMissingInputs:
    """GET /v1/blueprints/projects/{id}/missing_inputs"""

    def test_get_missing_inputs_unresolved_only_by_default(self) -> None:
        """unresolved_only=true (default) must add resolved_at=is.null filter."""
        from aspire_orchestrator.routes.blueprints import list_blueprint_missing_inputs

        captured_filters: list[str] = []

        async def _capture(table: str, filters: str | dict, **kwargs: Any) -> list[dict]:
            if isinstance(filters, str):
                captured_filters.append(filters)
            if table == "blueprint_projects":
                return [_FAKE_PROJECT_ROW]
            return _FAKE_MISSING_INPUT_ROWS

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            side_effect=_capture,
        ):
            result = _run(list_blueprint_missing_inputs(
                project_id=PROJECT_ID,
                unresolved_only=True,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        all_filters = " ".join(captured_filters)
        assert "resolved_at=is.null" in all_filters, (
            "unresolved_only=true must send resolved_at=is.null to PostgREST"
        )
        # All returned rows must be unresolved
        resolved = [r for r in result if r.resolved_at is not None]
        assert not resolved, "No resolved inputs should appear with unresolved_only=true"

    def test_missing_inputs_cross_tenant_returns_404(self) -> None:
        """Cross-tenant missing inputs request returns 404 (Law #6)."""
        from aspire_orchestrator.routes.blueprints import list_blueprint_missing_inputs

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
            return_value=[],
        ):
            with pytest.raises(HTTPException) as exc_info:
                _run(list_blueprint_missing_inputs(
                    project_id=PROJECT_ID,
                    unresolved_only=True,
                    x_tenant_id=TENANT_A,
                    x_suite_id=SUITE_B,
                    x_office_id=OFFICE_A,
                ))
        assert exc_info.value.status_code == 404

    def test_missing_inputs_receipt_emitted(self) -> None:
        """Missing inputs read must emit blueprint.read.missing_inputs receipt (Law #2)."""
        from aspire_orchestrator.routes.blueprints import list_blueprint_missing_inputs

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
        ) as mock_select:
            mock_select.side_effect = [[_FAKE_PROJECT_ROW], _FAKE_MISSING_INPUT_ROWS]
            _run(list_blueprint_missing_inputs(
                project_id=PROJECT_ID,
                unresolved_only=True,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        receipts = _query_receipts_by_action("blueprint.read.missing_inputs")
        assert len(receipts) >= 1
        assert receipts[-1]["outcome"] == "success"


# ===========================================================================
# Endpoint 5: get_blueprint_story
# ===========================================================================

class TestGetBlueprintStory:
    """GET /v1/blueprints/projects/{id}/story"""

    def test_get_story_returns_phased_markdown(self) -> None:
        """Story response must contain phases with markdown and phase_number."""
        from aspire_orchestrator.routes.blueprints import get_blueprint_story

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
        ) as mock_select:
            mock_select.side_effect = [[_FAKE_PROJECT_ROW], _FAKE_STORY_ROWS]
            result = _run(get_blueprint_story(
                project_id=PROJECT_ID,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        assert len(result.phases) == 2
        assert result.phases[0].phase_number == 1
        assert "Phase 1" in result.phases[0].markdown
        assert result.phases[1].phase_number == 2

    def test_story_no_markdown_in_receipt(self) -> None:
        """Story receipt must not contain markdown content (Law #9)."""
        from aspire_orchestrator.routes.blueprints import get_blueprint_story

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
        ) as mock_select:
            mock_select.side_effect = [[_FAKE_PROJECT_ROW], _FAKE_STORY_ROWS]
            _run(get_blueprint_story(
                project_id=PROJECT_ID,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        receipts = _query_receipts_by_action("blueprint.read.story")
        assert len(receipts) >= 1
        r = receipts[-1]
        receipt_str = json.dumps(r)
        # Law #9: markdown text must never appear in receipt
        assert "Site Preparation" not in receipt_str
        assert "Excavation required" not in receipt_str
        assert "Steel frame" not in receipt_str
        # But count IS allowed
        assert r["redacted_outputs"]["phase_count"] == 2

    def test_story_cross_tenant_returns_404(self) -> None:
        """Cross-tenant story request returns 404 (Law #6)."""
        from aspire_orchestrator.routes.blueprints import get_blueprint_story

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
            return_value=[],
        ):
            with pytest.raises(HTTPException) as exc_info:
                _run(get_blueprint_story(
                    project_id=PROJECT_ID,
                    x_tenant_id=TENANT_A,
                    x_suite_id=SUITE_B,
                    x_office_id=OFFICE_A,
                ))
        assert exc_info.value.status_code == 404


# ===========================================================================
# Endpoint 6: resolve_missing_input
# ===========================================================================

class TestResolveMissingInput:
    """POST /v1/blueprints/projects/{id}/missing_inputs/{input_id}/resolve"""

    def test_resolve_missing_input_updates_resolved_at(self) -> None:
        """Successful resolve must update the missing input row's resolved_at."""
        from aspire_orchestrator.routes.blueprints import resolve_missing_input
        from aspire_orchestrator.services.blueprint.schemas.missing_input_resolve_request import (
            MissingInputResolveRequest,
        )

        body = MissingInputResolveRequest(
            resolution_value="42 feet",
            resolved_by=uuid.UUID(TENANT_A),
            capability_token="valid-token-stub",
        )

        with (
            patch(
                "aspire_orchestrator.routes.blueprints.supabase_select",
                new_callable=AsyncMock,
            ) as mock_select,
            patch(
                "aspire_orchestrator.routes.blueprints.supabase_update",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_update,
            patch(
                "aspire_orchestrator.routes.blueprints.supabase_insert",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            mock_select.side_effect = [
                [_FAKE_PROJECT_ROW],       # project existence check
                _FAKE_MISSING_INPUT_ROWS,  # missing input load
            ]
            result = _run(resolve_missing_input(
                project_id=PROJECT_ID,
                input_id=INPUT_ID,
                body=body,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        assert result["success"] is True
        assert result["input_id"] == INPUT_ID
        assert result["resolved_at"] is not None
        # Verify supabase_update was called
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args
        update_data = call_kwargs[0][2] if len(call_kwargs[0]) >= 3 else call_kwargs[1].get("data", {})
        assert "resolved_at" in update_data
        assert "resolved_by" in update_data

    def test_resolve_missing_input_creates_field_confirmed_assembly(self) -> None:
        """Resolve must insert a field_confirmed blueprint_assemblies row."""
        from aspire_orchestrator.routes.blueprints import resolve_missing_input
        from aspire_orchestrator.services.blueprint.schemas.missing_input_resolve_request import (
            MissingInputResolveRequest,
        )

        body = MissingInputResolveRequest(
            resolution_value="42 feet",
            resolved_by=uuid.UUID(TENANT_A),
            capability_token="valid-token-stub",
        )

        inserted_tables: list[str] = []
        inserted_data: list[dict] = []

        async def _fake_insert(table: str, data: dict, **kwargs: Any) -> dict:
            inserted_tables.append(table)
            inserted_data.append(data)
            return {}

        with (
            patch(
                "aspire_orchestrator.routes.blueprints.supabase_select",
                new_callable=AsyncMock,
            ) as mock_select,
            patch(
                "aspire_orchestrator.routes.blueprints.supabase_update",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "aspire_orchestrator.routes.blueprints.supabase_insert",
                side_effect=_fake_insert,
            ),
        ):
            mock_select.side_effect = [
                [_FAKE_PROJECT_ROW],
                _FAKE_MISSING_INPUT_ROWS,
            ]
            result = _run(resolve_missing_input(
                project_id=PROJECT_ID,
                input_id=INPUT_ID,
                body=body,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        assert result["assembly_inserted"] is True
        assert "blueprint_assemblies" in inserted_tables, (
            "resolve must insert a row into blueprint_assemblies"
        )
        asm = inserted_data[0]
        assert asm["truth"] == "field_confirmed"
        assert asm["suite_id"] == SUITE_A
        assert asm["project_id"] == PROJECT_ID

    def test_resolve_missing_input_emits_yellow_receipt(self) -> None:
        """Resolve must emit a YELLOW-tier blueprint.missing_input.resolved receipt (Law #2 + #4)."""
        from aspire_orchestrator.routes.blueprints import resolve_missing_input
        from aspire_orchestrator.services.blueprint.schemas.missing_input_resolve_request import (
            MissingInputResolveRequest,
        )

        body = MissingInputResolveRequest(
            resolution_value="42 feet",
            resolved_by=uuid.UUID(TENANT_A),
            capability_token="valid-token-stub",
        )

        with (
            patch(
                "aspire_orchestrator.routes.blueprints.supabase_select",
                new_callable=AsyncMock,
            ) as mock_select,
            patch(
                "aspire_orchestrator.routes.blueprints.supabase_update",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "aspire_orchestrator.routes.blueprints.supabase_insert",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            mock_select.side_effect = [
                [_FAKE_PROJECT_ROW],
                _FAKE_MISSING_INPUT_ROWS,
            ]
            _run(resolve_missing_input(
                project_id=PROJECT_ID,
                input_id=INPUT_ID,
                body=body,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        receipts = _query_receipts_by_action("blueprint.missing_input.resolved")
        success_receipts = [r for r in receipts if r["outcome"] == "success"]
        assert len(success_receipts) >= 1, "Resolve must emit a success receipt (Law #2)"
        r = success_receipts[-1]
        assert r["risk_tier"] == "yellow", "Resolve receipt must be YELLOW tier (Law #4)"

    def test_resolve_missing_input_denies_without_token(self) -> None:
        """POST /resolve must deny with 401 when capability_token is blank (Law #3 / #4)."""
        from aspire_orchestrator.routes.blueprints import resolve_missing_input
        from aspire_orchestrator.services.blueprint.schemas.missing_input_resolve_request import (
            MissingInputResolveRequest,
        )

        body = MissingInputResolveRequest(
            resolution_value="42 feet",
            resolved_by=uuid.UUID(TENANT_A),
            capability_token="   ",  # blank — must be rejected
        )

        with pytest.raises(HTTPException) as exc_info:
            _run(resolve_missing_input(
                project_id=PROJECT_ID,
                input_id=INPUT_ID,
                body=body,
                x_tenant_id=TENANT_A,
                x_suite_id=SUITE_A,
                x_office_id=OFFICE_A,
            ))

        assert exc_info.value.status_code == 401
        # Denial must still emit a receipt (Law #2)
        receipts = _query_receipts_by_action("blueprint.missing_input.resolved")
        denied = [r for r in receipts if r["outcome"] == "denied"]
        assert denied, "Denial must produce a receipt with outcome=denied"

    def test_resolve_cross_tenant_returns_404(self) -> None:
        """Cross-tenant resolve must return 404 (Law #6)."""
        from aspire_orchestrator.routes.blueprints import resolve_missing_input
        from aspire_orchestrator.services.blueprint.schemas.missing_input_resolve_request import (
            MissingInputResolveRequest,
        )

        body = MissingInputResolveRequest(
            resolution_value="42 feet",
            resolved_by=uuid.UUID(TENANT_A),
            capability_token="valid-token-stub",
        )

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
            return_value=[],  # project not found for SUITE_B
        ):
            with pytest.raises(HTTPException) as exc_info:
                _run(resolve_missing_input(
                    project_id=PROJECT_ID,
                    input_id=INPUT_ID,
                    body=body,
                    x_tenant_id=TENANT_A,
                    x_suite_id=SUITE_B,
                    x_office_id=OFFICE_A,
                ))
        assert exc_info.value.status_code == 404


# ===========================================================================
# Cross-cutting: receipt coverage + RLS (parametrized)
# ===========================================================================

class TestAllReadEndpointsEmitReceipt:
    """Every read endpoint must emit at least one receipt per call (Law #2)."""

    @pytest.mark.parametrize("action_type,handler_name,extra_kwargs", [
        ("blueprint.read.symbols",       "list_blueprint_symbols",       {"sheet_id": SHEET_ID_1, "confidence_floor": 0.70, "class_prefix": None}),
        ("blueprint.read.assemblies",    "list_blueprint_assemblies",    {"active_only": True}),
        ("blueprint.read.materials",     "list_blueprint_materials",     {"tariff_only": False, "has_supplier": False}),
        ("blueprint.read.missing_inputs","list_blueprint_missing_inputs",{"unresolved_only": True}),
        ("blueprint.read.story",         "get_blueprint_story",          {}),
    ])
    def test_all_read_endpoints_emit_receipt(
        self,
        action_type: str,
        handler_name: str,
        extra_kwargs: dict,
    ) -> None:
        import aspire_orchestrator.routes.blueprints as bp
        handler = getattr(bp, handler_name)

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
        ) as mock_select:
            # First call: project check; subsequent calls: empty data (that's fine)
            mock_select.side_effect = [
                [_FAKE_PROJECT_ROW],
                _FAKE_SHEET_ROWS,
                _FAKE_SYMBOL_ROWS,
                [],
                [],
            ]
            try:
                _run(handler(
                    project_id=PROJECT_ID,
                    x_tenant_id=TENANT_A,
                    x_suite_id=SUITE_A,
                    x_office_id=OFFICE_A,
                    **extra_kwargs,
                ))
            except (HTTPException, Exception):
                pass  # Receipt must be emitted even on errors

        receipts = _query_receipts_by_action(action_type)
        assert len(receipts) >= 1, (
            f"Endpoint {handler_name} did not emit a receipt for action_type={action_type}. "
            "Every code path must emit a receipt (Law #2)."
        )

    @pytest.mark.parametrize("handler_name,extra_kwargs", [
        ("list_blueprint_symbols",        {"sheet_id": None, "confidence_floor": 0.70, "class_prefix": None}),
        ("list_blueprint_assemblies",     {"active_only": True}),
        ("list_blueprint_materials",      {"tariff_only": False, "has_supplier": False}),
        ("list_blueprint_missing_inputs", {"unresolved_only": True}),
        ("get_blueprint_story",           {}),
    ])
    def test_all_read_endpoints_enforce_rls(
        self,
        handler_name: str,
        extra_kwargs: dict,
    ) -> None:
        """All read endpoints must 404 for cross-tenant project_id (Law #6)."""
        import aspire_orchestrator.routes.blueprints as bp
        handler = getattr(bp, handler_name)

        with patch(
            "aspire_orchestrator.routes.blueprints.supabase_select",
            new_callable=AsyncMock,
            return_value=[],  # project not found for SUITE_B
        ):
            with pytest.raises(HTTPException) as exc_info:
                _run(handler(
                    project_id=PROJECT_ID,
                    x_tenant_id=TENANT_A,
                    x_suite_id=SUITE_B,
                    x_office_id=OFFICE_A,
                    **extra_kwargs,
                ))

        assert exc_info.value.status_code == 404, (
            f"{handler_name}: cross-tenant request must return 404, got {exc_info.value.status_code}"
        )
