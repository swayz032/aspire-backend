"""Canonical agent identity resolution for conversational and response paths.

This module centralizes agent identity selection so every node uses the same
decision order and mappings. It prevents persona drift where specialist agents
accidentally respond as Ava.
"""

from __future__ import annotations

from typing import Any

# Task domain prefix -> owning agent.
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
    "finance": "finn",
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

# Accepted aliases -> canonical public agent identity.
_AGENT_ALIASES: dict[str, str] = {
    "finn_fm": "finn",
}

_KNOWN_AGENTS = {
    "ava",
    "finn",
    "eli",
    "nora",
    "sarah",
    "adam",
    "quinn",
    "tec",
    "teressa",
    "milo",
    "clara",
    "mail_ops",
}


def _normalize_agent(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    raw = _AGENT_ALIASES.get(raw, raw)
    if raw in _KNOWN_AGENTS:
        return raw
    return None


def _request_agent_from_state(state: dict[str, Any]) -> str | None:
    request = state.get("request")
    explicit_agent = None
    if isinstance(request, dict):
        explicit_agent = request.get("requested_agent") or request.get("agent")
    elif hasattr(request, "payload") and isinstance(request.payload, dict):
        explicit_agent = request.payload.get("requested_agent") or request.payload.get("agent")
    return _normalize_agent(explicit_agent)


def resolve_assigned_agent(state: dict[str, Any]) -> str:
    """Resolve user-facing agent identity shown to clients."""
    explicit = _request_agent_from_state(state)
    if explicit:
        return explicit

    requested = _normalize_agent(state.get("requested_agent"))
    if requested:
        return requested

    target = _normalize_agent(state.get("agent_target"))
    if target:
        return target

    task_type = str(state.get("task_type", "unknown"))
    domain_prefix = task_type.split(".", 1)[0]
    return _DOMAIN_TO_AGENT.get(domain_prefix, "ava")


def resolve_persona_agent(state: dict[str, Any]) -> str:
    """Resolve persona id used for prompt file selection."""
    assigned = resolve_assigned_agent(state)
    if assigned == "finn":
        return "finn_fm"
    return assigned
