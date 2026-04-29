"""Agentic Memory Mixin — Persistent, intelligent storage for Aspire agents.

Gives any agent a 3-tier memory system:
  1. Working Memory  — In-context dict, lives within a single conversation
  2. Episodic Memory  — Cross-session episode summaries (agent_episodes table, migration 068)
  3. Semantic Memory  — Persistent learned facts (agent_semantic_memory table, migration 068)

Agents decide what/when to store/retrieve via these methods (agentic control).
All operations are tenant-scoped (Law #6) and emit receipts (Law #2).

Usage:
    class MySkillPack(AgenticSkillPack):
        async def my_action(self, params, ctx):
            # Recall relevant context before acting
            past = await self.search_memory("client payment history", ctx)
            # ... execute action ...
            # Store learned facts after acting
            await self.remember("client_payment_pattern", "always pays net-15", ctx)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class AgentMemoryMixin:
    """Persistent, searchable memory for Aspire agents.

    Backs onto migration 068 tables:
      - agent_episodes: cross-session episode summaries with vector embeddings
      - agent_semantic_memory: persistent learned facts (key-value with types)

    Scoped by (suite_id, agent_id) — zero cross-tenant leakage (Law #6).
    Every write emits a receipt (Law #2).
    Interface-first: swap in embeddings/vector search later without changing agent code.
    """

    def __init_memory__(self) -> None:
        """Initialize working memory (tier 1 — in-context, per-conversation)."""
        self._working_memory: dict[str, Any] = {}

    # ── Tier 1: Working Memory (in-context, per-conversation) ──────────

    def working_set(self, key: str, value: Any) -> None:
        """Store a value in working memory (conversation-scoped, no persistence)."""
        self._working_memory[key] = value

    def working_get(self, key: str, default: Any = None) -> Any:
        """Retrieve a value from working memory."""
        return self._working_memory.get(key, default)

    def working_clear(self) -> None:
        """Clear all working memory (end of conversation)."""
        self._working_memory.clear()

    # ── Tier 2: Episodic Memory (cross-session, agent_episodes table) ──

    async def store_episode(
        self,
        summary: str,
        ctx: "AgentContext",
        *,
        session_id: str,
        key_topics: list[str] | None = None,
        key_entities: dict[str, Any] | None = None,
        turn_count: int = 0,
    ) -> dict[str, Any]:
        """Store an episode summary for cross-session recall.

        Episodes capture the essence of a conversation — what happened,
        who was involved, what decisions were made.

        Args:
            summary: Natural language summary of the episode
            ctx: Agent context (suite_id, actor_id for scoping)
            session_id: Conversation/session identifier
            key_topics: Tags for quick filtering (e.g., ["invoicing", "client_abc"])
            key_entities: Structured entities (e.g., {"client": "ABC Corp", "amount": 5000})
            turn_count: Number of turns in the conversation

        Returns:
            Receipt dict for the storage operation
        """
        from aspire_orchestrator.services.supabase_client import supabase_insert

        episode = {
            "suite_id": ctx.suite_id,
            "user_id": ctx.actor_id,
            "agent_id": self._agent_id,  # type: ignore[attr-defined]
            "session_id": session_id,
            "summary": summary,
            "key_topics": key_topics or [],
            "key_entities": key_entities or {},
            "turn_count": turn_count,
        }

        try:
            await supabase_insert("agent_episodes", episode)
            receipt = self.build_receipt(  # type: ignore[attr-defined]
                ctx=ctx,
                event_type="memory.episode.store",
                status="ok",
                inputs={"session_id": session_id, "summary_length": len(summary)},
                metadata={"agent_id": self._agent_id},  # type: ignore[attr-defined]
            )
            await self.emit_receipt(receipt)  # type: ignore[attr-defined]
            return receipt
        except Exception as e:
            logger.error("Failed to store episode for %s: %s", self._agent_id, e)  # type: ignore[attr-defined]
            receipt = self.build_receipt(  # type: ignore[attr-defined]
                ctx=ctx,
                event_type="memory.episode.store",
                status="failed",
                inputs={"session_id": session_id},
                metadata={"error": str(e)},
            )
            await self.emit_receipt(receipt)  # type: ignore[attr-defined]
            return receipt

    async def recall_episodes(
        self,
        ctx: "AgentContext",
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Recall recent episodes for this agent + tenant.

        Returns most recent episodes ordered by creation time.
        For semantic search, use search_memory() instead.

        Pass 7 dual-read: a parallel shadow query against memory_objects
        (memory_type='session_summary') runs alongside the legacy read; any
        divergence is logged at WARNING level. The legacy result is always
        returned to the caller -- the new path never blocks production.
        """
        from aspire_orchestrator.services.supabase_client import supabase_select

        try:
            rows = await supabase_select(
                "agent_episodes",
                filters={
                    "suite_id": ctx.suite_id,
                    "agent_id": self._agent_id,  # type: ignore[attr-defined]
                },
                order_by="created_at.desc",
                limit=limit,
            )
            legacy_rows = rows or []

            # Shadow read -- best-effort, never raises. Wrapped in an extra
            # try/except so even an import error in the shadow module cannot
            # mask the legacy result that's already been retrieved above.
            try:
                await self._shadow_recall_episodes(
                    suite_id=ctx.suite_id,
                    agent_id=self._agent_id,  # type: ignore[attr-defined]
                    limit=limit,
                    legacy_rows=legacy_rows,
                    surface="agent_memory_mixin.recall_episodes",
                )
            except Exception as shadow_exc:  # noqa: BLE001 - never propagate
                logger.warning(
                    "memory_dual_read shadow_path_outer_error: surface=%s err=%s",
                    "agent_memory_mixin.recall_episodes", shadow_exc,
                )

            return legacy_rows
        except Exception as e:
            logger.error("Failed to recall episodes: %s", e)
            return []

    async def _shadow_recall_episodes(
        self,
        *,
        suite_id: str,
        agent_id: str,
        limit: int,
        legacy_rows: list[dict[str, Any]],
        surface: str,
    ) -> None:
        """Shadow-read memory_objects for parity verification (Pass 7).

        Mirrors the filter shape of the legacy `agent_episodes` query so we
        can compare ID sets. Failures are caught and logged via
        `memory_dual_read.log_shadow_error` -- they MUST NOT propagate.
        """
        try:
            from aspire_orchestrator.services.memory_dual_read import (
                is_dual_read_enabled,
                log_divergence,
                log_shadow_error,
            )
            from aspire_orchestrator.services.supabase_client import supabase_select
        except Exception:
            return

        if not is_dual_read_enabled():
            return

        try:
            canonical_map = {
                "ava": "ava", "finn": "finn", "eli": "eli", "nora": "nora",
                "tim": "tim", "sarah": "sarah",
                "sarah-front-desk": "sarah", "sarah-frontdesk": "sarah",
                "sarah-receptionist": "sarah", "finn-finance": "finn",
            }
            source_agent = canonical_map.get((agent_id or "").lower(), "system")

            filter_str = (
                f"tenant_id=eq.{suite_id}"
                f"&suite_id=eq.{suite_id}"
                f"&memory_type=eq.session_summary"
                f"&source_agent=eq.{source_agent}"
            )
            shadow_rows = await supabase_select(
                "memory_objects",
                filter_str,
                order_by="last_activity_at.desc",
                limit=max(50, limit * 4),
            )

            legacy_ids = [str(r.get("id", "")) for r in legacy_rows]
            shadow_ids = [str(r.get("memory_id", "")) for r in (shadow_rows or [])]

            log_divergence(
                surface=surface,
                legacy_ids=legacy_ids,
                shadow_ids=shadow_ids,
                legacy_count=len(legacy_rows),
                shadow_count=len(shadow_rows or []),
                extra={"agent_id": agent_id, "source_agent": source_agent},
            )
        except Exception as exc:
            try:
                from aspire_orchestrator.services.memory_dual_read import log_shadow_error
                log_shadow_error(surface=surface, error=exc)
            except Exception:
                pass  # never raise from shadow path

    # ── Tier 3: Semantic Memory (persistent facts, agent_semantic_memory) ─

    # Valid fact types (must match DB CHECK constraint in migration 068)
    VALID_FACT_TYPES = frozenset({
        "preference", "business_fact", "relationship",
        "industry", "workflow", "communication_style",
    })
    MAX_FACTS_PER_AGENT = 500

    async def remember(
        self,
        key: str,
        value: str,
        ctx: "AgentContext",
        *,
        fact_type: str = "business_fact",
        confidence: float = 1.0,
        source_episode_id: str | None = None,
    ) -> dict[str, Any]:
        """Store or update a learned fact in semantic memory.

        The agent decides what to remember — preferences, patterns, relationships.
        Facts are upserted on (suite_id, user_id, agent_id, fact_key).

        Args:
            key: Fact identifier (e.g., "client_abc_payment_terms")
            value: The learned fact (e.g., "Always pays net-15, prefers ACH")
            ctx: Agent context for tenant scoping
            fact_type: One of: preference, business_fact, relationship,
                       industry, workflow, communication_style
            confidence: 0.0-1.0 confidence score
            source_episode_id: Link back to the episode that produced this fact

        Returns:
            Receipt dict for the storage operation
        """
        # Validate fact_type before hitting DB (fail closed — Law #3)
        if fact_type not in self.VALID_FACT_TYPES:
            receipt = self.build_receipt(  # type: ignore[attr-defined]
                ctx=ctx,
                event_type="memory.fact.store",
                status="denied",
                inputs={"fact_key": key, "fact_type": fact_type},
                metadata={"error": f"Invalid fact_type: {fact_type}"},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["INVALID_FACT_TYPE"]}
            await self.emit_receipt(receipt)  # type: ignore[attr-defined]
            return receipt

        from aspire_orchestrator.services.supabase_client import supabase_upsert

        # Prune oldest facts if at cap (Phase 3B)
        try:
            from aspire_orchestrator.services.supabase_client import supabase_rpc
            await supabase_rpc("prune_agent_semantic_memory", {
                "p_suite_id": ctx.suite_id,
                "p_user_id": ctx.actor_id,
                "p_agent_id": self._agent_id,  # type: ignore[attr-defined]
                "p_max_facts": self.MAX_FACTS_PER_AGENT,
            })
        except Exception as e:
            # Non-blocking — prune failure shouldn't prevent fact storage
            logger.warning("Failed to prune semantic memory (non-blocking): %s", e)

        fact = {
            "suite_id": ctx.suite_id,
            "user_id": ctx.actor_id,
            "agent_id": self._agent_id,  # type: ignore[attr-defined]
            "fact_type": fact_type,
            "fact_key": key,
            "fact_value": value,
            "confidence": confidence,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if source_episode_id:
            fact["source_episode_id"] = source_episode_id

        try:
            from aspire_orchestrator.services.embedding_cache import get_embedding_cache
            from aspire_orchestrator.services.legal_embedding_service import embed_text

            cache = get_embedding_cache()
            embedding = await cache.get_or_embed(
                f"{key}: {value}",
                embed_text,
                model="text-embedding-3-large",
            )
            if embedding is not None:
                fact["embedding"] = embedding
        except Exception as e:
            logger.warning("Semantic memory embedding failed (non-fatal): %s", e)

        try:
            try:
                await supabase_upsert(
                    "agent_semantic_memory",
                    fact,
                    on_conflict="suite_id,user_id,agent_id,fact_key",
                )
            except Exception:
                fact.pop("embedding", None)
                await supabase_upsert(
                    "agent_semantic_memory",
                    fact,
                    on_conflict="suite_id,user_id,agent_id,fact_key",
                )
            receipt = self.build_receipt(  # type: ignore[attr-defined]
                ctx=ctx,
                event_type="memory.fact.store",
                status="ok",
                inputs={"fact_key": key, "fact_type": fact_type},
                metadata={"agent_id": self._agent_id, "confidence": confidence},  # type: ignore[attr-defined]
            )
            await self.emit_receipt(receipt)  # type: ignore[attr-defined]
            return receipt
        except Exception as e:
            logger.error("Failed to store fact %s: %s", key, e)
            receipt = self.build_receipt(  # type: ignore[attr-defined]
                ctx=ctx,
                event_type="memory.fact.store",
                status="failed",
                inputs={"fact_key": key},
                metadata={"error": str(e)},
            )
            await self.emit_receipt(receipt)  # type: ignore[attr-defined]
            return receipt

    async def recall(self, key: str, ctx: "AgentContext") -> str | None:
        """Recall a specific fact by key. Returns the value or None."""
        from aspire_orchestrator.services.supabase_client import supabase_select

        try:
            rows = await supabase_select(
                "agent_semantic_memory",
                filters={
                    "suite_id": ctx.suite_id,
                    "user_id": ctx.actor_id,
                    "agent_id": self._agent_id,  # type: ignore[attr-defined]
                    "fact_key": key,
                },
                limit=1,
            )
            if rows:
                return rows[0].get("fact_value")
            return None
        except Exception as e:
            logger.error("Failed to recall fact %s: %s", key, e)
            return None

    async def search_memory(
        self,
        query: str,
        ctx: "AgentContext",
        *,
        fact_type: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Search semantic memory using hybrid semantic + lexical retrieval.

        Args:
            query: Search query (matched against keys and values)
            ctx: Agent context for tenant scoping
            fact_type: Optional filter by fact type
            limit: Max results to return

        Returns:
            List of matching fact dicts with keys: fact_key, fact_value,
            fact_type, confidence, updated_at
        """
        try:
            from aspire_orchestrator.services.semantic_memory import get_semantic_memory

            memory = get_semantic_memory()
            rows = await memory.search_facts(
                query=query,
                suite_id=ctx.suite_id,
                user_id=ctx.actor_id,
                agent_id=self._agent_id,  # type: ignore[attr-defined]
                fact_type=fact_type,
                limit=limit,
            )
            return rows or []
        except Exception as e:
            logger.error("Failed to search memory: %s", e)
            return []

    async def forget(self, key: str, ctx: "AgentContext") -> bool:
        """Soft-delete a fact from semantic memory.

        Marks the fact with confidence=0 rather than hard-deleting
        (Law #2: receipts are append-only, data modifications are tracked).

        Returns True if the fact was found and marked, False otherwise.
        """
        from aspire_orchestrator.services.supabase_client import supabase_upsert

        try:
            # First, look up the existing fact to preserve its fact_type
            from aspire_orchestrator.services.supabase_client import supabase_select

            existing_type = "preference"  # Fallback if fact doesn't exist
            try:
                rows = await supabase_select(
                    "agent_semantic_memory",
                    filters={
                        "suite_id": ctx.suite_id,
                        "user_id": ctx.actor_id,
                        "agent_id": self._agent_id,  # type: ignore[attr-defined]
                        "fact_key": key,
                    },
                    limit=1,
                )
                if rows:
                    existing_type = rows[0].get("fact_type", "preference")
            except Exception:
                pass  # Use fallback type

            await supabase_upsert(
                "agent_semantic_memory",
                {
                    "suite_id": ctx.suite_id,
                    "user_id": ctx.actor_id,
                    "agent_id": self._agent_id,  # type: ignore[attr-defined]
                    "fact_key": key,
                    "confidence": 0.0,
                    "fact_value": "[forgotten]",
                    "fact_type": existing_type,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="suite_id,user_id,agent_id,fact_key",
            )
            receipt = self.build_receipt(  # type: ignore[attr-defined]
                ctx=ctx,
                event_type="memory.fact.forget",
                status="ok",
                inputs={"fact_key": key},
            )
            await self.emit_receipt(receipt)  # type: ignore[attr-defined]
            return True
        except Exception as e:
            logger.error("Failed to forget fact %s: %s", key, e)
            return False
