"""Wave 20.14.5L L-3 — Generator run metrics.

Per-run telemetry. Captured during EdgeGenerator.run() and serialized
into blueprint_semantic_edge_generation_receipts. Used to diagnose
recall collapse + graph explosion before they reach production.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class GeneratorHealth(str, Enum):
    """Health verdict derived from UNKNOWN_REVIEW quota.

    Thresholds (reviewer-locked 2026-05-25):
      <= 30% UNKNOWN_REVIEW  -> GREEN
      <= 50%                 -> YELLOW
      <= 70%                 -> RED
      >  70%                 -> HARD_FAIL  (run aborts; no edges persisted)
    """
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    HARD_FAIL = "hard_fail"


# Reviewer-locked thresholds. Do not change without acceptance gate review.
HEALTH_YELLOW_THRESHOLD = 0.30
HEALTH_RED_THRESHOLD = 0.50
HEALTH_HARD_FAIL_THRESHOLD = 0.70


def compute_health(unknown_review_count: int, total_candidates: int) -> GeneratorHealth:
    """Pure function — derives health from UNKNOWN_REVIEW ratio."""
    if total_candidates == 0:
        return GeneratorHealth.GREEN
    ratio = unknown_review_count / total_candidates
    if ratio > HEALTH_HARD_FAIL_THRESHOLD:
        return GeneratorHealth.HARD_FAIL
    if ratio > HEALTH_RED_THRESHOLD:
        return GeneratorHealth.RED
    if ratio > HEALTH_YELLOW_THRESHOLD:
        return GeneratorHealth.YELLOW
    return GeneratorHealth.GREEN


@dataclass
class StageMetrics:
    """Per-stage observability. All counts default to 0."""
    name: str
    candidates_in: int = 0
    candidates_kept: int = 0
    candidates_rejected: int = 0
    rejected_by_reason: Counter[str] = field(default_factory=Counter)
    runtime_ms: float = 0.0
    expensive_ops_used: int = 0

    @property
    def pass_through_ratio(self) -> float:
        if self.candidates_in == 0:
            return 0.0
        return self.candidates_kept / self.candidates_in


@dataclass
class RunMetrics:
    """Aggregate per-run metrics. Populated by EdgeGenerator.run()."""

    # Identity
    generator_name: str = ""
    generator_version: str = ""
    taxonomy_version: str = ""
    pipeline_version: str = ""

    # Stage breakdown — 7 stages (0..6) but constraint is shared from L-2 validator
    stages: dict[str, StageMetrics] = field(default_factory=dict)

    # Top-line counts
    candidates_considered: int = 0
    edges_emitted: int = 0
    unknown_review_count: int = 0
    rejected_count: int = 0
    locality_trimmed_count: int = 0

    # Distribution
    confidence_distribution: dict[str, int] = field(default_factory=dict)  # 0.0-0.1, 0.1-0.2, ...
    edges_by_type: Counter[str] = field(default_factory=Counter)
    edges_by_locality: Counter[str] = field(default_factory=Counter)
    rejected_by_reason: Counter[str] = field(default_factory=Counter)
    unknown_review_reasons: Counter[str] = field(default_factory=Counter)

    # Locality observability
    avg_neighborhood_size: float = 0.0
    max_neighborhood_size: int = 0
    locality_violations: int = 0

    # Performance
    runtime_ms_total: float = 0.0
    expensive_ops_total: int = 0
    cost_usd: float = 0.0

    # Replay determinism
    input_evidence_hash: str = ""
    output_edge_hash: str = ""

    # Final verdict
    health: GeneratorHealth = GeneratorHealth.GREEN
    aborted: bool = False
    abort_reason: str = ""

    def stage(self, name: str) -> StageMetrics:
        """Lazy-create + return a StageMetrics by name."""
        if name not in self.stages:
            self.stages[name] = StageMetrics(name=name)
        return self.stages[name]

    def record_confidence(self, confidence: float) -> None:
        """Bucket confidence into 10 deciles for distribution observability."""
        bucket = min(int(confidence * 10), 9)
        key = f"{bucket / 10:.1f}-{(bucket + 1) / 10:.1f}"
        self.confidence_distribution[key] = self.confidence_distribution.get(key, 0) + 1

    def to_receipt_dict(self) -> dict[str, Any]:
        """Convert to the dict shape expected by
        blueprint_semantic_edge_generation_receipts insert."""
        return {
            "edges_emitted": self.edges_emitted,
            "edges_by_type": dict(self.edges_by_type),
            "edges_by_locality": dict(self.edges_by_locality),
            "candidates_considered": self.candidates_considered,
            "cheap_filter_kept": self.stages.get("stage1_cheap_filter",
                                                  StageMetrics(name="")).candidates_kept,
            "medium_filter_kept": self.stages.get("stage3_ocr_affinity",
                                                   StageMetrics(name="")).candidates_kept,
            "expensive_filter_kept": self.stages.get("stage4_topology",
                                                      StageMetrics(name="")).candidates_kept,
            "avg_neighborhood_size": self.avg_neighborhood_size,
            "max_neighborhood_size": self.max_neighborhood_size,
            "locality_violations": self.locality_violations,
            "unknown_review_count": self.unknown_review_count,
            "unknown_review_reasons": dict(self.unknown_review_reasons),
            "runtime_ms": self.runtime_ms_total,
            "cost_usd": self.cost_usd or None,
            "input_evidence_hash": self.input_evidence_hash,
            "output_edge_hash": self.output_edge_hash,
        }
