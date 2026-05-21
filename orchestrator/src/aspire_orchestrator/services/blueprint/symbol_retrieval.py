"""Wave 11g-v2 Track B Phase 6 — Symbol Retrieval Engine.

Confidence-fused cascade that resolves a DiscoveredMotif (Phase 4) into a
ResolvedSymbol with provenance.

Cascade order (highest confidence first):

    1. Legend topology match (Phase 7 anchors)             → conf 0.95
    2. Tenant memory hit (exact graph_hash, then cosine)   → conf 0.90
    3. OCR proximity (lexicon-resolved nearby word)        → conf 0.80
    4. VLM hook (NOT called in this wave — documented)     → conf 0.50
    5. TRUE_UNKNOWN                                        → conf 0.00

Every source queried is recorded in ``retrieval_matches`` — even if it lost.
This gives the receipt ledger full audit visibility into WHY a symbol was
labelled the way it was.

Law compliance:
    #1 / #3  Fail closed. An unresolved motif stays unresolved — we never
             synthesise a canonical_type out of thin air.
    #2       Every resolution carries a full audit trail of sources queried.
    #6       Tenant memory lookup is callback-based; the cascade itself
             holds no tenant state.
    #9       Never logs OCR words or labels — only source name + confidence.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from aspire_orchestrator.services.blueprint.legend_anchor_v2 import (
    LegendAnchor,
    match_motif_to_anchors,
)
from aspire_orchestrator.services.blueprint.subgraph_miner import (
    DiscoveredMotif,
)
from aspire_orchestrator.services.blueprint.spatial_ocr_binder import OcrWord
from aspire_orchestrator.services.blueprint import symbol_lexicon

logger = logging.getLogger(__name__)


# Source enum for retrieval_matches and the winning source field.
SourceLiteral = Literal["legend", "tenant_memory", "ocr", "vlm", "unresolved"]


# Confidence anchors. Used by both the cascade and downstream UIs that color-code
# resolutions (green ≥ 0.9, amber ≥ 0.75, red < 0.75).
CONF_LEGEND = 0.95
CONF_TENANT_MEMORY_EXACT = 0.90
CONF_TENANT_MEMORY_COSINE = 0.85
CONF_OCR = 0.80
CONF_VLM = 0.50
CONF_UNRESOLVED = 0.0

_LEGEND_ANCHOR_FLOOR = 0.85
_TENANT_COSINE_FLOOR = 0.92
_OCR_PROXIMITY_PT = 40.0


# --- Public types ------------------------------------------------------------


@dataclass(frozen=True)
class MemoryHit:
    """Tenant-memory lookup result. Provided by the caller via callback.

    Attributes:
        canonical_type: tenant-asserted canonical symbol name.
        graph_hash: hash of the motif the tenant previously resolved (used for
            exact-match boosting vs cosine).
        embedding: 32-dim vector for cosine fallback.
        confidence: tenant's confidence in this prior resolution (1.0 if
            human-confirmed, lower if auto-propagated).
    """
    canonical_type: str
    graph_hash: str
    embedding: list[float]
    confidence: float


@dataclass(frozen=True)
class ResolvedSymbol:
    """The fused decision for one DiscoveredMotif.

    Attributes:
        canonical_type: the resolved Aspire symbol name, or None when the
            cascade fell through to ``unresolved``.
        confidence: 0.0–1.0; never > the winning-source ceiling.
        source: which cascade rung produced the answer.
        retrieval_matches: full audit list of every source queried, each as a
            ``{source, hit, confidence, detail}`` record. Includes losing
            sources too — this is the receipt-grade audit trail.
        motif_id: copied from the input motif for cross-reference.
    """
    canonical_type: Optional[str]
    confidence: float
    source: SourceLiteral
    retrieval_matches: list[dict[str, Any]]
    motif_id: str


# --- Cascade entrypoint ------------------------------------------------------


def resolve_motif(
    motif: DiscoveredMotif,
    *,
    legend_anchors: list[LegendAnchor],
    tenant_memory_lookup: Callable[[str], MemoryHit | None],
    ocr_proximity_words: list[OcrWord],
    suite_id: str,
    vlm_hook: Callable[[DiscoveredMotif], tuple[str, float] | None] | None = None,
    discipline: str | None = None,
) -> ResolvedSymbol:
    """Resolve a single discovered motif via the confidence cascade.

    Args:
        motif: the topology to resolve.
        legend_anchors: anchors from Phase 7 for the same sheet/project.
        tenant_memory_lookup: callable that takes a graph_hash and returns a
            ``MemoryHit`` if the tenant has previously resolved a motif with
            that hash. The caller is responsible for tenant scoping (Law #6).
        ocr_proximity_words: OCR words on the page; used for proximity match
            against the motif's union bbox.
        suite_id: tenant identifier, threaded through to retrieval_matches.
        vlm_hook: optional callable invoked when all deterministic sources
            miss. Returns ``(canonical_type, confidence)`` or None. Default
            None (VLM is NOT called in this wave).
        discipline: optional discipline tag biasing lexicon lookups.

    Returns:
        ResolvedSymbol with the winning source's verdict and a full audit
        trail in ``retrieval_matches``.
    """
    matches: list[dict[str, Any]] = []

    # 1. Legend topology match
    legend_hit = match_motif_to_anchors(
        motif, legend_anchors, embedding_sim_floor=_LEGEND_ANCHOR_FLOOR
    )
    if legend_hit is not None:
        anchor, similarity = legend_hit
        matches.append(
            {
                "source": "legend",
                "hit": True,
                "confidence": CONF_LEGEND,
                "detail": {
                    "anchor_label": anchor.canonical_label,
                    "anchor_similarity": float(similarity),
                    "anchor_page": anchor.page_number,
                    "lexicon_known": anchor.lexicon_known,
                },
            }
        )
        if similarity >= _LEGEND_ANCHOR_FLOOR:
            logger.info(
                "resolve_motif: motif=%s -> legend (sim=%.2f)",
                motif.motif_id,
                similarity,
            )
            return ResolvedSymbol(
                canonical_type=anchor.canonical_label,
                confidence=CONF_LEGEND,
                source="legend",
                retrieval_matches=matches,
                motif_id=motif.motif_id,
            )
    else:
        matches.append(
            {"source": "legend", "hit": False, "confidence": 0.0, "detail": {}}
        )

    # 2. Tenant memory — exact hash, then cosine within orbit
    mem_hit = _query_tenant_memory(motif, tenant_memory_lookup)
    if mem_hit is not None:
        canonical_type, mem_conf, detail = mem_hit
        matches.append(
            {
                "source": "tenant_memory",
                "hit": True,
                "confidence": mem_conf,
                "detail": detail,
            }
        )
        logger.info(
            "resolve_motif: motif=%s -> tenant_memory (conf=%.2f, suite=%s)",
            motif.motif_id,
            mem_conf,
            suite_id,
        )
        return ResolvedSymbol(
            canonical_type=canonical_type,
            confidence=mem_conf,
            source="tenant_memory",
            retrieval_matches=matches,
            motif_id=motif.motif_id,
        )
    matches.append(
        {"source": "tenant_memory", "hit": False, "confidence": 0.0, "detail": {}}
    )

    # 3. OCR proximity → lexicon
    ocr_hit = _query_ocr_proximity(motif, ocr_proximity_words, discipline=discipline)
    if ocr_hit is not None:
        canonical_type, distance_pt, raw_label_redacted = ocr_hit
        matches.append(
            {
                "source": "ocr",
                "hit": True,
                "confidence": CONF_OCR,
                "detail": {
                    "distance_pt": float(distance_pt),
                    "label_redacted": raw_label_redacted,
                },
            }
        )
        logger.info(
            "resolve_motif: motif=%s -> ocr (dist=%.1fpt)", motif.motif_id, distance_pt
        )
        return ResolvedSymbol(
            canonical_type=canonical_type,
            confidence=CONF_OCR,
            source="ocr",
            retrieval_matches=matches,
            motif_id=motif.motif_id,
        )
    matches.append(
        {"source": "ocr", "hit": False, "confidence": 0.0, "detail": {}}
    )

    # 4. VLM hook — documented but not called in this wave by default.
    if vlm_hook is not None:
        vlm_result = vlm_hook(motif)
        if vlm_result is not None:
            canonical_type, vlm_conf = vlm_result
            matches.append(
                {
                    "source": "vlm",
                    "hit": True,
                    "confidence": vlm_conf,
                    "detail": {"reported_confidence": float(vlm_conf)},
                }
            )
            return ResolvedSymbol(
                canonical_type=canonical_type,
                confidence=min(vlm_conf, CONF_VLM),
                source="vlm",
                retrieval_matches=matches,
                motif_id=motif.motif_id,
            )
        matches.append(
            {"source": "vlm", "hit": False, "confidence": 0.0, "detail": {}}
        )

    # 5. TRUE_UNKNOWN — never invent.
    matches.append(
        {
            "source": "unresolved",
            "hit": True,
            "confidence": CONF_UNRESOLVED,
            "detail": {"motif_frequency": motif.frequency},
        }
    )
    logger.info("resolve_motif: motif=%s -> unresolved", motif.motif_id)
    return ResolvedSymbol(
        canonical_type=None,
        confidence=CONF_UNRESOLVED,
        source="unresolved",
        retrieval_matches=matches,
        motif_id=motif.motif_id,
    )


# --- Internal cascade helpers ------------------------------------------------


def _query_tenant_memory(
    motif: DiscoveredMotif,
    lookup: Callable[[str], MemoryHit | None],
) -> tuple[str, float, dict[str, Any]] | None:
    """Query tenant memory by graph_hash + rotation orbit; fall back to cosine.

    Returns (canonical_type, confidence, detail) or None.
    """
    # Exact graph_hash lookup.
    hit = _safe_lookup(lookup, motif.graph_hash)
    if hit is not None:
        return (
            hit.canonical_type,
            CONF_TENANT_MEMORY_EXACT,
            {
                "match": "graph_hash_exact",
                "memory_confidence": float(hit.confidence),
            },
        )
    # Rotation orbit — any orbit member matching the tenant's memory counts.
    for orbit_hash in motif.rotation_orbit:
        if orbit_hash == motif.graph_hash:
            continue
        orbit_hit = _safe_lookup(lookup, orbit_hash)
        if orbit_hit is not None:
            return (
                orbit_hit.canonical_type,
                CONF_TENANT_MEMORY_EXACT,
                {
                    "match": "rotation_orbit_hash",
                    "memory_confidence": float(orbit_hit.confidence),
                },
            )
    # Cosine fallback — the lookup callback owns memory iteration. The default
    # callback signature gets a hash; for cosine we ask via a sentinel form so
    # callers that don't support it just return None.
    cos_hit = _safe_lookup(lookup, f"__cosine__:{motif.motif_id}")
    if cos_hit is not None:
        sim = _cosine(motif.embedding, cos_hit.embedding)
        if sim >= _TENANT_COSINE_FLOOR:
            return (
                cos_hit.canonical_type,
                CONF_TENANT_MEMORY_COSINE,
                {
                    "match": "embedding_cosine",
                    "cosine": float(sim),
                    "memory_confidence": float(cos_hit.confidence),
                },
            )
    return None


def _query_ocr_proximity(
    motif: DiscoveredMotif,
    words: list[OcrWord],
    *,
    discipline: str | None,
) -> tuple[str, float, str] | None:
    """Find the closest lexicon-resolved OCR word within proximity of motif.

    Returns (canonical_type, distance_pt, redacted_label) or None.
    """
    if not motif.instances or not words:
        return None
    # Use the first instance's bbox center (deterministic & cheap; instances
    # are sorted by canonical hash).
    bbox = motif.instances[0].bbox
    cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0

    best: tuple[float, str, str] | None = None
    for w in words:
        wx, wy = (w.x0 + w.x1) / 2.0, (w.y0 + w.y1) / 2.0
        dist = ((wx - cx) ** 2 + (wy - cy) ** 2) ** 0.5
        if dist > _OCR_PROXIMITY_PT:
            continue
        canonical = symbol_lexicon.lookup(w.text, discipline=discipline)
        if canonical is None:
            continue
        if best is None or dist < best[0]:
            # Redact the raw label to its first 2 chars + length — Law #9. The
            # CANONICAL name still goes through.
            redacted = f"{w.text[:2]}*({len(w.text)})"
            best = (dist, canonical, redacted)
    if best is None:
        return None
    return (best[1], best[0], best[2])


def _safe_lookup(
    lookup: Callable[[str], MemoryHit | None], key: str
) -> MemoryHit | None:
    try:
        return lookup(key)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("tenant_memory_lookup raised: %s", exc)
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


__all__ = [
    "MemoryHit",
    "ResolvedSymbol",
    "resolve_motif",
    "CONF_LEGEND",
    "CONF_TENANT_MEMORY_EXACT",
    "CONF_OCR",
    "CONF_VLM",
    "CONF_UNRESOLVED",
]
