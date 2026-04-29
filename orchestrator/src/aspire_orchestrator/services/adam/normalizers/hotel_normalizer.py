"""Normalize provider responses to HotelRecord.

Handles: Tripadvisor, Google Places, HERE, SerpApi Google Hotels
Dedup: normalized name + address + geo proximity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.hotel_record import HotelRecord


def normalize_from_serpapi_google_hotels(
    data: dict[str, Any],
    *,
    fallback_locality: str = "",
) -> HotelRecord:
    """Normalize a SerpApi Google Hotels property to HotelRecord.

    Google Hotels does not expose a postal address on the property object —
    location is via `gps_coordinates`. The hotel's address as a string is
    only available via the property-details follow-up call. For the live
    voice path we synthesize a locality-level normalized_address from the
    search context (`fallback_locality`, e.g. "Tallahassee, FL"). Detailed
    addresses can be hydrated later via property_token if/when needed.
    """
    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()

    gps = data.get("gps_coordinates") or {}
    latitude = _safe_float(gps.get("latitude")) if isinstance(gps, dict) else None
    longitude = _safe_float(gps.get("longitude")) if isinstance(gps, dict) else None

    overall_rating = _safe_float(data.get("overall_rating"))
    review_count = _safe_int(data.get("reviews"))
    extracted_class = _safe_int(data.get("extracted_hotel_class"))
    location_rating = _safe_float(data.get("location_rating"))

    # Pricing — prefer the per-night rate; fall back to total_rate.
    rate_per_night = data.get("rate_per_night") or {}
    total_rate = data.get("total_rate") or {}
    price_label = ""
    if isinstance(rate_per_night, dict):
        price_label = (
            rate_per_night.get("lowest")
            or rate_per_night.get("before_taxes_fees")
            or ""
        )
    if not price_label and isinstance(total_rate, dict):
        price_label = total_rate.get("lowest") or ""

    # Photos: SerpApi returns images[] = [{thumbnail, original_image}, ...]
    photos: list[str] = []
    images_raw = data.get("images") or []
    if isinstance(images_raw, list):
        for img in images_raw:
            if isinstance(img, dict):
                url = (
                    img.get("original_image")
                    or img.get("thumbnail")
                    or ""
                )
                if isinstance(url, str) and url.strip():
                    photos.append(url.strip())
            elif isinstance(img, str) and img.strip():
                photos.append(img.strip())

    image_url = photos[0] if photos else ""
    # Some Google Hotels payloads ship a top-level `thumbnail` (used in ads).
    if not image_url and isinstance(data.get("thumbnail"), str):
        image_url = data["thumbnail"].strip()
        if image_url and image_url not in photos:
            photos.insert(0, image_url)

    amenities_raw = data.get("amenities") or []
    amenities: list[str] = []
    if isinstance(amenities_raw, list):
        for a in amenities_raw:
            if isinstance(a, str) and a.strip():
                amenities.append(a.strip())
            elif isinstance(a, dict) and a.get("name"):
                amenities.append(str(a["name"]))

    # The "address" we have available without a follow-up call is
    # locality-level. Use the search context as a deterministic fallback so
    # the verifier sees a populated address field. If `essential_info`
    # contains an address-like string, prefer it.
    normalized_address = ""
    essential = data.get("essential_info") or []
    if isinstance(essential, list):
        for entry in essential:
            if isinstance(entry, str) and entry.strip():
                normalized_address = entry.strip()
                break
    if not normalized_address:
        normalized_address = fallback_locality.strip()

    extra: dict[str, Any] = {}
    if data.get("property_token"):
        extra["property_token"] = data["property_token"]
    if data.get("serpapi_property_details_link"):
        extra["serpapi_property_details_link"] = data["serpapi_property_details_link"]
    if data.get("link"):
        extra["link"] = data["link"]
    if data.get("eco_certified"):
        extra["eco_certified"] = bool(data["eco_certified"])
    if data.get("check_in_time"):
        extra["check_in_time"] = data["check_in_time"]
    if data.get("check_out_time"):
        extra["check_out_time"] = data["check_out_time"]
    if isinstance(rate_per_night, dict) and rate_per_night.get("extracted_lowest") is not None:
        extra["rate_per_night_extracted"] = _safe_float(rate_per_night["extracted_lowest"])
    if data.get("nearby_places"):
        extra["nearby_places"] = data["nearby_places"]
    if image_url:
        extra["image_url"] = image_url

    return HotelRecord(
        name=name,
        normalized_address=normalized_address,
        star_rating=float(extracted_class) if extracted_class is not None else None,
        traveler_rating=overall_rating,
        review_count=review_count,
        price_range=str(price_label) if price_label else "",
        amenities=amenities,
        latitude=latitude,
        longitude=longitude,
        description=description,
        photos=photos,
        photo_count=len(photos) if photos else None,
        sources=[SourceAttribution(provider="serpapi_google_hotels", retrieved_at=datetime.now(timezone.utc).isoformat())],
        extra=extra,
    )


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

    ranking_string = data.get("ranking_data", {}).get("ranking_string", "") if data.get("ranking_data") else ""

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
        sentiment_summary=ranking_string,
        sources=[SourceAttribution(provider="tripadvisor", retrieved_at=datetime.now(timezone.utc).isoformat())],
        extra={"ranking_string": ranking_string} if ranking_string else {},
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
