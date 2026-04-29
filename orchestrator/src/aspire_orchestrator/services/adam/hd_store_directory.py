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
_LOAD_LOCK = Lock()


def _load() -> dict[str, dict[str, Any]]:
    """Load the directory once (process-wide) into a store_id -> record dict."""
    global _INDEX
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
            return _INDEX

        idx: dict[str, dict[str, Any]] = {}
        for record in raw:
            store_id = str(record.get("store_id", "")).strip()
            if not store_id:
                continue
            idx[store_id] = {
                "store_id": store_id,
                "name": record.get("name", "") or "",
                "address": record.get("address", "") or "",
                "city": record.get("city", "") or "",
                "state": record.get("state", "") or "",
                "postal_code": record.get("postal_code", "") or "",
            }
        _INDEX = idx
        logger.info("Loaded %d Home Depot stores from static directory", len(idx))
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


def directory_size() -> int:
    """Number of stores in the directory (used by health checks and tests)."""
    return len(_load())
