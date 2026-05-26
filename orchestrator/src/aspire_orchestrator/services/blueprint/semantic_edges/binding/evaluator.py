"""N-2 — BindingEvaluator: the 3-stage hybrid pipeline.

Reviewer-locked architecture (2026-05-26):

  Stage 1 — HARD GATE (cheap, binary, short-circuits)
    1. SUPERSEDED_LEGEND — addendum killed the legend definition
    2. PROJECT_MISMATCH — legend belongs to a different project
    3. DISCIPLINE_MISMATCH — reuses M-2 HARD set
       (plumbing↔electrical, plumbing↔fire, electrical↔fire)
    4. ENTITY_GEOMETRY_INCOMPATIBLE — point ↔ linear ↔ polygon mismatch
    On any HARD failure: return BindingScore(score=0.0, REJECTED,
    gate_reason=...) immediately. Don't run scorers.

  Stage 2 — WEIGHTED PRODUCT
    score = ∏ scorer.compute(proposal).score ^ weights[scorer.weight_key]
    Weights default 1.0 (pure product). Empty score = 1.0 (identity).

  Stage 3 — TIER MAPPING
    score >= 0.70 → VERIFIED        (Phase 5 acceptance threshold)
    score >= 0.50 → UNKNOWN_REVIEW
    score <  0.50 → REJECTED
"""
from __future__ import annotations

from typing import Optional, Sequence

from ..constraints.discipline import disciplines_hard_incompatible
from .base import (
    BindingProposal,
    BindingScore,
    BindingTier,
    BindingWeights,
    HardGateReason,
    Scorer,
    ScorerResult,
    compute_binding_replay_hash,
    map_tier,
)


class BindingEvaluator:
    """The 3-stage hybrid binding pipeline.

    Constructor:
      scorers: ordered list of Scorer implementations (cheap → expensive)
      weights: BindingWeights (defaults to all-1.0)

    Use:
      score = evaluator.evaluate(proposal)
    """

    def __init__(
        self,
        scorers: Sequence[Scorer],
        weights: Optional[BindingWeights] = None,
    ):
        if not scorers:
            raise ValueError("BindingEvaluator requires at least one scorer")
        # Reject duplicate scorer names (would shadow each other in receipt)
        seen: set[str] = set()
        for s in scorers:
            if s.name in seen:
                raise ValueError(f"duplicate scorer name: {s.name!r}")
            seen.add(s.name)
        self.scorers = tuple(scorers)
        self.weights = weights or BindingWeights()

    # ============================================================
    # Top-level
    # ============================================================

    def evaluate(self, proposal: BindingProposal) -> BindingScore:
        replay_hash = compute_binding_replay_hash(proposal)

        # Stage 1 — HARD GATE
        gate_reason = self._check_hard_gates(proposal)
        if gate_reason is not None:
            return BindingScore(
                motif_id=proposal.motif_id,
                legend_id=proposal.legend_id,
                project_id=proposal.project_id,
                final_score=0.0,
                tier=BindingTier.REJECTED,
                gate_passed=False,
                gate_reason=gate_reason,
                per_factor_scores={},
                weights_applied=self.weights,
                replay_hash=replay_hash,
            )

        # Stage 2 — WEIGHTED PRODUCT
        per_factor: dict[str, ScorerResult] = {}
        product = 1.0
        for scorer in self.scorers:
            result = scorer.compute(proposal)
            per_factor[scorer.name] = result
            w = self.weights.for_key(scorer.weight_key)
            # score^w: w=0 → 1.0 (factor ignored); w=1 → score (normal);
            # w>1 → score dragged down faster (factor emphasized)
            if w == 0:
                continue
            if result.score == 0.0:
                product = 0.0
                # Don't break — we still want to record every scorer's
                # output for audit/diagnostic transparency
            else:
                product *= result.score ** w

        # Clamp (floating-point hygiene)
        product = max(0.0, min(1.0, product))

        # Stage 3 — TIER MAPPING
        return BindingScore(
            motif_id=proposal.motif_id,
            legend_id=proposal.legend_id,
            project_id=proposal.project_id,
            final_score=product,
            tier=map_tier(product),
            gate_passed=True,
            gate_reason=None,
            per_factor_scores=per_factor,
            weights_applied=self.weights,
            replay_hash=replay_hash,
        )

    # ============================================================
    # Stage 1 helpers
    # ============================================================

    def _check_hard_gates(
        self, p: BindingProposal,
    ) -> Optional[HardGateReason]:
        # Gate 1 — superseded legend (cheapest check first)
        if p.legend_superseded:
            return HardGateReason.SUPERSEDED_LEGEND

        # Gate 2 — project mismatch (legend belongs to a different project)
        if p.legend_project_id and p.legend_project_id != p.project_id:
            return HardGateReason.PROJECT_MISMATCH

        # Gate 3 — discipline mismatch (reuses M-2 HARD matrix)
        if p.motif_discipline and p.legend_discipline:
            if disciplines_hard_incompatible(p.motif_discipline, p.legend_discipline):
                return HardGateReason.DISCIPLINE_MISMATCH

        # Gate 4 — entity-geometry incompatibility
        # 'unknown' on either side → fail open
        if (
            p.motif_entity_geometry != "unknown"
            and p.legend_entity_geometry != "unknown"
            and p.motif_entity_geometry != p.legend_entity_geometry
        ):
            return HardGateReason.ENTITY_GEOMETRY_INCOMPATIBLE

        return None
