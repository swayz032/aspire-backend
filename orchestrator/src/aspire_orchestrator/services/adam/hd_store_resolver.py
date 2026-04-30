"""Home Depot store resolver — OPTIONAL phone/website enrichment via Google Places v1.

Demoted role (post-Task #19): primary store identity (name, address, city, state) now
comes from the static directory in `hd_store_directory.py`, keyed by `pickup.store_id`
returned by SerpApi. This resolver only contributes phone + website + open_now via
Google Places v1 (`places.googleapis.com/v1/places:searchText`), which is best-effort.

If Google Places fails or is unconfigured, the store card still has name + address
from the static directory — no fallback chain required.

Risk tier: GREEN (read-only).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from aspire_orchestrator.services.adam import hd_store_directory

logger = logging.getLogger(__name__)

_PLACES_V1_URL = "https://places.googleapis.com/v1/places:searchText"
_PLACES_V1_FIELD_MASK = (
    "places.displayName,places.formattedAddress,"
    "places.nationalPhoneNumber,places.websiteUri,"
    "places.photos"
)
# F-MED-5: align with the outer voice-path resolver budget (1.5s). The previous
# 5.0s let a slow Google Places call eat the entire 5s voice budget.
_PLACES_V1_TIMEOUT_SECONDS = 1.5
# Server-side photo proxy path (THREAT-004 / receipt #26). We never embed the
# Google API key in client-visible URLs.
_PLACES_V1_PHOTO_PROXY_PATH = "/v1/places/photo"
_PHOTO_MAX_HEIGHT_PX = 400
_PHOTO_MAX_WIDTH_PX = 600


def _lookup_store_by_zip(zip_code: str) -> dict[str, Any] | None:
    """Lookup via the canonical hd_store_directory index (no duplicate state).

    F-MED-2: previously this module loaded `hd_stores_us.json` into its own
    parallel `_ZIP_INDEX` dict, which drifted from the canonical directory
    after the monthly refresh job (Task #23) started writing the new
    `home_depot_stores_us.json`. Routing all ZIP lookups through
    hd_store_directory keeps a single source of truth.
    """
    if not zip_code:
        return None
    zc = str(zip_code).strip().zfill(5)
    return hd_store_directory.lookup_store_by_zip_code(zc)


def _match_store_by_address(address: str) -> dict[str, Any] | None:
    if not address:
        return None
    zip_match = re.search(r"\b(\d{5})\b", address)
    if zip_match:
        return _lookup_store_by_zip(zip_match.group(1))
    return None


async def resolve_store_async(
    zip_code: str,
    location_hint: str = "",
    correlation_id: str = "",
    suite_id: str = "",
    office_id: str = "",
) -> dict[str, Any] | None:
    """Find nearest Home Depot store using Google Places API.

    Returns store fields needed by tool cards and store summary card.
    """
    zc = str(zip_code).strip().zfill(5) if zip_code else ""
    store = _lookup_store_by_zip(zc) if zc else None
    if store is not None:
        # Opportunistic Places lookup for image_url + phone + website. The card
        # still renders without these — they're enrichment, not core identity.
        place = await _places_v1_searchtext(
            f"Home Depot {store.get('address', '')} {store.get('city', '')} {store.get('state', '')}".strip(),
        )
        return {
            "store_id": str(store.get("store_id", "")),
            "store_name": store.get("name", "Home Depot"),
            "address": store.get("address", ""),
            "city": store.get("city", ""),
            "state": store.get("state", ""),
            "postal_code": store.get("postal_code", ""),
            "phone": (place or {}).get("phone", "") or store.get("phone", ""),
            "website": (place or {}).get("website", "") or store.get("website", ""),
            "image_url": (place or {}).get("image_url", ""),
        }

    place = await _places_v1_searchtext(
        f"Home Depot near {location_hint or zip_code}",
    )
    if place is not None:
        gp_name = place.get("name", "")
        gp_address = place.get("address", "")
        matched = _match_store_by_address(gp_address) if gp_address else None
        return {
            "store_id": str(matched.get("store_id", "")) if matched else "",
            "store_name": gp_name,
            "address": gp_address,
            "city": matched.get("city", "") if matched else "",
            "state": matched.get("state", "") if matched else "",
            "postal_code": matched.get("postal_code", "") if matched else "",
            "phone": place.get("phone", ""),
            "website": place.get("website", ""),
            "image_url": place.get("image_url", ""),
        }

    return _fallback_zip_match(zc)


async def _places_v1_searchtext(query: str) -> dict[str, Any] | None:
    """Call Google Places v1 searchText. Returns first place's normalized fields.

    Reads v1 field names: `displayName.text`, `formattedAddress`,
    `nationalPhoneNumber`, `websiteUri`. Sends `X-Goog-FieldMask` header.

    Returns None on any failure — caller continues without phone/website.
    """
    from aspire_orchestrator.config.settings import settings

    api_key = getattr(settings, "google_maps_api_key", "") or ""
    if not api_key:
        logger.debug("Google Places v1 disabled — no API key configured")
        return None

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _PLACES_V1_FIELD_MASK,
    }
    body = {"textQuery": query}

    # Tight per-call connection limits to keep voice-budget enrichment from
    # holding the global pool when Google is slow.
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
    try:
        async with httpx.AsyncClient(
            timeout=_PLACES_V1_TIMEOUT_SECONDS,
            limits=limits,
        ) as client:
            response = await client.post(
                _PLACES_V1_URL, json=body, headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Google Places v1 lookup failed: %s", exc)
        return None

    places = payload.get("places") or []
    if not places:
        return None

    place = places[0]
    photos = place.get("photos") or []
    image_url = ""
    if photos:
        # Photo "name" comes back as: places/{PLACE_ID}/photos/{PHOTO_REF}.
        # Emit the server-side proxy URL — the orchestrator signs the upstream
        # call with the API key so we never expose it to the Desktop client.
        first_photo_name = str(photos[0].get("name", "")).strip()
        if first_photo_name:
            from urllib.parse import quote

            image_url = (
                f"{_PLACES_V1_PHOTO_PROXY_PATH}"
                f"?ref={quote(first_photo_name, safe='')}"
                f"&maxHeightPx={_PHOTO_MAX_HEIGHT_PX}"
                f"&maxWidthPx={_PHOTO_MAX_WIDTH_PX}"
            )

    return {
        "name": (place.get("displayName") or {}).get("text", ""),
        "address": place.get("formattedAddress", ""),
        "phone": place.get("nationalPhoneNumber", ""),
        "website": place.get("websiteUri", ""),
        "image_url": image_url,
    }


def resolve_store(zip_code: str, **_kwargs: Any) -> dict[str, Any] | None:
    """Synchronous fallback - uses ZIP directory match only (no Google Places)."""
    zc = str(zip_code).strip().zfill(5) if zip_code else ""
    if not zc:
        return None
    direct = _lookup_store_by_zip(zc)
    if direct is not None:
        return direct
    return _fallback_zip_match(zc)


def _fallback_zip_match(zc: str) -> dict[str, Any] | None:
    """Approximate ZIP match by 3-digit prefix when exact ZIP not in directory.

    Reads through the canonical hd_store_directory rather than maintaining a
    separate file load (F-MED-2).
    """
    if not zc:
        return None

    try:
        base = int(zc[:3])
        target = int(zc)
    except ValueError:
        return None

    # Pull all stores once; the directory caches and returns dicts.
    all_stores = list(hd_store_directory.all_stores())
    if not all_stores:
        return None

    candidates: list[tuple[int, dict[str, Any]]] = []
    for store in all_stores:
        zip_str = str(store.get("postal_code", "")).strip().zfill(5)
        try:
            store_prefix = int(zip_str[:3])
            store_zip = int(zip_str)
        except ValueError:
            continue
        if abs(store_prefix - base) <= 3:
            candidates.append((abs(store_zip - target), store))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    store = candidates[0][1]
    return {
        "store_id": str(store.get("store_id", "")),
        "store_name": store.get("name", "Home Depot"),
        "address": store.get("address", ""),
        "city": store.get("city", ""),
        "state": store.get("state", ""),
        "postal_code": store.get("postal_code", ""),
        "phone": "",
        "website": "",
    }
