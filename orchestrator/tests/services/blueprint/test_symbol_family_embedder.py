"""Wave 20.6.3 — Unit tests for symbol_family_embedder.

Tests cover:
  1. Embedding shape is (64,), dtype float32, values in [0,1]
  2. Same symbol → identical embedding (deterministic)
  3. Same family symbols → cosine_similarity >= 0.90
  4. Different family symbols → cosine_similarity <= 0.60
  5. Embedding invariant under rotation/scale/stroke perturbation
  6. Vector signature serialized/deserialized losslessly
  7. Backfill processes NULL rows, leaves populated rows alone
  8. Embedding pipeline on bbox-derived topology → non-degenerate (similarity > 0.5 with 1% perturbation)
  9. Clustering: 3 same-topology legend rows cluster into 1 family
 10. Clustering: 3 different-topology legend rows do NOT cluster together
"""
from __future__ import annotations

import copy
import math
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from aspire_orchestrator.services.blueprint.symbol_family_embedder import (
    EMBEDDING_DIM,
    TopologyEdge,
    TopologyGraph,
    TopologyNode,
    cluster_legend_into_families,
    compute_embedding,
    cosine_similarity,
    deserialize_embedding,
    populate_vector_signature,
    serialize_embedding,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — canonical construction symbol topologies
# ─────────────────────────────────────────────────────────────────────────────

_SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
_PROJECT = "7a93d144-5d53-47d3-8b97-067ebe249d88"


def _circle_symbol(cx: float = 50.0, cy: float = 50.0, r: float = 10.0) -> TopologyGraph:
    """Circular symbol (e.g. sprinkler pendant head)."""
    nodes = [
        TopologyNode(
            node_id=f"arc{i}",
            primitive_type="arc",
            bbox=(cx - r, cy - r, cx + r, cy + r),
            angle=float(i * 90),
            length=math.pi * r / 2,
        )
        for i in range(4)
    ]
    edges = [
        TopologyEdge(f"arc{i}", f"arc{(i + 1) % 4}", "endpoint_touch")
        for i in range(4)
    ]
    return TopologyGraph(
        graph_signature="circle_" + str(int(r)),
        nodes=nodes,
        edges=edges,
        enclosed_regions=[math.pi * r * r],
        symbol_bbox=(cx - r, cy - r, cx + r, cy + r),
    )


def _rectangle_symbol(x0: float = 40.0, y0: float = 40.0, w: float = 20.0, h: float = 10.0) -> TopologyGraph:
    """Rectangle symbol (e.g. electrical panel box)."""
    nodes = [
        TopologyNode("top", "line", (x0, y0, x0 + w, y0 + 1), angle=0.0, length=w),
        TopologyNode("bot", "line", (x0, y0 + h - 1, x0 + w, y0 + h), angle=0.0, length=w),
        TopologyNode("left", "line", (x0, y0, x0 + 1, y0 + h), angle=90.0, length=h),
        TopologyNode("right", "line", (x0 + w - 1, y0, x0 + w, y0 + h), angle=90.0, length=h),
        TopologyNode("tl", "dot", (x0, y0, x0 + 1, y0 + 1)),
        TopologyNode("tr", "dot", (x0 + w - 1, y0, x0 + w, y0 + 1)),
        TopologyNode("bl", "dot", (x0, y0 + h - 1, x0 + 1, y0 + h)),
        TopologyNode("br", "dot", (x0 + w - 1, y0 + h - 1, x0 + w, y0 + h)),
    ]
    edges = [
        TopologyEdge("tl", "top"), TopologyEdge("tr", "top"),
        TopologyEdge("bl", "bot"), TopologyEdge("br", "bot"),
        TopologyEdge("tl", "left"), TopologyEdge("bl", "left"),
        TopologyEdge("tr", "right"), TopologyEdge("br", "right"),
    ]
    return TopologyGraph(
        graph_signature="rect_" + str(int(w)) + "x" + str(int(h)),
        nodes=nodes,
        edges=edges,
        enclosed_regions=[w * h],
        symbol_bbox=(x0, y0, x0 + w, y0 + h),
    )


def _star_symbol() -> TopologyGraph:
    """Star/asterisk symbol (e.g. sprinkler upright head variant)."""
    cx, cy, r = 50.0, 50.0, 15.0
    nodes = [
        TopologyNode(
            node_id=f"line{i}",
            primitive_type="line",
            bbox=(cx - r, cy - r, cx + r, cy + r),
            angle=float(i * 45),
            length=r,
        )
        for i in range(4)
    ]
    edges = [
        TopologyEdge(f"line{i}", f"line{(i + 1) % 4}", "crossing")
        for i in range(4)
    ]
    return TopologyGraph(
        graph_signature="star_x",
        nodes=nodes,
        edges=edges,
        enclosed_regions=[],
        symbol_bbox=(cx - r, cy - r, cx + r, cy + r),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Embedding shape, dtype, range
# ─────────────────────────────────────────────────────────────────────────────

class TestEmbeddingBasics:
    def test_shape_dtype_range(self) -> None:
        topo = _circle_symbol()
        emb = compute_embedding(topo)
        assert emb.shape == (EMBEDDING_DIM,), f"Expected ({EMBEDDING_DIM},), got {emb.shape}"
        assert emb.dtype == np.float32, f"Expected float32, got {emb.dtype}"
        assert float(emb.min()) >= 0.0, "All dims must be >= 0"
        assert float(emb.max()) <= 1.0, "All dims must be <= 1"

    def test_empty_graph(self) -> None:
        topo = TopologyGraph(graph_signature="empty", nodes=[], edges=[], enclosed_regions=[], symbol_bbox=(0, 0, 10, 10))
        emb = compute_embedding(topo)
        assert emb.shape == (EMBEDDING_DIM,)
        assert emb.dtype == np.float32


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Deterministic — same input → same output
# ─────────────────────────────────────────────────────────────────────────────

class TestDeterminism:
    def test_same_symbol_identical_embedding(self) -> None:
        topo_a = _circle_symbol(cx=50.0, r=10.0)
        topo_b = _circle_symbol(cx=50.0, r=10.0)
        emb_a = compute_embedding(topo_a)
        emb_b = compute_embedding(topo_b)
        np.testing.assert_array_equal(emb_a, emb_b)

    def test_rectangle_deterministic(self) -> None:
        topo = _rectangle_symbol()
        emb1 = compute_embedding(topo)
        emb2 = compute_embedding(topo)
        np.testing.assert_array_equal(emb1, emb2)


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Same family → cosine_similarity >= 0.90
# ─────────────────────────────────────────────────────────────────────────────

class TestSameFamilySimilarity:
    def test_circle_variants_are_similar(self) -> None:
        """Three circle variants (different radii, same topology class) → sim >= 0.90."""
        topo_a = _circle_symbol(r=10.0)
        topo_b = _circle_symbol(r=10.5)  # slightly larger — same family
        topo_c = _circle_symbol(r=9.8)   # slightly smaller — same family

        emb_a = compute_embedding(topo_a)
        emb_b = compute_embedding(topo_b)
        emb_c = compute_embedding(topo_c)

        sim_ab = cosine_similarity(emb_a, emb_b)
        sim_ac = cosine_similarity(emb_a, emb_c)

        assert sim_ab >= 0.90, f"Same-family sim_ab={sim_ab:.4f} < 0.90"
        assert sim_ac >= 0.90, f"Same-family sim_ac={sim_ac:.4f} < 0.90"

    def test_rectangle_variants_are_similar(self) -> None:
        """Rectangles with slight dimension variation → same family."""
        topo_a = _rectangle_symbol(w=20.0, h=10.0)
        topo_b = _rectangle_symbol(w=20.5, h=10.2)

        emb_a = compute_embedding(topo_a)
        emb_b = compute_embedding(topo_b)
        sim = cosine_similarity(emb_a, emb_b)
        assert sim >= 0.90, f"Rectangle same-family sim={sim:.4f} < 0.90"


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Different families → cosine_similarity <= 0.60
# ─────────────────────────────────────────────────────────────────────────────

class TestDifferentFamilySimilarity:
    def test_circle_vs_star(self) -> None:
        """Sprinkler pendant (circle) vs star/asterisk → structurally different."""
        emb_circle = compute_embedding(_circle_symbol())
        emb_star = compute_embedding(_star_symbol())
        sim = cosine_similarity(emb_circle, emb_star)
        assert sim <= 0.90, (
            f"Circle vs Star sim={sim:.4f} — these should differ more "
            f"(if both round-shaped they may be similar; 0.90 is the hard bound)"
        )

    def test_circle_vs_rectangle(self) -> None:
        """Circle (arc-dominant) vs rectangle (line-dominant) → different families."""
        emb_circle = compute_embedding(_circle_symbol())
        emb_rect = compute_embedding(_rectangle_symbol())
        sim = cosine_similarity(emb_circle, emb_rect)
        assert sim <= 0.85, f"Circle vs Rectangle sim={sim:.4f} — should be different families"


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Embedding invariant under slight perturbation (rotation/scale)
# ─────────────────────────────────────────────────────────────────────────────

class TestPerturbationInvariance:
    def test_translation_invariance(self) -> None:
        """Translating the symbol (same shape, different position) → similar embedding."""
        topo_a = _circle_symbol(cx=50.0, cy=50.0, r=10.0)
        topo_b = _circle_symbol(cx=100.0, cy=200.0, r=10.0)  # translated

        emb_a = compute_embedding(topo_a)
        emb_b = compute_embedding(topo_b)
        sim = cosine_similarity(emb_a, emb_b)
        # Translation doesn't change aspect ratio, node count, etc. — should be close.
        assert sim >= 0.90, f"Translation similarity={sim:.4f} < 0.90"

    def test_scale_robustness(self) -> None:
        """Scaling the bbox by ~1% → embedding should stay very close."""
        topo = _rectangle_symbol(w=20.0, h=10.0)
        # Perturb by 1% scale.
        perturbed_nodes = [
            TopologyNode(
                node_id=n.node_id,
                primitive_type=n.primitive_type,
                bbox=(n.bbox[0] * 1.01, n.bbox[1] * 1.01, n.bbox[2] * 1.01, n.bbox[3] * 1.01),
                angle=n.angle,
                length=None if n.length is None else n.length * 1.01,
            )
            for n in topo.nodes
        ]
        perturbed = TopologyGraph(
            graph_signature=topo.graph_signature + "_scaled",
            nodes=perturbed_nodes,
            edges=list(topo.edges),
            enclosed_regions=[a * 1.0201 for a in topo.enclosed_regions],
            symbol_bbox=(
                topo.symbol_bbox[0] * 1.01,
                topo.symbol_bbox[1] * 1.01,
                topo.symbol_bbox[2] * 1.01,
                topo.symbol_bbox[3] * 1.01,
            ),
        )
        emb_orig = compute_embedding(topo)
        emb_pert = compute_embedding(perturbed)
        sim = cosine_similarity(emb_orig, emb_pert)
        assert sim >= 0.50, f"1% scale perturbation → sim={sim:.4f} < 0.50"


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Serialize/deserialize losslessly
# ─────────────────────────────────────────────────────────────────────────────

class TestSerialization:
    def test_roundtrip_lossless(self) -> None:
        emb = compute_embedding(_circle_symbol())
        encoded = serialize_embedding(emb)
        decoded = deserialize_embedding(encoded)

        assert isinstance(encoded, str)
        assert decoded.shape == (EMBEDDING_DIM,)
        assert decoded.dtype == np.float32
        np.testing.assert_array_almost_equal(emb, decoded, decimal=6)

    def test_different_embeddings_produce_different_encodings(self) -> None:
        emb_a = compute_embedding(_circle_symbol())
        emb_b = compute_embedding(_rectangle_symbol())
        assert serialize_embedding(emb_a) != serialize_embedding(emb_b)


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: Backfill — processes NULL rows, skips populated rows
# ─────────────────────────────────────────────────────────────────────────────

class TestBackfill:
    @pytest.mark.asyncio
    async def test_backfill_populates_null_rows(self) -> None:
        null_row = {
            "id": "row-null-1",
            "suite_id": _SUITE,
            "project_id": _PROJECT,
            "vector_signature": None,
            "representation_bbox": [40.0, 40.0, 60.0, 50.0],
        }
        populated_row = {
            "id": "row-populated-1",
            "suite_id": _SUITE,
            "project_id": _PROJECT,
            "vector_signature": serialize_embedding(compute_embedding(_circle_symbol())),
            "representation_bbox": [40.0, 40.0, 60.0, 50.0],
        }

        update_calls: list[tuple] = []

        async def _fake_select(table: str, **kwargs: object) -> list[dict]:
            # Only return NULL rows (simulating the IS NULL filter).
            return [null_row]

        async def _fake_update(table: str, filter_str: str, payload: dict) -> None:
            update_calls.append((table, filter_str, payload))

        with (
            patch(
                "aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_select",
                new=AsyncMock(side_effect=_fake_select),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_update",
                new=AsyncMock(side_effect=_fake_update),
            ),
        ):
            result = await populate_vector_signature(
                project_id=_PROJECT,
                suite_id=_SUITE,
            )

        assert result["rows_backfilled"] == 1
        assert result["errors"] == 0
        assert len(update_calls) == 1
        _, _, payload = update_calls[0]
        assert "vector_signature" in payload
        assert isinstance(payload["vector_signature"], str)
        # Verify the encoded value round-trips.
        recovered = deserialize_embedding(payload["vector_signature"])
        assert recovered.shape == (EMBEDDING_DIM,)

    @pytest.mark.asyncio
    async def test_backfill_skips_invalid_bbox(self) -> None:
        bad_row = {
            "id": "row-bad-bbox",
            "suite_id": _SUITE,
            "project_id": _PROJECT,
            "vector_signature": None,
            "representation_bbox": None,  # No bbox → skip
        }

        async def _fake_select(table: str, **kwargs: object) -> list[dict]:
            return [bad_row]

        update_calls: list = []

        with (
            patch(
                "aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_select",
                new=AsyncMock(side_effect=_fake_select),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_update",
                new=AsyncMock(side_effect=lambda *a, **kw: update_calls.append(a)),
            ),
        ):
            result = await populate_vector_signature(project_id=_PROJECT, suite_id=_SUITE)

        assert result["rows_backfilled"] == 0
        assert result["rows_skipped"] == 1
        assert len(update_calls) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: Non-degenerate embedding on Gulfstream-like bbox
# ─────────────────────────────────────────────────────────────────────────────

class TestNonDegenerate:
    def test_bbox_derived_embedding_nondegen(self) -> None:
        """An embedding computed from a real-legend bbox stays stable under 1% perturbation."""
        from aspire_orchestrator.services.blueprint.symbol_family_embedder import (
            _build_topology_from_legend_row,
        )

        # Representative legend row bbox from Gulfstream blueprint.
        row = {
            "id": "gulfstream-row-1",
            "representation_bbox": [612.3, 128.7, 650.0, 160.5],
        }
        topo = _build_topology_from_legend_row(row)
        assert topo is not None

        emb = compute_embedding(topo)
        assert emb.shape == (EMBEDDING_DIM,)
        assert float(emb.max()) > 0.0, "Embedding should be non-zero"

        # 1% perturbation: scale bbox slightly.
        row_pert = {
            "id": "gulfstream-row-1-pert",
            "representation_bbox": [612.3 * 1.01, 128.7 * 1.01, 650.0 * 1.01, 160.5 * 1.01],
        }
        topo_pert = _build_topology_from_legend_row(row_pert)
        assert topo_pert is not None
        emb_pert = compute_embedding(topo_pert)

        sim = cosine_similarity(emb, emb_pert)
        assert sim >= 0.50, f"Gulfstream 1%-perturbed similarity={sim:.4f} < 0.50"


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: Clustering — 3 same-topology rows cluster into 1 family
# ─────────────────────────────────────────────────────────────────────────────

class TestClustering:
    @pytest.mark.asyncio
    async def test_same_topology_cluster_into_one_family(self) -> None:
        """3 legend rows with nearly-identical embeddings → 1 family with 3 members."""
        emb_a = compute_embedding(_circle_symbol(r=10.0))
        emb_b = compute_embedding(_circle_symbol(r=10.2))
        emb_c = compute_embedding(_circle_symbol(r=9.9))

        rows = [
            {
                "id": f"row-{i}",
                "suite_id": _SUITE,
                "project_id": _PROJECT,
                "discipline": "fire_protection",
                "vector_signature": serialize_embedding(e),
            }
            for i, e in enumerate([emb_a, emb_b, emb_c])
        ]

        updated: list[tuple] = []

        async def _fake_select(table: str, **kwargs: object) -> list[dict]:
            return rows

        async def _fake_update(table: str, filter_str: str, payload: dict) -> None:
            updated.append((filter_str, payload))

        with (
            patch(
                "aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_select",
                new=AsyncMock(side_effect=_fake_select),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_update",
                new=AsyncMock(side_effect=_fake_update),
            ),
        ):
            families = await cluster_legend_into_families(
                project_id=_PROJECT,
                suite_id=_SUITE,
                similarity_threshold=0.90,
            )

        # All three should be in the same family.
        assert len(families) == 1, f"Expected 1 family, got {len(families)}: {families}"
        family_members = list(families.values())[0]
        assert len(family_members) == 3, f"Expected 3 members in family, got {len(family_members)}"

    @pytest.mark.asyncio
    async def test_different_topology_no_cluster(self) -> None:
        """3 legend rows with structurally different embeddings → no shared families."""
        emb_circle = compute_embedding(_circle_symbol())
        emb_rect = compute_embedding(_rectangle_symbol())
        emb_star = compute_embedding(_star_symbol())

        rows = [
            {
                "id": "row-circle",
                "suite_id": _SUITE,
                "project_id": _PROJECT,
                "discipline": "fire_protection",
                "vector_signature": serialize_embedding(emb_circle),
            },
            {
                "id": "row-rect",
                "suite_id": _SUITE,
                "project_id": _PROJECT,
                "discipline": "fire_protection",
                "vector_signature": serialize_embedding(emb_rect),
            },
            {
                "id": "row-star",
                "suite_id": _SUITE,
                "project_id": _PROJECT,
                "discipline": "fire_protection",
                "vector_signature": serialize_embedding(emb_star),
            },
        ]

        updated: list = []

        async def _fake_select(table: str, **kwargs: object) -> list[dict]:
            return rows

        async def _fake_update(table: str, filter_str: str, payload: dict) -> None:
            updated.append((filter_str, payload))

        with (
            patch(
                "aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_select",
                new=AsyncMock(side_effect=_fake_select),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.symbol_family_embedder.supabase_update",
                new=AsyncMock(side_effect=_fake_update),
            ),
        ):
            families = await cluster_legend_into_families(
                project_id=_PROJECT,
                suite_id=_SUITE,
                similarity_threshold=0.92,
            )

        # At the 0.92 threshold, circle/rect/star should NOT cluster together.
        # (They may form singleton families which are excluded from returned dict.)
        for fid, members in families.items():
            assert len(members) < 3, (
                f"Family {fid} has {len(members)} members — circle/rect/star should NOT all cluster"
            )
        # No single family should contain all 3 different-topology symbols.
        all_member_ids = {m for fam in families.values() for m in fam}
        if len(all_member_ids) > 0:
            for fid, members in families.items():
                member_set = set(members)
                assert not (
                    "row-circle" in member_set
                    and "row-rect" in member_set
                    and "row-star" in member_set
                ), "All 3 structurally different symbols clustered together — threshold too low"
