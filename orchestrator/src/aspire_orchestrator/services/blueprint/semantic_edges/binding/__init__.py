"""Wave 20.14.5N (CSIR Phase 5) — Referential Binding (the breakthrough layer).

This subpackage is where Drew finally tells the GC what every symbol IS.
Inputs: motifs (symbols on a sheet) + legends (definitions in the
project legend table). Output: VERIFIED bindings that become real
LEGEND_ENTRY_FOR edges in the semantic graph, which downstream
material aggregation rolls into counts.

3-stage hybrid evaluator (locked by reviewer 2026-05-26):

  Stage 1 — HARD GATE (cheap, binary, short-circuits)
    Reuses M-2 discipline matrix + adds project/geometry/superseded gates.
    Returns 0.0 score immediately on any HARD failure — no expensive
    topology / OCR / family-coherence work runs.

  Stage 2 — WEIGHTED PRODUCT (smooth, all factors)
    binding_score = topology^w_t × OCR^w_o × connectivity^w_c
                    × spatial^w_s × family^w_f
    Weights default to 1.0 (pure product) — calibrated later via audit.

  Stage 3 — TIER MAPPING
    score >= 0.70 → VERIFIED        (persisted as LEGEND_ENTRY_FOR edge)
    score >= 0.50 → UNKNOWN_REVIEW  (surfaced to GC for verification)
    score <  0.50 → REJECTED        (receipt only, no edge persisted)

The 0.70 threshold IS the locked Phase 5 acceptance target: ≥70%
verified motif-to-legend binding precision before any autonomous
material counting ships.

Scorers (filled in over the wave):
  - OCRAffinityScorer       (N-2 — low-hanging fruit, ship first)
  - TopologySimilarityScorer (N-3+ — heavy vector math)
  - ConnectivityScorer       (N-4+ — reuses M-4 fixture roles)
  - SpatialPriorScorer       (N-5+ — proximity to other binds)
  - FamilyCoherenceScorer    (N-6+ — propagation boost)
"""
from .base import (  # noqa: F401
    BaseScorer,
    BindingProposal,
    BindingScore,
    BindingTier,
    BindingWeights,
    HardGateReason,
    Scorer,
    ScorerResult,
    VERIFIED_THRESHOLD,
    UNKNOWN_REVIEW_THRESHOLD,
    compute_binding_replay_hash,
    map_tier,
)
from .ocr_affinity import OCRAffinityScorer  # noqa: F401
from .evaluator import BindingEvaluator  # noqa: F401
from .geometry import (  # noqa: F401
    GeometryClass,
    LINEAR_KEYWORDS,
    NONE_KEYWORDS,
    POINT_KEYWORDS,
    POLYGON_KEYWORDS,
    MotifGeometryFeatures,
    classify_legend_geometry,
    classify_motif_geometry,
    geometry_gate_compatible,
)
