"""Wave 20.14.5L L-3 — Locality enforcement primitives.

Reviewer requirement: hard locality caps live in the framework, not in
generators. This module provides the LocalityCaps dataclass + bbox/KDTree
utilities every generator uses.

If a generator exceeds any cap during a single invocation, the framework
emits a receipt + downgrades the offending candidate branch to
UNKNOWN_REVIEW and terminates that branch. Fail closed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class LocalityCaps:
    """Per-invocation hard limits. Subclasses can tighten, never relax.

    The framework enforces these before any expensive op runs. Subclass
    constructors that try to set values larger than the framework
    defaults are rejected at registration time.
    """
    max_radius_px: float = 800.0          # spatial search radius (page coords)
    max_neighbors: int = 64               # max candidates per src entity
    max_candidates: int = 10_000          # max stage-0 candidates per run
    max_edges_emitted: int = 100_000      # hard ceiling on edge volume per run
    max_expensive_ops: int = 1_000        # stage-4 topology calls per run

    # Defense-in-depth: framework-level absolute ceilings. Subclasses
    # cannot raise these — only lower.
    _FRAMEWORK_MAX_RADIUS_PX: float = 5_000.0
    _FRAMEWORK_MAX_NEIGHBORS: int = 256
    _FRAMEWORK_MAX_CANDIDATES: int = 200_000
    _FRAMEWORK_MAX_EDGES_EMITTED: int = 5_000_000
    _FRAMEWORK_MAX_EXPENSIVE_OPS: int = 50_000

    def __post_init__(self):
        if self.max_radius_px <= 0 or self.max_radius_px > self._FRAMEWORK_MAX_RADIUS_PX:
            raise ValueError(
                f"max_radius_px {self.max_radius_px} out of bounds "
                f"(0, {self._FRAMEWORK_MAX_RADIUS_PX}]"
            )
        if self.max_neighbors <= 0 or self.max_neighbors > self._FRAMEWORK_MAX_NEIGHBORS:
            raise ValueError(f"max_neighbors {self.max_neighbors} out of bounds")
        if self.max_candidates <= 0 or self.max_candidates > self._FRAMEWORK_MAX_CANDIDATES:
            raise ValueError(f"max_candidates {self.max_candidates} out of bounds")
        if self.max_edges_emitted <= 0 or self.max_edges_emitted > self._FRAMEWORK_MAX_EDGES_EMITTED:
            raise ValueError(f"max_edges_emitted {self.max_edges_emitted} out of bounds")
        if self.max_expensive_ops <= 0 or self.max_expensive_ops > self._FRAMEWORK_MAX_EXPENSIVE_OPS:
            raise ValueError(f"max_expensive_ops {self.max_expensive_ops} out of bounds")


@dataclass(frozen=True)
class BBox:
    """Axis-aligned bbox in page coords (points or pixels, caller-defined)."""
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def distance_to(self, other: "BBox") -> float:
        """Center-to-center Euclidean distance."""
        return math.hypot(self.cx - other.cx, self.cy - other.cy)

    def overlap_area(self, other: "BBox") -> float:
        """Intersection area; 0 if disjoint."""
        dx = max(0.0, min(self.x1, other.x1) - max(self.x0, other.x0))
        dy = max(0.0, min(self.y1, other.y1) - max(self.y0, other.y0))
        return dx * dy

    def overlap_ratio(self, other: "BBox") -> float:
        """Overlap as fraction of self.area."""
        if self.area == 0:
            return 0.0
        return self.overlap_area(other) / self.area

    def contains(self, other: "BBox") -> bool:
        return (
            other.x0 >= self.x0
            and other.y0 >= self.y0
            and other.x1 <= self.x1
            and other.y1 <= self.y1
        )

    @classmethod
    def from_list(cls, coords: list[float] | tuple[float, ...]) -> "BBox":
        if len(coords) != 4:
            raise ValueError(f"BBox expects 4 coords, got {len(coords)}")
        return cls(float(coords[0]), float(coords[1]), float(coords[2]), float(coords[3]))


@dataclass
class LocalityNeighborhood:
    """A bounded set of neighbors around a query point.

    Returned by query_neighbors. Already capped at max_neighbors.
    Tracks whether trimming occurred — generators must check this and
    emit a locality_violations counter increment if so.
    """
    query_center: tuple[float, float]
    neighbors: list  # subclass-defined neighbor objects
    trimmed: bool = False
    candidates_before_trim: int = 0


def euclidean_distance(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


def query_neighbors_within(
    *,
    query_point: tuple[float, float],
    candidates: list,
    point_of: callable,                     # candidate -> (x, y)
    max_radius_px: float,
    max_neighbors: int,
) -> LocalityNeighborhood:
    """Brute-force-bounded neighbor query.

    For Phase 3 first cut we use a flat O(n) scan capped by max_radius
    + max_neighbors. If candidate set ever exceeds ~10k we'll swap in a
    KDTree (scipy.spatial.cKDTree). For now correctness > optimization.

    Reviewer requirement: deterministic ordering. Results are sorted by
    distance then by (x, y) for tie-break.
    """
    if max_radius_px <= 0:
        raise ValueError("max_radius_px must be > 0")
    if max_neighbors <= 0:
        raise ValueError("max_neighbors must be > 0")

    scored: list[tuple[float, float, float, object]] = []
    for c in candidates:
        pt = point_of(c)
        d = euclidean_distance(query_point, pt)
        if d <= max_radius_px:
            scored.append((d, pt[0], pt[1], c))

    # Deterministic sort: distance, then x, then y
    scored.sort(key=lambda s: (s[0], s[1], s[2]))

    pre_trim = len(scored)
    trimmed = pre_trim > max_neighbors
    return LocalityNeighborhood(
        query_center=query_point,
        neighbors=[s[3] for s in scored[:max_neighbors]],
        trimmed=trimmed,
        candidates_before_trim=pre_trim,
    )


class LocalityCapExceeded(Exception):
    """Raised when a generator exceeds a framework-level cap mid-run.

    The framework catches this, records the violation in metrics, emits
    a receipt, and terminates the generator branch with HARD_FAIL.
    """

    def __init__(self, cap_name: str, observed: int | float, limit: int | float):
        self.cap_name = cap_name
        self.observed = observed
        self.limit = limit
        super().__init__(
            f"locality cap exceeded: {cap_name} observed={observed} limit={limit}"
        )
