"""Google Distance Matrix API client — drive-minutes resolver for materials route.

Used exclusively by the materials search route (Bug B/C fix) to enrich
closest_store_info with real drive_minutes from the user's project address.

Design:
  - Fail-soft (Law #3 variant): on any error, returns None so the caller
    falls back to null drive_minutes. Never raises to the caller.
  - 3-second hard timeout (provider timeout < 5s per Law #10).
  - Process-local TTL cache via drive_minutes_cache.py (1h, pod-local).
  - No PII in cache keys — ZIP + store_id only (Law #9).
  - Single external call per (origin, destination) pair within TTL window.

Auth: GOOGLE_MAPS_API_KEY env var. If absent → returns None immediately (fail-soft).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from aspire_orchestrator.services.adam.drive_minutes_cache import (
    get_drive_minutes,
    set_drive_minutes,
)

logger = logging.getLogger(__name__)

_API_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
_TIMEOUT_SECONDS = 3.0


def _extract_origin_zip(address: str) -> str:
    """Extract a 5-digit US ZIP from a full address string for cache keying."""
    import re
    m = re.search(r"\b(\d{5})\b", address)
    return m.group(1) if m else address[:20].strip()


async def resolve_drive_minutes(
    origin_address: str,
    destination_address: str,
    destination_store_id: str,
) -> tuple[int, bool] | None:
    """Return (drive_minutes, in_traffic) or None on any failure.

    Args:
        origin_address:       Full project address (e.g. "4863 Price St, Forest Park, GA 30297").
        destination_address:  Store street address from closest_store_info.
        destination_store_id: HD store_id used as cache key suffix (stable across calls).

    Returns:
        (drive_minutes: int, in_traffic: bool) on success, None on failure.

    Law compliance:
        Law #3 — fail-soft: any exception → None, never 502.
        Law #9 — cache key uses only ZIP + store_id, never full PII address.
    """
    if not origin_address or not destination_address:
        return None

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        logger.debug("resolve_drive_minutes: GOOGLE_MAPS_API_KEY not set — skipping")
        return None

    # Cache key: origin ZIP (not full address) + destination store_id (Law #9).
    origin_zip = _extract_origin_zip(origin_address)
    cache_key_dest = destination_store_id or destination_address[:20].strip()

    cached = get_drive_minutes(origin_zip, cache_key_dest)
    if cached is not None:
        logger.debug(
            "resolve_drive_minutes cache hit origin_zip=%s dest_id=%s → %s min",
            origin_zip, cache_key_dest, cached[0],
        )
        return cached

    params: dict[str, Any] = {
        "origins": origin_address,
        "destinations": destination_address,
        "mode": "driving",
        "departure_time": "now",
        "traffic_model": "best_guess",
        "key": api_key,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await asyncio.wait_for(
                client.get(_API_URL, params=params),
                timeout=_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        logger.warning(
            "resolve_drive_minutes HTTP error origin_zip=%s dest_id=%s: %s",
            origin_zip, cache_key_dest, exc,
        )
        return None

    try:
        rows = body.get("rows", [])
        if not rows:
            return None
        elements = rows[0].get("elements", [])
        if not elements:
            return None
        element = elements[0]
        if element.get("status") != "OK":
            logger.debug(
                "resolve_drive_minutes element status=%s origin_zip=%s",
                element.get("status"), origin_zip,
            )
            return None

        # Prefer duration_in_traffic when present (departure_time=now was sent).
        duration_block = element.get("duration_in_traffic") or element.get("duration")
        if not duration_block:
            return None

        seconds = duration_block.get("value", 0)
        in_traffic = "duration_in_traffic" in element
        drive_minutes = max(1, int(seconds // 60))

        set_drive_minutes(origin_zip, cache_key_dest, drive_minutes, in_traffic)
        logger.info(
            "resolve_drive_minutes resolved origin_zip=%s dest_id=%s → %d min (traffic=%s)",
            origin_zip, cache_key_dest, drive_minutes, in_traffic,
        )
        return (drive_minutes, in_traffic)

    except Exception as exc:
        logger.warning(
            "resolve_drive_minutes parse error origin_zip=%s dest_id=%s: %s",
            origin_zip, cache_key_dest, exc,
        )
        return None
