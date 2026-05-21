"""Tests for Wave 19.1a SheetDataProvider — pure unit, no real Storage/Supabase.

Asserts:
  * Construction enforces non-empty suite_id / project_id (Law #6).
  * PNG fetch hits the underlying ``download_sheet_image`` ONCE per sheet,
    then serves from cache (including ``None`` results — cache-miss is
    sticky for the lifetime of the instance).
  * YOLO fetch hits ``supabase_select`` ONCE per sheet, then caches.
  * RLS scoping: every YOLO query carries both ``sheet_id`` and the
    provider's own ``suite_id`` in the PostgREST filter.
  * Both fetchers swallow underlying exceptions and degrade to
    ``None`` / ``[]`` (Law #3 — resolvers never crash the cascade).
  * Empty sheet_id short-circuits without touching the network.
  * ``get_legend`` and ``get_spatial_ocr_bindings`` return None today
    (deferred per the module docstring) — locked here so a future
    accidental "stub returns fake data" change fails this test.
  * ``cache_stats`` shape is stable (used by receipts in later waves).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.services.blueprint.resolvers.sheet_data_provider import (
    SheetDataProvider,
)


_SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
_PROJECT = "5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2"
_SHEET = "63fce39a-61a3-44c6-87c9-a48e08b4589c"
_CORR = "test-corr-id"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_requires_suite_id(self) -> None:
        with pytest.raises(ValueError, match="suite_id"):
            SheetDataProvider(suite_id="", project_id=_PROJECT, correlation_id=_CORR)

    def test_requires_project_id(self) -> None:
        with pytest.raises(ValueError, match="project_id"):
            SheetDataProvider(suite_id=_SUITE, project_id="", correlation_id=_CORR)

    def test_empty_correlation_id_allowed(self) -> None:
        # Cascade may run without a correlation id (e.g. unit fixtures) —
        # provider should not raise; downstream logging tolerates empty.
        provider = SheetDataProvider(
            suite_id=_SUITE, project_id=_PROJECT, correlation_id=""
        )
        assert provider.suite_id == _SUITE
        assert provider.project_id == _PROJECT

    def test_exposes_scoping_identity(self) -> None:
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        assert provider.suite_id == _SUITE
        assert provider.project_id == _PROJECT


# ---------------------------------------------------------------------------
# get_sheet_png
# ---------------------------------------------------------------------------


class TestGetSheetPng:
    @pytest.mark.asyncio
    async def test_returns_png_bytes_on_success(self) -> None:
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        with patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.download_sheet_image",
            new=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nFAKE"),
        ) as dl:
            result = await provider.get_sheet_png(_SHEET)
        assert result == b"\x89PNG\r\n\x1a\nFAKE"
        assert dl.await_count == 1
        # Confirm RLS-correct args passed through.
        kwargs = dl.await_args.kwargs
        assert kwargs["suite_id"] == _SUITE
        assert kwargs["project_id"] == _PROJECT
        assert kwargs["sheet_id"] == _SHEET

    @pytest.mark.asyncio
    async def test_caches_success(self) -> None:
        """Second call for the same sheet must not re-hit Storage."""
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        mock = AsyncMock(return_value=b"PNG_BYTES")
        with patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.download_sheet_image",
            new=mock,
        ):
            first = await provider.get_sheet_png(_SHEET)
            second = await provider.get_sheet_png(_SHEET)
        assert first == second == b"PNG_BYTES"
        assert mock.await_count == 1, "expected exactly one Storage call across two get_sheet_png() calls"

    @pytest.mark.asyncio
    async def test_caches_none_result(self) -> None:
        """Cache miss (None) is also sticky — don't hammer Storage on every call."""
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        mock = AsyncMock(return_value=None)
        with patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.download_sheet_image",
            new=mock,
        ):
            first = await provider.get_sheet_png(_SHEET)
            second = await provider.get_sheet_png(_SHEET)
        assert first is None and second is None
        assert mock.await_count == 1

    @pytest.mark.asyncio
    async def test_swallows_exceptions(self) -> None:
        """Underlying network/Storage error must NOT propagate (Law #3)."""
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        with patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.download_sheet_image",
            new=AsyncMock(side_effect=RuntimeError("storage offline")),
        ):
            result = await provider.get_sheet_png(_SHEET)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_sheet_id_short_circuits(self) -> None:
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        mock = AsyncMock(return_value=b"X")
        with patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.download_sheet_image",
            new=mock,
        ):
            result = await provider.get_sheet_png("")
        assert result is None
        assert mock.await_count == 0, "empty sheet_id must not reach Storage"


# ---------------------------------------------------------------------------
# get_yolo_detections
# ---------------------------------------------------------------------------


class TestGetYoloDetections:
    @pytest.mark.asyncio
    async def test_returns_detection_rows(self) -> None:
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        rows = [
            {"id": "d1", "sheet_id": _SHEET, "class": "door", "confidence": 0.9},
            {"id": "d2", "sheet_id": _SHEET, "class": "window", "confidence": 0.7},
        ]
        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new=AsyncMock(return_value=rows),
        ) as sel:
            result = await provider.get_yolo_detections(_SHEET)
        assert result == rows
        # Filter must include BOTH sheet_id AND suite_id (Law #6 defense in depth).
        called_filter = sel.await_args.kwargs["filters"]
        assert f"sheet_id=eq.{_SHEET}" in called_filter
        assert f"suite_id=eq.{_SUITE}" in called_filter

    @pytest.mark.asyncio
    async def test_caches_results(self) -> None:
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        mock = AsyncMock(return_value=[{"id": "d1"}])
        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new=mock,
        ):
            await provider.get_yolo_detections(_SHEET)
            await provider.get_yolo_detections(_SHEET)
        assert mock.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_result_caches_as_empty_list(self) -> None:
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        mock = AsyncMock(return_value=[])
        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new=mock,
        ):
            first = await provider.get_yolo_detections(_SHEET)
            second = await provider.get_yolo_detections(_SHEET)
        assert first == [] and second == []
        assert mock.await_count == 1

    @pytest.mark.asyncio
    async def test_swallows_exceptions(self) -> None:
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new=AsyncMock(side_effect=RuntimeError("db offline")),
        ):
            result = await provider.get_yolo_detections(_SHEET)
        assert result == []

    @pytest.mark.asyncio
    async def test_empty_sheet_id_short_circuits(self) -> None:
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        mock = AsyncMock(return_value=[{"x": 1}])
        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new=mock,
        ):
            result = await provider.get_yolo_detections("")
        assert result == []
        assert mock.await_count == 0


# ---------------------------------------------------------------------------
# Deferred methods (locked at None)
# ---------------------------------------------------------------------------


class TestDeferredMethods:
    @pytest.mark.asyncio
    async def test_get_legend_returns_none(self) -> None:
        """Locked at None until upstream legend persistence is decided.

        If a future change makes this return a non-None value, update the
        module docstring's "Out of scope" section AND every resolver that
        depends on the interface — don't silently start returning fake data.
        """
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        assert await provider.get_legend(page_num=1, discipline="E") is None

    @pytest.mark.asyncio
    async def test_get_spatial_ocr_bindings_returns_none(self) -> None:
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        assert await provider.get_spatial_ocr_bindings(_SHEET) is None


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class TestCacheStats:
    @pytest.mark.asyncio
    async def test_cache_stats_shape_and_counts(self) -> None:
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        with patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.download_sheet_image",
            new=AsyncMock(return_value=b"PNG"),
        ):
            await provider.get_sheet_png(_SHEET)
        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new=AsyncMock(return_value=[{"id": "d1"}, {"id": "d2"}]),
        ):
            await provider.get_yolo_detections(_SHEET)

        stats = provider.cache_stats()
        assert set(stats.keys()) == {
            "png_cache_entries",
            "png_cache_hits_with_bytes",
            "yolo_cache_entries",
            "yolo_detections_total",
        }
        assert stats["png_cache_entries"] == 1
        assert stats["png_cache_hits_with_bytes"] == 1
        assert stats["yolo_cache_entries"] == 1
        assert stats["yolo_detections_total"] == 2


# ---------------------------------------------------------------------------
# Integration with MissingInputCandidate (data_provider field present and
# defaults to None so pre-19.1 fixtures still work)
# ---------------------------------------------------------------------------


class TestCandidateIntegration:
    def test_candidate_default_data_provider_is_none(self) -> None:
        from aspire_orchestrator.services.blueprint.resolvers.base import (
            MissingInputCandidate,
            MissingInputCategory,
        )
        c = MissingInputCandidate(
            missing_input_id="mi-1",
            project_id=_PROJECT,
            suite_id=_SUITE,
            office_id=None,
            description="x",
            suggested_resolution=None,
            category=MissingInputCategory.UNKNOWN,
        )
        assert c.data_provider is None

    def test_build_candidate_threads_data_provider_through(self) -> None:
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            build_candidate,
        )
        provider = SheetDataProvider(_SUITE, _PROJECT, _CORR)
        row = {
            "id": "mi-1",
            "project_id": _PROJECT,
            "suite_id": _SUITE,
            "office_id": None,
            "description": "Sheet 19 scale calibration is not confirmed",
            "suggested_resolution": None,
        }
        candidate = build_candidate(
            row, sheets=[], symbols_by_sheet={}, ocr_by_sheet={},
            data_provider=provider,
        )
        assert candidate.data_provider is provider

    def test_build_candidate_omitting_data_provider_is_back_compat(self) -> None:
        """Existing pre-19.1 callers that omit the kwarg must still work."""
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            build_candidate,
        )
        row = {
            "id": "mi-1",
            "project_id": _PROJECT,
            "suite_id": _SUITE,
            "office_id": None,
            "description": "discipline ambiguity",
            "suggested_resolution": None,
        }
        candidate = build_candidate(row, sheets=[], symbols_by_sheet={}, ocr_by_sheet={})
        assert candidate.data_provider is None
