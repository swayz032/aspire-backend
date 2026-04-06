"""Normalize provider responses to BusinessRecord.

Handles: Google Places, HERE, Foursquare, TomTom, OSM Overpass
Each provider returns different field names/structures — this normalizer
maps them all to a single canonical BusinessRecord.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import (
    BusinessRecord,
    SourceAttribution,
)


def normalize_phone(raw: str) -> str:
    """Normalize phone to consistent format: (XXX) XXX-XXXX or international."""
    if not raw:
        return ""
    digits = re.sub(r"[^\d+]", "", raw)
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits[0] == "1":
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return raw.strip()


def normalize_from_google_places(data: dict[str, Any]) -> BusinessRecord:
    """Normalize a Google Places result to BusinessRecord."""
    location = data.get("location", {}) or {}
    return BusinessRecord(
        name=data.get("displayName", {}).get("text", "") or data.get("name", ""),
        normalized_address=data.get("formattedAddress", "") or data.get("shortFormattedAddress", ""),
        phone=normalize_phone(data.get("nationalPhoneNumber", "") or data.get("internationalPhoneNumber", "")),
        website=data.get("websiteUri", ""),
        category=data.get("primaryTypeDisplayName", {}).get("text", "") or data.get("primaryType", ""),
        rating=data.get("rating"),
        review_count=data.get("userRatingCount"),
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
        sources=[SourceAttribution(provider="google_places", retrieved_at=datetime.now(timezone.utc).isoformat())],
    )


def normalize_from_here(data: dict[str, Any]) -> BusinessRecord:
    """Normalize a HERE search result to BusinessRecord."""
    address = data.get("address", {}) or {}
    position = data.get("position", {}) or {}
    contacts = data.get("contacts", [{}])
    phone_raw = ""
    website_raw = ""
    if contacts and isinstance(contacts, list):
        for c in contacts:
            for p in c.get("phone", []):
                if not phone_raw:
                    phone_raw = p.get("value", "")
            for w in c.get("www", []):
                if not website_raw:
                    website_raw = w.get("value", "")

    return BusinessRecord(
        name=data.get("title", ""),
        normalized_address=address.get("label", ""),
        phone=normalize_phone(phone_raw),
        website=website_raw,
        category=", ".join(c.get("name", "") for c in data.get("categories", [])[:3]),
        latitude=position.get("lat"),
        longitude=position.get("lng"),
        sources=[SourceAttribution(provider="here", retrieved_at=datetime.now(timezone.utc).isoformat())],
    )


def normalize_from_foursquare(data: dict[str, Any]) -> BusinessRecord:
    """Normalize a Foursquare place result to BusinessRecord."""
    location = data.get("location", {}) or {}
    geocodes = data.get("geocodes", {}).get("main", {}) or {}
    cats = data.get("categories", [])
    category = cats[0].get("name", "") if cats else ""

    return BusinessRecord(
        name=data.get("name", ""),
        normalized_address=location.get("formatted_address", "") or location.get("address", ""),
        phone=normalize_phone(data.get("tel", "")),
        website=data.get("website", ""),
        category=category,
        rating=data.get("rating"),
        latitude=geocodes.get("latitude"),
        longitude=geocodes.get("longitude"),
        distance_miles=round(data.get("distance", 0) / 1609.34, 1) if data.get("distance") else None,
        sources=[SourceAttribution(provider="foursquare", retrieved_at=datetime.now(timezone.utc).isoformat())],
    )


def normalize_from_tomtom(data: dict[str, Any]) -> BusinessRecord:
    """Normalize a TomTom search result to BusinessRecord."""
    address = data.get("address", {}) or {}
    position = data.get("position", {}) or {}

    return BusinessRecord(
        name=data.get("poi", {}).get("name", "") if data.get("poi") else "",
        normalized_address=address.get("freeformAddress", ""),
        phone=normalize_phone((data.get("poi", {}) or {}).get("phone", "")),
        website=(data.get("poi", {}) or {}).get("url", ""),
        category=", ".join((data.get("poi", {}) or {}).get("categories", [])[:3]),
        latitude=position.get("lat"),
        longitude=position.get("lon"),
        distance_miles=round(data.get("dist", 0) / 1609.34, 1) if data.get("dist") else None,
        sources=[SourceAttribution(provider="tomtom", retrieved_at=datetime.now(timezone.utc).isoformat())],
    )
