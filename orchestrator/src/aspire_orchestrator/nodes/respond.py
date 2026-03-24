"""Respond Node — AvaResult construction, LLM summarization, and egress validation.

Responsibilities:
1. Persist any unpersisted pipeline receipts (Law #2 safety net)
2. Generate human-readable response via LLM with agent persona (Summary step)
3. Construct AvaResult from pipeline state
4. Validate AvaResult schema before returning (egress validation)
5. Handle error cases (return AspireError instead of AvaResult)
6. Include all receipt_ids in governance metadata

Pipeline position:
  Intake → Safety → Classify → Route → Policy → Approval → TokenMint →
  Execute → ReceiptWrite → QA → **Respond (Summary)** → Client

The Summary step is where the LLM generates a natural language response
using the routed agent's persona. This is what makes Ava sound like Ava
and Finn sound like Finn. Template responses are the fallback when the
LLM is unavailable (Law #3: fail closed, but gracefully).

Law #2 Safety Net:
  Denied/blocked flows skip receipt_write_node. The respond node is the
  terminal node ALL graph paths pass through, so it ensures receipts are
  ALWAYS persisted — even for denied, blocked, or approval-pending flows.
  Receipts without a receipt_hash have not been through receipt_write.

This is the final node in the pipeline — its output becomes the HTTP response.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from pydantic import ValidationError

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.models import (
    AvaResult,
    AvaResultGovernance,
    AvaResultRisk,
    Outcome,
    RiskTier,
)
from aspire_orchestrator.services.narration import compose_narration
from aspire_orchestrator.services.openai_client import generate_text_sync
from aspire_orchestrator.services.output_guard import guard_output
from aspire_orchestrator.services.agent_identity import (
    resolve_assigned_agent as _resolve_assigned_agent_shared,
    resolve_persona_agent as _resolve_persona_agent_shared,
)
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)

def _resolve_agent_id(state: OrchestratorState) -> str:
    """Resolve persona id from canonical shared agent identity logic."""
    return _resolve_persona_agent_shared(state)


def _resolve_assigned_agent(state: OrchestratorState) -> str:
    """Resolve user-facing assigned agent with shared canonical precedence."""
    return _resolve_assigned_agent_shared(state)


def _persist_unpersisted_receipts(state: OrchestratorState) -> list[str]:
    """Persist any pipeline receipts that were not processed by receipt_write.

    Law #2 safety net: denied/blocked flows skip receipt_write_node, so
    pipeline_receipts accumulate but are never chain-hashed or stored.
    This function detects unpersisted receipts (missing receipt_hash),
    assigns chain metadata, and stores them.

    Returns the list of receipt IDs that were persisted.
    """
    pipeline_receipts = list(state.get("pipeline_receipts", []))
    if not pipeline_receipts:
        return []

    # Check if receipts already have real chain hashes (receipt_write handled them).
    # Nodes set receipt_hash="" as a placeholder; receipt_write sets a real SHA-256 hash.
    unpersisted = [r for r in pipeline_receipts if not r.get("receipt_hash")]
    if not unpersisted:
        return []

    suite_id = state.get("suite_id", "unknown")

    try:
        from aspire_orchestrator.services.receipt_chain import assign_chain_metadata
        from aspire_orchestrator.services.receipt_store import store_receipts

        assign_chain_metadata(unpersisted, chain_id=suite_id)
        store_receipts(unpersisted)

        persisted_ids = [r["id"] for r in unpersisted if "id" in r]
        logger.info(
            "Law #2 safety net: persisted %d unpersisted receipts for suite=%s",
            len(persisted_ids), suite_id,
        )
        return persisted_ids

    except Exception as e:
        # Fail closed — log but don't crash the response
        logger.error("Law #2 safety net failed to persist receipts: %s", e)
        return []


def _generate_response_text(state: OrchestratorState) -> str:
    """Generate human-readable response text for voice/chat output.

    Maps task_type + outcome to a natural language response that Ava
    speaks to the user via ElevenLabs TTS or displays in chat.
    Prioritizes agent-provided messages if available.
    """
    task_type = state.get("task_type", "unknown")
    outcome = state.get("outcome", Outcome.SUCCESS)
    outcome_val = outcome.value if hasattr(outcome, "value") else str(outcome)
    execution_result = state.get("execution_result") or {}
    utterance = state.get("utterance", "")
    tool_used = state.get("tool_used", "unknown")
    is_stub = execution_result.get("stub", False)

    # 1. Prefer agent-generated error/status messages if present (Premium Human Logic)
    # The agent skill packs now generate warm, personalized errors.
    agent_message = execution_result.get("error_message") or execution_result.get("error") or execution_result.get("message")
    if agent_message and isinstance(agent_message, str) and len(agent_message) > 5:
        # If it looks like a real sentence, use it.
        return agent_message

    # Skill pack prefix → human-readable domain
    domain_prefix = task_type.split(".")[0] if "." in task_type else task_type
    action_suffix = task_type.split(".", 1)[1] if "." in task_type else ""

    # If tool is a stub (provider not connected), tell the user
    if is_stub and outcome_val == "success":
        provider_names = {
            "stripe": "Stripe",
            "pandadoc": "PandaDoc",
            "brave": "Brave Search",
            "tavily": "Tavily",
            "livekit": "LiveKit",
            "deepgram": "Deepgram",
            "twilio": "Twilio",
            "plaid": "Plaid",
            "gusto": "Gusto",
            "qbo": "QuickBooks",
            "puppeteer": "document generator",
            "s3": "file storage",
            "polaris": "email service",
            "elevenlabs": "ElevenLabs",
        }
        provider_key = tool_used.split(".")[0] if "." in tool_used else tool_used
        provider_name = provider_names.get(provider_key, provider_key)
        return (
            f"I see what you need, but {provider_name} isn't connected yet. "
            f"If you head to the connections page and set it up, I can handle this for you immediately."
        )

    # Outbox-submitted (RED tier async)
    if execution_result.get("status") == "outbox_submitted":
        _action_descriptions = {
            "payment": "Your payment",
            "transfer": "Your transfer",
            "payroll": "The payroll run",
            "contract": "Your contract",
            "filing": "Your filing",
        }
        desc = _action_descriptions.get(domain_prefix, "Your request")
        return f"{desc} has been securely queued. I'll let you know as soon as it's finished."

    # Success with live execution
    if outcome_val == "success":
        _success_responses: dict[str, str] = {
            "research": "I've gathered some results for you. Take a look at your activity feed for the details.",
            "invoice": "Your invoice is ready and has been created successfully.",
            "contract": "I've drafted that contract for you. It's ready for review.",
            "calendar": "All set — your calendar is updated.",
            "email": "I've taken care of that email.",
            "office": "Your office message is drafted and ready.",
            "meeting": "Your meeting room is open. You can join whenever you're ready.",
            "conference": "Your conference room is live and set up.",
            "finance": "I've pulled those financial numbers for you.",
            "payment": "Payment processed successfully.",
            "document": "Your document is ready.",
            "contact": "Contact details updated.",
            "booking": "Booking confirmed.",
            "scheduling": "Schedule updated.",
        }
        base = _success_responses.get(domain_prefix, "All set — I've completed that request for you.")

        # Enrich with action detail when available
        if domain_prefix == "research" and utterance:
            # Trim to first 60 chars of utterance for voice brevity
            topic = utterance[:60].rstrip()
            if len(utterance) > 60:
                topic += "..."
            base = f"I've looked into \"{topic}\" for you. Check your feed for the full report."
        elif domain_prefix == "invoice" and action_suffix == "send":
            base = "I've sent that invoice off to the client."
        elif domain_prefix == "invoice" and action_suffix == "create":
            base = "I've created the invoice. It's waiting in your drafts folder."
        elif domain_prefix == "contract" and action_suffix == "generate":
            base = "I've drafted the contract. Please give it a quick review."
        elif domain_prefix == "email" and action_suffix in ("send", "draft"):
            base = "I've prepared that email draft for you."
        elif domain_prefix == "office" and action_suffix in ("send", "draft", "create"):
            base = "I've drafted the message. It's ready for your review."

        return base

    # Failed execution - Premium Fallback
    if outcome_val == "failed":
        import random
        _fail_responses = [
            "I ran into a slight hiccup processing that. Could we try again?",
            "Something interrupted me while I was working on that. Do you mind trying once more?",
            "I hit a snag with that request. It might be a connection issue — want to retry?",
        ]
        return random.choice(_fail_responses)

    # Denied by policy - Premium Fallback
    if outcome_val == "denied":
        return "I can't go ahead with that — it conflicts with your current security settings."

    # Fallback
    return "I've processed your request. Let me know if you need anything else."


_FORMAT_STRIP_PHRASES = [
    "return json only",
    "output json only",
    "json only",
    "matching the shared output schema",
]


def _strip_format_instructions(persona_text: str) -> str:
    """Remove JSON/structured format instructions from persona for voice/chat.

    Persona files may contain 'Return JSON only' or similar instructions that
    force the LLM to output structured data instead of natural language. These
    must be stripped when generating conversational responses for voice/chat.
    """
    lines = persona_text.split("\n")
    filtered: list[str] = []
    skip_section = False
    for line in lines:
        lower = line.lower().strip()
        if any(p in lower for p in _FORMAT_STRIP_PHRASES):
            skip_section = lower.startswith("##")
            continue
        if skip_section:
            if line.strip().startswith("##"):
                skip_section = False
            else:
                continue
        filtered.append(line)
    return "\n".join(filtered)


def _llm_summarize(state: OrchestratorState, fallback_text: str, channel: str = "chat") -> str:
    """Generate persona-aware response text via LLM (Summary step).

    Calls the LLM with the routed agent's persona to produce a natural
    response. Falls back to template text if the LLM is unavailable.

    This is a sync wrapper — the respond_node is called from a sync
    LangGraph context, so we run the async LLM call in a thread.
    """
    utterance = state.get("utterance", "")
    if not utterance:
        return fallback_text

    task_type = state.get("task_type", "unknown")
    outcome = state.get("outcome", Outcome.SUCCESS)
    outcome_val = outcome.value if hasattr(outcome, "value") else str(outcome)
    execution_result = state.get("execution_result") or {}
    tool_used = state.get("tool_used", "unknown")

    # Keep deterministic phrasing for stub/provider-unavailable executions.
    if execution_result.get("stub"):
        return fallback_text

    # Guard against empty/stub execution producing garbage responses
    is_empty_execution = (
        task_type in ("unknown", "")
        and tool_used in ("unknown", "")
        and (not execution_result or execution_result == {} or execution_result.get("stub"))
    )
    if is_empty_execution:
        return _llm_conversational_reply(state, utterance, channel=channel)

    # Load the routed agent's persona for response generation
    agent_id = _resolve_agent_id(state)
    persona = _load_agent_persona(agent_id)
    if persona:
        persona = _strip_format_instructions(persona)

    # Formatting instructions based on Agent and Channel (3-path: voice, backend-chat, frontend-chat)
    agent_id = _resolve_agent_id(state)
    is_backend_ops = agent_id in ("ava_admin", "sre", "security", "release", "qa")

    if channel in ("voice", "avatar"):
        # TTS constraints for ALL agents on voice/avatar channels
        format_instruction = (
            "- NO markdown, NO bullet points, NO bold/italic, NO special symbols ($)\n"
            "- Write out numbers and symbols (e.g., 'twenty dollars' instead of '$20')\n"
            "- Speak naturally with brief fillers ('Sure thing', 'I see')\n"
            "- Sounds natural when spoken aloud (optimized for text-to-speech)"
        )
    elif is_backend_ops:
        # Backend Ops on chat gets data-rich structured text
        format_instruction = (
            "- Use Markdown and bullet points to structure data and lists clearly\n"
            "- Keep the tone natural but data-rich\n"
            "- Address the user as the Founder when appropriate"
        )
    else:
        # Frontend agents on chat — warm conversational with light formatting
        format_instruction = (
            "- Warm, professional tone — like a trusted colleague\n"
            "- Light formatting OK: bold for emphasis, short lists for multi-part answers\n"
            "- Be substantive — don't force brevity when the topic warrants detail\n"
            "- Use dollar signs and numbers normally ($20, 15%, etc.)"
        )

    # Build the summarization prompt
    prompt = (
        f"The user said: \"{utterance}\"\n\n"
        f"Action taken: {task_type}\n"
        f"Tool used: {tool_used}\n"
        f"Outcome: {outcome_val}\n"
        f"Execution details: {json.dumps(execution_result, default=str)}\n\n"
        "Generate a brief, natural response (1-3 sentences max) that:\n"
        "- Directly addresses what the user asked\n"
        "- Confirms what was done or explains what happened\n"
        f"{format_instruction}\n"
        "Respond with ONLY the text, nothing else."
    )

    try:
        # Use LLM Router to get natural_chat profile (StepType.CHAT)
        from aspire_orchestrator.services.llm_router import get_llm_router, StepType
        router = get_llm_router()
        route = router.route(StepType.CHAT, state.get("risk_tier", "green"), desk=agent_id)

        messages = []
        if persona:
            messages.append({"role": "system", "content": persona})
        messages.append({"role": "user", "content": prompt})

        # Use the routed model and temperature (0.7)
        content = _call_openai_sync(
            messages,
            model=route.concrete_model,
            channel=channel,
            temperature=route.temperature
        )

        if content:
            logger.info("LLM summarization success for %s (len=%d)", agent_id, len(content))
            return content

        return fallback_text

    except Exception as e:
        # Law #3: Fail gracefully — use template response if LLM fails
        logger.warning("LLM summarization failed for %s: %s — using template", agent_id, e)
        return fallback_text


def _llm_conversational_reply(state: OrchestratorState, utterance: str, channel: str = "chat") -> str:
    """Generate a natural conversational reply for non-action input."""
    agent_id = _resolve_agent_id(state)

    normalized = utterance.lower().strip().rstrip("!?.,")
    _IDENTITY_SUBSTRINGS = ("who are you", "your name", "what do you do", "how can you help", "what can you do")
    _IDENTITY_EXACT = {
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
    is_identity_query = normalized in _IDENTITY_EXACT or any(sub in normalized for sub in _IDENTITY_SUBSTRINGS)
    if is_identity_query:
        intros = {
            "ava": "I'm Ava, your chief of staff in Aspire. I coordinate your operations across calendar, inbox, finance, legal, and front desk workflows.",
            "finn_fm": "I'm Finn, your finance manager in Aspire. I help with cash flow, tax strategy, and financial decisions so your numbers stay healthy.",
            "finn": "I'm Finn, your finance manager in Aspire. I help with cash flow, tax strategy, and financial decisions so your numbers stay healthy.",
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

    persona = _load_agent_persona(agent_id)
    if persona:
        persona = _strip_format_instructions(persona)

    _DISPLAY_NAMES = {
        "finn_fm": "Finn", "ava": "Ava", "eli": "Eli",
        "nora": "Nora", "sarah": "Sarah", "adam": "Adam",
        "quinn": "Quinn", "tec": "Tec", "teressa": "Teressa",
        "milo": "Milo", "clara": "Clara",
    }
    agent_name = _DISPLAY_NAMES.get(agent_id, "Ava")

    # Build user context if available
    user_ctx = ""
    user_profile = state.get("user_profile")
    if user_profile:
        name = user_profile.get("owner_name") or user_profile.get("display_name") or ""
        biz = user_profile.get("business_name") or ""
        if name:
            parts = name.strip().split()
            last = parts[-1] if parts else name
            user_ctx = f"\nYou are speaking to Mr./Ms. {last}"
            if biz:
                user_ctx += f" of {biz}"
            user_ctx += ". Address them formally.\n"

    # Load the shared awareness file (team roster, platform context)
    from aspire_orchestrator.nodes.agent_reason import _build_aspire_awareness
    awareness = _build_aspire_awareness()

    # Temporal context (RC1) — agents need current date/time
    from datetime import datetime as _dt, timezone as _tz
    _now = _dt.now(_tz.utc)
    temporal_ctx = (
        f"Today is {_now.strftime('%A, %B %d, %Y')}. "
        f"Current time: {_now.strftime('%I:%M %p')} UTC. "
        f"Quarter: Q{(_now.month - 1) // 3 + 1} {_now.year}."
    )

    # Channel-aware formatting (RC3)
    if channel in ("voice", "avatar"):
        style_instruction = (
            "Keep it brief (1-3 sentences), warm, and natural. "
            "Do NOT use markdown or bullet points. This will be spoken aloud via TTS.\n"
        )
    else:
        style_instruction = (
            "Be warm, conversational, and substantive. "
            "For complex topics, use 3-6 sentences. Light formatting (bold, short lists) is OK. "
            "Don't force brevity — give real value.\n"
        )

    prompt = (
        f'The user said: "{utterance}"\n\n'
        f"You are {agent_name}, speaking to the user directly.\n"
        f"{temporal_ctx}\n"
        "This is a conversational message, not an action request. "
        "Respond in character using your persona's personality and expertise. "
        "If the user asks who you are, introduce yourself warmly with your name, "
        "role, and what you can help them with.\n"
        f"{awareness}\n"
        f"{user_ctx}"
        f"{style_instruction}"
        "Respond with ONLY the text."
    )

    try:
        messages: list[dict[str, str]] = []
        if persona:
            messages.append({"role": "system", "content": persona})
        messages.append({"role": "user", "content": prompt})
        # generate_text_sync already iterates ASPIRE_MODEL_FALLBACK_MAP chain
        # (gpt-5 → gpt-5-mini) via _candidate_models(), so no manual retry needed.
        # Avatar/voice channels use shorter timeout — filler talk buys time but
        # user experience degrades fast. Default 120s is for reasoning models.
        # All channels use fast timeout — 20s voice/avatar, 30s text/chat.
        # Users won't wait 90s staring at a chat bubble.
        avatar_timeout = 20.0 if channel in ("avatar", "voice") else min(30.0, settings.openai_timeout_seconds)
        content = _call_openai_sync(
            messages, model=settings.router_model_general, channel=channel, timeout=avatar_timeout,
        )
        if content:
            return content
    except Exception as e:
        logger.warning("Conversational LLM failed (all fallback models exhausted): %s", e)

    # Deterministic fallback
    return "Hey! I'm here to help. What would you like me to work on today?"


def _call_openai_sync(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    timeout: float = settings.openai_timeout_seconds,
    channel: str = "chat",
    temperature: float | None = None,
) -> str:
    """Shared sync OpenAI SDK call for respond node LLM operations.

    Handles reasoning model logic (developer role, no temperature, 4096 min tokens).
    Injects channel-based verbosity instruction for GPT-5 models (the verbosity
    API parameter is Responses-API-only; Chat Completions uses system instructions).
    Returns content string or empty string on failure.
    """
    api_key = os.environ.get("ASPIRE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return ""

    if model is None:
        model = settings.router_model_classifier

    _is_reasoning = model.startswith(("gpt-5", "o1", "o3"))

    # Channel-based verbosity instruction (GPT-5 prompting guide pattern).
    # Voice needs concise output for TTS; chat/video get moderate detail.
    _VERBOSITY_INSTRUCTIONS: dict[str, str] = {
        "voice": (
            "Verbosity: LOW. Keep responses to 1-2 sentences max. "
            "No filler, no preamble, no lists. This will be spoken aloud via TTS."
        ),
        "chat": (
            "Verbosity: MEDIUM. Keep responses to 3-5 sentences. "
            "Be informative but concise. No unnecessary padding."
        ),
        "video": (
            "Verbosity: MEDIUM. Keep responses to 2-4 sentences. "
            "Be clear and direct for video conversation."
        ),
    }
    verbosity_instruction = _VERBOSITY_INSTRUCTIONS.get(channel, _VERBOSITY_INSTRUCTIONS["chat"])

    # Rewrite system role to developer for reasoning models and inject verbosity
    processed_messages = []
    verbosity_injected = False
    for msg in messages:
        if msg["role"] == "system":
            # Append verbosity instruction to the existing system/developer message
            content_with_verbosity = msg["content"] + "\n\n" + verbosity_instruction
            if _is_reasoning:
                processed_messages.append({"role": "developer", "content": content_with_verbosity})
            else:
                processed_messages.append({"role": "system", "content": content_with_verbosity})
            verbosity_injected = True
        else:
            processed_messages.append(msg)

    # If no system message existed, prepend the verbosity instruction
    if not verbosity_injected:
        role = "developer" if _is_reasoning else "system"
        processed_messages.insert(0, {"role": role, "content": verbosity_instruction})

    # Use provided temperature or default to 0.1 for precision if not specified
    effective_temp = temperature if temperature is not None else 0.1

    # GPT-5 reasoning_effort="low" cuts TTFT dramatically for summarization
    effort = "low" if _is_reasoning else None

    return generate_text_sync(
        model=model,
        messages=processed_messages,
        api_key=api_key,
        base_url=settings.openai_base_url,
        timeout_seconds=timeout,
        max_output_tokens=4096,
        temperature=None if _is_reasoning else effective_temp,
        prefer_responses_api=True,
        reasoning_effort=effort,
        prompt_cache_key="aspire-respond",
        prompt_cache_retention="24h",
    ).strip()


def _load_agent_persona(agent_id: str) -> str:
    """Load the agent's persona file for response generation.

    Injects universal platform awareness (Law #1: Single Brain) by prepending
    aspire_awareness.md to the agent's specific system prompt.
    """
    from pathlib import Path

    config_dir = Path(__file__).parent.parent / "config"
    persona_dir = config_dir / "pack_personas"

    # 1. Load Universal Awareness (Who is on the team, how Aspire works)
    awareness_file = config_dir / "aspire_awareness.md"
    awareness_text = ""
    if awareness_file.exists():
        try:
            awareness_text = awareness_file.read_text(encoding="utf-8").strip() + "\n\n"
        except Exception:
            pass

    # 2. Load Specific Agent Persona
    from aspire_orchestrator.services.agent_identity import AGENT_PERSONA_MAP
    filename = AGENT_PERSONA_MAP.get(agent_id, f"{agent_id}_system_prompt.md")
    persona_file = persona_dir / filename

    agent_text = ""
    if persona_file.exists():
        try:
            agent_text = persona_file.read_text(encoding="utf-8")
        except Exception:
            pass

    # Fallback: Ava's persona (she orchestrates all responses)
    if not agent_text:
        ava_file = persona_dir / "ava_user_system_prompt.md"
        if ava_file.exists():
            try:
                agent_text = ava_file.read_text(encoding="utf-8")
            except Exception:
                pass

    if not agent_text:
        agent_text = (
            "You are Ava, the AI executive assistant for Aspire. "
            "You speak in a warm, professional, concise voice. "
            "You confirm actions, explain outcomes, and guide the user."
        )

    # Combine: Awareness + Specific Persona
    return awareness_text + agent_text


def _generate_approval_prompt(state: OrchestratorState, channel: str = "chat") -> str:
    """Generate inline approval prompt for YELLOW tier (WARM state).

    Ecosystem rule: YELLOW actions are presented as drafts inline in the
    conversation. Ava describes what she's about to do and asks "Should I
    proceed?" — the user confirms right there in voice/chat. This NEVER
    goes to the Authority Queue (that's for async items only).

    Uses LLM with persona for natural phrasing, template as fallback.
    """
    task_type = state.get("task_type", "unknown")
    utterance = state.get("utterance", "")
    domain_prefix = task_type.split(".")[0] if "." in task_type else task_type
    action_suffix = task_type.split(".", 1)[1] if "." in task_type else ""

    # Template fallback — describes the draft and asks for confirmation
    _draft_descriptions: dict[str, str] = {
        "invoice.create": "I've prepared an invoice draft based on your request.",
        "invoice.send": "I've got the invoice ready to send.",
        "email.send": "I've drafted the email for you.",
        "email.draft": "I've drafted the email for you.",
        "office.create": "I've prepared the office message draft.",
        "office.draft": "I've prepared the office message draft.",
        "office.send": "I've prepared your office message to send.",
        "calendar.create": "I've prepared a calendar event.",
        "contract.generate": "I've drafted the contract.",
        "booking.create": "I've prepared the booking details.",
        "scheduling.create": "I've set up the schedule.",
    }

    specific_key = f"{domain_prefix}.{action_suffix}" if action_suffix else domain_prefix
    draft_desc = _draft_descriptions.get(
        specific_key,
        _draft_descriptions.get(domain_prefix, "I've prepared this for you."),
    )
    fallback_text = f"{draft_desc} Want me to go ahead?"

    # Try LLM for more natural phrasing
    if not utterance:
        return fallback_text

    agent_id = _resolve_agent_id(state)
    persona = _load_agent_persona(agent_id)

    prompt = (
        f"The user said: \"{utterance}\"\n\n"
        f"You are about to perform: {task_type}\n"
        f"Risk tier: YELLOW (requires user confirmation before executing)\n\n"
        "Generate a brief voice response (1-2 sentences max) that:\n"
        "- Describes what you've prepared (the draft)\n"
        "- Asks the user for confirmation to proceed\n"
        "- Sounds natural when spoken aloud (this is text-to-speech)\n"
        "- Does NOT mention 'Authority Queue' or 'approval queue'\n"
        "- Does NOT use markdown or formatting\n"
        "Example tone: 'I've drafted an invoice for $500 to John Smith. Should I send it?'\n"
        "Respond with ONLY the spoken text, nothing else."
    )

    try:
        messages = []
        if persona:
            messages.append({"role": "system", "content": persona})
        messages.append({"role": "user", "content": prompt})

        content = _call_openai_sync(messages, channel=channel)

        if content:
            logger.info("LLM approval prompt success for %s", agent_id)
            return content

        return fallback_text

    except Exception as e:
        logger.warning("LLM approval prompt failed: %s — using template", e)
        return fallback_text


def _generate_presence_prompt(state: OrchestratorState, channel: str = "chat") -> str:
    """Generate video escalation prompt for RED tier (HOT state).

    Ecosystem rule: RED actions (payments >$5000, contracts, payroll, filings)
    require video presence with Ava. She tells the user to navigate to
    'Video with Ava' for the binding authority moment. Ava does NOT start
    the video — the user clicks into the video screen.

    Uses LLM with persona for natural phrasing, template as fallback.
    """
    task_type = state.get("task_type", "unknown")
    utterance = state.get("utterance", "")
    domain_prefix = task_type.split(".")[0] if "." in task_type else task_type

    # Template fallback — tells user video is required
    _presence_descriptions: dict[str, str] = {
        "payment": "This payment requires your video confirmation.",
        "transfer": "This transfer requires your video confirmation.",
        "payroll": "Running payroll requires your video confirmation.",
        "contract": "Signing this contract requires your video presence.",
        "filing": "This filing requires your video confirmation.",
    }

    desc = _presence_descriptions.get(
        domain_prefix,
        "This action requires your video confirmation.",
    )
    fallback_text = f"{desc} Head over to Video with Ava so we can finalize this together."

    # Try LLM for more natural phrasing
    if not utterance:
        return fallback_text

    agent_id = _resolve_agent_id(state)
    persona = _load_agent_persona(agent_id)

    prompt = (
        f"The user said: \"{utterance}\"\n\n"
        f"You are about to perform: {task_type}\n"
        f"Risk tier: RED (requires video presence — binding authority action)\n\n"
        "Generate a brief voice response (1-2 sentences max) that:\n"
        "- Explains this is a high-stakes action requiring video confirmation\n"
        "- Tells the user to go to 'Video with Ava' to finalize it (the USER clicks, you don't start it)\n"
        "- Sounds natural and reassuring (not alarming)\n"
        "- Sounds natural when spoken aloud (this is text-to-speech)\n"
        "- Does NOT mention 'Authority Queue' or 'approval queue'\n"
        "- Does NOT say you will start or launch a video session — the user navigates there\n"
        "- Does NOT use markdown or formatting\n"
        "Example tone: 'This payment needs your video sign-off. Head over to Video with Ava so we can finalize it together.'\n"
        "Respond with ONLY the spoken text, nothing else."
    )

    try:
        messages = []
        if persona:
            messages.append({"role": "system", "content": persona})
        messages.append({"role": "user", "content": prompt})

        content = _call_openai_sync(messages, channel=channel)

        if content:
            logger.info("LLM presence prompt success for %s", agent_id)
            return content

        return fallback_text

    except Exception as e:
        logger.warning("LLM presence prompt failed: %s — using template", e)
        return fallback_text


def _extract_media_items(state: OrchestratorState) -> list[dict[str, Any]]:
    """Extract media payloads from execution_result for chat rendering."""
    execution_result = state.get("execution_result") or {}
    data = execution_result.get("data") if isinstance(execution_result, dict) else None
    if not isinstance(data, dict):
        return []

    media_items: list[dict[str, Any]] = []

    # Preferred: explicit image payload from search.image route
    images = data.get("images")
    if isinstance(images, list):
        for item in images:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if isinstance(url, str) and url.strip():
                media_items.append(
                    {
                        "type": "image",
                        "url": url.strip(),
                        "title": item.get("title", ""),
                        "source": item.get("source", ""),
                    }
                )

    # Fallback: collect image_url fields from generic search results
    if not media_items:
        results = data.get("results")
        if isinstance(results, list):
            for row in results:
                if not isinstance(row, dict):
                    continue
                image_url = row.get("image_url")
                if isinstance(image_url, str) and image_url.strip():
                    media_items.append(
                        {
                            "type": "image",
                            "url": image_url.strip(),
                            "title": row.get("title", ""),
                            "source": row.get("url", ""),
                        }
                    )
                    if len(media_items) >= 4:
                        break

    return media_items


def respond_node(state: OrchestratorState) -> dict[str, Any]:
    """Construct and validate the response.

    Returns the full response dict to be sent to the client.
    """
    # Law #2 safety net: persist any receipts that skipped receipt_write
    safety_net_ids = _persist_unpersisted_receipts(state)

    correlation_id = state.get("correlation_id", "unknown")
    request_id = state.get("request_id", "unknown")
    error_code = state.get("error_code")
    receipt_ids = list(state.get("receipt_ids", []))

    # Merge any newly persisted receipt IDs
    if safety_net_ids:
        receipt_ids.extend(safety_net_ids)

    # Error case — return structured error with human-readable text
    if error_code:
        # Ecosystem interaction states (Law #8):
        #   APPROVAL_REQUIRED (YELLOW) → WARM state: Ava presents draft inline
        #   PRESENCE_REQUIRED (RED) → HOT state: Ava escalates to video
        #   Authority Queue is ASYNC only — never used for inline approvals

        if error_code == "APPROVAL_REQUIRED":
            # YELLOW tier → WARM state → Ava presents the draft and asks inline
            risk_tier_val = state.get("risk_tier").value if hasattr(state.get("risk_tier"), "value") else str(state.get("risk_tier", "yellow"))
            # Extract channel for voice verification UX
            _req_ap = state.get("request")
            _req_ap_payload = (_req_ap.get("payload", {}) if isinstance(_req_ap, dict) else getattr(_req_ap, "payload", {})) or {}
            _channel_ap = _req_ap_payload.get("channel", "chat")
            narration = compose_narration(
                outcome="pending",
                task_type=state.get("task_type", "unknown"),
                tool_used=state.get("tool_used"),
                execution_params=state.get("execution_params"),
                execution_result=None,
                draft_id=state.get("draft_id"),
                risk_tier=risk_tier_val,
                channel=_channel_ap,
            )
            text = narration if narration else _generate_approval_prompt(state, channel=_channel_ap)
            # Guard against misleading clarification text when recipient is present.
            _params_ap = state.get("execution_params") or {}
            if (
                state.get("task_type") == "email.draft"
                and isinstance(_params_ap, dict)
                and _params_ap.get("to")
            ):
                to_val = str(_params_ap.get("to")).strip()
                text = f"I drafted an email to {to_val}. Review it in your Authority Queue, then approve or deny."
        elif error_code == "PRESENCE_REQUIRED":
            # RED tier → HOT state → Ava escalates to video
            _req_pr = state.get("request")
            _req_pr_payload = (_req_pr.get("payload", {}) if isinstance(_req_pr, dict) else getattr(_req_pr, "payload", {})) or {}
            _channel_pr = _req_pr_payload.get("channel", "chat")
            text = _generate_presence_prompt(state, channel=_channel_pr)
        else:
            _error_messages: dict[str, str] = {
                "POLICY_DENIED": "Your security policy doesn't allow this action.",
                "SAFETY_BLOCKED": "I can't help with that request. I can offer a safer alternative if you want.",
                "CAPABILITY_TOKEN_REQUIRED": "I need authorization to perform this action.",
                "CAPABILITY_TOKEN_EXPIRED": "The authorization for this action has expired. Please try again.",
                "SCHEMA_VALIDATION_FAILED": "I didn't understand that request. Could you try rephrasing it?",
                "MODEL_UNAVAILABLE": "I'm having trouble reaching the language model right now. Please try again in a moment.",
                "CHECKPOINTER_UNAVAILABLE": "I'm having trouble accessing conversation memory right now. Please try again shortly.",
                "UPSTREAM_TIMEOUT": "This task is taking longer than expected. I'm still working on it and can continue if you want.",
                "ROUTER_FALLBACK_ACTIVE": "I'm routing this request through a fallback path to keep things moving.",
                "PROVIDER_ALL_FAILED": "All providers failed this request. Try a narrower query or retry in a moment.",
                "PROVIDER_AUTH_MISSING": "A required provider connection is missing or expired. Please check your connected services.",
                "ROUTING_DENIED": "This request could not be routed to a valid skill path. Please rephrase the task.",
                "EXECUTION_FAILED": "I ran into an execution error while handling that task. Please try again.",
            }
            raw_fallback = state.get("error_message", "Something went wrong. Please try again.")
            # Strip correlation IDs and raw field names from user-facing text
            import re
            sanitized_fallback = re.sub(r"\(ref\s+corr_[a-zA-Z0-9_-]+\)", "", raw_fallback).strip()
            sanitized_fallback = re.sub(r"\b[a-z]+_[a-z]+(?:_[a-z]+)*\b", lambda m: m.group().replace("_", " "), sanitized_fallback)
            text = _error_messages.get(error_code, sanitized_fallback or "Something went wrong. Please try again.")

        response: dict[str, Any] = {
            "error": error_code,
            "message": state.get("error_message", "Unknown error"),  # internal, not shown to user
            "text": text,
            "correlation_id": correlation_id,
            "request_id": request_id,
            "receipt_ids": receipt_ids,
            "assigned_agent": _resolve_assigned_agent(state),
        }

        # For approval-required, include the payload hash + draft details
        if error_code in ("APPROVAL_REQUIRED", "PRESENCE_REQUIRED"):
            response["approval_payload_hash"] = state.get("approval_payload_hash")
            response["required_approvals"] = state.get("required_approvals", [])
            response["presence_required"] = state.get("presence_required", False)
            response["draft_id"] = state.get("draft_id")
            # Include task context so the Desktop can display draft details
            response["task_type"] = state.get("task_type")
            response["risk_tier"] = state.get("risk_tier").value if hasattr(state.get("risk_tier"), "value") else str(state.get("risk_tier", "yellow"))
            response["utterance"] = state.get("utterance", "")
            response["execution_params"] = state.get("execution_params")

        return {"response": response}

    # Success case — construct AvaResult
    risk_tier = state.get("risk_tier", RiskTier.GREEN)
    risk_tier_val = risk_tier.value if isinstance(risk_tier, RiskTier) else str(risk_tier)

    # ── Conversation path short-circuit (Wave 1) ──────────────────────
    # If agent_reason_node produced a response, use it directly.
    # Skip template/LLM summarize — the agent already reasoned.
    conversation_response = state.get("conversation_response")
    if conversation_response:
        # Wave 3B: Prepend greeting on first interaction (non-greeting utterance)
        if state.get("_inject_greeting_prefix"):
            from aspire_orchestrator.nodes.greeting_fast_path import greeting_response
            _agent_for_greet = _resolve_assigned_agent(state)
            _greet = greeting_response(_agent_for_greet, state.get("user_profile"))
            conversation_response = f"{_greet} {conversation_response}"
            logger.info("Injected greeting prefix into conversation_response (agent=%s)", _agent_for_greet)

        response: dict[str, Any] = {
            "text": conversation_response,
            "correlation_id": correlation_id,
            "request_id": request_id,
            "receipt_ids": receipt_ids,
            "assigned_agent": _resolve_assigned_agent(state),
        }
        return {"response": response}

    # ── Phantom execution guard ──────────────────────────────────────
    # If pipeline reached respond without executing (no token minted,
    # no execution result), the classify/route/param_extract node
    # short-circuited to respond. Do NOT let LLM claim success —
    # return clarification response instead.
    _execution_happened = (
        state.get("capability_token_id") is not None
        or bool(state.get("execution_result"))
    )

    if not _execution_happened and state.get("utterance"):
        intent_result = state.get("intent_result") or {}
        utterance = state.get("utterance", "")

        # Detect conversational/greeting input vs. unclear action request
        intent_type = intent_result.get("intent_type", "") if isinstance(intent_result, dict) else ""

        _GREETING_PATTERNS = frozenset({
            # Basic greetings
            "hey", "hi", "hello", "yo", "sup",
            "good morning", "good afternoon", "good evening",
            "what's up", "whats up", "how are you",
            # Identity questions
            "whats your name", "what's your name", "what is your name",
            "who are you", "who is this", "who am i talking to",
            "what are you", "what is your purpose",
            "what do you do", "what can you do",
            "tell me about yourself", "introduce yourself",
            # Capability questions
            "how can you help", "how can you help me",
            "what are your capabilities", "what services do you offer",
            # Team/agent questions
            "who is finn", "who is nora", "who is eli", "who is sarah",
            "who is adam", "who is quinn", "who is clara", "who is tec",
            "who do you work for", "what team do you have",
            "who is on the team", "who is on your team",
            # Courtesy
            "help", "thanks", "thank you", "bye", "goodbye",
            "see you", "later", "nice to meet you",
        })
        normalized = utterance.lower().strip().rstrip("!?.,")
        _IDENTITY_SUBSTRINGS = (
            "your name", "who are you", "what do you do", "how can you help", "what can you",
            "who is ", "who do you", "tell me about", "what is aspire", "what's aspire",
            "do you know", "can you tell me", "explain ", "describe ",
        )
        is_conversational = (
            intent_type in ("greeting", "chitchat", "conversational", "conversation", "knowledge", "advice", "hybrid")
            or normalized in _GREETING_PATTERNS
            or any(sub in normalized for sub in _IDENTITY_SUBSTRINGS)
        )

        # Handle __greeting__ sentinel from Desktop mount
        if utterance == "__greeting__":
            agent_id = _resolve_agent_id(state)
            user_profile = state.get("user_profile") or {}
            # Prioritize owner_name for formal greeting (Mr./Mrs. Last Name logic in skillpack)
            user_name = user_profile.get("owner_name") or user_profile.get("display_name") or user_profile.get("first_name")
            
            # Resolve agent instance and call LLM-powered get_greeting (Premium Human Logic)
            try:
                import asyncio
                from aspire_orchestrator.services.agent_registry import get_agent_registry
                from aspire_orchestrator.services.agent_sdk_base import AgentContext
                
                registry = get_agent_registry()
                agent_instance = registry.get_agent(agent_id)
                
                if agent_instance:
                    ctx = AgentContext(
                        suite_id=state.get("suite_id", "unknown"),
                        office_id=state.get("office_id", "default"),
                        correlation_id=correlation_id,
                        risk_tier="green",
                    )
                    # Run async greeting in sync context
                    greeting_text = asyncio.run(agent_instance.get_greeting(ctx, user_name=user_name))
                else:
                    greeting_text = "Good morning! How can I help you today?"
            except Exception as e:
                logger.warning("Failed to generate LLM greeting for %s: %s", agent_id, e)
                greeting_text = "Good morning! How can I help you today?"

            response: dict[str, Any] = {
                "text": greeting_text,
                "correlation_id": correlation_id,
                "request_id": request_id,
                "receipt_ids": receipt_ids,
                "assigned_agent": _resolve_assigned_agent(state),
            }
            return {"response": response}

        if is_conversational:
            # Natural conversational reply (not an unclear action)
            # Extract channel for verbosity adaptation
            _req_conv = state.get("request")
            _req_conv_payload = (_req_conv.get("payload", {}) if isinstance(_req_conv, dict) else getattr(_req_conv, "payload", {})) or {}
            _channel_conv = _req_conv_payload.get("channel", "chat")
            conv_text = _llm_conversational_reply(state, utterance, channel=_channel_conv)
            response = {
                "text": conv_text,
                "correlation_id": correlation_id,
                "request_id": request_id,
                "receipt_ids": receipt_ids,
                "assigned_agent": _resolve_assigned_agent(state),
            }
            return {"response": response}

        # Genuinely unclear action request — return clarification
        clarification = intent_result.get("clarification_prompt", "") if isinstance(intent_result, dict) else ""
        if not clarification:
            clarification = (
                "I didn't catch exactly what you need there. "
                "Can you give me a bit more context so I can route this to the right person on the team?"
            )

        logger.warning(
            "Phantom execution guard: pipeline reached respond without execution "
            "(no capability_token_id, no execution_result). Returning clarification. "
            "task_type=%s, utterance=%.60s",
            state.get("task_type", "unknown"),
            utterance[:60],
        )

        response = {
            "error": "CLASSIFICATION_UNCLEAR",
            "message": "Pipeline did not reach execution — classify/route short-circuit",
            "text": clarification,
            "correlation_id": correlation_id,
            "request_id": request_id,
            "receipt_ids": receipt_ids,
            "assigned_agent": _resolve_assigned_agent(state),
        }
        return {"response": response}

    # Generate human-readable response text
    # Step 1: Check narration layer (deterministic, template-based for action outcomes)
    narration_text = state.get("narration_text")
    outcome = state.get("outcome", Outcome.SUCCESS)
    outcome_val = outcome.value if hasattr(outcome, "value") else str(outcome)

    # Extract channel (voice/chat/video) for UX adaptation
    _req = state.get("request")
    _req_payload = (_req.get("payload", {}) if isinstance(_req, dict) else getattr(_req, "payload", {})) or {}
    _channel = _req_payload.get("channel", "chat")

    if not narration_text and state.get("tool_used"):
        narration_text = compose_narration(
            outcome=outcome_val,
            task_type=state.get("task_type", "unknown"),
            tool_used=state.get("tool_used"),
            execution_params=state.get("execution_params"),
            execution_result=state.get("execution_result"),
            draft_id=state.get("draft_id"),
            risk_tier=risk_tier_val,
            channel=_channel,
        )

    if narration_text:
        # Narration layer produced text — use it directly (skip LLM)
        response_text = narration_text
    else:
        # Step 2: Build template fallback (fast, deterministic)
        template_text = _generate_response_text(state)
        # Step 3: Use LLM with agent persona for natural response (preferred)
        response_text = _llm_summarize(state, fallback_text=template_text, channel=_channel)

    # Wave 3B: If first interaction flagged for greeting prefix, prepend agent greeting
    if state.get("_inject_greeting_prefix"):
        from aspire_orchestrator.nodes.greeting_fast_path import greeting_response
        agent = _resolve_assigned_agent(state)
        user_profile = state.get("user_profile")
        greeting = greeting_response(agent, user_profile)
        # Prepend greeting with natural separator
        response_text = f"{greeting} {response_text}"
        logger.info("Injected greeting prefix for first interaction (agent=%s)", agent)

    # Output guard: strip phantom execution claims
    response_text = guard_output(
        text=response_text,
        receipts=list(state.get("pipeline_receipts", [])),
        outcome=outcome_val,
        channel=_channel,
    )

    # Build governance metadata
    required_approvals = state.get("required_approvals", [])
    presence_required = state.get("presence_required", False)
    capability_token_required = state.get("capability_token_id") is not None

    # Agent identity for Desktop rendering (which persona to show)
    assigned_agent = _resolve_assigned_agent(state)

    try:
        result = AvaResult(
            schema_version="1.0",
            request_id=request_id,
            correlation_id=correlation_id,
            text=response_text,
            route={
                "skill_pack": state.get("task_type", "").split(".")[0] if state.get("task_type") else "unknown",
                "tool": state.get("tool_used", "unknown"),
                "agent": assigned_agent,
            },
            risk=AvaResultRisk(tier=RiskTier(risk_tier_val)),
            governance=AvaResultGovernance(
                approvals_required=required_approvals,
                presence_required=presence_required,
                capability_token_required=capability_token_required,
                receipt_ids=receipt_ids,
            ),
            plan={
                "task_type": state.get("task_type"),
                "outcome": state.get("outcome", Outcome.SUCCESS).value if hasattr(state.get("outcome", Outcome.SUCCESS), "value") else str(state.get("outcome", "success")),
                "execution_result": state.get("execution_result"),
            },
        )

        # Egress validation — validate AvaResult schema before returning
        response = result.model_dump()
        response["assigned_agent"] = assigned_agent
        media = _extract_media_items(state)
        if media:
            response["media"] = media
        return {"response": response}

    except (ValidationError, Exception) as e:
        # If we can't construct a valid AvaResult, return error
        return {
            "response": {
                "error": "INTERNAL_ERROR",
                "message": f"Failed to construct AvaResult: {e}",
                "text": "I'm having trouble right now. Please try again.",
                "correlation_id": correlation_id,
                "request_id": request_id,
                "receipt_ids": receipt_ids,
            }
        }
