"""L-5 acceptance tests — locality-bounded inference generators.

annotated_by + inside_zone + adjacent_to. Spatial index correctness,
confidence calibration, deterministic ordering, UNKNOWN_REVIEW routing,
symmetric-edge dedup, scale_factor passthrough.
"""
from __future__ import annotations

import pytest

from .. import (
    EdgeType,
    GeneratorContext,
    GeneratorHealth,
    InMemoryLockProvider,
    InMemoryReceiptPersistor,
    ResolutionState,
)
from ..taxonomy import LocalityScope
from .adjacent_to import AdjacentToGenerator
from .annotated_by import AnnotatedByGenerator
from .data_access import EntityRecord, InMemoryQueryClient, OcrSpanRecord, ZoneRecord
from .inside_zone import InsideZoneGenerator
from .spatial_index import (
    SpatialIndex,
    alignment_factor,
    bbox_center,
    bbox_overlap_ratio,
    normalize_radius_for_sheet,
    polygon_to_bbox,
)


# ============================================================
# Fixtures
# ============================================================

UID_SUITE = "00000000-0000-0000-0000-000000000001"
UID_PROJECT = "00000000-0000-0000-0000-000000000002"
UID_SHEET = "00000000-0000-0000-0000-00000000aaaa"


def _sheet() -> EntityRecord:
    return EntityRecord(
        entity_id=UID_SHEET,
        entity_type="sheet",
        project_id=UID_PROJECT,
        suite_id=UID_SUITE,
        sheet_id=UID_SHEET,
    )


def _primitive(eid: str, bbox: list[float]) -> EntityRecord:
    return EntityRecord(
        entity_id=eid,
        entity_type="primitive",
        project_id=UID_PROJECT,
        suite_id=UID_SUITE,
        sheet_id=UID_SHEET,
        bbox=bbox,
    )


def _ocr(span_id: str, bbox: list[float], text: str = "A-101") -> OcrSpanRecord:
    return OcrSpanRecord(
        span_id=span_id,
        sheet_id=UID_SHEET,
        project_id=UID_PROJECT,
        suite_id=UID_SUITE,
        text=text,
        bbox=bbox,
    )


def _zone(zone_id: str, bbox: list[float], *, zone_type: str = "title_block") -> ZoneRecord:
    return ZoneRecord(
        zone_id=zone_id,
        sheet_id=UID_SHEET,
        project_id=UID_PROJECT,
        suite_id=UID_SUITE,
        zone_type=zone_type,
        bbox=bbox,
    )


def _ctx() -> GeneratorContext:
    return GeneratorContext(
        suite_id=UID_SUITE,
        project_id=UID_PROJECT,
        pipeline_version="L5_1.0",
        lock_provider=InMemoryLockProvider(),
    )


# ============================================================
# Spatial index correctness
# ============================================================


def test_spatial_index_returns_sorted_by_distance():
    points = [
        ("a", (100.0, 100.0)),
        ("b", (105.0, 100.0)),
        ("c", (110.0, 100.0)),
        ("d", (200.0, 200.0)),
    ]
    idx = SpatialIndex([p for p in points], point_of=lambda p: p[1])
    out = idx.query_within((100.0, 100.0), radius=20.0, max_results=10)
    distances = [d for d, _ in out]
    assert distances == sorted(distances)
    assert len(out) == 3  # d is outside radius


def test_spatial_index_caps_max_results():
    points = [("p" + str(i), (i, 0.0)) for i in range(20)]
    idx = SpatialIndex(points, point_of=lambda p: p[1])
    out = idx.query_within((0.0, 0.0), radius=100.0, max_results=5)
    assert len(out) == 5


def test_polygon_to_bbox_handles_8point_ocr_polygon():
    # OCR span format from Azure: 4-point polygon (8 coords)
    polygon = [203, 59, 613, 40, 615, 82, 205, 100]
    bbox = polygon_to_bbox(polygon)
    assert bbox == [203, 40, 615, 100]


def test_bbox_overlap_ratio_full_containment():
    inner = [10, 10, 20, 20]
    outer = [0, 0, 100, 100]
    assert bbox_overlap_ratio(inner, outer) == 1.0


def test_bbox_overlap_ratio_partial():
    inner = [0, 0, 10, 10]                # area 100
    outer = [5, 5, 15, 15]                # overlaps [5..10, 5..10] = area 25
    assert bbox_overlap_ratio(inner, outer) == 0.25


def test_alignment_factor_horizontal():
    src = [0, 0, 10, 10]                  # center (5, 5)
    dst = [50, 0, 60, 10]                 # center (55, 5) — same y
    axis, factor = alignment_factor(src, dst)
    assert axis == "horizontal"
    assert factor == 1.0


def test_alignment_factor_vertical():
    src = [0, 0, 10, 10]                  # center (5, 5)
    dst = [0, 50, 10, 60]                 # center (5, 55) — same x
    axis, factor = alignment_factor(src, dst)
    assert axis == "vertical"
    assert 0 < factor < 1.0


def test_normalize_radius_passthrough_at_unit_scale():
    assert normalize_radius_for_sheet(50.0, sheet_scale_factor=1.0) == 50.0


def test_normalize_radius_scales_proportionally():
    # Watchpoint #2: same semantic radius, different sheet scale
    assert normalize_radius_for_sheet(50.0, sheet_scale_factor=2.0) == 100.0


def test_normalize_radius_rejects_invalid_scale():
    with pytest.raises(ValueError):
        normalize_radius_for_sheet(50.0, sheet_scale_factor=0.0)


# ============================================================
# AnnotatedByGenerator
# ============================================================


def test_annotated_by_emits_horizontal_neighbor():
    """Primitive at (100,100) + OCR span at (130,100) → horizontal annotation."""
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[_primitive("prim-1", [95, 95, 105, 105])],
        ocr_spans=[_ocr("ocr-1", [120, 95, 140, 105], text="A-101")],
    )
    gen = AnnotatedByGenerator(client)
    result = gen.run(_ctx(), receipt_persistor=InMemoryReceiptPersistor())

    assert result.metrics.edges_emitted == 1
    edge = result.accepted_edges[0]
    assert edge.evidence["alignment"] == "horizontal"
    assert edge.evidence["ocr_text"] == "A-101"
    assert edge.confidence > 0.7


def test_annotated_by_far_distance_drops_to_unknown_review():
    """Distance ~78% of radius + horizontal alignment → conf 0.40 → UR band.

    primitive center (101, 101); OCR span center (163, 101) → distance ~62.
    62 / 80 = 0.775 → base 0.40; horizontal align factor 1.0; no zone.
    Final conf 0.40 — inside ANNOTATED_BY ambiguity band [0.3, 0.5).
    """
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[_primitive("prim-1", [100, 100, 102, 102])],
        ocr_spans=[_ocr("ocr-1", [158, 99, 168, 103])],
    )
    gen = AnnotatedByGenerator(client)
    result = gen.run(_ctx(), receipt_persistor=InMemoryReceiptPersistor())

    assert result.metrics.edges_emitted == 0
    assert result.metrics.unknown_review_count == 1


def test_annotated_by_respects_max_radius():
    """OCR span beyond max_radius_px=80 → no candidate emitted."""
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[_primitive("prim-1", [0, 0, 5, 5])],
        ocr_spans=[_ocr("ocr-far", [500, 500, 510, 510])],
    )
    gen = AnnotatedByGenerator(client)
    result = gen.run(_ctx(), receipt_persistor=InMemoryReceiptPersistor())
    assert result.metrics.candidates_considered == 0


def test_annotated_by_zone_bonus_boosts_confidence():
    client_no_zone = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[_primitive("prim-1", [100, 100, 102, 102])],
        ocr_spans=[_ocr("ocr-1", [120, 100, 130, 102])],
    )
    client_with_zone = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[_primitive("prim-1", [100, 100, 102, 102])],
        ocr_spans=[_ocr("ocr-1", [120, 100, 130, 102])],
        zones=[_zone("z-1", [50, 50, 200, 200], zone_type="title_block")],
    )
    no_zone = AnnotatedByGenerator(client_no_zone).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    with_zone = AnnotatedByGenerator(client_with_zone).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    assert with_zone.accepted_edges[0].confidence >= no_zone.accepted_edges[0].confidence


def test_annotated_by_caps_neighbors_per_primitive():
    """max_neighbors=8 → only 8 closest OCR spans considered per primitive."""
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[_primitive("prim-1", [100, 100, 102, 102])],
        ocr_spans=[
            _ocr(f"ocr-{i}", [110 + i, 100, 110 + i + 5, 102])
            for i in range(15)
        ],
    )
    result = AnnotatedByGenerator(client).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    # Some candidates VERIFIED, some UNKNOWN_REVIEW; total ≤ 8
    total = result.metrics.edges_emitted + result.metrics.unknown_review_count
    assert total <= 8


# ============================================================
# InsideZoneGenerator
# ============================================================


def test_inside_zone_emits_when_primitive_center_inside_zone():
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[_primitive("prim-1", [100, 100, 110, 110])],
        zones=[_zone("z-1", [50, 50, 200, 200], zone_type="legend")],
    )
    result = InsideZoneGenerator(client).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    assert result.metrics.edges_emitted == 1
    assert result.accepted_edges[0].confidence == 0.95   # fully contained
    assert result.accepted_edges[0].evidence["overlap_pct"] == 1.0


def test_inside_zone_skips_primitives_outside_zone():
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[_primitive("prim-far", [500, 500, 510, 510])],
        zones=[_zone("z-1", [0, 0, 100, 100])],
    )
    result = InsideZoneGenerator(client).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    assert result.metrics.edges_emitted == 0


def test_inside_zone_partial_overlap_routes_to_unknown_review():
    """Partial overlap (~44%) → conf 0.55 → INSIDE_ZONE UR band.

    primitive [0, 0, 30, 30] area 900, center (15, 15).
    zone [10, 10, 30, 30] — center inside.
    overlap area = 400 → ratio = 0.444 → score returns 0.55.
    INSIDE_ZONE ambiguity band: 0.5 <= conf < 0.7 → UNKNOWN_REVIEW.
    """
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[_primitive("prim-edge", [0, 0, 30, 30])],
        zones=[_zone("z-1", [10, 10, 30, 30])],
    )
    result = InsideZoneGenerator(client).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    assert result.metrics.unknown_review_count == 1
    assert result.metrics.edges_emitted == 0


def test_inside_zone_dst_type_is_zone():
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[_primitive("prim-1", [100, 100, 110, 110])],
        zones=[_zone("z-1", [50, 50, 200, 200])],
    )
    result = InsideZoneGenerator(client).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    edge = result.accepted_edges[0]
    assert edge.dst_entity_type.value == "zone"
    assert edge.edge_type == EdgeType.INSIDE_ZONE
    assert edge.locality_scope == LocalityScope.SAME_ZONE


# ============================================================
# AdjacentToGenerator
# ============================================================


def test_adjacent_to_emits_one_edge_per_close_pair_symmetric_dedupe():
    """Two primitives 10px apart → 1 edge (not 2; symmetric dedup)."""
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[
            _primitive("aaa-prim", [100, 100, 105, 105]),
            _primitive("bbb-prim", [110, 100, 115, 105]),
        ],
    )
    result = AdjacentToGenerator(client).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    assert result.metrics.edges_emitted == 1
    edge = result.accepted_edges[0]
    assert edge.src_entity_id == "aaa-prim"   # lex-sorted
    assert edge.dst_entity_id == "bbb-prim"


def test_adjacent_to_respects_radius():
    """Primitives 50px apart → outside 30px radius → no edge."""
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[
            _primitive("a", [100, 100, 105, 105]),
            _primitive("b", [200, 100, 205, 105]),
        ],
    )
    result = AdjacentToGenerator(client).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    assert result.metrics.edges_emitted == 0


def test_adjacent_to_classifies_axis():
    horizontal_client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[
            _primitive("a", [100, 100, 105, 105]),
            _primitive("b", [115, 100, 120, 105]),
        ],
    )
    result = AdjacentToGenerator(horizontal_client).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    edge = result.accepted_edges[0]
    assert edge.evidence["axis"] == "horizontal"


def test_adjacent_to_no_self_loop():
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[_primitive("only", [100, 100, 105, 105])],
    )
    result = AdjacentToGenerator(client).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    assert result.metrics.edges_emitted == 0


# ============================================================
# Cross-cutting: scale_factor passthrough
# ============================================================


def test_annotated_by_scale_factor_doubles_effective_radius():
    """With scale_factor=2.0, radius becomes 160px → previously-out-of-range
    span at 120px is now in range."""
    spans = [_ocr("ocr-1", [100 + 120, 100, 100 + 125, 102])]   # 120px to the right
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[_primitive("prim-1", [100, 100, 102, 102])],
        ocr_spans=spans,
    )
    no_scale = AnnotatedByGenerator(client, sheet_scale_factor=1.0).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    scaled = AnnotatedByGenerator(client, sheet_scale_factor=2.0).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    # At 1.0× span is out of range → 0 candidates
    # At 2.0× span is in range → ≥1 candidate
    assert no_scale.metrics.candidates_considered == 0
    assert scaled.metrics.candidates_considered >= 1


# ============================================================
# Framework invariants under inference generators
# ============================================================


def test_locality_generators_emit_receipts_with_distance_bucketing():
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[
            _primitive("a", [100, 100, 105, 105]),
            _primitive("b", [110, 100, 115, 105]),
            _primitive("c", [120, 100, 125, 105]),
        ],
    )
    persistor = InMemoryReceiptPersistor()
    AdjacentToGenerator(client).run(_ctx(), receipt_persistor=persistor)

    assert len(persistor.receipts) == 1
    r = persistor.receipts[0]
    assert r.generator_name == "adjacent_to"
    assert r.edges_by_locality == {"same_sheet": r.edges_emitted}


def test_all_three_generators_health_green_on_normal_data():
    client = InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=[
            _primitive("aaa", [100, 100, 105, 105]),
            _primitive("bbb", [110, 100, 115, 105]),
        ],
        ocr_spans=[_ocr("ocr-1", [120, 100, 130, 102])],
        zones=[_zone("z-1", [50, 50, 200, 200], zone_type="legend")],
    )
    for cls in (AnnotatedByGenerator, InsideZoneGenerator, AdjacentToGenerator):
        result = cls(client).run(_ctx(), receipt_persistor=InMemoryReceiptPersistor())
        assert result.metrics.health == GeneratorHealth.GREEN, (
            f"{cls.__name__} health was {result.metrics.health}"
        )
