"""Wave 20.14.5L L-5 — InsideZoneGenerator.

Primitive ↔ Zone via bbox containment + overlap ratio.

Why GCs care: location-based takeoffs. If Drew knows a sprinkler head
is *inside* the Master Bathroom polygon, he can generate room-by-room
material lists and stage deliveries per room/phase instead of dumping
one big undifferentiated count.

Confidence calibration (overlap_ratio = fraction of primitive bbox
inside the zone bbox):

  overlap >= 0.95 (fully contained)  → 0.95
  overlap 0.75-0.95                  → 0.85
  overlap 0.50-0.75                  → 0.70
  overlap 0.30-0.50                  → 0.45 (UNKNOWN_REVIEW band, INSIDE_ZONE floor 0.7)
  overlap < 0.30                     → reject

INSIDE_ZONE taxonomy: confidence_floor=0.7, ambiguity_floor=0.5.
"""
from __future__ import annotations

from typing import ClassVar, Iterable

from ..generator import Candidate, EdgeGenerator, GeneratorContext
from ..locality import LocalityCaps
from ..taxonomy import EdgeType, EntityType, LocalityScope
from .data_access import EntitiesQueryClient, EntityRecord, ZoneRecord
from .spatial_index import bbox_center, bbox_overlap_ratio


class InsideZoneGenerator(EdgeGenerator):
    """Emits inside_zone edges from primitive ↔ zone containment."""

    name: ClassVar[str] = "inside_zone"
    version: ClassVar[str] = "1.0"
    edge_types: ClassVar[tuple[EdgeType, ...]] = (EdgeType.INSIDE_ZONE,)
    extraction_method: ClassVar[str] = "inside_zone_containment_v1"

    caps: ClassVar[LocalityCaps] = LocalityCaps(
        max_radius_px=1.0,           # not radius-bounded; uses bbox containment
        max_neighbors=1,
        max_candidates=50_000,
        max_edges_emitted=50_000,
        max_expensive_ops=50_000,
    )

    def __init__(self, query_client: EntitiesQueryClient):
        self._client = query_client

    def stage0_emit_candidates(
        self, ctx: GeneratorContext
    ) -> Iterable[Candidate]:
        sheets = self._client.list_sheets(
            project_id=ctx.project_id, suite_id=ctx.suite_id
        )
        if ctx.sheet_id is not None:
            sheets = [s for s in sheets if s.entity_id == ctx.sheet_id]

        for sheet in sheets:
            yield from self._emit_for_sheet(ctx, sheet)

    def _emit_for_sheet(
        self, ctx: GeneratorContext, sheet: EntityRecord
    ) -> Iterable[Candidate]:
        zones = self._client.list_zones_on_sheet(
            project_id=ctx.project_id, suite_id=ctx.suite_id, sheet_id=sheet.entity_id,
        )
        if not zones:
            return

        primitives = self._client.list_primitives_on_sheet(
            project_id=ctx.project_id, suite_id=ctx.suite_id, sheet_id=sheet.entity_id,
        )
        if not primitives:
            return

        # For each zone, scan primitives whose center is inside the zone bbox.
        # The cheap_filter stage prunes obvious misses before the expensive
        # overlap_ratio call.
        for zone in zones:
            for prim in primitives:
                prim_bbox = prim.bbox or [0.0, 0.0, 0.0, 0.0]
                # Cheap filter: primitive center inside zone bbox
                cx, cy = bbox_center(prim_bbox)
                if not (zone.bbox[0] <= cx <= zone.bbox[2]
                        and zone.bbox[1] <= cy <= zone.bbox[3]):
                    continue
                yield self._candidate(ctx, sheet, prim, zone, prim_bbox)

    def _candidate(
        self,
        ctx: GeneratorContext,
        sheet: EntityRecord,
        prim: EntityRecord,
        zone: ZoneRecord,
        prim_bbox: list[float],
    ) -> Candidate:
        overlap = bbox_overlap_ratio(prim_bbox, zone.bbox)
        confidence = self._score(overlap)

        return Candidate(
            edge_type=EdgeType.INSIDE_ZONE,
            project_id=ctx.project_id,
            sheet_id=sheet.entity_id,
            src_entity_id=prim.entity_id,
            src_entity_type=EntityType.PRIMITIVE.value,
            dst_entity_id=zone.zone_id,
            dst_entity_type=EntityType.ZONE.value,
            locality_scope=LocalityScope.SAME_ZONE.value,
            confidence=confidence,
            evidence={
                "containment_method": "bbox_overlap",
                "overlap_pct": round(overlap, 4),
            },
            provenance={
                "sheet_id": sheet.entity_id,
                "bbox": prim_bbox,
                "source_table": "blueprint_sheet_semantics (or future zones table)",
                "zone_type": zone.zone_type,
            },
            neighborhood_size=1,
        )

    @staticmethod
    def _score(overlap: float) -> float:
        # INSIDE_ZONE taxonomy: confidence_floor=0.7, ambiguity_floor=0.5
        if overlap >= 0.95:
            return 0.95               # VERIFIED
        if overlap >= 0.75:
            return 0.85               # VERIFIED
        if overlap >= 0.55:
            return 0.72               # VERIFIED (just above 0.7 floor)
        if overlap >= 0.35:
            return 0.55               # UNKNOWN_REVIEW band (0.5 <= conf < 0.7)
        return 0.0                    # below ambiguity → caller skips
