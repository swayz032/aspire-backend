"""Semantic Memory — Persistent learned facts about users.

Extracts and stores long-term facts from conversations:
  - Business facts: industry, company size, revenue range
  - Preferences: invoice format, communication style, scheduling habits
  - Relationships: key contacts, vendors, clients
  - Workflows: how the user prefers to handle tasks

Facts are upserted on (suite_id, user_id, agent_id, fact_key) so
they naturally update as the agent learns more.

Law compliance:
  - Law #2: Fact extraction generates receipt
  - Law #3: Fail-closed — returns empty on failure
  - Law #6: Suite-scoped queries (RLS + explicit suite_id)
  - Law #7: Storage only, no decisions
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.services.openai_client import generate_text_async, parse_json_text
from aspire_orchestrator.services.receipt_store import store_receipts

logger = logging.getLogger(__name__)


def _is_uuid(value: str | None) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(str(value))
        return True
    except Exception:
        return False


@dataclass
class Fact:
    """A learned fact about a user."""

    fact_id: str = ""
    fact_type: str = ""
    fact_key: str = ""
    fact_value: str = ""
    confidence: float = 1.0
    created_at: str = ""
    updated_at: str = ""


class SemanticMemory:
    """Persistent user fact memory.

    After meaningful conversations, uses GPT-5-mini to extract facts
    about the user (industry, preferences, relationships, etc.) and
    upserts them into Supabase for future personalization.
    """

    async def extract_and_store(
        self,
        turns: list[dict[str, Any]],
        suite_id: str,
        user_id: str,
        agent_id: str,
        source_episode_id: str | None = None,
    ) -> int:
        """Extract facts from conversation and upsert into memory.

        Args:
            turns: Conversation turns to analyze
            suite_id: Tenant ID (Law #6)
            user_id: User being learned about
            agent_id: Agent doing the learning
            source_episode_id: Link to the episode that generated these facts

        Returns:
            Number of facts extracted and stored.
        """
        if not turns or len(turns) < 2:
            return 0
        if not _is_uuid(suite_id) or not _is_uuid(user_id):
            return 0

        receipt_id = f"rcpt-sm-{uuid.uuid4().hex[:12]}"

        try:
            # 1. Format conversation for fact extraction
            conversation = "\n".join(
                f"{t.get('role', 'unknown')}: {t.get('content', '')}"
                for t in turns[:30]  # Cap for token efficiency
            )

            # 2. Call GPT-5-mini for fact extraction
            raw = await generate_text_async(
                model="gpt-5-mini",
                messages=[
                    {
                        "role": "developer",
                        "content": (
                            "Extract factual information about the USER from this "
                            "conversation. Only extract facts the user explicitly "
                            "stated or clearly implied. Do NOT infer or guess.\n\n"
                            "Return a JSON array of objects with these fields:\n"
                            '- fact_type: one of "preference", "business_fact", '
                            '"relationship", "industry", "workflow", "communication_style"\n'
                            "- fact_key: short identifier (e.g., \"industry\", "
                            "\"preferred_invoice_format\", \"main_client\")\n"
                            "- fact_value: the fact itself\n"
                            "- confidence: 0.0-1.0 (how certain based on conversation)\n\n"
                            "If no facts can be extracted, return an empty array [].\n"
                            "Return ONLY the JSON array, no other text."
                        ),
                    },
                    {"role": "user", "content": conversation},
                ],
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                timeout_seconds=float(settings.openai_timeout_seconds),
                max_output_tokens=400,
                prefer_responses_api=True,
            ) or "[]"

            # 3. Parse extracted facts
            try:
                parsed = parse_json_text(raw)
                if not parsed and raw.strip().startswith("["):
                    parsed = json.loads(raw)
                # Handle both {"facts": [...]} and [...] formats
                if isinstance(parsed, dict):
                    facts = parsed.get("facts", parsed.get("items", []))
                elif isinstance(parsed, list):
                    facts = parsed
                else:
                    facts = []
            except json.JSONDecodeError:
                logger.warning("Semantic fact extraction returned invalid JSON")
                facts = []

            if not facts:
                return 0

            # 4. Validate and upsert each fact
            stored_count = 0
            for fact_data in facts:
                if not isinstance(fact_data, dict):
                    continue

                fact_type = fact_data.get("fact_type", "")
                fact_key = fact_data.get("fact_key", "")
                fact_value = fact_data.get("fact_value", "")
                confidence = float(fact_data.get("confidence", 0.8))

                if not fact_type or not fact_key or not fact_value:
                    continue

                # Validate fact_type
                valid_types = {
                    "preference", "business_fact", "relationship",
                    "industry", "workflow", "communication_style",
                }
                if fact_type not in valid_types:
                    continue

                try:
                    await self._upsert_fact(
                        suite_id=suite_id,
                        user_id=user_id,
                        agent_id=agent_id,
                        fact_type=fact_type,
                        fact_key=fact_key,
                        fact_value=fact_value,
                        confidence=confidence,
                        source_episode_id=source_episode_id,
                    )
                    stored_count += 1
                except Exception as e:
                    logger.warning("Failed to upsert fact %s: %s", fact_key, e)

            # 5. Receipt (Law #2)
            store_receipts([{
                "receipt_id": receipt_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "event_type": "memory.facts_extracted",
                "actor": "service:semantic-memory",
                "suite_id": suite_id,
                "action_type": "memory.extract_and_store",
                "risk_tier": "green",
                "tool_used": "semantic_memory",
                "outcome": "success",
                "reason_code": "EXECUTED",
                "agent_id": agent_id,
                "facts_extracted": stored_count,
                "facts_attempted": len(facts),
            }])

            logger.info(
                "SemanticMemory: stored %d/%d facts for agent=%s",
                stored_count, len(facts), agent_id,
            )
            return stored_count

        except Exception as e:
            logger.warning("Semantic fact extraction failed (non-fatal): %s", e)
            return 0

    async def _upsert_fact(
        self,
        *,
        suite_id: str,
        user_id: str,
        agent_id: str,
        fact_type: str,
        fact_key: str,
        fact_value: str,
        confidence: float,
        source_episode_id: str | None,
    ) -> None:
        """Upsert a single fact. INSERT on conflict UPDATE."""
        from aspire_orchestrator.services.supabase_client import (
            supabase_insert,
            supabase_select,
            supabase_update,
        )

        now = datetime.now(timezone.utc).isoformat()

        # Check if fact exists
        filters = (
            f"suite_id=eq.{suite_id}"
            f"&user_id=eq.{user_id}"
            f"&agent_id=eq.{agent_id}"
            f"&fact_key=eq.{fact_key}"
        )
        existing = await supabase_select("agent_semantic_memory", filters)

        if existing:
            # Update existing fact
            row_id = existing[0].get("id")
            await supabase_update(
                "agent_semantic_memory",
                f"id=eq.{row_id}",
                {
                    "fact_value": fact_value,
                    "fact_type": fact_type,
                    "confidence": confidence,
                    "source_episode_id": source_episode_id,
                    "updated_at": now,
                },
            )
        else:
            # Insert new fact
            await supabase_insert("agent_semantic_memory", {
                "id": str(uuid.uuid4()),
                "suite_id": suite_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "fact_type": fact_type,
                "fact_key": fact_key,
                "fact_value": fact_value,
                "confidence": confidence,
                "source_episode_id": source_episode_id,
                "created_at": now,
                "updated_at": now,
            })

    async def get_user_facts(
        self,
        suite_id: str,
        user_id: str,
        agent_id: str,
    ) -> list[Fact]:
        """Load all known facts about the current user for an agent.

        Returns empty list on any failure (Law #3).
        """
        if not _is_uuid(suite_id) or not _is_uuid(user_id):
            return []
        try:
            from aspire_orchestrator.services.supabase_client import supabase_select

            filters = (
                f"suite_id=eq.{suite_id}"
                f"&user_id=eq.{user_id}"
                f"&agent_id=eq.{agent_id}"
                f"&order=updated_at.desc"
            )
            rows = await supabase_select("agent_semantic_memory", filters)

            return [
                Fact(
                    fact_id=str(row.get("id", "")),
                    fact_type=row.get("fact_type", ""),
                    fact_key=row.get("fact_key", ""),
                    fact_value=row.get("fact_value", ""),
                    confidence=float(row.get("confidence", 1.0)),
                    created_at=str(row.get("created_at", "")),
                    updated_at=str(row.get("updated_at", "")),
                )
                for row in rows
            ]

        except Exception as e:
            logger.warning("SemanticMemory load failed (non-fatal): %s", e)
            return []


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_memory: SemanticMemory | None = None


def get_semantic_memory() -> SemanticMemory:
    """Get or create the singleton semantic memory."""
    global _memory
    if _memory is None:
        _memory = SemanticMemory()
    return _memory
