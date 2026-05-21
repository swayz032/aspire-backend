"""Scale resolver — Wave 19b.

Multi-method scale resolution for sheets that failed calibration.

Methods (tried in parallel):
  1. scale_bar_ocr   — existing scale_calibrator.calibrate_scale() (bar + text)
  2. dimension_callout_regression — derive scale from annotated dimension
                                     callouts in OCR text (e.g., "10'-0"" line = X px)
  3. reference_object — known real-world sizes: door=36" wide, parking=9',
                        standard interior wall=4", exterior wall=6".

Confidence-weighted vote: highest confidence wins if >= 0.85.

Law compliance:
  Law #3 — Never raises; low confidence is the signal.
  Law #9 — No raw OCR logged; only matched pattern strings and pixel counts.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCandidate,
    MissingInputCategory,
    MissingInputResolver,
    ResolverResult,
)

_log = logging.getLogger(__name__)
_TIMEOUT_S: float = 15.0

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
        tasks = [
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
        return results

    async def _method_scale_calibrator(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Run scale_calibrator on stored sheet image bytes."""
        try:
            from aspire_orchestrator.services.blueprint.scale_calibrator import calibrate_scale

            best_conf = 0.0
            best_scale: float | None = None
            best_method = "scale_calibrator"
            sheets_tried = 0

            for sheet in candidate.sheets:
                image_bytes = sheet.get("image_bytes")
                ocr_text = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )
                if not image_bytes and not ocr_text:
                    continue

                result = calibrate_scale(
                    sheet_image_bytes=image_bytes or b"",
                    sheet_text=ocr_text,
                )
                sheets_tried += 1
                if result.confidence > best_conf:
                    best_conf = result.confidence
                    best_scale = result.scale_factor
                    best_method = result.method

            resolved = best_conf >= 0.85 and best_scale is not None
            return ResolverResult(
                resolved=resolved,
                value=best_scale if resolved else None,
                confidence=best_conf,
                method=f"scale_calibrator:{best_method}",
                evidence={"sheets_tried": sheets_tried, "best_confidence": best_conf},
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("scale_calibrator", exc)

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
