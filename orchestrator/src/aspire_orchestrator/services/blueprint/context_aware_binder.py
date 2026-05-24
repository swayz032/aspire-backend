"""Wave 20.6 Stage 6a — Context-Aware Binding Scorer.

Same circular glyph means SPRINKLER on FP sheets, JUNCTION on E sheets,
DRAIN on plumbing, FIXTURE on reflected ceiling plans. Geometry alone is
insufficient — discipline + sheet context + nearby text disambiguate.

This module provides ``compute_binding_score()``, a four-component weighted
scorer consumed by Stage 5's ``propagate_legend_bindings_to_clusters`` in
place of raw cosine similarity.

Component breakdown (per plan v7 §2.7.6):
    geometry_score      = cosine_similarity(motif_embedding, legend_embedding)
    discipline_score    = 1.0 if motif_discipline == legend_discipline else 0.0
    sheet_context_score = 1.0 if motif_sheet_region in {'plan', 'detail'} else 0.5
    nearby_text_score   = Jaccard similarity(nearby_text_tokens, legend_label_tokens)

Default weights: geometry=0.20, discipline=0.35, sheet_context=0.20, nearby_text=0.25

Law compliance:
    #1 Returns a score; never decides, retries, or escalates.
    #9 Never logs raw symbol labels, spec text, or nearby OCR paragraphs.
"""
from __future__ import annotations

import math
import re
from typing import Sequence

import numpy as np

# ---------------------------------------------------------------------------
# Default weight profile (plan v7 §2.7.6)
# ---------------------------------------------------------------------------

GEOMETRY_WEIGHT: float = 0.20
DISCIPLINE_WEIGHT: float = 0.35
SHEET_CONTEXT_WEIGHT: float = 0.20
NEARBY_TEXT_WEIGHT: float = 0.25

# Sheet regions that indicate the motif is a real placed instance.
_PLACED_REGIONS: frozenset[str] = frozenset({"plan", "detail"})


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> frozenset[str]:
    """Lower-case, strip punctuation, split on whitespace and hyphens."""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return frozenset(t for t in cleaned.split() if t)


def _jaccard(set_a: frozenset[str], set_b: frozenset[str]) -> float:
    if not set_a and not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union else 0.0


# ---------------------------------------------------------------------------
# Core scoring function
# ---------------------------------------------------------------------------

def compute_binding_score(
    motif_embedding: np.ndarray,
    legend_embedding: np.ndarray,
    *,
    motif_discipline: str,
    legend_discipline: str,
    motif_sheet_region: str,
    nearby_text: list[str],
    legend_label_tokens: list[str],
    geometry_weight: float = GEOMETRY_WEIGHT,
    discipline_weight: float = DISCIPLINE_WEIGHT,
    sheet_context_weight: float = SHEET_CONTEXT_WEIGHT,
    nearby_text_weight: float = NEARBY_TEXT_WEIGHT,
) -> float:
    """Compute a combined binding score in [0, 1].

    Args:
        motif_embedding:      L2-normalised embedding vector for the candidate motif.
        legend_embedding:     L2-normalised embedding vector for the legend entry.
        motif_discipline:     Discipline code of the sheet the motif appears on
                              (e.g. ``'fire_protection'``, ``'electrical'``).
        legend_discipline:    Discipline code of the legend entry's source sheet.
        motif_sheet_region:   Region type where the motif was found:
                              ``'plan'`` | ``'detail'`` | ``'schedule'`` | ``'legend'``.
        nearby_text:          Azure OCR paragraph strings within 100 px of the motif bbox.
        legend_label_tokens:  Pre-tokenized legend label tokens
                              (e.g. ``['qr', 'pendant', 'v2708']``).
        geometry_weight:      Weight for cosine similarity component.
        discipline_weight:    Weight for discipline match component.
        sheet_context_weight: Weight for sheet region plausibility component.
        nearby_text_weight:   Weight for nearby-text / label Jaccard component.

    Returns:
        Combined score in [0.0, 1.0].  The orchestrator applies thresholds:
        score ≥ 0.92 → auto-bind; 0.75 ≤ score < 0.92 → borderline (VLM path);
        score < 0.75 → unresolved.
    """
    # 1. Geometry — cosine similarity (clipped to [0, 1])
    me = np.asarray(motif_embedding, dtype=float)
    le = np.asarray(legend_embedding, dtype=float)
    me_norm = np.linalg.norm(me)
    le_norm = np.linalg.norm(le)
    if me_norm == 0.0 or le_norm == 0.0:
        geometry_score = 0.0
    else:
        raw_cos = float(np.dot(me / me_norm, le / le_norm))
        geometry_score = max(0.0, min(1.0, raw_cos))

    # 2. Discipline — binary match
    discipline_score = 1.0 if (
        motif_discipline and legend_discipline and
        motif_discipline.lower() == legend_discipline.lower()
    ) else 0.0

    # 3. Sheet context — placed regions score higher
    sheet_context_score = 1.0 if motif_sheet_region in _PLACED_REGIONS else 0.5

    # 4. Nearby text — Jaccard similarity between OCR tokens and legend label tokens
    nearby_tokens: frozenset[str] = frozenset()
    for phrase in nearby_text:
        nearby_tokens = nearby_tokens | _tokenize(phrase)

    label_tokens: frozenset[str] = frozenset(
        t.lower() for t in legend_label_tokens if t
    )
    nearby_text_score = _jaccard(nearby_tokens, label_tokens)

    combined = (
        geometry_weight * geometry_score
        + discipline_weight * discipline_score
        + sheet_context_weight * sheet_context_score
        + nearby_text_weight * nearby_text_score
    )
    return round(min(1.0, max(0.0, combined)), 6)


__all__ = ["compute_binding_score"]
