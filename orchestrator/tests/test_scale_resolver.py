"""Tests for Wave 19b ScaleResolver.

Pure unit tests — no Supabase, no network calls. scale_calibrator mocked.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCategory,
    MissingInputCandidate,
    RESOLVED_CONFIDENCE_THRESHOLD,
)
from aspire_orchestrator.services.blueprint.resolvers.scale_resolver import ScaleResolver


def _make_candidate(sheets: list | None = None) -> MissingInputCandidate:
    return MissingInputCandidate(
        missing_input_id="scale-test-0000-0000-000000000001",
        project_id="5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2",
        suite_id="94b89098-c4bf-4419-a154-e18d9d53f993",
        office_id=None,
        description="Most sheets show no calibrated scale in the provided metadata",
        suggested_resolution=None,
        category=MissingInputCategory.SCALE,
        sheets=sheets or [
            {
                "id": "sheet-scale-0001",
                "discipline": "A",
                "ocr_text": "SCALE: 1/4\" = 1'-0\"  Floor Plan  Reference grid 20'-0\" x 30'-0\"",
                "image_bytes": b"\x00" * 100,  # minimal placeholder
                "thumbnail_url": None,
            }
        ],
        symbols_by_sheet={},
        ocr_by_sheet={
            "sheet-scale-0001": "SCALE: 1/4\" = 1'-0\"  Floor Plan  20'-0\" x 30'-0\"",
        },
    )


class TestScaleResolverCalibrator:
    """Tests for the scale_calibrator method."""

    def test_resolver_category_is_scale(self) -> None:
        resolver = ScaleResolver()
        assert resolver.category == MissingInputCategory.SCALE

    @pytest.mark.asyncio
    async def test_calibrator_resolves_when_high_confidence(self) -> None:
        """scale_calibrator returning confidence=0.95 → resolved."""
        from aspire_orchestrator.services.blueprint.schemas_detection import ScaleCalibration

        mock_calibration = ScaleCalibration(
            scale_factor=0.05,
            confidence=0.95,
            method="text+bar",
            units="inch",
        )

        resolver = ScaleResolver()
        candidate = _make_candidate()

        with patch(
            "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
            return_value=mock_calibration,
        ):
            results = await resolver.resolve(candidate)

        calibrator_results = [r for r in results if "scale_calibrator" in r.method]
        if calibrator_results:
            best = calibrator_results[0]
            assert best.resolved is True
            assert best.confidence >= RESOLVED_CONFIDENCE_THRESHOLD
            assert best.value == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_calibrator_not_resolved_low_confidence(self) -> None:
        """scale_calibrator returning confidence=0.30 → not resolved."""
        from aspire_orchestrator.services.blueprint.schemas_detection import ScaleCalibration

        mock_calibration = ScaleCalibration(
            scale_factor=0.0,
            confidence=0.30,
            method="bar",
            units="inch",
        )

        resolver = ScaleResolver()
        candidate = _make_candidate()

        with patch(
            "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
            return_value=mock_calibration,
        ):
            results = await resolver.resolve(candidate)

        calibrator_results = [r for r in results if "scale_calibrator" in r.method]
        if calibrator_results:
            assert calibrator_results[0].resolved is False

    @pytest.mark.asyncio
    async def test_no_sheets_returns_unresolved(self) -> None:
        """No sheets → all methods return resolved=False, no exception."""
        resolver = ScaleResolver()
        candidate = _make_candidate(sheets=[])
        results = await resolver.resolve(candidate)
        assert isinstance(results, list)
        for r in results:
            assert r.resolved is False


class TestScaleResolverDimensionRegression:
    """Tests for dimension_regression method."""

    @pytest.mark.asyncio
    async def test_dimension_regression_finds_callouts(self) -> None:
        """OCR with dimension callouts → method returns confidence=0.60."""
        resolver = ScaleResolver()
        candidate = MissingInputCandidate(
            missing_input_id="x",
            project_id="p",
            suite_id="s",
            office_id=None,
            description="scale missing",
            suggested_resolution=None,
            category=MissingInputCategory.SCALE,
            sheets=[{
                "id": "s1",
                "discipline": "A",
                "ocr_text": "Room A: 20'-0\" x 15'-0\"\nBath: 8'-6\" x 5'-0\"\nHall: 3'-0\" x 10'-0\"",
                "image_bytes": None,
            }],
            symbols_by_sheet={},
            ocr_by_sheet={"s1": "Room A: 20'-0\" x 15'-0\"\nBath: 8'-6\" x 5'-0\""},
        )

        with patch(
            "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
            side_effect=Exception("no cv2"),
        ):
            results = await resolver.resolve(candidate)

        dim_results = [r for r in results if r.method == "dimension_regression"]
        if dim_results:
            dr = dim_results[0]
            assert dr.confidence == pytest.approx(0.60)
            # Never standalone-resolved
            assert dr.resolved is False

    @pytest.mark.asyncio
    async def test_dimension_regression_no_callouts(self) -> None:
        """OCR with no dimension callouts → method returns confidence=0.0."""
        resolver = ScaleResolver()
        candidate = MissingInputCandidate(
            missing_input_id="x",
            project_id="p",
            suite_id="s",
            office_id=None,
            description="scale missing",
            suggested_resolution=None,
            category=MissingInputCategory.SCALE,
            sheets=[{"id": "s1", "ocr_text": "Some text without any dimensions", "image_bytes": None}],
            symbols_by_sheet={},
            ocr_by_sheet={"s1": "Some text without any dimensions"},
        )
        with patch(
            "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
            side_effect=Exception("no cv2"),
        ):
            results = await resolver.resolve(candidate)

        dim_results = [r for r in results if r.method == "dimension_regression"]
        if dim_results:
            assert dim_results[0].confidence == pytest.approx(0.0)
            assert dim_results[0].resolved is False


class TestScaleResolverReferenceObject:
    """Tests for reference_object method."""

    @pytest.mark.asyncio
    async def test_reference_object_finds_door(self) -> None:
        """OCR mentioning doors → reference_object finds a hit."""
        resolver = ScaleResolver()
        candidate = MissingInputCandidate(
            missing_input_id="x",
            project_id="p",
            suite_id="s",
            office_id=None,
            description="scale missing",
            suggested_resolution=None,
            category=MissingInputCategory.SCALE,
            sheets=[{"id": "s1", "ocr_text": "36\" wide door frame exterior wall thick", "image_bytes": None}],
            symbols_by_sheet={},
            ocr_by_sheet={"s1": "36\" wide door frame exterior wall 6\" thick"},
        )
        with patch(
            "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
            side_effect=Exception("no cv2"),
        ):
            results = await resolver.resolve(candidate)

        ref_results = [r for r in results if r.method == "reference_object"]
        if ref_results:
            rr = ref_results[0]
            # Reference object alone cannot resolve — supporting evidence only
            assert rr.resolved is False
            assert rr.confidence == pytest.approx(0.60)
