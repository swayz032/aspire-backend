"""In-process TTL cache for Google Distance Matrix drive-minutes — Pass C.

Design:
  - Module-level dict keyed by (origin_zip, destination_place_id).
  - 1-hour TTL per entry.
  - Thread-safe via threading.Lock (same pattern as serpapi_budget.py).
  - Pod-local: if Railway scales to multiple instances this cache does not
    synchronise across pods. Acceptable for single-instance deploy. If Railway
    scales, this must move to Redis.

Law #9: No PII in cache keys — zip codes and place IDs only.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_TTL_SECONDS = 3600  # 1 hour

# Cache value: (drive_minutes: int, in_traffic: bool, cached_at: float)
_DriveEntry = tuple[int, bool, float]
_cache: dict[str, _DriveEntry] = {}
_lock = threading.Lock()


def _make_key(origin_zip: str, destination_id: str) -> str:
    return f"{origin_zip.strip()}:{destination_id.strip()}"


def get_drive_minutes(origin_zip: str, destination_id: str) -> tuple[int, bool] | None:
    """Return (drive_minutes, in_traffic) from cache, or None on miss/expiry."""
    key = _make_key(origin_zip, destination_id)
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        drive_minutes, in_traffic, cached_at = entry
        if time.monotonic() - cached_at > _TTL_SECONDS:
            del _cache[key]
            logger.debug("drive_minutes cache expired key=%s", key)
            return None
        return (drive_minutes, in_traffic)


def set_drive_minutes(
    origin_zip: str, destination_id: str, drive_minutes: int, in_traffic: bool
) -> None:
    """Store (drive_minutes, in_traffic) in the cache with a 1h TTL."""
    key = _make_key(origin_zip, destination_id)
    with _lock:
        _cache[key] = (drive_minutes, in_traffic, time.monotonic())
    logger.debug("drive_minutes cached key=%s minutes=%d", key, drive_minutes)


def cache_size() -> int:
    """Return the number of entries currently in the cache (live + expired)."""
    with _lock:
        return len(_cache)


def _reset_for_tests() -> None:
    """Clear all entries. Call in test teardown only."""
    with _lock:
        _cache.clear()
