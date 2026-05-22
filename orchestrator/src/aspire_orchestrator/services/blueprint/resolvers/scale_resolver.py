"""Scale resolver — Wave 19b (text-only), 19.1f (provider + CAD-door).

Multi-method scale resolution for sheets that failed calibration.

Methods (tried in parallel):
  1. geometric_door_reference — Wave 19.1f. Uses Wave 11g-v2 detected door
                                  symbols as a known-dimension ruler.
                                  Median door pixel-width / 36" = px/ft.
                                  Cheap (SQL only, no PNG, no LLM tokens).
  2. scale_calibrator — Wave 19.1f provider-wired. Graphic-bar detection
                         on PNG bytes from Supabase Storage, plus text-only
                         scale-mark OCR. PNGs fetched concurrently via
                         ``asyncio.gather``.
  3. dimension_callout_regression — derive scale from annotated dimension
                                     callouts in OCR text (e.g., "10'-0"" line = X px).
  4. reference_object — known real-world sizes: door=36" wide, parking=9',
                        standard interior wall=4", exterior wall=6".
                        Text-only — does NOT compute pixels.

Confidence-weighted vote (Commit 3c — preview of Wave 6 multi-method vote):
  * Any single method @ ≥ 0.85 → resolves.
  * Geometric + scale_calibrator both at 0.75 AND derived px/ft within ±5%
    → synthetic resolve at 0.85 confidence. Two independent geometric
    signals agreeing is more reliable than one method at 0.85. No false
    positives — disagreement leaves the row open.

Law compliance:
  Law #3 — Never raises; low confidence is the signal. Disagreement does
           NOT synthesize a result.
  Law #9 — No raw OCR logged; only matched pattern strings and pixel counts.
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
from typing import Any

from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCandidate,
    MissingInputCategory,
    MissingInputResolver,
    ResolverResult,
)

_log = logging.getLogger(__name__)

# Wave 19.1f — per-method timeout. Graphic-bar OCR + provider PNG fetch +
# geometric door lookup are all fast (≤ 5-10s each). 45s is generous;
# stays well under the cascade's outer 90s cap (missing_input_cascade.py:43).
_TIMEOUT_S: float = 45.0

# Geometric-door method constants
_RENDER_DPI: float = 200.0
_STANDARD_DOOR_WIDTH_IN: float = 36.0  # standard interior swing door
_STANDARD_DOOR_WIDTH_FT: float = _STANDARD_DOOR_WIDTH_IN / 12.0

# Sanity floor: at 200 DPI a real 36" door is ~600 pixels wide. Anything
# narrower than 50 px is almost certainly a legend-symbol primitive
# (entropy/signature_hash metadata in bbox) or a misclassified noise
# detection — NOT a plan-view door. Reject these to prevent the resolver
# from deriving scale from a 3-pixel "door" (observed on Gulfstream).
_MIN_PLAN_DOOR_WIDTH_PX: float = 50.0

# Multi-method consensus: agreement within ±5% on derived px/ft promotes
# two 0.75 signals to a synthetic 0.85 resolution.
_CONSENSUS_TOLERANCE_PCT: float = 0.05
_CONSENSUS_CONFIDENCE: float = 0.85
_LOW_CONFIDENCE_FLOOR: float = 0.75

# Known real-world reference object sizes in inches
_REFERENCE_OBJECTS: list[tuple[str, float, re.Pattern[str]]] = [
    ("door_width", 36.0, re.compile(r"\bdoor\b.*?(\d+)\s*[\"'x×]", re.IGNORECASE)),
    ("parking_width", 108.0, re.compile(r"\bparking\s+stall\b.*?(\d+)\s*[\"']?", re.IGNORECASE)),
    ("interior_wall", 4.0, re.compile(r"\bint(?:erior)?\s+wall\b.*?(\d+)\s*[\"']?\s*thick", re.IGNORECASE)),
    ("exterior_wall", 6.0, re.compile(r"\bext(?:erior)?\s+wall\b.*?(\d+)\s*[\"']?\s*thick", re.IGNORECASE)),
]

_RENDER_DPI: float = 200.0

# Dimension callout: 10'-6" or 10.5' or 2600mm
_DIM_CALLOUT_FT_IN = re.compile(
    r'(\d{1,3})\s*[\'`]\s*(?:-?\s*(\d{1,2})\s*")?',
    re.IGNORECASE,
)
_DIM_CALLOUT_MM = re.compile(r"(\d{3,5})\s*mm", re.IGNORECASE)


def _to_inches(feet: int, inches: int) -> float:
    return feet * 12.0 + inches


class ScaleResolver(MissingInputResolver):
    """Resolves scale-calibration missing_inputs."""

    category = MissingInputCategory.SCALE

    async def resolve(self, candidate: MissingInputCandidate) -> list[ResolverResult]:
        # Wave 19.1f — geometric_door_reference runs FIRST in the dispatch
        # list. It is cheaper than scale_calibrator (SQL-only — no PNG
        # download, no OCR, no LLM tokens) so if doors are abundant on
        # this project the cascade resolves without fetching any PNGs.
        # All methods still run concurrently via asyncio.as_completed;
        # ordering only matters for the consensus pass below.
        tasks = [
            asyncio.create_task(self._method_geometric_door_reference(candidate)),
            asyncio.create_task(self._method_scale_calibrator(candidate)),
            asyncio.create_task(self._method_dimension_regression(candidate)),
            asyncio.create_task(self._method_reference_object(candidate)),
        ]
        results: list[ResolverResult] = []
        for coro in asyncio.as_completed(tasks):
            try:
                result = await asyncio.wait_for(coro, timeout=_TIMEOUT_S)
                results.append(result)
            except asyncio.TimeoutError:
                results.append(ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="timeout", evidence={"timeout_s": _TIMEOUT_S},
                    category=self.category,
                ))
            except Exception as exc:  # noqa: BLE001
                results.append(self._safe_result("scale_resolve", exc))

        # Wave 19.1f (Commit 3c) — multi-method consensus pass.
        # If geometric_door_reference AND scale_calibrator both returned a
        # numeric scale at ≥ 0.75 confidence AND their derived pixels-per-foot
        # agree within ±5%, promote to a synthetic resolved result at 0.85.
        # Two independent geometric/visual signals agreeing is a stronger
        # signal than either alone. Strict guard: disagreement does NOT
        # produce a result — under the "no false positives" constraint we
        # would rather leave the row open than guess.
        consensus = self._build_consensus_result(results)
        if consensus is not None:
            results.append(consensus)

        return results

    def _build_consensus_result(
        self, results: list[ResolverResult]
    ) -> ResolverResult | None:
        """If geometric + calibrator agree on px/ft, emit a 0.85 consensus result.

        Returns ``None`` (no consensus result appended) when:
          - either method is missing or below ``_LOW_CONFIDENCE_FLOOR``,
          - either method has a non-numeric value,
          - derived px/ft disagree by more than ``_CONSENSUS_TOLERANCE_PCT``.

        The two source results remain in the list either way — the cascade's
        ``best_result`` selector picks whichever has highest confidence.
        """
        geo: ResolverResult | None = None
        cal: ResolverResult | None = None
        for r in results:
            if r.method == "geometric_door_reference" and r.confidence >= _LOW_CONFIDENCE_FLOOR:
                geo = r
            elif r.method.startswith("scale_calibrator") and r.confidence >= _LOW_CONFIDENCE_FLOOR:
                cal = r

        if geo is None or cal is None:
            return None

        # Both ``value`` fields are the derived pixels-per-foot OR None.
        # If either is None, we cannot numerically compare.
        try:
            geo_pxft = float(geo.value) if geo.value is not None else None
            cal_pxft = float(cal.value) if cal.value is not None else None
        except (TypeError, ValueError):
            return None

        if geo_pxft is None or cal_pxft is None:
            return None
        if geo_pxft <= 0 or cal_pxft <= 0:
            return None

        # Compare against the smaller value to keep the tolerance check
        # symmetric (5% of the smaller side, not biased toward either).
        diff_pct = abs(geo_pxft - cal_pxft) / min(geo_pxft, cal_pxft)
        if diff_pct > _CONSENSUS_TOLERANCE_PCT:
            return None

        # Agreement. Synthesize a resolved result. Use the mean of the
        # two derived scales as the canonical value.
        consensus_pxft = (geo_pxft + cal_pxft) / 2.0
        return ResolverResult(
            resolved=True,
            value=consensus_pxft,
            confidence=_CONSENSUS_CONFIDENCE,
            method="multi_method_consensus",
            evidence={
                "consensus_pxft": round(consensus_pxft, 3),
                "geometric_pxft": round(geo_pxft, 3),
                "calibrator_pxft": round(cal_pxft, 3),
                "agreement_pct": round(100.0 * (1.0 - diff_pct), 2),
                "tolerance_pct": round(100.0 * _CONSENSUS_TOLERANCE_PCT, 2),
                "source_methods": "geometric_door_reference,scale_calibrator",
            },
            category=self.category,
        )

    async def _method_scale_calibrator(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Run scale_calibrator on PNG bytes pulled from the SheetDataProvider.

        Wave 19.1f changes:
        * PNG source: ``candidate.data_provider.get_sheet_png(sheet_id)``
          (Supabase Storage) with legacy ``sheet["image_bytes"]`` fallback
          for pre-19.1 unit-test fixtures.
        * PNG fetches dispatched concurrently via ``asyncio.gather`` —
          previously sequential, which compounded on multi-sheet projects.
        * Evidence keys added: ``sheets_with_png``, ``png_source``,
          ``bars_detected`` (count of sheets whose result method was
          ``graphic_bar`` / produced a calibration).
        * Value contract change: when calibrator returns
          ``scale_factor > 0``, expose it as **pixels per foot** so the
          consensus pass (and downstream Wave 19.2 vision pass) can
          numerically compare against ``geometric_door_reference``.
          ``ScaleCalibration.scale_factor`` is ``inches per pixel`` at
          200 DPI → ``px/ft = 12 / scale_factor``.
        """
        try:
            from aspire_orchestrator.services.blueprint.scale_calibrator import (
                calibrate_scale,
            )

            # Fetch all candidate PNGs concurrently.
            sheets_with_text_or_png = [
                s for s in candidate.sheets
                if s.get("id")
            ]
            png_results = await asyncio.gather(
                *[
                    self._resolve_png_for_sheet(s, candidate)
                    for s in sheets_with_text_or_png
                ],
                return_exceptions=False,
            )

            png_sources_seen: set[str] = {
                src for _sid, png, src in png_results if png
            }
            png_source_used: str = (
                "provider" if "provider" in png_sources_seen
                else ("legacy" if "legacy" in png_sources_seen else "none")
            )

            best_conf = 0.0
            best_scale_pxft: float | None = None
            best_method = "scale_calibrator"
            sheets_tried = 0
            sheets_with_png = 0
            bars_detected = 0

            for sheet, (sheet_id, png_bytes, _src) in zip(
                sheets_with_text_or_png, png_results
            ):
                ocr_text = (
                    candidate.ocr_by_sheet.get(sheet_id)
                    or sheet.get("ocr_text")
                    or ""
                )
                if not png_bytes and not ocr_text:
                    continue
                if png_bytes:
                    sheets_with_png += 1

                result = calibrate_scale(
                    sheet_image_bytes=png_bytes or b"",
                    sheet_text=ocr_text,
                )
                sheets_tried += 1
                # ScaleMethod literal values from scale_calibrator.py are
                # "text" | "bar" | "both" | "none". "bar" and "both" mean
                # a graphic scale bar was detected on the PNG.
                if result.method in ("bar", "both"):
                    bars_detected += 1

                if result.confidence > best_conf and result.scale_factor > 0:
                    best_conf = result.confidence
                    # Convert in/px → px/ft for cross-method comparison.
                    best_scale_pxft = 12.0 / result.scale_factor
                    best_method = result.method

            resolved = best_conf >= 0.85 and best_scale_pxft is not None
            return ResolverResult(
                resolved=resolved,
                value=best_scale_pxft if best_scale_pxft is not None else None,
                confidence=best_conf,
                method=f"scale_calibrator:{best_method}",
                evidence={
                    "sheets_tried": sheets_tried,
                    "sheets_with_png": sheets_with_png,
                    "bars_detected": bars_detected,
                    "best_confidence": round(best_conf, 3),
                    "png_source": png_source_used,
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("scale_calibrator", exc)

    async def _resolve_png_for_sheet(
        self,
        sheet: dict[str, Any],
        candidate: MissingInputCandidate,
    ) -> tuple[str, bytes | None, str]:
        """Fetch PNG bytes for a single sheet — provider → legacy fallback.

        Returns ``(sheet_id, png_bytes_or_None, source_tag)`` where
        ``source_tag`` is ``"provider"``, ``"legacy"``, or ``"none"``.
        Never raises. Same pattern as schedule_resolver._resolve_png_for_sheet.
        """
        sheet_id = str(sheet.get("id") or "")
        if not sheet_id:
            return ("", None, "none")

        provider = candidate.data_provider
        if provider is not None:
            try:
                png = await provider.get_sheet_png(sheet_id)
                if png:
                    return (sheet_id, png, "provider")
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "scale_resolver.png provider fetch failed sheet=%s err=%s",
                    sheet_id[:8], type(exc).__name__,
                )

        legacy = sheet.get("image_bytes")
        if isinstance(legacy, (bytes, bytearray)) and legacy:
            return (sheet_id, bytes(legacy), "legacy")

        return (sheet_id, None, "none")

    async def _method_geometric_door_reference(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Derive pixels-per-foot from detected door symbols (the CAD lever).

        Wave 11g-v2's symbol detection yields ``blueprint_symbols`` rows
        with ``class='door'`` and a ``bbox`` jsonb of shape
        ``{x, y, w, h}``. A standard interior swing door is 36" / 3 ft
        wide. Median door pixel-width / 3.0 = pixels per foot — without
        downloading any PNG and without calling any LLM.

        Sanity floor (``_MIN_PLAN_DOOR_WIDTH_PX = 50``):
          Legend-extracted "doors" can be 3-5 px wide
          (``name_source='legend'`` metadata in bbox). These are SYMBOL
          GLYPHS, not plan-view doors. At 200 DPI a real 36" door is
          ~600px. Any detection narrower than 50px is rejected so a
          legend artifact can never seed a false scale.

        Confidence (per the founder spec):
          * ≥ 5 plan-view doors: 0.85 (resolves standalone)
          * 2-4 doors:           0.75 (consensus-eligible)
          * 1 door:              skip — not enough samples for a median
          * 0 doors:             skip — emit diagnostic skip_reason

        Provider use: ``candidate.data_provider.get_yolo_detections(sheet_id)``.
        Falls back to ``candidate.symbols_by_sheet`` for pre-19.1 fixtures.
        """
        try:
            provider = candidate.data_provider

            # Gather doors across all sheets in the candidate.
            doors_per_sheet: dict[str, list[float]] = {}
            sheets_with_doors = 0
            doors_rejected_too_narrow = 0

            # Concurrent YOLO fetches when provider is present.
            sheet_ids = [
                str(s.get("id") or "") for s in candidate.sheets if s.get("id")
            ]

            if provider is not None and sheet_ids:
                detection_lists = await asyncio.gather(
                    *[provider.get_yolo_detections(sid) for sid in sheet_ids],
                    return_exceptions=False,
                )
                detections_by_sheet = dict(zip(sheet_ids, detection_lists))
            else:
                # Fallback to candidate.symbols_by_sheet (pre-19.1 path,
                # populated by drew_blueprint or test fixtures).
                detections_by_sheet = {
                    sid: list(candidate.symbols_by_sheet.get(sid, []))
                    for sid in sheet_ids
                }

            for sid, detections in detections_by_sheet.items():
                widths_for_sheet: list[float] = []
                for det in detections:
                    if det.get("class") != "door":
                        continue
                    bbox = det.get("bbox") or {}
                    # Bbox jsonb shape verified against production data
                    # (2026-05-21 supabase query): {x, y, w, h}.
                    try:
                        width_px = float(bbox.get("w") or 0)
                    except (TypeError, ValueError):
                        continue
                    if width_px < _MIN_PLAN_DOOR_WIDTH_PX:
                        doors_rejected_too_narrow += 1
                        continue
                    widths_for_sheet.append(width_px)
                if widths_for_sheet:
                    doors_per_sheet[sid] = widths_for_sheet
                    sheets_with_doors += 1

            all_widths: list[float] = [
                w for ws in doors_per_sheet.values() for w in ws
            ]
            doors_detected_total = len(all_widths)

            if doors_detected_total == 0:
                # Fail-closed with a specific diagnostic skip_reason.
                skip = (
                    "no_classified_doors_in_project"
                    if doors_rejected_too_narrow == 0
                    else "only_legend_glyph_doors_below_sanity_floor"
                )
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="geometric_door_reference",
                    evidence={
                        "skip_reason": skip,
                        "doors_detected_total": 0,
                        "doors_rejected_too_narrow": doors_rejected_too_narrow,
                        "sheets_with_doors": 0,
                    },
                    category=self.category,
                )

            if doors_detected_total == 1:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="geometric_door_reference",
                    evidence={
                        "skip_reason": "insufficient_doors_for_median",
                        "doors_detected_total": 1,
                        "doors_rejected_too_narrow": doors_rejected_too_narrow,
                        "sheets_with_doors": sheets_with_doors,
                    },
                    category=self.category,
                )

            median_door_width_px = statistics.median(all_widths)
            pixels_per_foot = median_door_width_px / _STANDARD_DOOR_WIDTH_FT

            if doors_detected_total >= 5:
                confidence = 0.85
            else:  # 2, 3, or 4
                confidence = _LOW_CONFIDENCE_FLOOR

            return ResolverResult(
                resolved=confidence >= 0.85,
                value=pixels_per_foot,
                confidence=confidence,
                method="geometric_door_reference",
                evidence={
                    "doors_detected_total": doors_detected_total,
                    "doors_per_sheet_median": round(median_door_width_px, 2),
                    "pixels_per_foot_derived": round(pixels_per_foot, 3),
                    "derivation_method": "door_36in",
                    "sheets_with_doors": sheets_with_doors,
                    "doors_rejected_too_narrow": doors_rejected_too_narrow,
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("geometric_door_reference", exc)

    async def _method_dimension_regression(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Infer scale from dimension callouts in OCR text.

        Looks for annotated real-world dimensions (e.g., "20'-0"") and
        attempts to correlate them with known-size features to derive
        a pixels-per-inch ratio.
        """
        try:
            all_dims_ft_in: list[tuple[int, int]] = []
            all_dims_mm: list[int] = []

            for sheet in candidate.sheets:
                ocr = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )
                for m in _DIM_CALLOUT_FT_IN.finditer(ocr[:3000]):
                    try:
                        ft = int(m.group(1))
                        inches = int(m.group(2) or "0")
                        if 1 <= ft <= 200:
                            all_dims_ft_in.append((ft, inches))
                    except (ValueError, IndexError):
                        pass
                for m in _DIM_CALLOUT_MM.finditer(ocr[:3000]):
                    try:
                        mm = int(m.group(1))
                        if 100 <= mm <= 20000:
                            all_dims_mm.append(mm)
                    except ValueError:
                        pass

            if not all_dims_ft_in and not all_dims_mm:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="dimension_regression",
                    evidence={"skip_reason": "no_dimension_callouts_found"},
                    category=self.category,
                )

            # Most common dimension value is likely a room span —
            # use the median to represent the "typical" dimension.
            dims_inches: list[float] = [
                _to_inches(ft, i) for ft, i in all_dims_ft_in
            ]
            dims_inches += [mm / 25.4 for mm in all_dims_mm]
            dims_inches.sort()
            median_in = dims_inches[len(dims_inches) // 2]

            # Confidence is moderate — we can't verify against pixel coords
            # without actual dimension line endpoints. Mark it below threshold
            # so cascade only uses this as a tiebreaker if scale_calibrator fails.
            return ResolverResult(
                resolved=False,  # Never standalone-resolved; used as supporting evidence
                value=median_in,
                confidence=0.60,
                method="dimension_regression",
                evidence={
                    "dim_count": len(dims_inches),
                    "median_dim_inches": round(median_in, 2),
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("dimension_regression", exc)

    async def _method_reference_object(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Infer scale from known-size reference objects in OCR text.

        Matches patterns like "36" wide" or "3'-0" door" against known
        real-world sizes and counts matching instances.
        """
        try:
            object_hits: dict[str, int] = {}

            for sheet in candidate.sheets:
                ocr = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )[:4000]

                for obj_name, _real_size_in, pattern in _REFERENCE_OBJECTS:
                    m = pattern.search(ocr)
                    if m:
                        object_hits[obj_name] = object_hits.get(obj_name, 0) + 1

            if not object_hits:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="reference_object",
                    evidence={"skip_reason": "no_reference_objects_found"},
                    category=self.category,
                )

            # We found reference objects but cannot derive exact scale_factor
            # without pixel-coordinate data — confidence is supporting only.
            best_object = max(object_hits, key=lambda k: object_hits[k])
            return ResolverResult(
                resolved=False,  # Supporting evidence only
                value=None,
                confidence=0.60,
                method="reference_object",
                evidence={
                    "objects_found": list(object_hits.keys()),
                    "best_hit": best_object,
                    "hit_count": object_hits[best_object],
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("reference_object", exc)
