"""N-1 — Base protocols + dataclasses for the binding evaluator.

Pure functions only — every scorer is a stateless transform from
BindingProposal to ScorerResult. No I/O.

Locked architecture (reviewer 2026-05-26):
  - HARD gates run before any scorer (Stage 1)
  - Weighted product across scorers (Stage 2)
  - Tier mapping at 0.70 / 0.50 thresholds (Stage 3)
  - Weights default 1.0 (pure product) — calibrate via audit, not guesswork
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol


# ============================================================
# Tier thresholds — locked by reviewer 2026-05-26
# ============================================================

VERIFIED_THRESHOLD: float = 0.70
UNKNOWN_REVIEW_THRESHOLD: float = 0.50


class BindingTier(str, Enum):
    """Resolution tier for a binding proposal."""
    VERIFIED = "verified"               # score >= 0.70 → persist as edge
    UNKNOWN_REVIEW = "unknown_review"   # 0.50 <= score < 0.70 → GC review
    REJECTED = "rejected"               # score < 0.50 → drop


def map_tier(score: float) -> BindingTier:
    """Pure tier-mapping function. Single source of truth."""
    if score >= VERIFIED_THRESHOLD:
        return BindingTier.VERIFIED
    if score >= UNKNOWN_REVIEW_THRESHOLD:
        return BindingTier.UNKNOWN_REVIEW
    return BindingTier.REJECTED


# ============================================================
# Hard-gate reasons
# ============================================================


class HardGateReason(str, Enum):
    """Why Stage 1 short-circuited a binding proposal."""
    DISCIPLINE_MISMATCH = "discipline_mismatch"
    PROJECT_MISMATCH = "project_mismatch"
    ENTITY_GEOMETRY_INCOMPATIBLE = "entity_geometry_incompatible"
    SUPERSEDED_LEGEND = "superseded_legend"


# ============================================================
# Geometry classes for HARD gate #3
# ============================================================
# Linear element (pipe, duct, wire) cannot bind a point fixture (sink,
# outlet, sprinkler head). Polygon (zone, slab) cannot bind a point
# fixture. These are physical impossibilities — HARD reject.

ENTITY_GEOMETRY_VALUES: frozenset[str] = frozenset({
    "point",     # zero-dimensional fixture symbol
    "linear",    # one-dimensional line / curve (pipe, duct, conduit)
    "polygon",   # two-dimensional area (room, zone, slab)
    "none",      # NOT a graphical entity — glossary / abbreviation row
    "unknown",   # cannot determine — passes the gate (fail open)
})


# ============================================================
# Inputs + outputs
# ============================================================


@dataclass(frozen=True)
class BindingProposal:
    """One (motif, legend) candidate pair being evaluated for binding.

    All fields are pre-extracted by the caller; the evaluator reads
    them through scorers without doing any DB work itself.
    """
    # Tenant scoping (Law #6)
    suite_id: str
    project_id: str

    # ---- Motif side (the symbol on the sheet) ----
    motif_id: str
    motif_entity_type: str                       # 'primitive' | 'composed_symbol'
    motif_sheet_id: str
    motif_entity_geometry: str = "unknown"       # ENTITY_GEOMETRY_VALUES
    motif_discipline: Optional[str] = None       # set if generator knew (e.g. from sheet code prefix)
    motif_bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    motif_nearby_ocr_text: tuple[str, ...] = ()  # OCR spans within proximity
    motif_extra: dict[str, Any] = field(default_factory=dict)

    # ---- Legend side (the candidate definition) ----
    legend_id: str = ""
    legend_label: Optional[str] = None
    legend_symbol_id: Optional[str] = None
    legend_specification: Optional[str] = None
    legend_discipline: Optional[str] = None
    legend_entity_geometry: str = "unknown"
    legend_project_id: Optional[str] = None       # populated for cross-project gate
    legend_superseded: bool = False               # marked dead by addendum

    def __post_init__(self):
        if self.motif_entity_geometry not in ENTITY_GEOMETRY_VALUES:
            raise ValueError(
                f"motif_entity_geometry={self.motif_entity_geometry!r} not in "
                f"{sorted(ENTITY_GEOMETRY_VALUES)}"
            )
        if self.legend_entity_geometry not in ENTITY_GEOMETRY_VALUES:
            raise ValueError(
                f"legend_entity_geometry={self.legend_entity_geometry!r} not in "
                f"{sorted(ENTITY_GEOMETRY_VALUES)}"
            )


@dataclass(frozen=True)
class ScorerResult:
    """One scorer's output — score in [0, 1] + reasoning + evidence."""
    scorer_name: str
    score: float
    reasoning: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(
                f"{self.scorer_name}: score={self.score} out of [0,1]"
            )


@dataclass(frozen=True)
class BindingWeights:
    """Per-scorer weights. Defaults to 1.0 (pure product, no tuning)."""
    topology: float = 1.0
    ocr_affinity: float = 1.0
    connectivity: float = 1.0
    spatial_prior: float = 1.0
    family_coherence: float = 1.0

    def for_key(self, weight_key: str) -> float:
        """Lookup by scorer.weight_key. Default 1.0 if unknown."""
        return float(getattr(self, weight_key, 1.0))


@dataclass(frozen=True)
class BindingScore:
    """Final output of the 3-stage evaluator. Receipt-ready."""
    motif_id: str
    legend_id: str
    project_id: str

    final_score: float
    tier: BindingTier

    gate_passed: bool
    gate_reason: Optional[HardGateReason]

    per_factor_scores: dict[str, ScorerResult]
    weights_applied: BindingWeights

    replay_hash: str

    @property
    def is_verified(self) -> bool:
        return self.tier == BindingTier.VERIFIED


# ============================================================
# Scorer Protocol + BaseScorer ABC
# ============================================================


class Scorer(Protocol):
    """Pluggable scoring component. Pure function: proposal → score in [0, 1]."""
    name: str
    weight_key: str

    def compute(self, proposal: BindingProposal) -> ScorerResult: ...


class BaseScorer(ABC):
    """ABC for ergonomic scorer implementations.

    Subclasses set class-level `name` + `weight_key`, override `compute()`,
    and use `_result()` / `_zero()` helpers to construct ScorerResults.
    """

    name: str = ""
    weight_key: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return
        if not cls.name:
            raise TypeError(f"{cls.__name__}: must set class attr `name`")
        if not cls.weight_key:
            raise TypeError(f"{cls.__name__}: must set class attr `weight_key`")

    @abstractmethod
    def compute(self, proposal: BindingProposal) -> ScorerResult: ...

    def _result(
        self,
        score: float,
        reasoning: str,
        evidence: Optional[dict[str, Any]] = None,
    ) -> ScorerResult:
        return ScorerResult(
            scorer_name=self.name,
            score=max(0.0, min(1.0, score)),
            reasoning=reasoning,
            evidence=evidence or {},
        )

    def _zero(
        self,
        reasoning: str = "no_signal",
        evidence: Optional[dict[str, Any]] = None,
    ) -> ScorerResult:
        return self._result(0.0, reasoning, evidence)


# ============================================================
# Replay hash
# ============================================================


def compute_binding_replay_hash(proposal: BindingProposal) -> str:
    """Deterministic SHA-1 over (motif, legend, project) identity.

    Replay key for the audit ledger — same proposal always produces
    same hash, regardless of when or where evaluation runs.
    """
    canonical = json.dumps(
        {
            "project_id": str(proposal.project_id),
            "motif_id": str(proposal.motif_id),
            "motif_entity_type": str(proposal.motif_entity_type),
            "legend_id": str(proposal.legend_id),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:24]
