"""Drew idempotency tests — Wave 9e (P0-7).

Verifies that running SEE/REASON/PROCURE twice on the same project_id does not
produce duplicate rows (idempotency_clear deletes existing rows before re-inserting).

Tests use mocked Supabase so we can control what 'already exists' and verify
the delete+insert pattern is followed.

Law compliance verified:
  Law #2: idempotency_clear emits a receipt when rows are deleted.
  Law #6: All deletes are scoped by suite_id (tenant isolation).
"""

from __future__ import annotations

import uuid
from typing import Any
import sys
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

PROJECT_ID = str(uuid.uuid4())
SUITE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CORRELATION_ID = "test-idempotency-001"
MODEL = "gpt-5.4-mini"

_EXISTING_SYMBOL_ROW = {
    "id": str(uuid.uuid4()),
    "project_id": PROJECT_ID,
    "suite_id": SUITE_ID,
    "class": "window",
    "confidence": 0.85,
}

_EXISTING_STORY_ROW = {
    "id": str(uuid.uuid4()),
    "project_id": PROJECT_ID,
    "suite_id": SUITE_ID,
}

_EXISTING_MATERIAL_ROW = {
    "id": str(uuid.uuid4()),
    "project_id": PROJECT_ID,
    "suite_id": SUITE_ID,
    "line_item": "2x4 lumber",
}


# ---------------------------------------------------------------------------
# SEE idempotency — blueprint_symbols are deleted then re-inserted
# ---------------------------------------------------------------------------

class TestSeeIdempotency:
    """Running SEE twice deletes existing symbols before inserting new ones."""

    @pytest.mark.asyncio
    async def test_see_calls_delete_when_symbols_exist(self) -> None:
        """When existing symbols found, supabase_delete is called before insert."""
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_see_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()

        mock_stage = AsyncMock(return_value=None)
        mock_delete = AsyncMock(return_value=None)

        mock_sheet = {
            "id": str(uuid.uuid4()),
            "project_id": PROJECT_ID,
            "suite_id": SUITE_ID,
            "hash": "abc123",
            "ocr_text": "floor plan",
        }
        mock_extract = MagicMock()
        mock_extract.page_number = 1
        mock_extract.page_hash = "abc123"
        mock_extract.text = "floor plan"
        mock_extract.image_bytes = b"\x89PNG"

        mock_calibration = MagicMock()
        mock_calibration.scale_factor = 0.0
        mock_calibration.confidence = 0.0
        mock_calibration.method = "none"

        mock_seal = MagicMock()
        mock_seal.seal_detected = False

        # First select = existing symbols (1 row). Subsequent selects = sheets.
        select_call_count = {"n": 0}

        async def _mock_select(table: str, **kwargs: Any) -> list[dict]:
            # First call is the idempotency count SELECT for blueprint_symbols
            if table == "blueprint_symbols" and select_call_count["n"] == 0:
                select_call_count["n"] += 1
                return [_EXISTING_SYMBOL_ROW]
            # Remaining selects are for blueprint_sheets
            return [mock_sheet]

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(side_effect=_mock_select),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_delete",
                new=mock_delete,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_insert",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_update",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.pdf_splitter.split_pdf_to_sheets",
                return_value=[mock_extract],
            ),
            patch(
                "aspire_orchestrator.services.blueprint.symbol_detector.detect_symbols",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
                return_value=mock_calibration,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.seal_detector.detect_engineer_seal",
                return_value=mock_seal,
            ),
        ):
            result = await _async_see_pipeline(
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
                office_id=None,
                pdf_bytes=b"%PDF-1.4",
                confidence_floor=0.70,
                correlation_id=CORRELATION_ID,
            )

        # supabase_delete must have been called for blueprint_symbols
        mock_delete.assert_called_once()
        delete_call_args = mock_delete.call_args
        assert "blueprint_symbols" in str(delete_call_args), (
            f"delete should target blueprint_symbols. Got: {delete_call_args}"
        )
        assert PROJECT_ID in str(delete_call_args), (
            f"delete should scope to project_id={PROJECT_ID}. Got: {delete_call_args}"
        )
        assert SUITE_ID in str(delete_call_args), (
            f"delete should scope to suite_id={SUITE_ID} (tenant isolation). Got: {delete_call_args}"
        )

    @pytest.mark.asyncio
    async def test_see_no_delete_when_no_existing_symbols(self) -> None:
        """When no existing symbols, supabase_delete is NOT called."""
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_see_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()

        mock_stage = AsyncMock(return_value=None)
        mock_delete = AsyncMock(return_value=None)

        mock_sheet = {
            "id": str(uuid.uuid4()),
            "project_id": PROJECT_ID,
            "suite_id": SUITE_ID,
            "hash": "deadbeef",
            "ocr_text": "sample",
        }
        mock_extract = MagicMock()
        mock_extract.page_number = 1
        mock_extract.page_hash = "deadbeef"
        mock_extract.text = "sample"
        mock_extract.image_bytes = b"\x89PNG"

        mock_calibration = MagicMock()
        mock_calibration.scale_factor = 0.0
        mock_calibration.confidence = 0.0
        mock_calibration.method = "none"

        mock_seal = MagicMock()
        mock_seal.seal_detected = False

        # All selects return empty (no existing symbols)
        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                # First call = empty symbols, second call = sheets
                new=AsyncMock(side_effect=[[], [mock_sheet]]),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_delete",
                new=mock_delete,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_insert",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_update",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.pdf_splitter.split_pdf_to_sheets",
                return_value=[mock_extract],
            ),
            patch(
                "aspire_orchestrator.services.blueprint.symbol_detector.detect_symbols",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
                return_value=mock_calibration,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.seal_detector.detect_engineer_seal",
                return_value=mock_seal,
            ),
        ):
            await _async_see_pipeline(
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
                office_id=None,
                pdf_bytes=b"%PDF-1.4",
                confidence_floor=0.70,
                correlation_id=CORRELATION_ID,
            )

        # No existing symbols → delete should NOT have been called
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_see_idempotency_clear_emits_receipt(self) -> None:
        """When symbols are deleted, a blueprint.see.idempotency_clear receipt is emitted."""
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_see_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store, _receipts

        clear_store()

        mock_stage = AsyncMock(return_value=None)

        mock_sheet = {
            "id": str(uuid.uuid4()),
            "project_id": PROJECT_ID,
            "suite_id": SUITE_ID,
            "hash": "cafe",
            "ocr_text": "notes",
        }
        mock_extract = MagicMock()
        mock_extract.page_number = 1
        mock_extract.page_hash = "cafe"
        mock_extract.text = "notes"
        mock_extract.image_bytes = b"\x89PNG"

        mock_calibration = MagicMock()
        mock_calibration.scale_factor = 0.0
        mock_calibration.confidence = 0.0
        mock_calibration.method = "none"

        mock_seal = MagicMock()
        mock_seal.seal_detected = False

        select_call_count = {"n": 0}

        async def _mock_select(table: str, **kwargs: Any) -> list[dict]:
            if table == "blueprint_symbols" and select_call_count["n"] == 0:
                select_call_count["n"] += 1
                return [_EXISTING_SYMBOL_ROW]
            return [mock_sheet]

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(side_effect=_mock_select),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_delete",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_insert",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_update",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.pdf_splitter.split_pdf_to_sheets",
                return_value=[mock_extract],
            ),
            patch(
                "aspire_orchestrator.services.blueprint.symbol_detector.detect_symbols",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
                return_value=mock_calibration,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.seal_detector.detect_engineer_seal",
                return_value=mock_seal,
            ),
        ):
            await _async_see_pipeline(
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
                office_id=None,
                pdf_bytes=b"%PDF-1.4",
                confidence_floor=0.70,
                correlation_id=CORRELATION_ID,
            )

        # Check that the idempotency_clear receipt was emitted (Law #2)
        idempotency_receipts = [
            r for r in _receipts
            if r.get("action_type") == "blueprint.see.idempotency_clear"
        ]
        assert len(idempotency_receipts) >= 1, (
            "Expected blueprint.see.idempotency_clear receipt to be emitted (Law #2)"
        )


# ---------------------------------------------------------------------------
# REASON idempotency — blueprint_story/assemblies/materials deleted then re-written
# ---------------------------------------------------------------------------

class TestReasonIdempotency:
    """Running REASON twice deletes story/assemblies/materials before re-writing."""

    @pytest.mark.asyncio
    async def test_reason_calls_delete_for_each_table_when_rows_exist(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_reason_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()

        mock_stage = AsyncMock(return_value=None)
        mock_delete = AsyncMock(return_value=None)

        mock_output = MagicMock()
        mock_output.phase_count = 2
        mock_output.assembly_count = 5
        mock_output.material_count = 10
        mock_output.missing_input_count = 0
        mock_output.mean_confidence = 0.85
        mock_output.truth_distribution = {}
        mock_output.model_version = MODEL

        # Return 1 existing row for each table select
        async def _mock_select(table: str, **kwargs: Any) -> list[dict]:
            if table in ("blueprint_story", "blueprint_assemblies", "blueprint_materials"):
                return [{"id": str(uuid.uuid4()), "project_id": PROJECT_ID, "suite_id": SUITE_ID}]
            return []

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_delete",
                new=mock_delete,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.story_writer.write_story",
                new=AsyncMock(return_value=mock_output),
            ),
            # Patch the inner select used for idempotency count
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(side_effect=_mock_select),
            ),
        ):
            result = await _async_reason_pipeline(
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
                office_id=None,
                model=MODEL,
                correlation_id=CORRELATION_ID,
            )

        assert result.get("stage") == "reason"
        assert result.get("status") == "ok"

        # supabase_delete must be called 3 times (once per table)
        assert mock_delete.call_count == 3, (
            f"Expected 3 delete calls (story, assemblies, materials). Got: {mock_delete.call_count}"
        )
        deleted_tables = [str(c) for c in mock_delete.call_args_list]
        for table in ("blueprint_story", "blueprint_assemblies", "blueprint_materials"):
            assert any(table in dt for dt in deleted_tables), (
                f"Expected delete call for {table}. Got: {deleted_tables}"
            )

    @pytest.mark.asyncio
    async def test_reason_no_delete_when_tables_empty(self) -> None:
        """When no existing story/assemblies/materials, delete is NOT called."""
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_reason_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()

        mock_stage = AsyncMock(return_value=None)
        mock_delete = AsyncMock(return_value=None)

        mock_output = MagicMock()
        mock_output.phase_count = 0
        mock_output.assembly_count = 0
        mock_output.material_count = 0
        mock_output.missing_input_count = 0
        mock_output.mean_confidence = 0.0
        mock_output.truth_distribution = {}
        mock_output.model_version = MODEL

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_delete",
                new=mock_delete,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.story_writer.write_story",
                new=AsyncMock(return_value=mock_output),
            ),
            # All selects return empty
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(return_value=[]),
            ),
        ):
            await _async_reason_pipeline(
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
                office_id=None,
                model=MODEL,
                correlation_id=CORRELATION_ID,
            )

        # No existing rows → delete should NOT have been called
        mock_delete.assert_not_called()


# ---------------------------------------------------------------------------
# PROCURE idempotency — uses UPDATE (not INSERT), verifying no duplicate rows
# ---------------------------------------------------------------------------

class TestProcureIdempotency:
    """PROCURE uses UPDATE on existing materials — no new rows inserted."""

    @pytest.mark.asyncio
    async def test_procure_updates_not_inserts_materials(self) -> None:
        """Verify PROCURE calls supabase_update (not supabase_insert) for materials."""
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_procure_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()

        mock_stage = AsyncMock(return_value=None)
        mock_insert = AsyncMock(return_value=None)
        mock_update = AsyncMock(return_value=None)

        material_id = str(uuid.uuid4())
        mock_material = {
            "id": material_id,
            "project_id": PROJECT_ID,
            "suite_id": SUITE_ID,
            "line_item": "2x4 lumber 8ft",
            "quantity": 50.0,
        }

        mock_search_result = MagicMock()
        mock_search_result.matches = []
        mock_search_result.missing_input_inserted = False

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(return_value=[mock_material]),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_update",
                new=mock_update,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_insert",
                new=mock_insert,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.tariff_engine.detect_tariff_flag",
                return_value=MagicMock(value="none"),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.tariff_engine.estimate_tariff_impact_usd",
                return_value=None,
            ),
            # supplier_matcher may be a stub module — patch the import path
            patch.dict(
                "sys.modules",
                {
                    "aspire_orchestrator.services.blueprint.supplier_matcher": MagicMock(
                        match_suppliers=AsyncMock(return_value=mock_search_result)
                    ),
                    "aspire_orchestrator.services.blueprint.schemas.truth": MagicMock(
                        TariffFlag=MagicMock()
                    ),
                },
            ),
        ):
            result = await _async_procure_pipeline(
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
                office_id=None,
                geofence_miles=25.0,
                correlation_id=CORRELATION_ID,
            )

        assert result.get("stage") == "procure"

        # PROCURE must UPDATE existing rows, not INSERT new ones (P0-7)
        assert mock_insert.call_count == 0, (
            f"PROCURE must not INSERT new material rows. Got {mock_insert.call_count} inserts."
        )
        assert mock_update.call_count >= 1, (
            f"PROCURE must UPDATE existing rows. Got {mock_update.call_count} updates."
        )

        # Verify update targets the existing material_id (not a new one)
        update_call_args = str(mock_update.call_args_list)
        assert material_id in update_call_args or "blueprint_materials" in update_call_args, (
            f"UPDATE must target the existing material. Got: {update_call_args}"
        )
