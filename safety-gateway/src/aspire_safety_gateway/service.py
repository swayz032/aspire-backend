from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aspire_safety_gateway.config import settings
from aspire_safety_gateway.models import SafetyCheckResponse

logger = logging.getLogger(__name__)
_nemo_rails: Any | None = None
_nemo_init_attempted = False

_JAILBREAK_PATTERNS = (
    "ignore previous instructions",
    "ignore all instructions",
    "you are now",
    "pretend you are",
    "act as if",
    "disregard your rules",
    "bypass safety",
    "ignore your guidelines",
    "forget your instructions",
    "override your programming",
    "new system prompt",
    "system: you are",
    "ignore safety",
    "jailbreak",
    "dan mode",
    "developer mode",
    "do anything now",
    "sudo mode",
    "ignore all previous",
    "disregard all previous",
    "forget all previous",
    "you must obey",
    "roleplay as",
    "simulate being",
)

_TOPIC_DENY_PATTERNS = (
    "build malware",
    "steal credentials",
    "phishing campaign",
    "exfiltrate data",
    "ransomware",
)


def normalize_payload(payload: Any) -> str:
    if payload is None:
        return ""
    try:
        text = json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        text = str(payload)
    if len(text) > settings.max_payload_chars:
        text = text[: settings.max_payload_chars]
    return text.lower()


def local_screen(payload: Any) -> SafetyCheckResponse:
    haystack = normalize_payload(payload)
    for pattern in _JAILBREAK_PATTERNS:
        if pattern in haystack:
            return SafetyCheckResponse(
                allowed=False,
                reason=f"Safety gateway blocked jailbreak pattern ({pattern})",
                source="local",
                matched_rule=pattern,
                metadata={"category": "jailbreak"},
            )
    for pattern in _TOPIC_DENY_PATTERNS:
        if pattern in haystack:
            return SafetyCheckResponse(
                allowed=False,
                reason=f"Safety gateway blocked unsafe topic ({pattern})",
                source="local",
                matched_rule=pattern,
                metadata={"category": "topic"},
            )
    return SafetyCheckResponse(allowed=True, source="local", metadata={"category": "pass"})


def nemo_screen(payload: Any) -> SafetyCheckResponse:
    global _nemo_rails, _nemo_init_attempted
    try:
        from nemoguardrails import LLMRails, RailsConfig
    except Exception as exc:
        logger.warning("NeMo Guardrails unavailable, using local fallback: %s", exc)
        return local_screen(payload)

    if not _nemo_init_attempted:
        _nemo_init_attempted = True
        try:
            config_path = Path(settings.nemo_config_path)
            rails_config = RailsConfig.from_path(str(config_path))
            _nemo_rails = LLMRails(rails_config)
        except Exception as exc:
            logger.warning("NeMo Guardrails config failed to initialize, using local fallback: %s", exc)
            _nemo_rails = None

    if _nemo_rails is None:
        return local_screen(payload).model_copy(update={"source": "nemo-fallback"})

    try:
        payload_text = normalize_payload(payload)
        response = _nemo_rails.generate(messages=[{"role": "user", "content": payload_text}])
        if isinstance(response, dict):
            content = str(response.get("content") or response.get("text") or "")
        else:
            content = str(response or "")

        refusal = (settings.nemo_refusal_contains or "").strip().lower()
        if refusal and refusal in content.lower():
            return SafetyCheckResponse(
                allowed=False,
                reason="Safety gateway blocked by NeMo Guardrails",
                source="nemo",
                matched_rule="nemo_refusal",
                metadata={"raw_response": content},
            )
        return SafetyCheckResponse(
            allowed=True,
            source="nemo",
            metadata={"raw_response": content},
        )
    except Exception as exc:
        logger.warning("NeMo Guardrails evaluation failed, using local fallback: %s", exc)
        return local_screen(payload).model_copy(update={"source": "nemo-fallback"})


def screen_payload(payload: Any) -> SafetyCheckResponse:
    mode = (settings.mode or "local").strip().lower()
    if mode == "nemo":
        return nemo_screen(payload)
    return local_screen(payload)
