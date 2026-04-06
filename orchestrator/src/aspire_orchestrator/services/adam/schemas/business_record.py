"""BusinessRecord — Canonical schema for local business/vendor data.

Sources: Google Places, HERE, Foursquare, TomTom, Brave/Exa (web enrichment)
Dedup: normalized address + phone + website domain + lat/lng proximity
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SourceAttribution:
    """Field-level provenance for a single source (ADR-003)."""

    provider: str
    retrieved_at: str = ""
    source_id: str = ""


@dataclass
class BusinessRecord:
    """Canonical business/vendor record."""

    name: str
    normalized_address: str = ""
    phone: str = ""
    website: str = ""
    category: str = ""
    rating: float | None = None
    review_count: int | None = None
    distance_miles: float | None = None
    hours: str = ""
    latitude: float | None = None
    longitude: float | None = None
    verification_status: str = "unverified"  # verified | partially_verified | unverified
    confidence: float = 0.0
    sources: list[SourceAttribution] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "normalized_address": self.normalized_address,
            "phone": self.phone,
            "website": self.website,
            "category": self.category,
            "rating": self.rating,
            "review_count": self.review_count,
            "distance_miles": self.distance_miles,
            "hours": self.hours,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "verification_status": self.verification_status,
            "confidence": self.confidence,
            "sources": [{"provider": s.provider, "retrieved_at": s.retrieved_at, "source_id": s.source_id} for s in self.sources],
            **self.extra,
        }
