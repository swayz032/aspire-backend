"""ResearchResponse — Final output contract for Adam research results.

Every research response includes: artifact_type, summary, records,
sources, freshness, confidence, missing_fields, next_queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.verification_report import VerificationReport


@dataclass
class ResearchResponse:
    """Final research output artifact."""

    artifact_type: str = ""  # VendorShortlist, PropertyFactPack, PriceComparison, etc.
    summary: str = ""
    records: list[dict[str, Any]] = field(default_factory=list)
    sources: list[SourceAttribution] = field(default_factory=list)
    freshness: dict[str, str] = field(default_factory=dict)  # {"mode": "live", "provider": "..."}
    confidence: dict[str, Any] = field(default_factory=dict)  # {"status": "verified", "score": 0.91}
    missing_fields: list[str] = field(default_factory=list)
    next_queries: list[str] = field(default_factory=list)
    verification_report: VerificationReport | None = None
    segment: str = ""
    intent: str = ""
    playbook: str = ""
    providers_called: list[str] = field(default_factory=list)
    cost_estimate: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)  # Store info, metadata, etc.

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "artifact_type": self.artifact_type,
            "summary": self.summary,
            "records": self.records,
            "sources": [{"provider": s.provider, "retrieved_at": s.retrieved_at} for s in self.sources],
            "freshness": self.freshness,
            "confidence": self.confidence,
            "missing_fields": self.missing_fields,
            "next_queries": self.next_queries,
            "segment": self.segment,
            "intent": self.intent,
            "playbook": self.playbook,
            "providers_called": self.providers_called,
            "cost_estimate": self.cost_estimate,
            "extra": self.extra,
        }
        if self.verification_report:
            d["verification_report"] = self.verification_report.to_dict()
        return d
