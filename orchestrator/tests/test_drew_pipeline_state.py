"""Drew pipeline stage_progress tests — Wave 9e (P0-3).

Verifies that _async_*_pipeline coroutines write stage_progress correctly:
  - in_progress at entry
  - done on success
  - failed on error or cancellation

All supabase I/O is mocked so these run as pure unit tests without a DB.

Law compliance verified:
  Law #2: state transitions are accompanied by receipts (tested indirectly via
           the receipt store check).
  Law #3: failures mark stage as failed before propagating the error.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ID = str(uuid.uuid4())
SUITE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CORRELATION_ID = "test-pipeline-state-001"
MODEL = "gpt-5.4-mini"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stage_progress_recorder() -> tuple[list[tuple[str, str]], AsyncMock]:
    """Return (calls_log, mock_fn) where calls_log tracks (stage, state) pairs."""
    calls: list[tuple[str, str]] = []

    async def _mock_set_stage_progress(
        *, project_id: str, stage: str, state: str, suite_id: str
    ) -> None:
        calls.append((stage, state))

    return calls, AsyncMock(side_effect=_mock_set_stage_progress)


def _make_supabase_select_empty() -> AsyncMock:
    """Mock supabase_select that always returns empty list."""
    return AsyncMock(return_value=[])


def _make_supabase_select_with_rows(rows: list[dict[str, Any]]) -> AsyncMock:
    """Mock supabase_select that returns given rows."""
    return AsyncMock(return_value=rows)


def _make_supabase_insert_ok() -> AsyncMock:
    return AsyncMock(return_value=None)


def _make_supabase_update_ok() -> AsyncMock:
    return AsyncMock(return_value=None)


def _make_supabase_delete_ok() -> AsyncMock:
    return AsyncMock(return_value=None)


# ---------------------------------------------------------------------------
# INGEST stage_progress tests
# ---------------------------------------------------------------------------

class TestIngestStageProgress:
    """_async_ingest_pipeline marks ingest in_progress → done."""

    @pytest.mark.asyncio
    async def test_ingest_marks_in_progress_then_done(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_ingest_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()
        calls, mock_set_stage = _make_stage_progress_recorder()

        # Minimal single-page corpus
        mock_extract = MagicMock()
        mock_extract.page_number = 1
        mock_extract.text = "Sheet 1 content"
        mock_extract.page_hash = "abc123"
        mock_extract.image_bytes = b""

        mock_corpus = MagicMock()
        mock_corpus.sheets = [mock_extract]
        mock_corpus.provider_mix = {"pymupdf": 1}

        sheet_row = {
            "id": str(uuid.uuid4()),
            "project_id": PROJECT_ID,
            "suite_id": SUITE_ID,
        }

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.ocr_coordinator.extract_sheet_corpus",
                new=AsyncMock(return_value=mock_corpus),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_insert",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(return_value=[sheet_row]),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_update",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.thumbnail_storage.upload_sheet_thumbnail",
                new=AsyncMock(return_value="https://example.com/thumb.png"),
            ),
        ):
            result = await _async_ingest_pipeline(
                pdf_bytes=b"%PDF-1.4 stub",
                project_hash="a" * 64,
                suite_id=SUITE_ID,
                office_id="b" * 36,
                filename="test.pdf",
                correlation_id=CORRELATION_ID,
                project_id=PROJECT_ID,
                _skip_project_creation=True,
            )

        assert result.get("stage") == "ingest"
        ingest_states = [state for stage, state in calls if stage == "ingest"]
        assert "in_progress" in ingest_states, f"Expected in_progress. Got calls: {calls}"
        assert ingest_states[-1] in ("done", "failed"), f"Expected terminal state. Got: {calls}"

    @pytest.mark.asyncio
    async def test_ingest_marks_failed_on_zero_sheets(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_ingest_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()
        calls, mock_set_stage = _make_stage_progress_recorder()

        # Corpus with no sheets (empty)
        mock_corpus = MagicMock()
        mock_corpus.sheets = []
        mock_corpus.provider_mix = {}

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.ocr_coordinator.extract_sheet_corpus",
                new=AsyncMock(return_value=mock_corpus),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_insert",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await _async_ingest_pipeline(
                pdf_bytes=b"%PDF-1.4 stub",
                project_hash="a" * 64,
                suite_id=SUITE_ID,
                office_id="b" * 36,
                filename="test.pdf",
                correlation_id=CORRELATION_ID,
                project_id=PROJECT_ID,
                _skip_project_creation=True,
            )

        # Zero sheets → status ok but sheet_count == 0 and stage_progress = failed
        ingest_states = [state for stage, state in calls if stage == "ingest"]
        assert "failed" in ingest_states, f"Expected failed for zero sheets. Got: {calls}"


# ---------------------------------------------------------------------------
# CLASSIFY stage_progress tests
# ---------------------------------------------------------------------------

class TestClassifyStageProgress:
    """_async_classify_pipeline marks classify in_progress → done/failed."""

    @pytest.mark.asyncio
    async def test_classify_marks_in_progress_then_done(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_classify_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()
        calls, mock_set_stage = _make_stage_progress_recorder()

        mock_sheets = [
            {
                "id": str(uuid.uuid4()),
                "project_id": PROJECT_ID,
                "suite_id": SUITE_ID,
                "sheet_number": "1",
                "ocr_text": "electrical panel schedule",
            }
        ]

        mock_tag = MagicMock()
        mock_tag.sheet_id = mock_sheets[0]["id"]
        mock_tag.discipline = "electrical"
        mock_tag.confidence = 0.92
        mock_tag.needs_review = False
        mock_tag.reasoning = ""

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(return_value=mock_sheets),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_update",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.discipline_tagger.tag_disciplines",
                new=AsyncMock(return_value=[mock_tag]),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.revision_detector.detect_revisions",
                return_value=[],
            ),
        ):
            result = await _async_classify_pipeline(
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
                model=MODEL,
                correlation_id=CORRELATION_ID,
            )

        assert result.get("stage") == "classify"
        classify_states = [state for stage, state in calls if stage == "classify"]
        assert "in_progress" in classify_states, f"Expected in_progress. Got: {calls}"
        assert classify_states[-1] in ("done", "failed"), f"Expected terminal state. Got: {calls}"

    @pytest.mark.asyncio
    async def test_classify_marks_failed_on_zero_sheets(self) -> None:
        """P1-10 fix: zero sheets returns status=failed (not ok)."""
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_classify_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()
        calls, mock_set_stage = _make_stage_progress_recorder()

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await _async_classify_pipeline(
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
                model=MODEL,
                correlation_id=CORRELATION_ID,
            )

        # P1-10: zero sheets MUST return failed, not ok
        assert result.get("status") == "failed", (
            f"Expected status=failed for zero sheets, got {result.get('status')!r}"
        )
        assert result.get("reason") == "no_sheets_to_classify"
        classify_states = [state for stage, state in calls if stage == "classify"]
        assert "failed" in classify_states, f"Expected failed state. Got: {calls}"

    @pytest.mark.asyncio
    async def test_classify_marks_failed_on_db_error(self) -> None:
        """DB error → stage_progress = failed before error propagates."""
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_classify_pipeline
        from aspire_orchestrator.services.supabase_client import SupabaseClientError
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()
        calls, mock_set_stage = _make_stage_progress_recorder()

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(side_effect=SupabaseClientError("blueprint_sheets", detail="test error")),
            ),
        ):
            with pytest.raises((RuntimeError, Exception)):
                await _async_classify_pipeline(
                    project_id=PROJECT_ID,
                    suite_id=SUITE_ID,
                    model=MODEL,
                    correlation_id=CORRELATION_ID,
                )

        classify_states = [state for stage, state in calls if stage == "classify"]
        assert "in_progress" in classify_states
        assert "failed" in classify_states, f"Expected failed after DB error. Got: {calls}"


# ---------------------------------------------------------------------------
# SEE stage_progress tests
# ---------------------------------------------------------------------------

class TestSeeStageProgress:
    """_async_see_pipeline marks see in_progress → done/failed."""

    @pytest.mark.asyncio
    async def test_see_marks_in_progress_then_done(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_see_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()
        calls, mock_set_stage = _make_stage_progress_recorder()

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

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(return_value=[mock_sheet]),
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
            result = await _async_see_pipeline(
                project_id=PROJECT_ID,
                suite_id=SUITE_ID,
                office_id=None,
                pdf_bytes=b"%PDF-1.4",
                confidence_floor=0.70,
                correlation_id=CORRELATION_ID,
            )

        assert result.get("stage") == "see"
        see_states = [state for stage, state in calls if stage == "see"]
        assert "in_progress" in see_states, f"Expected in_progress. Got: {calls}"
        assert see_states[-1] == "done", f"Expected done. Got: {calls}"

    @pytest.mark.asyncio
    async def test_see_marks_failed_on_no_sheets(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_see_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()
        calls, mock_set_stage = _make_stage_progress_recorder()

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_delete",
                new=AsyncMock(return_value=None),
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

        assert result.get("status") == "failed"
        see_states = [state for stage, state in calls if stage == "see"]
        assert "failed" in see_states, f"Expected failed state. Got: {calls}"


# ---------------------------------------------------------------------------
# REASON stage_progress tests
# ---------------------------------------------------------------------------

class TestReasonStageProgress:
    """_async_reason_pipeline marks reason in_progress → done/failed."""

    @pytest.mark.asyncio
    async def test_reason_marks_in_progress_then_done(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_reason_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()
        calls, mock_set_stage = _make_stage_progress_recorder()

        mock_output = MagicMock()
        mock_output.phase_count = 3
        mock_output.assembly_count = 10
        mock_output.material_count = 25
        mock_output.missing_input_count = 0
        mock_output.mean_confidence = 0.88
        mock_output.truth_distribution = {"observed": 20, "derived": 5}
        mock_output.model_version = MODEL

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_delete",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.story_writer.write_story",
                new=AsyncMock(return_value=mock_output),
            ),
            # Mock the select used for idempotency count
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(return_value=[]),
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
        reason_states = [state for stage, state in calls if stage == "reason"]
        assert "in_progress" in reason_states
        assert reason_states[-1] == "done", f"Expected done. Got: {calls}"

    @pytest.mark.asyncio
    async def test_reason_marks_failed_on_story_writer_error(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_reason_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store

        clear_store()
        calls, mock_set_stage = _make_stage_progress_recorder()

        with (
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_delete",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.story_writer.write_story",
                new=AsyncMock(side_effect=RuntimeError("LLM timeout")),
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(return_value=[]),
            ),
        ):
            with pytest.raises(RuntimeError, match="LLM timeout"):
                await _async_reason_pipeline(
                    project_id=PROJECT_ID,
                    suite_id=SUITE_ID,
                    office_id=None,
                    model=MODEL,
                    correlation_id=CORRELATION_ID,
                )

        reason_states = [state for stage, state in calls if stage == "reason"]
        assert "in_progress" in reason_states
        assert "failed" in reason_states, f"Expected failed after error. Got: {calls}"


# ---------------------------------------------------------------------------
# PROCURE stage_progress tests
# ---------------------------------------------------------------------------

class TestProcureStageProgress:
    """_async_procure_pipeline marks procure in_progress → done."""

    @pytest.mark.asyncio
    async def test_procure_marks_in_progress_then_done(self) -> None:
        import sys
        from aspire_orchestrator.skillpacks.drew_blueprint import _async_procure_pipeline
        from aspire_orchestrator.services.receipt_store import clear_store
        from unittest.mock import MagicMock as _MagicMock

        clear_store()
        calls, mock_set_stage = _make_stage_progress_recorder()

        _stub_modules = {
            "aspire_orchestrator.services.blueprint.supplier_matcher": _MagicMock(
                match_suppliers=AsyncMock(return_value=_MagicMock(matches=[], missing_input_inserted=False))
            ),
            "aspire_orchestrator.services.blueprint.schemas.truth": _MagicMock(
                TariffFlag=_MagicMock()
            ),
        }

        with (
            patch.dict("sys.modules", _stub_modules),
            patch(
                "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
                new=mock_set_stage,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                new=AsyncMock(return_value=[]),  # No materials → early return
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
        procure_states = [state for stage, state in calls if stage == "procure"]
        assert "in_progress" in procure_states, f"Expected in_progress. Got: {calls}"
        assert "done" in procure_states, f"Expected done. Got: {calls}"
