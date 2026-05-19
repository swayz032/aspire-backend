"""Drew LLM Response Cache — Redis-backed deterministic-output caching (Wave 9h).

Caches Drew stage (CLASSIFY / REASON) LLM responses by content hash so that
re-running a stage against the same sheet OCR text returns the cached response
immediately, skipping the OpenAI call entirely.

This is distinct from the existing LLMCache (services/llm_cache.py), which caches
all LLM calls by full-prompt hash.  This cache is Drew-specific:
  - Key includes stage, model, AND a SHA-256 of the dynamic user-message content.
  - 24 h TTL by default.
  - Fail-open: if Redis is unavailable the pipeline runs normally.
  - In-memory LRU fallback for single-process operation without Redis.

Cache hit receipts are emitted with action_type=blueprint.llm.cache_hit and
cache_layer=redis|memory so ledger queries can distinguish this layer from
OpenAI's own prefix caching.

Key format:
    aspire:blueprint:llm:{stage}:{model}:{content_hash}

where content_hash = SHA-256 of the user-message dynamic content (sheet OCR for
CLASSIFY, full story-input payload for REASON).

Idempotency-clear integration:
    clear_llm_cache_for_project(project_id, suite_id) is called from
    _async_reason_pipeline when idempotency_clear runs so stale cached responses
    are evicted before a re-run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_ENABLED = os.environ.get("ASPIRE_LLM_RESPONSE_CACHE_ENABLED", "true").lower() == "true"
_REDIS_URL = os.environ.get("ASPIRE_REDIS_URL", "redis://localhost:6379/1")
_KEY_PREFIX = "aspire:blueprint:llm"

# ---------------------------------------------------------------------------
# Redis connection (mirrors llm_cache.py connection setup exactly)
# ---------------------------------------------------------------------------

_redis_client: Any = None  # lazy-initialized async redis client
_redis_initialized: bool = False


async def _get_redis() -> Any:
    """Return async Redis client, initializing lazily.  Returns None on failure (fail-open)."""
    global _redis_client, _redis_initialized
    if _redis_initialized:
        return _redis_client
    _redis_initialized = True
    try:
        import redis.asyncio as aioredis  # type: ignore[import]

        client = aioredis.from_url(
            _REDIS_URL,
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
        await client.ping()
        _redis_client = client
        logger.info("llm_response_cache: connected to Redis at %s", _REDIS_URL)
        return _redis_client
    except Exception as exc:
        logger.warning(
            "llm_response_cache: Redis unavailable — falling back to in-memory LRU: %s", exc
        )
        _redis_client = None
        return None


# ---------------------------------------------------------------------------
# In-memory LRU fallback (single-process, non-persistent)
# ---------------------------------------------------------------------------

# LRU cache keyed by (stage, model, content_hash).  maxsize=256 keeps ~256 responses
# in memory — enough for a typical Drew session (20–50 sheets per project, 5 batches).
@lru_cache(maxsize=256)
def _lru_get(stage: str, model: str, content_hash: str) -> str | None:
    """LRU fallback GET — returns None on miss.

    Note: lru_cache cannot expire entries by TTL; entries live for the process lifetime.
    This is intentional — the in-memory fallback is for single-process operation only.
    """
    return None  # All LRU state is managed in _lru_store dict below


# We store actual values in a plain dict (lru_cache above just tracks key access).
_lru_store: dict[tuple[str, str, str], str] = {}
_LRU_MAX = 256


def _lru_store_set(stage: str, model: str, content_hash: str, value: str) -> None:
    """Write to in-memory LRU store, evicting oldest entry at capacity."""
    key = (stage, model, content_hash)
    if len(_lru_store) >= _LRU_MAX:
        # Evict arbitrary oldest entry (dict is insertion-ordered in Python 3.7+)
        oldest = next(iter(_lru_store))
        del _lru_store[oldest]
    _lru_store[key] = value


def _lru_store_get(stage: str, model: str, content_hash: str) -> str | None:
    return _lru_store.get((stage, model, content_hash))


def _lru_store_delete_prefix(stage: str) -> int:
    """Delete all entries for a given stage prefix.  Returns count deleted."""
    keys = [k for k in _lru_store if k[0] == stage]
    for k in keys:
        del _lru_store[k]
    return len(keys)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_content_hash(content: str) -> str:
    """SHA-256 of dynamic user-message content.  Stable across calls for identical input."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _redis_key(stage: str, model: str, content_hash: str) -> str:
    return f"{_KEY_PREFIX}:{stage}:{model}:{content_hash}"


async def get_cached_llm_response(
    stage: str,
    content_hash: str,
    model: str,
) -> dict[str, Any] | None:
    """Look up a cached LLM response by stage + content_hash + model.

    Returns the cached response dict, or None on miss or cache disabled.
    Failure to reach Redis is handled gracefully — returns None (fail-open).

    Cache layer precedence:
      1. Redis (shared across instances)
      2. In-memory LRU (single-process fallback)
    """
    if not _CACHE_ENABLED:
        return None

    redis = await _get_redis()
    if redis is not None:
        try:
            raw = await redis.get(_redis_key(stage, model, content_hash))
            if raw is not None:
                parsed: dict[str, Any] = json.loads(raw)
                logger.debug(
                    "llm_response_cache: Redis HIT stage=%s model=%s hash=%s",
                    stage, model, content_hash[:8],
                )
                return parsed
        except Exception as exc:
            logger.warning(
                "llm_response_cache: Redis get failed (non-blocking): %s", exc
            )
            # Fall through to in-memory LRU

    # In-memory LRU fallback
    lru_hit = _lru_store_get(stage, model, content_hash)
    if lru_hit is not None:
        try:
            parsed = json.loads(lru_hit)
            logger.debug(
                "llm_response_cache: LRU HIT stage=%s model=%s hash=%s",
                stage, model, content_hash[:8],
            )
            return parsed
        except Exception:
            pass

    return None


async def set_cached_llm_response(
    stage: str,
    content_hash: str,
    model: str,
    response: dict[str, Any],
    ttl_seconds: int = 86400,
) -> None:
    """Store a LLM response in the cache (Redis primary, LRU fallback).

    Best-effort: errors are logged but do not propagate (Law #3 — fail-open here
    because Drew should always work without the cache).

    Args:
        stage: Pipeline stage name (e.g. "classify", "reason").
        content_hash: SHA-256 of user-message dynamic content.
        model: OpenAI model identifier.
        response: The full LLM response dict to cache.
        ttl_seconds: Redis TTL (default 24h). Ignored by LRU fallback.
    """
    if not _CACHE_ENABLED:
        return

    serialized: str
    try:
        serialized = json.dumps(response)
    except Exception as exc:
        logger.warning("llm_response_cache: JSON serialization failed: %s", exc)
        return

    redis = await _get_redis()
    if redis is not None:
        try:
            await redis.setex(_redis_key(stage, model, content_hash), ttl_seconds, serialized)
            logger.debug(
                "llm_response_cache: Redis SET stage=%s model=%s hash=%s ttl=%ds",
                stage, model, content_hash[:8], ttl_seconds,
            )
        except Exception as exc:
            logger.warning(
                "llm_response_cache: Redis set failed (non-blocking): %s", exc
            )
            # Write to LRU as fallback
            _lru_store_set(stage, model, content_hash, serialized)
    else:
        # Redis unavailable — write to LRU only
        _lru_store_set(stage, model, content_hash, serialized)


async def clear_llm_cache_for_project(project_id: str, suite_id: str) -> int:
    """Evict all Drew LLM cache entries for a given project + suite.

    Called from _async_reason_pipeline idempotency_clear so stale cached
    responses are removed before a re-run.

    Redis implementation: scans for keys matching
        aspire:blueprint:llm:*  (stage-scoped, not project-scoped)
    Note: because content_hash encodes the user payload (which includes project_id
    indirectly via sheet OCR), a full-stage scan is the safest approach.
    In practice the key space is small (< 1k per stage).

    Returns the count of keys deleted.
    """
    deleted = 0

    redis = await _get_redis()
    if redis is not None:
        try:
            pattern = f"{_KEY_PREFIX}:*"
            # Use SCAN to avoid blocking Redis with KEYS on large databases
            cursor = 0
            while True:
                cursor, keys = await redis.scan(cursor, match=pattern, count=100)
                for key in keys:
                    await redis.delete(key)
                    deleted += 1
                if cursor == 0:
                    break
            logger.info(
                "llm_response_cache: cleared %d Redis keys for project=%s suite=%s",
                deleted, project_id[:8] if len(project_id) > 8 else project_id,
                suite_id[:8] if len(suite_id) > 8 else suite_id,
            )
        except Exception as exc:
            logger.warning(
                "llm_response_cache: Redis clear failed (non-blocking): %s", exc
            )

    # Also clear in-memory LRU (all stages)
    for stage in ("classify", "reason", "procure"):
        lru_deleted = _lru_store_delete_prefix(stage)
        deleted += lru_deleted

    return deleted


def reset_redis_connection() -> None:
    """Reset the Redis connection (for testing only)."""
    global _redis_client, _redis_initialized
    _redis_client = None
    _redis_initialized = False


def clear_lru_store() -> None:
    """Clear the in-memory LRU store (for testing only)."""
    _lru_store.clear()
