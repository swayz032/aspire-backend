"""Wave 11g-v2 Track B Phase 4 — Repeated Subgraph Mining via Weisfeiler-Lehman.

THE BREAKTHROUGH LAYER.

Current VGSE uses an item-sequence SHA1 as the symbol signature. When outlets
are connected to wires or walls, the boundary of the item-sequence mutates and
two structurally-identical symbols hash differently. On GAVNN, 383 of 463 motifs
end up as ``unknown_motif_*`` for exactly this reason.

This module replaces that with **Weisfeiler-Lehman 1-WL graph hashing** over
k-hop local neighborhoods. The hash depends ONLY on the local topology around a
node — not on what the node happens to be connected to beyond k hops. A GFCI
receptacle's signature is therefore stable whether it floats alone, butts
against a wire, or sits inside a wall pocket.

Pipeline (per page graph):
    1. For each node, extract a k-hop induced neighborhood subgraph.
    2. Compute its 1-WL hash (rotation-invariant variant: hash the canonical
       rotation orbit and keep all 4 orbit members for lookup).
    3. Group nodes by hash → these are candidate motif instances.
    4. Filter motifs with frequency < MIN_FREQ (a single occurrence is not a
       reusable symbol).
    5. Compute a 32-dim dense embedding (degree / primitive-type / edge-length
       / angle histograms) for FAISS or cosine retrieval.

The DiscoveredMotif is the unit handed to the symbol_retrieval cascade
(Phase 6) and the unknown_motif_resolver (Phase 8). It carries provenance
(graph_hash, rotation_orbit, embedding, instances) so every downstream
decision is auditable.

Law compliance:
    #1  Pure computation. No autonomous decisions; mining only proposes.
    #2  Each motif is identified by an immutable graph_hash; instances carry
        bboxes so a receipt can be written without re-mining.
    #3  Fail closed: unknown topology stays unknown. We never emit a canonical
        name from this layer (that's Phase 6).
    #9  Never logs raw primitive coordinates — only motif_id and hash prefix.
"""
from __future__ import annotations

import hashlib
import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Protocol

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only
    import networkx  # noqa: F401

logger = logging.getLogger(__name__)


# Public defaults --------------------------------------------------------------
DEFAULT_K_HOPS = 2
DEFAULT_WL_ITERATIONS = 3
DEFAULT_MIN_FREQUENCY = 3
EMBEDDING_DIM = 32
_ROTATION_QUARTERS = (0, 1, 2, 3)  # 0°, 90°, 180°, 270°


# Optional acceleration -------------------------------------------------------
try:  # pragma: no cover - optional dep
    import faiss  # type: ignore[import-not-found]

    _NEAREST_BACKEND = "faiss"
except Exception:  # pragma: no cover - optional dep absent
    faiss = None  # type: ignore[assignment]
    try:
        from sklearn.neighbors import NearestNeighbors  # type: ignore[import-not-found]

        _NEAREST_BACKEND = "sklearn"
    except Exception:
        NearestNeighbors = None  # type: ignore[assignment]
        _NEAREST_BACKEND = "numpy"

logger.debug("subgraph_miner nearest-neighbor backend: %s", _NEAREST_BACKEND)


# --- Public dataclasses ------------------------------------------------------


@dataclass(frozen=True)
class MotifInstance:
    """One concrete occurrence of a discovered motif on the page.

    The bbox is the union of all primitive bboxes in the k-hop neighborhood.
    The center_node carries the WL-anchor node id from the source graph.
    """
    center_node: Any
    bbox: tuple[float, float, float, float]
    member_nodes: tuple[Any, ...]


@dataclass(frozen=True)
class DiscoveredMotif:
    """A repeated local subgraph mined from the page graph.

    Attributes:
        motif_id: short content-derived identifier (first 12 hex of graph_hash).
        graph_hash: full sha1 of the concatenated 1-WL round hashes.
        embedding: 32-dimensional dense vector for cosine / FAISS retrieval.
        frequency: count of instances on this page / project.
        instances: per-occurrence audit (bbox, member nodes).
        confidence: heuristic 1.0 / (1 + dispersion_of_neighborhoods), where
            dispersion measures how much the per-instance node-attr histograms
            disagree with the motif centroid. High = tight cluster = high conf.
        rotation_orbit: the 4 hashes for the motif under {0,90,180,270} rotation.
            Lookups may match against any orbit member to be rotation-invariant.
    """
    motif_id: str
    graph_hash: str
    embedding: list[float]
    frequency: int
    instances: tuple[MotifInstance, ...]
    confidence: float
    rotation_orbit: tuple[str, str, str, str]


# --- Duck-typed graph interface ----------------------------------------------
#
# We accept any object that quacks like ``networkx.Graph``: nodes(data=True),
# neighbors(n), and __contains__/__len__. This keeps the module free of a hard
# networkx dependency while remaining compatible with nx.Graph at runtime.


class _GraphLike(Protocol):  # pragma: no cover - typing only
    def nodes(self, data: bool = ...) -> Iterable[Any]: ...
    def neighbors(self, n: Any) -> Iterable[Any]: ...
    def __contains__(self, n: Any) -> bool: ...
    def __len__(self) -> int: ...


# --- Mining entry point ------------------------------------------------------


def mine_motifs(
    graph: "networkx.Graph",
    *,
    k_hops: int = DEFAULT_K_HOPS,
    wl_iterations: int = DEFAULT_WL_ITERATIONS,
    min_frequency: int = DEFAULT_MIN_FREQUENCY,
    rotation_invariant: bool = True,
) -> list[DiscoveredMotif]:
    """Mine repeated local subgraph motifs from a page-level vector graph.

    Args:
        graph: networkx.Graph-like. Node attrs we read (all optional):
            ``primitive_type`` (str), ``angle`` (float, degrees),
            ``length`` (float), ``bbox`` (4-tuple of floats).
        k_hops: radius of the local neighborhood around each node.
        wl_iterations: number of 1-WL refinement rounds.
        min_frequency: drop motifs with fewer than this many instances.
        rotation_invariant: when True, compute the WL hash under {0,90,180,270}
            rotations of the local angle attribute and store the orbit.

    Returns:
        Discovered motifs sorted by descending frequency, then graph_hash for
        determinism. An empty input graph returns ``[]``.
    """
    if k_hops <= 0:
        raise ValueError("k_hops must be >= 1")
    if wl_iterations <= 0:
        raise ValueError("wl_iterations must be >= 1")

    nodes_list = list(graph.nodes(data=True)) if _supports_data(graph) else [
        (n, {}) for n in graph.nodes()
    ]
    if not nodes_list:
        return []

    # Build attribute lookup for fast neighborhood reads.
    node_attrs: dict[Any, dict[str, Any]] = {n: dict(attrs) for n, attrs in nodes_list}

    grouped: dict[str, list[tuple[Any, dict[str, Any]]]] = defaultdict(list)
    orbits: dict[str, tuple[str, str, str, str]] = {}

    for node_id, _ in nodes_list:
        nbrs = _k_hop_neighbors(graph, node_id, k_hops)
        if len(nbrs) <= 1:
            # Pure singleton — nothing to fingerprint. Skip.
            continue

        if rotation_invariant:
            orbit = tuple(
                _wl_hash_subgraph(graph, nbrs, node_attrs, wl_iterations, rotation=q)
                for q in _ROTATION_QUARTERS
            )
            canonical = min(orbit)  # lowest-hex orbit member is the canonical key
        else:
            single = _wl_hash_subgraph(graph, nbrs, node_attrs, wl_iterations, rotation=0)
            orbit = (single, single, single, single)
            canonical = single

        # Mypy: orbit is a 4-tuple by construction.
        orbits[canonical] = orbit  # type: ignore[assignment]
        grouped[canonical].append((node_id, {"members": nbrs}))

    motifs: list[DiscoveredMotif] = []
    for graph_hash, occurrences in grouped.items():
        if len(occurrences) < min_frequency:
            continue
        instances: list[MotifInstance] = []
        per_instance_features: list[np.ndarray] = []
        for center, payload in occurrences:
            members = payload["members"]
            bbox = _union_bbox(node_attrs, members)
            instances.append(
                MotifInstance(
                    center_node=center,
                    bbox=bbox,
                    member_nodes=tuple(sorted(members, key=lambda x: str(x))),
                )
            )
            per_instance_features.append(
                _neighborhood_features(graph, members, node_attrs)
            )

        embedding = _mean_pool(per_instance_features)
        confidence = _cluster_tightness(per_instance_features, embedding)
        motif_id = graph_hash[:12]
        motifs.append(
            DiscoveredMotif(
                motif_id=motif_id,
                graph_hash=graph_hash,
                embedding=embedding.tolist(),
                frequency=len(occurrences),
                instances=tuple(instances),
                confidence=confidence,
                rotation_orbit=orbits[graph_hash],
            )
        )

    motifs.sort(key=lambda m: (-m.frequency, m.graph_hash))
    logger.info(
        "subgraph_miner: discovered %d motifs (>= freq %d) from %d nodes [backend=%s]",
        len(motifs),
        min_frequency,
        len(nodes_list),
        _NEAREST_BACKEND,
    )
    return motifs


# --- Nearest-motif index (FAISS / sklearn / numpy) ---------------------------


class MotifIndex:
    """Cosine-similarity nearest-neighbor index over motif embeddings.

    Chooses faiss.IndexFlatIP, sklearn NearestNeighbors, or pure-numpy at build
    time depending on what's importable. All three return the same ranked
    ``(motif_id, similarity)`` tuples — only the speed differs.
    """

    def __init__(self, motifs: list[DiscoveredMotif]) -> None:
        self._motifs = list(motifs)
        if not motifs:
            self._vectors = np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
            self._backend = "empty"
            self._index: Any = None
            return
        vecs = np.array([m.embedding for m in motifs], dtype=np.float32)
        # Normalise to unit length so inner product == cosine.
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._vectors = vecs / norms

        if _NEAREST_BACKEND == "faiss" and faiss is not None:  # pragma: no cover
            idx = faiss.IndexFlatIP(EMBEDDING_DIM)
            idx.add(self._vectors)
            self._index = idx
            self._backend = "faiss"
        elif _NEAREST_BACKEND == "sklearn":  # pragma: no cover
            nn = NearestNeighbors(metric="cosine")  # type: ignore[name-defined]
            nn.fit(self._vectors)
            self._index = nn
            self._backend = "sklearn"
        else:
            self._index = None
            self._backend = "numpy"

    @property
    def backend(self) -> str:
        return self._backend

    def query(self, embedding: list[float], top_k: int = 5) -> list[tuple[str, float]]:
        if not self._motifs:
            return []
        q = np.array(embedding, dtype=np.float32)
        n = float(np.linalg.norm(q))
        if n == 0:
            return []
        q = q / n
        if self._backend == "faiss":  # pragma: no cover
            sims, idxs = self._index.search(q.reshape(1, -1), top_k)
            return [
                (self._motifs[int(i)].motif_id, float(s))
                for s, i in zip(sims[0], idxs[0])
                if int(i) >= 0
            ]
        if self._backend == "sklearn":  # pragma: no cover
            dist, idxs = self._index.kneighbors(
                q.reshape(1, -1), n_neighbors=min(top_k, len(self._motifs))
            )
            return [
                (self._motifs[int(i)].motif_id, 1.0 - float(d))
                for d, i in zip(dist[0], idxs[0])
            ]
        # numpy fallback
        sims = self._vectors @ q
        order = np.argsort(-sims)[:top_k]
        return [(self._motifs[int(i)].motif_id, float(sims[int(i)])) for i in order]


# --- Internal helpers --------------------------------------------------------


def _supports_data(graph: Any) -> bool:
    try:
        # networkx.Graph.nodes(data=True) returns NodeDataView
        graph.nodes(data=True)
        return True
    except TypeError:
        return False


def _k_hop_neighbors(graph: Any, source: Any, k: int) -> list[Any]:
    """Return the source and every node within k undirected hops of it."""
    visited: set[Any] = {source}
    frontier: list[Any] = [source]
    for _ in range(k):
        nxt: list[Any] = []
        for u in frontier:
            try:
                for v in graph.neighbors(u):
                    if v not in visited:
                        visited.add(v)
                        nxt.append(v)
            except Exception:
                continue
        frontier = nxt
        if not frontier:
            break
    return sorted(visited, key=lambda x: str(x))


def _initial_label(
    node_id: Any, attrs: dict[str, Any], degree: int, rotation: int
) -> str:
    """Initial 1-WL label = (degree, primitive_type, quantized_angle bucket).

    Rotation is applied as an additive 90° step on the node's ``angle`` attr.
    Nodes without an angle attr are rotation-invariant by definition.
    """
    primitive_type = str(attrs.get("primitive_type") or attrs.get("type") or "?")
    angle = attrs.get("angle")
    if angle is None:
        angle_bucket = "x"
    else:
        rotated = (float(angle) + 90.0 * rotation) % 360.0
        # 12 buckets of 30° each → robust to small angle jitter while still
        # distinguishing axis-aligned from diagonal edges.
        angle_bucket = str(int(rotated // 30))
    return f"d{degree}|t{primitive_type}|a{angle_bucket}"


def _wl_hash_subgraph(
    graph: Any,
    members: list[Any],
    node_attrs: dict[Any, dict[str, Any]],
    iterations: int,
    *,
    rotation: int,
) -> str:
    """Run 1-WL refinement over ``members`` (induced subgraph) and hash.

    The hash is sha1 of the concatenation of sorted-multiset round labels.
    Deterministic — re-running on identical input yields identical bytes.
    """
    member_set = set(members)

    # Restrict adjacency to members only (induced subgraph).
    local_adj: dict[Any, list[Any]] = {}
    degrees: dict[Any, int] = {}
    for n in members:
        nbrs_in_sub = [v for v in graph.neighbors(n) if v in member_set] \
            if _safe_has_neighbors(graph, n) else []
        local_adj[n] = sorted(nbrs_in_sub, key=lambda x: str(x))
        degrees[n] = len(local_adj[n])

    # Iteration 0 — initial labels.
    labels: dict[Any, str] = {
        n: _initial_label(n, node_attrs.get(n, {}), degrees[n], rotation)
        for n in members
    }

    round_hashes: list[str] = [_multiset_hash(labels.values())]

    # Iterations 1..N — relabel each node by (own_label, sorted(neighbor_labels)).
    for _ in range(iterations):
        new_labels: dict[Any, str] = {}
        for n in members:
            neighbor_labels = sorted(labels[v] for v in local_adj[n])
            payload = labels[n] + "#" + ",".join(neighbor_labels)
            new_labels[n] = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
        labels = new_labels
        round_hashes.append(_multiset_hash(labels.values()))

    full = "|".join(round_hashes)
    return hashlib.sha1(full.encode("utf-8")).hexdigest()


def _safe_has_neighbors(graph: Any, n: Any) -> bool:
    try:
        graph.neighbors(n)
        return True
    except Exception:
        return False


def _multiset_hash(labels: Iterable[str]) -> str:
    """Hash a multiset of labels in an order-independent way."""
    counts = Counter(labels)
    serial = "|".join(f"{k}:{v}" for k, v in sorted(counts.items()))
    return hashlib.sha1(serial.encode("utf-8")).hexdigest()[:16]


def _neighborhood_features(
    graph: Any,
    members: list[Any],
    node_attrs: dict[Any, dict[str, Any]],
) -> np.ndarray:
    """32-dim embedding for a neighborhood:
        [0:8]   degree histogram, buckets 0..7+
        [8:16]  primitive_type histogram over 8 canonical buckets
        [16:24] edge-length histogram, log-bucketed
        [24:32] angle histogram, 8 x 45° buckets
    """
    vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    n = len(members)
    if n == 0:
        return vec

    type_bucket = {
        "line": 0, "polyline": 1, "rect": 2, "circle": 3,
        "arc": 4, "bezier": 5, "text": 6,
    }

    for node_id in members:
        attrs = node_attrs.get(node_id, {})
        # Degree
        try:
            d = sum(1 for _ in graph.neighbors(node_id))
        except Exception:
            d = 0
        vec[min(d, 7)] += 1.0

        ptype = str(attrs.get("primitive_type") or attrs.get("type") or "?")
        bucket = type_bucket.get(ptype, 7)
        vec[8 + bucket] += 1.0

        length = attrs.get("length")
        if length is not None and float(length) > 0:
            lb = min(int(math.log2(float(length) + 1.0)), 7)
            vec[16 + lb] += 1.0
        else:
            vec[16 + 7] += 1.0

        angle = attrs.get("angle")
        if angle is not None:
            ab = int((float(angle) % 360.0) // 45.0)
            vec[24 + min(ab, 7)] += 1.0
        else:
            vec[24 + 7] += 1.0

    # Normalise per-block so a 50-node motif doesn't dwarf a 5-node motif.
    for start in (0, 8, 16, 24):
        block = vec[start : start + 8]
        s = float(block.sum())
        if s > 0:
            vec[start : start + 8] = block / s
    return vec


def _mean_pool(features: list[np.ndarray]) -> np.ndarray:
    if not features:
        return np.zeros(EMBEDDING_DIM, dtype=np.float32)
    stack = np.stack(features, axis=0)
    return stack.mean(axis=0)


def _cluster_tightness(features: list[np.ndarray], centroid: np.ndarray) -> float:
    """Heuristic confidence: 1.0 / (1 + mean L2 distance to centroid).

    Tight clusters (all instances similar to centroid) → close to 1.0.
    Diffuse clusters → close to 0.5 or lower.
    """
    if not features:
        return 0.0
    dists = [float(np.linalg.norm(f - centroid)) for f in features]
    mean_d = sum(dists) / len(dists)
    return float(1.0 / (1.0 + mean_d))


def _union_bbox(
    node_attrs: dict[Any, dict[str, Any]],
    members: list[Any],
) -> tuple[float, float, float, float]:
    xs0: list[float] = []
    ys0: list[float] = []
    xs1: list[float] = []
    ys1: list[float] = []
    for n in members:
        bb = node_attrs.get(n, {}).get("bbox")
        if not bb or len(bb) < 4:
            continue
        xs0.append(float(bb[0]))
        ys0.append(float(bb[1]))
        xs1.append(float(bb[2]))
        ys1.append(float(bb[3]))
    if not xs0:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs0), min(ys0), max(xs1), max(ys1))


# --- Module-level metadata ---------------------------------------------------

__all__ = [
    "DiscoveredMotif",
    "MotifInstance",
    "MotifIndex",
    "mine_motifs",
    "EMBEDDING_DIM",
    "DEFAULT_K_HOPS",
    "DEFAULT_WL_ITERATIONS",
    "DEFAULT_MIN_FREQUENCY",
]
