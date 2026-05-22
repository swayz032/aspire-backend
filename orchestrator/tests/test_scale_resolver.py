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
            # Wave 19.1f — value contract changed: now px/ft for consensus
            # comparability. ScaleCalibration.scale_factor=0.05 (in/px) →
            # px/ft = 12 / 0.05 = 240.0.
            assert best.value == pytest.approx(240.0)

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


# ---------------------------------------------------------------------------
# Wave 19.1f (Commit 3) — CAD-door geometric method + multi-method consensus
# ---------------------------------------------------------------------------


from typing import Any  # noqa: E402  (test-only helper imports)
from unittest.mock import AsyncMock, MagicMock  # noqa: E402


def _door_detection(width_px: float, *, cls: str = "door") -> dict[str, Any]:
    """Construct a blueprint_symbols-shaped row with the production bbox shape.

    Bbox jsonb verified against production data on 2026-05-21: ``{x, y, w, h}``.
    Tests must use this shape, NOT ``[x_min, y_min, x_max, y_max]``.
    """
    return {
        "class": cls,
        "bbox": {"x": 100.0, "y": 100.0, "w": width_px, "h": width_px * 0.9},
        "confidence": 0.92,
    }


def _candidate_with_doors(
    detections_by_sheet: dict[str, list[dict[str, Any]]],
    *,
    provider: Any | None = None,
    sheet_ocr_text: str = "SCALE bar present",
) -> MissingInputCandidate:
    """Build a SCALE candidate with multiple sheets and a doors-only YOLO map.

    Each sheet carries non-empty ``ocr_text`` so ``_method_scale_calibrator``'s
    ``if not png_bytes and not ocr_text: continue`` guard does not skip
    every sheet and zero out the calibrator path. Consensus tests need
    the calibrator to actually be invoked (the mocked return value is
    what drives the test assertions).
    """
    sheets = [
        {"id": sid, "discipline": "A", "ocr_text": sheet_ocr_text}
        for sid in detections_by_sheet
    ]
    return MissingInputCandidate(
        missing_input_id="scale-geo-0000-0000-000000000001",
        project_id="5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2",
        suite_id="94b89098-c4bf-4419-a154-e18d9d53f993",
        office_id=None,
        description="Most sheets show no calibrated scale",
        suggested_resolution=None,
        category=MissingInputCategory.SCALE,
        sheets=sheets,
        symbols_by_sheet=detections_by_sheet,
        ocr_by_sheet={sid: sheet_ocr_text for sid in detections_by_sheet},
        data_provider=provider,
    )


class TestGeometricDoorReference:
    """The Wave 11g-v2 payoff lever."""

    @pytest.mark.asyncio
    async def test_resolves_at_0_85_with_5_plus_doors(self) -> None:
        """5+ doors detected → confidence 0.85, resolves, value=pixels-per-foot."""
        resolver = ScaleResolver()
        # 5 doors, all ~600px wide (real plan-view scale at 200 DPI = 1/4"=1'-0")
        # → 600 px / 3 ft = 200 px/ft.
        detections = [_door_detection(600.0) for _ in range(5)]
        cand = _candidate_with_doors({"sheet-A": detections})

        result = await resolver._method_geometric_door_reference(cand)

        assert result.method == "geometric_door_reference"
        assert result.resolved is True
        assert result.confidence == pytest.approx(0.85)
        assert result.value == pytest.approx(200.0)
        assert result.evidence["doors_detected_total"] == 5
        assert result.evidence["pixels_per_foot_derived"] == pytest.approx(200.0)
        assert result.evidence["derivation_method"] == "door_36in"

    @pytest.mark.asyncio
    async def test_low_confidence_with_2_4_doors(self) -> None:
        """2-4 doors → confidence 0.75, NOT resolved (eligible for consensus)."""
        resolver = ScaleResolver()
        detections = [_door_detection(600.0) for _ in range(3)]
        cand = _candidate_with_doors({"sheet-A": detections})

        result = await resolver._method_geometric_door_reference(cand)
        assert result.resolved is False
        assert result.confidence == pytest.approx(0.75)
        # Value still present so consensus pass can compare against it.
        assert result.value == pytest.approx(200.0)
        assert result.evidence["doors_detected_total"] == 3

    @pytest.mark.asyncio
    async def test_skip_with_one_door(self) -> None:
        """1 door is insufficient for a median — fail closed, no value."""
        resolver = ScaleResolver()
        cand = _candidate_with_doors({"sheet-A": [_door_detection(600.0)]})
        result = await resolver._method_geometric_door_reference(cand)
        assert result.resolved is False
        assert result.confidence == 0.0
        assert result.value is None
        assert result.evidence["skip_reason"] == "insufficient_doors_for_median"
        assert result.evidence["doors_detected_total"] == 1

    @pytest.mark.asyncio
    async def test_legend_glyph_sanity_floor_rejects_3px_doors(self) -> None:
        """A 3-pixel "door" (legend artifact) MUST be rejected, not used as a ruler.

        Production observation 2026-05-21: Gulfstream has exactly 1 ``door``
        detection on sheet A-600 with width 3.17 px and bbox metadata
        ``name_source: legend``. At 200 DPI a real 36" door is ~600 px;
        a 3 px detection is a legend-symbol glyph, NOT a plan-view door.
        If the resolver used it as a ruler, derived px/ft would be 1.06
        (3.17 / 3) — implying 1 inch = 1 foot, off by 1000x.
        """
        resolver = ScaleResolver()
        # 5 narrow legend glyphs + 0 real doors → fail closed.
        legend_glyphs = [_door_detection(3.17) for _ in range(5)]
        cand = _candidate_with_doors({"sheet-A": legend_glyphs})

        result = await resolver._method_geometric_door_reference(cand)
        assert result.resolved is False
        assert result.confidence == 0.0
        assert result.value is None
        assert result.evidence["doors_detected_total"] == 0
        assert result.evidence["doors_rejected_too_narrow"] == 5
        assert result.evidence["skip_reason"] == "only_legend_glyph_doors_below_sanity_floor"

    @pytest.mark.asyncio
    async def test_no_doors_in_project_evidence(self) -> None:
        """Zero door classifications across all sheets — diagnostic skip."""
        resolver = ScaleResolver()
        cand = _candidate_with_doors({"sheet-A": [], "sheet-B": []})
        result = await resolver._method_geometric_door_reference(cand)
        assert result.resolved is False
        assert result.evidence["skip_reason"] == "no_classified_doors_in_project"
        assert result.evidence["doors_detected_total"] == 0
        assert result.evidence["sheets_with_doors"] == 0

    @pytest.mark.asyncio
    async def test_uses_provider_yolo_query_when_provider_present(self) -> None:
        """Wave 19.1a foundation: provider.get_yolo_detections is consulted."""
        resolver = ScaleResolver()
        provider = MagicMock()
        # Return 5 real doors via provider, NOT via symbols_by_sheet.
        provider.get_yolo_detections = AsyncMock(
            return_value=[_door_detection(600.0) for _ in range(5)]
        )

        # symbols_by_sheet is intentionally empty — proves provider path
        # is being consulted, not the fallback.
        sheets = [{"id": "sheet-A", "discipline": "A", "ocr_text": ""}]
        cand = MissingInputCandidate(
            missing_input_id="x",
            project_id="p", suite_id="s",
            office_id=None,
            description="scale missing",
            suggested_resolution=None,
            category=MissingInputCategory.SCALE,
            sheets=sheets,
            symbols_by_sheet={},  # empty — would yield 0 doors via fallback
            ocr_by_sheet={},
            data_provider=provider,
        )

        result = await resolver._method_geometric_door_reference(cand)

        provider.get_yolo_detections.assert_awaited_once_with("sheet-A")
        assert result.resolved is True
        assert result.evidence["doors_detected_total"] == 5

    @pytest.mark.asyncio
    async def test_falls_back_to_symbols_by_sheet_when_no_provider(self) -> None:
        """Pre-19.1 fixtures (data_provider=None) still work via symbols_by_sheet."""
        resolver = ScaleResolver()
        cand = _candidate_with_doors(
            {"sheet-A": [_door_detection(600.0) for _ in range(5)]},
            provider=None,
        )
        result = await resolver._method_geometric_door_reference(cand)
        assert result.resolved is True
        assert result.evidence["doors_detected_total"] == 5

    @pytest.mark.asyncio
    async def test_ignores_non_door_detections(self) -> None:
        """Only ``class == 'door'`` is considered. Windows / outlets are ignored."""
        resolver = ScaleResolver()
        mixed = [
            _door_detection(600.0, cls="window"),
            _door_detection(600.0, cls="electrical_outlet"),
            _door_detection(600.0, cls="exit_sign"),
            _door_detection(600.0),  # the only door
            _door_detection(600.0),  # second door
        ]
        cand = _candidate_with_doors({"sheet-A": mixed})
        result = await resolver._method_geometric_door_reference(cand)
        assert result.evidence["doors_detected_total"] == 2


class TestMultiMethodConsensus:
    """Two-method agreement on px/ft promotes 0.75+0.75 to a 0.85 resolve."""

    @pytest.mark.asyncio
    async def test_consensus_when_geometric_and_calibrator_agree(self) -> None:
        """Both methods return 200 px/ft @ 0.75 → consensus result at 0.85."""
        from aspire_orchestrator.services.blueprint.schemas_detection import ScaleCalibration

        resolver = ScaleResolver()
        # 3 doors → geometric returns confidence 0.75, value=200 px/ft.
        cand = _candidate_with_doors(
            {"sheet-A": [_door_detection(600.0) for _ in range(3)]}
        )
        # calibrator returns confidence 0.75, scale_factor=0.06 in/px →
        # px/ft = 12 / 0.06 = 200. Same number → consensus fires.
        mock_calibration = ScaleCalibration(
            scale_factor=0.06,
            confidence=0.75,
            method="graphic_bar",
            units="inch",
        )

        with patch(
            "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
            return_value=mock_calibration,
        ):
            results = await resolver.resolve(cand)

        consensus = [r for r in results if r.method == "multi_method_consensus"]
        assert len(consensus) == 1, (
            "consensus result must be appended when geo + calibrator agree"
        )
        c = consensus[0]
        assert c.resolved is True
        assert c.confidence == pytest.approx(0.85)
        assert c.value == pytest.approx(200.0)
        assert c.evidence["geometric_pxft"] == pytest.approx(200.0)
        assert c.evidence["calibrator_pxft"] == pytest.approx(200.0)
        assert c.evidence["agreement_pct"] == pytest.approx(100.0)
        assert c.evidence["source_methods"] == "geometric_door_reference,scale_calibrator"

    @pytest.mark.asyncio
    async def test_no_consensus_when_methods_disagree_by_more_than_5pct(self) -> None:
        """Geo=200 px/ft, calibrator=240 px/ft → 20% disagreement → no consensus."""
        from aspire_orchestrator.services.blueprint.schemas_detection import ScaleCalibration

        resolver = ScaleResolver()
        cand = _candidate_with_doors(
            {"sheet-A": [_door_detection(600.0) for _ in range(3)]}
        )
        # scale_factor=0.05 → 12/0.05 = 240 px/ft. Geo derives 200. 20% off.
        mock_calibration = ScaleCalibration(
            scale_factor=0.05, confidence=0.75, method="graphic_bar", units="inch",
        )

        with patch(
            "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
            return_value=mock_calibration,
        ):
            results = await resolver.resolve(cand)

        consensus = [r for r in results if r.method == "multi_method_consensus"]
        assert consensus == [], (
            "consensus MUST NOT fire when methods disagree by more than 5% — "
            "this is the no-false-positives guarantee."
        )

    @pytest.mark.asyncio
    async def test_no_consensus_when_only_one_method_qualifies(self) -> None:
        """Calibrator returns 0 confidence — consensus requires both ≥ 0.75."""
        from aspire_orchestrator.services.blueprint.schemas_detection import ScaleCalibration

        resolver = ScaleResolver()
        cand = _candidate_with_doors(
            {"sheet-A": [_door_detection(600.0) for _ in range(3)]}
        )
        mock_calibration = ScaleCalibration(
            scale_factor=0.0, confidence=0.0, method="none", units="inch",
        )

        with patch(
            "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
            return_value=mock_calibration,
        ):
            results = await resolver.resolve(cand)

        consensus = [r for r in results if r.method == "multi_method_consensus"]
        assert consensus == []

    @pytest.mark.asyncio
    async def test_consensus_within_5pct_tolerance_succeeds(self) -> None:
        """Geo=200, calibrator=204 (2% off) → within tolerance → consensus fires."""
        from aspire_orchestrator.services.blueprint.schemas_detection import ScaleCalibration

        resolver = ScaleResolver()
        cand = _candidate_with_doors(
            {"sheet-A": [_door_detection(600.0) for _ in range(3)]}
        )
        # 12 / 0.0588... ≈ 204 px/ft
        mock_calibration = ScaleCalibration(
            scale_factor=12.0 / 204.0,
            confidence=0.75, method="graphic_bar", units="inch",
        )

        with patch(
            "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
            return_value=mock_calibration,
        ):
            results = await resolver.resolve(cand)

        consensus = [r for r in results if r.method == "multi_method_consensus"]
        assert len(consensus) == 1
        # Synthesized value is the mean.
        assert consensus[0].value == pytest.approx(202.0, rel=0.01)


class TestScaleResolverProviderWiring:
    """Wave 19.1f — scale_calibrator now consumes provider PNGs."""

    @pytest.mark.asyncio
    async def test_png_pulled_from_provider_when_present(self) -> None:
        from aspire_orchestrator.services.blueprint.schemas_detection import ScaleCalibration

        resolver = ScaleResolver()
        provider = MagicMock()
        provider.get_sheet_png = AsyncMock(return_value=b"\x89PNG_PROVIDER")
        provider.get_yolo_detections = AsyncMock(return_value=[])

        cand = MissingInputCandidate(
            missing_input_id="x",
            project_id="p", suite_id="s",
            office_id=None,
            description="scale missing",
            suggested_resolution=None,
            category=MissingInputCategory.SCALE,
            sheets=[{"id": "sheet-A", "discipline": "A", "ocr_text": "SCALE: 1/4\" = 1'-0\""}],
            symbols_by_sheet={},
            ocr_by_sheet={"sheet-A": "SCALE: 1/4\" = 1'-0\""},
            data_provider=provider,
        )

        seen_png_bytes: list[bytes] = []

        def _capturing_calibrate(*, sheet_image_bytes: bytes, sheet_text: str) -> Any:
            seen_png_bytes.append(sheet_image_bytes)
            return ScaleCalibration(
                scale_factor=0.0, confidence=0.0, method="none", units="inch",
            )

        with patch(
            "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
            side_effect=_capturing_calibrate,
        ):
            result = await resolver._method_scale_calibrator(cand)

        provider.get_sheet_png.assert_awaited_once_with("sheet-A")
        assert seen_png_bytes == [b"\x89PNG_PROVIDER"]
        assert result.evidence["png_source"] == "provider"

    @pytest.mark.asyncio
    async def test_falls_back_to_legacy_image_bytes_when_provider_returns_none(self) -> None:
        from aspire_orchestrator.services.blueprint.schemas_detection import ScaleCalibration

        resolver = ScaleResolver()
        provider = MagicMock()
        provider.get_sheet_png = AsyncMock(return_value=None)
        provider.get_yolo_detections = AsyncMock(return_value=[])

        cand = MissingInputCandidate(
            missing_input_id="x",
            project_id="p", suite_id="s",
            office_id=None,
            description="scale missing",
            suggested_resolution=None,
            category=MissingInputCategory.SCALE,
            sheets=[{
                "id": "sheet-A", "discipline": "A",
                "ocr_text": "SCALE: 1/4\" = 1'-0\"",
                "image_bytes": b"LEGACY_BYTES",
            }],
            symbols_by_sheet={},
            ocr_by_sheet={"sheet-A": "SCALE: 1/4\" = 1'-0\""},
            data_provider=provider,
        )

        seen_png_bytes: list[bytes] = []

        def _capture(*, sheet_image_bytes: bytes, sheet_text: str) -> Any:
            seen_png_bytes.append(sheet_image_bytes)
            return ScaleCalibration(
                scale_factor=0.0, units="inch", method="none", confidence=0.0,
            )

        with patch(
            "aspire_orchestrator.services.blueprint.scale_calibrator.calibrate_scale",
            side_effect=_capture,
        ):
            result = await resolver._method_scale_calibrator(cand)

        assert seen_png_bytes == [b"LEGACY_BYTES"]
        assert result.evidence["png_source"] == "legacy"

    @pytest.mark.asyncio
    async def test_evidence_keys_on_receipt_allowlist(self) -> None:
        """All new scale_resolver evidence keys survive the cascade allow-list."""
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            _safe_evidence_for_receipt,
        )
        ev = {
            "sheets_with_png": 3,
            "bars_detected": 2,
            "doors_detected_total": 5,
            "doors_per_sheet_median": 600.0,
            "pixels_per_foot_derived": 200.0,
            "sheets_with_doors": 2,
            "doors_rejected_too_narrow": 1,
            "consensus_pxft": 200.0,
            "geometric_pxft": 200.0,
            "calibrator_pxft": 200.0,
            "agreement_pct": 100.0,
            "tolerance_pct": 5.0,
            "png_source": "provider",
            "derivation_method": "door_36in",
            "source_methods": "geometric_door_reference,scale_calibrator",
        }
        safe = _safe_evidence_for_receipt(ev)
        for k in ev:
            assert k in safe, f"scale_resolver evidence key {k!r} dropped by allow-list"
