"""Greeting Fast Path — bypasses full graph for simple greetings.

Law #1 compliance: This IS the orchestrator deciding (fast path within the graph).
Law #2 compliance: Emits a GREEN receipt for the greeting.
Law #3 compliance: Falls through to full pipeline if uncertain.
"""

from __future__ import annotations

import logging
import random
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.llm_cache import get_llm_cache

logger = logging.getLogger(__name__)

GREETING_PATTERNS = [
    r"^(hi|hello|hey|howdy|yo|sup|good\s*(morning|afternoon|evening|day)|what'?s?\s*up|greetings)[\s!?.]*$",
    r"^(how\s*(are\s*you(\s*doing)?|you\s*doing|is\s*it\s*going))[\s!?.]*$",
    r"^(can\s*you\s*hear\s*me|are\s*you\s*there|testing|test)[\s!?.]*$",
]

GREETING_RESPONSES: dict[str, list[str]] = {
    "ava": [
        "Hey{name}! Ava here — your chief of staff. What can I help you with today?",
        "Good {tod}{name}! I'm ready whenever you are.",
        "Hello{name}! What's on your mind?",
    ],
    "nora": [
        "Hey{name}! Nora here. Ready for your conference needs.",
        "Hi{name}! I'm set up for your meeting. What do you need?",
    ],
    "eli": [
        "Hi{name}! Eli here. I can help with your inbox — what's up?",
        "Hey{name}! Ready to help with emails. What do you need?",
    ],
    "finn": [
        "Hey{name}! Finn here. Let's talk numbers — what do you need?",
        "Hi{name}! Ready to dive into your financials. What's on your mind?",
    ],
    "sarah": [
        "Hi{name}! Sarah here. How can I help with your calls?",
        "Hey{name}! Front desk is ready. What do you need?",
    ],
    "adam": [
        "Hey{name}! Adam here. Need me to research something?",
        "Hi{name}! Ready to dig into some research for you.",
    ],
}

_COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in GREETING_PATTERNS]
MAX_GREETING_WORDS = 10


def is_greeting(utterance: str) -> bool:
    """Check if utterance is a simple greeting (< 10 words, matches pattern)."""
    words = utterance.strip().split()
    if len(words) > MAX_GREETING_WORDS:
        return False
    cleaned = utterance.strip()
    return any(p.match(cleaned) for p in _COMPILED_PATTERNS)


def _time_of_day() -> str:
    """Return 'morning', 'afternoon', or 'evening' based on UTC hour."""
    hour = datetime.now(timezone.utc).hour
    if hour < 12:
        return "morning"
    elif hour < 17:
        return "afternoon"
    return "evening"


def _formal_name(user_profile: dict[str, Any] | None) -> str:
    """Extract 'Mr./Ms. LastName' from user_profile, or empty string."""
    if not user_profile:
        return ""
    name = user_profile.get("owner_name") or user_profile.get("display_name") or ""
    if not name or not name.strip():
        return ""
    parts = name.strip().split()
    last_name = parts[-1] if parts else name
    return f", Mr. {last_name}"


def greeting_response(agent: str, user_profile: dict[str, Any] | None = None) -> str:
    """Return a random persona-appropriate greeting with user name."""
    responses = GREETING_RESPONSES.get(agent, GREETING_RESPONSES["ava"])
    template = random.choice(responses)
    formal = _formal_name(user_profile)
    return template.format(name=formal, tod=_time_of_day())


async def greeting_fast_path_node(state: dict[str, Any]) -> dict[str, Any]:
    """Graph node: If utterance is a greeting, short-circuit to respond.

    Sets response text + receipt directly, skipping classify/route/execute.
    Uses LLM cache for sub-10ms repeated greetings.
    """
    utterance = state.get("utterance", "")
    agent = state.get("requested_agent") or state.get("agent") or "ava"
    if isinstance(agent, str):
        agent = agent.strip().lower() or "ava"

    logger.info("greeting_check: utterance=%r agent=%s", utterance, agent)

    if not is_greeting(utterance):
        logger.info("greeting_check: not a greeting, continuing to full pipeline")
        state["_greeting_fast_path"] = False
        return state

    logger.info("greeting_check: GREETING DETECTED, using fast path")

    user_profile = state.get("user_profile")

    # Generate personalized greeting (no cache — user name + time-of-day make each unique)
    response_text = greeting_response(agent, user_profile)
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    state["_greeting_fast_path"] = True
    state["response_text"] = response_text
    state["conversation_response"] = response_text
    state["status"] = "success"
    state["assigned_agent"] = agent
    state["governance"] = {
        "receipt_ids": [receipt_id],
        "risk_tier": "green",
    }
    state["_fast_path_receipt"] = {
        "id": receipt_id,
        "receipt_id": receipt_id,
        "receipt_type": "greeting.fast_path",
        "action_type": "greeting",
        "outcome": "success",
        "reason_code": "GREETING_FAST_PATH",
        "risk_tier": "green",
        "created_at": now,
        "receipt_hash": "",
        "actor_type": state.get("actor_type", "user"),
        "actor_id": state.get("actor_id", ""),
        "suite_id": state.get("suite_id", ""),
        "office_id": state.get("office_id", ""),
        "correlation_id": state.get("correlation_id", ""),
        "tool_used": "greeting_fast_path_node",
        "redacted_inputs": {
            "utterance_words": len(utterance.strip().split()),
            "agent": agent,
            "fast_path": True,
        },
    }

    # Append to pipeline_receipts so respond node's safety net can persist it
    pipeline_receipts = list(state.get("pipeline_receipts", []))
    pipeline_receipts.append(state["_fast_path_receipt"])
    state["pipeline_receipts"] = pipeline_receipts

    return state
