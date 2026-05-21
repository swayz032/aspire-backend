"""Wave 11g-v2 Track A — Phase 3 tests: vector_graph_builder.

Covers:
  * 4 collinear lines forming a rectangle => 4 nodes (corners) + 4 edges
  * 2 disconnected circles => 2 nodes, 2 components, 0 edges
  * Snap tolerance: endpoints at distance 0.3 with tol=0.5 => 1 merged node
  * Snap tolerance: same with tol=0.1 => 2 separate nodes
  * Rotation-invariant view: graph from rotated primitives has same
    neighborhood transform_signatures
  * Graph metadata: page_number / total_primitives / snap_tolerance carried
"""
from __future__ import annotations

import hashlib

from aspire_orchestrator.services.blueprint.vector_extractor import VectorPrimitive
from aspire_orchestrator.services.blueprint.vector_graph_builder import (
    VectorGraph,
    build_graph,
)


def _mk_prim(
    *,
    type: str,
    points: list[tuple[float, float]],
    pid: str = "p",
    page: int = 1,
    sig: str = "sig",
) -> VectorPrimitive:
    bbox = (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )
    h = hashlib.sha1((pid + sig).encode()).hexdigest()
    return VectorPrimitive(
        primitive_id=pid,
        type=type,  # type: ignore[arg-type]
        points=points,
        stroke_width=0.5,
        bbox=bbox,
        page_number=page,
        transform_signature=sig,
        fill=False,
        hash=h,
    )


def test_four_lines_form_rectangle() -> None:
    """Four lines whose endpoints meet at 4 corners → 4 nodes, 4 edges."""
    corners = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    primitives = []
    for i in range(4):
        a = corners[i]
        b = corners[(i + 1) % 4]
        primitives.append(_mk_prim(type="line", points=[a, b], pid=f"line_{i}"))

    g = build_graph(primitives, snap_tolerance=0.5)
    assert g.number_of_nodes() == 4
    assert g.number_of_edges() == 4
    components = g.connected_components()
    assert len(components) == 1


def test_two_disconnected_circles() -> None:
    c1 = _mk_prim(
        type="circle",
        points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
        pid="c1",
    )
    c2 = _mk_prim(
        type="circle",
        points=[(100.0, 100.0), (101.0, 100.0), (101.0, 101.0), (100.0, 101.0)],
        pid="c2",
    )
    g = build_graph([c1, c2], snap_tolerance=0.5)
    assert g.number_of_nodes() == 2  # one centre per circle
    assert g.number_of_edges() == 0  # circles are degenerate single-node primitives
    assert len(g.connected_components()) == 2


def test_snap_tolerance_merges_close_endpoints() -> None:
    """Two endpoints 0.3 units apart with tol=0.5 → 1 merged node."""
    a = _mk_prim(type="line", points=[(0.0, 0.0), (10.0, 0.0)], pid="a")
    b = _mk_prim(type="line", points=[(10.3, 0.0), (20.0, 0.0)], pid="b")
    g = build_graph([a, b], snap_tolerance=0.5)
    # Expect 3 unique nodes (a-start, merged middle, b-end)
    assert g.number_of_nodes() == 3
    assert g.number_of_edges() == 2
    assert len(g.connected_components()) == 1


def test_snap_tolerance_keeps_close_endpoints_separate() -> None:
    """Two endpoints 0.3 units apart with tol=0.1 → 2 separate nodes."""
    a = _mk_prim(type="line", points=[(0.0, 0.0), (10.0, 0.0)], pid="a")
    b = _mk_prim(type="line", points=[(10.3, 0.0), (20.0, 0.0)], pid="b")
    g = build_graph([a, b], snap_tolerance=0.1)
    assert g.number_of_nodes() == 4
    assert g.number_of_edges() == 2
    assert len(g.connected_components()) == 2


def test_rotation_invariant_edge_signatures_match() -> None:
    """A graph built from rotated primitives keeps the same transform_signature
    on every edge (rotation-invariance comes from the extractor; the graph
    just preserves it)."""
    # Original L-shape
    orig = [
        _mk_prim(type="line", points=[(0.0, 0.0), (1.0, 0.0)], pid="seg1", sig="sigA"),
        _mk_prim(type="line", points=[(1.0, 0.0), (1.0, 1.0)], pid="seg2", sig="sigB"),
    ]
    # Rotated 90° CCW — same transform_signatures because extractor would
    # canonicalise them. We mimic that here by reusing sigA/sigB.
    rotated = [
        _mk_prim(type="line", points=[(0.0, 0.0), (0.0, 1.0)], pid="seg1r", sig="sigA"),
        _mk_prim(type="line", points=[(0.0, 1.0), (-1.0, 1.0)], pid="seg2r", sig="sigB"),
    ]
    g_orig = build_graph(orig)
    g_rot = build_graph(rotated)
    orig_sigs = sorted(e.transform_signature for e in g_orig.edge_data.values())
    rot_sigs = sorted(e.transform_signature for e in g_rot.edge_data.values())
    assert orig_sigs == rot_sigs


def test_graph_metadata_carried() -> None:
    p = _mk_prim(type="line", points=[(0.0, 0.0), (1.0, 0.0)], pid="p", page=7)
    g = build_graph([p], snap_tolerance=0.25, rotation_invariant=True)
    assert g.graph["page_number"] == 7
    assert g.graph["total_primitives"] == 1
    assert g.graph["snap_tolerance"] == 0.25
    assert g.graph["rotation_invariant"] is True


def test_empty_primitives_returns_empty_graph() -> None:
    g = build_graph([], snap_tolerance=0.5)
    assert isinstance(g, VectorGraph)
    assert g.number_of_nodes() == 0
    assert g.number_of_edges() == 0
    assert g.graph["total_primitives"] == 0


def test_rect_primitive_builds_4_edges() -> None:
    r = _mk_prim(
        type="rect",
        points=[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
        pid="r",
    )
    g = build_graph([r], snap_tolerance=0.5)
    assert g.number_of_nodes() == 4
    assert g.number_of_edges() == 4
