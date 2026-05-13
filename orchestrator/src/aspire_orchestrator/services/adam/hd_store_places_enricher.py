"""HD Store Places Enricher — Google Places Details for closest_store phone + hours.

Called exclusively by routes/materials.py after closest_store_info is resolved.
Adds phone, hours_open_now, hours_today, and current_status to the store dict.

Design:
  - Fail-soft (Law #3 variant): any error → returns unchanged store dict.
  - 3-second hard timeout per Law #10 (tools < 5s).
  - 6-hour process-local TTL cache keyed on store_id (not address — Law #9).
  - Uses google_places /details/json endpoint with fields:
      formatted_phone_number, opening_hours, business_status
  - place_id looked up via Text Search: "Home Depot {store_address}".
  - place_id also cached per store_id (separate 24h TTL).
  - No PII in cache keys (store_id is a stable HD internal ID — Law #9).

Auth: GOOGLE_MAPS_API_KEY env var. Missing → returns store dict unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Process-local cache
# ---------------------------------------------------------------------------

# Structure: { store_id: (enriched_fields_dict, expires_at_monotonic) }
_ENRICHMENT_CACHE: dict[str, tuple[dict[str, Any], float]] = {}
_ENRICHMENT_TTL_S = 6 * 3600  # 6 hours

# Structure: { store_id: (place_id_str, expires_at_monotonic) }
_PLACE_ID_CACHE: dict[str, tuple[str, float]] = {}
_PLACE_ID_TTL_S = 24 * 3600  # 24 hours

_PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
_PLACES_TEXTSEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
_TIMEOUT_S = 3.0

# Maps Google business_status values to our canonical current_status
_STATUS_MAP: dict[str, str] = {
    "OPERATIONAL": "OPEN",
    "CLOSED_TEMPORARILY": "CLOSED",
    "CLOSED_PERMANENTLY": "CLOSED",
}


def _now() -> float:
    return time.monotonic()


def _get_cached_enrichment(store_id: str) -> dict[str, Any] | None:
    entry = _ENRICHMENT_CACHE.get(store_id)
    if entry and entry[1] > _now():
        return entry[0]
    if entry:
        del _ENRICHMENT_CACHE[store_id]
    return None


def _set_cached_enrichment(store_id: str, fields: dict[str, Any]) -> None:
    _ENRICHMENT_CACHE[store_id] = (fields, _now() + _ENRICHMENT_TTL_S)


def _get_cached_place_id(store_id: str) -> str | None:
    entry = _PLACE_ID_CACHE.get(store_id)
    if entry and entry[1] > _now():
        return entry[0]
    if entry:
        del _PLACE_ID_CACHE[store_id]
    return None


def _set_cached_place_id(store_id: str, place_id: str) -> None:
    _PLACE_ID_CACHE[store_id] = (place_id, _now() + _PLACE_ID_TTL_S)


def _parse_hours_today(opening_hours: dict[str, Any]) -> str | None:
    """Extract today's hours string from a Places opening_hours block.

    Returns a string like '6 AM - 10 PM' or None if not parseable.
    """
    weekday_text: list[str] = opening_hours.get("weekday_text") or []
    if not weekday_text:
        return None
    # weekday_text is Mon-Sun (0=Monday in Google's schema).
    # Python weekday(): Mon=0 ... Sun=6.
    today_idx = datetime.now(timezone.utc).weekday()  # 0=Mon
    if today_idx < len(weekday_text):
        raw = weekday_text[today_idx]
        # Format: "Monday: 6:00 AM - 10:00 PM" → "6:00 AM - 10:00 PM"
        if ":" in raw:
            return raw.split(":", 1)[1].strip()
    return None


def _infer_closing_soon(opening_hours: dict[str, Any]) -> bool:
    """Return True if the store is open but closes within 60 minutes.

    Uses the periods array if available. Fail-soft: returns False on any error.
    """
    try:
        periods = opening_hours.get("periods") or []
        if not periods:
            return False
        now_utc = datetime.now(timezone.utc)
        # Google periods use local time (no timezone). Use UTC as approximation.
        current_hhmm = int(now_utc.strftime("%H%M"))
        today_day = now_utc.weekday()  # 0=Mon; Google uses 0=Sun, so offset by 1
        google_day = (today_day + 1) % 7
        for period in periods:
            close_block = period.get("close", {})
            if close_block.get("day") == google_day:
                close_hhmm_str = str(close_block.get("time", "0000"))
                close_hhmm = int(close_hhmm_str) if close_hhmm_str.isdigit() else 0
                if 0 < close_hhmm - current_hhmm <= 100:  # within ~60 min
                    return True
    except Exception:
        pass
    return False


async def enrich_store_with_places(
    store: dict[str, Any],
) -> dict[str, Any]:
    """Return store dict with phone, hours_open_now, hours_today, current_status added.

    Law compliance:
      Law #3 — fail-soft: any error → return original store dict unchanged.
      Law #9 — cache key is store_id only (no address PII).
      Law #10 — 3s timeout on all HTTP calls.

    Args:
        store: Dict with at least store_id and address fields.

    Returns:
        Same dict (mutated in-place) with enrichment fields merged. If any
        error occurs the dict is returned unchanged with no enrichment fields.
    """
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        logger.debug("enrich_store_with_places: GOOGLE_MAPS_API_KEY not set — skipping")
        return store

    store_id = str(store.get("store_id") or store.get("id") or "")
    if not store_id:
        logger.debug("enrich_store_with_places: no store_id — skipping")
        return store

    # Check process-local enrichment cache first
    cached = _get_cached_enrichment(store_id)
    if cached is not None:
        store.update(cached)
        logger.debug("enrich_store_with_places cache hit store_id=%s", store_id)
        return store

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            # Step 1: resolve place_id (cached separately for 24h)
            place_id = _get_cached_place_id(store_id)
            if not place_id:
                store_address = store.get("address") or store.get("store_address") or ""
                store_name = store.get("store_name") or store.get("name") or "Home Depot"
                search_query = f"{store_name} {store_address}".strip()
                if not search_query or search_query == "Home Depot":
                    logger.debug(
                        "enrich_store_with_places: no address for store_id=%s — skipping",
                        store_id,
                    )
                    return store

                text_resp = await asyncio.wait_for(
                    client.get(
                        _PLACES_TEXTSEARCH_URL,
                        params={
                            "key": api_key,
                            "query": search_query,
                            "type": "hardware_store",
                        },
                    ),
                    timeout=_TIMEOUT_S,
                )
                text_body = text_resp.json()
                results = text_body.get("results") or []
                if not results:
                    logger.debug(
                        "enrich_store_with_places: Places text-search no results store_id=%s",
                        store_id,
                    )
                    return store
                place_id = results[0].get("place_id", "")
                if place_id:
                    _set_cached_place_id(store_id, place_id)

            if not place_id:
                return store

            # Step 2: fetch place details (phone, hours, business_status)
            detail_resp = await asyncio.wait_for(
                client.get(
                    _PLACES_DETAILS_URL,
                    params={
                        "key": api_key,
                        "place_id": place_id,
                        "fields": "formatted_phone_number,opening_hours,business_status",
                    },
                ),
                timeout=_TIMEOUT_S,
            )
            detail_body = detail_resp.json()
            result = detail_body.get("result") or {}

            phone = result.get("formatted_phone_number") or ""
            opening_hours: dict[str, Any] = result.get("opening_hours") or {}
            business_status: str = result.get("business_status") or "OPERATIONAL"

            hours_open_now: bool = bool(opening_hours.get("open_now", True))
            hours_today: str | None = _parse_hours_today(opening_hours)

            # Determine current_status
            if not hours_open_now:
                current_status = "CLOSED"
            elif _infer_closing_soon(opening_hours):
                current_status = "CLOSING_SOON"
            else:
                current_status = _STATUS_MAP.get(business_status, "OPEN")

            enrichment: dict[str, Any] = {
                "phone": phone,
                "hours_open_now": hours_open_now,
                "hours_today": hours_today,
                "current_status": current_status,
            }

            # Cache enrichment for 6h
            _set_cached_enrichment(store_id, enrichment)
            store.update(enrichment)
            logger.info(
                "enrich_store_with_places enriched store_id=%s phone=%s status=%s",
                store_id,
                # Redact digits for log safety (Law #9) — phone is public HD number but
                # we log only the presence flag
                bool(phone),
                current_status,
            )

    except Exception as exc:
        # Fail-soft: log at DEBUG, return store unchanged (Law #3)
        logger.debug(
            "enrich_store_with_places fail-soft store_id=%s: %s",
            store_id, exc,
        )

    return store
