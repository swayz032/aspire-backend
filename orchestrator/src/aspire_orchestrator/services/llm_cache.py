"""LLM Response Cache — Redis-backed exact-match cache for LLM responses.

Caches classification and routing calls (deterministic prompts) to avoid
redundant API calls. Does NOT cache reasoning or high-risk profiles.

TTL strategy by profile:
- cheap_classifier: 3600s (1 hour) — classifications are deterministic
- fast_general: 300s (5 min) — reasonably stable
- primary_reasoner / high_risk_guard: NO CACHE — needs fresh reasoning
"""

import hashlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Profile -> TTL mapping. None = do not cache.
_PROFILE_TTL: dict[str, int | None] = {
    "cheap_classifier": 3600,
    "fast_general": 300,
    "voice_greeting": 3600,     # 1 hour — greetings don't change
    "voice_classifier": 300,    # 5 min — intent classification is stable
    "primary_reasoner": None,  # Never cache
    "high_risk_guard": None,   # Never cache
}

_CACHE_ENABLED = os.environ.get("ASPIRE_LLM_CACHE_ENABLED", "true").lower() == "true"


class LLMCache:
    """Redis-backed LLM response cache with profile-aware TTL."""

    def __init__(self, redis_url: str | None = None, default_ttl: int = 3600):
        self._redis: Any = None
        self._default_ttl = default_ttl
        self._redis_url = redis_url or os.environ.get("ASPIRE_REDIS_URL", "redis://localhost:6379/1")
        self._initialized = False

    async def _ensure_redis(self) -> bool:
        """Lazy-initialize Redis connection."""
        if self._initialized:
            return self._redis is not None
        self._initialized = True
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_timeout=2.0,
                socket_connect_timeout=2.0,
            )
            await self._redis.ping()
            logger.info("LLM cache connected to Redis at %s", self._redis_url)
            return True
        except Exception as e:
            logger.warning("LLM cache Redis unavailable (caching disabled): %s", e)
            self._redis = None
            return False

    @staticmethod
    def cache_key(model: str, system_prompt: str, user_prompt: str) -> str:
        """Generate cache key from model + prompts."""
        content = f"{model}::{system_prompt}::{user_prompt}"
        return f"llm:{hashlib.sha256(content.encode()).hexdigest()}"

    async def get(self, key: str) -> str | None:
        """Get cached response. Returns None on miss or error."""
        if not _CACHE_ENABLED or not await self._ensure_redis():
            return None
        try:
            result = await self._redis.get(key)
            return result
        except Exception as e:
            logger.debug("LLM cache get error (non-blocking): %s", e)
            return None

    async def set(self, key: str, response: str, profile: str | None = None) -> None:
        """Cache a response with profile-aware TTL."""
        if not _CACHE_ENABLED or not await self._ensure_redis():
            return

        ttl = _PROFILE_TTL.get(profile or "", self._default_ttl)
        if ttl is None:
            return  # Profile should not be cached

        try:
            await self._redis.setex(key, ttl, response)
        except Exception as e:
            logger.debug("LLM cache set error (non-blocking): %s", e)

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


# Module-level singleton
_llm_cache: LLMCache | None = None


def get_llm_cache() -> LLMCache:
    """Get or create the module-level LLM cache."""
    global _llm_cache
    if _llm_cache is None:
        _llm_cache = LLMCache()
    return _llm_cache
