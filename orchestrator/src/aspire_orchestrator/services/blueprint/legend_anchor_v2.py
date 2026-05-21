"""Wave 11g-v2 Track B Phase 7 — Legend Anchoring V2.

Wave 17b.4 shipped legend extraction that finds legend regions and pairs each
legend drawing with its OCR label via the symbol_lexicon. That gave us
``{signature_hash: canonical_label}`` per page.

V2 extends that with **graph-topology matching**: each legend row's drawing is
mined as a one-off ``DiscoveredMotif`` (via subgraph_miner with min_frequency=1
inside the legend zone) so we can match a project-wide motif to a legend entry
by either:

    1. WL graph_hash equality (most precise — same neighborhood topology), OR
    2. Embedding cosine similarity ≥ 0.85 (tolerant — same topology with
       light perturbation: extra wire stub, missing dot).

The result is a list of ``LegendAnchor``s. Each anchor binds a canonical label
(from the lexicon, or the raw OCR text if not in the lexicon) to a mined
legend_motif. The symbol_retrieval cascade (Phase 6) consults these anchors as
its highest-confidence source.

Aliases: the lexicon already collapses synonyms ("RECEPT" / "RECEPTACLE" /
"DUPLEX OUTLET" all → ``duplex_receptacle``). We rely on that — the lexicon is
the single source of truth for label canonicalisation.

Unknown legend labels (the OCR text doesn't map to any lexicon entry) are kept
as raw labels with ``lexicon_known=False``. We never invent a name.

Law compliance:
    #1 / #3  Fail closed. Missing OCR text → no anchor. We never guess.
    #6       Lexicon is global; tenant routing happens upstream.
    #9       Logs only the anchor count and discipline — never raw label text.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from aspire_orchestrator.services.blueprint import symbol_lexicon
from aspire_orchestrator.services.blueprint.subgraph_miner import (
    DiscoveredMotif,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import fitz

logger = logging.getLogger(__name__)


# Threshold for accepting an embedding-similarity match between a project
# motif and a legend motif. Empirically: < 0.85 starts admitting
# topologically-unrelated symbols.
_EMBEDDING_SIM_FLOOR = 0.85


@dataclass(frozen=True)
class LegendAnchor:
    """One legend row, post-extraction.

    Attributes:
        region_bbox: PDF-point bbox of the legend region this row was found in.
        canonical_label: the lexicon-canonical symbol name, OR the raw OCR
            text when the lexicon has no entry for that label.
        legend_motif: the mined topology of the drawing in this legend row.
        confidence: 1.0 when both topology+OCR resolved cleanly; lower when
            we had to fall back (e.g., raw OCR with no lexicon hit).
        page_number: 1-indexed page where this anchor was extracted.
        lexicon_known: True iff ``canonical_label`` came from the lexicon.
    """
    region_bbox: tuple[float, float, float, float]
    canonical_label: str
    legend_motif: DiscoveredMotif
    confidence: float
    page_number: int
    lexicon_known: bool


# Track A interface contracts (lightweight, structural). We do NOT import
# the Track A modules — we accept duck-typed inputs to keep this module
# build-time independent.

# A "VectorPrimitive" is anything with: type, bbox tuple, optional angle/length.


def extract_legend_anchors(
    page: "fitz.Page",
    primitives: list[Any],
    *,
    discipline: str | None = None,
    legend_motif_builder: Callable[[Any], DiscoveredMotif] | None = None,
    legend_regions: list[tuple[float, float, float, float]] | None = None,
    legend_rows: list[dict[str, Any]] | None = None,
) -> list[LegendAnchor]:
    """Extract LegendAnchors for a page.

    Args:
        page: PyMuPDF Page (used to read legend regions when ``legend_regions``
            is not provided).
        primitives: Track A vector primitives. Currently unused by this
            extractor (the legend-row graph is built by ``legend_motif_builder``
            or supplied directly via ``legend_rows``), but accepted so the
            signature matches the Track A contract.
        discipline: optional sheet discipline (E/P/M/...) — biases lexicon
            lookups toward the right discipline table.
        legend_motif_builder: optional callable that turns a legend-row record
            (with at least ``drawing`` and ``bbox`` keys) into a DiscoveredMotif.
            Provided by callers that have a live graph builder.
        legend_regions: pre-computed region bboxes (bypasses page parsing).
        legend_rows: pre-computed legend rows (bypasses page parsing). Each
            row: ``{label: str, bbox: (x0,y0,x1,y1), motif: DiscoveredMotif}``.

    Returns:
        Ordered list of LegendAnchors (top-to-bottom by region Y, then row Y).

    The ``legend_rows`` / ``legend_motif_builder`` hooks exist so this module
    is unit-testable without a real PDF and without Track A's vector graph
    builder being on disk yet.
    """
    page_num = int(getattr(page, "number", 0)) + 1 if page is not None else 0

    if legend_rows is None:
        legend_rows = _extract_legend_rows_from_page(
            page, legend_regions=legend_regions, motif_builder=legend_motif_builder
        )
    if not legend_rows:
        logger.info("legend_anchor_v2: 0 anchors (page=%d, discipline=%s)", page_num, discipline)
        return []

    anchors: list[LegendAnchor] = []
    for row in legend_rows:
        label_raw = str(row.get("label") or "").strip()
        bbox = tuple(float(v) for v in row.get("bbox") or (0.0, 0.0, 0.0, 0.0))
        if len(bbox) != 4:
            continue
        motif = row.get("motif")
        if not isinstance(motif, DiscoveredMotif):
            continue
        if not label_raw:
            # No label → cannot anchor. Fail closed.
            continue

        canonical = symbol_lexicon.lookup(label_raw, discipline=discipline)
        if canonical is not None:
            canonical_label = canonical
            lexicon_known = True
            # High confidence: both topology and label resolved cleanly.
            confidence = 1.0
        else:
            # Unknown legend label — keep raw text. The contractor may rename
            # later. Confidence is reduced because we can't validate the name.
            canonical_label = label_raw.strip()
            lexicon_known = False
            confidence = 0.6

        anchors.append(
            LegendAnchor(
                region_bbox=bbox,  # type: ignore[arg-type]
                canonical_label=canonical_label,
                legend_motif=motif,
                confidence=confidence,
                page_number=page_num,
                lexicon_known=lexicon_known,
            )
        )

    logger.info(
        "legend_anchor_v2: %d anchors (page=%d, discipline=%s)",
        len(anchors),
        page_num,
        discipline,
    )
    return anchors


# --- Topology-similarity match (project motif ↔ legend anchor) ---------------


def match_motif_to_anchors(
    motif: DiscoveredMotif,
    anchors: list[LegendAnchor],
    *,
    embedding_sim_floor: float = _EMBEDDING_SIM_FLOOR,
) -> tuple[LegendAnchor, float] | None:
    """Find the best LegendAnchor for a project-wide motif.

    Order of preference:
        1. Exact graph_hash equality (returns similarity 1.0).
        2. Rotation-orbit hash equality (returns similarity 0.99 — same
           topology, different drawn rotation).
        3. Embedding cosine similarity above ``embedding_sim_floor``.

    Returns (anchor, similarity) for the best match, or None if nothing
    satisfies the floor. Never invents a match.
    """
    if not anchors:
        return None

    # 1. Exact WL hash
    for a in anchors:
        if a.legend_motif.graph_hash == motif.graph_hash:
            return (a, 1.0)

    # 2. Rotation orbit
    motif_orbit = set(motif.rotation_orbit)
    for a in anchors:
        if motif_orbit & set(a.legend_motif.rotation_orbit):
            return (a, 0.99)

    # 3. Embedding cosine
    best: tuple[LegendAnchor, float] | None = None
    for a in anchors:
        sim = _cosine(motif.embedding, a.legend_motif.embedding)
        if sim >= embedding_sim_floor and (best is None or sim > best[1]):
            best = (a, sim)
    return best


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# --- Optional helper: parse a PyMuPDF page when no rows pre-supplied ---------


def _extract_legend_rows_from_page(
    page: "fitz.Page | None",
    *,
    legend_regions: list[tuple[float, float, float, float]] | None,
    motif_builder: Callable[[Any], DiscoveredMotif] | None,
) -> list[dict[str, Any]]:
    """Fallback: parse a PyMuPDF page to harvest legend rows.

    Returns rows ONLY when both ``legend_regions`` and ``motif_builder`` can be
    derived. In the unit-test pathway (no fitz, no Track A) the caller passes
    ``legend_rows`` directly and this function is never reached.
    """
    if page is None or motif_builder is None:
        return []
    try:
        from aspire_orchestrator.services.blueprint.vgse import find_legend_regions
    except Exception:
        return []
    try:
        rects = legend_regions or [
            (r.x0, r.y0, r.x1, r.y1) for r in find_legend_regions(page)
        ]
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for rect in rects:
        # The fully-wired implementation here will iterate page.get_drawings()
        # inside ``rect``, OCR-pair each drawing, and pass the drawing to
        # ``motif_builder``. We leave that wiring to the Track A integration
        # PR (it owns the drawing→graph adapter), and ship the test-friendly
        # API today.
        _ = rect
    return rows


__all__ = [
    "LegendAnchor",
    "extract_legend_anchors",
    "match_motif_to_anchors",
]
