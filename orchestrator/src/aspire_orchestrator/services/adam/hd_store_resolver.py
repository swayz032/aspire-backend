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

import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_STORE_DATA: list[dict[str, Any]] | None = None
_ZIP_INDEX: dict[str, dict[str, Any]] | None = None

_PLACES_V1_URL = "https://places.googleapis.com/v1/places:searchText"
_PLACES_V1_FIELD_MASK = (
    "places.displayName,places.formattedAddress,"
    "places.nationalPhoneNumber,places.websiteUri"
)
_PLACES_V1_TIMEOUT_SECONDS = 5.0


def _load_stores() -> None:
    global _STORE_DATA, _ZIP_INDEX
    if _STORE_DATA is not None:
        return

    store_file = Path(__file__).resolve().parent.parent.parent / "config" / "hd_stores_us.json"
    try:
        with open(store_file, encoding="utf-8") as f:
            _STORE_DATA = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("HD store data not found: %s", exc)
        _STORE_DATA = []

    _ZIP_INDEX = {}
    for store in _STORE_DATA:
        zc = str(store.get("postal_code", "")).strip().zfill(5)
        store["_zip"] = zc
        store["_addr_norm"] = re.sub(r"[^a-z0-9 ]", "", str(store.get("address", "")).lower())
        _ZIP_INDEX[zc] = store


def _match_store_by_address(address: str) -> dict[str, Any] | None:
    _load_stores()
    if not _STORE_DATA:
        return None

    addr_norm = re.sub(r"[^a-z0-9 ]", "", address.lower())

    zip_match = re.search(r"\b(\d{5})\b", address)
    if zip_match and zip_match.group(1) in (_ZIP_INDEX or {}):
        return _ZIP_INDEX[zip_match.group(1)]

    street_match = re.search(r"(\d+)\s+(\w+)", addr_norm)
    if street_match:
        num = street_match.group(1)
        street = street_match.group(2)
        for store in _STORE_DATA:
            if num in store.get("_addr_norm", "") and street in store.get("_addr_norm", ""):
                return store

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
    _load_stores()

    zc = str(zip_code).strip().zfill(5)
    if zc and zc in (_ZIP_INDEX or {}):
        store = _ZIP_INDEX[zc]
        return {
            "store_id": str(store.get("store_id", "")),
            "store_name": store.get("name", "Home Depot"),
            "address": store.get("address", ""),
            "city": store.get("city", ""),
            "state": store.get("state", ""),
            "postal_code": store.get("postal_code", ""),
            "phone": store.get("phone", ""),
            "website": store.get("website", ""),
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

    try:
        async with httpx.AsyncClient(timeout=_PLACES_V1_TIMEOUT_SECONDS) as client:
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
    return {
        "name": (place.get("displayName") or {}).get("text", ""),
        "address": place.get("formattedAddress", ""),
        "phone": place.get("nationalPhoneNumber", ""),
        "website": place.get("websiteUri", ""),
    }


def resolve_store(zip_code: str, **_kwargs: Any) -> dict[str, Any] | None:
    """Synchronous fallback - uses ZIP directory match only (no Google Places)."""
    _load_stores()
    zc = str(zip_code).strip().zfill(5)
    if zc in (_ZIP_INDEX or {}):
        return _ZIP_INDEX[zc]
    return _fallback_zip_match(zc)


def _fallback_zip_match(zc: str) -> dict[str, Any] | None:
    _load_stores()
    if not _STORE_DATA or not zc:
        return None

    try:
        base = int(zc[:3])
        target = int(zc)
    except ValueError:
        return None

    candidates: list[dict[str, Any]] = []
    for store in _STORE_DATA:
        try:
            store_prefix = int(str(store.get("_zip", "00000"))[:3])
        except ValueError:
            continue
        if abs(store_prefix - base) <= 3:
            candidates.append(store)

    if not candidates:
        return None

    candidates.sort(key=lambda s: abs(int(str(s.get("_zip", "0"))) - target))
    store = candidates[0]
    return {
        "store_id": str(store.get("store_id", "")),
        "store_name": store.get("name", "Home Depot"),
        "address": store.get("address", ""),
        "city": store.get("city", ""),
        "state": store.get("state", ""),
        "postal_code": store.get("postal_code", ""),
        "phone": store.get("phone", ""),
        "website": store.get("website", ""),
    }
