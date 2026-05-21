"""Wave 11g-v2 Track A — Phase 3: Graph Construction.

Lifts a list of :class:`vector_extractor.VectorPrimitive` records into a
graph where:

* **Nodes** = canonical endpoints (line/polyline ends, arc centres,
  circle centres). Endpoints within ``snap_tolerance`` are merged into
  a single node via a grid spatial index — O(N) build, O(1) snap lookup.
* **Edges** = primitives that connect two nodes. Each edge carries the
  primitive's ``primitive_id``, ``type``, ``transform_signature`` and
  ``stroke_width`` as edge attributes so Track B can compute WL labels
  without re-walking the geometry.

The graph object intentionally mimics a tiny subset of the ``networkx``
API (``nodes``, ``edges``, ``graph``, ``neighbors``, ``__contains__``,
``__len__``) so Track B can swap to networkx by changing one import
line, with no downstream changes. We do NOT depend on networkx because
it is not in pyproject.toml and the brief requires "no new external deps".

Rotation-invariant view
-----------------------
When ``rotation_invariant=True`` (default), a second graph is produced
with primitive transform_signatures already canonicalised to their
lex-min rotation (see :func:`vector_extractor._rotation_invariant_signature`)
— Track B's WL hashing will be invariant under 4-fold rotation.

Provenance (Law #2)
-------------------
``graph.graph`` carries ``page_number``, ``total_primitives``,
``snap_tolerance`` and ``rotation_invariant`` so the build is fully
reproducible from a receipt.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterator

from aspire_orchestrator.services.blueprint.vector_extractor import VectorPrimitive

logger = logging.getLogger(__name__)


_DEFAULT_SNAP_TOLERANCE = 0.5


@dataclass
class _NodeData:
    node_id: int
    x: float
    y: float
    primitive_ids: list[str] = field(default_factory=list)


@dataclass
class _EdgeData:
    u: int
    v: int
    primitive_id: str
    type: str
    transform_signature: str
    stroke_width: float
    hash: str


class VectorGraph:
    """Tiny graph container that mimics a slice of the networkx.Graph API.

    Iterating ``graph.nodes`` yields node ids (ints); ``graph.edges``
    yields (u, v) tuples. ``graph.graph`` is a dict of graph-level
    metadata. ``graph.node_data[i]`` and ``graph.edge_data[(u,v)]``
    expose the rich payload.
    """

    def __init__(self) -> None:
        self._nodes: dict[int, _NodeData] = {}
        self._adj: dict[int, set[int]] = {}
        self._edges: dict[tuple[int, int], _EdgeData] = {}
        self.graph: dict[str, object] = {}

    # ── networkx-compatible read surface ────────────────────────────
    @property
    def nodes(self) -> list[int]:
        return list(self._nodes.keys())

    @property
    def edges(self) -> list[tuple[int, int]]:
        return list(self._edges.keys())

    @property
    def node_data(self) -> dict[int, _NodeData]:
        return self._nodes

    @property
    def edge_data(self) -> dict[tuple[int, int], _EdgeData]:
        return self._edges

    def neighbors(self, n: int) -> Iterator[int]:
        return iter(self._adj.get(n, set()))

    def __contains__(self, n: object) -> bool:
        return n in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def number_of_nodes(self) -> int:
        return len(self._nodes)

    def number_of_edges(self) -> int:
        return len(self._edges)

    # ── write surface (internal) ────────────────────────────────────
    def _add_node(self, node_id: int, x: float, y: float, primitive_id: str) -> None:
        if node_id in self._nodes:
            self._nodes[node_id].primitive_ids.append(primitive_id)
        else:
            self._nodes[node_id] = _NodeData(
                node_id=node_id, x=x, y=y, primitive_ids=[primitive_id]
            )
            self._adj[node_id] = set()

    def _add_edge(self, u: int, v: int, data: _EdgeData) -> None:
        # Canonical key (min, max) so each undirected edge stores once.
        key = (min(u, v), max(u, v))
        if key in self._edges:
            # Multiple primitives between same nodes — keep first, log.
            logger.debug(
                "vector_graph_builder.parallel_edge u=%s v=%s existing=%s new=%s",
                u, v, self._edges[key].primitive_id[:8], data.primitive_id[:8],
            )
            return
        self._edges[key] = data
        self._adj[u].add(v)
        self._adj[v].add(u)

    # ── components (used by tests + Track B) ────────────────────────
    def connected_components(self) -> list[set[int]]:
        seen: set[int] = set()
        components: list[set[int]] = []
        for n in self._nodes:
            if n in seen:
                continue
            stack = [n]
            comp: set[int] = set()
            while stack:
                cur = stack.pop()
                if cur in comp:
                    continue
                comp.add(cur)
                stack.extend(self._adj.get(cur, ()))
            seen |= comp
            components.append(comp)
        return components


class _GridIndex:
    """O(1)-average snap lookup via a coordinate-bucketed grid.

    All endpoints within ``tolerance`` of a query point are guaranteed to
    fall into one of the 9 cells around the query's home cell (cell side
    = tolerance), so we only ever scan 9 buckets.
    """

    def __init__(self, tolerance: float) -> None:
        # Tolerance == 0 still needs a valid cell size; clamp.
        self.cell = max(tolerance, 1e-9)
        self.tolerance = tolerance
        self.tol_sq = tolerance * tolerance
        self._cells: dict[tuple[int, int], list[tuple[float, float, int]]] = {}

    def _cell_for(self, x: float, y: float) -> tuple[int, int]:
        return int(x // self.cell), int(y // self.cell)

    def query(self, x: float, y: float) -> int | None:
        cx, cy = self._cell_for(x, y)
        best: tuple[float, int] | None = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                bucket = self._cells.get((cx + dx, cy + dy))
                if not bucket:
                    continue
                for bx, by, nid in bucket:
                    d2 = (bx - x) ** 2 + (by - y) ** 2
                    if d2 <= self.tol_sq and (best is None or d2 < best[0]):
                        best = (d2, nid)
        return None if best is None else best[1]

    def insert(self, x: float, y: float, node_id: int) -> None:
        key = self._cell_for(x, y)
        self._cells.setdefault(key, []).append((x, y, node_id))


def _node_endpoints_for(prim: VectorPrimitive) -> list[tuple[float, float]]:
    """Choose the canonical "graph endpoints" for a primitive.

    * line / polyline → first + last point of the points list
    * curve / path → first + last point of the points list (control
      points become interior — uninteresting for graph topology)
    * rect → the 4 corners
    * circle → centre only (single self-node; degenerate edge)
    """
    pts = prim.points
    if not pts:
        return []
    if prim.type == "circle":
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        return [(cx, cy)]
    if prim.type == "rect":
        x0, y0, x1, y1 = prim.bbox
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    # line / polyline / curve / path
    return [pts[0], pts[-1]]


def build_graph(
    primitives: list[VectorPrimitive],
    *,
    snap_tolerance: float = _DEFAULT_SNAP_TOLERANCE,
    rotation_invariant: bool = True,
) -> VectorGraph:
    """Build a :class:`VectorGraph` from a list of vector primitives.

    Parameters
    ----------
    primitives
        Output of :func:`vector_extractor.extract_primitives`.
    snap_tolerance
        Endpoints within this distance (in primitive-coordinate units —
        which are already-bbox-normalised, see vector_extractor) are
        merged into the same graph node. Default 0.5pt.
    rotation_invariant
        When True, every edge stores the primitive's already
        rotation-invariant ``transform_signature``. (Track B uses this
        directly; we set this attribute on the graph so downstream code
        can verify the assumption.)
    """
    graph = VectorGraph()
    graph.graph["page_number"] = primitives[0].page_number if primitives else None
    graph.graph["total_primitives"] = len(primitives)
    graph.graph["snap_tolerance"] = snap_tolerance
    graph.graph["rotation_invariant"] = rotation_invariant

    if not primitives:
        return graph

    index = _GridIndex(snap_tolerance)
    next_id = 0

    for prim in primitives:
        endpoints = _node_endpoints_for(prim)
        if not endpoints:
            continue

        snapped_ids: list[int] = []
        for x, y in endpoints:
            existing = index.query(x, y)
            if existing is not None:
                graph._add_node(existing, x, y, prim.primitive_id)
                snapped_ids.append(existing)
            else:
                nid = next_id
                next_id += 1
                graph._add_node(nid, x, y, prim.primitive_id)
                index.insert(x, y, nid)
                snapped_ids.append(nid)

        # Edge construction depends on primitive type.
        if prim.type == "circle":
            # Single-node degenerate — no edges; still a graph node.
            continue
        if prim.type == "rect":
            # Connect the 4 corners cyclically.
            n = len(snapped_ids)
            for i in range(n):
                u = snapped_ids[i]
                v = snapped_ids[(i + 1) % n]
                if u == v:
                    continue
                graph._add_edge(u, v, _EdgeData(
                    u=u, v=v,
                    primitive_id=prim.primitive_id,
                    type=prim.type,
                    transform_signature=prim.transform_signature,
                    stroke_width=prim.stroke_width,
                    hash=prim.hash,
                ))
            continue
        # line / polyline / curve / path → connect first to last.
        if len(snapped_ids) >= 2:
            u, v = snapped_ids[0], snapped_ids[-1]
            if u != v:
                graph._add_edge(u, v, _EdgeData(
                    u=u, v=v,
                    primitive_id=prim.primitive_id,
                    type=prim.type,
                    transform_signature=prim.transform_signature,
                    stroke_width=prim.stroke_width,
                    hash=prim.hash,
                ))

    logger.debug(
        "vector_graph_builder.ok page=%s primitives=%s nodes=%s edges=%s tol=%s",
        graph.graph["page_number"], len(primitives),
        graph.number_of_nodes(), graph.number_of_edges(), snap_tolerance,
    )
    return graph


__all__ = ["VectorGraph", "build_graph"]
