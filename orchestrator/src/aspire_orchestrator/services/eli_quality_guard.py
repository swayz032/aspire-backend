"""Eli Quality Guard — deterministic email quality scoring + policy gates.

This service gives Eli a production-grade drafting gate for email.draft/email.send:
  - Subject/body structure checks
  - Call-to-action presence
  - Length and clarity heuristics
  - Config-driven thresholds from config/pack_policies/eli/autonomy_policy.yaml
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_POLICY_PATH = (
    Path(__file__).parent.parent
    / "config"
    / "pack_policies"
    / "eli"
    / "autonomy_policy.yaml"
)


@dataclass(frozen=True)
class EliQualityReport:
    score: int
    passed: bool
    violations: list[str]
    warnings: list[str]
    body_word_count: int
    subject_length: int


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_text(payload: dict[str, Any]) -> tuple[str, str]:
    subject = str(payload.get("subject", "")).strip()
    body_text = str(payload.get("body_text", "")).strip()
    body_html = str(payload.get("body_html", "")).strip()
    body = body_text or _strip_html(body_html)
    return subject, body


def _has_call_to_action(text: str) -> bool:
    lower = text.lower()
    cta_markers = (
        "please",
        "let me know",
        "can you",
        "could you",
        "reply by",
        "by ",
        "next step",
        "confirm",
        "approve",
        "review",
        "send",
        "share",
    )
    return any(marker in lower for marker in cta_markers)


def _has_greeting(text: str) -> bool:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False
    return bool(re.match(r"^(dear|hello|hi)\b", lines[0], re.IGNORECASE))


def _has_signoff(text: str) -> bool:
    lower = (text or "").strip().lower()
    signoffs = ("\n\nbest,", "\n\nbest regards,", "\n\nregards,", "\n\nthanks,", "\n\nsincerely,", "\n\ncheers,")
    return any(marker in lower for marker in signoffs)


def _is_purpose_first(text: str) -> bool:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False
    start_idx = 1 if re.match(r"^(dear|hello|hi)\b", lines[0], re.IGNORECASE) and len(lines) > 1 else 0
    sentence = re.split(r"[.!?]", lines[start_idx], maxsplit=1)[0].strip().lower()
    if not sentence:
        return False
    if sentence.startswith(("hope this email finds", "hope you're well", "hope you are well", "just circling back")):
        return False
    purpose_markers = (
        "following up",
        "reaching out",
        "wanted to",
        "writing to",
        "quick update",
        "confirm",
        "request",
        "schedule",
        "regarding",
        "about",
        "share",
    )
    return any(marker in sentence for marker in purpose_markers)


def _has_emoji(text: str) -> bool:
    return bool(re.search(r"[\U0001F300-\U0001FAFF]", text or ""))


def _has_slang(text: str) -> bool:
    return bool(re.search(r"\b(gonna|wanna|kinda|sorta|lol|thx|pls)\b", (text or "").lower()))


@lru_cache(maxsize=1)
def load_eli_autonomy_policy() -> dict[str, Any]:
    """Load Eli autonomy/quality policy from YAML with safe defaults."""
    default_policy = {
        "quality_gates": {
            "draft": {
                "min_score": 78,
                "min_word_count": 30,
                "require_call_to_action": True,
                "max_subject_chars": 72,
                "min_subject_chars": 6,
                "require_greeting": True,
                "require_signoff": True,
                "require_purpose_first": True,
                "block_emojis": True,
                "block_slang": True,
            },
            "send": {
                "min_score": 82,
                "min_word_count": 35,
                "require_call_to_action": True,
                "max_subject_chars": 72,
                "min_subject_chars": 6,
                "require_greeting": True,
                "require_signoff": True,
                "require_purpose_first": True,
                "block_emojis": True,
                "block_slang": True,
            },
        },
    }
    if not _POLICY_PATH.exists():
        return default_policy
    try:
        raw = yaml.safe_load(_POLICY_PATH.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return default_policy
        gates = raw.get("quality_gates", {})
        if not isinstance(gates, dict):
            raw["quality_gates"] = default_policy["quality_gates"]
        return raw
    except Exception:
        return default_policy


def evaluate_email_quality(
    *,
    payload: dict[str, Any],
    mode: str = "draft",
) -> EliQualityReport:
    """Evaluate outbound email payload quality using deterministic heuristics."""
    policy = load_eli_autonomy_policy()
    gates = policy.get("quality_gates", {}) if isinstance(policy, dict) else {}
    gate = gates.get(mode, gates.get("draft", {})) if isinstance(gates, dict) else {}

    min_score = int(gate.get("min_score", 78))
    min_words = int(gate.get("min_word_count", 30))
    require_cta = bool(gate.get("require_call_to_action", True))
    max_subject_chars = int(gate.get("max_subject_chars", 72))
    min_subject_chars = int(gate.get("min_subject_chars", 6))
    require_greeting = bool(gate.get("require_greeting", True))
    require_signoff = bool(gate.get("require_signoff", True))
    require_purpose_first = bool(gate.get("require_purpose_first", True))
    block_emojis = bool(gate.get("block_emojis", True))
    block_slang = bool(gate.get("block_slang", True))

    subject, body = _extract_text(payload)
    word_count = len(re.findall(r"\b[\w'-]+\b", body))
    subject_len = len(subject)

    score = 100
    violations: list[str] = []
    warnings: list[str] = []

    if subject_len < min_subject_chars:
        score -= 20
        violations.append(f"subject too short (<{min_subject_chars} chars)")
    elif subject_len > max_subject_chars:
        score -= 15
        warnings.append(f"subject too long (>{max_subject_chars} chars)")

    if word_count < min_words:
        score -= 25
        violations.append(f"body too short (<{min_words} words)")

    if require_cta and not _has_call_to_action(body):
        score -= 20
        violations.append("missing clear call-to-action")
    if require_greeting and not _has_greeting(body):
        score -= 10
        violations.append("missing professional greeting")
    if require_signoff and not _has_signoff(body):
        score -= 10
        violations.append("missing professional sign-off")
    if require_purpose_first and not _is_purpose_first(body):
        score -= 10
        violations.append("first sentence is not purpose-first")
    if block_emojis and _has_emoji(body):
        score -= 10
        violations.append("contains emoji")
    if block_slang and _has_slang(body):
        score -= 10
        violations.append("contains casual slang")

    # Anti-slop and professionalism checks.
    lower = body.lower()
    if "as an ai" in lower or "i cannot access" in lower:
        score -= 25
        violations.append("contains non-user-facing AI/system phrasing")
    if "hope this email finds you well" in lower:
        score -= 5
        warnings.append("generic opener detected")

    score = max(0, min(100, score))
    passed = score >= min_score and len(violations) == 0

    return EliQualityReport(
        score=score,
        passed=passed,
        violations=violations,
        warnings=warnings,
        body_word_count=word_count,
        subject_length=subject_len,
    )
