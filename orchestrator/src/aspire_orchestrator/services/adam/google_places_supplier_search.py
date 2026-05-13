"""Google Places v1 Supplier Search — PRIMARY engine for Materials Supplier mode.

Replaces Yelp SerpApi as the primary source for local specialty supplier
discovery. Google Places searchText returns address, formattedPhoneNumber,
websiteUri, regularOpeningHours, photos, rating, and userRatingCount in a
single call — eliminating the Yelp detail-page round-trip pattern.

Law compliance:
  Law #1  — No autonomous decisions. Returns results or failure; orchestrator
             decides fallback to Yelp.
  Law #3  — Fail closed: no API key → return empty list (caller falls back).
             Never guesses; never silently downgrades.
  Law #7  — This module is a pure helper (a "hand"). Receipt is emitted by
             the calling route (materials.py), not here, consistent with the
             places_nearest_finder pattern.
  Law #9  — PII: user-supplied addresses are redacted before logging via
             _redact_address (imported from places_nearest_finder). Phone
             numbers are public retail data (same carve-out as serpapi_yelp).
             They appear in result payloads but never in receipts.

Photo proxy:
  The photo resource name returned by Places is proxied through the
  orchestrator's /v1/places/photo endpoint (THREAT-004). The API key is
  never embedded in client-visible URLs.

Cost note:
  Places searchText (New): $0.017 per request billed under Places Text Search
  SKU. Well within the $200/mo Google Maps free credit. No per-field metering
  for the field mask categories used here.

Failure modes (documented for orchestrator):
  PLACES_NO_API_KEY     — ASPIRE_GOOGLE_MAPS_API_KEY not configured.
                          Returns empty list. Caller falls back to Yelp.
  PLACES_TIMEOUT        — Network/API did not respond in timeout window.
                          Returns empty list. Caller falls back to Yelp.
  PLACES_QUOTA_EXCEEDED — HTTP 429 or RESOURCE_EXHAUSTED error body.
                          Returns empty list. Caller falls back to Yelp.
  PLACES_DISABLED       — HTTP 403 / PERMISSION_DENIED from GCP console.
                          Returns empty list. Caller falls back to Yelp.
  PLACES_EMPTY_RESULTS  — Valid response but zero places returned.
                          Returns empty list. Caller falls back to Yelp.
  PLACES_SERVER_ERROR   — HTTP 5xx from Google.
                          Returns empty list. Caller falls back to Yelp.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Any
from urllib.parse import quote

import httpx

from aspire_orchestrator.services.adam.places_nearest_finder import (
    _PLACES_PHOTO_PROXY_PATH,
    _POSTAL_RE,
    _redact_address,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PLACES_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"

# Field mask — tight to avoid billing for unused field categories.
# Google bills Places (New) by field category (Basic, Advanced, Preferred).
# We request: Basic (id, displayName, types, location, formattedAddress,
# shortFormattedAddress) + Preferred (rating, userRatingCount, photos,
# regularOpeningHours, currentOpeningHours) + Contact (nationalPhoneNumber,
# internationalPhoneNumber, websiteUri).
_SUPPLIER_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.shortFormattedAddress",
    "places.location",
    "places.types",
    "places.rating",
    "places.userRatingCount",
    "places.currentOpeningHours",
    "places.regularOpeningHours",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.photos",
])

# Photo dimensions — supplier card hero (matches Yelp thumbnail render size).
_PHOTO_MAX_HEIGHT_PX = 400
_PHOTO_MAX_WIDTH_PX = 600

# State abbreviation extractor — captures "FL" from "..., FL 32303, USA"
_STATE_RE = re.compile(r",\s*([A-Z]{2})\s+\d{5}")

# City extractor — captures "Tallahassee" from "123 Main St, Tallahassee, FL 32303, USA"
# Matches second-to-last comma-delimited token before state+ZIP
_CITY_RE = re.compile(r",\s*([^,]+?)\s*,\s*[A-Z]{2}\s+\d{5}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def search_suppliers_via_places(
    query: str,
    *,
    location: str,
    max_results: int = 10,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Search for specialty material suppliers using Google Places v1 searchText.

    Args:
        query:       What to search for (e.g. "concrete supplier", "lumber yard").
        location:    Full project address — Places handles geocoding inline.
                     May be a city+state, ZIP, or full street address.
        max_results: Maximum supplier records to return (1–20; capped at 20 by API).
        timeout:     Hard wall-clock timeout in seconds (default 5.0).

    Returns:
        List of normalized supplier dicts matching BackendSupplier shape.
        Returns empty list on ANY error — never raises to caller.
        Returns empty list when no API key is configured (caller falls back).

    Result shape per item:
        {
          "id":             str   — Google place_id
          "name":           str   — Business display name
          "address":        str   — Full formatted address
          "city":           str   — Parsed city
          "state":          str   — 2-letter state abbreviation
          "zip":            str   — 5-digit ZIP
          "phone":          str   — National phone number (public retail data)
          "website":        str   — websiteUri from Places
          "thumbnail":      str   — /v1/places/photo proxy URL (never raw Google URL)
          "rating":         float | None
          "review_count":   int
          "distance_miles": None  — not computed here; caller enriches if needed
          "hours_open_now": bool | None
          "categories":     list[str]  — Place type labels
        }
    """
    query_s = (query or "").strip()
    location_s = (location or "").strip()
    if not query_s:
        logger.warning("search_suppliers_via_places: empty query — returning []")
        return []

    try:
        return await asyncio.wait_for(
            _execute_supplier_search(
                query=query_s,
                location=location_s,
                max_results=max(1, min(max_results, 20)),
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "search_suppliers_via_places timed out at %.1fs for query=%r location=%s",
            timeout,
            query_s[:60],
            _redact_address(location_s),
        )
        return []
    except Exception as exc:  # noqa: BLE001 — logged then swallowed by design
        logger.warning(
            "search_suppliers_via_places failed for query=%r location=%s: %s",
            query_s[:60],
            _redact_address(location_s),
            exc,
        )
        return []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _execute_supplier_search(
    query: str,
    location: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Inner implementation — may raise; outer wrapper logs and returns []."""
    from aspire_orchestrator.config.settings import settings

    api_key = (getattr(settings, "google_maps_api_key", "") or "").strip()
    if not api_key:
        logger.debug("search_suppliers_via_places: no API key configured — returning []")
        return []

    # Build textQuery: embed location in query so Places geocodes inline.
    # When location is empty, Places falls back to IP-based geolocation.
    if location:
        text_query = f"{query} suppliers near {location}"
    else:
        text_query = f"{query} suppliers"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _SUPPLIER_FIELD_MASK,
    }
    body: dict[str, Any] = {
        "textQuery": text_query,
        "includedType": "store",
        "rankPreference": "RELEVANCE",
        "maxResultCount": max_results,
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(_PLACES_TEXT_URL, json=body, headers=headers)
    except (httpx.ConnectError, httpx.NetworkError) as exc:
        logger.warning("search_suppliers_via_places network error: %s", exc)
        return []

    # Map error status codes before raise_for_status so we can log meaningfully.
    if resp.status_code == 429:
        logger.warning(
            "search_suppliers_via_places QUOTA_EXCEEDED (HTTP 429) for query=%r",
            query[:60],
        )
        return []
    if resp.status_code == 403:
        logger.warning(
            "search_suppliers_via_places PLACES_DISABLED (HTTP 403) — check GCP console for query=%r",
            query[:60],
        )
        return []
    if resp.status_code >= 500:
        logger.warning(
            "search_suppliers_via_places SERVER_ERROR (HTTP %d) for query=%r",
            resp.status_code,
            query[:60],
        )
        return []

    try:
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPStatusError, ValueError) as exc:
        logger.warning("search_suppliers_via_places parse error: %s", exc)
        return []

    # Check for API-level error body (GCP returns errors as 200 with error key)
    if "error" in payload:
        err = payload["error"]
        status_val = err.get("status", "")
        message = err.get("message", str(err))
        if "RESOURCE_EXHAUSTED" in status_val or "QUOTA" in message.upper():
            logger.warning(
                "search_suppliers_via_places QUOTA_EXCEEDED in body: %s", message[:200]
            )
        elif "PERMISSION_DENIED" in status_val:
            logger.warning(
                "search_suppliers_via_places PLACES_DISABLED in body: %s", message[:200]
            )
        else:
            logger.warning(
                "search_suppliers_via_places API error: %s", message[:200]
            )
        return []

    places = list(payload.get("places") or [])
    if not places:
        logger.info(
            "search_suppliers_via_places EMPTY_RESULTS for query=%r location=%s",
            query[:60],
            _redact_address(location),
        )
        return []

    suppliers: list[dict[str, Any]] = []
    for place in places:
        normalized = _normalize_place(place)
        if normalized:
            suppliers.append(normalized)

    logger.info(
        "search_suppliers_via_places returned %d suppliers for query=%r",
        len(suppliers),
        query[:60],
    )
    return suppliers


def _normalize_place(place: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a single Google Places result to the BackendSupplier shape.

    Returns None only if the place has no usable name (should never happen in
    practice — Places always includes displayName for store results).
    """
    # Identity
    place_id = str(place.get("id") or "").strip()
    display_name_obj = place.get("displayName") or {}
    name = str(display_name_obj.get("text") or "").strip()
    if not name:
        return None  # Unusable record — skip silently

    # Address
    formatted_address = str(place.get("formattedAddress") or "").strip()

    city = _parse_city(formatted_address)
    state = _parse_state(formatted_address)
    zip_code = _parse_zip(formatted_address)

    # Contact — public retail data (same carve-out as serpapi_yelp per Law #9 note)
    phone = str(place.get("nationalPhoneNumber") or "").strip()
    if not phone:
        # Fall back to international format (e.g., "+1 850-555-0100")
        phone = str(place.get("internationalPhoneNumber") or "").strip()
    website = str(place.get("websiteUri") or "").strip()

    # Photo — proxy URL pattern from places_nearest_finder (THREAT-004)
    thumbnail = ""
    photos = place.get("photos") or []
    if photos and isinstance(photos[0], dict):
        first_photo_name = str(photos[0].get("name") or "").strip()
        if first_photo_name:
            thumbnail = (
                f"{_PLACES_PHOTO_PROXY_PATH}"
                f"?ref={quote(first_photo_name, safe='')}"
                f"&maxHeightPx={_PHOTO_MAX_HEIGHT_PX}"
                f"&maxWidthPx={_PHOTO_MAX_WIDTH_PX}"
            )

    # Rating
    raw_rating = place.get("rating")
    rating: float | None = None
    if isinstance(raw_rating, (int, float)):
        rating = float(raw_rating)

    # Review count
    raw_count = place.get("userRatingCount")
    review_count: int = 0
    if isinstance(raw_count, int):
        review_count = raw_count
    elif isinstance(raw_count, float):
        review_count = int(raw_count)

    # Hours open now — prefer currentOpeningHours (live status) over regular
    hours_open_now: bool | None = None
    current_hours = place.get("currentOpeningHours") or {}
    if isinstance(current_hours, dict) and "openNow" in current_hours:
        val = current_hours["openNow"]
        if isinstance(val, bool):
            hours_open_now = val
    if hours_open_now is None:
        regular_hours = place.get("regularOpeningHours") or {}
        if isinstance(regular_hours, dict) and "openNow" in regular_hours:
            val = regular_hours["openNow"]
            if isinstance(val, bool):
                hours_open_now = val

    # Categories — Places returns a flat list of type strings like
    # ["hardware_store", "home_goods_store", "store", "point_of_interest"]
    raw_types = place.get("types") or []
    categories: list[str] = []
    if isinstance(raw_types, list):
        for t in raw_types:
            if isinstance(t, str) and t not in ("point_of_interest", "establishment"):
                # Convert snake_case → Title Case for display consistency with Yelp
                label = t.replace("_", " ").title()
                categories.append(label)

    return {
        "id": place_id,
        "name": name,
        "address": formatted_address,
        "city": city,
        "state": state,
        "zip": zip_code,
        "phone": phone,
        "website": website,
        "thumbnail": thumbnail,
        "rating": rating,
        "review_count": review_count,
        # distance_miles: not computable here — caller enriches if they have
        # user lat/lng. We emit None rather than 0.0 (F-MED-7 precedent).
        "distance_miles": None,
        "hours_open_now": hours_open_now,
        "categories": categories,
    }


# ---------------------------------------------------------------------------
# Address parsing helpers (private)
# ---------------------------------------------------------------------------


def _parse_zip(formatted_address: str) -> str:
    """Extract 5-digit ZIP from Google formattedAddress.

    Reuses the same _POSTAL_RE regex used by places_nearest_finder for
    consistency.  Falls back to any 5-digit cluster anywhere in the string.
    """
    if not formatted_address:
        return ""
    match = _POSTAL_RE.search(formatted_address)
    if match:
        return match.group(1)
    loose = re.search(r"\b(\d{5})\b", formatted_address)
    return loose.group(1) if loose else ""


def _parse_state(formatted_address: str) -> str:
    """Extract 2-letter US state abbreviation from formattedAddress."""
    if not formatted_address:
        return ""
    match = _STATE_RE.search(formatted_address)
    return match.group(1) if match else ""


def _parse_city(formatted_address: str) -> str:
    """Extract city name from formattedAddress.

    Handles forms like:
      "123 Main St, Tallahassee, FL 32303, USA"  → "Tallahassee"
      "123 Main St, Atlanta, GA 30309"            → "Atlanta"
    """
    if not formatted_address:
        return ""
    match = _CITY_RE.search(formatted_address)
    return match.group(1).strip() if match else ""
