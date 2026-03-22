"""Greeting Fast Path — bypasses full graph for simple greetings.

Law #1 compliance: This IS the orchestrator deciding (fast path within the graph).
Law #2 compliance: Emits a GREEN receipt for the greeting.
Law #3 compliance: Falls through to full pipeline if uncertain.
"""

from __future__ import annotations

import random
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.llm_cache import get_llm_cache

GREETING_PATTERNS = [
    r"^(hi|hello|hey|howdy|yo|sup|good\s*(morning|afternoon|evening|day)|what'?s?\s*up|greetings)[\s!?.]*$",
    r"^(how\s*(are\s*you(\s*doing)?|you\s*doing|is\s*it\s*going))[\s!?.]*$",
    r"^(can\s*you\s*hear\s*me|are\s*you\s*there|testing|test)[\s!?.]*$",
]

GREETING_RESPONSES: dict[str, list[str]] = {
    "ava": [
        "Hey! I'm here and ready. What can I help you with?",
        "Hi there! What's on your mind?",
        "Hello! I'm listening — what do you need?",
    ],
    "nora": [
        "Hey! Nora here. Ready for your conference needs.",
        "Hi! I'm set up for your meeting. What do you need?",
    ],
    "eli": [
        "Hi! Eli here. I can help with your inbox — what's up?",
        "Hey! Ready to help with emails. What do you need?",
    ],
    "finn": [
        "Hey! Finn here. Let's talk numbers — what do you need?",
        "Hi! Ready to dive into your financials. What's on your mind?",
    ],
    "sarah": [
        "Hi! Sarah here. How can I help with your calls?",
        "Hey! Front desk is ready. What do you need?",
    ],
    "adam": [
        "Hey! Adam here. Need me to research something?",
        "Hi! Ready to dig into some research for you.",
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


def greeting_response(agent: str) -> str:
    """Return a random persona-appropriate greeting."""
    responses = GREETING_RESPONSES.get(agent, GREETING_RESPONSES["ava"])
    return random.choice(responses)


async def greeting_fast_path_node(state: dict[str, Any]) -> dict[str, Any]:
    """Graph node: If utterance is a greeting, short-circuit to respond.

    Sets response text + receipt directly, skipping classify/route/execute.
    Uses LLM cache for sub-10ms repeated greetings.
    """
    utterance = state.get("utterance", "")
    agent = state.get("requested_agent") or state.get("agent") or "ava"
    if isinstance(agent, str):
        agent = agent.strip().lower() or "ava"

    if not is_greeting(utterance):
        state["_greeting_fast_path"] = False
        return state

    # Try cache first (sub-10ms)
    cache = get_llm_cache()
    cache_key = f"voice:greeting:{agent}"
    cached = await cache.get(cache_key)
    if cached:
        response_text = cached
    else:
        response_text = greeting_response(agent)
        await cache.set(cache_key, response_text, profile="voice_greeting")
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
        "action": "greeting.fast_path",
        "result": "success",
        "risk_tier": "green",
        "created_at": now,
        "receipt_hash": "",
        "payload": {
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
