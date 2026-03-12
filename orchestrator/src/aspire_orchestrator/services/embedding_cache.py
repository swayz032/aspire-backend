"""
Cross-domain embedding cache using Redis.
Embeddings are deterministic (same text -> same vector), so caching is safe.
TTL: 3600s (1 hour). Shared across all retrieval services.

Phase 5C of the scaling plan.
"""

import hashlib
import json
import logging
import os
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

# Try to import redis, gracefully degrade if unavailable
try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("redis package not available -- embedding cache disabled")


class EmbeddingCache:
    """Redis-backed embedding cache with TTL expiry."""

    def __init__(self, redis_url: str | None = None, ttl: int = 3600, prefix: str = "emb") -> None:
        self._ttl = ttl
        self._prefix = prefix
        self._redis: "aioredis.Redis | None" = None
        self._redis_url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        self._enabled = REDIS_AVAILABLE and bool(self._redis_url)

    async def _get_redis(self) -> "aioredis.Redis | None":
        if not self._enabled:
            return None
        if self._redis is None:
            try:
                self._redis = aioredis.from_url(
                    self._redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                )
                await self._redis.ping()
            except Exception as e:
                logger.warning("Redis connection failed for embedding cache: %s", e)
                self._enabled = False
                self._redis = None
                return None
        return self._redis

    def _cache_key(self, text: str, model: str = "") -> str:
        content = f"{model}:{text}" if model else text
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]
        return f"{self._prefix}:{h}"

    async def get_or_embed(
        self,
        text: str,
        embed_fn: Callable[[str], Awaitable[list[float]]],
        model: str = "",
    ) -> list[float]:
        """Get cached embedding or compute and cache it."""
        r = await self._get_redis()
        if r is not None:
            key = self._cache_key(text, model)
            try:
                cached = await r.get(key)
                if cached:
                    return json.loads(cached)
            except Exception as e:
                logger.debug("Embedding cache read error: %s", e)

        # Cache miss or Redis unavailable -- compute embedding
        result = await embed_fn(text)

        # Store in cache (best-effort)
        if r is not None:
            try:
                key = self._cache_key(text, model)
                await r.setex(key, self._ttl, json.dumps(result))
            except Exception as e:
                logger.debug("Embedding cache write error: %s", e)

        return result

    async def get_or_embed_batch(
        self,
        texts: list[str],
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
        model: str = "",
    ) -> list[list[float]]:
        """Batch version: check cache for each text, compute missing, cache results."""
        r = await self._get_redis()
        results: list[list[float] | None] = [None] * len(texts)
        uncached_indices: list[int] = []

        if r is not None:
            # Check cache for all texts
            keys = [self._cache_key(t, model) for t in texts]
            try:
                cached_values = await r.mget(keys)
                for i, val in enumerate(cached_values):
                    if val:
                        results[i] = json.loads(val)
                    else:
                        uncached_indices.append(i)
            except Exception:
                uncached_indices = list(range(len(texts)))
        else:
            uncached_indices = list(range(len(texts)))

        # Compute uncached embeddings
        if uncached_indices:
            uncached_texts = [texts[i] for i in uncached_indices]
            computed = await embed_fn(uncached_texts)
            for idx, embedding in zip(uncached_indices, computed):
                results[idx] = embedding

            # Cache computed results (best-effort)
            if r is not None:
                try:
                    pipe = r.pipeline()
                    for idx, embedding in zip(uncached_indices, computed):
                        key = self._cache_key(texts[idx], model)
                        pipe.setex(key, self._ttl, json.dumps(embedding))
                    await pipe.execute()
                except Exception as e:
                    logger.debug("Embedding cache batch write error: %s", e)

        return results  # type: ignore[return-value]

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()
            self._redis = None


# Module-level singleton
_embedding_cache: EmbeddingCache | None = None


def get_embedding_cache() -> EmbeddingCache:
    global _embedding_cache
    if _embedding_cache is None:
        _embedding_cache = EmbeddingCache()
    return _embedding_cache
