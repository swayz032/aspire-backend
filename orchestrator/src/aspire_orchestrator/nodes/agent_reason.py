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
from aspire_orchestrator.services.openai_client import generate_text_async, generate_text_streaming_async
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


def _resolve_channel(state: OrchestratorState) -> str:
    """Resolve interaction channel from state with 3-location priority.

    Priority:
      1. state["channel"]           — top-level (set by intake)
      2. state["request"].payload.channel — where desktop puts it
      3. state["user_profile"]["channel"] — original location (usually null)
      4. Default: "chat" (NOT "voice" — safer default)

    Normalizes "text" → "chat" for consistency.
    """
    # Priority 1: top-level state (set by intake node)
    channel = state.get("channel")

    # Priority 2: request payload
    if not channel:
        req = state.get("request")
        if req is not None:
            payload = req.get("payload", {}) if isinstance(req, dict) else getattr(req, "payload", {})
            if isinstance(payload, dict):
                channel = payload.get("channel")

    # Priority 3: user_profile
    if not channel:
        profile = state.get("user_profile")
        if profile and isinstance(profile, dict):
            channel = profile.get("channel")

    # Default + normalize + allowlist (THREAT-002)
    channel = channel or "chat"
    if isinstance(channel, str):
        channel = channel.strip().lower()
    else:
        channel = "chat"
    if channel == "text":
        channel = "chat"
    _ALLOWED_CHANNELS = {"chat", "voice", "avatar"}
    if channel not in _ALLOWED_CHANNELS:
        channel = "chat"
    return channel


def _build_channel_context(state: OrchestratorState) -> str:
    """Channel-aware response style: voice (audio-only), avatar (Anam video), chat (text)."""
    channel = _resolve_channel(state)

    # Shared TTS rules for spoken channels (voice + avatar)
    _tts_base = (
        "RESPONSE STYLE: Keep your response to one to three sentences. "
        "Speak naturally like a trusted professional. "
        "Use brief fillers like 'Got it', 'Let me check', 'Here is what I see'. "
        "Write out numbers and symbols for TTS: 'twenty dollars' not '$20'. "
        "No markdown, no bullet points, no special characters. "
    )

    if channel in ("avatar", "voice"):
        suffix = "Optimized for text-to-speech via Anam avatar." if channel == "avatar" else "Optimized for text-to-speech delivery."
        return _tts_base + suffix

    # Chat — structured text formatting allowed, warm conversational tone
    return (
        "RESPONSE STYLE: You are chatting with the user in a text interface. "
        "Be warm and conversational but substantive. "
        "For complex topics, use 3-6 sentences. Light formatting (bold for emphasis, "
        "short lists for multi-part answers) is welcome. "
        "Don't force brevity — give the user real value."
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
        "ava_admin": "I'm Ava Admin, your ops commander in Aspire. I monitor platform health, triage incidents, track workflows, audit receipts, and coordinate the council when needed.",
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

        # 3b: If no working memory turns, fall back to payload.history
        # (admin portal sends explicit history since it lacks persistent sessions)
        if not _conversation_history:
            _payload_history = []
            _req = state.get("request")
            if isinstance(_req, dict):
                _payload_history = _req.get("payload", {}).get("history", [])
            elif hasattr(_req, "payload") and isinstance(getattr(_req, "payload", None), dict):
                _payload_history = _req.payload.get("history", [])
            if isinstance(_payload_history, list):
                for _h in _payload_history[-6:]:
                    if isinstance(_h, dict) and _h.get("role") in ("user", "assistant") and isinstance(_h.get("content"), str):
                        _conversation_history.append({"role": _h["role"], "content": _h["content"][:500]})

        if mem_parts:
            memory_ctx = "\n".join(mem_parts)

    except Exception as e:
        logger.warning("Memory loading failed (non-fatal, continuing without): %s", e)

    # 4b. Finn research delegation hint (RC5)
    if agent_id in ("finn", "finn_fm") and retrieval_status in ("no_results", "offline", "degraded"):
        _research_hint = (
            "\n## Research Delegation\n"
            "If this question is outside your financial expertise, tell the user you'll "
            "ask Adam (your research specialist) to look into it. Suggest specific "
            "angles Adam could research. Don't give generic advice — either give "
            "domain-specific financial insight or delegate to Adam."
        )
        rag_context = (rag_context + _research_hint) if rag_context else _research_hint

    # 4c. If routed here after param_extract failure, inject missing fields context
    #     so the agent knows what information to ask for using its persona + RAG.
    param_extract_ctx = ""
    if state.get("error_code") == "PARAM_EXTRACTION_FAILED":
        task_type = state.get("task_type", "")
        error_msg = state.get("error_message", "")
        missing_fields = state.get("missing_fields", [])
        field_list = ", ".join(missing_fields) if missing_fields else ""
        param_extract_ctx = (
            "## Action Context — Missing Information\n"
            f"The user wants to perform: **{task_type}**\n"
        )
        if field_list:
            param_extract_ctx += f"Missing required fields: {field_list}\n"
        if error_msg:
            param_extract_ctx += f"Details: {error_msg}\n"
        param_extract_ctx += (
            "\nYour job: Ask the user for the missing information naturally, "
            "using your domain expertise and personality. Don't list fields "
            "mechanically — have a real conversation. Use your knowledge "
            "(including any RAG context below) to ask smart follow-up questions "
            "and suggest defaults where appropriate. If this involves a new "
            "client, suggest onboarding them first."
        )
        # Override intent_type so RAG retrieval uses the action context
        if intent_type in ("action", "conversation"):
            intent_type = "advice"  # triggers more tokens + RAG retrieval

    # 5. Assemble system message
    identity_guard = (
        f"IDENTITY CONSTRAINT: You are {agent_id}. "
        f"Never claim to be Ava unless your agent id is exactly 'ava'. "
        "Do not self-identify as any other agent."
    )

    # Temporal context (RC1) — agents need to know current date/time
    now = datetime.now(timezone.utc)
    temporal_ctx = (
        f"## Current Date & Time\n"
        f"Today is {now.strftime('%A, %B %d, %Y')}. "
        f"Current time is {now.strftime('%I:%M %p')} UTC. "
        f"Current quarter: Q{(now.month - 1) // 3 + 1} {now.year}."
    )

    # Prompt caching optimization: stable prefix FIRST, dynamic content LAST.
    # OpenAI caches exact token prefixes (>1024 tokens). identity_guard + awareness +
    # persona + prompt_contract + channel_ctx are identical between requests for the
    # same agent — these form the cacheable prefix (~1500-3000 tokens).
    # Dynamic content (temporal, user profile, memory, RAG) goes after so it doesn't
    # invalidate the cached prefix.
    system_parts = [identity_guard, "", awareness, "", persona]
    if prompt_contract:
        system_parts.extend(["", "## Runtime Prompt Contract", prompt_contract])
    system_parts.extend(["", channel_ctx])
    # --- Dynamic content below (changes per request — after cacheable prefix) ---
    system_parts.extend(["", temporal_ctx])
    if user_ctx:
        system_parts.extend(["", "## User Context", user_ctx])
    if param_extract_ctx:
        system_parts.extend(["", param_extract_ctx])
    if memory_ctx:
        system_parts.extend(["", memory_ctx])
    if rag_context:
        system_parts.extend(["", rag_context])
    system_message = "\n".join(system_parts)

    # 6. Call LLM
    try:
        # Voice channel: use non-reasoning model (GPT-4o, ~440ms TTFT) for real-time.
        # Chat/other: use reasoning model (GPT-5-mini, better quality for text).
        # Reasoning models (GPT-5*) have 8-97s TTFT due to internal thinking tokens —
        # architecturally wrong for voice where sub-500ms TTFT is needed.
        _channel_for_stream = _resolve_channel(state)
        _is_voice = _channel_for_stream in ("voice", "avatar")
        if _is_voice and settings.ava_voice_model:
            model = settings.ava_voice_model  # Default: gpt-4o (non-reasoning)
        else:
            model = settings.ava_llm_model or "gpt-5-mini"
        _is_reasoning = model.startswith(("gpt-5", "o1", "o3"))

        # Reasoning models need "developer" role and no temperature
        msg_role = "developer" if _is_reasoning else "system"
        # 3a: Build messages with proper conversation history
        llm_messages: list[dict[str, str]] = [{"role": msg_role, "content": system_message}]
        if _conversation_history:
            llm_messages.extend(_conversation_history)
        llm_messages.append({"role": "user", "content": utterance})
        # RC4: Advisory questions get more tokens for substantive answers
        _max_tokens = 1200 if intent_type in ("knowledge", "advice") else 500

        # OpenAI prompt cache key: same agent = same persona prefix = same server.
        # ~15 RPM per key is optimal. agent_id groups requests with identical prefixes.
        _cache_key = f"aspire-reason-{agent_id}"

        # Voice/avatar channels: stream tokens as SSE deltas for progressive TTS.
        # Chat channel: standard non-streaming call (no latency benefit from streaming).
        if _is_voice:
            from aspire_orchestrator.skillpacks.adam_research import get_activity_event_callback
            _stream_cb = get_activity_event_callback()

            def _on_token(delta: str) -> None:
                if _stream_cb and delta:
                    _stream_cb({
                        "type": "delta",
                        "content": delta,
                        "agent": agent_id,
                        "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                    })

            response_text = await generate_text_streaming_async(
                model=model,
                messages=llm_messages,
                api_key=resolve_openai_api_key(),
                base_url=settings.openai_base_url,
                timeout_seconds=float(settings.openai_timeout_seconds),
                max_output_tokens=_max_tokens,
                temperature=0.7,  # GPT-4o (non-reasoning) uses temperature
                on_token=_on_token,
                prompt_cache_key=_cache_key,
                prompt_cache_retention="24h",
            )
        else:
            response_text = await generate_text_async(
                model=model,
                messages=llm_messages,
                api_key=resolve_openai_api_key(),
                base_url=settings.openai_base_url,
                timeout_seconds=float(settings.openai_timeout_seconds),
                max_output_tokens=_max_tokens,
                temperature=None if _is_reasoning else 0.7,
                prefer_responses_api=True,
                prompt_cache_key=_cache_key,
                prompt_cache_retention="24h",
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
            "ava": "I hit a snag on my end — let me try that differently. Can you say that again or give me a bit more context?",
            "ava_admin": "I hit a snag pulling that data. Can you say that again? I'll re-run the check.",
            "finn": "I hit a bump pulling that together — can you give me a bit more context? I want to make sure I get the numbers right.",
            "eli": "I stumbled on that one — can you rephrase it? I want to make sure I handle the message correctly.",
            "nora": "I hit a snag setting that up — can you give me a bit more detail so I can get it right?",
            "clara": "I ran into an issue processing that — can you give me a bit more context? I want to make sure we handle this properly.",
            "quinn": "I hit a bump on that — can you rephrase it? I want to make sure the invoice details are accurate.",
            "sarah": "I hit a snag with that — can you say that again? I want to make sure I route this correctly.",
            "adam": "I stumbled on that one — can you give me a bit more context so I can research this properly?",
            "tec": "I ran into an issue with that — can you rephrase it? I want to make sure the document comes out right.",
            "teressa": "I hit a bump on that — can you give me a bit more detail? I want to make sure the books are accurate.",
            "milo": "I hit a snag processing that — can you rephrase it so I can handle the payroll correctly?",
            "mail_ops": "I ran into an issue with that — can you give me more detail so I can sort out the mail setup?",
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
    channel = _resolve_channel(state)

    fallback_map = {
        "ava": "I can give you a safe first pass now. Which exact detail should I verify first?",
        "ava_admin": "I can give you an initial read now. Which system or metric should I check first?",
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
