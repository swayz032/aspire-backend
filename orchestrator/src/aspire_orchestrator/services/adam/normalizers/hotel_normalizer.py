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
        phone=data.get("phone", ""),
        website=data.get("web_url", data.get("website", "")),
        amenities=amenities,
        latitude=_safe_float(data.get("latitude")),
        longitude=_safe_float(data.get("longitude")),
        sentiment_summary=data.get("ranking_data", {}).get("ranking_string", "") if data.get("ranking_data") else "",
        sources=[SourceAttribution(provider="tripadvisor", retrieved_at=datetime.now(timezone.utc).isoformat())],
    )


def normalize_from_google_places_hotel(data: dict[str, Any]) -> HotelRecord:
    """Normalize a Google Places result (hotel category) to HotelRecord."""
    location = data.get("location", {}) or {}

    # Handle both old (integer 0-4) and new (string) price level formats
    price_val = data.get("priceLevel", data.get("price_level"))
    price_map_new = {"PRICE_LEVEL_FREE": "Free", "PRICE_LEVEL_INEXPENSIVE": "$",
                     "PRICE_LEVEL_MODERATE": "$$", "PRICE_LEVEL_EXPENSIVE": "$$$",
                     "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$"}
    price_map_old = {0: "Free", 1: "$", 2: "$$", 3: "$$$", 4: "$$$$"}
    if isinstance(price_val, str):
        price_level = price_map_new.get(price_val, price_val)
    elif isinstance(price_val, int):
        price_level = price_map_old.get(price_val, "")
    else:
        price_level = ""

    # Opening hours
    hours = data.get("opening_hours", {}) or {}
    open_now = hours.get("open_now")

    return HotelRecord(
        name=data.get("displayName", {}).get("text", "") or data.get("name", ""),
        normalized_address=data.get("formattedAddress", data.get("formatted_address", "")),
        traveler_rating=data.get("rating"),
        review_count=_safe_int(data.get("userRatingCount", data.get("user_ratings_total"))),
        price_range=price_level,
        phone=data.get("phone", ""),
        website=data.get("website", ""),
        latitude=location.get("latitude", location.get("lat")),
        longitude=location.get("longitude", location.get("lng")),
        sources=[SourceAttribution(provider="google_places", retrieved_at=datetime.now(timezone.utc).isoformat())],
        extra={"open_now": open_now, "types": data.get("types", [])} if open_now is not None else {},
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
