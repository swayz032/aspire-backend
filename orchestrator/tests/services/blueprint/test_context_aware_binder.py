"""Tests for Stage 6a — context_aware_binder.compute_binding_score().

Four required tests (per plan v7 §2.7.6):
1. Same geometry + same discipline + same context → score ≥ 0.92 (auto-bind)
2. Same geometry + DIFFERENT discipline → score < 0.92 even when cosine_sim == 1.0
3. Same geometry + same discipline + matching nearby text → score boost over geometry alone
4. Token-overlap: label "QR Pendant V2708", nearby contains "QR-PENDANT" → nearby_text_score ≥ 0.5
"""
from __future__ import annotations

import numpy as np
import pytest

from aspire_orchestrator.services.blueprint.context_aware_binder import (
    _jaccard,
    _tokenize,
    compute_binding_score,
)

# Shared fixtures -----------------------------------------------------------

def _unit_vec(n: int = 128) -> np.ndarray:
    """Return a unit-length embedding vector (all-ones normalised)."""
    v = np.ones(n, dtype=float)
    return v / np.linalg.norm(v)


# ---------------------------------------------------------------------------
# Test 1 — perfect geometry + matching discipline + plan region → auto-bind
# ---------------------------------------------------------------------------

def test_perfect_match_scores_above_auto_bind_threshold() -> None:
    """Same geometry, discipline, and region → combined score ≥ 0.92."""
    motif = _unit_vec()
    legend = _unit_vec()  # identical vector → cosine_sim = 1.0

    score = compute_binding_score(
        motif,
        legend,
        motif_discipline="fire_protection",
        legend_discipline="fire_protection",
        motif_sheet_region="plan",
        nearby_text=["SPRINKLER HEAD"],
        legend_label_tokens=["sprinkler", "head"],
    )

    assert score >= 0.92, (
        f"Expected auto-bind score ≥ 0.92 for identical embeddings + same discipline "
        f"+ plan region; got {score:.4f}"
    )


# ---------------------------------------------------------------------------
# Test 2 — identical geometry, DIFFERENT discipline → score < 0.92
# ---------------------------------------------------------------------------

def test_cross_discipline_rejected_despite_perfect_geometry() -> None:
    """cosine_sim = 1.0 but discipline mismatch → combined score < 0.92.

    This verifies that discipline_weight (0.35) is heavy enough to prevent
    cross-discipline auto-binding even when geometry is perfect.
    """
    motif = _unit_vec()
    legend = _unit_vec()  # same vector

    score = compute_binding_score(
        motif,
        legend,
        motif_discipline="fire_protection",
        legend_discipline="electrical",   # DIFFERENT
        motif_sheet_region="plan",
        nearby_text=[],
        legend_label_tokens=[],
    )

    # geometry(1.0)*0.20 + discipline(0.0)*0.35 + sheet(1.0)*0.20 + text(0.0)*0.25 = 0.40
    assert score < 0.92, (
        f"Cross-discipline binding must score < 0.92; got {score:.4f}"
    )
    # Sanity: the score should be exactly 0.40 (geometry + sheet context only)
    assert abs(score - 0.40) < 0.001, (
        f"Expected 0.40 for geometry+sheet only with discipline mismatch; got {score:.4f}"
    )


# ---------------------------------------------------------------------------
# Test 3 — nearby text boost over geometry-only baseline
# ---------------------------------------------------------------------------

def test_nearby_text_boosts_score_over_geometry_alone() -> None:
    """Same geometry + same discipline + matching nearby text scores higher than geometry alone."""
    motif = _unit_vec()
    legend = _unit_vec()

    base_score = compute_binding_score(
        motif,
        legend,
        motif_discipline="electrical",
        legend_discipline="electrical",
        motif_sheet_region="plan",
        nearby_text=[],        # no nearby text
        legend_label_tokens=["junction", "box"],
    )

    boosted_score = compute_binding_score(
        motif,
        legend,
        motif_discipline="electrical",
        legend_discipline="electrical",
        motif_sheet_region="plan",
        nearby_text=["JUNCTION BOX"],   # matching text
        legend_label_tokens=["junction", "box"],
    )

    assert boosted_score > base_score, (
        f"Matching nearby text should increase score; base={base_score:.4f} boosted={boosted_score:.4f}"
    )


# ---------------------------------------------------------------------------
# Test 4 — token overlap: "QR Pendant V2708" vs nearby "QR-PENDANT"
# ---------------------------------------------------------------------------

def test_nearby_text_score_with_hyphenated_label() -> None:
    """Token tokenizer handles hyphens; Jaccard("QR Pendant V2708", "QR-PENDANT") ≥ 0.5.

    Tokenizing "QR-PENDANT" → {'qr', 'pendant'}.
    Legend tokens for "QR Pendant V2708" → ['qr', 'pendant', 'v2708'].
    Intersection = {'qr', 'pendant'} (size 2), union = {'qr', 'pendant', 'v2708'} (size 3).
    Jaccard = 2/3 ≈ 0.667.
    """
    nearby_tokens = _tokenize("QR-PENDANT")
    label_tokens = frozenset(["qr", "pendant", "v2708"])

    score = _jaccard(nearby_tokens, label_tokens)

    assert score >= 0.5, (
        f"Expected Jaccard ≥ 0.5 for 'QR-PENDANT' vs ['qr','pendant','v2708']; got {score:.4f}"
    )
    # Exact value: 2/3
    assert abs(score - 2 / 3) < 0.001, (
        f"Expected Jaccard = 2/3 ≈ 0.667; got {score:.4f}"
    )


# ---------------------------------------------------------------------------
# Additional edge-case: zero-vector embeddings don't crash
# ---------------------------------------------------------------------------

def test_zero_embeddings_return_valid_score() -> None:
    zero = np.zeros(128, dtype=float)
    score = compute_binding_score(
        zero,
        zero,
        motif_discipline="plumbing",
        legend_discipline="plumbing",
        motif_sheet_region="plan",
        nearby_text=[],
        legend_label_tokens=[],
    )
    assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Additional edge-case: schedule region scores lower than plan region
# ---------------------------------------------------------------------------

def test_schedule_region_scores_lower_than_plan_region() -> None:
    motif = _unit_vec()
    legend = _unit_vec()

    plan_score = compute_binding_score(
        motif, legend,
        motif_discipline="mechanical",
        legend_discipline="mechanical",
        motif_sheet_region="plan",
        nearby_text=[],
        legend_label_tokens=[],
    )
    schedule_score = compute_binding_score(
        motif, legend,
        motif_discipline="mechanical",
        legend_discipline="mechanical",
        motif_sheet_region="schedule",
        nearby_text=[],
        legend_label_tokens=[],
    )
    assert plan_score > schedule_score, (
        f"Plan region should score higher than schedule; plan={plan_score:.4f} schedule={schedule_score:.4f}"
    )
