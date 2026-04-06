"""Normalize provider responses to HotelRecord.

Handles: Tripadvisor, Google Places, HERE
Dedup: normalized name + address + geo proximity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.hotel_record import HotelRecord


def normalize_from_tripadvisor(data: dict[str, Any]) -> HotelRecord:
    """Normalize a Tripadvisor location search result to HotelRecord."""
    address = data.get("address_obj", {}) or {}
    address_str = ", ".join(filter(None, [
        address.get("street1", ""),
        address.get("city", ""),
        address.get("state", ""),
        address.get("postalcode", ""),
    ]))

    # Extract amenities from subcategory or amenities field
    amenities: list[str] = []
    for sub in data.get("subcategory", []):
        if isinstance(sub, dict) and sub.get("name"):
            amenities.append(sub["name"])

    return HotelRecord(
        name=data.get("name", ""),
        normalized_address=address_str or data.get("address_string", ""),
        star_rating=_safe_float(data.get("hotel_class")),
        traveler_rating=_safe_float(data.get("rating")),
        review_count=_safe_int(data.get("num_reviews")),
        price_range=data.get("price_level", "") or data.get("price", ""),
        amenities=amenities,
        latitude=_safe_float(data.get("latitude")),
        longitude=_safe_float(data.get("longitude")),
        sentiment_summary=data.get("ranking_data", {}).get("ranking_string", "") if data.get("ranking_data") else "",
        sources=[SourceAttribution(provider="tripadvisor", retrieved_at=datetime.now(timezone.utc).isoformat())],
    )


def normalize_from_google_places_hotel(data: dict[str, Any]) -> HotelRecord:
    """Normalize a Google Places result (hotel category) to HotelRecord."""
    location = data.get("location", {}) or {}

    # Map Google Places priceLevel to readable string
    price_map = {"PRICE_LEVEL_FREE": "Free", "PRICE_LEVEL_INEXPENSIVE": "$",
                 "PRICE_LEVEL_MODERATE": "$$", "PRICE_LEVEL_EXPENSIVE": "$$$",
                 "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$"}
    price_level = price_map.get(data.get("priceLevel", ""), "")

    return HotelRecord(
        name=data.get("displayName", {}).get("text", "") or data.get("name", ""),
        normalized_address=data.get("formattedAddress", ""),
        traveler_rating=data.get("rating"),
        review_count=data.get("userRatingCount"),
        price_range=price_level,
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
        sources=[SourceAttribution(provider="google_places", retrieved_at=datetime.now(timezone.utc).isoformat())],
    )


def normalize_from_here_hotel(data: dict[str, Any]) -> HotelRecord:
    """Normalize a HERE search result (hotel/accommodation) to HotelRecord."""
    address = data.get("address", {}) or {}
    position = data.get("position", {}) or {}
    contacts = data.get("contacts", [{}])
    website = ""
    if contacts and isinstance(contacts, list):
        for c in contacts:
            for w in c.get("www", []):
                if not website:
                    website = w.get("value", "")

    return HotelRecord(
        name=data.get("title", ""),
        normalized_address=address.get("label", ""),
        latitude=position.get("lat"),
        longitude=position.get("lng"),
        sources=[SourceAttribution(provider="here", retrieved_at=datetime.now(timezone.utc).isoformat())],
        extra={"website": website} if website else {},
    )


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None
