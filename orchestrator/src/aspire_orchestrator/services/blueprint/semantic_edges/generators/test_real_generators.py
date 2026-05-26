"""L-4 acceptance tests — real generators run through the L-3 framework.

Unit tests use InMemoryQueryClient. The optional integration test at the
bottom runs against the real Supabase project when ASPIRE_RUN_LIVE_DB=1
is set (skipped by default in CI).
"""
from __future__ import annotations

import os
from typing import Optional

import pytest

from .. import (
    EdgeType,
    GeneratorContext,
    GeneratorHealth,
    InMemoryLockProvider,
    InMemoryReceiptPersistor,
)
from ..taxonomy import LocalityScope
from .belongs_to_sheet import BelongsToSheetGenerator
from .data_access import (
    CompositionRecord,
    EntitiesQueryClient,
    EntityRecord,
    InMemoryQueryClient,
)
from .member_of_family import MemberOfFamilyGenerator


# ============================================================
# Fixture helpers
# ============================================================

UID_SUITE = "00000000-0000-0000-0000-000000000001"
UID_PROJECT = "7a93d144-5d53-47d3-8b97-067ebe249d88"  # the real GAVNN project
UID_SHEET_A = "00000000-0000-0000-0000-00000000aaaa"
UID_SHEET_B = "00000000-0000-0000-0000-00000000bbbb"


def _composed(eid: str, sheet_id: str) -> EntityRecord:
    return EntityRecord(
        entity_id=eid,
        entity_type="composed_symbol",
        project_id=UID_PROJECT,
        suite_id=UID_SUITE,
        sheet_id=sheet_id,
        family_id="fam_e17879f0",
        page_index=1,
    )


def _legend(eid: str, sheet_id: Optional[str], *, label: str = "QR Pendant") -> EntityRecord:
    return EntityRecord(
        entity_id=eid,
        entity_type="legend",
        project_id=UID_PROJECT,
        suite_id=UID_SUITE,
        sheet_id=sheet_id,
        family_id="fam_legend",
        label=label,
        page_index=1,
    )


def _composition(composed_id: str, sheet_id: str, constituent_ids: list[str]) -> CompositionRecord:
    return CompositionRecord(
        composed_symbol_id=composed_id,
        sheet_id=sheet_id,
        project_id=UID_PROJECT,
        suite_id=UID_SUITE,
        constituent_ids=constituent_ids,
    )


def _ctx() -> GeneratorContext:
    return GeneratorContext(
        suite_id=UID_SUITE,
        project_id=UID_PROJECT,
        pipeline_version="L4_1.0",
        lock_provider=InMemoryLockProvider(),
    )


# ============================================================
# BelongsToSheetGenerator unit tests
# ============================================================


def test_belongs_to_sheet_emits_one_edge_per_composed_symbol():
    client = InMemoryQueryClient(
        composed_symbols=[
            _composed("cs-1", UID_SHEET_A),
            _composed("cs-2", UID_SHEET_A),
            _composed("cs-3", UID_SHEET_B),
        ],
    )
    gen = BelongsToSheetGenerator(client)
    persistor = InMemoryReceiptPersistor()
    result = gen.run(_ctx(), receipt_persistor=persistor)

    assert result.metrics.edges_emitted == 3
    assert result.metrics.candidates_considered == 3
    assert result.metrics.health == GeneratorHealth.GREEN
    assert not result.aborted

    # Every edge points at a sheet
    for edge in result.accepted_edges:
        assert edge.edge_type == EdgeType.BELONGS_TO_SHEET
        assert edge.dst_entity_type.value == "sheet"
        assert edge.locality_scope == LocalityScope.SAME_SHEET
        assert edge.confidence == 0.999
        assert edge.evidence == {"page_index": 1}


def test_belongs_to_sheet_includes_legends():
    client = InMemoryQueryClient(
        composed_symbols=[_composed("cs-1", UID_SHEET_A)],
        legends=[
            _legend("leg-1", UID_SHEET_A),
            _legend("leg-2", UID_SHEET_B),
            _legend("leg-3", UID_SHEET_A),
        ],
    )
    gen = BelongsToSheetGenerator(client)
    result = gen.run(_ctx(), receipt_persistor=InMemoryReceiptPersistor())

    assert result.metrics.edges_emitted == 4
    # 1 composed + 3 legends


def test_belongs_to_sheet_skips_entities_with_null_sheet_id():
    """Legends with NULL source_sheet_id (some Wave 20.6 rows) are skipped."""
    client = InMemoryQueryClient(
        legends=[
            _legend("leg-1", UID_SHEET_A),
            _legend("leg-2", None),     # NULL sheet_id — skipped
        ],
    )
    gen = BelongsToSheetGenerator(client)
    result = gen.run(_ctx(), receipt_persistor=InMemoryReceiptPersistor())

    assert result.metrics.edges_emitted == 1


def test_belongs_to_sheet_receipt_records_identity():
    client = InMemoryQueryClient(composed_symbols=[_composed("cs-1", UID_SHEET_A)])
    gen = BelongsToSheetGenerator(client)
    persistor = InMemoryReceiptPersistor()
    gen.run(_ctx(), receipt_persistor=persistor)

    assert len(persistor.receipts) == 1
    r = persistor.receipts[0]
    assert r.generator_name == "belongs_to_sheet"
    assert r.generator_version == "1.0"
    assert r.extraction_method == "belongs_to_sheet_v1"
    assert r.edges_emitted == 1
    assert r.edges_by_type == {"belongs_to_sheet": 1}
    assert r.edges_by_locality == {"same_sheet": 1}


def test_belongs_to_sheet_provenance_includes_required_keys():
    client = InMemoryQueryClient(composed_symbols=[_composed("cs-1", UID_SHEET_A)])
    gen = BelongsToSheetGenerator(client)
    result = gen.run(_ctx(), receipt_persistor=InMemoryReceiptPersistor())

    edge = result.accepted_edges[0]
    # Framework injects extractor/wave/pipeline_version/generator_window
    # generator supplies sheet_id + bbox
    required = {"extractor", "wave", "pipeline_version", "sheet_id",
                "generator_window", "bbox"}
    assert required <= set(edge.provenance.keys())
    assert edge.provenance["extractor"] == "belongs_to_sheet"
    assert edge.provenance["source_table"] == "blueprint_composed_symbols"


# ============================================================
# MemberOfFamilyGenerator unit tests
# ============================================================


def test_member_of_family_expands_constituent_array():
    """One composed symbol with 5 constituents → 5 member_of_family edges."""
    client = InMemoryQueryClient(
        compositions=[
            _composition("cs-1", UID_SHEET_A,
                         ["p-001", "p-002", "p-003", "p-004", "p-005"]),
        ],
    )
    gen = MemberOfFamilyGenerator(client)
    result = gen.run(_ctx(), receipt_persistor=InMemoryReceiptPersistor())

    assert result.metrics.edges_emitted == 5
    assert result.metrics.health == GeneratorHealth.GREEN
    for edge in result.accepted_edges:
        assert edge.edge_type == EdgeType.MEMBER_OF_FAMILY
        assert edge.dst_entity_id == "cs-1"
        assert edge.src_entity_type.value == "primitive"
        assert edge.dst_entity_type.value == "composed_symbol"
        assert edge.locality_scope == LocalityScope.SAME_SHEET
        assert edge.confidence == 0.999


def test_member_of_family_handles_empty_compositions():
    client = InMemoryQueryClient(
        compositions=[
            _composition("cs-1", UID_SHEET_A, []),    # 0 constituents
            _composition("cs-2", UID_SHEET_A, ["p-a"]),
        ],
    )
    gen = MemberOfFamilyGenerator(client)
    result = gen.run(_ctx(), receipt_persistor=InMemoryReceiptPersistor())

    assert result.metrics.edges_emitted == 1


def test_member_of_family_emits_one_edge_per_constituent_pair():
    client = InMemoryQueryClient(
        compositions=[
            _composition("cs-1", UID_SHEET_A, ["p-1", "p-2"]),
            _composition("cs-2", UID_SHEET_B, ["p-3", "p-4", "p-5"]),
        ],
    )
    gen = MemberOfFamilyGenerator(client)
    result = gen.run(_ctx(), receipt_persistor=InMemoryReceiptPersistor())

    assert result.metrics.edges_emitted == 5
    src_dst_pairs = {(e.src_entity_id, e.dst_entity_id) for e in result.accepted_edges}
    assert src_dst_pairs == {
        ("p-1", "cs-1"), ("p-2", "cs-1"),
        ("p-3", "cs-2"), ("p-4", "cs-2"), ("p-5", "cs-2"),
    }


def test_member_of_family_evidence_contains_family_id():
    client = InMemoryQueryClient(
        compositions=[_composition("cs-99", UID_SHEET_A, ["p-x"])],
    )
    gen = MemberOfFamilyGenerator(client)
    result = gen.run(_ctx(), receipt_persistor=InMemoryReceiptPersistor())

    edge = result.accepted_edges[0]
    assert edge.evidence["family_id"] == "cs-99"
    assert edge.evidence["similarity"] == 1.0
    assert edge.provenance["composition_size"] == 1


# ============================================================
# Framework invariants verified through real generators
# ============================================================


def test_real_generators_are_deterministic_across_reruns():
    """Same input → same output_edge_hash for both generators."""
    composed = [_composed(f"cs-{i:02d}", UID_SHEET_A) for i in range(5)]
    compositions = [
        _composition(f"cs-{i:02d}", UID_SHEET_A, [f"p-{i:02d}-{j}" for j in range(3)])
        for i in range(5)
    ]
    client_a = InMemoryQueryClient(composed_symbols=composed, compositions=compositions)
    client_b = InMemoryQueryClient(
        composed_symbols=list(reversed(composed)),
        compositions=list(reversed(compositions)),
    )

    bts_a = BelongsToSheetGenerator(client_a).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    bts_b = BelongsToSheetGenerator(client_b).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    mof_a = MemberOfFamilyGenerator(client_a).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )
    mof_b = MemberOfFamilyGenerator(client_b).run(
        _ctx(), receipt_persistor=InMemoryReceiptPersistor()
    )

    assert bts_a.metrics.output_edge_hash == bts_b.metrics.output_edge_hash
    assert mof_a.metrics.output_edge_hash == mof_b.metrics.output_edge_hash


def test_real_generators_emit_receipt_even_with_zero_edges():
    """Zero data still produces a (zero-edge) receipt."""
    client = InMemoryQueryClient()
    persistor = InMemoryReceiptPersistor()
    BelongsToSheetGenerator(client).run(_ctx(), receipt_persistor=persistor)
    MemberOfFamilyGenerator(client).run(_ctx(), receipt_persistor=persistor)

    assert len(persistor.receipts) == 2
    for r in persistor.receipts:
        assert r.edges_emitted == 0
        assert r.candidates_considered == 0


# ============================================================
# Integration test (skipped by default)
# ============================================================


@pytest.mark.skipif(
    os.getenv("ASPIRE_RUN_LIVE_DB") != "1",
    reason="Set ASPIRE_RUN_LIVE_DB=1 to run against real Supabase",
)
def test_belongs_to_sheet_runs_on_real_gavnn_project():
    """Smoke test against GAVNN project in production Supabase.

    Expected: 8 composed_symbols + 288 legends = 296 candidate edges.
    Edges where legend.source_sheet_id is NULL are skipped — so actual
    emit count may be 200–296 depending on legend completeness.
    """
    from .data_access import SupabaseQueryClient

    client = SupabaseQueryClient()
    ctx = GeneratorContext(
        suite_id="<from-real-data>",     # tester supplies the real suite_id
        project_id=UID_PROJECT,
        pipeline_version="L4_smoke",
        lock_provider=InMemoryLockProvider(),
    )
    result = BelongsToSheetGenerator(client).run(
        ctx, receipt_persistor=InMemoryReceiptPersistor()
    )
    assert result.metrics.edges_emitted >= 100
    assert result.metrics.health == GeneratorHealth.GREEN


@pytest.mark.skipif(
    os.getenv("ASPIRE_RUN_LIVE_DB") != "1",
    reason="Set ASPIRE_RUN_LIVE_DB=1 to run against real Supabase",
)
def test_member_of_family_runs_on_real_gavnn_project():
    """8 composed_symbols × avg 5.25 constituents = ~42 expected edges."""
    from .data_access import SupabaseQueryClient

    client = SupabaseQueryClient()
    ctx = GeneratorContext(
        suite_id="<from-real-data>",
        project_id=UID_PROJECT,
        pipeline_version="L4_smoke",
        lock_provider=InMemoryLockProvider(),
    )
    result = MemberOfFamilyGenerator(client).run(
        ctx, receipt_persistor=InMemoryReceiptPersistor()
    )
    assert 30 <= result.metrics.edges_emitted <= 80
