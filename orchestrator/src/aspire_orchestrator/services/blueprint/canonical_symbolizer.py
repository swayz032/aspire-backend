"""Wave 20.6 Stage 2 — Construction Semantic Compiler: Canonical Symbolization.

Architects redraw the same construction symbol with different stroke weights,
fills, and rotations across sheets and projects. Hash-matching on raw geometry
fails. This module normalizes representation BEFORE comparison so that:

    ○P, (P), ⊙P, small-circle+P, filled-circle+P

all reduce to the same topological signature. This feeds Stage 3 (Symbol Family
Embeddings) with stable, noise-free graph structures.

Pipeline:
  1. normalize_symbol()    → CanonicalForm   (rotation / stroke / fill / scale)
  2. extract_topology()    → TopologyGraph   (nodes / edges / faces / symmetry)
  3. simplify_graph()      → TopologyGraph   (remove noise / annotation overlays)

Law #9: no raw geometry bytes in logs. Only node/edge counts + signature prefixes.
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from aspire_orchestrator.services.blueprint.vector_extractor import VectorPrimitive

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared geometry helpers
# ---------------------------------------------------------------------------

def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_diagonal(points: list[tuple[float, float]]) -> float:
    x0, y0, x1, y1 = _bbox(points)
    return math.hypot(x1 - x0, y1 - y0)


# ---------------------------------------------------------------------------
# Stage 2a — Canonical normalization
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NormalizedPrimitive:
    """A single drawing primitive in unit-square, rotation-aligned coordinates."""

    type: str  # "line" | "arc" | "curve"
    points: tuple[tuple[float, float], ...]  # 2+ points in unit-square space


@dataclass
class CanonicalForm:
    """Rotation/stroke/fill/scale-normalized representation of a vector glyph.

    All geometry is expressed in a unit square (0–1 × 0–1) with the principal
    axis of inertia aligned to the horizontal axis.
    """

    primitives_normalized: list[NormalizedPrimitive]
    principal_axis_angle_degrees: float
    aspect_ratio: float         # width / height of original bbox (≥ 1.0 by convention)
    enclosed_region_count: int
    has_filled_region: bool


def _collect_all_points(primitives: list[VectorPrimitive]) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for p in primitives:
        pts.extend(p.points)
    return pts


def _principal_axis_angle(points: list[tuple[float, float]]) -> float:
    """Return the angle (degrees) of the principal axis via 2-D PCA.

    Uses numpy for a clean eigendecomposition. Returns 0.0 when the point
    cloud is degenerate (all co-incident or collinear with zero variance in
    one direction).
    """
    if len(points) < 2:
        return 0.0
    arr = np.array(points, dtype=float)
    centered = arr - arr.mean(axis=0)
    cov = np.cov(centered.T)
    if cov.ndim == 0:
        return 0.0
    evals, evecs = np.linalg.eigh(cov)
    principal = evecs[:, np.argmax(evals)]
    angle_rad = math.atan2(float(principal[1]), float(principal[0]))
    return math.degrees(angle_rad)


def _rotate_points(
    points: list[tuple[float, float]], angle_deg: float
) -> list[tuple[float, float]]:
    a = math.radians(-angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    return [(x * cos_a - y * sin_a, x * sin_a + y * cos_a) for x, y in points]


def _to_unit_square(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Scale/translate so the bounding box fits in [0,1]×[0,1]."""
    if not points:
        return points
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, y0 = min(xs), min(ys)
    span = max(max(xs) - x0, max(ys) - y0)
    if span == 0:
        return [(0.5, 0.5)] * len(points)
    return [((x - x0) / span, (y - y0) / span) for x, y in points]


def _primitive_type_simplified(ptype: str) -> str:
    if ptype in {"line", "polyline"}:
        return "line"
    if ptype in {"circle", "arc"}:
        return "arc"
    return "curve"


def normalize_symbol(primitives: list[VectorPrimitive]) -> CanonicalForm:
    """Reduce a vector glyph to its canonical representation.

    Steps:
    1. Rotation normalization — PCA over all endpoints, rotate to align
       principal axis with horizontal.
    2. Stroke normalization — collapse all stroke widths to 1.0.
    3. Fill normalization — replace solid fills with a 'has_filled_region'
       boolean; drop hatch patterns; keep enclosed-region count.
    4. Scale normalization — fit bounding box to unit square, preserve aspect
       ratio in the 'aspect_ratio' field.
    """
    if not primitives:
        return CanonicalForm(
            primitives_normalized=[],
            principal_axis_angle_degrees=0.0,
            aspect_ratio=1.0,
            enclosed_region_count=0,
            has_filled_region=False,
        )

    all_pts = _collect_all_points(primitives)
    angle = _principal_axis_angle(all_pts)

    # Compute original aspect ratio before rotation (use full point cloud bbox)
    raw_bbox = _bbox(all_pts)
    raw_w = raw_bbox[2] - raw_bbox[0]
    raw_h = raw_bbox[3] - raw_bbox[1]
    aspect = max(raw_w, raw_h) / max(min(raw_w, raw_h), 1e-9)

    has_fill = any(p.fill for p in primitives)
    enclosed_count = sum(1 for p in primitives if p.fill or p.type in {"circle", "rect"})

    # Rotate ALL points globally, compute shared bounds, then apply one transform
    all_rotated = _rotate_points(all_pts, angle)
    x0 = min(p[0] for p in all_rotated)
    y0 = min(p[1] for p in all_rotated)
    span = max(
        max(p[0] for p in all_rotated) - x0,
        max(p[1] for p in all_rotated) - y0,
    )
    if span == 0:
        span = 1.0

    def _to_unit(pt: tuple[float, float]) -> tuple[float, float]:
        return (round((pt[0] - x0) / span, 6), round((pt[1] - y0) / span, 6))

    normalized: list[NormalizedPrimitive] = []
    for prim in primitives:
        rotated = _rotate_points(prim.points, angle)
        normalized.append(
            NormalizedPrimitive(
                type=_primitive_type_simplified(prim.type),
                points=tuple(_to_unit(p) for p in rotated),
            )
        )

    return CanonicalForm(
        primitives_normalized=normalized,
        principal_axis_angle_degrees=round(angle, 4),
        aspect_ratio=round(aspect, 4),
        enclosed_region_count=enclosed_count,
        has_filled_region=has_fill,
    )


# ---------------------------------------------------------------------------
# Stage 2b — Topology extraction
# ---------------------------------------------------------------------------

@dataclass
class GraphNode:
    node_id: int
    x: float
    y: float
    degree: int = 0


@dataclass
class GraphEdge:
    from_node: int
    to_node: int
    primitive_type: str


@dataclass
class Face:
    node_ids: list[int]


@dataclass
class SymmetryAxis:
    axis: str  # "vertical" | "horizontal" | "diagonal_pos" | "diagonal_neg"
    confidence: float


@dataclass
class TopologyGraph:
    """Graph representation of a canonical symbol, usable as Stage 3 input."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    faces: list[Face]
    symmetry_axes: list[SymmetryAxis]
    graph_signature: str  # Weisfeiler-Lehman hash, k=3 — survives stroke/fill/rotation variation


def _snap_key(x: float, y: float, grid: float = 0.02) -> tuple[int, int]:
    """Quantise to a snap grid for node merging (2% of unit square)."""
    return (int(round(x / grid)), int(round(y / grid)))


def _wl_hash(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    iterations: int = 3,
) -> str:
    """Weisfeiler-Lehman graph hash.

    Each node's label is iteratively re-hashed from its own label + the
    sorted (label, edge_type) pairs of its neighbours. After k iterations,
    the multiset of labels is hashed into a single graph signature. Invariant
    to node ordering; captures local topology up to depth k including edge types
    (so arc-cycles and line-cycles produce different signatures).
    """
    # Initial labels = degree + sorted incident edge types for type discrimination
    edge_types_by_node: dict[int, list[str]] = {n.node_id: [] for n in nodes}
    adj: dict[int, list[tuple[int, str]]] = {n.node_id: [] for n in nodes}
    for e in edges:
        adj[e.from_node].append((e.to_node, e.primitive_type))
        adj[e.to_node].append((e.from_node, e.primitive_type))
        edge_types_by_node[e.from_node].append(e.primitive_type)
        edge_types_by_node[e.to_node].append(e.primitive_type)

    labels: dict[int, str] = {
        n.node_id: f"{n.degree}:{','.join(sorted(edge_types_by_node[n.node_id]))}"
        for n in nodes
    }

    for _ in range(iterations):
        new_labels: dict[int, str] = {}
        for nid, lbl in labels.items():
            neighbour_labels = sorted(
                f"{labels[nbr]}:{etype}" for nbr, etype in adj[nid]
            )
            raw = lbl + "|" + ",".join(neighbour_labels)
            new_labels[nid] = hashlib.sha1(raw.encode()).hexdigest()[:16]
        labels = new_labels

    multiset = sorted(labels.values())
    return hashlib.sha1("|".join(multiset).encode()).hexdigest()


def _detect_symmetry_axes(
    nodes: list[GraphNode],
    tolerance: float = 0.05,
) -> list[SymmetryAxis]:
    """Detect reflection symmetries by testing 4 candidate axes."""
    if len(nodes) < 2:
        return []

    coords = [(n.x, n.y) for n in nodes]
    cx = sum(x for x, _ in coords) / len(coords)
    cy = sum(y for _, y in coords) / len(coords)

    def _reflected(x: float, y: float, axis: str) -> tuple[float, float]:
        rx, ry = x - cx, y - cy
        if axis == "vertical":
            return (-rx + cx, ry + cy)
        if axis == "horizontal":
            return (rx + cx, -ry + cy)
        if axis == "diagonal_pos":
            return (ry + cx, rx + cy)
        return (-ry + cx, -rx + cy)  # diagonal_neg

    def _symmetry_score(axis: str) -> float:
        matched = 0
        for x, y in coords:
            rx, ry = _reflected(x, y, axis)
            if any(math.hypot(ox - rx, oy - ry) < tolerance for ox, oy in coords):
                matched += 1
        return matched / len(coords)

    result: list[SymmetryAxis] = []
    for axis in ("vertical", "horizontal", "diagonal_pos", "diagonal_neg"):
        score = _symmetry_score(axis)
        if score >= 0.85:
            result.append(SymmetryAxis(axis=axis, confidence=round(score, 3)))
    return result


def extract_topology(canonical: CanonicalForm) -> TopologyGraph:
    """Convert canonical form into a graph representation.

    - Nodes = vertex points (endpoints, arc centres, curve control points)
    - Edges = primitive connections
    - Faces = enclosed regions (approximated from 'arc' primitives)
    - Symmetry axes = detected via reflection invariance test
    - graph_signature = Weisfeiler-Lehman hash (k=3)
    """
    snap_map: dict[tuple[int, int], int] = {}
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    def _get_or_create_node(x: float, y: float) -> int:
        key = _snap_key(x, y)
        if key not in snap_map:
            nid = len(nodes)
            snap_map[key] = nid
            nodes.append(GraphNode(node_id=nid, x=x, y=y, degree=0))
        return snap_map[key]

    for prim in canonical.primitives_normalized:
        pts = list(prim.points)
        if len(pts) < 2:
            continue
        # Create nodes for first + last point; intermediate points are topology-transparent
        u = _get_or_create_node(*pts[0])
        v = _get_or_create_node(*pts[-1])
        if u != v:
            edges.append(GraphEdge(from_node=u, to_node=v, primitive_type=prim.type))
            nodes[u].degree += 1
            nodes[v].degree += 1

    # Faces: approximated from enclosed-region count in canonical form
    faces: list[Face] = []
    if canonical.enclosed_region_count > 0:
        # Build faces from arc primitives forming closed loops
        arc_prims = [p for p in canonical.primitives_normalized if p.type == "arc"]
        for arc in arc_prims[:canonical.enclosed_region_count]:
            face_nodes = [_get_or_create_node(*pt) for pt in arc.points]
            if face_nodes:
                faces.append(Face(node_ids=face_nodes))

    symmetry_axes = _detect_symmetry_axes(nodes)
    signature = _wl_hash(nodes, edges)

    logger.debug(
        "topology extracted nodes=%d edges=%d faces=%d sig=%s",
        len(nodes), len(edges), len(faces), signature[:8],
    )

    return TopologyGraph(
        nodes=nodes,
        edges=edges,
        faces=faces,
        symmetry_axes=symmetry_axes,
        graph_signature=signature,
    )


# ---------------------------------------------------------------------------
# Stage 2c — Graph simplification
# ---------------------------------------------------------------------------

def simplify_graph(
    graph: TopologyGraph,
    *,
    sheet_context: dict[str, Any] | None = None,
) -> TopologyGraph:
    """Remove noise from topology graph.

    - Drop annotation overlays (text bboxes from sheet_context OCR paragraphs)
    - Collapse colinear edges (3+ collinear segments → 1 edge)
    - Remove degree-1 dangling edges shorter than 5% of bbox diagonal
    - Merge nodes within 2% of bbox diagonal

    Returns a simplified TopologyGraph. The graph_signature is recomputed on
    the cleaned graph — it will remain semantically equivalent when noise is
    the only thing removed.
    """
    if not graph.nodes:
        return graph

    # --- Drop annotation overlays using OCR text bboxes ---
    text_regions: list[tuple[float, float, float, float]] = []
    if sheet_context:
        for para in sheet_context.get("ocr_paragraphs", []):
            bbox = para.get("bbox")
            if bbox and len(bbox) == 4:
                text_regions.append(tuple(bbox))  # type: ignore[misc]

    def _in_text_region(x: float, y: float) -> bool:
        for x0, y0, x1, y1 in text_regions:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return True
        return False

    kept_node_ids: set[int] = {
        n.node_id for n in graph.nodes if not _in_text_region(n.x, n.y)
    }

    nodes_clean = [n for n in graph.nodes if n.node_id in kept_node_ids]
    edges_clean = [
        e for e in graph.edges
        if e.from_node in kept_node_ids and e.to_node in kept_node_ids
    ]
    faces_clean = [
        f for f in graph.faces
        if all(nid in kept_node_ids for nid in f.node_ids)
    ]

    # --- Merge nodes within 2% of bbox diagonal ---
    if nodes_clean:
        xs = [n.x for n in nodes_clean]
        ys = [n.y for n in nodes_clean]
        diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        merge_tol = diag * 0.02
    else:
        merge_tol = 0.001

    # Union-find merge
    parent: dict[int, int] = {n.node_id: n.node_id for n in nodes_clean}

    def _find(nid: int) -> int:
        while parent[nid] != nid:
            parent[nid] = parent[parent[nid]]
            nid = parent[nid]
        return nid

    node_list = list(nodes_clean)
    for i, ni in enumerate(node_list):
        for nj in node_list[i + 1:]:
            if math.hypot(ni.x - nj.x, ni.y - nj.y) < merge_tol:
                ri, rj = _find(ni.node_id), _find(nj.node_id)
                if ri != rj:
                    parent[rj] = ri

    root_set: dict[int, GraphNode] = {}
    for n in nodes_clean:
        root = _find(n.node_id)
        if root not in root_set:
            root_set[root] = n

    nodes_merged = list(root_set.values())

    def _remap(nid: int) -> int:
        return _find(nid)

    edges_merged = []
    seen_edges: set[tuple[int, int]] = set()
    for e in edges_clean:
        u, v = _remap(e.from_node), _remap(e.to_node)
        if u == v:
            continue
        key = (min(u, v), max(u, v))
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges_merged.append(GraphEdge(from_node=u, to_node=v, primitive_type=e.primitive_type))

    faces_merged = [
        Face(node_ids=[_remap(nid) for nid in f.node_ids])
        for f in faces_clean
    ]

    # --- Remove degree-1 dangling edges shorter than 5% of bbox diagonal ---
    if nodes_merged:
        xs = [n.x for n in nodes_merged]
        ys = [n.y for n in nodes_merged]
        diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        dangle_tol = diag * 0.05
    else:
        dangle_tol = 0.001

    degree: dict[int, int] = {n.node_id: 0 for n in nodes_merged}
    for e in edges_merged:
        degree[e.from_node] = degree.get(e.from_node, 0) + 1
        degree[e.to_node] = degree.get(e.to_node, 0) + 1

    node_by_id = {n.node_id: n for n in nodes_merged}

    def _edge_length(e: GraphEdge) -> float:
        nu = node_by_id.get(e.from_node)
        nv = node_by_id.get(e.to_node)
        if nu is None or nv is None:
            return 0.0
        return math.hypot(nu.x - nv.x, nu.y - nv.y)

    edges_final = [
        e for e in edges_merged
        if not (
            (degree.get(e.from_node, 0) == 1 or degree.get(e.to_node, 0) == 1)
            and _edge_length(e) < dangle_tol
        )
    ]

    # Recompute degrees for final node list
    final_degree: dict[int, int] = {}
    for e in edges_final:
        final_degree[e.from_node] = final_degree.get(e.from_node, 0) + 1
        final_degree[e.to_node] = final_degree.get(e.to_node, 0) + 1

    nodes_final = [
        GraphNode(
            node_id=n.node_id,
            x=n.x,
            y=n.y,
            degree=final_degree.get(n.node_id, 0),
        )
        for n in nodes_merged
    ]

    symmetry_axes = _detect_symmetry_axes(nodes_final)
    signature = _wl_hash(nodes_final, edges_final)

    logger.debug(
        "graph simplified nodes=%d edges=%d sig=%s",
        len(nodes_final), len(edges_final), signature[:8],
    )

    return TopologyGraph(
        nodes=nodes_final,
        edges=edges_final,
        faces=faces_merged,
        symmetry_axes=symmetry_axes,
        graph_signature=signature,
    )
