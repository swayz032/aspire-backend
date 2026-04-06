"""HotelRecord — Canonical schema for hotel/hospitality data.

Sources: Google Places (primary), Tripadvisor (enrichment), HERE, Exa
Dedup: normalized name + address + geo proximity
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution


@dataclass
class HotelRecord:
    """Canonical hotel/hospitality record — full potential."""

    # Identity
    name: str = ""
    normalized_address: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""

    # Ratings
    star_rating: float | None = None  # Hotel class (1-5 stars)
    traveler_rating: float | None = None  # Overall rating (e.g. 4.0/5)
    review_count: int | None = None
    rating_breakdown: dict[str, int] = field(default_factory=dict)  # {"5": 71, "4": 17, ...}
    subratings: dict[str, float] = field(default_factory=dict)  # {"Location": 3.4, "Sleep Quality": 3.5}

    # Pricing & Style
    price_range: str = ""  # $, $$, $$$, $$$$
    styles: list[str] = field(default_factory=list)  # ["Budget", "Business"]

    # Contact
    phone: str = ""
    website: str = ""
    tripadvisor_url: str = ""
    booking_url: str = ""

    # Amenities
    amenities: list[str] = field(default_factory=list)

    # Description & Sentiment
    description: str = ""
    sentiment_summary: str = ""  # TA ranking string: "#1 of 12 hotels in Tucker"
    ta_ranking: str = ""  # Numeric ranking position

    # Trip Types — who stays here (from TA)
    trip_types: dict[str, int] = field(default_factory=dict)  # {"Business": 23, "Family": 26}

    # Location
    latitude: float | None = None
    longitude: float | None = None
    distance_to_meeting_miles: float | None = None
    distance_to_airport_miles: float | None = None

    # Hours
    open_now: bool | None = None
    hours_text: str = ""  # "Open 24 hours" or weekday schedule

    # Media
    photos: list[str] = field(default_factory=list)
    photo_count: int | None = None

    # Verification
    verification_status: str = "unverified"
    confidence: float = 0.0
    sources: list[SourceAttribution] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k not in ("sources", "extra")}
        d["sources"] = [{"provider": s.provider, "retrieved_at": s.retrieved_at} for s in self.sources]
        d.update(self.extra)
        return d
