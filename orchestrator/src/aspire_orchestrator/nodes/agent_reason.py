"""Agent Reasoning Node — Conversational Intelligence (Wave 1 + Wave 4 Memory).

When the classifier determines the user's intent is conversational
(knowledge question, advice, greeting, general chat), this node
generates an intelligent response using the target agent's persona,
domain context, and 4-tier memory architecture.

Memory tiers loaded before LLM call:
  1. Working Memory  — recent turns from current session (Redis/in-memory)
  2. Episodic Memory  — relevant past session summaries (Supabase + vector)
  3. Semantic Memory  — learned user facts (Supabase)
  4. Procedural       — persona files + RAG (loaded as context)

Governance: This is a GREEN operation (read-only, no state change).
Still generates a receipt (Law #2) but no approval required.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.services.openai_client import generate_text_async
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)

# Agent persona files (same map as respond.py)
_PERSONA_MAP: dict[str, str] = {
    "ava": "ava_user_system_prompt.md",
    "finn": "finn_fm_system_prompt.md",
    "eli": "eli_system_prompt.md",
    "quinn": "quinn_system_prompt.md",
    "nora": "nora_system_prompt.md",
    "sarah": "sarah_system_prompt.md",
    "adam": "adam_system_prompt.md",
    "tec": "tec_system_prompt.md",
    "teressa": "teressa_system_prompt.md",
    "milo": "milo_system_prompt.md",
    "clara": "clara_system_prompt.md",
    "mail_ops": "mail_ops_system_prompt.md",
}

_PERSONAS_DIR = Path(__file__).parent.parent / "config" / "pack_personas"

_AWARENESS_FILE = Path(__file__).parent.parent / "config" / "aspire_awareness.md"
_AWARENESS_CACHE: str | None = None
_LAST_EPISODE_TURN_SNAPSHOT: dict[str, int] = {}
_ENABLE_BACKGROUND_MEMORY_PERSISTENCE = (
    os.getenv("ENABLE_BACKGROUND_MEMORY_PERSISTENCE", "true").strip().lower()
    not in {"0", "false", "off"}
)


async def _persist_memory_layers(
    *,
    session_id: str,
    suite_id: str,
    actor_id: str,
    agent_id: str,
) -> None:
    """Persist semantic/episodic memory in the background.

    This runs non-blocking from the conversational path so user latency is
    unaffected while long-term memory still improves over time.
    """
    if not session_id or suite_id in ("", "unknown") or actor_id in ("", "unknown"):
        return

    try:
        from aspire_orchestrator.services.episodic_memory import get_episodic_memory
        from aspire_orchestrator.services.semantic_memory import get_semantic_memory
        from aspire_orchestrator.services.working_memory import get_working_memory

        wm = get_working_memory()
        em = get_episodic_memory()
        sm = get_semantic_memory()

        turns = await wm.get_recent_turns(session_id, suite_id, max_turns=20)
        if len(turns) < 2:
            return

        turns_payload = [
            {
                "role": turn.role,
                "content": turn.content,
                "agent_id": turn.agent_id,
                "timestamp": turn.timestamp,
            }
            for turn in turns
        ]

        await sm.extract_and_store(
            turns=turns_payload,
            suite_id=suite_id,
            user_id=actor_id,
            agent_id=agent_id,
        )

        # Summarize periodically to avoid expensive storage writes every turn.
        turn_count = len(turns_payload)
        key = f"{suite_id}:{session_id}:{agent_id}"
        last_snapshot = _LAST_EPISODE_TURN_SNAPSHOT.get(key, 0)
        if turn_count >= 12 and (turn_count - last_snapshot) >= 10:
            episode_id = await em.summarize_and_store(
                turns=turns_payload,
                session_id=session_id,
                suite_id=suite_id,
                user_id=actor_id,
                agent_id=agent_id,
            )
            if episode_id:
                _LAST_EPISODE_TURN_SNAPSHOT[key] = turn_count

    except Exception as e:
        logger.warning("Background memory persistence failed (non-fatal): %s", e)


def _load_persona(agent_id: str) -> str:
    """Load agent persona from markdown file."""
    filename = _PERSONA_MAP.get(agent_id, _PERSONA_MAP["ava"])
    path = _PERSONAS_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    logger.warning("Persona file not found for %s, using Ava fallback", agent_id)
    fallback = _PERSONAS_DIR / _PERSONA_MAP["ava"]
    return fallback.read_text(encoding="utf-8") if fallback.exists() else "You are Ava, a helpful business assistant."


def _build_aspire_awareness() -> str:
    """Load shared Aspire platform awareness context from config file."""
    global _AWARENESS_CACHE
    if _AWARENESS_CACHE is not None:
        return _AWARENESS_CACHE

    if _AWARENESS_FILE.exists():
        try:
            _AWARENESS_CACHE = _AWARENESS_FILE.read_text(encoding="utf-8")
            return _AWARENESS_CACHE
        except Exception:
            pass

    # Fallback if file not found
    _AWARENESS_CACHE = (
        "You are an AI agent on the Aspire platform — a governed execution platform "
        "for small business professionals."
    )
    return _AWARENESS_CACHE


def _build_user_context(state: OrchestratorState) -> str:
    """Format user/business profile for LLM context."""
    profile = state.get("user_profile")
    if not profile:
        return ""
    parts = []
    if profile.get("display_name"):
        parts.append(f"User: {profile['display_name']}")
    if profile.get("business_name"):
        parts.append(f"Business: {profile['business_name']}")
    if profile.get("industry"):
        parts.append(f"Industry: {profile['industry']}")
    return "\n".join(parts) if parts else ""


def _build_channel_context(state: OrchestratorState) -> str:
    """Voice vs chat response style guidance."""
    channel = "voice"  # Default
    profile = state.get("user_profile")
    if profile and isinstance(profile, dict):
        channel = profile.get("channel", "voice")

    if channel in ("voice", "avatar"):
        return (
            "RESPONSE STYLE: Keep your response to 1-3 sentences. "
            "Speak naturally and conversationally. No markdown, no bullet points. "
            "Optimized for text-to-speech."
        )
    return (
        "RESPONSE STYLE: You may be more detailed. Use clear structure "
        "when appropriate. You can use markdown formatting."
    )


def _guard_output(text: str, agent_id: str) -> str:
    """Strip phantom action claims from conversational responses.

    Agents should NEVER claim they've taken actions in conversation mode.
    They can suggest actions but not claim execution.
    """
    # Common phantom patterns to catch
    phantom_markers = [
        "I've sent", "I've created", "I've scheduled", "I've filed",
        "I've processed", "I've signed", "I've deleted", "I've transferred",
        "Invoice sent", "Payment processed", "Meeting scheduled",
    ]
    for marker in phantom_markers:
        if marker.lower() in text.lower():
            logger.warning(
                "Phantom action detected in %s response: '%s'. Stripping.",
                agent_id, marker,
            )
            text = (
                f"I can help with that, but I'd need to go through the proper "
                f"approval process first. Would you like me to set that up?"
            )
            break
    return text


async def _empty_list() -> list:
    """Async no-op returning empty list (for conditional asyncio.gather)."""
    return []


def _make_conversation_receipt(
    state: OrchestratorState,
    agent_id: str,
    intent_type: str,
    response_length: int,
) -> dict[str, Any]:
    """Generate receipt for conversation (Law #2)."""
    return {
        "id": str(uuid.uuid4()),
        "correlation_id": state.get("correlation_id", str(uuid.uuid4())),
        "action_type": "agent.conversation",
        "risk_tier": "green",
        "actor_id": state.get("actor_id", "unknown"),
        "suite_id": state.get("suite_id", "unknown"),
        "agent_id": agent_id,
        "intent_type": intent_type,
        "outcome": "success",
        "response_length": response_length,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def agent_reason_node(state: OrchestratorState) -> dict[str, Any]:
    """Conversational intelligence node — where agents think and respond.

    This node handles non-action intents: knowledge questions, advice,
    greetings, and general conversation. It assembles full context
    (persona + awareness + user profile + channel) and calls GPT-5
    for an intelligent response.

    Governance:
    - GREEN tier (read-only, no state change)
    - Generates receipt (Law #2)
    - No capability token needed
    - No approval flow needed
    """
    utterance = state.get("utterance", "")
    requested_agent = state.get("requested_agent")
    normalized_requested = (
        requested_agent.strip().lower()
        if isinstance(requested_agent, str) and requested_agent.strip()
        else None
    )
    raw_target = state.get("agent_target", "ava") or "ava"
    agent_id = raw_target.strip().lower() if isinstance(raw_target, str) else "ava"
    if normalized_requested and normalized_requested != "ava":
        agent_id = normalized_requested
    intent_type = state.get("intent_type", "conversation")

    logger.info(
        "agent_reason: agent=%s intent=%s utterance='%s'",
        agent_id, intent_type, utterance[:80],
    )

    # 1. Load agent persona
    persona = _load_persona(agent_id)

    # 2. Build context layers
    awareness = _build_aspire_awareness()
    user_ctx = _build_user_context(state)
    channel_ctx = _build_channel_context(state)

    # 3. Agentic RAG retrieval (cross-domain if needed)
    rag_context = ""
    try:
        from aspire_orchestrator.services.retrieval_router import get_retrieval_router
        router = get_retrieval_router()
        suite_id = state.get("suite_id")
        retrieval_result = await router.retrieve(
            query=utterance,
            agent_id=agent_id,
            suite_id=suite_id,
        )
        rag_context = retrieval_result.context
        if retrieval_result.receipt_id:
            existing_receipts = list(state.get("pipeline_receipts", []))
            # Retrieval receipt added to pipeline
    except Exception as e:
        logger.warning("RAG retrieval failed (non-fatal, continuing without): %s", e)

    # 4. Load memory layers in parallel (independent queries)
    memory_ctx = ""
    suite_id = state.get("suite_id") or "unknown"
    session_id = state.get("session_id") or ""
    actor_id = state.get("actor_id") or "unknown"
    try:
        from aspire_orchestrator.services.working_memory import get_working_memory
        from aspire_orchestrator.services.episodic_memory import get_episodic_memory
        from aspire_orchestrator.services.semantic_memory import get_semantic_memory

        wm = get_working_memory()
        em = get_episodic_memory()
        sm = get_semantic_memory()

        recent_turns, past_episodes, user_facts = await asyncio.gather(
            wm.get_recent_turns(session_id, suite_id, max_turns=10) if session_id else _empty_list(),
            em.search_relevant_episodes(utterance, suite_id, agent_id, max_episodes=3) if suite_id != "unknown" else _empty_list(),
            sm.get_user_facts(suite_id, actor_id, agent_id) if suite_id != "unknown" else _empty_list(),
        )

        # Format memory into context string
        mem_parts: list[str] = []

        if user_facts:
            mem_parts.append("## What I Know About You")
            for fact in user_facts[:10]:
                mem_parts.append(f"- {fact.fact_key}: {fact.fact_value}")

        if past_episodes:
            mem_parts.append("\n## Relevant Past Conversations")
            for ep in past_episodes:
                mem_parts.append(f"- [{ep.created_at[:10]}] {ep.summary}")

        if recent_turns:
            mem_parts.append("\n## Current Conversation")
            for turn in recent_turns[-6:]:
                role_label = "You" if turn.role == "agent" else "User"
                content_preview = turn.content[:200]
                mem_parts.append(f"{role_label}: {content_preview}")

        if mem_parts:
            memory_ctx = "\n".join(mem_parts)

    except Exception as e:
        logger.warning("Memory loading failed (non-fatal, continuing without): %s", e)

    # 5. Assemble system message
    system_parts = [awareness, "", persona]
    if user_ctx:
        system_parts.extend(["", "## User Context", user_ctx])
    if memory_ctx:
        system_parts.extend(["", memory_ctx])
    if rag_context:
        system_parts.extend(["", rag_context])
    system_parts.extend(["", channel_ctx])
    system_message = "\n".join(system_parts)

    # 6. Call LLM
    try:
        model = settings.ava_llm_model or "gpt-5-mini"
        _is_reasoning = model.startswith(("gpt-5", "o1", "o3"))

        # Reasoning models need "developer" role and no temperature
        msg_role = "developer" if _is_reasoning else "system"
        response_text = await generate_text_async(
            model=model,
            messages=[
                {"role": msg_role, "content": system_message},
                {"role": "user", "content": utterance},
            ],
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout_seconds=float(settings.openai_timeout_seconds),
            max_output_tokens=500,
            temperature=None if _is_reasoning else 0.7,
            prefer_responses_api=True,
        )

    except Exception as e:
        logger.error("agent_reason LLM call failed: %s", e)
        # Persona-specific fallback (NOT generic "I wasn't sure")
        fallback_map = {
            "finn": "Hey, I'm Finn — I hit a snag processing that. Can you try rephrasing?",
            "eli": "Sorry about that — I couldn't process your message. Could you try again?",
            "clara": "I ran into an issue with that request. Mind rephrasing?",
            "ava": "I apologize — I had trouble with that. Could you rephrase your question?",
        }
        response_text = fallback_map.get(agent_id, fallback_map["ava"])

    # 7. Guard output (strip phantom action claims)
    response_text = _guard_output(response_text, agent_id)

    # 8. Save turns to working memory (user utterance + agent response)
    if session_id:
        try:
            from aspire_orchestrator.services.working_memory import (
                ConversationTurn,
                get_working_memory,
            )

            wm_save = get_working_memory()
            await asyncio.gather(
                wm_save.add_turn(session_id, suite_id, ConversationTurn(
                    role="user", content=utterance, agent_id=agent_id,
                )),
                wm_save.add_turn(session_id, suite_id, ConversationTurn(
                    role="agent", content=response_text, agent_id=agent_id,
                )),
            )
        except Exception as e:
            logger.warning("Working memory save failed (non-fatal): %s", e)

        # Non-blocking long-term memory persistence (semantic + episodic).
        if _ENABLE_BACKGROUND_MEMORY_PERSISTENCE:
            try:
                asyncio.create_task(
                    _persist_memory_layers(
                        session_id=session_id,
                        suite_id=suite_id,
                        actor_id=actor_id,
                        agent_id=agent_id,
                    )
                )
            except Exception as e:
                logger.warning("Failed to schedule background memory persistence: %s", e)

    # 9. Generate receipt (Law #2)
    receipt = _make_conversation_receipt(state, agent_id, intent_type, len(response_text))

    # 10. Add to pipeline receipts
    existing_receipts = list(state.get("pipeline_receipts", []))
    existing_receipts.append(receipt)

    logger.info(
        "agent_reason complete: agent=%s response_len=%d",
        agent_id, len(response_text),
    )

    return {
        "conversation_response": response_text,
        "pipeline_receipts": existing_receipts,
    }
