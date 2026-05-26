"""Wave 20.14.5L L-5 — AdjacentToGenerator.

Primitive ↔ Primitive within a tight radius. Symmetric edge.

Why GCs care: assemblies. A light switch next to a door swing implies
"switch + cover plate + box + Romex pull" all moved together. A shut-
off valve adjacent to a pump implies pipe + union + valve + fittings.
Missing the adjacency means missing accessory line items in the bid.

Confidence calibration (piecewise on distance/radius ratio):

  ratio ≤ 0.25                     → 0.85
  ratio 0.25-0.50                  → 0.70
  ratio 0.50-0.75                  → 0.55
  ratio 0.75-1.00                  → 0.40 (UNKNOWN_REVIEW band, ADJACENT_TO floor 0.5)
  ratio > 1.00                     → reject (outside radius)

ADJACENT_TO is SYMMETRIC. We dedupe by emitting only when
src_id < dst_id lexicographically — deterministic + matches the
DB unique constraint.

ADJACENT_TO taxonomy: confidence_floor=0.5, ambiguity_floor=0.3.
"""
from __future__ import annotations

from typing import ClassVar, Iterable

from ..generator import Candidate, EdgeGenerator, GeneratorContext
from ..locality import LocalityCaps
from ..taxonomy import EdgeType, EntityType, LocalityScope
from .data_access import EntitiesQueryClient, EntityRecord
from .spatial_index import SpatialIndex, bbox_center, normalize_radius_for_sheet


class AdjacentToGenerator(EdgeGenerator):
    """Emits symmetric adjacent_to edges between primitives within radius."""

    name: ClassVar[str] = "adjacent_to"
    version: ClassVar[str] = "1.0"
    edge_types: ClassVar[tuple[EdgeType, ...]] = (EdgeType.ADJACENT_TO,)
    extraction_method: ClassVar[str] = "adjacent_to_spatial_v1"

    # Tight defaults — adjacency is a high-cardinality edge type and
    # the dominant graph-explosion risk. Keep it small until L-6
    # audit suite confirms quality.
    caps: ClassVar[LocalityCaps] = LocalityCaps(
        max_radius_px=30.0,                # ~30pt
        max_neighbors=4,                   # 4 closest other primitives
        max_candidates=50_000,
        max_edges_emitted=50_000,
        max_expensive_ops=50_000,
    )

    def __init__(
        self,
        query_client: EntitiesQueryClient,
        *,
        sheet_scale_factor: float = 1.0,
    ):
        self._client = query_client
        self._scale_factor = sheet_scale_factor

    def stage0_emit_candidates(
        self, ctx: GeneratorContext
    ) -> Iterable[Candidate]:
        sheets = self._client.list_sheets(
            project_id=ctx.project_id, suite_id=ctx.suite_id
        )
        if ctx.sheet_id is not None:
            sheets = [s for s in sheets if s.entity_id == ctx.sheet_id]

        radius = normalize_radius_for_sheet(
            self.caps.max_radius_px,
            sheet_scale_factor=self._scale_factor,
        )

        for sheet in sheets:
            yield from self._emit_for_sheet(ctx, sheet, radius)

    def _emit_for_sheet(
        self, ctx: GeneratorContext, sheet: EntityRecord, radius: float
    ) -> Iterable[Candidate]:
        primitives = self._client.list_primitives_on_sheet(
            project_id=ctx.project_id, suite_id=ctx.suite_id, sheet_id=sheet.entity_id,
        )
        if len(primitives) < 2:
            return

        # ONE spatial index per sheet
        index = SpatialIndex(
            primitives,
            point_of=lambda p: bbox_center(p.bbox or [0, 0, 0, 0]),
        )

        # Iterate primitives sorted by id for deterministic dedup
        for prim in sorted(primitives, key=lambda p: p.entity_id):
            center = bbox_center(prim.bbox or [0, 0, 0, 0])
            neighbors = index.query_within(
                center,
                radius=radius,
                max_results=self.caps.max_neighbors + 1,  # +1 because self is included
            )
            for distance, other in neighbors:
                if other.entity_id == prim.entity_id:
                    continue
                # Dedup: emit only when src_id < dst_id lex
                if prim.entity_id >= other.entity_id:
                    continue

                confidence = self._score(distance, radius)
                if confidence <= 0.0:
                    continue

                axis = self._classify_axis(prim, other)
                yield Candidate(
                    edge_type=EdgeType.ADJACENT_TO,
                    project_id=ctx.project_id,
                    sheet_id=sheet.entity_id,
                    src_entity_id=prim.entity_id,
                    src_entity_type=EntityType.PRIMITIVE.value,
                    dst_entity_id=other.entity_id,
                    dst_entity_type=EntityType.PRIMITIVE.value,
                    locality_scope=LocalityScope.SAME_SHEET.value,
                    spatial_distance=distance,
                    confidence=confidence,
                    evidence={
                        "distance_px": round(distance, 2),
                        "axis": axis,
                    },
                    provenance={
                        "sheet_id": sheet.entity_id,
                        "bbox": prim.bbox or [0, 0, 0, 0],
                        "source_table": "blueprint_primitives",
                        "scale_factor": self._scale_factor,
                    },
                    neighborhood_size=len(neighbors),
                )

    @staticmethod
    def _score(distance: float, radius: float) -> float:
        ratio = distance / max(radius, 1e-6)
        if ratio <= 0.25:
            return 0.85
        if ratio <= 0.50:
            return 0.70
        if ratio <= 0.75:
            return 0.55
        if ratio <= 1.00:
            return 0.40                # UNKNOWN_REVIEW band
        return 0.0

    @staticmethod
    def _classify_axis(a: EntityRecord, b: EntityRecord, *, tol: float = 4.0) -> str:
        ax, ay = bbox_center(a.bbox or [0, 0, 0, 0])
        bx, by = bbox_center(b.bbox or [0, 0, 0, 0])
        dx = abs(ax - bx)
        dy = abs(ay - by)
        if dy <= tol and dx > tol:
            return "horizontal"
        if dx <= tol and dy > tol:
            return "vertical"
        if dx <= tol and dy <= tol:
            return "coincident"
        return "diagonal"
