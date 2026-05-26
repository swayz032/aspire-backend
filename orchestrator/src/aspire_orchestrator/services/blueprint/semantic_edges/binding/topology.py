"""N-3 — TopologySimilarityScorer v1 (PrimitiveHistogram fast filter).

The structural discriminator. After Stage 1 (discipline + geometry) and
Stage 2 OCR, we have a population of candidates that:
  - share the same project
  - share a compatible discipline
  - share a compatible geometry class
  - have meaningful textual overlap

These are exactly the cases where text and category are not enough.
A 'T'-in-circle (thermostat) and an 'S'-in-circle (smoke detector)
both classify as POINT and may share OCR fragments. The discriminator
IS THE SHAPE.

Design — two-pass topology (only pass 1 ships in v1):

  Pass 1 (this commit): PRIMITIVE HISTOGRAM COSINE SIMILARITY
    Each motif and legend representation is summarized as a 6-vector
    of primitive counts: (lines, arcs, circles, polygons, text, other).
    Cosine similarity over the L2-normalized vectors is fast, scale-
    invariant by construction (counts, not lengths), and orientation-
    invariant. A complex 12-line louver versus a single-circle sprinkler
    head will score near 0 even if OCR and discipline agree.

  Pass 2 (FUTURE — N-3.1): SCALE-INVARIANT GEOMETRIC OVERLAP
    Normalize both shapes into a 1×1 bounding box, align centers,
    compute Hausdorff distance over primitive endpoints. Captures
    "two intersecting triangles vs circle" mismatches that histogram
    alone misses (same primitive counts, different topology).

Why this is the right v1
------------------------
Histogram cosine is O(1) per pair, never wrong-direction (zero shape
overlap → near-zero score), and immediately measurable on Gulfstream
where motifs already carry primitive_count and legends can be enriched
from the primitive table by bbox containment. Hausdorff geometry
requires endpoint arrays and per-pair O(n²) work — defer until Pass 1
proves insufficient on the audit.

Calibrated bands (locked 2026-05-26):
    cosine >= 0.95 → 1.00   (near-identical primitive distribution)
    cosine >= 0.85 → 0.85
    cosine >= 0.70 → 0.70
    cosine >= 0.50 → 0.50
    cosine <  0.50 → 0.00   (structural rejection)

Insufficient data fallback
--------------------------
If either side has no primitive histogram attached, the scorer
returns score=1.0 with reasoning="insufficient_data". This is the
multiplicative identity — it lets OCR and other scorers carry the
weight without artificially deflating their product. Topology kills
candidates when we HAVE evidence they're shape-mismatched, not when
we lack evidence.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from .base import BaseScorer, BindingProposal, ScorerResult


# ============================================================
# Primitive histogram
# ============================================================


# The six primitive classes Drew's vector pipeline (Wave 11g-v2)
# emits. 'other' is a catch-all for ellipses, beziers, custom
# spline objects that don't fit the canonical four geometric types.
HISTOGRAM_KEYS: tuple[str, ...] = (
    "lines",      # straight line segments
    "arcs",       # arc segments (not full circles)
    "circles",    # closed circles / ellipses-as-circle
    "polygons",   # closed polylines
    "text",       # text spans inside the symbol bbox
    "other",      # bezier / spline / unknown primitive type
)


@dataclass(frozen=True)
class PrimitiveHistogram:
    """Six-dimensional vector of primitive counts.

    Use either as keyword arguments or via from_counts(). All counts
    default to 0 so callers can specify only what they know.
    """
    lines: int = 0
    arcs: int = 0
    circles: int = 0
    polygons: int = 0
    text: int = 0
    other: int = 0

    def to_tuple(self) -> tuple[int, int, int, int, int, int]:
        return (
            self.lines, self.arcs, self.circles,
            self.polygons, self.text, self.other,
        )

    @property
    def total(self) -> int:
        return sum(self.to_tuple())

    def is_empty(self) -> bool:
        return self.total == 0

    @classmethod
    def from_dict(cls, data: dict[str, int]) -> "PrimitiveHistogram":
        """Construct from a plain dict (e.g., from JSON / Supabase row).
        Unknown keys are silently ignored; missing keys default to 0.
        """
        return cls(**{k: int(data.get(k, 0)) for k in HISTOGRAM_KEYS})


# ============================================================
# Cosine similarity
# ============================================================


def cosine_similarity(
    a: PrimitiveHistogram, b: PrimitiveHistogram,
) -> float:
    """Cosine similarity in [0, 1] over the 6-D count vectors.

    Both empty → 0.0 (cannot compare; treat as no signal).
    One empty → 0.0 (totally different shape).
    Otherwise: standard cosine, clamped to [0, 1].

    Counts are non-negative so the cosine cannot be negative — the
    [0, 1] range is mathematically guaranteed.
    """
    av = a.to_tuple()
    bv = b.to_tuple()
    dot = sum(x * y for x, y in zip(av, bv))
    if dot == 0:
        return 0.0
    norm_a = math.sqrt(sum(x * x for x in av))
    norm_b = math.sqrt(sum(y * y for y in bv))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return min(1.0, max(0.0, dot / (norm_a * norm_b)))


# ============================================================
# Scorer
# ============================================================


# Locked band thresholds (2026-05-26)
_BANDS: tuple[tuple[float, float, str], ...] = (
    (0.95, 1.00, "histogram_cosine_near_identical"),
    (0.85, 0.85, "histogram_cosine_high"),
    (0.70, 0.70, "histogram_cosine_moderate"),
    (0.50, 0.50, "histogram_cosine_weak"),
)


# Keys the scorer reads from BindingProposal.motif_extra (defensive
# isolation — the proposal doesn't need to grow new top-level fields).
MOTIF_HISTOGRAM_KEY: str = "primitive_histogram"
LEGEND_HISTOGRAM_KEY: str = "legend_primitive_histogram"


def _extract_histogram(extra: dict[str, Any], key: str) -> Optional[PrimitiveHistogram]:
    """Pull a PrimitiveHistogram from a motif_extra dict, accepting
    either a PrimitiveHistogram instance or a plain dict payload.
    Returns None if the key is missing or empty.
    """
    raw = extra.get(key)
    if raw is None:
        return None
    if isinstance(raw, PrimitiveHistogram):
        return raw if not raw.is_empty() else None
    if isinstance(raw, dict):
        hist = PrimitiveHistogram.from_dict(raw)
        return hist if not hist.is_empty() else None
    return None


class TopologySimilarityScorer(BaseScorer):
    """Score motif↔legend topological similarity from primitive histograms.

    Reads both histograms from `proposal.motif_extra`:
      - 'primitive_histogram' (motif side)
      - 'legend_primitive_histogram' (legend side)

    Both expected to be either PrimitiveHistogram instances or dicts.
    """

    name = "topology"
    weight_key = "topology"

    def compute(self, proposal: BindingProposal) -> ScorerResult:
        motif_hist = _extract_histogram(proposal.motif_extra, MOTIF_HISTOGRAM_KEY)
        legend_hist = _extract_histogram(proposal.motif_extra, LEGEND_HISTOGRAM_KEY)

        # Multiplicative-identity fallback: when we lack evidence,
        # don't penalize. Pure recall protection.
        if motif_hist is None or legend_hist is None:
            missing = []
            if motif_hist is None:
                missing.append("motif")
            if legend_hist is None:
                missing.append("legend")
            return self._result(
                1.0,
                "insufficient_data",
                {"missing_side": missing},
            )

        cos = cosine_similarity(motif_hist, legend_hist)

        # Map raw cosine to a calibrated band
        score = 0.0
        reasoning = "histogram_cosine_below_floor"
        for threshold, band_score, band_label in _BANDS:
            if cos >= threshold:
                score = band_score
                reasoning = band_label
                break

        return self._result(
            score,
            reasoning,
            {
                "cosine_similarity": round(cos, 4),
                "motif_histogram": motif_hist.to_tuple(),
                "legend_histogram": legend_hist.to_tuple(),
            },
        )
