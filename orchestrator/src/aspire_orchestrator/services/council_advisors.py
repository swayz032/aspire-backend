"""Council Advisors — Multi-model API calls for Meeting of Minds.

Each advisor receives a read-only evidence pack and returns a structured
triage proposal. Advisors NEVER execute — they only analyze (Law #7).

Models:
  - GPT-5.2 (OpenAI): Architecture critic, root cause analysis
  - Gemini 3 (Google): Research cross-check, alternative approaches
  - Opus 4.6 (Anthropic): Implementation planning

Budget: Configurable per-session, default $5 for testing.
Timeout: 30s per advisor call.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_ADVISOR_SYSTEM_PROMPT = """You are a council advisor in the Aspire platform's Meeting of Minds.
You receive an evidence pack about a platform incident and must produce a structured triage proposal.

You are READ-ONLY. You cannot execute any actions. You analyze and propose.

Respond with ONLY a JSON object (no markdown, no explanation) with these fields:
{
  "root_cause": "Your analysis of the root cause",
  "fix_plan": "Step-by-step fix plan",
  "tests": ["test_names to validate the fix"],
  "risk_tier": "green|yellow|red",
  "confidence": 0.0-1.0,
  "reasoning": "Why you believe this diagnosis"
}"""

_MODEL_MAP = {
    "gpt": {"provider": "openai", "model": "gpt-5.2", "role": "Architecture critic, root cause analysis"},
    "gemini": {"provider": "google", "model": "gemini-3", "role": "Research cross-check, alternative approaches"},
    "claude": {"provider": "anthropic", "model": "claude-opus-4-6", "role": "Implementation planning"},
}

_TIMEOUT_SECONDS = 30


def _resolve_api_key(*env_names: str) -> str:
    """Resolve an API key from environment variables (secrets injected at startup)."""
    for name in env_names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    raise ValueError(f"API key not configured. Checked: {', '.join(env_names)}")


async def _call_openai(prompt: str, model: str = "gpt-5.2") -> dict[str, Any]:
    """Call OpenAI API for GPT advisor."""
    import httpx

    api_key = _resolve_api_key("ASPIRE_OPENAI_API_KEY", "OPENAI_API_KEY")

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _ADVISOR_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 1000,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)


async def _call_google(prompt: str, model: str = "gemini-3") -> dict[str, Any]:
    """Call Google Generative AI API for Gemini advisor."""
    import httpx

    api_key = _resolve_api_key("ASPIRE_GOOGLE_API_KEY", "GOOGLE_API_KEY")

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": f"{_ADVISOR_SYSTEM_PROMPT}\n\n{prompt}"}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1000},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(text)


async def _call_anthropic(prompt: str, model: str = "claude-opus-4-6") -> dict[str, Any]:
    """Call Anthropic API for Claude advisor."""
    import httpx

    api_key = _resolve_api_key("ASPIRE_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1000,
                "system": _ADVISOR_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"]
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(text)


_PROVIDER_MAP = {
    "openai": _call_openai,
    "google": _call_google,
    "anthropic": _call_anthropic,
}


async def query_advisor(
    *,
    advisor: str,
    evidence_pack: dict[str, Any],
    incident_id: str,
) -> dict[str, Any]:
    """Query a single council advisor and return structured proposal.

    Returns dict with: advisor, root_cause, fix_plan, tests, risk_tier,
    confidence, reasoning, model_used, tokens_used, latency_ms.
    """
    config = _MODEL_MAP.get(advisor)
    if not config:
        raise ValueError(f"Unknown advisor: {advisor}. Valid: {list(_MODEL_MAP.keys())}")

    prompt = (
        f"Incident ID: {incident_id}\n"
        f"Your role: {config['role']}\n\n"
        f"Evidence pack:\n{json.dumps(evidence_pack, indent=2, default=str)}\n\n"
        "Analyze this incident and produce your triage proposal as JSON."
    )

    # Look up provider function dynamically so mocks on module attributes work
    import sys
    _this_module = sys.modules[__name__]
    fn_name = {"openai": "_call_openai", "google": "_call_google", "anthropic": "_call_anthropic"}[config["provider"]]
    provider_fn = getattr(_this_module, fn_name)
    start = time.monotonic()

    try:
        result = await provider_fn(prompt, config["model"])
    except Exception as e:
        logger.error("Council advisor %s failed: %s", advisor, e)
        return {
            "advisor": advisor,
            "model_used": config["model"],
            "root_cause": f"Advisor {advisor} failed to respond: {e}",
            "fix_plan": "",
            "tests": [],
            "risk_tier": "yellow",
            "confidence": 0.0,
            "reasoning": f"Error: {e}",
            "tokens_used": 0,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "error": str(e),
        }

    latency_ms = int((time.monotonic() - start) * 1000)

    return {
        "advisor": advisor,
        "model_used": config["model"],
        "root_cause": result.get("root_cause", ""),
        "fix_plan": result.get("fix_plan", ""),
        "tests": result.get("tests", []),
        "risk_tier": result.get("risk_tier", "yellow"),
        "confidence": float(result.get("confidence", 0.5)),
        "reasoning": result.get("reasoning", ""),
        "tokens_used": 0,
        "latency_ms": latency_ms,
    }
