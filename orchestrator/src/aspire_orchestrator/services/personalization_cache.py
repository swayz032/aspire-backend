"""Personalization Cache — Redis warm-cache for Sarah personalization webhook.

Pass 4 of make-sure-we-have-wise-quail plan.

Cache key  : personalization:{suite_id}:{agent_id}
TTL        : 300s (5 minutes)
Backend    : Reuses the same Redis URL pattern as llm_cache.py
             (ASPIRE_REDIS_URL env var, defaults to redis://localhost:6379/1).
             Does NOT create a new connection pool — calls get_llm_cache()._ensure_redis()
             to borrow the existing pool handle, avoiding duplicate connections.

Fallback contract:
  - Every cache method is non-raising: Redis unavailable → warning log + None/noop.
  - On Redis outage: get() returns None (miss), set() is a no-op, invalidate() is a no-op.
  - Caller (sarah.py) emits personalization_cache_unavailable receipt when get() fails
    during a timeout-fallback path.

TODO (separate pass): wire Postgres LISTEN/NOTIFY on suite_profiles UPDATE to call
  invalidate(suite_id) so a tenant rebrand is reflected within seconds rather than
  waiting for TTL expiry. Skeleton receipt placeholder left as
  personalization_cache_invalidated_by_notify in receipt_store.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300  # 5 minutes
_KEY_PREFIX = "personalization"

# Allow tuning TTL per environment without code changes.
_TTL = int(os.environ.get("ASPIRE_PERSONALIZATION_CACHE_TTL_SECONDS", str(_CACHE_TTL_SECONDS)))


def _make_key(suite_id: str, agent_id: str) -> str:
    """Stable cache key scoped to tenant + agent (Law #6: no cross-tenant leakage)."""
    return f"{_KEY_PREFIX}:{suite_id}:{agent_id}"


async def _get_redis() -> Any | None:
    """Return the shared redis client handle, or None if unavailable.

    Reuses llm_cache.py's connection pool — single import path, no new pool.
    """
    try:
        from aspire_orchestrator.services.llm_cache import get_llm_cache

        cache = get_llm_cache()
        connected = await cache._ensure_redis()
        if not connected:
            return None
        return cache._redis
    except Exception as exc:
        logger.warning("personalization_cache redis_handle_error: %s", exc)
        return None


async def get(suite_id: str, agent_id: str) -> dict[str, Any] | None:
    """Return cached dyn_vars dict for (suite_id, agent_id), or None on miss/error.

    Never raises. Returns None on Redis unavailability, missing key, or
    deserialization error.
    """
    redis = await _get_redis()
    if redis is None:
        return None
    key = _make_key(suite_id, agent_id)
    try:
        raw = await redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("personalization_cache get_error key=%s: %s", key, exc)
        return None


async def set(suite_id: str, agent_id: str, payload: dict[str, Any]) -> None:
    """Write dyn_vars to cache with TTL. Non-raising — Redis outage is a no-op.

    Callers in sarah.py are required to emit personalization_cache_unavailable
    receipt when they detect a set() failure (catch exception from this layer
    is NOT needed — this function already swallows errors and logs them).
    """
    redis = await _get_redis()
    if redis is None:
        logger.warning("personalization_cache set_skipped key=%s redis_unavailable", _make_key(suite_id, agent_id))
        return
    key = _make_key(suite_id, agent_id)
    try:
        serialized = json.dumps(payload, default=str)
        await redis.setex(key, _TTL, serialized)
    except Exception as exc:
        logger.warning("personalization_cache set_error key=%s: %s", key, exc)


async def invalidate(suite_id: str) -> int:
    """Delete ALL cache entries for a suite_id across all agent_ids.

    Returns the number of keys deleted (0 on Redis unavailability).
    Used when the LISTEN/NOTIFY hook fires on suite_profiles UPDATE.

    TODO (separate pass — full LISTEN/NOTIFY wiring):
      - Add PostgreSQL trigger on suite_profiles UPDATE
      - Wire asyncio LISTEN loop in lifespan handler
      - Call personalization_cache.invalidate(suite_id) from the listener coroutine
      - Emit personalization_cache_invalidated_by_notify receipt per invalidation
    """
    redis = await _get_redis()
    if redis is None:
        logger.warning("personalization_cache invalidate_skipped suite_id=%s redis_unavailable", suite_id)
        return 0
    pattern = f"{_KEY_PREFIX}:{suite_id}:*"
    try:
        keys = await redis.keys(pattern)
        if not keys:
            return 0
        deleted = await redis.delete(*keys)
        logger.info("personalization_cache invalidated suite_id=%s keys=%d", suite_id, deleted)
        return int(deleted)
    except Exception as exc:
        logger.warning("personalization_cache invalidate_error suite_id=%s: %s", suite_id, exc)
        return 0
