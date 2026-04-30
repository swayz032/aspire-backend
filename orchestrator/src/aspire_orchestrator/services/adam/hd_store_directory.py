"""Static Home Depot store directory — O(1) lookup by store_id.

Source: SerpApi canonical static JSON (https://serpapi.com/home-depot-stores-us.json),
normalized into `data/home_depot_stores_us.json` with derived `name`, `city`, `state`.

This is the PRIMARY path for store identity in trades.py — when SerpApi search returns
products, we read `pickup.store_id` from the first product and look up name + address
here. Phone + website become optional Google Places enrichment (see hd_store_resolver).

Refresh cadence: monthly via `.github/workflows/refresh-hd-store-directory.yml` (Task #23).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).resolve().parent / "data" / "home_depot_stores_us.json"

_INDEX: dict[str, dict[str, Any]] | None = None
_BY_CITY: dict[tuple[str, str], list[dict[str, Any]]] = {}
_LOAD_LOCK = Lock()


def _load() -> dict[str, dict[str, Any]]:
    """Load the directory once (process-wide) into a store_id -> record dict.

    Also builds a (city.lower(), state.upper()) -> [records] secondary index
    so city→zip and city→[stores] lookups are O(1).
    """
    global _INDEX, _BY_CITY
    if _INDEX is not None:
        return _INDEX
    with _LOAD_LOCK:
        if _INDEX is not None:
            return _INDEX
        try:
            raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.error("HD store directory unavailable: %s", exc)
            _INDEX = {}
            _BY_CITY = {}
            return _INDEX

        idx: dict[str, dict[str, Any]] = {}
        by_city: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in raw:
            store_id = str(record.get("store_id", "")).strip()
            if not store_id:
                continue
            normalized = {
                "store_id": store_id,
                "name": record.get("name", "") or "",
                "address": record.get("address", "") or "",
                "city": record.get("city", "") or "",
                "state": record.get("state", "") or "",
                "postal_code": record.get("postal_code", "") or "",
                # Coordinates for haversine — best-effort; not all rows have them.
                "lat": record.get("lat") or record.get("latitude"),
                "lng": record.get("lng") or record.get("longitude"),
            }
            idx[store_id] = normalized

            city_key = (normalized["city"].lower().strip(), normalized["state"].upper().strip())
            if city_key[0]:
                by_city.setdefault(city_key, []).append(normalized)

        _INDEX = idx
        _BY_CITY = by_city
        logger.info(
            "Loaded %d Home Depot stores from static directory (cities indexed: %d)",
            len(idx), len(by_city),
        )
        return _INDEX


def lookup_store_by_id(store_id: str | int | None) -> dict[str, Any] | None:
    """Return the store record for a given Home Depot store_id, or None if unknown.

    Returns a dict with keys: store_id, name, address, city, state, postal_code.
    All values are strings (possibly empty).
    """
    if store_id is None:
        return None
    sid = str(store_id).strip()
    if not sid:
        return None
    record = _load().get(sid)
    if record is None:
        return None
    return dict(record)


def lookup_zip_by_city(city: str, state: str | None = None) -> str | None:
    """Return the postal_code of the FIRST store matching (city, state).

    Case-insensitive on city, normalized to upper on state. Returns None when
    the city/state pair has no Home Depot store on record. State is required
    for disambiguation (Springfield, IL ≠ Springfield, MO).

    Returned postal codes are zero-padded to 5 digits (NE/CT/MA stores in the
    source JSON sometimes lose leading zeros — "4401" -> "04401").
    """
    if not city or not city.strip():
        return None
    _load()
    state_key = (state or "").upper().strip()
    matches = _BY_CITY.get((city.lower().strip(), state_key))
    if not matches:
        return None
    postal = str(matches[0].get("postal_code", "")).strip()
    if not postal:
        return None
    return postal.zfill(5) if len(postal) < 5 else postal


def find_stores_in_city(city: str, state: str | None = None) -> list[dict[str, Any]]:
    """Return ALL store records for a (city, state). Empty list when none found."""
    if not city or not city.strip():
        return []
    _load()
    state_key = (state or "").upper().strip()
    matches = _BY_CITY.get((city.lower().strip(), state_key))
    if not matches:
        return []
    return [dict(record) for record in matches]


def find_nearest_store(
    lat: float,
    lng: float,
    city: str | None = None,
    state: str | None = None,
    max_km: float = 50.0,
) -> dict[str, Any] | None:
    """Return the nearest store within max_km of (lat, lng) via haversine.

    If city/state are provided, the search is restricted to stores in that city.
    Stores without coordinates in the directory are skipped. Returns None when
    no store with coordinates is within max_km (or none in the city pool).
    """
    import math

    _load()
    if city:
        candidates = find_stores_in_city(city, state)
    else:
        candidates = list((_INDEX or {}).values())
    if not candidates:
        return None

    def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        radius_km = 6371.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lng2 - lng1)
        a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
        return 2 * radius_km * math.asin(math.sqrt(a))

    best: dict[str, Any] | None = None
    best_km: float = float("inf")
    for record in candidates:
        s_lat = record.get("lat")
        s_lng = record.get("lng")
        if s_lat is None or s_lng is None:
            continue
        try:
            distance_km = _haversine_km(float(lat), float(lng), float(s_lat), float(s_lng))
        except (TypeError, ValueError):
            continue
        if distance_km < best_km and distance_km <= max_km:
            best = dict(record)
            best["distance_km"] = distance_km
            best["distance_miles"] = distance_km * 0.621371
            best_km = distance_km
    return best


def directory_size() -> int:
    """Number of stores in the directory (used by health checks and tests)."""
    return len(_load())


def all_stores() -> list[dict[str, Any]]:
    """Return every store record (immutable copies) — used by approximate-zip fallback."""
    return [dict(record) for record in _load().values()]


def lookup_store_by_zip_code(zip_code: str) -> dict[str, Any] | None:
    """O(1)-ish ZIP -> first store record. Returns None when no exact match.

    Provides a single source of truth for ZIP lookups so callers don't maintain
    parallel indexes (F-MED-2). Builds a lazy ZIP -> record cache on first call.
    """
    if not zip_code:
        return None
    zc = str(zip_code).strip().zfill(5)
    if not zc:
        return None
    cache = _zip_index()
    record = cache.get(zc)
    return dict(record) if record is not None else None


_ZIP_INDEX: dict[str, dict[str, Any]] | None = None


def _zip_index() -> dict[str, dict[str, Any]]:
    """Build (or return) the ZIP -> record index, anchored to the canonical _load() dict."""
    global _ZIP_INDEX
    if _ZIP_INDEX is not None:
        return _ZIP_INDEX
    with _LOAD_LOCK:
        if _ZIP_INDEX is not None:
            return _ZIP_INDEX
        index: dict[str, dict[str, Any]] = {}
        for record in _load().values():
            zc = str(record.get("postal_code", "")).strip().zfill(5)
            if zc and zc not in index:
                index[zc] = record
        _ZIP_INDEX = index
        return _ZIP_INDEX
