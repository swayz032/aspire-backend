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
            },
            "send": {
                "min_score": 82,
                "min_word_count": 35,
                "require_call_to_action": True,
                "max_subject_chars": 72,
                "min_subject_chars": 6,
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
