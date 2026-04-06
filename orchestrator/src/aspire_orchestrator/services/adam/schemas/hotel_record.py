"""HotelRecord — Canonical schema for hotel/hospitality data.

Sources: Tripadvisor (primary), HERE, Google Places
Dedup: normalized name + address + geo proximity
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution


@dataclass
class HotelRecord:
    """Canonical hotel/hospitality record."""

    name: str = ""
    normalized_address: str = ""
    star_rating: float | None = None
    traveler_rating: float | None = None
    review_count: int | None = None
    price_range: str = ""
    amenities: list[str] = field(default_factory=list)
    distance_to_meeting_miles: float | None = None
    distance_to_airport_miles: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    photos: list[str] = field(default_factory=list)
    sentiment_summary: str = ""
    verification_status: str = "unverified"
    confidence: float = 0.0
    sources: list[SourceAttribution] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k not in ("sources", "extra")}
        d["sources"] = [{"provider": s.provider, "retrieved_at": s.retrieved_at} for s in self.sources]
        d.update(self.extra)
        return d
