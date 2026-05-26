"""Wave 18 — Wave 11g-v2 SEE integration (vector → graph → mine → resolve).

End-to-end orchestrator that wires the four Wave 11g-v2 primitives into a
single project-level call that mirrors :mod:`vgse_pipeline` but uses the
new WL-mining stack:

    classify_page → extract_primitives → build_graph → mine_motifs →
    resolve_motif (legend/tenant_memory/ocr cascade) → blueprint_symbols.

Runs additively alongside VGSE. Controlled by ``ASPIRE_USE_WAVE_11G2``;
default OFF so production behavior is unchanged unless explicitly flipped.

Provenance (Law #2):
    drew.see.11g2.completed       — one per project
    drew.see.11g2.page            — one per processed page

Law compliance:
    #2 Receipts for every state change.
    #3 Fail closed — RASTER_SCAN pages and motif resolution misses produce
       ``unresolved_<motif_id>`` rows; we never invent a canonical type.
    #6 Tenant isolation — every row INSERT and tenant_memory lookup is
       scoped by ``suite_id``.
    #9 Never logs raw OCR or primitive coordinates.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid as uuid_mod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Track B Pass 2.B — page-level concurrency for wave_11g-v2 SEE.
# Pages are independent extractions (no cross-page state). Bounded gather
# overlaps per-page Supabase I/O while keeping CPU work serialized under
# the asyncio event loop (single thread, no PyMuPDF re-entrancy hazard).
# Default chosen empirically — see
# docs/plans/track-b-pass-2b-concurrency-benchmark.md.
_PAGE_CONCURRENCY = max(1, int(os.environ.get("ASPIRE_DREW_SEE_PAGE_CONCURRENCY", "4")))


# Per-page cap on the number of primitives mined. The GAVNN stress test
# observed pages with 441k+ primitives — beyond this the WL graph blows
# memory before producing useful motifs. The cap is configurable so
# operators can dial it for specific tenants.
MAX_PRIMITIVES_PER_PAGE = int(os.environ.get("ASPIRE_11G2_MAX_PRIMITIVES", "50000"))

# Per-motif cap on emitted instance rows. With 232k instances/motif on dense
# pages, an uncapped write would explode blueprint_symbols. Contractors
# only need a representative sample per motif type.
MAX_INSTANCES_PER_MOTIF = int(os.environ.get("ASPIRE_11G2_MAX_INSTANCES", "500"))

# Mining hyperparameters. Defaults validated on the GAVNN PDF.
_K_HOPS = int(os.environ.get("ASPIRE_11G2_K_HOPS", "2"))
_WL_ITERATIONS = int(os.environ.get("ASPIRE_11G2_WL_ITERATIONS", "3"))
_MIN_FREQUENCY = int(os.environ.get("ASPIRE_11G2_MIN_FREQUENCY", "3"))


def is_enabled() -> bool:
    """Feature flag check. Default OFF."""
    return os.environ.get("ASPIRE_USE_WAVE_11G2", "").lower() == "true"


# --- Track A / Track B graph adapter -----------------------------------------


class TrackAGraphAdapter:
    """Adapt :class:`vector_graph_builder.VectorGraph` to the networkx-style
    interface expected by :func:`subgraph_miner.mine_motifs`.

    Track A's VectorGraph exposes ``nodes`` and ``edges`` as iterable
    properties; the miner calls them as ``graph.nodes(data=True)``.
    """

    def __init__(self, vg: Any) -> None:
        self._vg = vg
        self.graph: dict[str, Any] = dict(getattr(vg, "graph", {}) or {})
        self._nodes: list[Any] = list(getattr(vg, "nodes", []) or [])
        self._edges: list[tuple[Any, Any]] = list(getattr(vg, "edges", []) or [])
        # Pre-compute the node-attr dict the miner reads. We project Track A's
        # ``_NodeData`` (x, y, primitive_ids) into the miner's attr names:
        #   primitive_type, angle, length, bbox.
        node_data_lookup = getattr(vg, "node_data", {}) or {}
        edge_data_lookup = getattr(vg, "edge_data", {}) or {}
        self._node_attrs: dict[Any, dict[str, Any]] = {}
        for n in self._nodes:
            nd = node_data_lookup.get(n)
            if nd is None:
                self._node_attrs[n] = {}
                continue
            self._node_attrs[n] = {
                "x": float(getattr(nd, "x", 0.0)),
                "y": float(getattr(nd, "y", 0.0)),
                "primitive_type": "node",
                "bbox": (
                    float(getattr(nd, "x", 0.0)),
                    float(getattr(nd, "y", 0.0)),
                    float(getattr(nd, "x", 0.0)),
                    float(getattr(nd, "y", 0.0)),
                ),
            }
        # Edge attrs not consumed by the miner today, but kept for parity.
        self._edge_attrs: dict[tuple[Any, Any], dict[str, Any]] = {}
        for (u, v) in self._edges:
            ed = edge_data_lookup.get((u, v))
            if ed is None:
                self._edge_attrs[(u, v)] = {}
                continue
            self._edge_attrs[(u, v)] = {
                "type": str(getattr(ed, "type", "edge")),
                "transform_signature": str(getattr(ed, "transform_signature", "")),
                "stroke_width": float(getattr(ed, "stroke_width", 0.0)),
            }
        # Adjacency for neighbors().
        self._adj: dict[Any, list[Any]] = {n: [] for n in self._nodes}
        for (u, v) in self._edges:
            self._adj.setdefault(u, []).append(v)
            self._adj.setdefault(v, []).append(u)

    # networkx-style interface --------------------------------------------------
    def nodes(self, data: bool = False) -> Iterable[Any]:
        if data:
            return [(n, dict(self._node_attrs.get(n, {}))) for n in self._nodes]
        return list(self._nodes)

    def edges(self, data: bool = False) -> Iterable[Any]:
        if data:
            return [(u, v, dict(self._edge_attrs.get((u, v), {}))) for (u, v) in self._edges]
        return list(self._edges)

    def neighbors(self, n: Any) -> list[Any]:
        return list(self._adj.get(n, []))

    def degree(self, n: Any) -> int:
        return len(self._adj.get(n, []))

    def __contains__(self, n: Any) -> bool:
        return n in self._node_attrs

    def __len__(self) -> int:
        return len(self._nodes)

    def __iter__(self) -> Iterable[Any]:
        return iter(self._nodes)


# --- Receipts ----------------------------------------------------------------


def _emit_receipt(
    *,
    action_type: str,
    suite_id: str,
    office_id: str | None,
    correlation_id: str,
    metadata: dict[str, Any],
    outcome: str = "success",
) -> None:
    """Best-effort receipt write. Never raises."""
    try:
        from aspire_orchestrator.services.receipt_store import store_receipts
        store_receipts([{
            "id": str(uuid_mod.uuid4()),
            "outcome": outcome,
            "action_type": action_type,
            "receipt_type": action_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": "skillpack:drew-blueprint:wave_11g2",
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": office_id,
            "metadata": metadata,
            "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
        }])
    except Exception as exc:  # noqa: BLE001
        logger.debug("wave_11g2_see: receipt emit failed (%s)", type(exc).__name__)


# --- Helpers -----------------------------------------------------------------


@dataclass
class _PageOutcome:
    page_number: int
    mode: str
    primitive_count: int
    motif_count: int
    instance_rows: list[dict[str, Any]]
    resolution_counts: dict[str, int]


@dataclass
class _PageResult:
    """Per-page outcome aggregated under the page-concurrency semaphore.

    All counters are PER PAGE — merge into the project summary sequentially
    after ``asyncio.gather`` so peer-page races cannot corrupt aggregate state.
    """
    page_number: int
    classified: bool = False
    mode: str | None = None
    skipped_raster: bool = False
    skipped_no_sheet: bool = False
    processed: bool = False
    primitives_emitted: int = 0
    primitives_truncated: bool = False
    motifs_emitted: int = 0
    instances_emitted: int = 0
    rows_inserted: int = 0
    rows_failed: int = 0
    memory_upserts: int = 0
    memory_upserts_failed: int = 0
    resolution_counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def _build_tenant_memory_lookup(suite_id: str, signature_index: dict[str, Any]):
    """Return a sync callback compatible with symbol_retrieval.resolve_motif.

    The retrieval cascade calls ``lookup(graph_hash)`` synchronously inside
    the cascade. We pre-fetch all candidate signatures in bulk (async) up
    front, then serve the lookup from an in-memory dict.
    """
    from aspire_orchestrator.services.blueprint.symbol_retrieval import MemoryHit

    def _lookup(key: str) -> MemoryHit | None:
        # Cosine sentinel — we don't carry an embedding index in the in-memory
        # pre-fetch yet, so return None and let the cascade move on.
        if key.startswith("__cosine__:"):
            return None
        row = signature_index.get(key)
        if not row:
            return None
        canonical = row.get("canonical_name")
        if not canonical:
            return None
        return MemoryHit(
            canonical_type=str(canonical),
            graph_hash=str(key),
            embedding=[],
            confidence=float(row.get("confidence") or 0.9),
        )

    return _lookup


# --- Public entry point ------------------------------------------------------


async def run_wave_11g2_see_for_project(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    pdf_bytes: bytes,
    sheet_ids_by_page: dict[int, str],
    correlation_id: str,
    discipline_by_page: dict[int, str | None] | None = None,
) -> dict[str, Any]:
    """Run the Wave 11g-v2 vector→graph→mine→resolve pipeline.

    Returns
    -------
    dict with keys:
        pages_classified, total_primitives, total_motifs, total_instances,
        rows_inserted, mode_distribution, resolution_distribution,
        elapsed_ms, correlation_id.
    """
    import fitz  # PyMuPDF
    from aspire_orchestrator.services.blueprint import symbol_memory
    from aspire_orchestrator.services.blueprint.document_classifier import classify_page
    from aspire_orchestrator.services.blueprint.subgraph_miner import (
        DiscoveredMotif,
        mine_motifs,
    )
    from aspire_orchestrator.services.blueprint.symbol_retrieval import (
        ResolvedSymbol,
        resolve_motif,
    )
    from aspire_orchestrator.services.blueprint.vector_extractor import (
        extract_primitives,
    )
    from aspire_orchestrator.services.blueprint.vector_graph_builder import (
        build_graph,
    )
    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_insert,
    )

    started_at = datetime.now(timezone.utc)
    discipline_by_page = discipline_by_page or {}

    summary: dict[str, Any] = {
        "pages_classified": 0,
        "pages_processed": 0,
        "pages_skipped_raster": 0,
        "pages_skipped_no_sheet": 0,
        "total_primitives": 0,
        "primitives_truncated_pages": 0,
        "total_motifs": 0,
        "total_instances": 0,
        "rows_inserted": 0,
        "rows_failed": 0,
        "mode_distribution": {},
        "resolution_distribution": {
            "legend": 0,
            "tenant_memory": 0,
            "ocr": 0,
            "vlm": 0,
            "unresolved": 0,
        },
        "memory_upserts": 0,
        "memory_upserts_failed": 0,
        "page_outcomes": [],
        "correlation_id": correlation_id,
    }

    # Open the PDF once.
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "wave_11g2_see: pdf open failed project=%s err=%s",
            project_id[:8], type(exc).__name__,
        )
        summary["error"] = f"pdf_open_failed:{type(exc).__name__}"
        return summary

    # Per-page coroutine. Returns a _PageResult — NEVER mutates `summary`
    # directly. Aggregate merge happens sequentially after asyncio.gather
    # so peer-page races cannot corrupt counters or page_outcomes ordering.
    async def _process_one_page(page_idx: int) -> _PageResult:
        page_number = page_idx + 1
        result = _PageResult(page_number=page_number)

        try:
            page = doc[page_idx]
        except Exception as exc:  # noqa: BLE001
            result.error = f"page_open_failed:{type(exc).__name__}"
            return result

        # 1) Classify page.
        try:
            classification = classify_page(page)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wave_11g2_see: classify failed page=%d (%s) — skipping",
                page_number, type(exc).__name__,
            )
            result.error = f"classify_failed:{type(exc).__name__}"
            return result
        result.classified = True
        mode = classification.page_mode
        result.mode = mode

        # Fail-closed: RASTER_SCAN pages have no vector content to mine.
        if mode == "RASTER_SCAN":
            result.skipped_raster = True
            # Persistence-asserted receipt for RASTER_SCAN page (expected_min=0).
            try:
                from aspire_orchestrator.services.receipt_store import (
                    emit_persistence_asserted_receipt as _emit_pa,
                )
                _emit_pa(
                    stage="drew.see.11g2.page",
                    expected_table="blueprint_symbols",
                    expected_min=0,
                    count_before=0,
                    count_after=0,
                    correlation_id=correlation_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    project_id=project_id,
                    redacted_outputs={
                        "page_idx": page_number,
                        "mode": mode,
                        "n_primitives": 0,
                        "n_motifs": 0,
                        "n_instance_rows": 0,
                        "n_insert_failures": 0,
                        "skipped": "raster_scan",
                    },
                )
            except Exception as _pa_exc:  # noqa: BLE001
                logger.debug(
                    "wave_11g2_see: raster persistence assertion emit failed (%s)",
                    type(_pa_exc).__name__,
                )
                _emit_receipt(
                    action_type="drew.see.11g2.page",
                    suite_id=suite_id,
                    office_id=office_id,
                    correlation_id=correlation_id,
                    metadata={
                        "project_id": project_id,
                        "page_idx": page_number,
                        "mode": mode,
                        "n_primitives": 0,
                        "n_motifs": 0,
                        "skipped": "raster_scan",
                    },
                )
            return result

        sheet_id = sheet_ids_by_page.get(page_number)
        if not sheet_id:
            result.skipped_no_sheet = True
            logger.warning(
                "wave_11g2_see: no sheet_id page=%d project=%s — skipping emit",
                page_number, project_id[:8],
            )
            return result

        # 2) Extract primitives.
        try:
            primitives = extract_primitives(page)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wave_11g2_see: extract failed page=%d (%s)",
                page_number, type(exc).__name__,
            )
            result.error = f"extract_failed:{type(exc).__name__}"
            return result

        n_primitives_raw = len(primitives)
        if n_primitives_raw > MAX_PRIMITIVES_PER_PAGE:
            logger.warning(
                "wave_11g2_see: page=%d truncating primitives %d -> %d",
                page_number, n_primitives_raw, MAX_PRIMITIVES_PER_PAGE,
            )
            primitives = primitives[:MAX_PRIMITIVES_PER_PAGE]
            result.primitives_truncated = True

        result.primitives_emitted = len(primitives)
        if not primitives:
            return result

        # 3) Build graph (Track A).
        try:
            vg = build_graph(primitives)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wave_11g2_see: build_graph failed page=%d (%s)",
                page_number, type(exc).__name__,
            )
            result.error = f"build_graph_failed:{type(exc).__name__}"
            return result

        adapter = TrackAGraphAdapter(vg)

        # 4) Mine motifs.
        try:
            motifs: list[DiscoveredMotif] = mine_motifs(
                adapter,  # type: ignore[arg-type]
                k_hops=_K_HOPS,
                wl_iterations=_WL_ITERATIONS,
                min_frequency=_MIN_FREQUENCY,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wave_11g2_see: mine_motifs failed page=%d (%s)",
                page_number, type(exc).__name__,
            )
            result.error = f"mine_motifs_failed:{type(exc).__name__}"
            return result

        if not motifs:
            result.processed = True
            return result

        # 5) Pre-fetch tenant memory hits for all candidate hashes.
        candidate_hashes: list[str] = []
        for motif in motifs:
            candidate_hashes.append(motif.graph_hash)
            candidate_hashes.extend(motif.rotation_orbit)
        unique_hashes = list({h for h in candidate_hashes if h})
        try:
            mem_index = await symbol_memory.bulk_lookup(suite_id, unique_hashes)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wave_11g2_see: bulk_lookup failed page=%d (%s) — proceeding empty",
                page_number, type(exc).__name__,
            )
            mem_index = {}
        tenant_lookup = _build_tenant_memory_lookup(suite_id, mem_index)

        # 6) Resolve each motif and emit instance rows.
        now_iso = datetime.now(timezone.utc).isoformat()
        page_instance_rows: list[dict[str, Any]] = []
        page_motif_count = 0
        for motif in motifs:
            try:
                resolved: ResolvedSymbol = resolve_motif(
                    motif,
                    legend_anchors=[],  # Wave 18b will wire project-wide anchors
                    tenant_memory_lookup=tenant_lookup,
                    ocr_proximity_words=[],  # spatial OCR binding wave 18c
                    suite_id=suite_id,
                    vlm_hook=None,
                    discipline=discipline_by_page.get(page_number),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "wave_11g2_see: resolve failed motif=%s (%s)",
                    motif.motif_id, type(exc).__name__,
                )
                continue

            source = resolved.source
            result.resolution_counts[source] = result.resolution_counts.get(source, 0) + 1

            # Persist successful resolutions to tenant memory (Law #2).
            if resolved.canonical_type and source in ("legend", "tenant_memory", "ocr"):
                try:
                    await symbol_memory.upsert_observation(
                        suite_id=suite_id,
                        office_id=office_id,
                        signature_hash=motif.graph_hash,
                        canonical_name=resolved.canonical_type,
                        category=None,
                        source=source if source != "ocr" else "spatial_ocr",
                        project_id=project_id,
                        instances_observed=motif.frequency,
                        confidence=resolved.confidence,
                    )
                    result.memory_upserts += 1
                except Exception as exc:  # noqa: BLE001
                    result.memory_upserts_failed += 1
                    logger.debug(
                        "wave_11g2_see: memory upsert failed sig=%s (%s)",
                        motif.graph_hash[:8], type(exc).__name__,
                    )

            klass = (
                resolved.canonical_type
                if resolved.canonical_type
                else f"unresolved_{motif.motif_id}"
            )
            # Cap instances per motif.
            emit_instances = motif.instances[:MAX_INSTANCES_PER_MOTIF]
            if len(motif.instances) > MAX_INSTANCES_PER_MOTIF:
                logger.debug(
                    "wave_11g2_see: capping motif=%s instances %d -> %d",
                    motif.motif_id, len(motif.instances), MAX_INSTANCES_PER_MOTIF,
                )
            for inst in emit_instances:
                x0, y0, x1, y1 = inst.bbox
                row = {
                    "id": str(uuid_mod.uuid4()),
                    "suite_id": suite_id,
                    "office_id": office_id,
                    "sheet_id": sheet_id,
                    "class": klass,
                    "bbox": {
                        "x": float(x0),
                        "y": float(y0),
                        "w": float(max(0.0, x1 - x0)),
                        "h": float(max(0.0, y1 - y0)),
                        "signature_hash": motif.graph_hash,
                        "motif_id": motif.motif_id,
                        "name_source": "wl_motif",
                        "resolution_source": source,
                        "page_number": page_number,
                        "graph_hash": motif.graph_hash,
                        "k_hops": _K_HOPS,
                        "wl_iterations": _WL_ITERATIONS,
                        "rotation_orbit": list(motif.rotation_orbit),
                        "retrieval_matches": resolved.retrieval_matches,
                        "motif_frequency": motif.frequency,
                        "motif_confidence": float(motif.confidence),
                    },
                    "confidence": float(resolved.confidence),
                    "created_at": now_iso,
                    "created_by": None,
                }
                page_instance_rows.append(row)
            page_motif_count += 1
            result.motifs_emitted += 1
            result.instances_emitted += len(emit_instances)

        # 7) Insert rows.
        inserted = 0
        failed = 0
        for row in page_instance_rows:
            try:
                await supabase_insert("blueprint_symbols", row)
                inserted += 1
            except SupabaseClientError as exc:
                failed += 1
                logger.warning(
                    "wave_11g2_see: insert failed page=%d sig=%s (%s)",
                    page_number,
                    str(row["bbox"].get("signature_hash", ""))[:8],
                    type(exc).__name__,
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning(
                    "wave_11g2_see: insert unexpected error page=%d (%s)",
                    page_number, type(exc).__name__,
                )
        result.rows_inserted = inserted
        result.rows_failed = failed
        result.processed = True

        # Persistence assertion for this page (Track B Pass 1.C).
        # count_before / count_after are LOCAL to this coroutine — must NOT
        # be hoisted out of the per-page closure or peer pages would race.
        try:
            from aspire_orchestrator.services.receipt_store import (
                emit_persistence_asserted_receipt as _emit_pa,
            )
            _page_expected_min = 1  # non-raster pages must write >=1 row
            _emit_pa(
                stage="drew.see.11g2.page",
                expected_table="blueprint_symbols",
                expected_min=_page_expected_min,
                count_before=0,
                count_after=inserted,
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                project_id=project_id,
                redacted_outputs={
                    "page_idx": page_number,
                    "mode": mode,
                    "n_primitives": len(primitives),
                    "n_motifs": page_motif_count,
                    "n_instance_rows": inserted,
                    "n_insert_failures": failed,
                },
            )
        except Exception as _pa_exc:  # noqa: BLE001
            logger.debug(
                "wave_11g2_see: persistence assertion emit failed (%s)",
                type(_pa_exc).__name__,
            )
            _emit_receipt(
                action_type="drew.see.11g2.page",
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                metadata={
                    "project_id": project_id,
                    "page_idx": page_number,
                    "mode": mode,
                    "n_primitives": len(primitives),
                    "n_motifs": page_motif_count,
                    "n_instance_rows": inserted,
                    "n_insert_failures": failed,
                },
            )
        return result

    # Bounded-concurrent page execution. One bad page must NOT kill peers —
    # return_exceptions=True is non-negotiable. Read env at runtime so
    # benchmark sweeps and tests can override per-invocation.
    _runtime_concurrency = max(
        1, int(os.environ.get("ASPIRE_DREW_SEE_PAGE_CONCURRENCY", str(_PAGE_CONCURRENCY)))
    )
    sem = asyncio.Semaphore(_runtime_concurrency)

    async def _bounded(idx: int) -> _PageResult:
        async with sem:
            return await _process_one_page(idx)

    try:
        page_count = len(doc)
        logger.info(
            "wave_11g2_see: dispatching %d pages at concurrency=%d project=%s",
            page_count, _runtime_concurrency, project_id[:8],
        )
        gathered = await asyncio.gather(
            *[_bounded(i) for i in range(page_count)],
            return_exceptions=True,
        )

        # Sequential merge — page order preserved by enumerate.
        for page_idx, outcome in enumerate(gathered):
            page_number = page_idx + 1
            if isinstance(outcome, BaseException):
                logger.error(
                    "wave_11g2_see: page coroutine raised page=%d (%s) %s",
                    page_number, type(outcome).__name__, outcome,
                )
                # Emit a FAILED page receipt so the orphan sweeper sees a heartbeat
                # and downstream auditors can trace the failure.
                _emit_receipt(
                    action_type="drew.see.11g2.page",
                    suite_id=suite_id,
                    office_id=office_id,
                    correlation_id=correlation_id,
                    outcome="failure",
                    metadata={
                        "project_id": project_id,
                        "page_idx": page_number,
                        "error_class": type(outcome).__name__,
                        "error_message": str(outcome)[:500],
                    },
                )
                continue

            r: _PageResult = outcome
            if r.classified:
                summary["pages_classified"] += 1
            if r.mode:
                summary["mode_distribution"][r.mode] = (
                    summary["mode_distribution"].get(r.mode, 0) + 1
                )
            if r.skipped_raster:
                summary["pages_skipped_raster"] += 1
            if r.skipped_no_sheet:
                summary["pages_skipped_no_sheet"] += 1
            if r.processed:
                summary["pages_processed"] += 1
            if r.primitives_truncated:
                summary["primitives_truncated_pages"] += 1
            summary["total_primitives"] += r.primitives_emitted
            summary["total_motifs"] += r.motifs_emitted
            summary["total_instances"] += r.instances_emitted
            summary["rows_inserted"] += r.rows_inserted
            summary["rows_failed"] += r.rows_failed
            summary["memory_upserts"] += r.memory_upserts
            summary["memory_upserts_failed"] += r.memory_upserts_failed
            for src, n in r.resolution_counts.items():
                summary["resolution_distribution"][src] = (
                    summary["resolution_distribution"].get(src, 0) + n
                )
            if r.processed or r.skipped_raster or r.skipped_no_sheet:
                summary["page_outcomes"].append({
                    "page_number": r.page_number,
                    "mode": r.mode,
                    "primitives": r.primitives_emitted,
                    "motifs": r.motifs_emitted,
                    "rows": r.rows_inserted,
                })

    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass

    elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    summary["elapsed_ms"] = elapsed_ms

    # Project-level completion receipt with persistence assertion (Track B Pass 1.C).
    # Uses locally-tracked rows_inserted as delta (blueprint_symbols has no project_id).
    # expected_min=0: a PDF of all-raster pages legitimately writes 0 rows.
    try:
        from aspire_orchestrator.services.receipt_store import (
            emit_persistence_asserted_receipt as _emit_pa_completed,
        )
        _emit_pa_completed(
            stage="drew.see.11g2.completed",
            expected_table="blueprint_symbols",
            expected_min=0,
            count_before=0,
            count_after=summary["rows_inserted"],
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            project_id=project_id,
            redacted_outputs={
                "pages_classified": summary["pages_classified"],
                "pages_processed": summary["pages_processed"],
                "pages_skipped_raster": summary["pages_skipped_raster"],
                "pages_skipped_no_sheet": summary["pages_skipped_no_sheet"],
                "total_primitives": summary["total_primitives"],
                "primitives_truncated_pages": summary["primitives_truncated_pages"],
                "total_motifs": summary["total_motifs"],
                "total_instances": summary["total_instances"],
                "rows_inserted": summary["rows_inserted"],
                "rows_failed": summary["rows_failed"],
                "mode_distribution": summary["mode_distribution"],
                "resolution_distribution": summary["resolution_distribution"],
                "memory_upserts": summary["memory_upserts"],
                "elapsed_ms": elapsed_ms,
                "limits": {
                    "max_primitives_per_page": MAX_PRIMITIVES_PER_PAGE,
                    "max_instances_per_motif": MAX_INSTANCES_PER_MOTIF,
                    "k_hops": _K_HOPS,
                    "wl_iterations": _WL_ITERATIONS,
                    "min_frequency": _MIN_FREQUENCY,
                },
            },
        )
    except Exception as _pa_exc:  # noqa: BLE001
        logger.debug("wave_11g2_see: completed assertion emit failed (%s)", type(_pa_exc).__name__)
        _emit_receipt(
            action_type="drew.see.11g2.completed",
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            metadata={
                "project_id": project_id,
                "pages_classified": summary["pages_classified"],
                "pages_processed": summary["pages_processed"],
                "pages_skipped_raster": summary["pages_skipped_raster"],
                "pages_skipped_no_sheet": summary["pages_skipped_no_sheet"],
                "total_primitives": summary["total_primitives"],
                "primitives_truncated_pages": summary["primitives_truncated_pages"],
                "total_motifs": summary["total_motifs"],
                "total_instances": summary["total_instances"],
                "rows_inserted": summary["rows_inserted"],
                "rows_failed": summary["rows_failed"],
                "mode_distribution": summary["mode_distribution"],
                "resolution_distribution": summary["resolution_distribution"],
                "memory_upserts": summary["memory_upserts"],
                "elapsed_ms": elapsed_ms,
                "limits": {
                    "max_primitives_per_page": MAX_PRIMITIVES_PER_PAGE,
                    "max_instances_per_motif": MAX_INSTANCES_PER_MOTIF,
                    "k_hops": _K_HOPS,
                    "wl_iterations": _WL_ITERATIONS,
                    "min_frequency": _MIN_FREQUENCY,
                },
            },
        )

    logger.info(
        "wave_11g2_see: project=%s pages=%d processed=%d primitives=%d motifs=%d "
        "instances=%d rows=%d (legend=%d memory=%d ocr=%d unresolved=%d) (%dms)",
        project_id[:8],
        summary["pages_classified"],
        summary["pages_processed"],
        summary["total_primitives"],
        summary["total_motifs"],
        summary["total_instances"],
        summary["rows_inserted"],
        summary["resolution_distribution"].get("legend", 0),
        summary["resolution_distribution"].get("tenant_memory", 0),
        summary["resolution_distribution"].get("ocr", 0),
        summary["resolution_distribution"].get("unresolved", 0),
        elapsed_ms,
    )

    return summary


__all__ = [
    "MAX_PRIMITIVES_PER_PAGE",
    "MAX_INSTANCES_PER_MOTIF",
    "TrackAGraphAdapter",
    "is_enabled",
    "run_wave_11g2_see_for_project",
]
