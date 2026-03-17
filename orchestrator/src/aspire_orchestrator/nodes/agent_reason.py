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

from aspire_orchestrator.config.settings import resolve_openai_api_key, settings
from aspire_orchestrator.services.openai_client import generate_text_async
from aspire_orchestrator.services.agent_identity import (
    resolve_assigned_agent as _resolve_assigned_agent_shared,
)
from aspire_orchestrator.services.metrics import METRICS
from aspire_orchestrator.services.pack_policy_loader import get_prompt_contract
from aspire_orchestrator.services.response_quality_guard import enforce_response_quality
from aspire_orchestrator.services.retrieval_verifier import verify_retrieval_grounding
from aspire_orchestrator.services.task_queue import TaskQueueFullError, get_task_queue
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)

# 3c: Import canonical persona map from single source of truth
from aspire_orchestrator.services.agent_identity import AGENT_PERSONA_MAP as _PERSONA_MAP  # noqa: E402

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
        if turn_count >= 4 and (turn_count - last_snapshot) >= 4:
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


def _load_prompt_contract(agent_id: str) -> str:
    """Load runtime prompt contract for the agent if present."""
    try:
        return get_prompt_contract(agent_id).strip()
    except Exception as e:
        logger.warning("Failed to load prompt contract for %s: %s", agent_id, e)
        return ""


def _build_user_context(state: OrchestratorState) -> str:
    """Format user/business profile for LLM context."""
    profile = state.get("user_profile")
    if not profile:
        return ""
    parts = []
    # Owner name (gateway sends owner_name, fallback to display_name)
    name = profile.get("owner_name") or profile.get("display_name") or ""
    if name:
        # Extract last name for formal greeting (e.g., "Test Founder" → "Mr. Founder")
        name_parts = name.strip().split()
        last_name = name_parts[-1] if name_parts else name
        parts.append(f"User: {name} (address as Mr./Ms. {last_name} in greetings)")
    if profile.get("business_name"):
        parts.append(f"Business: {profile['business_name']}")
    if profile.get("industry"):
        parts.append(f"Industry: {profile['industry']}")
    if profile.get("team_size"):
        parts.append(f"Team size: {profile['team_size']}")
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
    lower = text.lower()
    if agent_id != "ava":
        if "i'm ava" in lower or "i am ava" in lower or "i’m ava" in lower:
            fallback_map = {
                "finn": "Hey, I'm Finn - your finance manager here in Aspire. What numbers should we dig into?",
                "eli": "Hey, I'm Eli - I run your inbox and messaging workflows. What do you want handled first?",
                "nora": "Hey, I'm Nora - I handle meetings and conference coordination. What should I schedule?",
                "clara": "Hey, I'm Clara - I handle legal and contract support. What do you need reviewed?",
                "quinn": "Hey, I'm Quinn - I manage invoices and payments operations. What should I prepare?",
                "sarah": "Hey, I'm Sarah - I handle front desk calls and routing. What do you want covered?",
                "adam": "Hey, I'm Adam - I run research and vendor analysis. What should I investigate?",
                "tec": "Hey, I'm Tec - I handle docs and filings workflows. What should I generate?",
                "teressa": "Hey, I'm Teressa - I handle bookkeeping and close tasks. What should I reconcile?",
                "milo": "Hey, I'm Milo - I handle payroll operations. What payroll task should I run?",
                "mail_ops": "Hey, I'm Mail Ops - I handle domain and mailbox operations. What do you need configured?",
            }
            logger.warning("Identity drift corrected: agent=%s output referenced Ava identity", agent_id)
            return fallback_map.get(agent_id, "I can help with that. Tell me what you need and I'll take it from here.")
    return text


def _is_identity_query(utterance: str) -> bool:
    """Detect direct identity/capability introductions."""
    import re
    # S3-L3: Normalize inner punctuation ("Who...are...you" → "who are you")
    normalized = re.sub(r"[^a-z0-9\s']", " ", utterance.lower().strip())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return False
    direct = {
        "who are you",
        "what is your name",
        "what's your name",
        "whats your name",
        "introduce yourself",
        "tell me about yourself",
        "what do you do",
        "how can you help",
        "what can you do",
    }
    if normalized in direct:
        return True
    substrings = ("who are you", "your name", "what do you do", "how can you help", "what can you do")
    return any(s in normalized for s in substrings)


def _identity_intro(agent_id: str) -> str:
    """Deterministic intros prevent model identity drift."""
    intros = {
        "ava": "I'm Ava, your chief of staff in Aspire. I coordinate your operations across calendar, inbox, finance, legal, and front desk workflows.",
        "finn": "I'm Finn, your finance manager in Aspire. I help with cash flow, tax strategy, and financial decisions so your numbers stay healthy.",
        "finn_fm": "I'm Finn, your finance manager in Aspire. I help with cash flow, tax strategy, and financial decisions so your numbers stay healthy.",
        "clara": "I'm Clara, your legal desk specialist in Aspire. I handle contracts, compliance checks, and signature workflows with governance controls.",
        "eli": "I'm Eli, your inbox and communications specialist in Aspire. I triage email, draft replies, and keep client communication moving.",
        "nora": "I'm Nora, your meetings specialist in Aspire. I handle scheduling, conference coordination, and follow-up summaries.",
        "quinn": "I'm Quinn, your invoicing specialist in Aspire. I manage invoices, collections flow, and payment operations.",
        "sarah": "I'm Sarah, your front desk specialist in Aspire. I manage call routing, intake coverage, and reception workflows.",
        "adam": "I'm Adam, your research specialist in Aspire. I investigate vendors, markets, and decisions with evidence-backed findings.",
        "tec": "I'm Tec, your documents specialist in Aspire. I handle document generation, filing workflows, and structured paperwork ops.",
        "teressa": "I'm Teressa, your bookkeeping specialist in Aspire. I handle reconciliations, books hygiene, and close-readiness.",
        "milo": "I'm Milo, your payroll specialist in Aspire. I handle payroll operations, timing, and employee pay workflows.",
        "mail_ops": "I'm Mail Ops, your domain and mailbox specialist in Aspire. I handle mailbox setup, routing, and domain mail operations.",
    }
    return intros.get(agent_id, "I'm your Aspire specialist assistant. Tell me what you need and I'll handle it.")


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
    agent_id = _resolve_assigned_agent_shared(state)
    intent_type = state.get("intent_type", "conversation")

    try:
        return await _agent_reason_inner(state, utterance, agent_id, intent_type)
    except Exception as exc:
        logger.error(
            "agent_reason CRASHED: agent=%s error=%s",
            agent_id, str(exc), exc_info=True,
        )
        # Emit FAILED receipt (Law #2 — failures get receipts too)
        error_receipt = {
            "id": str(uuid.uuid4()),
            "correlation_id": state.get("correlation_id", str(uuid.uuid4())),
            "action_type": "agent.conversation",
            "risk_tier": "green",
            "actor_id": state.get("actor_id", "unknown"),
            "suite_id": state.get("suite_id", "unknown"),
            "agent_id": agent_id,
            "intent_type": intent_type,
            "outcome": "failed",
            "error": type(exc).__name__,
            "error_message": str(exc)[:500],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(error_receipt)
        return {
            "conversation_response": "I'm sorry, I encountered an issue processing your request. Please try again.",
            "pipeline_receipts": existing_receipts,
            "agent_target": state.get("agent_target"),
            "error": True,
            "error_code": "AGENT_REASON_CRASH",
            "error_message": f"Agent reason failed: {type(exc).__name__}",
        }


async def _agent_reason_inner(
    state: OrchestratorState,
    utterance: str,
    agent_id: str,
    intent_type: str,
) -> dict[str, Any]:
    """Inner logic for agent_reason_node — separated for crash isolation."""

    logger.info(
        "agent_reason: agent=%s intent=%s utterance='%s'",
        agent_id, intent_type, utterance[:80],
    )

    if _is_identity_query(utterance):
        response_text = _identity_intro(agent_id)
        receipt = _make_conversation_receipt(state, agent_id, intent_type, len(response_text))
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(receipt)
        logger.info("agent_reason identity short-circuit: agent=%s", agent_id)
        return {
            "conversation_response": response_text,
            "pipeline_receipts": existing_receipts,
            "agent_target": state.get("agent_target"),
        }

    # 1. Load agent persona
    persona = _load_persona(agent_id)

    # 2. Build context layers
    awareness = _build_aspire_awareness()
    user_ctx = _build_user_context(state)
    channel_ctx = _build_channel_context(state)
    prompt_contract = _load_prompt_contract(agent_id)

    # 3. Agentic RAG retrieval (cross-domain if needed)
    #    Skip for greetings — RAG adds noise ("Here is the best grounded answer...")
    _skip_rag = intent_type in ("greeting", "__greeting__") or _is_identity_query(utterance)
    rag_context = ""
    retrieval_status = "not_applicable"
    retrieval_grounding_score = 0.0
    retrieval_conflicts: list[str] = []
    if _skip_rag:
        logger.debug("agent_reason: skipping RAG for intent_type=%s", intent_type)
    else:
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
            retrieval_status = retrieval_result.status
            retrieval_grounding_score = retrieval_result.grounding_score
            retrieval_conflicts = list(retrieval_result.conflict_flags or [])
            if retrieval_result.status in {"offline", "degraded"} and retrieval_result.degraded_reason:
                rag_context = (
                    f"{rag_context}\n\n## Retrieval Status\n"
                    f"Grounding status: {retrieval_result.status} ({retrieval_result.degraded_reason})"
                ).strip()
            if retrieval_result.receipt_id:
                existing_receipts = list(state.get("pipeline_receipts", []))
                existing_receipts.append({"retrieval_receipt_id": retrieval_result.receipt_id})
        except Exception as e:
            logger.warning("RAG retrieval failed (non-fatal, continuing without): %s", e)
            METRICS.record_retrieval_router(
                agent_id=agent_id, status="error", cache_hit=False, grounding_score=0.0,
            )

    # 4. Load memory layers in parallel (independent queries)
    memory_ctx = ""
    _conversation_history: list[dict[str, str]] = []
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

        # 3a: Convert working memory to proper message history (not flat text)
        # recent_turns are stored separately and injected as structured messages
        # in the LLM call below — NOT as system message text
        _conversation_history: list[dict[str, str]] = []
        if recent_turns:
            for turn in recent_turns[-6:]:
                msg_role_turn = "assistant" if turn.role == "agent" else "user"
                _conversation_history.append({"role": msg_role_turn, "content": turn.content[:500]})

        if mem_parts:
            memory_ctx = "\n".join(mem_parts)

    except Exception as e:
        logger.warning("Memory loading failed (non-fatal, continuing without): %s", e)

    # 5. Assemble system message
    identity_guard = (
        f"IDENTITY CONSTRAINT: You are {agent_id}. "
        f"Never claim to be Ava unless your agent id is exactly 'ava'. "
        "Do not self-identify as any other agent."
    )
    system_parts = [identity_guard, "", awareness, "", persona]
    if user_ctx:
        system_parts.extend(["", "## User Context", user_ctx])
    if memory_ctx:
        system_parts.extend(["", memory_ctx])
    if rag_context:
        system_parts.extend(["", rag_context])
    if prompt_contract:
        system_parts.extend(["", "## Runtime Prompt Contract", prompt_contract])
    system_parts.extend(["", channel_ctx])
    system_message = "\n".join(system_parts)

    # 6. Call LLM
    try:
        model = settings.ava_llm_model or "gpt-5-mini"
        _is_reasoning = model.startswith(("gpt-5", "o1", "o3"))

        # Reasoning models need "developer" role and no temperature
        msg_role = "developer" if _is_reasoning else "system"
        # 3a: Build messages with proper conversation history
        llm_messages: list[dict[str, str]] = [{"role": msg_role, "content": system_message}]
        if _conversation_history:
            llm_messages.extend(_conversation_history)
        llm_messages.append({"role": "user", "content": utterance})
        response_text = await generate_text_async(
            model=model,
            messages=llm_messages,
            api_key=resolve_openai_api_key(),
            base_url=settings.openai_base_url,
            timeout_seconds=float(settings.openai_timeout_seconds),
            max_output_tokens=500,
            temperature=None if _is_reasoning else 0.7,
            prefer_responses_api=True,
        )

    except Exception as e:
        logger.error("agent_reason LLM call failed: %s", e)
        # H3: Emit FAILED receipt for LLM exception (Law #2)
        llm_fail_receipt = {
            "id": str(uuid.uuid4()),
            "correlation_id": state.get("correlation_id", str(uuid.uuid4())),
            "action_type": "agent.conversation.llm_call",
            "risk_tier": "green",
            "actor_id": state.get("actor_id", "unknown"),
            "suite_id": state.get("suite_id", "unknown"),
            "agent_id": agent_id,
            "outcome": "failed",
            "error": type(e).__name__,
            "error_message": str(e)[:500],
            "quality_report": "DEGRADED_FALLBACK",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(llm_fail_receipt)
        # Persona-specific fallback (NOT generic "I wasn't sure")
        fallback_map = {
            "ava": "I apologize - I had trouble with that. Could you rephrase your question?",
            "finn": "Hey, I'm Finn - I hit a snag processing that. Can you try rephrasing?",
            "eli": "Hey, I'm Eli - I hit a snag processing that. Can you try that again?",
            "nora": "Hey, I'm Nora - I hit a snag with that request. Can you rephrase it?",
            "clara": "Hey, I'm Clara - I ran into an issue processing that. Can you rephrase?",
            "quinn": "Hey, I'm Quinn - I hit an issue with that request. Can you rephrase it?",
            "sarah": "Hey, I'm Sarah - I hit an issue processing that. Can you try again?",
            "adam": "Hey, I'm Adam - I hit an issue with that request. Can you rephrase it?",
            "tec": "Hey, I'm Tec - I ran into an issue with that request. Can you try again?",
            "teressa": "Hey, I'm Teressa - I hit a snag processing that. Can you rephrase?",
            "milo": "Hey, I'm Milo - I hit an issue with that request. Can you rephrase it?",
            "mail_ops": "Hey, I'm Mail Ops - I ran into an issue processing that. Can you rephrase?",
        }
        response_text = fallback_map.get(agent_id, fallback_map["ava"])

    # 7. Guard output (strip phantom action claims)
    response_text = _guard_output(response_text, agent_id)
    grounding_report = verify_retrieval_grounding(
        intent_type=intent_type,
        retrieval_status=retrieval_status,
        grounding_score=retrieval_grounding_score,
        agent_id=agent_id,
        conflict_flags=retrieval_conflicts,
    )
    if not grounding_report.passed:
        response_text = grounding_report.fallback_text
    channel = "voice"
    profile = state.get("user_profile")
    if isinstance(profile, dict):
        channel = profile.get("channel", "voice")

    fallback_map = {
        "ava": "I can give you a safe first pass now. Which exact detail should I verify first?",
        "finn": "I can give you a conservative finance read now. Which number or timeframe should I verify first?",
        "eli": "I can draft a safe first pass now. Which exact thread or message should I verify first?",
        "nora": "I can give you a safe scheduling direction now. Which meeting detail should I verify first?",
        "clara": "I can give a cautious legal read now. Which clause should I verify first?",
        "quinn": "I can give you a safe invoicing direction now. Which invoice detail should I verify first?",
        "sarah": "I can give you a safe front-desk direction now. Which caller or case should I verify first?",
        "adam": "I can give you an evidence-first start now. Which source or claim should I verify first?",
        "tec": "I can give you a safe document first pass now. Which field should I verify first?",
        "teressa": "I can give you a cautious books read now. Which line item should I verify first?",
        "milo": "I can give you a cautious payroll read now. Which pay period should I verify first?",
        "mail_ops": "I can give you a safe mail-ops direction now. Which mailbox or domain detail should I verify first?",
    }
    response_text, quality_report = enforce_response_quality(
        text=response_text,
        channel=channel,
        agent_id=agent_id,
        prompt_contract=prompt_contract,
        fallback_text=fallback_map.get(agent_id, fallback_map["ava"]),
        allow_markdown=channel not in {"voice", "avatar"},
    )
    METRICS.record_response_quality(
        agent_id=agent_id,
        channel=channel,
        score=quality_report.score,
        passed=quality_report.passed,
    )

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
                queue = get_task_queue()
                await queue.enqueue(
                    _persist_memory_layers,
                    session_id=session_id,
                    suite_id=suite_id,
                    actor_id=actor_id,
                    agent_id=agent_id,
                )
            except TaskQueueFullError:
                logger.warning("Background memory persistence skipped: task queue at capacity")
            except Exception as e:
                logger.warning("Failed to schedule background memory persistence: %s", e)

    # 9. Generate receipt (Law #2)
    receipt = _make_conversation_receipt(state, agent_id, intent_type, len(response_text))
    receipt["quality_report"] = {
        "score": quality_report.score,
        "passed": quality_report.passed,
        "violations": quality_report.violations,
        "warnings": quality_report.warnings,
        "style_signals": quality_report.style_signals,
    }
    receipt["retrieval_verification"] = {
        "passed": grounding_report.passed,
        "mode": grounding_report.mode,
        "confidence": grounding_report.confidence,
        "reasons": grounding_report.reasons,
        "retrieval_status": retrieval_status,
        "grounding_score": retrieval_grounding_score,
    }

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
        "agent_target": state.get("agent_target"),
    }
