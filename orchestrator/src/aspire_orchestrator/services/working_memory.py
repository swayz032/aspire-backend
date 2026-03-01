"""Working Memory — Within-session conversation buffer.

Fast in-memory (or Redis when available) storage for recent conversation
turns within an active session. Auto-expires after 2 hours.

Architecture:
  - Key pattern: aspire:memory:working:{suite_id}:{session_id}
  - TTL: 2 hours (auto-cleanup)
  - Cap: 50 turns per session (oldest evicted)
  - Fallback: In-memory dict when Redis is unavailable

Law compliance:
  - Law #6: suite_id in key ensures tenant isolation
  - Law #3: Fail-closed — returns empty on any failure
  - Law #7: Storage only, no decisions
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Constants
_TTL_SECONDS = 7200  # 2 hours
_MAX_TURNS = 50


@dataclass
class ConversationTurn:
    """A single turn in the conversation."""

    role: str  # "user" | "agent"
    content: str
    agent_id: str = ""
    timestamp: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


class WorkingMemory:
    """Within-session conversation memory.

    Uses in-memory storage with TTL-based eviction. When Redis is
    configured (REDIS_URL), uses Redis lists for persistence across
    restarts and multi-instance support.

    Thread-safe for concurrent async usage via key-based isolation.
    """

    def __init__(self) -> None:
        self._store: dict[str, list[str]] = {}
        self._expiry: dict[str, float] = {}
        self._redis_client: Any = None
        self._redis_checked = False

    def _make_key(self, suite_id: str, session_id: str) -> str:
        """Build tenant-scoped key (Law #6)."""
        return f"aspire:memory:working:{suite_id}:{session_id}"

    async def _get_redis(self) -> Any:
        """Lazy-init async Redis client. Returns None if unavailable."""
        if self._redis_checked:
            return self._redis_client

        self._redis_checked = True
        try:
            import os
            redis_url = os.environ.get("REDIS_URL") or os.environ.get("ASPIRE_REDIS_URL")
            if not redis_url:
                return None

            import redis.asyncio as aioredis
            self._redis_client = aioredis.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=2,
            )
            # Verify connectivity
            await self._redis_client.ping()
            logger.info("WorkingMemory: Redis connected")
            return self._redis_client
        except Exception as e:
            logger.info("WorkingMemory: Redis unavailable, using in-memory (%s)", e)
            self._redis_client = None
            return None

    def _evict_expired(self) -> None:
        """Remove expired in-memory sessions."""
        now = time.monotonic()
        expired = [k for k, exp in self._expiry.items() if now > exp]
        for k in expired:
            self._store.pop(k, None)
            self._expiry.pop(k, None)

    async def add_turn(
        self,
        session_id: str,
        suite_id: str,
        turn: ConversationTurn,
    ) -> None:
        """Add a conversation turn. Capped at MAX_TURNS per session."""
        key = self._make_key(suite_id, session_id)
        turn_json = json.dumps(asdict(turn), default=str)

        r = await self._get_redis()
        if r is not None:
            try:
                pipe = r.pipeline()
                pipe.rpush(key, turn_json)
                pipe.ltrim(key, -_MAX_TURNS, -1)  # Keep last N turns
                pipe.expire(key, _TTL_SECONDS)
                await pipe.execute()
                return
            except Exception as e:
                logger.warning("WorkingMemory Redis write failed: %s", e)

        # In-memory fallback
        self._evict_expired()
        if key not in self._store:
            self._store[key] = []
        self._store[key].append(turn_json)
        if len(self._store[key]) > _MAX_TURNS:
            self._store[key] = self._store[key][-_MAX_TURNS:]
        self._expiry[key] = time.monotonic() + _TTL_SECONDS

    async def get_recent_turns(
        self,
        session_id: str,
        suite_id: str,
        max_turns: int = 10,
    ) -> list[ConversationTurn]:
        """Get recent turns for LLM context window."""
        key = self._make_key(suite_id, session_id)

        r = await self._get_redis()
        if r is not None:
            try:
                raw = await r.lrange(key, -max_turns, -1)
                return [ConversationTurn(**json.loads(t)) for t in raw]
            except Exception as e:
                logger.warning("WorkingMemory Redis read failed: %s", e)

        # In-memory fallback
        self._evict_expired()
        turns_json = self._store.get(key, [])
        recent = turns_json[-max_turns:]
        return [ConversationTurn(**json.loads(t)) for t in recent]

    async def get_all_turns(
        self,
        session_id: str,
        suite_id: str,
    ) -> list[ConversationTurn]:
        """Get all turns for end-of-session summarization."""
        key = self._make_key(suite_id, session_id)

        r = await self._get_redis()
        if r is not None:
            try:
                raw = await r.lrange(key, 0, -1)
                return [ConversationTurn(**json.loads(t)) for t in raw]
            except Exception as e:
                logger.warning("WorkingMemory Redis read failed: %s", e)

        self._evict_expired()
        turns_json = self._store.get(key, [])
        return [ConversationTurn(**json.loads(t)) for t in turns_json]

    async def clear_session(
        self,
        session_id: str,
        suite_id: str,
    ) -> None:
        """Clear working memory for a session (after summarization)."""
        key = self._make_key(suite_id, session_id)

        r = await self._get_redis()
        if r is not None:
            try:
                await r.delete(key)
            except Exception:
                pass

        self._store.pop(key, None)
        self._expiry.pop(key, None)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_memory: WorkingMemory | None = None


def get_working_memory() -> WorkingMemory:
    """Get or create the singleton working memory."""
    global _memory
    if _memory is None:
        _memory = WorkingMemory()
    return _memory
