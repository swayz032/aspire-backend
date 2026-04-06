"""Home Depot store resolver — finds nearest store using Google Places + SerpApi store directory.

Strategy:
  1. Google Places text search: "Home Depot near {zip_code}" → real address
  2. Match Google result address against SerpApi store directory → store_id
  3. Fallback: ZIP-based match from directory if Google Places unavailable

Store data source: https://serpapi.com/home-depot-stores-us (1,776 stores)
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STORE_DATA: list[dict[str, Any]] | None = None
_ZIP_INDEX: dict[str, dict[str, Any]] | None = None


def _load_stores() -> None:
    global _STORE_DATA, _ZIP_INDEX
    if _STORE_DATA is not None:
        return

    store_file = Path(__file__).resolve().parent.parent.parent / "config" / "hd_stores_us.json"
    try:
        with open(store_file) as f:
            _STORE_DATA = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("HD store data not found: %s", exc)
        _STORE_DATA = []

    _ZIP_INDEX = {}
    for store in _STORE_DATA:
        zc = str(store.get("postal_code", "")).strip().zfill(5)
        store["_zip"] = zc
        # Normalize address for matching
        store["_addr_norm"] = re.sub(r'[^a-z0-9 ]', '', store.get("address", "").lower())
        _ZIP_INDEX[zc] = store


def _match_store_by_address(address: str) -> dict[str, Any] | None:
    """Match a Google Places address to SerpApi store directory."""
    _load_stores()
    if not _STORE_DATA:
        return None

    addr_norm = re.sub(r'[^a-z0-9 ]', '', address.lower())

    # Try to extract ZIP from the Google address
    zip_match = re.search(r'\b(\d{5})\b', address)
    if zip_match and zip_match.group(1) in (_ZIP_INDEX or {}):
        return _ZIP_INDEX[zip_match.group(1)]

    # Try to match street number + street name
    street_match = re.search(r'(\d+)\s+(\w+)', addr_norm)
    if street_match:
        num = street_match.group(1)
        street = street_match.group(2)
        for store in _STORE_DATA:
            if num in store["_addr_norm"] and street in store["_addr_norm"]:
                return store

    return None


async def resolve_store_async(
    zip_code: str,
    correlation_id: str = "",
    suite_id: str = "",
    office_id: str = "",
) -> dict[str, Any] | None:
    """Find nearest Home Depot store using Google Places API.

    Returns dict with store_id, address, postal_code, plus Google Places data
    (formatted_address, name, rating, opening_hours).
    """
    _load_stores()

    # Exact ZIP match — skip Google Places
    zc = str(zip_code).strip().zfill(5)
    if zc in (_ZIP_INDEX or {}):
        store = _ZIP_INDEX[zc]
        return {
            "store_id": str(store.get("store_id", "")),
            "address": store.get("address", ""),
            "postal_code": store.get("postal_code", ""),
        }

    # Google Places: find nearest Home Depot
    try:
        from aspire_orchestrator.providers.google_places_client import execute_google_places_search
        result = await execute_google_places_search(
            payload={
                "query": f"Home Depot near {zip_code}",
                "type": "home_goods_store",
            },
            correlation_id=correlation_id or "hd_store_lookup",
            suite_id=suite_id or "adam",
            office_id=office_id or "adam",
        )

        if result.outcome.value == "success" and result.data:
            places = result.data.get("results", [])
            if places:
                place = places[0]  # Nearest result
                gp_address = place.get("formatted_address", "")
                gp_name = place.get("name", "")

                # Match to SerpApi store directory
                matched = _match_store_by_address(gp_address)

                store_info: dict[str, Any] = {
                    "store_id": str(matched["store_id"]) if matched else "",
                    "address": gp_address,
                    "postal_code": matched.get("postal_code", "") if matched else "",
                    "store_name": gp_name,
                    "rating": place.get("rating"),
                    "open_now": (place.get("opening_hours") or {}).get("open_now"),
                }
                return store_info
    except Exception as exc:
        logger.warning("Google Places store lookup failed: %s", exc)

    # Fallback: ZIP distance within same 3-digit prefix range
    return _fallback_zip_match(zc)


def resolve_store(zip_code: str, **_kwargs: Any) -> dict[str, Any] | None:
    """Synchronous fallback — uses ZIP directory match only (no Google Places).

    Use resolve_store_async when possible for accurate results.
    """
    _load_stores()
    zc = str(zip_code).strip().zfill(5)
    if zc in (_ZIP_INDEX or {}):
        return _ZIP_INDEX[zc]
    return _fallback_zip_match(zc)


def _fallback_zip_match(zc: str) -> dict[str, Any] | None:
    """Match by ZIP proximity within adjacent prefixes."""
    _load_stores()
    if not _STORE_DATA:
        return None

    base = int(zc[:3])
    candidates: list[dict[str, Any]] = []
    for store in _STORE_DATA:
        store_prefix = int(store["_zip"][:3])
        if abs(store_prefix - base) <= 3:
            candidates.append(store)

    if not candidates:
        return None

    target = int(zc)
    candidates.sort(key=lambda s: abs(int(s["_zip"]) - target))
    return candidates[0]
