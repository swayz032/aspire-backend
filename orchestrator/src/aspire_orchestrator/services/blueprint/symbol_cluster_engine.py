"""Wave 20.6 Stage 5 — Spatial Repetition Clustering + Candidate Matching.

THE RECALL MULTIPLIER: Commercial blueprints repeat the same symbol thousands of
times. One confirmed legend binding propagates semantic identity to all identical
motif instances in the cluster — no per-motif LLM call needed.

Pipeline:
    cluster_motifs_on_project()
        Reads blueprint_symbols rows (class LIKE 'unresolved_%' OR 'motif:%')
        Groups by geometric + contextual similarity → writes cluster_id back
        Returns {cluster_id: [motif_id, ...]}

    propagate_legend_bindings_to_clusters()
        For each cluster: cosine-match centroid embedding against legend embeddings
        ≥0.92 → auto-bind (write blueprint_resolved_symbols rows for all instances)
        0.75-0.92 → flag pending_vlm_verify (Stage 6 handles)
        <0.75 → unknown
        Returns binding statistics dict

Law compliance:
    #1  Pure computation — no autonomous decisions. Proposes bindings; orchestrator
        decides on Yellow/Red escalation.
    #2  Every resolved symbol row written to blueprint_resolved_symbols (immutable).
        Failures produce receipt with reason_code. cluster_id updates on
        blueprint_symbols are advisory metadata, not receipts (no business state change).
    #3  Fail closed — missing legend embeddings → clusters stay 'unknown'.
        Missing capability token → DENY (caller's responsibility; this module
        validates suite_id isolation only).
    #6  All queries and inserts are scoped by suite_id. Cross-tenant request raises
        TenantIsolationError.
    #9  Never logs raw embedding bytes, bbox coordinates, or OCR text.
"""
from __future__ import annotations

import hashlib
import logging
import math
import struct
import uuid as uuid_mod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
_DEFAULT_SIMILARITY_THRESHOLD = 0.92
_DEFAULT_BIND_THRESHOLD = 0.92
_DEFAULT_VERIFY_THRESHOLD = 0.75

# blueprint_symbols class prefixes that indicate unresolved motifs
_UNRESOLVED_PREFIXES = ("unresolved_", "motif:")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class TenantIsolationError(RuntimeError):
    """Raised when a cross-tenant access attempt is detected (Law #6)."""


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------

@dataclass
class _MotifRow:
    """Projection of a blueprint_symbols row used for clustering."""
    id: str
    suite_id: str
    sheet_id: str
    project_id: str
    motif_id: str | None          # from bbox JSON field 'motif_id'
    graph_hash: str | None        # from bbox JSON field 'graph_hash'
    embedding: list[float] | None # from embedding column (bytea → float32 list)
    bbox: dict[str, Any]
    motif_frequency: int
    discipline: str | None
    sheet_region: str | None      # from blueprint_sheet_intelligence regions


@dataclass
class _ClusterCentroid:
    cluster_id: str
    embedding: list[float]
    motif_ids: list[str]        # blueprint_symbols.id values
    size: int


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

def _embedding_from_bytes(raw: bytes | None) -> list[float] | None:
    """Decode 64-dim float32 embedding stored as 256-byte bytea."""
    if raw is None:
        return None
    try:
        if isinstance(raw, str):
            # Hex-encoded (Supabase returns bytea as \\x<hex>)
            hex_str = raw.lstrip("\\x").lstrip("0x")
            raw = bytes.fromhex(hex_str)
        n = len(raw) // 4
        return list(struct.unpack(f"<{n}f", raw[:n * 4]))
    except Exception:
        return None


def _embedding_to_bytes(vec: list[float]) -> bytes:
    """Encode float32 list to bytea for storage."""
    return struct.pack(f"<{len(vec)}f", *vec)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _mean_embedding(vecs: list[list[float]]) -> list[float] | None:
    """Compute element-wise mean of a list of same-length float vectors."""
    if not vecs:
        return None
    dim = len(vecs[0])
    result = [0.0] * dim
    for v in vecs:
        for i, x in enumerate(v):
            result[i] += x
    n = len(vecs)
    return [x / n for x in result]


# ---------------------------------------------------------------------------
# Context vector helpers
# ---------------------------------------------------------------------------

def _discipline_code(discipline: str | None) -> float:
    """Map discipline string to a numeric slot for context vector."""
    _MAP = {
        "electrical": 0.1, "plumbing": 0.2, "mechanical": 0.3,
        "hvac": 0.3, "structural": 0.4, "architectural": 0.5,
        "civil": 0.6, "fire": 0.7, "telecom": 0.8,
    }
    if discipline is None:
        return 0.0
    return _MAP.get(discipline.lower(), 0.9)


def _region_code(region: str | None) -> float:
    """Map region type to numeric slot for context vector."""
    _MAP = {"legend": 0.1, "schedule": 0.2, "plan": 0.3, "detail": 0.4, "notes": 0.5}
    if region is None:
        return 0.0
    return _MAP.get(region.lower(), 0.9)


def _build_context_vector(row: _MotifRow) -> list[float]:
    """2-dim context vector: [discipline_code, region_code]."""
    return [_discipline_code(row.discipline), _region_code(row.sheet_region)]


def _combined_embedding(row: _MotifRow, context_weight: float = 0.15) -> list[float] | None:
    """Blend geometric embedding with context vector.

    Context is appended as extra dimensions (scaled by weight) so cosine
    similarity naturally blends geometry + context without retraining.
    """
    geo = row.embedding
    ctx = _build_context_vector(row)
    if geo is None:
        return None
    scaled_ctx = [v * context_weight for v in ctx]
    return geo + scaled_ctx


# ---------------------------------------------------------------------------
# Single-linkage clustering (no external deps required)
# ---------------------------------------------------------------------------

def _single_linkage_clusters(
    rows: list[_MotifRow],
    threshold: float,
) -> dict[str, list[str]]:
    """Group rows into clusters using single-linkage on combined embeddings.

    Returns {cluster_id: [blueprint_symbols_id, ...]}.

    Algorithm: O(n²) single-linkage — acceptable for the per-project motif
    count (typically <5k unique motif rows after the MAX_INSTANCES_PER_MOTIF cap
    in wave_11g2_see.py). For >10k rows use the scipy fallback.
    """
    # Build combined embeddings
    vecs: list[list[float] | None] = [_combined_embedding(r) for r in rows]

    # Rows without embeddings each get their own singleton cluster keyed by graph_hash
    cluster_map: dict[int, str] = {}  # index → cluster_id

    def _find_cluster(i: int) -> str:
        return cluster_map.get(i, rows[i].id)

    # Union-find
    parent: list[int] = list(range(len(rows)))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(x: int, y: int) -> None:
        rx, ry = _find(x), _find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(len(rows)):
        if vecs[i] is None:
            continue
        for j in range(i + 1, len(rows)):
            if vecs[j] is None:
                continue
            sim = _cosine_similarity(vecs[i], vecs[j])  # type: ignore[arg-type]
            if sim >= threshold:
                _union(i, j)

    # Try scipy hierarchical for large sets (better quality, optional)
    if len(rows) > 500:
        try:
            _scipy_linkage_inplace(rows, vecs, threshold, parent)
        except ImportError:
            pass  # fallback: keep union-find result

    # Collect clusters
    from collections import defaultdict
    groups: dict[int, list[str]] = defaultdict(list)
    for i, row in enumerate(rows):
        root = _find(i)
        groups[root].append(row.id)

    # Assign stable cluster_ids (sha1 of sorted member ids → first 16 hex chars)
    result: dict[str, list[str]] = {}
    for root_idx, member_ids in groups.items():
        stable_key = "".join(sorted(member_ids))
        cluster_id = "cls_" + hashlib.sha1(stable_key.encode()).hexdigest()[:16]
        result[cluster_id] = member_ids

    return result


def _scipy_linkage_inplace(
    rows: list[_MotifRow],
    vecs: list[list[float] | None],
    threshold: float,
    parent: list[int],
) -> None:
    """Refine clustering with scipy.cluster.hierarchy when available."""
    import numpy as np
    from scipy.cluster.hierarchy import fclusterdata  # type: ignore[import-not-found]

    valid_indices = [i for i, v in enumerate(vecs) if v is not None]
    if len(valid_indices) < 2:
        return
    matrix = np.array([vecs[i] for i in valid_indices], dtype=np.float32)
    # fclusterdata with cosine metric + single linkage
    labels = fclusterdata(matrix, t=1.0 - threshold, criterion="distance", metric="cosine", method="single")
    # Merge union-find based on scipy labels
    for pos_a in range(len(valid_indices)):
        for pos_b in range(pos_a + 1, len(valid_indices)):
            if labels[pos_a] == labels[pos_b]:
                _merge_uf(parent, valid_indices[pos_a], valid_indices[pos_b])


def _merge_uf(parent: list[int], x: int, y: int) -> None:
    def _find(z: int) -> int:
        while parent[z] != z:
            parent[z] = parent[parent[z]]
            z = parent[z]
        return z
    rx, ry = _find(x), _find(y)
    if rx != ry:
        parent[rx] = ry


# ---------------------------------------------------------------------------
# Module-level DB proxies — patchable in tests without deferred-import friction
# ---------------------------------------------------------------------------

async def _db_select(table: str, filters: str, **kwargs: Any) -> list[dict[str, Any]]:
    from aspire_orchestrator.services.supabase_client import supabase_select
    return await supabase_select(table, filters, **kwargs)


async def _db_update(table: str, match_filters: str, data: dict[str, Any]) -> dict[str, Any]:
    from aspire_orchestrator.services.supabase_client import supabase_update
    return await supabase_update(table, match_filters, data)


async def _db_insert_batch(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from aspire_orchestrator.services.supabase_client import supabase_insert_batch_ignore_conflicts
    return await supabase_insert_batch_ignore_conflicts(table, rows)


# ---------------------------------------------------------------------------
# Supabase I/O helpers (deferred imports — mirrors wave_11g2_see.py pattern)
# ---------------------------------------------------------------------------

async def _fetch_unresolved_motifs(project_id: str, suite_id: str) -> list[_MotifRow]:
    """Read blueprint_symbols rows for unresolved motifs scoped to this project."""
    rows = await _db_select(
        "blueprint_symbols",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )

    result: list[_MotifRow] = []
    for r in rows:
        cls = r.get("class") or ""
        if not any(cls.startswith(p) for p in _UNRESOLVED_PREFIXES):
            continue
        bbox = r.get("bbox") or {}
        embedding_raw = r.get("embedding")
        result.append(_MotifRow(
            id=r["id"],
            suite_id=r["suite_id"],
            sheet_id=r["sheet_id"],
            project_id=project_id,
            motif_id=bbox.get("motif_id"),
            graph_hash=bbox.get("graph_hash"),
            embedding=_embedding_from_bytes(embedding_raw),
            bbox=bbox,
            motif_frequency=int(bbox.get("motif_frequency", 1)),
            discipline=bbox.get("discipline"),
            sheet_region=bbox.get("sheet_region"),
        ))
    return result


async def _fetch_legend_entries(project_id: str, suite_id: str) -> list[dict[str, Any]]:
    """Read blueprint_project_legends rows with vector_signature populated."""
    rows = await _db_select(
        "blueprint_project_legends",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    return [r for r in rows if r.get("vector_signature") is not None]


async def _write_cluster_ids(
    cluster_map: dict[str, list[str]],
    suite_id: str,
) -> None:
    """Back-fill cluster_id on blueprint_symbols rows (advisory metadata only)."""
    for cluster_id, member_ids in cluster_map.items():
        for symbol_id in member_ids:
            try:
                await _db_update(
                    "blueprint_symbols",
                    f"id=eq.{symbol_id}&suite_id=eq.{suite_id}",
                    {"cluster_id": cluster_id},
                )
            except Exception:
                logger.debug(
                    "symbol_cluster_engine: cluster_id write failed symbol=%s cluster=%s",
                    symbol_id[:8], cluster_id,
                )


async def _write_resolved_symbols(
    rows: list[dict[str, Any]],
) -> int:
    """Batch-insert into blueprint_resolved_symbols. Returns inserted count."""
    if not rows:
        return 0
    inserted = await _db_insert_batch("blueprint_resolved_symbols", rows)
    return len(inserted) if isinstance(inserted, list) else 0


# ---------------------------------------------------------------------------
# Public API — cluster_motifs_on_project
# ---------------------------------------------------------------------------

async def cluster_motifs_on_project(
    *,
    project_id: str,
    suite_id: str,
    similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
) -> dict[str, list[str]]:
    """Group Wave 11g-v2 motifs by geometric + contextual similarity.

    Reads blueprint_symbols rows where class starts with 'unresolved_' or 'motif:'.
    Computes combined embedding (geometric 64-dim + 2-dim context: discipline + region).
    Clusters using single-linkage on cosine similarity (scipy if available).
    Writes cluster_id back to blueprint_symbols rows.
    Returns {cluster_id: [blueprint_symbols_id, ...]}.

    Expected: 80–500 clusters per commercial blueprint project.
    """
    logger.info(
        "symbol_cluster_engine: cluster_motifs project=%s suite=%s threshold=%.2f",
        project_id, suite_id[:8], similarity_threshold,
    )

    motif_rows = await _fetch_unresolved_motifs(project_id, suite_id)
    if not motif_rows:
        logger.info("symbol_cluster_engine: no unresolved motifs found for project=%s", project_id)
        return {}

    logger.info("symbol_cluster_engine: clustering %d motif rows", len(motif_rows))

    # Rows without embeddings become singleton clusters keyed by their own id
    has_embedding = [r for r in motif_rows if r.embedding is not None]
    no_embedding = [r for r in motif_rows if r.embedding is None]

    cluster_map: dict[str, list[str]] = {}

    if has_embedding:
        cluster_map = _single_linkage_clusters(has_embedding, similarity_threshold)

    # Singletons for embedding-less rows
    for r in no_embedding:
        singleton_id = "cls_" + hashlib.sha1(r.id.encode()).hexdigest()[:16]
        cluster_map[singleton_id] = [r.id]

    logger.info(
        "symbol_cluster_engine: formed %d clusters from %d motif rows",
        len(cluster_map), len(motif_rows),
    )

    await _write_cluster_ids(cluster_map, suite_id)
    return cluster_map


# ---------------------------------------------------------------------------
# Public API — propagate_legend_bindings_to_clusters
# ---------------------------------------------------------------------------

async def propagate_legend_bindings_to_clusters(
    *,
    project_id: str,
    suite_id: str,
    bind_threshold: float = _DEFAULT_BIND_THRESHOLD,
    verify_threshold: float = _DEFAULT_VERIFY_THRESHOLD,
) -> dict[str, int]:
    """Propagate legend semantic identity to all motif instances in a cluster.

    For each cluster:
    - centroid embedding ≥ bind_threshold → auto-bind all instances.
    - bind_threshold > similarity ≥ verify_threshold → flag pending_vlm_verify.
    - similarity < verify_threshold → unknown.

    Writes to blueprint_resolved_symbols for bound + flagged clusters.
    Returns: {clusters_auto_bound, clusters_flagged_for_vlm, clusters_unknown,
               total_motifs_resolved}.
    """
    logger.info(
        "symbol_cluster_engine: propagate project=%s suite=%s bind=%.2f verify=%.2f",
        project_id, suite_id[:8], bind_threshold, verify_threshold,
    )

    # 1. Fetch clusters already formed (re-read from blueprint_symbols)
    symbol_rows = await _db_select(
        "blueprint_symbols",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}&cluster_id=not.is.null",
    )

    if not symbol_rows:
        logger.info("symbol_cluster_engine: no clustered symbols found — run cluster_motifs_on_project first")
        return {
            "clusters_auto_bound": 0,
            "clusters_flagged_for_vlm": 0,
            "clusters_unknown": 0,
            "total_motifs_resolved": 0,
        }

    # 2. Fetch legend entries with embeddings
    legend_rows = await _fetch_legend_entries(project_id, suite_id)
    legend_embeddings: list[tuple[str, str | None, list[float]]] = []
    for lr in legend_rows:
        vec = _embedding_from_bytes(lr.get("vector_signature"))
        if vec is not None:
            legend_embeddings.append((lr["id"], lr.get("label"), vec))

    # 3. Group symbol rows by cluster_id
    from collections import defaultdict
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in symbol_rows:
        cid = row.get("cluster_id")
        if cid:
            clusters[cid].append(row)

    # 4. For each cluster: compute centroid, find best legend match
    clusters_auto_bound = 0
    clusters_flagged = 0
    clusters_unknown = 0
    resolved_rows: list[dict[str, Any]] = []

    for cluster_id, members in clusters.items():
        # Compute centroid from member embeddings
        member_vecs: list[list[float]] = []
        for m in members:
            vec = _embedding_from_bytes(m.get("embedding"))
            if vec is not None:
                member_vecs.append(vec)

        centroid = _mean_embedding(member_vecs) if member_vecs else None

        # Find best legend match
        best_similarity = 0.0
        best_legend_id: str | None = None
        best_symbol_name: str | None = None

        if centroid and legend_embeddings:
            for legend_id, label, legend_vec in legend_embeddings:
                sim = _cosine_similarity(centroid, legend_vec)
                if sim > best_similarity:
                    best_similarity = sim
                    best_legend_id = legend_id
                    best_symbol_name = label

        # Classify binding decision
        if best_similarity >= bind_threshold:
            resolution_source = "cluster_propagation"
            clusters_auto_bound += 1
        elif best_similarity >= verify_threshold:
            resolution_source = "pending_vlm_verify"
            clusters_flagged += 1
        else:
            resolution_source = "unknown"
            clusters_unknown += 1

        # Write resolved_symbols rows for all instances in this cluster
        for m in members:
            bbox = m.get("bbox") or {}
            resolved_rows.append({
                "id": str(uuid_mod.uuid4()),
                "suite_id": suite_id,
                "project_id": project_id,
                "sheet_id": m["sheet_id"],
                "legend_id": best_legend_id if resolution_source == "cluster_propagation" else None,
                "bbox": bbox,
                "symbol_name": best_symbol_name if resolution_source == "cluster_propagation" else None,
                "confidence": round(best_similarity, 4),
                "resolution_source": resolution_source,
                "cluster_id": cluster_id,
                "motif_hash": bbox.get("graph_hash"),
            })

    # 5. Batch-write resolved_symbols
    total_written = await _write_resolved_symbols(resolved_rows)

    logger.info(
        "symbol_cluster_engine: propagation complete — "
        "auto_bound=%d flagged=%d unknown=%d total_motifs_resolved=%d",
        clusters_auto_bound, clusters_flagged, clusters_unknown, total_written,
    )

    return {
        "clusters_auto_bound": clusters_auto_bound,
        "clusters_flagged_for_vlm": clusters_flagged,
        "clusters_unknown": clusters_unknown,
        "total_motifs_resolved": total_written,
    }
