"""Wave 20.14.5L L-5 — Spatial index for locality-bounded generators.

Addresses reviewer watchpoint #1 (the O(N²) trap). Generators that
operate on geometry build a SpatialIndex once per (project, sheet) and
do O(log n) radius queries instead of O(n) brute-force scans.

The index transparently falls back to brute force if scipy is missing
so unit tests run without optional native deps. Production hits the
KDTree path.

Also exposes `normalize_radius_for_sheet` — reviewer watchpoint #2 (DPI/
scale normalization). For L-5 first cut this is a passthrough; Phase 4
will inject per-sheet scale factor from blueprint_sheets metadata.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Optional

try:                                                # pragma: no cover
    from scipy.spatial import cKDTree as _KDTree   # type: ignore[import]
    _HAS_SCIPY = True
except ImportError:                                 # pragma: no cover
    _KDTree = None
    _HAS_SCIPY = False


@dataclass
class IndexedEntity:
    """Entity attached to a (x, y) point in the index."""
    entity: Any
    x: float
    y: float

    @property
    def point(self) -> tuple[float, float]:
        return self.x, self.y


class SpatialIndex:
    """KDTree-backed neighbor lookup. O(log n) per query.

    Build cost is O(n log n). For a 30K-primitive sheet that's ~ms.
    Subsequent queries are sub-millisecond. With brute-force fallback
    you get O(n) per query — fine for test fixtures, painful in prod.

    Reviewer-driven invariants:
      - results sorted deterministically (by distance, then x, then y)
      - radius is in page coordinates; caller normalizes via
        normalize_radius_for_sheet if cross-sheet comparisons are needed
      - max_results enforced even on the brute-force path
    """

    def __init__(
        self,
        items: list[Any],
        *,
        point_of: Callable[[Any], tuple[float, float]],
    ) -> None:
        self._items = items
        self._points: list[tuple[float, float]] = [point_of(it) for it in items]
        self._tree: Optional[Any] = None
        if _HAS_SCIPY and self._points:
            self._tree = _KDTree(self._points)

    def __len__(self) -> int:
        return len(self._items)

    def query_within(
        self,
        center: tuple[float, float],
        *,
        radius: float,
        max_results: int,
    ) -> list[tuple[float, Any]]:
        """Return [(distance, entity), ...] sorted by distance asc.

        Results capped at max_results (trimmed deterministically by
        (distance, x, y)).
        """
        if radius <= 0:
            raise ValueError("radius must be > 0")
        if max_results <= 0:
            raise ValueError("max_results must be > 0")

        if not self._items:
            return []

        scored: list[tuple[float, float, float, Any]] = []

        if self._tree is not None:
            # KDTree path
            idxs = self._tree.query_ball_point(center, r=radius)
            for i in idxs:
                pt = self._points[i]
                d = math.hypot(pt[0] - center[0], pt[1] - center[1])
                if d <= radius:
                    scored.append((d, pt[0], pt[1], self._items[i]))
        else:
            # Brute-force fallback
            for i, pt in enumerate(self._points):
                d = math.hypot(pt[0] - center[0], pt[1] - center[1])
                if d <= radius:
                    scored.append((d, pt[0], pt[1], self._items[i]))

        # Deterministic sort
        scored.sort(key=lambda s: (s[0], s[1], s[2]))
        return [(d, ent) for d, _, _, ent in scored[:max_results]]


# ============================================================
# DPI / scale normalization (watchpoint #2)
# ============================================================


def normalize_radius_for_sheet(
    base_radius_px: float,
    *,
    sheet_scale_factor: float = 1.0,
) -> float:
    """Scale a base radius to a specific sheet's coordinate system.

    L-5 first cut: passthrough. Phase 4 will read the sheet's actual
    scale (e.g. 1/4"=1' → factor 48) from blueprint_sheets and inject
    it here so the same semantic radius means the same thing across
    sheets at different zoom levels.

    For now the contract is: callers may pass sheet_scale_factor=1.0
    and get the literal radius back. Future integration of true scale
    extraction will not change the call site.
    """
    if sheet_scale_factor <= 0:
        raise ValueError(f"sheet_scale_factor must be > 0, got {sheet_scale_factor}")
    return base_radius_px * sheet_scale_factor


def bbox_center(bbox: list[float]) -> tuple[float, float]:
    """Center of an axis-aligned bbox [x0, y0, x1, y1]."""
    if len(bbox) < 4:
        return (0.0, 0.0)
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def polygon_to_bbox(coords: list[float]) -> list[float]:
    """Convert an N-point polygon (flat [x0,y0,x1,y1,...]) to axis-aligned bbox.

    OCR spans from blueprint_sheet_intelligence are stored as 4-point
    polygons (8 coords). Normalize to [min_x, min_y, max_x, max_y].
    """
    if len(coords) < 4:
        return [0.0, 0.0, 0.0, 0.0]
    xs = coords[0::2]
    ys = coords[1::2]
    return [min(xs), min(ys), max(xs), max(ys)]


def alignment_factor(
    src_bbox: list[float],
    dst_bbox: list[float],
    *,
    tolerance: float = 8.0,
) -> tuple[str, float]:
    """Classify alignment between two bboxes; return ('axis', factor in [0,1]).

    'horizontal' = same y-band → highest factor
    'vertical' = same x-band
    'diagonal' = neither aligned
    """
    src_cx, src_cy = bbox_center(src_bbox)
    dst_cx, dst_cy = bbox_center(dst_bbox)
    dy = abs(src_cy - dst_cy)
    dx = abs(src_cx - dst_cx)
    if dy <= tolerance and dx > tolerance:
        return ("horizontal", 1.0)
    if dx <= tolerance and dy > tolerance:
        return ("vertical", 0.85)
    if dx <= tolerance and dy <= tolerance:
        # Centers nearly coincident — treat as horizontal (label-on-symbol)
        return ("horizontal", 1.0)
    return ("diagonal", 0.5)


def bbox_overlap_ratio(inner: list[float], outer: list[float]) -> float:
    """Fraction of `inner` that lies within `outer`. Range [0, 1]."""
    if len(inner) < 4 or len(outer) < 4:
        return 0.0
    ix0, iy0, ix1, iy1 = inner[:4]
    ox0, oy0, ox1, oy1 = outer[:4]
    dx = max(0.0, min(ix1, ox1) - max(ix0, ox0))
    dy = max(0.0, min(iy1, oy1) - max(iy0, oy0))
    overlap = dx * dy
    inner_area = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inner_area <= 0:
        return 0.0
    return min(1.0, overlap / inner_area)
