"""Episodic Memory — Cross-session episode summaries with vector search.

Stores LLM-generated session summaries with embeddings for semantic
recall of past conversations. Enables agents to remember what was
discussed in previous sessions.

End-of-session flow:
  1. Load all working memory turns
  2. Call GPT-5-mini to summarize key topics and entities
  3. Embed summary with text-embedding-3-large
  4. Store in agent_episodes (Supabase, RLS-scoped)

Law compliance:
  - Law #2: Episode storage generates receipt
  - Law #3: Fail-closed — returns empty on failure
  - Law #6: Suite-scoped queries (RLS + explicit suite_id filter)
  - Law #7: Storage only, no decisions
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.receipt_store import store_receipts

logger = logging.getLogger(__name__)


@dataclass
class Episode:
    """A past session episode recalled from memory."""

    episode_id: str = ""
    agent_id: str = ""
    session_id: str = ""
    summary: str = ""
    key_topics: list[str] = field(default_factory=list)
    key_entities: dict[str, Any] = field(default_factory=dict)
    turn_count: int = 0
    created_at: str = ""
    similarity: float = 0.0


class EpisodicMemory:
    """Cross-session memory via LLM-generated episode summaries.

    After a session ends, the orchestrator calls summarize_and_store()
    which uses GPT-5-mini to generate a concise summary, embeds it,
    and stores it in Supabase for future semantic recall.
    """

    async def summarize_and_store(
        self,
        turns: list[dict[str, Any]],
        session_id: str,
        suite_id: str,
        user_id: str,
        agent_id: str,
    ) -> str | None:
        """Summarize a session and store as an episode.

        Args:
            turns: List of conversation turns (from working memory)
            session_id: The session being summarized
            suite_id: Tenant ID (Law #6)
            user_id: User who participated
            agent_id: Primary agent in the session

        Returns:
            Episode ID if stored, None on failure.
        """
        if not turns or len(turns) < 2:
            logger.debug("Skipping episode storage: too few turns (%d)", len(turns))
            return None

        receipt_id = f"rcpt-ep-{uuid.uuid4().hex[:12]}"

        try:
            # 1. Format turns for summarization
            conversation = "\n".join(
                f"{t.get('role', 'unknown')}: {t.get('content', '')}"
                for t in turns[:50]  # Cap at 50 turns for context window
            )

            # 2. Call GPT-5-mini for summarization
            from aspire_orchestrator.config.settings import settings
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=settings.openai_api_key)
            summary_response = await client.chat.completions.create(
                model="gpt-5-mini",
                messages=[
                    {
                        "role": "developer",
                        "content": (
                            "Summarize this conversation in 2-4 sentences. "
                            "Focus on: what was discussed, key decisions made, "
                            "and any follow-up items. Extract key topics as a "
                            "comma-separated list. Extract key entities as JSON "
                            "(business_name, industry, people, amounts, dates).\n\n"
                            "Respond in this exact format:\n"
                            "SUMMARY: <summary>\n"
                            "TOPICS: <comma-separated topics>\n"
                            "ENTITIES: <json object>"
                        ),
                    },
                    {"role": "user", "content": conversation},
                ],
                max_completion_tokens=300,
            )

            raw_output = summary_response.choices[0].message.content or ""

            # 3. Parse structured output
            summary, topics, entities = self._parse_summary_output(raw_output)

            # 4. Embed summary for vector search
            embedding = None
            try:
                from aspire_orchestrator.services.legal_embedding_service import embed_text
                embedding = await embed_text(summary)
            except Exception as e:
                logger.warning("Episode embedding failed (non-fatal): %s", e)

            # 5. Store in Supabase
            from aspire_orchestrator.services.supabase_client import supabase_insert

            episode_id = str(uuid.uuid4())
            row = {
                "id": episode_id,
                "suite_id": suite_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "summary": summary,
                "key_topics": topics,
                "key_entities": entities,
                "turn_count": len(turns),
            }
            if embedding is not None:
                row["embedding"] = embedding

            await supabase_insert("agent_episodes", row)

            # 6. Receipt (Law #2)
            store_receipts([{
                "receipt_id": receipt_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "event_type": "memory.episode_stored",
                "actor": "service:episodic-memory",
                "suite_id": suite_id,
                "action_type": "memory.summarize_and_store",
                "risk_tier": "green",
                "tool_used": "episodic_memory",
                "outcome": "success",
                "reason_code": "EXECUTED",
                "agent_id": agent_id,
                "session_id": session_id,
                "turn_count": len(turns),
                "topic_count": len(topics),
            }])

            logger.info(
                "Episode stored: agent=%s session=%s turns=%d topics=%d",
                agent_id, session_id, len(turns), len(topics),
            )
            return episode_id

        except Exception as e:
            logger.warning("Episode storage failed (non-fatal): %s", e)
            return None

    def _parse_summary_output(
        self, raw: str
    ) -> tuple[str, list[str], dict[str, Any]]:
        """Parse structured summary output from GPT-5-mini."""
        import json as _json

        summary = raw
        topics: list[str] = []
        entities: dict[str, Any] = {}

        for line in raw.split("\n"):
            line = line.strip()
            if line.upper().startswith("SUMMARY:"):
                summary = line[len("SUMMARY:"):].strip()
            elif line.upper().startswith("TOPICS:"):
                raw_topics = line[len("TOPICS:"):].strip()
                topics = [t.strip() for t in raw_topics.split(",") if t.strip()]
            elif line.upper().startswith("ENTITIES:"):
                raw_entities = line[len("ENTITIES:"):].strip()
                try:
                    entities = _json.loads(raw_entities)
                except _json.JSONDecodeError:
                    pass

        return summary, topics, entities

    async def search_relevant_episodes(
        self,
        query: str,
        suite_id: str,
        agent_id: str,
        user_id: str | None = None,
        max_episodes: int = 3,
    ) -> list[Episode]:
        """Find past sessions relevant to current query.

        Uses vector similarity search on episode summaries.
        Returns empty list on any failure (Law #3).
        """
        if not query or not query.strip():
            return []

        try:
            # 1. Embed query
            from aspire_orchestrator.services.legal_embedding_service import embed_text
            query_embedding = await embed_text(query)
            if query_embedding is None:
                return []

            # 2. Search via Supabase RPC
            from aspire_orchestrator.services.supabase_client import supabase_rpc

            params: dict[str, Any] = {
                "query_embedding": query_embedding,
                "p_suite_id": suite_id,
                "p_agent_id": agent_id,
                "p_limit": max_episodes,
                "p_min_similarity": 0.35,
            }
            if user_id:
                params["p_user_id"] = user_id

            result = await supabase_rpc("search_agent_episodes", params)

            # 3. Parse results
            rows = result if isinstance(result, list) else []
            episodes = []
            for row in rows:
                episodes.append(Episode(
                    episode_id=str(row.get("id", "")),
                    agent_id=row.get("agent_id", ""),
                    session_id=row.get("session_id", ""),
                    summary=row.get("summary", ""),
                    key_topics=row.get("key_topics", []),
                    key_entities=row.get("key_entities", {}),
                    turn_count=row.get("turn_count", 0),
                    created_at=str(row.get("created_at", "")),
                    similarity=float(row.get("similarity", 0)),
                ))

            logger.debug(
                "EpisodicMemory: found %d relevant episodes for agent=%s",
                len(episodes), agent_id,
            )
            return episodes

        except Exception as e:
            logger.warning("Episode search failed (non-fatal): %s", e)
            return []


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_memory: EpisodicMemory | None = None


def get_episodic_memory() -> EpisodicMemory:
    """Get or create the singleton episodic memory."""
    global _memory
    if _memory is None:
        _memory = EpisodicMemory()
    return _memory
