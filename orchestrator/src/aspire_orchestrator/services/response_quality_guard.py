"""Shared conversational response quality guard.

Applies bounded, deterministic quality checks to user-facing responses so
packs do not rely on persona text alone for natural language behavior.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResponseQualityReport:
    score: int
    passed: bool
    violations: list[str]
    warnings: list[str]
    style_signals: list[str]
    normalized_text: str


_AI_PHRASES = (
    "as an ai",
    "i cannot access",
    "i'm just an ai",
    "language model",
)
_ROBOTIC_OPENERS = (
    "certainly,",
    "absolutely,",
    "i'd be happy to",
    "i can certainly help",
)
_PHANTOM_MARKERS = (
    "i've sent",
    "i've created",
    "i've scheduled",
    "i've processed",
    "invoice sent",
    "payment processed",
    "meeting scheduled",
)


def _normalize_whitespace(text: str) -> str:
    lines = [line.rstrip() for line in (text or "").splitlines()]
    collapsed = "\n".join(lines).strip()
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed)
    return collapsed


def evaluate_response_quality(
    *,
    text: str,
    channel: str = "chat",
    agent_id: str = "",
    prompt_contract: str = "",
    allow_markdown: bool = True,
) -> ResponseQualityReport:
    normalized = _normalize_whitespace(text)
    lower = normalized.lower()

    score = 100
    violations: list[str] = []
    warnings: list[str] = []
    style_signals: list[str] = []

    if any(phrase in lower for phrase in _AI_PHRASES):
        score -= 30
        violations.append("contains non-user-facing AI/system phrasing")

    if any(marker in lower for marker in _PHANTOM_MARKERS):
        score -= 35
        violations.append("contains unsupported execution claim")

    opener = lower.splitlines()[0] if lower.splitlines() else lower
    if any(opener.startswith(prefix) for prefix in _ROBOTIC_OPENERS):
        score -= 10
        warnings.append("generic robotic opener")

    if re.search(r"([!?.,])\1{2,}", normalized):
        score -= 10
        warnings.append("excessive punctuation")

    if channel in {"voice", "avatar"}:
        sentence_count = len([s for s in re.split(r"[.!?]+", normalized) if s.strip()])
        if sentence_count > 3:
            score -= 20
            violations.append("voice response exceeds 3 sentences")
        if re.search(r"(^|\n)[*-]\s", normalized):
            score -= 10
            violations.append("voice response uses bullets")
        style_signals.append("voice_compact")
    else:
        if not allow_markdown and re.search(r"(^|\n)[*-]\s", normalized):
            score -= 5
            warnings.append("unexpected markdown bullets")
        style_signals.append("chat_structured")

    if prompt_contract:
        contract_lower = prompt_contract.lower()
        if "never claim execution without receipt" in contract_lower and any(
            marker in lower for marker in _PHANTOM_MARKERS
        ):
            score -= 15
        if "concise operational language" in contract_lower and len(normalized.split()) > 220:
            score -= 10
            warnings.append("too verbose for operational style")

    if agent_id and re.search(rf"\b{i_escape(agent_id)}\b", lower):
        style_signals.append("self_identity_present")

    score = max(0, min(100, score))
    return ResponseQualityReport(
        score=score,
        passed=len(violations) == 0,
        violations=violations,
        warnings=warnings,
        style_signals=style_signals,
        normalized_text=normalized,
    )


def i_escape(value: str) -> str:
    return re.escape(value.lower())


def enforce_response_quality(
    *,
    text: str,
    channel: str = "chat",
    agent_id: str = "",
    prompt_contract: str = "",
    fallback_text: str | None = None,
    allow_markdown: bool = True,
) -> tuple[str, ResponseQualityReport]:
    report = evaluate_response_quality(
        text=text,
        channel=channel,
        agent_id=agent_id,
        prompt_contract=prompt_contract,
        allow_markdown=allow_markdown,
    )
    if report.passed:
        return report.normalized_text, report

    safe_fallback = fallback_text or (
        "I can help with that, but I need to stay grounded in what I can actually confirm. "
        "Tell me the next step you want and I'll keep it precise."
    )
    return _normalize_whitespace(safe_fallback), report
