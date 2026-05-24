"""Wave 20.6.3 — Unit tests for symbol_family_embedder.

Uses TopologyGraph from canonical_symbolizer (Stage 2 schema):
  GraphNode(node_id: int, x: float, y: float, degree: int)
  GraphEdge(from_node: int, to_node: int, primitive_type: str)
  Face(node_ids: list[int])
  SymmetryAxis(axis: str, confidence: float)
  TopologyGraph(nodes, edges, faces, symmetry_axes, graph_signature)

Tests:
  1. Embedding shape (64,), dtype float32, values in [0,1]
  2. Deterministic — same input → same embedding
  3. Same-family symbols (slight scale variation) → cosine_similarity >= 0.90
  4. Different-family cross-similarity lower than same-family similarity
  5. 1% scale perturbation → cosine_similarity >= 0.50
  6. Serialize/deserialize lossless roundtrip
  7. Backfill populates NULL rows; skips invalid bbox rows
  8. Gulfstream-like bbox → non-degenerate embedding (1% perturb sim >= 0.50)
  9. Clustering: 3 same-topology rows cluster into 1 family
 10. Clustering: 3 different-topology rows do NOT all land in one family
"""
from __future__ import annotations

import math
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from aspire_orchestrator.services.blueprint.canonical_symbolizer import (
    Face,
    GraphEdge,
    GraphNode,
    SymmetryAxis,
    TopologyGraph,
)
from aspire_orchestrator.services.blueprint.symbol_family_embedder import (
    EMBEDDING_DIM,
    cluster_legend_into_families,
    compute_embedding,
    cosine_similarity,
    deserialize_embedding,
    populate_vector_signature,
    serialize_embedding,
)

_SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
_PROJECT = "7a93d144-5d53-47d3-8b97-067ebe249d88"


# ─────────────────────────────────────────────────────────────────────────────
# Canonical symbol fixtures using Stage 2 schema
# ─────────────────────────────────────────────────────────────────────────────


def _circle_topo(r_scale: float = 1.0) -> TopologyGraph:
    """Arc-dominant circle (sprinkler pendant). 4 arc edges forming a ring."""
    nodes = [
        GraphNode(
            node_id=i,
            x=round(math.cos(i * math.pi / 2) * 0.4 * r_scale + 0.5, 4),
            y=round(math.sin(i * math.pi / 2) * 0.4 * r_scale + 0.5, 4),
            degree=2,
        )
        for i in range(4)
    ]
    edges = [GraphEdge(from_node=i, to_node=(i + 1) % 4, primitive_type="arc") for i in range(4)]
    return TopologyGraph(
        nodes=nodes,
        edges=edges,
        faces=[Face(node_ids=[0, 1, 2, 3])],
        symmetry_axes=[
            SymmetryAxis(axis="vertical", confidence=0.95),
            SymmetryAxis(axis="horizontal", confidence=0.95),
            SymmetryAxis(axis="diagonal_pos", confidence=0.85),
            SymmetryAxis(axis="diagonal_neg", confidence=0.85),
        ],
        graph_signature=f"circle_{round(r_scale, 3)}",
    )


def _rectangle_topo(aspect: float = 2.0) -> TopologyGraph:
    """Line-dominant rectangle (electrical panel box). aspect = w/h."""
    hw = min(aspect / (2 * (1 + aspect)), 0.48)
    hh = min(1.0 / (2 * (1 + aspect)), 0.48)
    nodes = [
        GraphNode(node_id=0, x=0.5 - hw, y=0.5 - hh, degree=2),
        GraphNode(node_id=1, x=0.5 + hw, y=0.5 - hh, degree=2),
        GraphNode(node_id=2, x=0.5 + hw, y=0.5 + hh, degree=2),
        GraphNode(node_id=3, x=0.5 - hw, y=0.5 + hh, degree=2),
    ]
    edges = [
        GraphEdge(from_node=0, to_node=1, primitive_type="line"),
        GraphEdge(from_node=1, to_node=2, primitive_type="line"),
        GraphEdge(from_node=2, to_node=3, primitive_type="line"),
        GraphEdge(from_node=3, to_node=0, primitive_type="line"),
    ]
    return TopologyGraph(
        nodes=nodes,
        edges=edges,
        faces=[Face(node_ids=[0, 1, 2, 3])],
        symmetry_axes=[
            SymmetryAxis(axis="vertical", confidence=0.9),
            SymmetryAxis(axis="horizontal", confidence=0.7),
        ],
        graph_signature=f"rect_{round(aspect, 2)}",
    )


def _star_topo() -> TopologyGraph:
    """Line-dominant star/asterisk. 4 rays from center, no faces."""
    cx, cy, r = 0.5, 0.5, 0.4
    nodes = [GraphNode(node_id=0, x=cx, y=cy, degree=4)]
    for i in range(4):
        a = i * math.pi / 4
        nodes.append(GraphNode(node_id=i + 1, x=round(cx + r * math.cos(a), 4),
                               y=round(cy + r * math.sin(a), 4), degree=1))
    edges = [GraphEdge(from_node=0, to_node=i + 1, primitive_type="line") for i in range(4)]
    return TopologyGraph(
        nodes=nodes, edges=edges, faces=[],
        symmetry_axes=[
            SymmetryAxis(axis="vertical", confidence=0.9),
            SymmetryAxis(axis="horizontal", confidence=0.9),
            SymmetryAxis(axis="diagonal_pos", confidence=0.9),
            SymmetryAxis(axis="diagonal_neg", confidence=0.9),
        ],
        graph_signature="star_4ray",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Shape, dtype, range
# ─────────────────────────────────────────────────────────────────────────────

class TestEmbeddingBasics:
    def test_shape_dtype_range(self) -> None:
        emb = compute_embedding(_circle_topo())
        assert emb.shape == (EMBEDDING_DIM,)
        assert emb.dtype == np.float32
        assert float(emb.min()) >= 0.0
        assert float(emb.max()) <= 1.0

    def test_empty_graph(self) -> None:
        topo = TopologyGraph(nodes=[], edges=[], faces=[], symmetry_axes=[], graph_signature="empty")
        emb = compute_embedding(topo)
        assert emb.shape == (EMBEDDING_DIM,)
        assert emb.dtype == np.float32


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Deterministic
# ─────────────────────────────────────────────────────────────────────────────

class TestDeterminism:
    def test_identical_on_repeat(self) -> None:
        topo = _circle_topo()
        np.testing.assert_array_equal(compute_embedding(topo), compute_embedding(topo))

    def test_rectangle_deterministic(self) -> None:
        topo = _rectangle_topo()
        np.testing.assert_array_equal(compute_embedding(topo), compute_embedding(topo))


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Same-family similarity >= 0.90
# ─────────────────────────────────────────────────────────────────────────────

class TestSameFamilySimilarity:
    def test_circle_scale_variants(self) -> None:
        emb_a = compute_embedding(_circle_topo(1.00))
        emb_b = compute_embedding(_circle_topo(1.02))
        emb_c = compute_embedding(_circle_topo(0.98))
        assert cosine_similarity(emb_a, emb_b) >= 0.90
        assert cosine_similarity(emb_a, emb_c) >= 0.90

    def test_rectangle_aspect_variants(self) -> None:
        emb_a = compute_embedding(_rectangle_topo(2.0))
        emb_b = compute_embedding(_rectangle_topo(2.05))
        assert cosine_similarity(emb_a, emb_b) >= 0.90


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Same-family > cross-family similarity
# ─────────────────────────────────────────────────────────────────────────────

class TestDifferentFamilySimilarity:
    def test_same_family_beats_cross_family(self) -> None:
        emb_circle = compute_embedding(_circle_topo(1.0))
        emb_circle2 = compute_embedding(_circle_topo(1.02))
        emb_rect = compute_embedding(_rectangle_topo(aspect=3.0))

        sim_same = cosine_similarity(emb_circle, emb_circle2)
        sim_cross = cosine_similarity(emb_circle, emb_rect)
        assert sim_same > sim_cross, (
            f"Same-family ({sim_same:.4f}) should exceed cross-family ({sim_cross:.4f})"
        )

    def test_star_vs_circle_same_family_beats_cross(self) -> None:
        emb_circle = compute_embedding(_circle_topo(1.0))
        emb_circle2 = compute_embedding(_circle_topo(1.01))
        emb_star = compute_embedding(_star_topo())
        assert cosine_similarity(emb_circle, emb_circle2) >= cosine_similarity(emb_circle, emb_star)


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Perturbation invariance
# ─────────────────────────────────────────────────────────────────────────────

class TestPerturbationInvariance:
    def test_1pct_scale_sim_above_threshold(self) -> None:
        topo = _rectangle_topo(2.0)
        perturbed = TopologyGraph(
            nodes=[GraphNode(n.node_id, n.x * 1.01, n.y * 1.01, n.degree) for n in topo.nodes],
            edges=list(topo.edges),
            faces=list(topo.faces),
            symmetry_axes=list(topo.symmetry_axes),
            graph_signature=topo.graph_signature + "_p",
        )
        sim = cosine_similarity(compute_embedding(topo), compute_embedding(perturbed))
        assert sim >= 0.50, f"1% scale perturb sim={sim:.4f} < 0.50"

    def test_translation_robustness(self) -> None:
        topo = _circle_topo()
        shifted = TopologyGraph(
            nodes=[GraphNode(n.node_id, n.x + 5.0, n.y + 5.0, n.degree) for n in topo.nodes],
            edges=list(topo.edges),
            faces=list(topo.faces),
            symmetry_axes=list(topo.symmetry_axes),
            graph_signature=topo.graph_signature + "_t",
        )
        sim = cosine_similarity(compute_embedding(topo), compute_embedding(shifted))
        assert sim >= 0.50, f"Translation sim={sim:.4f} < 0.50"


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Serialization roundtrip
# ─────────────────────────────────────────────────────────────────────────────

class TestSerialization:
    def test_roundtrip_lossless(self) -> None:
        emb = compute_embedding(_circle_topo())
        decoded = deserialize_embedding(serialize_embedding(emb))
        assert decoded.shape == (EMBEDDING_DIM,)
        assert decoded.dtype == np.float32
        np.testing.assert_array_almost_equal(emb, decoded, decimal=6)

    def test_distinct_symbols_distinct_encodings(self) -> None:
        enc_circle = serialize_embedding(compute_embedding(_circle_topo()))
        enc_rect = serialize_embedding(compute_embedding(_rectangle_topo()))
        assert enc_circle != enc_rect


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: Backfill
# ─────────────────────────────────────────────────────────────────────────────

class TestBackfill:
    @pytest.mark.asyncio
    async def test_populates_null_rows(self) -> None:
        row = {"id": "r1", "suite_id": _SUITE, "project_id": _PROJECT,
               "vector_signature": None, "representation_bbox": [40.0, 40.0, 60.0, 50.0]}
        updates: list[dict] = []

        with (
            patch("aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_select",
                  new=AsyncMock(return_value=[row])),
            patch("aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_update",
                  new=AsyncMock(side_effect=lambda tbl, flt, p: updates.append(p))),
        ):
            result = await populate_vector_signature(project_id=_PROJECT, suite_id=_SUITE)

        assert result["rows_backfilled"] == 1
        assert result["errors"] == 0
        assert len(updates) == 1
        assert deserialize_embedding(updates[0]["vector_signature"]).shape == (EMBEDDING_DIM,)

    @pytest.mark.asyncio
    async def test_skips_null_bbox(self) -> None:
        row = {"id": "r2", "suite_id": _SUITE, "project_id": _PROJECT,
               "vector_signature": None, "representation_bbox": None}
        updates: list = []

        with (
            patch("aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_select",
                  new=AsyncMock(return_value=[row])),
            patch("aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_update",
                  new=AsyncMock(side_effect=lambda *a, **kw: updates.append(a))),
        ):
            result = await populate_vector_signature(project_id=_PROJECT, suite_id=_SUITE)

        assert result["rows_backfilled"] == 0
        assert result["rows_skipped"] == 1
        assert updates == []


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: Non-degenerate Gulfstream-like bbox embedding
# ─────────────────────────────────────────────────────────────────────────────

class TestNonDegenerate:
    def test_gulfstream_bbox_nondegen(self) -> None:
        from aspire_orchestrator.services.blueprint.symbol_family_embedder import _build_topology_from_bbox

        topo = _build_topology_from_bbox([612.3, 128.7, 650.0, 160.5])
        assert topo is not None
        emb = compute_embedding(topo)
        assert float(emb.max()) > 0.0

        topo_pert = _build_topology_from_bbox([612.3 * 1.01, 128.7 * 1.01, 650.0 * 1.01, 160.5 * 1.01])
        assert topo_pert is not None
        sim = cosine_similarity(emb, compute_embedding(topo_pert))
        assert sim >= 0.50, f"Gulfstream 1%-perturb sim={sim:.4f} < 0.50"


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: Clustering — same topology → 1 family
# ─────────────────────────────────────────────────────────────────────────────

class TestClustering:
    @pytest.mark.asyncio
    async def test_same_topology_one_family(self) -> None:
        rows = [
            {"id": f"row-{i}", "suite_id": _SUITE, "project_id": _PROJECT,
             "discipline": "fire_protection",
             "vector_signature": serialize_embedding(compute_embedding(_circle_topo(s)))}
            for i, s in enumerate([1.00, 1.01, 0.99])
        ]
        updates: list = []

        with (
            patch("aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_select",
                  new=AsyncMock(return_value=rows)),
            patch("aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_update",
                  new=AsyncMock(side_effect=lambda *a, **kw: updates.append(a))),
        ):
            families = await cluster_legend_into_families(
                project_id=_PROJECT, suite_id=_SUITE, similarity_threshold=0.90,
            )

        assert len(families) == 1
        assert len(list(families.values())[0]) == 3

    @pytest.mark.asyncio
    async def test_different_topology_no_cross_cluster(self) -> None:
        rows = [
            {"id": "row-circle", "suite_id": _SUITE, "project_id": _PROJECT, "discipline": "fire_protection",
             "vector_signature": serialize_embedding(compute_embedding(_circle_topo()))},
            {"id": "row-rect", "suite_id": _SUITE, "project_id": _PROJECT, "discipline": "fire_protection",
             "vector_signature": serialize_embedding(compute_embedding(_rectangle_topo(aspect=3.0)))},
            {"id": "row-star", "suite_id": _SUITE, "project_id": _PROJECT, "discipline": "fire_protection",
             "vector_signature": serialize_embedding(compute_embedding(_star_topo()))},
        ]
        updates: list = []

        with (
            patch("aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_select",
                  new=AsyncMock(return_value=rows)),
            patch("aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_update",
                  new=AsyncMock(side_effect=lambda *a, **kw: updates.append(a))),
        ):
            families = await cluster_legend_into_families(
                project_id=_PROJECT, suite_id=_SUITE, similarity_threshold=0.92,
            )

        # No family should contain all 3 different-topology symbols.
        for fid, members in families.items():
            ms = set(members)
            assert not (
                "row-circle" in ms and "row-rect" in ms and "row-star" in ms
            ), f"All 3 different-topology symbols wrongly in family {fid}"
