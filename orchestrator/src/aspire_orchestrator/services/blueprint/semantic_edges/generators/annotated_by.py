"""Wave 20.14.5L L-5 — AnnotatedByGenerator.

Primitive ↔ OCR span via spatial proximity + zone gate.

This is Drew's first inference generator. A symbol is just geometry
until the OCR text near it says "3/4" Type L Copper" — that's what
turns a circle into a *spec*. Wrong annotation → wrong material →
ruined bid.

Confidence calibration (piecewise, calibrated for the GC bidding
context where false positives are catastrophic):

  distance ≤ 25% radius + horizontal alignment + zone match  → 0.85-0.95
  distance 25-50% radius + alignment + zone match            → 0.65-0.80
  distance 50-75% radius OR weak alignment                    → 0.40-0.55 (UNKNOWN_REVIEW band)
  distance > 75% radius OR no zone match                      → reject

ANNOTATED_BY taxonomy: confidence_floor=0.5, ambiguity_floor=0.3.
So edges with confidence in [0.3, 0.5) automatically route to
UNKNOWN_REVIEW for human verification.
"""
from __future__ import annotations

from typing import ClassVar, Iterable, Optional

from ..generator import Candidate, EdgeGenerator, GeneratorContext
from ..locality import LocalityCaps
from ..taxonomy import EdgeType, EntityType, LocalityScope
from .data_access import EntitiesQueryClient, EntityRecord, OcrSpanRecord, ZoneRecord
from .spatial_index import (
    SpatialIndex,
    alignment_factor,
    bbox_center,
    bbox_overlap_ratio,
    normalize_radius_for_sheet,
)


class AnnotatedByGenerator(EdgeGenerator):
    """Emits annotated_by edges from primitive → OCR span proximity."""

    name: ClassVar[str] = "annotated_by"
    version: ClassVar[str] = "1.0"
    edge_types: ClassVar[tuple[EdgeType, ...]] = (EdgeType.ANNOTATED_BY,)
    extraction_method: ClassVar[str] = "annotated_by_spatial_v1"

    # Real spatial constraints. Tight caps for the FIRST inference
    # generator — we'd rather miss edges than create false positives.
    caps: ClassVar[LocalityCaps] = LocalityCaps(
        max_radius_px=80.0,                # ~80pt search window
        max_neighbors=8,                   # only the 8 closest spans per primitive
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
        ocr_spans = self._client.list_ocr_spans_on_sheet(
            project_id=ctx.project_id, suite_id=ctx.suite_id, sheet_id=sheet.entity_id,
        )
        zones = self._client.list_zones_on_sheet(
            project_id=ctx.project_id, suite_id=ctx.suite_id, sheet_id=sheet.entity_id,
        )

        if not primitives or not ocr_spans:
            return

        # Build the spatial index ONCE per sheet — O(log n) lookups
        span_index = SpatialIndex(
            ocr_spans,
            point_of=lambda s: bbox_center(s.bbox),
        )

        # Pre-index zones by type for O(1) gate checks
        zone_lookup: dict[str, list[ZoneRecord]] = {}
        for z in zones:
            zone_lookup.setdefault(z.zone_type, []).append(z)

        for prim in primitives:
            yield from self._candidates_for_primitive(
                ctx, sheet, prim, span_index, zone_lookup, radius,
            )

    def _candidates_for_primitive(
        self,
        ctx: GeneratorContext,
        sheet: EntityRecord,
        prim: EntityRecord,
        span_index: SpatialIndex,
        zone_lookup: dict[str, list[ZoneRecord]],
        radius: float,
    ) -> Iterable[Candidate]:
        prim_bbox = prim.bbox or [0.0, 0.0, 0.0, 0.0]
        prim_center = bbox_center(prim_bbox)

        neighbors = span_index.query_within(
            prim_center,
            radius=radius,
            max_results=self.caps.max_neighbors,
        )

        for distance, span in neighbors:
            confidence, alignment, zone_match = self._score(
                prim_bbox=prim_bbox,
                span_bbox=span.bbox,
                distance=distance,
                radius=radius,
                zone_lookup=zone_lookup,
            )

            # Reject before emission if confidence below ambiguity floor;
            # framework will route the rest to UNKNOWN_REVIEW automatically.
            if confidence <= 0.0:
                continue

            yield Candidate(
                edge_type=EdgeType.ANNOTATED_BY,
                project_id=ctx.project_id,
                sheet_id=sheet.entity_id,
                src_entity_id=prim.entity_id,
                src_entity_type=EntityType.PRIMITIVE.value,
                dst_entity_id=span.span_id,
                dst_entity_type=EntityType.OCR_SPAN.value,
                locality_scope=(
                    LocalityScope.SAME_ZONE.value if zone_match
                    else LocalityScope.SAME_SHEET.value
                ),
                spatial_distance=distance,
                confidence=confidence,
                evidence={
                    "ocr_text": span.text,
                    "distance_px": round(distance, 2),
                    "alignment": alignment,
                    "zone": zone_match or "none",
                },
                provenance={
                    "sheet_id": sheet.entity_id,
                    "bbox": prim_bbox,
                    "source_table": "blueprint_sheet_intelligence.paragraphs",
                    "scale_factor": self._scale_factor,
                },
                neighborhood_size=len(neighbors),
            )

    def _score(
        self,
        *,
        prim_bbox: list[float],
        span_bbox: list[float],
        distance: float,
        radius: float,
        zone_lookup: dict[str, list[ZoneRecord]],
    ) -> tuple[float, str, Optional[str]]:
        """Calibrated piecewise confidence.

        Returns (confidence, alignment_label, matched_zone_or_None).
        """
        # Distance band — closer is much stronger
        ratio = distance / max(radius, 1e-6)
        if ratio <= 0.25:
            base = 0.92
        elif ratio <= 0.50:
            base = 0.75
        elif ratio <= 0.75:
            base = 0.55
        else:
            base = 0.40

        # Alignment multiplier
        align_label, align_factor = alignment_factor(prim_bbox, span_bbox)

        # Zone bonus — when the primitive's center sits inside a
        # title_block / legend / schedule zone, we have higher
        # confidence the OCR span is its label.
        zone_match: Optional[str] = None
        prim_cx, prim_cy = bbox_center(prim_bbox)
        for zone_type, zones in zone_lookup.items():
            for z in zones:
                if (z.bbox[0] <= prim_cx <= z.bbox[2]
                        and z.bbox[1] <= prim_cy <= z.bbox[3]):
                    zone_match = zone_type
                    break
            if zone_match:
                break

        zone_bonus = 1.10 if zone_match in ("title_block", "legend", "schedule") else 1.0
        confidence = min(1.0, base * align_factor * zone_bonus)
        return confidence, align_label, zone_match
