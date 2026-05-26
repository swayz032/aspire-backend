"""L-3 acceptance tests — generator framework invariants.

These tests are the contract for L-3 completion:
 1. Generator framework enforces locality centrally
 2. Replay hashes stable across reruns
 3. Duplicate generation impossible under concurrency
 4. UNKNOWN_REVIEW operational with thresholds
 5. Every generator emits receipts
 6. Runtime per generator measurable independently
 7. Candidate counts observable
 8. Expensive-stage invocation counts observable
 9. No generator can emit invalid taxonomy edges bypassing validator
"""
from __future__ import annotations

from dataclasses import replace
from typing import Iterable
from unittest.mock import MagicMock

import pytest

from . import (
    BBox,
    Candidate,
    EdgeGenerator,
    EdgeType,
    GeneratorContext,
    GeneratorHealth,
    InMemoryLockProvider,
    InMemoryReceiptPersistor,
    LocalityCaps,
    TAXONOMY_VERSION,
    compute_health,
    euclidean_distance,
    query_neighbors_within,
)
from .taxonomy import LocalityScope


# ============================================================
# Fixtures: tiny in-memory generator
# ============================================================

UID_SUITE = "00000000-0000-0000-0000-000000000001"
UID_PROJECT = "00000000-0000-0000-0000-000000000002"
UID_SHEET = "00000000-0000-0000-0000-000000000003"


def _make_candidate(src: str, dst: str, *, confidence: float = 0.999) -> Candidate:
    return Candidate(
        edge_type=EdgeType.BELONGS_TO_SHEET,
        project_id=UID_PROJECT,
        sheet_id=UID_SHEET,
        src_entity_id=src,
        src_entity_type="primitive",
        dst_entity_id=dst,
        dst_entity_type="sheet",
        locality_scope=LocalityScope.SAME_SHEET.value,
        confidence=confidence,
        evidence={"page_index": 1},
        provenance={"sheet_id": UID_SHEET, "bbox": [0, 0, 10, 10]},
        neighborhood_size=4,
    )


class TinyGenerator(EdgeGenerator):
    """Belongs-to-sheet stand-in for framework tests."""
    name = "test_belongs_to_sheet"
    version = "1.0"
    edge_types = (EdgeType.BELONGS_TO_SHEET,)
    extraction_method = "test_v1"

    def __init__(self, candidates: list[Candidate]):
        self._candidates = candidates

    def stage0_emit_candidates(self, ctx) -> Iterable[Candidate]:
        return list(self._candidates)


def _ctx(
    *,
    lock_provider=None,
    sheet_id=None,
) -> GeneratorContext:
    return GeneratorContext(
        suite_id=UID_SUITE,
        project_id=UID_PROJECT,
        pipeline_version="test_1.0",
        sheet_id=sheet_id,
        lock_provider=lock_provider,
    )


# ============================================================
# Invariant 1: framework enforces locality caps centrally
# ============================================================


def test_max_candidates_cap_enforced():
    """If stage 0 emits > caps.max_candidates, framework trims + records violation."""

    class FloodGenerator(TinyGenerator):
        caps = LocalityCaps(max_candidates=5, max_edges_emitted=100)

    candidates = [
        _make_candidate(f"prim-{i:04d}", UID_SHEET) for i in range(20)
    ]
    gen = FloodGenerator(candidates)
    persistor = InMemoryReceiptPersistor()

    result = gen.run(_ctx(lock_provider=InMemoryLockProvider()), receipt_persistor=persistor)

    assert result.metrics.candidates_considered == 5
    assert result.metrics.locality_violations >= 1
    assert result.metrics.locality_trimmed_count == 15


def test_max_edges_emitted_cap_enforced():
    class TinyCap(TinyGenerator):
        caps = LocalityCaps(max_candidates=100, max_edges_emitted=3)

    candidates = [_make_candidate(f"p-{i}", UID_SHEET) for i in range(10)]
    gen = TinyCap(candidates)
    result = gen.run(_ctx(lock_provider=InMemoryLockProvider()),
                     receipt_persistor=InMemoryReceiptPersistor())
    assert result.metrics.edges_emitted == 3


def test_locality_caps_constructor_rejects_out_of_bounds():
    with pytest.raises(ValueError):
        LocalityCaps(max_radius_px=999_999.0)
    with pytest.raises(ValueError):
        LocalityCaps(max_neighbors=0)


# ============================================================
# Invariant 2: deterministic ordering
# ============================================================


def test_candidate_ordering_is_stable_across_reruns():
    """Same input set → same output edge order → same output_edge_hash."""
    candidates_a = [_make_candidate(f"prim-{i:04d}", UID_SHEET) for i in range(8)]
    candidates_b = list(reversed(candidates_a))  # shuffle input order

    gen_a = TinyGenerator(candidates_a)
    gen_b = TinyGenerator(candidates_b)
    r_a = gen_a.run(_ctx(lock_provider=InMemoryLockProvider()),
                    receipt_persistor=InMemoryReceiptPersistor())
    r_b = gen_b.run(_ctx(lock_provider=InMemoryLockProvider()),
                    receipt_persistor=InMemoryReceiptPersistor())

    assert r_a.metrics.output_edge_hash == r_b.metrics.output_edge_hash


# ============================================================
# Invariant 3: concurrency guardrails
# ============================================================


def test_advisory_lock_blocks_concurrent_runs():
    """When the lock is already held, a second invocation aborts cleanly."""
    lock = InMemoryLockProvider()
    candidates = [_make_candidate("p1", UID_SHEET)]
    gen = TinyGenerator(candidates)

    # Hold the lock manually
    with lock.acquire(UID_PROJECT, gen.name) as got:
        assert got
        # Second concurrent attempt aborts
        result = gen.run(_ctx(lock_provider=lock), receipt_persistor=InMemoryReceiptPersistor())
        assert result.aborted is True
        assert result.abort_reason == "advisory_lock_unavailable"
        assert result.metrics.edges_emitted == 0


# ============================================================
# Invariant 4: UNKNOWN_REVIEW quotas
# ============================================================


def test_health_thresholds_pure_function():
    assert compute_health(0, 0) == GeneratorHealth.GREEN
    assert compute_health(0, 100) == GeneratorHealth.GREEN
    assert compute_health(20, 100) == GeneratorHealth.GREEN     # 20% <= 30%
    assert compute_health(40, 100) == GeneratorHealth.YELLOW    # 40% > 30%
    assert compute_health(60, 100) == GeneratorHealth.RED       # 60% > 50%
    assert compute_health(80, 100) == GeneratorHealth.HARD_FAIL # 80% > 70%


def _make_annotated_by_candidate(src: str, ocr_id: str, *, confidence: float) -> Candidate:
    """ANNOTATED_BY has confidence_floor=0.5 + ambiguity_floor=0.3 → real ambiguity band."""
    return Candidate(
        edge_type=EdgeType.ANNOTATED_BY,
        project_id=UID_PROJECT,
        sheet_id=UID_SHEET,
        src_entity_id=src,
        src_entity_type="primitive",
        dst_entity_id=ocr_id,
        dst_entity_type="ocr_span",
        locality_scope=LocalityScope.SAME_SHEET.value,
        spatial_distance=20.0,
        confidence=confidence,
        evidence={"ocr_text": "A-101", "distance_px": 20.0,
                  "alignment": "horizontal", "zone": "title_block"},
        provenance={"sheet_id": UID_SHEET, "bbox": [0, 0, 10, 10]},
        neighborhood_size=4,
    )


class AnnotatedByGenerator(EdgeGenerator):
    name = "test_annotated_by"
    version = "1.0"
    edge_types = (EdgeType.ANNOTATED_BY,)
    extraction_method = "test_annotated_by_v1"

    def __init__(self, candidates):
        self._candidates = candidates

    def stage0_emit_candidates(self, ctx):
        return list(self._candidates)


def test_hard_fail_quota_drops_all_edges():
    """If UNKNOWN_REVIEW > 70%, framework drops all accepted edges.

    ANNOTATED_BY: confidence_floor=0.5, ambiguity_floor=0.3.
    8 candidates at conf=0.4 → UNKNOWN_REVIEW (in ambiguity band).
    2 candidates at conf=0.9 → VERIFIED (above floor).
    80% UR rate → HARD_FAIL → all edges dropped.
    """
    candidates = []
    for i in range(8):
        candidates.append(_make_annotated_by_candidate(f"ur-{i:04d}", f"ocr-ur-{i:04d}",
                                                        confidence=0.4))
    for i in range(2):
        candidates.append(_make_annotated_by_candidate(f"ok-{i:04d}", f"ocr-ok-{i:04d}",
                                                        confidence=0.9))

    gen = AnnotatedByGenerator(candidates)
    result = gen.run(_ctx(lock_provider=InMemoryLockProvider()),
                     receipt_persistor=InMemoryReceiptPersistor())

    assert result.metrics.unknown_review_count == 8
    assert result.metrics.health == GeneratorHealth.HARD_FAIL
    assert result.aborted is True
    # Hard fail = no edges persisted
    assert result.accepted_edges == []
    assert result.metrics.output_edge_hash == compute_output_edge_hash_of_empty()


def compute_output_edge_hash_of_empty():
    from .replay import compute_output_edge_hash
    return compute_output_edge_hash([])


# ============================================================
# Invariant 5: every generator emits a receipt
# ============================================================


def test_receipt_always_persisted_even_on_abort():
    lock = InMemoryLockProvider()
    persistor = InMemoryReceiptPersistor()
    candidates = [_make_candidate("p1", UID_SHEET)]
    gen = TinyGenerator(candidates)

    # Force abort via held lock
    with lock.acquire(UID_PROJECT, gen.name):
        result = gen.run(_ctx(lock_provider=lock), receipt_persistor=persistor)

    assert result.aborted
    assert len(persistor.receipts) == 1
    assert persistor.receipts[0].aborted is True


def test_receipt_records_generator_identity():
    candidates = [_make_candidate("p1", UID_SHEET, confidence=0.999)]
    gen = TinyGenerator(candidates)
    persistor = InMemoryReceiptPersistor()
    gen.run(_ctx(lock_provider=InMemoryLockProvider()), receipt_persistor=persistor)

    r = persistor.receipts[0]
    assert r.generator_name == "test_belongs_to_sheet"
    assert r.generator_version == "1.0"
    assert r.taxonomy_version == TAXONOMY_VERSION


# ============================================================
# Invariant 6: runtime measurable
# ============================================================


def test_runtime_per_stage_measurable():
    candidates = [_make_candidate(f"p-{i}", UID_SHEET, confidence=0.999) for i in range(5)]
    gen = TinyGenerator(candidates)
    result = gen.run(_ctx(lock_provider=InMemoryLockProvider()),
                     receipt_persistor=InMemoryReceiptPersistor())

    assert result.metrics.runtime_ms_total > 0
    assert "stage0_locality" in result.metrics.stages
    assert "stage1_cheap_filter" in result.metrics.stages
    assert "stage4_topology" in result.metrics.stages


# ============================================================
# Invariant 7: candidate counts observable
# ============================================================


def test_candidate_counts_observable():
    candidates = [_make_candidate(f"p-{i}", UID_SHEET, confidence=0.999) for i in range(6)]
    gen = TinyGenerator(candidates)
    result = gen.run(_ctx(lock_provider=InMemoryLockProvider()),
                     receipt_persistor=InMemoryReceiptPersistor())

    assert result.metrics.candidates_considered == 6
    assert result.metrics.edges_emitted == 6
    assert result.metrics.edges_by_type["belongs_to_sheet"] == 6


# ============================================================
# Invariant 8: expensive ops counter observable
# ============================================================


def test_expensive_ops_observable():
    candidates = [_make_candidate(f"p-{i}", UID_SHEET, confidence=0.999) for i in range(5)]
    gen = TinyGenerator(candidates)
    result = gen.run(_ctx(lock_provider=InMemoryLockProvider()),
                     receipt_persistor=InMemoryReceiptPersistor())

    s4 = result.metrics.stages["stage4_topology"]
    assert s4.expensive_ops_used == 5


def test_expensive_ops_cap_short_circuits():
    class CappedGen(TinyGenerator):
        caps = LocalityCaps(max_candidates=20, max_edges_emitted=20, max_expensive_ops=3)

    candidates = [_make_candidate(f"p-{i}", UID_SHEET, confidence=0.999) for i in range(10)]
    gen = CappedGen(candidates)
    result = gen.run(_ctx(lock_provider=InMemoryLockProvider()),
                     receipt_persistor=InMemoryReceiptPersistor())

    assert result.metrics.stages["stage4_topology"].expensive_ops_used == 3
    # After expensive budget exhausted, only first 3 edges emit
    assert result.metrics.edges_emitted == 3
    assert result.metrics.locality_violations >= 7


# ============================================================
# Invariant 9: invalid edges cannot bypass validator
# ============================================================


def test_invalid_taxonomy_edge_rejected():
    """Generator emits structurally-invalid candidate → stage 5 rejects it."""
    # dst_entity_type="primitive" is wrong for BELONGS_TO_SHEET (needs SHEET)
    bad = Candidate(
        edge_type=EdgeType.BELONGS_TO_SHEET,
        project_id=UID_PROJECT,
        sheet_id=UID_SHEET,
        src_entity_id="prim-1",
        src_entity_type="primitive",
        dst_entity_id="prim-2",
        dst_entity_type="primitive",  # invalid
        locality_scope=LocalityScope.SAME_SHEET.value,
        confidence=0.999,
        evidence={"page_index": 1},
        provenance={"sheet_id": UID_SHEET, "bbox": [0, 0, 10, 10]},
    )
    gen = TinyGenerator([bad])
    result = gen.run(_ctx(lock_provider=InMemoryLockProvider()),
                     receipt_persistor=InMemoryReceiptPersistor())

    assert result.metrics.edges_emitted == 0
    assert result.metrics.rejected_count == 1
    assert "invalid_dst_type" in result.metrics.rejected_by_reason


# ============================================================
# Subclass validation
# ============================================================


def test_subclass_missing_name_rejected():
    class Broken(EdgeGenerator):
        version = "1.0"
        edge_types = (EdgeType.BELONGS_TO_SHEET,)
        extraction_method = "x"

        def stage0_emit_candidates(self, ctx):
            return []

    with pytest.raises(ValueError, match="must set class attr `name`"):
        Broken().run(_ctx(lock_provider=InMemoryLockProvider()),
                     receipt_persistor=InMemoryReceiptPersistor())


# ============================================================
# Locality utility tests
# ============================================================


def test_query_neighbors_within_respects_radius():
    points = [(0, 0), (5, 0), (15, 0), (100, 0)]
    n = query_neighbors_within(
        query_point=(0, 0),
        candidates=points,
        point_of=lambda c: c,
        max_radius_px=10,
        max_neighbors=10,
    )
    assert len(n.neighbors) == 2  # (0,0) and (5,0)
    assert not n.trimmed


def test_query_neighbors_within_trims_to_max():
    points = [(i, 0) for i in range(20)]
    n = query_neighbors_within(
        query_point=(0, 0),
        candidates=points,
        point_of=lambda c: c,
        max_radius_px=100,
        max_neighbors=5,
    )
    assert len(n.neighbors) == 5
    assert n.trimmed is True
    assert n.candidates_before_trim == 20


def test_bbox_overlap():
    a = BBox(0, 0, 10, 10)
    b = BBox(5, 5, 15, 15)
    assert a.overlap_area(b) == 25
    assert a.overlap_ratio(b) == 0.25


# ============================================================
# Taxonomy version stability
# ============================================================


def test_taxonomy_version_deterministic_at_import():
    from .replay import compute_taxonomy_version
    # Re-computing should yield the same value
    assert compute_taxonomy_version() == TAXONOMY_VERSION
