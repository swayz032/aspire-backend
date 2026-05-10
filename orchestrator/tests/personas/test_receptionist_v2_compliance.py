"""Pass 2 compliance tests for receptionist_v2.md.

Validates the rewritten prompt scores 28/28 on ContractValidator, contains no
bracketed audio tokens, contains no banned read-back phrases, uses the
{{agent_first_name}} placeholder, references {{industry}}, and contains no
hardcoded vertical names.

Aspire Law #2: these tests generate no receipts (read-only assertion suite).
All external imports are stdlib only — no live DB or EL API calls.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PROMPT_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "aspire_orchestrator"
    / "config"
    / "personas"
    / "receptionist_v2.md"
)

# Minimal agent config that satisfies all config-level rules.
_VALID_AGENT_CFG: Final[dict[str, object]] = {
    "agent_id": "agent_4801kqtapvsre2gb0gyb1ng631qr",
    "display_name": "Tiffany",
    "text_normalisation_type": "system_prompt",
    "model_rationale": (
        "Gemini 3.1 Flash Lite — low-latency receptionist with strong tool-calling."
    ),
    "voice": {
        "tts_model_family": "v3_conversational",
        "suggested_audio_tags": [
            {
                "tag": "Warmly",
                "description": "Standard greeting on inbound; closing line after a routine call.",
            },
            {
                "tag": "Empathetically",
                "description": (
                    "When caller reports damage, distress, or a prior bad service experience."
                ),
            },
            {
                "tag": "Confidently",
                "description": (
                    "When stating a firm policy: license number, warranty terms, dispatch ETA."
                ),
            },
        ],
    },
    "enable_conversation_initiation_client_data_from_webhook": True,
    "post_call_webhook_id": "e173b1f67cb04153b69ad27894727372",
    "tools": [
        {"name": "transfer_to_number"},
        {"name": "capture_message"},
        {"name": "end_call"},
    ],
    "receipts_emitted": ["capture_message", "transfer_to_number", "end_call"],
}


@pytest.fixture(scope="module")
def prompt_text() -> str:
    """Load the receptionist_v2.md prompt once per module."""
    assert _PROMPT_PATH.exists(), f"Prompt file not found: {_PROMPT_PATH}"
    return _PROMPT_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def validator() -> object:
    """Instantiate ContractValidator once per module."""
    from aspire_orchestrator.services.el_contract import ContractValidator  # type: ignore[import]

    return ContractValidator()


@pytest.fixture(scope="module")
def validation_report(validator: object, prompt_text: str) -> object:
    """Run the full validation once and share the report across tests."""
    return validator.validate(  # type: ignore[union-attr]
        prompt_text=prompt_text,
        agent_config=_VALID_AGENT_CFG,
        agent_kind="receptionist",
    )


# ---------------------------------------------------------------------------
# Core compliance test
# ---------------------------------------------------------------------------


def test_receptionist_v2_validates_28_of_28(validation_report: object) -> None:
    """Prompt must achieve 28/28 with zero failing rules and no overrides needed."""
    failing = getattr(validation_report, "failing_rules", [])
    score: str = getattr(validation_report, "score", "")

    assert failing == [], (
        f"Expected zero failing rules but got: "
        + ", ".join(
            f"Rule {r.id}{r.id_suffix} ({r.name}): {r.evidence}"
            for r in failing
        )
    )
    assert "28/28" in score, f"Expected score to contain '28/28', got: {score!r}"


# ---------------------------------------------------------------------------
# Audio token tests
# ---------------------------------------------------------------------------

# Bracketed tokens that audio-tag passing refers to — must not appear in prompt body.
_BRACKETED_AUDIO_PATTERN: re.Pattern[str] = re.compile(r"\[[a-z][a-z_]*\]")

_KNOWN_BRACKETED_AUDIO_TOKENS: tuple[str, ...] = (
    "[warm]",
    "[empathetic]",
    "[empathetically]",
    "[bold]",
    "[laughs]",
    "[laughing]",
    "[sighs]",
    "[reassuring]",
    "[apologetic]",
    "[enthusiastic]",
    "[slow]",
    "[curious]",
    "[professional]",
    "[chuckles]",
    "[seriously]",
    "[patiently]",
    "[confidently]",
    "[excitedly]",
    "[thoughtfully]",
    "[slowly]",
    "[apologetically]",
    "[reassuringly]",
    "[curiously]",
    "[professionally]",
)


def test_no_bracketed_audio_tokens_in_prompt_body(prompt_text: str) -> None:
    """Rule 12: zero bracketed audio cue tokens anywhere in the prompt body."""
    matches: list[str] = _BRACKETED_AUDIO_PATTERN.findall(prompt_text)
    assert matches == [], (
        f"Bracketed audio tokens found in prompt body (would be spoken aloud): {matches}"
    )


def test_known_audio_token_names_not_bracketed(prompt_text: str) -> None:
    """Belt-and-suspenders: none of the known token strings appear bracketed."""
    violations: list[str] = [
        tok for tok in _KNOWN_BRACKETED_AUDIO_TOKENS if tok in prompt_text
    ]
    assert violations == [], (
        f"Known audio tokens found as bracketed text: {violations}"
    )


# ---------------------------------------------------------------------------
# Read-back phrase tests
# ---------------------------------------------------------------------------

_READ_BACK_PATTERN: re.Pattern[str] = re.compile(
    r"read\s+(back|it\s+back|that\s+back)", re.IGNORECASE
)


def test_no_read_back_phrases(prompt_text: str) -> None:
    """Rule 13: 'read back', 'read it back', 'read that back' must not appear."""
    matches: list[str] = _READ_BACK_PATTERN.findall(prompt_text)
    assert matches == [], (
        f"Forbidden read-back phrase(s) found in prompt: {matches}"
    )


# ---------------------------------------------------------------------------
# Placeholder presence tests
# ---------------------------------------------------------------------------


def test_agent_first_name_placeholder_present(prompt_text: str) -> None:
    """The {{agent_first_name}} placeholder must appear at least 3 times."""
    count: int = prompt_text.count("{{agent_first_name}}")
    assert count >= 3, (
        f"{{{{agent_first_name}}}} appears {count} time(s) in prompt (minimum 3 required). "
        "Sarah-FrontDesk and Tiffany share this template; the placeholder is mandatory."
    )


def test_industry_dyn_var_present(prompt_text: str) -> None:
    """Rule 26: {{industry}} must be referenced in the prompt template."""
    assert "{{industry}}" in prompt_text, (
        "{{industry}} is missing from prompt. Rule 26 requires trade-aware vocabulary."
    )


def test_industry_specialty_dyn_var_present(prompt_text: str) -> None:
    """Rule 26: {{industry_specialty}} must be referenced in the prompt template."""
    assert "{{industry_specialty}}" in prompt_text, (
        "{{industry_specialty}} is missing from prompt. Rule 26 requires trade-aware vocabulary."
    )


# ---------------------------------------------------------------------------
# Hardcoded vertical name tests
# ---------------------------------------------------------------------------

_BANNED_VERTICALS: tuple[str, ...] = (
    "HVAC",
    "Electrician",
    "Plumber",
    "Painting",
    "Remodeler",
    "electrician",
    "plumber",
    "specialty_remodeler",
    "specialty remodeler",
)


def test_no_hardcoded_vertical_in_prompt(prompt_text: str) -> None:
    """Rule 26: no hardcoded trade/vertical names — use {{industry}} instead."""
    violations: list[str] = [v for v in _BANNED_VERTICALS if v in prompt_text]
    assert violations == [], (
        f"Hardcoded vertical name(s) found in prompt (use {{{{industry}}}} instead): "
        f"{violations}"
    )


# ---------------------------------------------------------------------------
# Other mandatory rules verified as plain text assertions
# ---------------------------------------------------------------------------


def test_capture_first_rule_explicit(prompt_text: str) -> None:
    """Rule 24: capture-first instruction must be present for transfer_to_number."""
    assert "capture" in prompt_text.lower() and "before" in prompt_text.lower(), (
        "Prompt does not contain capture-before-transfer instruction (rule 24)."
    )


def test_ai_disclosure_only_when_asked(prompt_text: str) -> None:
    """Rule 20: AI disclosure must be scoped to 'only when asked'."""
    pattern: re.Pattern[str] = re.compile(
        r"(only\s+when\s+asked|when\s+directly\s+asked|if\s+(a\s+caller|someone|they)\s+ask)",
        re.IGNORECASE,
    )
    assert pattern.search(prompt_text) is not None, (
        "Prompt is missing 'only when asked' scoping for AI disclosure (rule 20)."
    )


def test_closing_single_utterance_rule(prompt_text: str) -> None:
    """Rule 21: prompt must instruct agent to say closing once and stop."""
    pattern: re.Pattern[str] = re.compile(
        r"(say.{0,30}once|do\s+not\s+(speak|continue|say).{0,30}after)",
        re.IGNORECASE,
    )
    assert pattern.search(prompt_text) is not None, (
        "Prompt is missing 'say closing once / do not continue after' instruction (rule 21)."
    )


def test_escalation_path_in_guardrails_or_error_handling(prompt_text: str) -> None:
    """Rule 23: escalation path must appear in Guardrails or Error handling section."""
    # Extract Guardrails + Error handling sections
    guardrails_match = re.search(
        r"^#\s+Guardrails\s*$(.+?)(?=^#\s|\Z)", prompt_text, re.MULTILINE | re.DOTALL | re.IGNORECASE
    )
    error_match = re.search(
        r"^#\s+Error\s+handling\s*$(.+?)(?=^#\s|\Z)", prompt_text, re.MULTILINE | re.DOTALL | re.IGNORECASE
    )
    combined: str = (
        (guardrails_match.group(1) if guardrails_match else "")
        + (error_match.group(1) if error_match else "")
    )
    pattern: re.Pattern[str] = re.compile(
        r"(escalat|transfer|human|message.{0,20}captur|voicemail|callback)", re.IGNORECASE
    )
    assert pattern.search(combined) is not None, (
        "No escalation path declared in Guardrails or Error handling (rule 23)."
    )


def test_no_other_agent_names_in_prompt(prompt_text: str) -> None:
    """Rule 14: other agents' literal names must not appear in the prompt."""
    # These are the workspace agent names per _WORKSPACE_AGENT_NAMES in el_contract.py
    other_agent_names: tuple[str, ...] = ("Tiffany", "Sarah", "Ava", "Finn", "Eli", "Nora")
    violations: list[str] = [
        name for name in other_agent_names
        if re.search(r"\b" + re.escape(name) + r"\b", prompt_text)
    ]
    assert violations == [], (
        f"Other agent names found in prompt (rule 14 — identity isolation): {violations}"
    )


def test_seven_section_headings_present(prompt_text: str) -> None:
    """Rule 1: all 7 required H1 headings must be present."""
    required_headings: tuple[str, ...] = (
        "Personality",
        "Environment",
        "Tone",
        "Goal",
        "Guardrails",
        "Tools",
        "Error handling",
    )
    missing: list[str] = []
    for heading in required_headings:
        pattern = re.compile(r"^#\s+" + re.escape(heading) + r"\s*$", re.MULTILINE | re.IGNORECASE)
        if not pattern.search(prompt_text):
            missing.append(heading)
    assert missing == [], f"Missing required H1 headings (rule 1): {missing}"
