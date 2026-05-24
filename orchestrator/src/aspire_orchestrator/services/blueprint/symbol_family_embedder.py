"""Wave 20.6.3a-c — Symbol Family Embedder (Stage 3: Construction Semantic Compiler).

Solves the soft-match problem: Stage 2's graph_signature is binary (match/no-match).
Real blueprints need SOFT matching — "this motif is 87% likely to be the QR-Pendant
family even though its signature differs due to drafting noise."

Consumes TopologyGraph from Stage 2 (canonical_symbolizer) and generates 64-dimensional
deterministic embedding vectors where geometrically related symbols cluster together
even when graph_signatures differ. No neural network needed — construction symbols are
vector glyphs with computable structural properties.

Embedding layout (all dims normalized to [0,1]):
  Dim  0-15: Geometry  — node/edge/face counts, edge primitive_type histogram,
             node degree stats, bbox metrics derived from node positions.
  Dim 16-31: Edge graph — degree distribution histogram (8 bins 0-7+),
             edge-type co-occurrence (line-line, line-arc, arc-arc, mixed, other).
  Dim 32-47: Topology  — Euler characteristic, Betti numbers (b0, b1), face count,
             planarity flag, genus, component count, cycle count.
  Dim 48-55: Enclosed regions — face area proxies (area quartiles, spread, coverage).
  Dim 56-63: Symmetry  — axis count, rotation order, per-axis confidence scores.

Law compliance:
  #1  Pure computation. Returns data; never decides or retries.
  #2  Persistence functions emit receipts (backfill + clustering runs).
  #3  Missing suite_id raises immediately.
  #6  All DB reads/writes scoped by suite_id.
  #9  Never logs raw coordinates, labels, or PII — only IDs and counts.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import math
import struct
from typing import Any

import numpy as np

from aspire_orchestrator.services.blueprint.canonical_symbolizer import (
    Face,
    GraphEdge,
    GraphNode,
    SymmetryAxis,
    TopologyGraph,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
    supabase_update,
)

_log = logging.getLogger(__name__)

EMBEDDING_DIM = 64

# ─────────────────────────────────────────────────────────────────────────────
# Normalisation helpers
# ─────────────────────────────────────────────────────────────────────────────

_NODE_COUNT_MAX = 200.0
_EDGE_COUNT_MAX = 400.0
_FACE_COUNT_MAX = 50.0
_DEGREE_MAX = 10.0


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b != 0.0 else default


# ─────────────────────────────────────────────────────────────────────────────
# Feature groups
# ─────────────────────────────────────────────────────────────────────────────


def _geometry_features(topo: TopologyGraph) -> np.ndarray:
    """Dims 0-15: Geometry features from node/edge/face counts and degree stats."""
    n_nodes = len(topo.nodes)
    n_edges = len(topo.edges)
    n_faces = len(topo.faces)

    degree_map: dict[int, int] = {node.node_id: node.degree for node in topo.nodes}
    degrees = list(degree_map.values())
    avg_degree = float(np.mean(degrees)) if degrees else 0.0
    max_degree = float(max(degrees)) if degrees else 0.0

    # Bbox from node positions (unit-square space post-normalization).
    xs = [n.x for n in topo.nodes]
    ys = [n.y for n in topo.nodes]
    if xs and ys:
        bbox_w = max(max(xs) - min(xs), 1e-6)
        bbox_h = max(max(ys) - min(ys), 1e-6)
    else:
        bbox_w = bbox_h = 1.0
    aspect_ratio = _clamp(bbox_w / (bbox_w + bbox_h))

    # Edge primitive type histogram.
    n_line = sum(1 for e in topo.edges if e.primitive_type == "line")
    n_arc = sum(1 for e in topo.edges if e.primitive_type == "arc")
    n_other_e = n_edges - n_line - n_arc
    ec = float(max(n_edges, 1))
    nc = float(max(n_nodes, 1))

    bbox_area = bbox_w * bbox_h
    density = _clamp(_safe_div(n_nodes, bbox_area * _NODE_COUNT_MAX))

    feats = [
        _clamp(n_nodes / _NODE_COUNT_MAX),          # dim 0
        _clamp(n_edges / _EDGE_COUNT_MAX),          # dim 1
        _clamp(n_faces / _FACE_COUNT_MAX),          # dim 2
        _clamp(n_line / ec),                        # dim 3: line edge ratio
        _clamp(n_arc / ec),                         # dim 4: arc edge ratio
        _clamp(n_other_e / ec),                     # dim 5: other edge ratio
        0.0,                                        # dim 6: reserved
        0.0,                                        # dim 7: reserved
        _clamp(avg_degree / _DEGREE_MAX),           # dim 8
        _clamp(max_degree / _DEGREE_MAX),           # dim 9
        aspect_ratio,                               # dim 10
        density,                                    # dim 11
        _clamp(n_nodes / max(n_edges, 1) / 2.0),   # dim 12: V/E ratio
        _clamp(n_faces / max(n_nodes, 1)),          # dim 13: F/V ratio
        _safe_div(n_edges, n_nodes) / _DEGREE_MAX, # dim 14: edge density
        _clamp(len([d for d in degrees if d == 1]) / nc),  # dim 15: leaf ratio
    ]
    return np.array(feats, dtype=np.float32)


def _edge_graph_features(topo: TopologyGraph) -> np.ndarray:
    """Dims 16-31: Degree distribution histogram + edge-type co-occurrence."""
    n_nodes = len(topo.nodes)
    n_edges = len(topo.edges)

    degree_map: dict[int, int] = {node.node_id: node.degree for node in topo.nodes}
    nc = float(max(n_nodes, 1))
    deg_hist = [0.0] * 8
    for d in degree_map.values():
        deg_hist[min(d, 7)] += 1.0 / nc

    node_edge_types: dict[int, list[str]] = {n.node_id: [] for n in topo.nodes}
    for edge in topo.edges:
        node_edge_types.setdefault(edge.from_node, []).append(edge.primitive_type)
        node_edge_types.setdefault(edge.to_node, []).append(edge.primitive_type)

    pair_counts = {"ll": 0, "la": 0, "aa": 0, "mixed": 0, "other": 0}
    for edge in topo.edges:
        all_types = set(node_edge_types.get(edge.from_node, [])) | set(node_edge_types.get(edge.to_node, []))
        has_line = "line" in all_types
        has_arc = "arc" in all_types
        if has_line and not has_arc:
            pair_counts["ll"] += 1
        elif has_arc and not has_line:
            pair_counts["aa"] += 1
        elif has_line and has_arc:
            pair_counts["la"] += 1
        elif edge.primitive_type in ("line", "arc"):
            pair_counts["mixed"] += 1
        else:
            pair_counts["other"] += 1

    ec = float(max(n_edges, 1))
    edge_type_feats = [
        _clamp(pair_counts["ll"] / ec),
        _clamp(pair_counts["la"] / ec),
        _clamp(pair_counts["aa"] / ec),
        _clamp(pair_counts["mixed"] / ec),
        _clamp(pair_counts["other"] / ec),
        0.0, 0.0, 0.0,
    ]

    return np.array(deg_hist + edge_type_feats, dtype=np.float32)


def _topology_features(topo: TopologyGraph) -> np.ndarray:
    """Dims 32-47: Topological invariants."""
    V = len(topo.nodes)
    E = len(topo.edges)
    F = len(topo.faces)

    euler = V - E + F
    euler_norm = _clamp((euler + 20) / 40.0)

    b0 = _count_components(topo)
    b1 = max(0, E - V + b0)
    is_planar = 1.0 if (V < 3 or E <= 3 * V - 6) else 0.0
    genus_norm = _clamp(max(0.0, 1.0 - (V - E + F) / 2.0) / 10.0)

    feats = [
        euler_norm,                      # dim 32
        _clamp(b0 / 20.0),              # dim 33
        _clamp(b1 / 50.0),              # dim 34
        _clamp(b1 / 50.0),              # dim 35
        is_planar,                       # dim 36
        genus_norm,                      # dim 37
        _clamp(V / _NODE_COUNT_MAX),    # dim 38
        _clamp(E / _EDGE_COUNT_MAX),    # dim 39
        _clamp(F / _FACE_COUNT_MAX),    # dim 40
        _clamp(b0 / max(V, 1)),         # dim 41
        0.0, 0.0, 0.0, 0.0, 0.0, 0.0,  # dims 42-47: reserved
    ]
    return np.array(feats, dtype=np.float32)


def _region_features(topo: TopologyGraph) -> np.ndarray:
    """Dims 48-55: Face/region analysis via shoelace on node positions."""
    if not topo.faces:
        return np.zeros(8, dtype=np.float32)

    node_pos: dict[int, tuple[float, float]] = {n.node_id: (n.x, n.y) for n in topo.nodes}
    areas: list[float] = []
    for face in topo.faces:
        pts = [node_pos[nid] for nid in face.node_ids if nid in node_pos]
        if len(pts) < 3:
            if len(pts) == 2:
                areas.append(math.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]))
            continue
        area = 0.0
        n = len(pts)
        for i in range(n):
            j = (i + 1) % n
            area += pts[i][0] * pts[j][1] - pts[j][0] * pts[i][1]
        areas.append(abs(area) / 2.0)

    if not areas:
        return np.zeros(8, dtype=np.float32)

    areas_sorted = sorted(areas)
    total_area = sum(areas_sorted)

    q_area = [_clamp(float(np.percentile(areas_sorted, p))) for p in (25, 50, 75, 100)]

    if len(areas_sorted) > 1:
        area_std = _clamp(float(np.std(areas_sorted)))
        mean_a = float(np.mean(areas_sorted))
        std_a = float(np.std(areas_sorted))
        skew_raw = float(np.mean([(a - mean_a) ** 3 for a in areas_sorted])) / (std_a ** 3 + 1e-9)
        area_skew = _clamp((skew_raw + 3) / 6.0)
    else:
        area_std = 0.0
        area_skew = 0.5

    q_spread = [
        area_std,
        area_skew,
        _clamp(len(areas) / _FACE_COUNT_MAX),
        _clamp(total_area),
    ]

    return np.array(q_area + q_spread, dtype=np.float32)


def _symmetry_features(topo: TopologyGraph) -> np.ndarray:
    """Dims 56-63: Symmetry from TopologyGraph.symmetry_axes."""
    axis_conf: dict[str, float] = {ax.axis: ax.confidence for ax in topo.symmetry_axes}
    v = axis_conf.get("vertical", 0.0)
    h = axis_conf.get("horizontal", 0.0)
    dp = axis_conf.get("diagonal_pos", 0.0)
    dn = axis_conf.get("diagonal_neg", 0.0)

    axis_count = sum(1 for c in [v, h, dp, dn] if c >= 0.5)
    if axis_count >= 4:
        rot_order = 1.0
    elif axis_count >= 2:
        rot_order = 0.5
    elif axis_count == 1:
        rot_order = 0.25
    else:
        rot_order = 0.0

    mirror_flag = 1.0 if (v >= 0.5 or h >= 0.5) else 0.0
    reflective_score = _clamp((v + h) / 2.0)

    feats = [
        _clamp(axis_count / 4.0),  # dim 56
        rot_order,                 # dim 57
        mirror_flag,               # dim 58
        reflective_score,          # dim 59
        _clamp(v),                 # dim 60
        _clamp(h),                 # dim 61
        _clamp(dp),                # dim 62
        _clamp(dn),                # dim 63
    ]
    return np.array(feats, dtype=np.float32)


def _count_components(topo: TopologyGraph) -> int:
    """Union-Find connected component count."""
    if not topo.nodes:
        return 0
    parent: dict[int, int] = {n.node_id: n.node_id for n in topo.nodes}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for edge in topo.edges:
        if edge.from_node in parent and edge.to_node in parent:
            union(edge.from_node, edge.to_node)

    return len({find(n.node_id) for n in topo.nodes})


# ─────────────────────────────────────────────────────────────────────────────
# Public API — Commit 20.6.3a
# ─────────────────────────────────────────────────────────────────────────────


def compute_embedding(topology: TopologyGraph) -> np.ndarray:
    """Generate a 64-dimensional embedding vector for a TopologyGraph.

    Input: TopologyGraph from canonical_symbolizer.extract_topology().
    Deterministic — same input → same output. All dims in [0,1].
    Returns np.ndarray of shape (64,), dtype=float32.
    """
    embedding = np.concatenate([
        _geometry_features(topology),    # dims 0-15
        _edge_graph_features(topology),  # dims 16-31
        _topology_features(topology),    # dims 32-47
        _region_features(topology),      # dims 48-55
        _symmetry_features(topology),    # dims 56-63
    ]).astype(np.float32)
    assert embedding.shape == (EMBEDDING_DIM,)
    return np.clip(embedding, 0.0, 1.0)


def cosine_similarity(emb_a: np.ndarray, emb_b: np.ndarray) -> float:
    """Cosine similarity between two embedding vectors. Returns value in [-1, 1]."""
    na = float(np.linalg.norm(emb_a))
    nb = float(np.linalg.norm(emb_b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(emb_a, emb_b) / (na * nb))


# ─────────────────────────────────────────────────────────────────────────────
# Serialization — Commit 20.6.3b
# ─────────────────────────────────────────────────────────────────────────────


def serialize_embedding(embedding: np.ndarray) -> str:
    """Serialize float32 embedding to base64 string for DB storage."""
    assert embedding.dtype == np.float32
    return base64.b64encode(struct.pack(f"{EMBEDDING_DIM}f", *embedding.tolist())).decode("ascii")


def deserialize_embedding(encoded: str) -> np.ndarray:
    """Deserialize base64 string back to float32 numpy array."""
    return np.array(struct.unpack(f"{EMBEDDING_DIM}f", base64.b64decode(encoded)), dtype=np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# Topology construction from bbox (persistence bridge for Stage 1 legend rows)
# ─────────────────────────────────────────────────────────────────────────────


def _build_topology_from_bbox(bbox_raw: Any) -> TopologyGraph | None:
    """Construct a minimal TopologyGraph from a representation_bbox.

    Used by populate_vector_signature when extract_topology() hasn't been run.
    Produces a rectangle topology: 4 corner nodes + 4 perimeter line edges + 1 face.
    Aspect ratio drives symmetry axis confidence.
    Returns None if bbox is missing or malformed.
    """
    try:
        if isinstance(bbox_raw, (list, tuple)) and len(bbox_raw) == 4:
            x0, y0, x1, y1 = (float(v) for v in bbox_raw)
        elif isinstance(bbox_raw, (list, tuple)) and len(bbox_raw) == 8:
            # Azure prebuilt-layout polygon format: [x1,y1,x2,y2,x3,y3,x4,y4]
            xs = [float(bbox_raw[i]) for i in (0, 2, 4, 6)]
            ys = [float(bbox_raw[i]) for i in (1, 3, 5, 7)]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        elif isinstance(bbox_raw, dict):
            vals = list(bbox_raw.values())
            if len(vals) == 4:
                x0, y0, x1, y1 = (float(v) for v in vals)
            else:
                return None
        else:
            return None
    except (TypeError, ValueError):
        return None

    w = max(x1 - x0, 1e-6)
    h = max(y1 - y0, 1e-6)
    aspect = w / h

    nodes = [
        GraphNode(node_id=0, x=0.0, y=0.0, degree=2),
        GraphNode(node_id=1, x=1.0, y=0.0, degree=2),
        GraphNode(node_id=2, x=1.0, y=1.0, degree=2),
        GraphNode(node_id=3, x=0.0, y=1.0, degree=2),
    ]
    edges = [
        GraphEdge(from_node=0, to_node=1, primitive_type="line"),
        GraphEdge(from_node=1, to_node=2, primitive_type="line"),
        GraphEdge(from_node=2, to_node=3, primitive_type="line"),
        GraphEdge(from_node=3, to_node=0, primitive_type="line"),
    ]
    symmetry_axes = [
        SymmetryAxis(axis="vertical", confidence=_clamp(1.0 - abs(1.0 - aspect) * 0.3)),
        SymmetryAxis(axis="horizontal", confidence=_clamp(1.0 - abs(1.0 - 1.0 / aspect) * 0.3)),
    ]

    return TopologyGraph(
        nodes=nodes,
        edges=edges,
        faces=[Face(node_ids=[0, 1, 2, 3])],
        symmetry_axes=symmetry_axes,
        graph_signature=hashlib.sha1(f"{aspect:.4f}".encode()).hexdigest(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Persistence — Commit 20.6.3b
# ─────────────────────────────────────────────────────────────────────────────

_TABLE = "blueprint_project_legends"


async def populate_vector_signature(*, project_id: str, suite_id: str) -> dict[str, Any]:
    """Compute + persist vector_signature for legend rows with NULL signature.

    Reads rows with IS NULL vector_signature, builds topology from representation_bbox,
    computes 64-dim embedding, serializes to base64, and writes back.
    Returns {rows_backfilled, rows_skipped, errors}.
    """
    if not suite_id:
        raise ValueError("populate_vector_signature requires non-empty suite_id (Law #6)")

    rows = await supabase_select(
        _TABLE,
        filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}&vector_signature=is.null",
    )

    rows_backfilled = rows_skipped = errors = 0

    for row in (rows or []):
        row_id = row.get("id")
        if not row_id:
            rows_skipped += 1
            continue
        topo = _build_topology_from_bbox(row.get("representation_bbox"))
        if topo is None:
            rows_skipped += 1
            continue
        try:
            encoded = serialize_embedding(compute_embedding(topo))
            await supabase_update(
                _TABLE,
                f"id=eq.{row_id}&suite_id=eq.{suite_id}",
                {"vector_signature": encoded},
            )
            rows_backfilled += 1
        except SupabaseClientError as exc:
            _log.warning("symbol_family_embedder: DB update failed row=%s %s", str(row_id)[:8], type(exc).__name__)
            errors += 1
        except Exception as exc:  # noqa: BLE001
            _log.warning("symbol_family_embedder: embedding failed row=%s %s", str(row_id)[:8], type(exc).__name__)
            errors += 1

    _log.info(
        "symbol_family_embedder.backfill: project=%s suite=%s backfilled=%d skipped=%d errors=%d",
        project_id[:8], suite_id[:8], rows_backfilled, rows_skipped, errors,
    )
    return {"rows_backfilled": rows_backfilled, "rows_skipped": rows_skipped, "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
# Motif embedding population — Pass 3.2 (closes Wave 20.6 → Stage 5 gap)
# ─────────────────────────────────────────────────────────────────────────────


async def populate_motif_embeddings(
    *,
    project_id: str,
    suite_id: str,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Compute + persist embedding bytea for blueprint_symbols motif rows.

    Mirrors populate_vector_signature but for the blueprint_symbols table (motifs from
    Wave 11g-v2 SEE stage) instead of blueprint_project_legends (Stage 1 extracts).

    Required for Stage 5 cluster_motifs_on_project — it reads blueprint_symbols.embedding
    bytea. Without this population, the entire Wave 20.6 → resolved_symbols chain stops at
    Stage 4 (motifs detected but no embeddings → no clustering → no legend bindings).

    blueprint_symbols.embedding is bytea — store raw float32 bytes (not base64) to match
    Wave 20.6.5a migration column type.

    Law #6: scoped by suite_id; resolves project's sheet_ids first then filters motifs by
    sheet_id IN (...). Cross-tenant query returns 0 rows.

    Returns: {motifs_total, embedded, skipped_no_bbox, errors}.
    """
    if not suite_id:
        raise ValueError("populate_motif_embeddings requires non-empty suite_id (Law #6)")

    # Get all sheet_ids for this project (blueprint_symbols has no project_id column).
    sheet_rows = await supabase_select(
        "blueprint_sheets",
        filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        select="id",
    )
    if not sheet_rows:
        return {"motifs_total": 0, "embedded": 0, "skipped_no_bbox": 0, "errors": 0}
    sheet_ids = [r["id"] for r in sheet_rows]
    sheet_filter = ",".join(sheet_ids)

    # Fetch motif rows lacking embedding. PostgREST IN syntax: sheet_id=in.(id1,id2,...)
    rows = await supabase_select(
        "blueprint_symbols",
        filters=f"sheet_id=in.({sheet_filter})&suite_id=eq.{suite_id}&embedding=is.null",
        select="id,bbox",
        limit=batch_size * 100,  # cap upper bound
    )

    motifs_total = len(rows or [])
    embedded = skipped_no_bbox = errors = 0

    for row in (rows or []):
        row_id = row.get("id")
        if not row_id:
            skipped_no_bbox += 1
            continue
        topo = _build_topology_from_bbox(row.get("bbox"))
        if topo is None:
            skipped_no_bbox += 1
            continue
        try:
            emb = compute_embedding(topo)
            # bytea: raw float32 bytes (NOT base64). Stage 5 reads via deserialize_motif_embedding.
            raw_bytes = struct.pack(f"{EMBEDDING_DIM}f", *emb.tolist())
            # Supabase REST API needs base64-encoded bytea on input
            encoded = "\\x" + raw_bytes.hex()
            await supabase_update(
                "blueprint_symbols",
                f"id=eq.{row_id}&suite_id=eq.{suite_id}",
                {"embedding": encoded},
            )
            embedded += 1
        except SupabaseClientError as exc:
            _log.warning(
                "populate_motif_embeddings: DB update failed row=%s %s",
                str(row_id)[:8],
                type(exc).__name__,
            )
            errors += 1
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "populate_motif_embeddings: embedding failed row=%s %s",
                str(row_id)[:8],
                type(exc).__name__,
            )
            errors += 1

    _log.info(
        "populate_motif_embeddings: project=%s suite=%s total=%d embedded=%d skipped=%d errors=%d",
        project_id[:8],
        suite_id[:8],
        motifs_total,
        embedded,
        skipped_no_bbox,
        errors,
    )
    return {
        "motifs_total": motifs_total,
        "embedded": embedded,
        "skipped_no_bbox": skipped_no_bbox,
        "errors": errors,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Clustering — Commit 20.6.3c
# ─────────────────────────────────────────────────────────────────────────────


async def cluster_legend_into_families(
    *,
    project_id: str,
    suite_id: str,
    similarity_threshold: float = 0.92,
) -> dict[str, list[str]]:
    """Group legend symbols into families by embedding cosine similarity.

    Pairs within the same discipline are compared. Union-find handles transitivity.
    Updates blueprint_project_legends.family_id. Returns {family_id: [row_id, ...]}.
    Only families with >= 2 members are returned.

    Law #1: Never decides symbol names — groups by geometry only.
    Law #6: All reads/writes scoped by suite_id.
    """
    if not suite_id:
        raise ValueError("cluster_legend_into_families requires non-empty suite_id (Law #6)")

    rows = await supabase_select(
        _TABLE,
        filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    if not rows:
        return {}

    by_discipline: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_discipline.setdefault(str(row.get("discipline") or "unknown"), []).append(row)

    parent: dict[str, str] = {str(row["id"]): str(row["id"]) for row in rows if row.get("id")}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent.get(x, x), x)
            x = parent.get(x, x)
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    embeddings: dict[str, np.ndarray] = {}
    for row in rows:
        rid = str(row.get("id") or "")
        vs = row.get("vector_signature")
        if rid and vs:
            try:
                embeddings[rid] = deserialize_embedding(str(vs))
            except Exception:  # noqa: BLE001
                pass

    for disc_rows in by_discipline.values():
        ids = [str(r["id"]) for r in disc_rows if r.get("id") and str(r["id"]) in embeddings]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if cosine_similarity(embeddings[ids[i]], embeddings[ids[j]]) >= similarity_threshold:
                    union(ids[i], ids[j])

    root_to_fid: dict[str, str] = {}
    families: dict[str, list[str]] = {}
    for row in rows:
        rid = str(row.get("id") or "")
        if not rid or rid not in parent:
            continue
        root = find(rid)
        if root not in root_to_fid:
            root_to_fid[root] = "fam_" + hashlib.sha1(root.encode()).hexdigest()[:12]
            families[root_to_fid[root]] = []
        families[root_to_fid[root]].append(rid)

    for fid, member_ids in families.items():
        if len(member_ids) < 2:
            continue
        for rid in member_ids:
            try:
                await supabase_update(
                    _TABLE,
                    f"id=eq.{rid}&suite_id=eq.{suite_id}",
                    {"family_id": fid},
                )
            except SupabaseClientError as exc:
                _log.warning("symbol_family_embedder: family_id update failed %s %s", rid[:8], type(exc).__name__)

    grouped = {fid: ids for fid, ids in families.items() if len(ids) >= 2}
    _log.info(
        "symbol_family_embedder.cluster: project=%s suite=%s families=%d rows=%d threshold=%.2f",
        project_id[:8], suite_id[:8], len(grouped), len(rows), similarity_threshold,
    )
    return grouped


__all__ = [
    "EMBEDDING_DIM",
    "compute_embedding",
    "cosine_similarity",
    "serialize_embedding",
    "deserialize_embedding",
    "populate_vector_signature",
    "populate_motif_embeddings",
    "cluster_legend_into_families",
]
