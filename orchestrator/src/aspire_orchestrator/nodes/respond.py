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
from aspire_orchestrator.services.output_guard import guard_output
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)

# Maps task_type domain prefix → agent persona ID.
# The classifier outputs task_types like "research.search", "invoice.create",
# "payment.send" — but the personas are named by agent (adam, quinn, finn).
# Users talk to Ava, Eli, Nora, Finn directly — Ava orchestrates everything.
_DOMAIN_TO_AGENT: dict[str, str] = {
    "research": "adam",
    "invoice": "quinn",
    "quote": "quinn",
    "email": "eli",
    "mail": "eli",
    "calendar": "ava",
    "scheduling": "ava",
    "booking": "ava",
    "conference": "nora",
    "meeting": "nora",
    "payment": "finn",
    "transfer": "finn",
    "finance": "finn_fm",
    "payroll": "milo",
    "contract": "clara",
    "legal": "clara",
    "document": "tec",
    "filing": "teressa",
    "bookkeeping": "teressa",
    "accounting": "teressa",
    "contact": "ava",
    "domain": "mail_ops",
    "mailbox": "mail_ops",
    "frontdesk": "sarah",
    "call": "sarah",
    "sms": "sarah",
    "telephony": "sarah",
}


def _resolve_agent_id(state: OrchestratorState) -> str:
    """Resolve the agent persona ID from task_type and request context.

    Priority: request.agent field (Desktop sends this) → domain prefix mapping → ava fallback.
    """
    # Desktop proxy sends agent field in the original request payload
    request = state.get("request")
    if isinstance(request, dict):
        explicit_agent = request.get("agent")
    elif hasattr(request, "payload") and isinstance(request.payload, dict):
        explicit_agent = request.payload.get("agent")
    else:
        explicit_agent = None

    # Map explicit agent names to persona IDs
    if explicit_agent and explicit_agent != "ava":
        _agent_name_map = {
            "finn": "finn_fm",
            "eli": "eli",
            "nora": "nora",
            "sarah": "sarah",
            "adam": "adam",
            "quinn": "quinn",
            "tec": "tec",
            "teressa": "teressa",
            "milo": "milo",
            "clara": "clara",
        }
        if explicit_agent in _agent_name_map:
            return _agent_name_map[explicit_agent]

    # Fall back to domain prefix mapping from task_type
    task_type = state.get("task_type", "unknown")
    domain_prefix = task_type.split(".")[0] if "." in task_type else task_type
    return _DOMAIN_TO_AGENT.get(domain_prefix, "ava")


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
    """
    task_type = state.get("task_type", "unknown")
    outcome = state.get("outcome", Outcome.SUCCESS)
    outcome_val = outcome.value if hasattr(outcome, "value") else str(outcome)
    execution_result = state.get("execution_result") or {}
    utterance = state.get("utterance", "")
    tool_used = state.get("tool_used", "unknown")
    is_stub = execution_result.get("stub", False)

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
            f"I understand your request, but {provider_name} isn't connected yet. "
            f"Head to your connections page to set it up, and I'll handle this right away."
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
        return f"{desc} has been submitted for secure processing. I'll notify you when it's complete."

    # Success with live execution
    if outcome_val == "success":
        _success_responses: dict[str, str] = {
            "research": "I found some results for you. Check your activity feed for the full details.",
            "invoice": "Your invoice has been created successfully.",
            "contract": "Your contract has been prepared.",
            "calendar": "Done — your calendar has been updated.",
            "email": "Your email has been handled.",
            "meeting": "Your meeting room is ready. You can join now.",
            "conference": "Your conference room is set up and ready to go.",
            "finance": "Here's what I found in your financial data.",
            "payment": "Your payment has been processed.",
            "document": "Your document is ready.",
            "contact": "Your contacts have been updated.",
            "booking": "Your booking has been confirmed.",
            "scheduling": "Your schedule has been updated.",
        }
        base = _success_responses.get(domain_prefix, "Done — I've completed your request.")

        # Enrich with action detail when available
        if domain_prefix == "research" and utterance:
            # Trim to first 60 chars of utterance for voice brevity
            topic = utterance[:60].rstrip()
            if len(utterance) > 60:
                topic += "..."
            base = f"I searched for \"{topic}\". Check your activity feed for the results."
        elif domain_prefix == "invoice" and action_suffix == "send":
            base = "Your invoice has been sent to the client."
        elif domain_prefix == "invoice" and action_suffix == "create":
            base = "Your invoice has been created. It's waiting in your drafts."
        elif domain_prefix == "contract" and action_suffix == "generate":
            base = "Your contract has been drafted. Review it before sending."
        elif domain_prefix == "email" and action_suffix in ("send", "draft"):
            base = "Your email draft is ready for review."

        return base

    # Failed execution
    if outcome_val == "failed":
        return "I ran into a problem processing that. Please try again, or check your connections page."

    # Denied by policy
    if outcome_val == "denied":
        return "I'm not able to perform that action. It was blocked by your security policy."

    # Fallback
    return "I've processed your request."


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


def _llm_summarize(state: OrchestratorState, fallback_text: str) -> str:
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

    # Guard against empty/stub execution producing garbage responses
    is_empty_execution = (
        task_type in ("unknown", "")
        and tool_used in ("unknown", "")
        and (not execution_result or execution_result == {} or execution_result.get("stub"))
    )
    if is_empty_execution:
        return _llm_conversational_reply(state, utterance)

    # Load the routed agent's persona for response generation
    agent_id = _resolve_agent_id(state)
    persona = _load_agent_persona(agent_id)
    if persona:
        persona = _strip_format_instructions(persona)

    # Build the summarization prompt
    prompt = (
        f"The user said: \"{utterance}\"\n\n"
        f"Action taken: {task_type}\n"
        f"Tool used: {tool_used}\n"
        f"Outcome: {outcome_val}\n"
        f"Execution details: {json.dumps(execution_result, default=str)}\n\n"
        "Generate a brief, natural voice response (1-2 sentences max) that:\n"
        "- Directly addresses what the user asked\n"
        "- Confirms what was done or explains what happened\n"
        "- Sounds natural when spoken aloud (this will be text-to-speech)\n"
        "- Does NOT use markdown, bullet points, or formatting\n"
        "- Does NOT say 'I processed your request' or generic filler\n"
        "Respond with ONLY the spoken text, nothing else."
    )

    try:
        messages = []
        if persona:
            messages.append({"role": "system", "content": persona})
        messages.append({"role": "user", "content": prompt})

        content = _call_openai_sync(messages, model=settings.router_model_general)

        if content:
            logger.info("LLM summarization success for %s (len=%d)", agent_id, len(content))
            return content

        return fallback_text

    except Exception as e:
        # Law #3: Fail gracefully — use template response if LLM fails
        logger.warning("LLM summarization failed for %s: %s — using template", agent_id, e)
        return fallback_text


def _llm_conversational_reply(state: OrchestratorState, utterance: str) -> str:
    """Generate a natural conversational reply for non-action input."""
    agent_id = _resolve_agent_id(state)
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

    prompt = (
        f'The user said: "{utterance}"\n\n'
        f"You are {agent_name}, speaking to the user directly.\n"
        "This is a conversational message, not an action request. "
        "Respond in character using your persona's personality and expertise. "
        "If the user asks who you are, introduce yourself warmly with your name, "
        "role, and what you can help them with. "
        "Keep it brief (1-3 sentences), warm, and natural. "
        "Do NOT use markdown or bullet points. This will be spoken aloud via TTS.\n"
        "Respond with ONLY the spoken text."
    )

    try:
        messages: list[dict[str, str]] = []
        if persona:
            messages.append({"role": "system", "content": persona})
        messages.append({"role": "user", "content": prompt})
        content = _call_openai_sync(messages, model=settings.router_model_general)
        if content:
            return content
    except Exception as e:
        logger.warning("Conversational LLM failed: %s", e)

    # Deterministic fallback
    return "Hey! I'm here to help. What would you like me to work on today?"


def _call_openai_sync(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    timeout: float = settings.openai_timeout_seconds,
) -> str:
    """Shared sync OpenAI SDK call for respond node LLM operations.

    Handles reasoning model logic (developer role, no temperature, 4096 min tokens).
    Returns content string or empty string on failure.
    """
    import os

    import openai

    api_key = os.environ.get("ASPIRE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return ""

    if model is None:
        model = settings.router_model_classifier

    _is_reasoning = model.startswith(("gpt-5", "o1", "o3"))

    # Rewrite system role to developer for reasoning models
    processed_messages = []
    for msg in messages:
        if msg["role"] == "system" and _is_reasoning:
            processed_messages.append({"role": "developer", "content": msg["content"]})
        else:
            processed_messages.append(msg)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": processed_messages,
        "max_completion_tokens": 4096,
    }
    if not _is_reasoning:
        kwargs["temperature"] = 0.3

    client = openai.OpenAI(
        api_key=api_key,
        base_url=settings.openai_base_url,
        timeout=timeout,
    )

    response = client.chat.completions.create(**kwargs)
    content = response.choices[0].message.content or "" if response.choices else ""
    return content.strip()


def _load_agent_persona(agent_id: str) -> str:
    """Load the agent's persona file for response generation.

    Personas are stored at:
      config/pack_personas/{agent_id}_system_prompt.md

    Special cases:
      - "ava" → ava_user_system_prompt.md (user-facing Ava)
      - "finn" when task is finance.* → finn_fm_system_prompt.md (Finance Manager)
      - "finn" otherwise → finn_system_prompt.md (Money Desk)

    Returns minimal built-in persona if file not found.
    """
    from pathlib import Path

    config_dir = Path(__file__).parent.parent / "config" / "pack_personas"

    # Map agent_id to persona filename — ALL agents have personas
    _persona_map: dict[str, str] = {
        "ava": "ava_user_system_prompt.md",
        "ava_admin": "ava_admin_system_prompt.md",
        "finn": "finn_system_prompt.md",
        "finn_fm": "finn_fm_system_prompt.md",
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

    filename = _persona_map.get(agent_id, f"{agent_id}_system_prompt.md")
    persona_file = config_dir / filename

    if persona_file.exists():
        try:
            return persona_file.read_text(encoding="utf-8")
        except Exception:
            pass

    # Fallback: Ava's persona (she orchestrates all responses)
    ava_file = config_dir / "ava_user_system_prompt.md"
    if ava_file.exists():
        try:
            return ava_file.read_text(encoding="utf-8")
        except Exception:
            pass

    # Minimal built-in persona (last resort)
    return (
        "You are Ava, the AI executive assistant for Aspire. "
        "You speak in a warm, professional, concise voice. "
        "You confirm actions, explain outcomes, and guide the user — always briefly."
    )


def _generate_approval_prompt(state: OrchestratorState) -> str:
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

        content = _call_openai_sync(messages)

        if content:
            logger.info("LLM approval prompt success for %s", agent_id)
            return content

        return fallback_text

    except Exception as e:
        logger.warning("LLM approval prompt failed: %s — using template", e)
        return fallback_text


def _generate_presence_prompt(state: OrchestratorState) -> str:
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

        content = _call_openai_sync(messages)

        if content:
            logger.info("LLM presence prompt success for %s", agent_id)
            return content

        return fallback_text

    except Exception as e:
        logger.warning("LLM presence prompt failed: %s — using template", e)
        return fallback_text


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
            text = narration if narration else _generate_approval_prompt(state)
        elif error_code == "PRESENCE_REQUIRED":
            # RED tier → HOT state → Ava escalates to video
            text = _generate_presence_prompt(state)
        else:
            _error_messages: dict[str, str] = {
                "POLICY_DENIED": "Your security policy doesn't allow this action.",
                "SAFETY_BLOCKED": "I can't process that request for safety reasons.",
                "CAPABILITY_TOKEN_REQUIRED": "I need authorization to perform this action.",
                "CAPABILITY_TOKEN_EXPIRED": "The authorization for this action has expired. Please try again.",
                "SCHEMA_VALIDATION_FAILED": "I didn't understand that request. Could you try rephrasing it?",
            }
            text = _error_messages.get(
                error_code,
                state.get("error_message", "Something went wrong. Please try again."),
            )

        response: dict[str, Any] = {
            "error": error_code,
            "message": state.get("error_message", "Unknown error"),
            "text": text,
            "correlation_id": correlation_id,
            "request_id": request_id,
            "receipt_ids": receipt_ids,
            "assigned_agent": state.get("assigned_agent", "ava"),
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
        response: dict[str, Any] = {
            "text": conversation_response,
            "correlation_id": correlation_id,
            "request_id": request_id,
            "receipt_ids": receipt_ids,
            "assigned_agent": state.get("agent_target") or state.get("assigned_agent", "ava"),
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
            # Courtesy
            "help", "thanks", "thank you", "bye", "goodbye",
            "see you", "later", "nice to meet you",
        })
        normalized = utterance.lower().strip().rstrip("!?.,")
        _IDENTITY_SUBSTRINGS = ("your name", "who are you", "what do you do", "how can you help", "what can you")
        is_conversational = (
            intent_type in ("greeting", "chitchat", "conversational")
            or normalized in _GREETING_PATTERNS
            or any(sub in normalized for sub in _IDENTITY_SUBSTRINGS)
        )

        # Handle __greeting__ sentinel from Desktop mount
        if utterance == "__greeting__":
            from datetime import datetime

            hour = datetime.now().hour
            time_greeting = (
                "Good morning" if hour < 12
                else "Good afternoon" if hour < 17
                else "Good evening"
            )
            greeting_text = f"{time_greeting}! How can I help you today?"
            response: dict[str, Any] = {
                "text": greeting_text,
                "correlation_id": correlation_id,
                "request_id": request_id,
                "receipt_ids": receipt_ids,
                "assigned_agent": state.get("assigned_agent", "ava"),
            }
            return {"response": response}

        if is_conversational:
            # Natural conversational reply (not an unclear action)
            conv_text = _llm_conversational_reply(state, utterance)
            response = {
                "text": conv_text,
                "correlation_id": correlation_id,
                "request_id": request_id,
                "receipt_ids": receipt_ids,
                "assigned_agent": state.get("assigned_agent", "ava"),
            }
            return {"response": response}

        # Genuinely unclear action request — return clarification
        clarification = intent_result.get("clarification_prompt", "") if isinstance(intent_result, dict) else ""
        if not clarification:
            clarification = (
                "I wasn't quite sure how to handle that. "
                "Could you rephrase your request or be more specific?"
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
            "assigned_agent": state.get("assigned_agent", "ava"),
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
        response_text = _llm_summarize(state, fallback_text=template_text)

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
    assigned_agent = state.get("assigned_agent", "ava")

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
