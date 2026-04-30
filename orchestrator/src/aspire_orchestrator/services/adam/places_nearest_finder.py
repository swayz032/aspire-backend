"""Nearest Home Depot finder — Google Places v1 + Geocoding by USER ADDRESS.

This is the Round-4-A primary path for trades workers. The user is at a job
site (NOT their office); Ava asks for the address they're at, and we resolve
the closest Home Depot via Google's geocoder + searchNearby v1.

Pipeline:
    user_address  ->  Geocoding API  ->  (lat, lng)
                  ->  Places searchNearby v1 (rankPreference=DISTANCE)
                  ->  filter for "Home Depot" branded results
                  ->  closest match -> NearestStore dataclass

Risk tier: GREEN (read-only). Receipts are emitted by the calling playbook,
not here — this module is a pure helper, consistent with hd_store_resolver.

Failure mode: returns None on ANY error (network, timeout, geocode miss,
no HD found within 50km). Logs a warning, never raises. The trades playbook
falls back to the existing Wave A.5 city -> zip path when None comes back.

Cost: 1 Geocoding ($0.005) + 1 Places Nearby ($0.025) per call. Combined
with the downstream SerpApi Home Depot search ($0.01), each user query
costs ~$0.04 — flagged in receipt.extra.cost_estimate_usd by the caller.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import asdict, dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Google endpoints
_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
_PLACES_PHOTO_MEDIA_URL = "https://places.googleapis.com/v1/{name}/media"

# Field mask for Places searchNearby v1 — keep tight so we only pay for
# fields we actually consume (Google bills per-field-mask category).
_PLACES_FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedAddress,"
    "places.shortFormattedAddress,"
    "places.location,"
    "places.photos"
)

# Timeouts — voice path budget is 5s end-to-end, so this whole helper has
# ~3s. Split it: 1.2s geocode + 1.5s places + 0.3s slack.
_GEOCODE_TIMEOUT = 1.2
_PLACES_TIMEOUT = 1.5

# Search radius — 50km is the design limit. Beyond that, "nearest HD" is
# not actually nearby for a trades worker on a job site.
_SEARCH_RADIUS_METERS = 50_000.0
_MAX_RESULT_COUNT = 10

# Photo size — store_summary card hero is 600x400.
_PHOTO_MAX_HEIGHT_PX = 400
_PHOTO_MAX_WIDTH_PX = 600

# US ZIP regex anchored at end of formattedAddress. Handles both "ZIP" and
# "ZIP-4" suffixes, optional country tail.
_POSTAL_RE = re.compile(r"(\d{5})(?:-\d{4})?\s*,?\s*USA?\s*$")

_HOME_DEPOT_NAME_PATTERN = re.compile(r"home\s*depot", re.IGNORECASE)


@dataclass
class NearestStore:
    """Resolved Home Depot near a user-provided address.

    All distances are miles (the trades-worker UI shows miles, not km).
    `place_id` is Google's place_id, NOT a Home Depot store_id — the
    trades playbook uses postal_code to route the SerpApi search.
    """

    place_id: str
    name: str
    address: str
    postal_code: str
    lat: float
    lng: float
    distance_miles: float
    photo_url: str
    user_lat: float
    user_lng: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def find_nearest_home_depot_by_address(
    user_address: str,
    *,
    timeout: float = 3.0,
) -> NearestStore | None:
    """Resolve closest Home Depot to `user_address` via Google Places v1.

    Returns None on:
      - empty / whitespace input
      - no API key configured
      - Geocoding miss (ZERO_RESULTS, REQUEST_DENIED, etc.)
      - searchNearby returns no Home Depot branded result within 50km
      - any HTTP error, timeout, or parse failure

    Never raises — caller (trades playbook) falls back to Wave A.5 when
    None is returned. The whole pipeline is wrapped in a single
    `asyncio.wait_for(..., timeout=timeout)` so total wall-clock is bounded.
    """
    addr = (user_address or "").strip()
    if not addr:
        return None

    try:
        return await asyncio.wait_for(
            _resolve_nearest(addr),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "find_nearest_home_depot_by_address timed out at %.1fs for %s",
            timeout, addr[:60],
        )
        return None
    except Exception as exc:  # noqa: BLE001 — logged then swallowed by design
        logger.warning(
            "find_nearest_home_depot_by_address failed for %s: %s",
            addr[:60], exc,
        )
        return None


async def _resolve_nearest(addr: str) -> NearestStore | None:
    """Inner pipeline. May raise — outer wrapper logs and returns None."""
    from aspire_orchestrator.config.settings import settings

    api_key = (getattr(settings, "google_maps_api_key", "") or "").strip()
    if not api_key:
        logger.debug("Google Places nearest finder disabled — no API key")
        return None

    # --- Step 1: Geocode user address -------------------------------------
    user_lat, user_lng = await _geocode(addr, api_key=api_key)
    if user_lat is None or user_lng is None:
        return None

    # --- Step 2: Places searchNearby v1 -----------------------------------
    candidates = await _search_nearby(
        lat=user_lat, lng=user_lng, api_key=api_key,
    )
    if not candidates:
        return None

    # --- Step 3: filter to Home Depot brand -------------------------------
    hd_candidates = [
        p for p in candidates
        if _HOME_DEPOT_NAME_PATTERN.search(
            (p.get("displayName") or {}).get("text", "")
        )
    ]
    if not hd_candidates:
        logger.info(
            "No Home Depot branded result within %.0fkm of %s",
            _SEARCH_RADIUS_METERS / 1000.0, addr[:60],
        )
        return None

    # rankPreference=DISTANCE means index 0 IS the closest (per Google docs),
    # but we still compute haversine because we need distance_miles for UI.
    closest = hd_candidates[0]

    location = closest.get("location") or {}
    place_lat = location.get("latitude")
    place_lng = location.get("longitude")
    if place_lat is None or place_lng is None:
        logger.warning("Closest HD result has no coordinates: %s", closest.get("id"))
        return None

    distance_miles = _haversine_miles(
        user_lat, user_lng, float(place_lat), float(place_lng),
    )

    formatted_address = closest.get("formattedAddress", "") or ""
    short_address = closest.get("shortFormattedAddress", "") or ""
    postal_code = _extract_postal_code(formatted_address) or _extract_postal_code(short_address)

    name = ((closest.get("displayName") or {}).get("text", "") or "").strip() or "Home Depot"

    photo_url = ""
    photos = closest.get("photos") or []
    if photos:
        first_photo_name = str(photos[0].get("name", "")).strip()
        if first_photo_name:
            photo_url = (
                f"{_PLACES_PHOTO_MEDIA_URL.format(name=first_photo_name)}"
                f"?maxHeightPx={_PHOTO_MAX_HEIGHT_PX}"
                f"&maxWidthPx={_PHOTO_MAX_WIDTH_PX}"
                f"&key={api_key}"
            )

    return NearestStore(
        place_id=str(closest.get("id", "") or ""),
        name=name,
        address=formatted_address,
        postal_code=postal_code or "",
        lat=float(place_lat),
        lng=float(place_lng),
        distance_miles=distance_miles,
        photo_url=photo_url,
        user_lat=float(user_lat),
        user_lng=float(user_lng),
    )


async def _geocode(addr: str, *, api_key: str) -> tuple[float | None, float | None]:
    """Google Geocoding API → (lat, lng) or (None, None)."""
    params = {"address": addr, "key": api_key}
    try:
        async with httpx.AsyncClient(timeout=_GEOCODE_TIMEOUT) as client:
            resp = await client.get(_GEOCODE_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Geocoding failed for %s: %s", addr[:60], exc)
        return None, None

    status = payload.get("status", "")
    if status != "OK":
        logger.info("Geocoding status=%s for %s", status, addr[:60])
        return None, None

    results = payload.get("results") or []
    if not results:
        return None, None

    location = (results[0].get("geometry") or {}).get("location") or {}
    lat = location.get("lat")
    lng = location.get("lng")
    if lat is None or lng is None:
        return None, None
    return float(lat), float(lng)


async def _search_nearby(
    *, lat: float, lng: float, api_key: str,
) -> list[dict[str, Any]]:
    """Places searchNearby v1 (POST). Returns places list or empty list."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _PLACES_FIELD_MASK,
    }
    body: dict[str, Any] = {
        "includedTypes": ["home_improvement_store"],
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": _SEARCH_RADIUS_METERS,
            },
        },
        "rankPreference": "DISTANCE",
        "maxResultCount": _MAX_RESULT_COUNT,
    }
    try:
        async with httpx.AsyncClient(timeout=_PLACES_TIMEOUT) as client:
            resp = await client.post(_PLACES_NEARBY_URL, json=body, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Places searchNearby failed: %s", exc)
        return []

    return list(payload.get("places") or [])


def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in miles. Earth radius = 3958.8 mi."""
    radius_miles = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2 * radius_miles * math.asin(math.sqrt(a))


def _extract_postal_code(formatted_address: str) -> str:
    """Pull the 5-digit ZIP from the tail of a Google formattedAddress.

    Examples:
      "1100 Capital Cir NE, Tallahassee, FL 32308, USA" -> "32308"
      "1100 Capital Cir NE, Tallahassee, FL 32308-1234, USA" -> "32308"
      "..., FL 32308" -> "32308"  (no country tail)
    """
    if not formatted_address:
        return ""
    match = _POSTAL_RE.search(formatted_address)
    if match:
        return match.group(1)
    # Final fallback — any 5-digit run anywhere in string.
    loose = re.search(r"\b(\d{5})\b", formatted_address)
    return loose.group(1) if loose else ""
