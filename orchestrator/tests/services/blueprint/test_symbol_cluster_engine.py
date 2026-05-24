"""Wave 20.6 Stage 5 — Unit tests for symbol_cluster_engine.

Tests:
  1. clustering: 5 visually-similar motifs cluster into 1 group
  2. clustering: 5 visually-different motifs cluster into 5 groups
  3. propagation: cluster with high-similarity legend match → all motifs bound
  4. propagation: cluster with low similarity → all motifs unknown
  5. propagation: cluster with mid similarity → flagged pending_vlm_verify
  6. cross-tenant: cluster propagation rejects cross-tenant suite_id (Law #6 evil test)

Pure unit tests — no real Supabase. All DB calls patched via module-level proxies.
"""
from __future__ import annotations

import math
import struct
import uuid
from unittest.mock import AsyncMock, patch

import pytest

import aspire_orchestrator.services.blueprint.symbol_cluster_engine as sce
from aspire_orchestrator.services.blueprint.symbol_cluster_engine import (
    _MotifRow,
    _cosine_similarity,
    _single_linkage_clusters,
    cluster_motifs_on_project,
    propagate_legend_bindings_to_clusters,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
_SUITE_OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_PROJECT = "7a93d144-5d53-47d3-8b97-067ebe249d88"
_SHEET = "11111111-0000-0000-0000-000000000001"
_LEGEND_ID = "22222222-0000-0000-0000-000000000002"

_DIM = 32


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unit_vec(direction: list[float]) -> list[float]:
    """Return a unit vector from raw direction list."""
    norm = math.sqrt(sum(x * x for x in direction))
    return [x / norm for x in direction]


def _perturbed_vec(base: list[float], scale: float = 0.005) -> list[float]:
    """Return a slightly perturbed version of base that stays cosine-close."""
    raw = [v + scale * (i % 3 - 1) for i, v in enumerate(base)]
    return _unit_vec(raw)


def _orthonormal_basis(n: int, dim: int) -> list[list[float]]:
    """Return n orthonormal vectors of length dim (standard basis e_0..e_{n-1})."""
    assert n <= dim
    result = []
    for i in range(n):
        v = [0.0] * dim
        v[i] = 1.0
        result.append(v)
    return result


def _encode(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _make_motif_row(
    row_id: str,
    *,
    embedding: list[float] | None = None,
    suite_id: str = _SUITE,
    sheet_id: str = _SHEET,
    project_id: str = _PROJECT,
) -> _MotifRow:
    return _MotifRow(
        id=row_id,
        suite_id=suite_id,
        sheet_id=sheet_id,
        project_id=project_id,
        motif_id=row_id[:12],
        graph_hash="abcdef" + row_id[:6],
        embedding=embedding,
        bbox={"motif_id": row_id[:12], "graph_hash": "abcdef" + row_id[:6]},
        motif_frequency=5,
        discipline="electrical",
        sheet_region="plan",
    )


def _make_symbol_row(
    row_id: str,
    *,
    cluster_id: str,
    embedding_vec: list[float] | None = None,
    suite_id: str = _SUITE,
    sheet_id: str = _SHEET,
    project_id: str = _PROJECT,
) -> dict:
    return {
        "id": row_id,
        "suite_id": suite_id,
        "sheet_id": sheet_id,
        "project_id": project_id,
        "cluster_id": cluster_id,
        "class": f"unresolved_{row_id[:8]}",
        "bbox": {"motif_id": row_id[:12], "graph_hash": "abc"},
        "embedding": _encode(embedding_vec) if embedding_vec else None,
    }


def _make_legend_row(
    legend_id: str,
    *,
    label: str = "outlet",
    vec: list[float],
    suite_id: str = _SUITE,
    project_id: str = _PROJECT,
) -> dict:
    return {
        "id": legend_id,
        "suite_id": suite_id,
        "project_id": project_id,
        "label": label,
        "vector_signature": _encode(vec),
    }


# ---------------------------------------------------------------------------
# Test 1: 5 similar motifs → 1 cluster
# ---------------------------------------------------------------------------

def test_similar_motifs_form_one_cluster() -> None:
    """5 motifs with nearly identical embeddings should cluster into 1 group."""
    base = _unit_vec([1.0] + [0.0] * (_DIM - 1))
    rows = [
        _make_motif_row(str(uuid.uuid4()), embedding=_perturbed_vec(base, scale=0.002))
        for _ in range(5)
    ]

    clusters = _single_linkage_clusters(rows, threshold=0.92)

    assert len(clusters) == 1, f"Expected 1 cluster, got {len(clusters)}"
    all_ids = [mid for mids in clusters.values() for mid in mids]
    assert len(all_ids) == 5


# ---------------------------------------------------------------------------
# Test 2: 5 orthogonal motifs → 5 clusters
# ---------------------------------------------------------------------------

def test_different_motifs_form_separate_clusters() -> None:
    """5 motifs with strictly orthogonal embeddings must form 5 distinct clusters."""
    # Use standard basis vectors — cosine similarity between any two = 0.0
    bases = _orthonormal_basis(5, _DIM)
    rows = [
        _make_motif_row(str(uuid.uuid4()), embedding=b)
        for b in bases
    ]

    # Verify the test's own precondition: max pairwise sim is 0
    for i in range(5):
        for j in range(i + 1, 5):
            sim = _cosine_similarity(rows[i].embedding, rows[j].embedding)  # type: ignore[arg-type]
            assert sim < 0.1, f"Vectors not orthogonal: sim({i},{j})={sim:.3f}"

    clusters = _single_linkage_clusters(rows, threshold=0.92)

    assert len(clusters) == 5, f"Expected 5 clusters, got {len(clusters)}: {list(clusters.keys())}"


# ---------------------------------------------------------------------------
# Test 3: high-similarity legend match → all motifs bound (cluster_propagation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propagation_high_similarity_binds_all_motifs() -> None:
    """Cluster centroid similarity ≥ 0.92 to a legend → all instances auto-bound."""
    vec = _unit_vec([1.0] + [0.0] * (_DIM - 1))
    cluster_id = "cls_testabc123456789"

    symbol_rows = [
        _make_symbol_row(str(uuid.uuid4()), cluster_id=cluster_id, embedding_vec=vec)
        for _ in range(4)
    ]
    legend_rows = [_make_legend_row(_LEGEND_ID, vec=vec)]

    async def _mock_select(table: str, filters: str, **_kw: object) -> list[dict]:
        if table == "blueprint_symbols":
            return symbol_rows
        return []

    with (
        patch.object(sce, "_db_select", new=AsyncMock(side_effect=_mock_select)),
        patch.object(sce, "_fetch_legend_entries", new=AsyncMock(return_value=legend_rows)),
        patch.object(sce, "_write_resolved_symbols", new=AsyncMock(return_value=4)),
    ):
        result = await propagate_legend_bindings_to_clusters(
            project_id=_PROJECT,
            suite_id=_SUITE,
            bind_threshold=0.92,
            verify_threshold=0.75,
        )

    assert result["clusters_auto_bound"] == 1
    assert result["clusters_flagged_for_vlm"] == 0
    assert result["clusters_unknown"] == 0
    assert result["total_motifs_resolved"] == 4


# ---------------------------------------------------------------------------
# Test 4: low-similarity → all motifs unknown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propagation_low_similarity_leaves_unknown() -> None:
    """Cluster centroid similarity < 0.75 to any legend → cluster stays unknown."""
    # e1 vs e2: cosine similarity = 0.0
    motif_vec = _unit_vec([1.0] + [0.0] * (_DIM - 1))
    legend_vec = _unit_vec([0.0, 1.0] + [0.0] * (_DIM - 2))

    sim = _cosine_similarity(motif_vec, legend_vec)
    assert sim < 0.75, f"Test setup error: sim={sim:.3f} not < 0.75"

    cluster_id = "cls_low_sim_test_id_"
    symbol_rows = [
        _make_symbol_row(str(uuid.uuid4()), cluster_id=cluster_id, embedding_vec=motif_vec)
        for _ in range(3)
    ]
    legend_rows = [_make_legend_row(_LEGEND_ID, vec=legend_vec)]

    async def _mock_select(table: str, filters: str, **_kw: object) -> list[dict]:
        return symbol_rows

    with (
        patch.object(sce, "_db_select", new=AsyncMock(side_effect=_mock_select)),
        patch.object(sce, "_fetch_legend_entries", new=AsyncMock(return_value=legend_rows)),
        patch.object(sce, "_write_resolved_symbols", new=AsyncMock(return_value=3)),
    ):
        result = await propagate_legend_bindings_to_clusters(
            project_id=_PROJECT,
            suite_id=_SUITE,
            bind_threshold=0.92,
            verify_threshold=0.75,
        )

    assert result["clusters_auto_bound"] == 0
    assert result["clusters_flagged_for_vlm"] == 0
    assert result["clusters_unknown"] == 1


# ---------------------------------------------------------------------------
# Test 5: mid-similarity → flagged pending_vlm_verify
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propagation_mid_similarity_flags_for_vlm() -> None:
    """0.75 ≤ similarity < 0.92 → cluster flagged pending_vlm_verify, not bound."""
    dim = _DIM
    # cos(35°) ≈ 0.819 — squarely between thresholds
    angle = math.radians(35)
    motif_vec = [1.0] + [0.0] * (dim - 1)
    legend_vec = [math.cos(angle), math.sin(angle)] + [0.0] * (dim - 2)

    sim = _cosine_similarity(motif_vec, legend_vec)
    assert 0.75 <= sim < 0.92, f"Test setup error: sim={sim:.3f} not in [0.75, 0.92)"

    cluster_id = "cls_mid_sim_test_id_"
    symbol_rows = [
        _make_symbol_row(str(uuid.uuid4()), cluster_id=cluster_id, embedding_vec=motif_vec)
        for _ in range(2)
    ]
    legend_rows = [_make_legend_row(_LEGEND_ID, vec=legend_vec)]

    async def _mock_select(table: str, filters: str, **_kw: object) -> list[dict]:
        return symbol_rows

    with (
        patch.object(sce, "_db_select", new=AsyncMock(side_effect=_mock_select)),
        patch.object(sce, "_fetch_legend_entries", new=AsyncMock(return_value=legend_rows)),
        patch.object(sce, "_write_resolved_symbols", new=AsyncMock(return_value=2)),
    ):
        result = await propagate_legend_bindings_to_clusters(
            project_id=_PROJECT,
            suite_id=_SUITE,
            bind_threshold=0.92,
            verify_threshold=0.75,
        )

    assert result["clusters_auto_bound"] == 0
    assert result["clusters_flagged_for_vlm"] == 1
    assert result["clusters_unknown"] == 0


# ---------------------------------------------------------------------------
# Test 6: cross-tenant evil test — suite_id isolation (Law #6)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cross_tenant_isolation_evil_test() -> None:
    """RLS returns 0 rows for wrong suite_id → propagation produces all-zero stats.

    The engine passes suite_id into every DB filter. PostgREST + RLS will return
    empty for a different tenant's data. This test simulates that: when an attacker
    passes _SUITE_OTHER, the DB returns 0 rows → no resolved symbols written.
    """
    async def _mock_select_empty(table: str, filters: str, **_kw: object) -> list[dict]:
        # Simulate RLS returning empty for the wrong tenant
        assert _SUITE_OTHER in filters, "suite_id must be present in every query filter"
        return []

    with (
        patch.object(sce, "_db_select", new=AsyncMock(side_effect=_mock_select_empty)),
        patch.object(sce, "_fetch_legend_entries", new=AsyncMock(return_value=[])),
        patch.object(sce, "_write_resolved_symbols", new=AsyncMock(return_value=0)),
    ):
        result = await propagate_legend_bindings_to_clusters(
            project_id=_PROJECT,
            suite_id=_SUITE_OTHER,
            bind_threshold=0.92,
            verify_threshold=0.75,
        )

    assert result["clusters_auto_bound"] == 0
    assert result["clusters_flagged_for_vlm"] == 0
    assert result["clusters_unknown"] == 0
    assert result["total_motifs_resolved"] == 0
