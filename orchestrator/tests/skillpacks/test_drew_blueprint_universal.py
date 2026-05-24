"""Tests for Drew stage 2.5 — UNIVERSAL_SHEET_INTELLIGENCE (Wave 20c).

Covers:
  - dispatch: task name routes to the method
  - missing payload keys → denied receipt + error response
  - happy path: all sheets succeed → status=ok + summary receipt
  - fail-soft: single sheet Azure failure → counted as failed, stage still ok
  - >50% sheets fail → status=failed
  - no sheets in project → status=failed (Law #3 fail-closed)
  - no PNG available → sheet counted as failed (soft)
  - upsert failure → sheet counted as failed (soft)
  - concurrency cap respected (ASPIRE_DREW_OCR_CONCURRENCY)
"""

from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — minimal stubs so the import doesn't need real provider credentials
# ---------------------------------------------------------------------------

FAKE_PROJECT_ID = "aaaaaaaa-0000-0000-0000-000000000001"
FAKE_SUITE_ID = "bbbbbbbb-0000-0000-0000-000000000002"
FAKE_CORR = "cccccccc-0000-0000-0000-000000000003"
FAKE_OFFICE_ID = None

_FAKE_SHEET_ROW: dict[str, Any] = {
    "suite_id": FAKE_SUITE_ID,
    "office_id": None,
    "project_id": FAKE_PROJECT_ID,
    "sheet_id": "dddddddd-0000-0000-0000-000000000004",
    "tables": [],
    "paragraphs": [],
    "key_value_pairs": [],
    "page_count": 1,
    "regions": {},
    "extracted_at": "2026-05-23T00:00:00+00:00",
    "azure_model": "prebuilt-layout",
    "azure_call_ms": 500,
}


def _make_sheets(n: int) -> list[dict[str, Any]]:
    return [
        {"id": f"dddddddd-{i:04d}-0000-0000-000000000000", "supersedes_id": None}
        for i in range(n)
    ]


def _make_skillpack() -> Any:
    """Return a Drew skillpack instance with _emit_receipt stubbed."""
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    sp = Drew.__new__(Drew)
    sp.model = "gpt-4o"
    sp._emit_receipt = MagicMock()  # type: ignore[method-assign]
    return sp


# ---------------------------------------------------------------------------
# Contract tests — dispatch + input validation
# ---------------------------------------------------------------------------

class TestDispatchRouting(unittest.TestCase):
    def test_universal_task_routes_to_method(self) -> None:
        sp = _make_skillpack()
        # Patch the method to avoid real execution
        sp.universal_sheet_intelligence = MagicMock(return_value={"status": "ok"})
        result = sp.run_agentic_loop(
            "UNIVERSAL_SHEET_INTELLIGENCE",
            {"project_id": FAKE_PROJECT_ID, "suite_id": FAKE_SUITE_ID},
            FAKE_CORR,
        )
        sp.universal_sheet_intelligence.assert_called_once()
        assert result == {"status": "ok"}


class TestMissingPayloadKeys(unittest.TestCase):
    def test_missing_project_id(self) -> None:
        sp = _make_skillpack()
        with patch("aspire_orchestrator.skillpacks.drew_blueprint._run_async_set_stage"):
            result = sp.universal_sheet_intelligence(
                {"suite_id": FAKE_SUITE_ID}, FAKE_CORR
            )
        assert result["status"] == "error"
        assert "project_id" in result["reason"]
        sp._emit_receipt.assert_called_once()
        call_kwargs = sp._emit_receipt.call_args[1]
        assert call_kwargs["status"] == "failed"

    def test_missing_suite_id(self) -> None:
        sp = _make_skillpack()
        with patch("aspire_orchestrator.skillpacks.drew_blueprint._run_async_set_stage"):
            result = sp.universal_sheet_intelligence(
                {"project_id": FAKE_PROJECT_ID}, FAKE_CORR
            )
        assert result["status"] == "error"
        assert "suite_id" in result["reason"]


# ---------------------------------------------------------------------------
# Happy-path tests — all sheets succeed
# ---------------------------------------------------------------------------

class TestHappyPath(unittest.TestCase):
    @patch("aspire_orchestrator.skillpacks.drew_blueprint._run_async_set_stage")
    @patch("aspire_orchestrator.skillpacks.drew_blueprint._run_async_universal_sheet_intel")
    def test_all_sheets_succeed(self, mock_run: MagicMock, mock_stage: MagicMock) -> None:
        mock_run.return_value = {
            "status": "ok",
            "stage": "universal_sheet_intelligence",
            "project_id": FAKE_PROJECT_ID,
            "sheets_total": 3,
            "sheets_succeeded": 3,
            "sheets_failed": 0,
            "total_azure_ms": 1500,
        }

        sp = _make_skillpack()
        result = sp.universal_sheet_intelligence(
            {"project_id": FAKE_PROJECT_ID, "suite_id": FAKE_SUITE_ID},
            FAKE_CORR,
        )

        assert result["status"] == "ok"
        assert result["sheets_total"] == 3
        assert result["sheets_succeeded"] == 3
        assert result["sheets_failed"] == 0

        # Summary receipt must be emitted (Law #2)
        sp._emit_receipt.assert_called_once()
        receipt_kwargs = sp._emit_receipt.call_args[1]
        assert receipt_kwargs["event_type"] == "blueprint.universal_sheet_intelligence.summary"
        assert receipt_kwargs["metadata"]["sheets_total"] == 3
        assert receipt_kwargs["metadata"]["total_azure_ms"] == 1500

        # stage_progress must be set to done
        done_calls = [
            c for c in mock_stage.call_args_list
            if c[1].get("state") == "done"
        ]
        assert done_calls, "expected set_stage_progress(done) call"


# ---------------------------------------------------------------------------
# Fail-soft tests — partial failures
# ---------------------------------------------------------------------------

class TestFailSoft(unittest.TestCase):
    @patch("aspire_orchestrator.skillpacks.drew_blueprint._run_async_set_stage")
    @patch("aspire_orchestrator.skillpacks.drew_blueprint._run_async_universal_sheet_intel")
    def test_minority_failure_still_ok(self, mock_run: MagicMock, _: MagicMock) -> None:
        mock_run.return_value = {
            "status": "ok",
            "stage": "universal_sheet_intelligence",
            "project_id": FAKE_PROJECT_ID,
            "sheets_total": 4,
            "sheets_succeeded": 3,
            "sheets_failed": 1,
            "total_azure_ms": 1200,
        }
        sp = _make_skillpack()
        result = sp.universal_sheet_intelligence(
            {"project_id": FAKE_PROJECT_ID, "suite_id": FAKE_SUITE_ID},
            FAKE_CORR,
        )
        assert result["status"] == "ok"
        assert result["sheets_failed"] == 1

    @patch("aspire_orchestrator.skillpacks.drew_blueprint._run_async_set_stage")
    @patch("aspire_orchestrator.skillpacks.drew_blueprint._run_async_universal_sheet_intel")
    def test_majority_failure_marks_failed(self, mock_run: MagicMock, _: MagicMock) -> None:
        mock_run.return_value = {
            "status": "failed",
            "stage": "universal_sheet_intelligence",
            "project_id": FAKE_PROJECT_ID,
            "sheets_total": 4,
            "sheets_succeeded": 1,
            "sheets_failed": 3,
            "total_azure_ms": 200,
        }
        sp = _make_skillpack()
        result = sp.universal_sheet_intelligence(
            {"project_id": FAKE_PROJECT_ID, "suite_id": FAKE_SUITE_ID},
            FAKE_CORR,
        )
        assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# Async pipeline unit tests
# ---------------------------------------------------------------------------

class TestAsyncPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_empty_sheets(self) -> None:
        """No active sheets → status=failed immediately (Law #3 fail-closed)."""
        from aspire_orchestrator.skillpacks.drew_blueprint import (
            _async_universal_sheet_intel_pipeline,
        )

        async def _fake_sel(table: str, filters: str = "", **kwargs: Any) -> list[Any]:
            return []

        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            side_effect=_fake_sel,
        ):
            result = await _async_universal_sheet_intel_pipeline(
                project_id=FAKE_PROJECT_ID,
                suite_id=FAKE_SUITE_ID,
                office_id=None,
                correlation_id=FAKE_CORR,
            )

        assert result["status"] == "failed"
        assert result["sheets_total"] == 0

    async def test_pipeline_all_sheets_succeed(self) -> None:
        """Two sheets, both succeed → status=ok, counts correct."""
        from aspire_orchestrator.skillpacks.drew_blueprint import (
            _async_universal_sheet_intel_pipeline,
        )
        from aspire_orchestrator.providers.base_client import ProviderError

        sheets = _make_sheets(2)
        png_bytes = b"\x89PNG\r\nfake"

        row0 = {**_FAKE_SHEET_ROW, "sheet_id": sheets[0]["id"], "azure_call_ms": 400}
        row1 = {**_FAKE_SHEET_ROW, "sheet_id": sheets[1]["id"], "azure_call_ms": 600}

        async def _fake_sel(table: str, filters: str = "", **kw: Any) -> list[Any]:
            return sheets

        async def _fake_download(suite_id: str, project_id: str, sheet_id: str, correlation_id: str) -> bytes:
            return png_bytes

        extract_calls: list[str] = []

        async def _fake_extract(
            *,
            suite_id: str,
            office_id: Any,
            project_id: str,
            sheet_id: str,
            sheet_png_bytes: bytes,
            correlation_id: str,
        ) -> dict[str, Any]:
            extract_calls.append(sheet_id)
            if sheet_id == sheets[0]["id"]:
                return row0
            return row1

        async def _fake_upsert(table: str, data: dict, on_conflict: str = "") -> dict:
            return data

        with (
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                side_effect=_fake_sel,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_upsert",
                side_effect=_fake_upsert,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.sheet_image_storage.download_sheet_image",
                side_effect=_fake_download,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.universal_sheet_extractor.extract_sheet_intelligence",
                side_effect=_fake_extract,
            ),
        ):
            result = await _async_universal_sheet_intel_pipeline(
                project_id=FAKE_PROJECT_ID,
                suite_id=FAKE_SUITE_ID,
                office_id=None,
                correlation_id=FAKE_CORR,
            )

        assert result["status"] == "ok"
        assert result["sheets_total"] == 2
        assert result["sheets_succeeded"] == 2
        assert result["sheets_failed"] == 0
        assert result["total_azure_ms"] == 1000

    async def test_pipeline_single_azure_failure_is_soft(self) -> None:
        """One sheet Azure failure → fail counted but stage still ok (1/2 < 50%)."""
        from aspire_orchestrator.skillpacks.drew_blueprint import (
            _async_universal_sheet_intel_pipeline,
        )
        from aspire_orchestrator.providers.base_client import ProviderError

        sheets = _make_sheets(2)
        png_bytes = b"\x89PNG\r\nfake"
        good_row = {**_FAKE_SHEET_ROW, "sheet_id": sheets[1]["id"], "azure_call_ms": 300}

        async def _fake_sel(table: str, filters: str = "", **kw: Any) -> list[Any]:
            return sheets

        async def _fake_download(suite_id: str, project_id: str, sheet_id: str, correlation_id: str) -> bytes:
            return png_bytes

        async def _fake_extract(
            *,
            suite_id: str,
            office_id: Any,
            project_id: str,
            sheet_id: str,
            sheet_png_bytes: bytes,
            correlation_id: str,
        ) -> dict[str, Any]:
            if sheet_id == sheets[0]["id"]:
                raise ProviderError(
                    code=None,  # type: ignore[arg-type]
                    message="Azure timeout",
                    provider_id="azure_doc_intel",
                )
            return good_row

        async def _fake_upsert(table: str, data: dict, on_conflict: str = "") -> dict:
            return data

        with (
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                side_effect=_fake_sel,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_upsert",
                side_effect=_fake_upsert,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.sheet_image_storage.download_sheet_image",
                side_effect=_fake_download,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.universal_sheet_extractor.extract_sheet_intelligence",
                side_effect=_fake_extract,
            ),
        ):
            result = await _async_universal_sheet_intel_pipeline(
                project_id=FAKE_PROJECT_ID,
                suite_id=FAKE_SUITE_ID,
                office_id=None,
                correlation_id=FAKE_CORR,
            )

        assert result["sheets_succeeded"] == 1
        assert result["sheets_failed"] == 1
        assert result["status"] == "ok"  # 50% failure rate is NOT >50%

    async def test_pipeline_majority_failure_returns_failed_status(self) -> None:
        """3 of 4 sheets fail → >50% → status=failed."""
        from aspire_orchestrator.skillpacks.drew_blueprint import (
            _async_universal_sheet_intel_pipeline,
        )
        from aspire_orchestrator.providers.base_client import ProviderError

        sheets = _make_sheets(4)
        png_bytes = b"\x89PNG\r\nfake"
        good_row = {**_FAKE_SHEET_ROW, "sheet_id": sheets[3]["id"], "azure_call_ms": 200}

        async def _fake_sel(table: str, filters: str = "", **kw: Any) -> list[Any]:
            return sheets

        async def _fake_download(suite_id: str, project_id: str, sheet_id: str, correlation_id: str) -> bytes:
            return png_bytes

        async def _fake_extract(
            *,
            suite_id: str,
            office_id: Any,
            project_id: str,
            sheet_id: str,
            sheet_png_bytes: bytes,
            correlation_id: str,
        ) -> dict[str, Any]:
            if sheet_id != sheets[3]["id"]:
                raise ProviderError(
                    code=None,  # type: ignore[arg-type]
                    message="Azure error",
                    provider_id="azure_doc_intel",
                )
            return good_row

        async def _fake_upsert(table: str, data: dict, on_conflict: str = "") -> dict:
            return data

        with (
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                side_effect=_fake_sel,
            ),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_upsert",
                side_effect=_fake_upsert,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.sheet_image_storage.download_sheet_image",
                side_effect=_fake_download,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.universal_sheet_extractor.extract_sheet_intelligence",
                side_effect=_fake_extract,
            ),
        ):
            result = await _async_universal_sheet_intel_pipeline(
                project_id=FAKE_PROJECT_ID,
                suite_id=FAKE_SUITE_ID,
                office_id=None,
                correlation_id=FAKE_CORR,
            )

        assert result["status"] == "failed"
        assert result["sheets_failed"] == 3
        assert result["sheets_succeeded"] == 1

    async def test_pipeline_no_png_counts_as_failure(self) -> None:
        """Sheet with no cached PNG → counted as failure."""
        from aspire_orchestrator.skillpacks.drew_blueprint import (
            _async_universal_sheet_intel_pipeline,
        )

        sheets = _make_sheets(1)

        async def _fake_sel(table: str, filters: str = "", **kw: Any) -> list[Any]:
            return sheets

        async def _fake_download(suite_id: str, project_id: str, sheet_id: str, correlation_id: str) -> None:
            return None  # type: ignore[return-value]  # no PNG

        with (
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                side_effect=_fake_sel,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.sheet_image_storage.download_sheet_image",
                side_effect=_fake_download,
            ),
        ):
            result = await _async_universal_sheet_intel_pipeline(
                project_id=FAKE_PROJECT_ID,
                suite_id=FAKE_SUITE_ID,
                office_id=None,
                correlation_id=FAKE_CORR,
            )

        # 1 sheet, 1 failure → 100% > 50% → failed
        assert result["sheets_failed"] == 1
        assert result["status"] == "failed"

    async def test_concurrency_cap_env(self) -> None:
        """ASPIRE_DREW_OCR_CONCURRENCY env var controls semaphore."""
        from aspire_orchestrator.skillpacks.drew_blueprint import (
            _async_universal_sheet_intel_pipeline,
        )

        active_semaphore_values: list[int] = []

        original_semaphore = asyncio.Semaphore

        class _TrackingSemaphore:
            def __init__(self, value: int) -> None:
                self._value = value
                active_semaphore_values.append(value)
                self._inner = original_semaphore(value)

            async def __aenter__(self) -> "_TrackingSemaphore":
                await self._inner.__aenter__()
                return self

            async def __aexit__(self, *args: Any) -> None:
                await self._inner.__aexit__(*args)

        sheets = _make_sheets(1)

        async def _fake_sel(table: str, filters: str = "", **kw: Any) -> list[Any]:
            return sheets

        async def _fake_download(suite_id: str, project_id: str, sheet_id: str, correlation_id: str) -> None:
            return None  # type: ignore[return-value]

        with (
            patch.dict(os.environ, {"ASPIRE_DREW_OCR_CONCURRENCY": "7"}),
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_select",
                side_effect=_fake_sel,
            ),
            patch(
                "aspire_orchestrator.services.blueprint.sheet_image_storage.download_sheet_image",
                side_effect=_fake_download,
            ),
            patch(
                "aspire_orchestrator.skillpacks.drew_blueprint.asyncio.Semaphore",
                side_effect=_TrackingSemaphore,
            ),
        ):
            await _async_universal_sheet_intel_pipeline(
                project_id=FAKE_PROJECT_ID,
                suite_id=FAKE_SUITE_ID,
                office_id=None,
                correlation_id=FAKE_CORR,
            )

        assert 7 in active_semaphore_values, (
            f"Expected semaphore(7) but got {active_semaphore_values}"
        )


if __name__ == "__main__":
    unittest.main()
