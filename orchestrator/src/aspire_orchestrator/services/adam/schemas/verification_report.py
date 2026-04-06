"""VerificationReport — Confidence scoring and conflict detection (ADR-003).

Trust tiers:
  A (Authoritative): ATTOM, official govt → highest confidence
  B (Strong Commercial): Google Places, SerpApi, Tripadvisor
  C (Web Extraction): Brave, Exa, Tavily, Parallel

Confidence levels:
  verified: trusted source(s) with no material conflict
  partially_verified: useful but incomplete or conflicted
  unverified: insufficient evidence to assert the fact
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FieldConflict:
    """A conflict between provider values for a single field."""

    field_name: str
    values: list[dict[str, Any]] = field(default_factory=list)  # [{provider, value}]
    resolution: str = ""  # "highest_trust_wins" | "unresolved"


@dataclass
class VerificationReport:
    """Verification output for a research response."""

    status: str = "unverified"  # verified | partially_verified | unverified
    confidence_score: float = 0.0  # 0.0 - 1.0
    source_count: int = 0
    conflict_count: int = 0
    conflicts: list[FieldConflict] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    freshness_summary: str = ""
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "confidence_score": self.confidence_score,
            "source_count": self.source_count,
            "conflict_count": self.conflict_count,
            "conflicts": [
                {"field_name": c.field_name, "values": c.values, "resolution": c.resolution}
                for c in self.conflicts
            ],
            "missing_fields": self.missing_fields,
            "freshness_summary": self.freshness_summary,
            "recommendations": self.recommendations,
        }
