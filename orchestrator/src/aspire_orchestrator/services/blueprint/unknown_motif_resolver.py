"""Wave 11g-v2 Track B Phase 8 — Unknown Motif Resolver.

When the retrieval cascade (Phase 6) returns ``source="unresolved"`` we do NOT
force a name. Instead, we bucket the unknown into one of three operational
states so the pipeline can route it correctly:

    VECTOR_RECOVERABLE  The motif's embedding carries high entropy — the
                        topology IS distinctive, we simply have no match for
                        it yet. Route to VLM or contractor-review.

    RASTER_RECOVERABLE  The source page was HYBRID or RASTER_SCAN — the
                        vector pipeline is the wrong tool. Defer to the
                        existing raster VGSE / YOLO fallback.

    TRUE_UNKNOWN        Low-entropy embedding AND no match. The drawing is
                        topologically indistinct from background noise.
                        Accept the unknown state — do NOT force a label.

Hard rule (Law #3 — Fail Closed):
    A TRUE_UNKNOWN must remain unknown. The downstream code paths that
    consume this bucket must treat it as a first-class state, not a sentinel
    to be replaced with "best guess".
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Literal

from aspire_orchestrator.services.blueprint.subgraph_miner import DiscoveredMotif

logger = logging.getLogger(__name__)


# Thresholds — chosen empirically from the spec.
_ENTROPY_RECOVERABLE = 1.5
_ENTROPY_TRUE_UNKNOWN = 1.0

UnknownBucket = Literal["VECTOR_RECOVERABLE", "RASTER_RECOVERABLE", "TRUE_UNKNOWN"]


@dataclass(frozen=True)
class PageClassification:
    """Minimal interface — matches Track A's classifier output.

    Track A's ``document_classifier`` will expose more fields; we only need
    the category to bucket unknowns correctly.
    """
    category: Literal[
        "VECTOR_NATIVE",
        "VECTOR_DEGRADED",
        "HYBRID",
        "RASTER_SCAN",
        "UNKNOWN_LOW_CONTENT",
    ]
    page_number: int


def bucket_unknown(
    motif: DiscoveredMotif,
    page_classification: PageClassification,
) -> UnknownBucket:
    """Bucket an unresolved motif into one of three operational states.

    Rules:
        * RASTER_SCAN or HYBRID page  → RASTER_RECOVERABLE (regardless of entropy).
        * VECTOR_DEGRADED → treat like vector_native (high-entropy = recoverable).
        * High-entropy embedding (>= 1.5)  → VECTOR_RECOVERABLE.
        * Low-entropy embedding  (< 1.0)   → TRUE_UNKNOWN.
        * Mid-band (1.0 ≤ H < 1.5)         → VECTOR_RECOVERABLE (lean optimistic
                                              when there's any topology signal).
    """
    cat = page_classification.category
    if cat in ("RASTER_SCAN", "HYBRID"):
        logger.info(
            "bucket_unknown: motif=%s page=%d category=%s -> RASTER_RECOVERABLE",
            motif.motif_id,
            page_classification.page_number,
            cat,
        )
        return "RASTER_RECOVERABLE"

    entropy = shannon_entropy(motif.embedding)
    if entropy >= _ENTROPY_RECOVERABLE:
        bucket: UnknownBucket = "VECTOR_RECOVERABLE"
    elif entropy < _ENTROPY_TRUE_UNKNOWN:
        bucket = "TRUE_UNKNOWN"
    else:
        bucket = "VECTOR_RECOVERABLE"

    logger.info(
        "bucket_unknown: motif=%s page=%d entropy=%.3f -> %s",
        motif.motif_id,
        page_classification.page_number,
        entropy,
        bucket,
    )
    return bucket


def shannon_entropy(vector: list[float]) -> float:
    """Shannon entropy of a normalized non-negative vector, base 2.

    Negative or zero-mass vectors return 0.0 (fail closed — no signal).
    """
    if not vector:
        return 0.0
    total = 0.0
    cleaned: list[float] = []
    for v in vector:
        if v <= 0 or math.isnan(v):
            continue
        cleaned.append(float(v))
        total += float(v)
    if total <= 0 or not cleaned:
        return 0.0
    h = 0.0
    for v in cleaned:
        p = v / total
        h -= p * math.log2(p)
    return h


__all__ = [
    "PageClassification",
    "UnknownBucket",
    "bucket_unknown",
    "shannon_entropy",
]
