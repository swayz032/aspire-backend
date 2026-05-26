"""N-3.2 — SheetCoordinateTransform: raster <-> PDF point-space mapping.

The coordinate bridge. Drew's data has two coordinate spaces:

  PDF point space (top-left origin, used by blueprint_primitives,
                   blueprint_composed_symbols.bbox, PyMuPDF output)
    24" x 36"  page at 72 DPI -> 1728 x 2592 points

  Azure raster pixel space (top-left origin, used by
                            blueprint_project_legends.representation_bbox,
                            blueprint_sheet_intelligence.regions[*].bbox)
    Same physical page at ~200 DPI -> ~7200 x 4800 pixels

Both spaces share top-left origin (no Y-axis inversion needed).
What can differ per sheet:
  - DPI / scale ratio
  - Page orientation (portrait vs landscape)
  - Page rotation (0 / 90 / 180 / 270)

This module is the single source of truth for raster<->PDF math.

Beyond Phase 5
--------------
This same matrix is the core IP for any raster<->vector feature:
  - Overlay rendering on the sheet canvas
  - Click-through coordinates from raster pixel to PDF point
  - OCR span -> primitive joins (Wave 17b.2)
  - Per-symbol bbox visualization in Takeoff
  - Future annotation tooling

Design choice (reviewer-locked 2026-05-26): persist transform metadata
explicitly. Empirical scale discovery is rejected as fragile — coverage-
maximization can converge on plausible-but-wrong transforms.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


VALID_ROTATIONS: frozenset[int] = frozenset({0, 90, 180, 270})


@dataclass(frozen=True)
class SheetCoordinateTransform:
    """The per-sheet coordinate bridge.

    Fields:
      raster_w, raster_h : page dimensions in Azure raster pixel space
      pdf_w, pdf_h       : page dimensions in PDF point space
      rotation           : 0 / 90 / 180 / 270 — degrees the raster
                            is rotated relative to PDF

    Both coordinate systems are top-left origin (no Y inversion).
    """
    raster_w: float
    raster_h: float
    pdf_w: float
    pdf_h: float
    rotation: int = 0

    def __post_init__(self):
        if self.rotation not in VALID_ROTATIONS:
            raise ValueError(
                f"rotation={self.rotation} not in {sorted(VALID_ROTATIONS)}"
            )
        for name, val in (
            ("raster_w", self.raster_w), ("raster_h", self.raster_h),
            ("pdf_w", self.pdf_w), ("pdf_h", self.pdf_h),
        ):
            if val <= 0:
                raise ValueError(f"{name} must be > 0 (got {val})")

    # ------------------------------------------------------------
    # Single-point transforms
    # ------------------------------------------------------------

    def _normalize_raster(self, rx: float, ry: float) -> Tuple[float, float]:
        """Convert raster pixel coords to [0, 1] x [0, 1]."""
        return rx / self.raster_w, ry / self.raster_h

    def raster_point_to_pdf(self, rx: float, ry: float) -> Tuple[float, float]:
        """Map a single (rx, ry) raster point to (px, py) PDF point."""
        nx, ny = self._normalize_raster(rx, ry)
        if self.rotation == 0:
            return nx * self.pdf_w, ny * self.pdf_h
        elif self.rotation == 90:
            # Raster top-left → PDF top-right (rotating raster CW by 90
            # to overlay PDF). x_pdf = (1 - ny) * pdf_w, y_pdf = nx * pdf_h
            return (1.0 - ny) * self.pdf_w, nx * self.pdf_h
        elif self.rotation == 180:
            return (1.0 - nx) * self.pdf_w, (1.0 - ny) * self.pdf_h
        elif self.rotation == 270:
            return ny * self.pdf_w, (1.0 - nx) * self.pdf_h
        else:
            raise ValueError(f"unsupported rotation {self.rotation}")

    # ------------------------------------------------------------
    # Bbox transforms — return AABB in PDF space
    # ------------------------------------------------------------

    def raster_bbox_to_pdf_aabb(
        self, raster_bbox: Tuple[float, float, float, float],
    ) -> Tuple[float, float, float, float]:
        """Map a raster axis-aligned bbox (rx_min, ry_min, rx_max, ry_max)
        to a PDF axis-aligned bbox.

        After rotation the input AABB may no longer be axis-aligned in PDF
        space, but for histogram bbox-containment we only need an AABB
        that covers the rotated quad — we return that bounding rectangle.
        """
        rx_min, ry_min, rx_max, ry_max = raster_bbox
        # Transform all four raster corners
        corners = [
            self.raster_point_to_pdf(rx_min, ry_min),
            self.raster_point_to_pdf(rx_max, ry_min),
            self.raster_point_to_pdf(rx_max, ry_max),
            self.raster_point_to_pdf(rx_min, ry_max),
        ]
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        return min(xs), min(ys), max(xs), max(ys)

    def raster_polygon_to_pdf_aabb(
        self, polygon_8: Tuple[float, ...],
    ) -> Tuple[float, float, float, float]:
        """Map an 8-coord raster polygon [x0,y0,x1,y1,x2,y2,x3,y3]
        to a PDF axis-aligned bbox.

        Azure layout output stores bboxes as 4-corner polygons.
        """
        if len(polygon_8) != 8:
            raise ValueError(f"polygon_8 must have 8 coords (got {len(polygon_8)})")
        pdf_corners = [
            self.raster_point_to_pdf(polygon_8[i], polygon_8[i + 1])
            for i in range(0, 8, 2)
        ]
        xs = [c[0] for c in pdf_corners]
        ys = [c[1] for c in pdf_corners]
        return min(xs), min(ys), max(xs), max(ys)
