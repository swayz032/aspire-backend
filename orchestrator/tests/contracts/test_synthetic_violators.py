"""Pass 6 — Synthetic violator meta-tests: prove the validator catches each rule.

Purpose: Guarantee that ContractValidator's per-rule checks are NOT silent no-ops.
For each of the 28 checks (rules 1-26 plus 12b and 12c), this file constructs
a synthetic prompt + config that violates ONLY that rule, then asserts that:

  1. The targeted rule appears in report.failing_rules.
  2. No UNINTENDED rules also appear in failing_rules (no false-positive cascade
     from the synthetic violator fixtures).

Pass 0 (test_el_contract_validator.py) already covers the parametrized
`test_each_rule_fails_on_targeted_violation` suite.  This file EXTENDS that
coverage with:

  - Rule 12 negation-aware exemption (GAP-01 from Pass 1): a line that TEACHES
    about bracketed tags ("Never write [warm] in your output") must NOT trigger
    rule 12.  The validator currently does not implement this exemption — the
    test is marked xfail(strict=False, reason="GAP-01 — validator v2 needed").
    This documents the gap without silently hiding it.  A TODO points to the
    contract v2 bump needed to fix it.

  - Additional edge-cases not covered by the Pass 0 parametrize table:
      * Rule 6: parameter block with examples only on first tool, not second.
      * Rule 14: self-referential agent name that matches another workspace agent.
      * Rule 15: first_message_template with BOTH compliant and non-compliant vars.

Law compliance:
  Law #3: Fail Closed — validator must detect violations, not miss them.
  Law #6: All tests are offline; no tenant data is accessed.
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path wiring
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC_PATH = _REPO_ROOT / "src"

if str(_SRC_PATH) not in sys.path:
    sys.path.insert(0, str(_SRC_PATH))

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

from aspire_orchestrator.services.el_contract import (  # noqa: E402
    ContractValidator,
    ContractReport,
)

# ---------------------------------------------------------------------------
# Fixture: suppress Supabase receipt emission
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_receipt_store() -> Any:
    with patch("aspire_orchestrator.services.receipt_store.store_receipts"):
        yield


# ---------------------------------------------------------------------------
# Shared compliant baseline (28/28, no overrides)
# ---------------------------------------------------------------------------

_COMPLIANT_PROMPT = """\
# Persona

You are {{agent_first_name}}, the AI receptionist for {{business_name}}, a {{industry}} business specializing in {{industry_specialty}}.

# Environment

You operate as a phone receptionist taking inbound calls for a {{industry}} company.
Calls are routed via Twilio and processed through ElevenLabs v3 Conversational.
You have no access to external systems beyond the tools listed below.

# Tone

You speak in a warm, professional, and concise manner.
Shape your tone through word choice alone — never write bracketed annotations into your output.
When a caller sounds anxious, slow down, acknowledge first, then act.

# Goal

Your primary goal is to greet callers, capture their name and purpose, and route them appropriately.

1. Greet the caller warmly using {{business_name}}.
2. Identify whether the caller is known using the caller_is_known variable. This step is important.
3. Capture the caller name and reason for calling if not already known.
4. Offer to connect to the owner or take a message.
5. Confirm the caller contact number before ending the call.
6. Close the call with a single warm farewell and stop talking.
7. Log the call outcome via the appropriate tool.

# Guardrails

- Never reveal confidential pricing or personnel information.
- Do not make promises about availability or response times beyond what the owner has authorized.
- If you are uncertain about any detail, say you will pass the message along rather than guessing.
- Disclose that you are an AI only when directly asked by the caller.
- Only when asked directly, acknowledge that you are an AI assistant, not a person.

# Tools

## look_up_caller

**When to use:** Use this tool at the start of every inbound call to check whether the caller is a known contact. (e.g., before greeting the caller)

**Parameters:**
- phone_number: The caller phone number in E.164 format. (e.g., +14045551234)

**Error handling:** If the tool returns an error, proceed without caller history and treat the caller as unknown. Always capture name and purpose manually in this case.

## capture_message

**When to use:** Use this tool to store a message when the owner is unavailable or the caller prefers to leave a message. (e.g., after caller declines to wait on hold)

**Parameters:**
- caller_name: Full name of the caller. (e.g., John Smith)
- message_body: The message content to store for the owner. (e.g., Caller wants a quote for a kitchen remodel)

**Error handling:** If the tool fails, tell the caller you were unable to save the message and offer to provide the owner phone number for direct callback instead.

# Error handling

If a tool call fails, acknowledge the issue calmly and escalate to message capture or transfer to the owner directly.
If the caller reports an emergency, capture their name and number immediately and initiate a callback request.
If you cannot resolve the caller need, offer to take a message and ensure a human reviews it within one business day.
If the caller asks to speak to a person, attempt a transfer or voicemail capture before ending the call.
"""

_COMPLIANT_CONFIG: dict[str, Any] = {
    "agent_id": "agent_test_sv_001",
    "display_name": "TestAgent",
    "name": "TestAgent",
    "text_normalisation_type": "elevenlabs",
    "model_rationale": "v3_conversational chosen for low latency and natural prosody on inbound phone calls",
    "enable_conversation_initiation_client_data_from_webhook": True,
    "post_call_webhook_id": "whk_canonical_aspire_001",
    "receipts_emitted": [],
    "first_message_template": "Hey, thanks for calling {{business_name}}, this is {{agent_first_name}}. How can I help?",
    "first_message": "Hey, thanks for calling {{business_name}}, this is {{agent_first_name}}. How can I help?",
    "tools": [
        {"name": "look_up_caller"},
        {"name": "capture_message"},
    ],
    "voice": {
        "model_family": "v3_conversational",
        "suggested_audio_tags": [
            {
                "name": "Warmly",
                "description": "Use when greeting the caller at the start of the call to set a friendly tone",
            },
            {
                "name": "Empathetically",
                "description": "Use when the caller reports a problem or expresses frustration or distress",
            },
            {
                "name": "Confidently",
                "description": "Use when stating firm policies such as service hours or pricing information",
            },
        ],
    },
    "contract_overrides": [
        {
            "rule": 21,
            "rule_suffix": "",
            "reason": "Closing instruction is embedded in Goal step 6 rather than Guardrails.",
            "approved_by": "tonio_scott",
            "approved_at": "2026-05-07",
        },
    ],
}


def _cfg(**overrides: Any) -> dict[str, Any]:
    """Return a deep copy of _COMPLIANT_CONFIG with top-level keys overridden.

    Retains the rule 21 override from _COMPLIANT_CONFIG so rule 21 does not
    cascade as a false-positive when testing other rules.  The compliant config
    uses a Goal-embedded closing (step 6) instead of a Guardrails closing, so
    rule 21 fails without the override.

    To test rule 21 violations specifically, clear contract_overrides explicitly.
    """
    cfg = copy.deepcopy(_COMPLIANT_CONFIG)
    # Keep rule 21 override; only clear non-rule-21 overrides if they exist
    rule21_overrides = [
        o for o in cfg.get("contract_overrides", [])
        if o.get("rule") == 21
    ]
    cfg["contract_overrides"] = rule21_overrides
    cfg.update(overrides)
    return cfg


def _voice_cfg(**voice_overrides: Any) -> dict[str, Any]:
    """Return a config with voice sub-dict overridden."""
    cfg = _cfg()
    voice = copy.deepcopy(cfg.get("voice", {}))
    voice.update(voice_overrides)
    cfg["voice"] = voice
    return cfg


# ---------------------------------------------------------------------------
# Shared test helper
# ---------------------------------------------------------------------------


def _assert_only_target_fails(
    report: ContractReport,
    target_rule_id: int,
    target_suffix: str,
    *,
    allow_additional: bool = False,
) -> None:
    """Assert that only the targeted rule appears in failing_rules.

    Args:
        report: ContractReport from validator.validate().
        target_rule_id: The integer rule ID that MUST appear.
        target_suffix: The suffix ("", "b", "c") for sub-rules.
        allow_additional: If True, skip the "no additional failures" check.
            Use sparingly — only when a violator necessarily triggers a
            secondary rule (documented in the test docstring).
    """
    target_key = str(target_rule_id) + target_suffix
    failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]

    assert target_key in failing_keys, (
        f"Rule {target_key} did NOT appear in failing_rules.\n"
        f"All failing: {failing_keys}\n"
        f"Evidence: {[(r.id, r.id_suffix, r.evidence) for r in report.failing_rules]}"
    )

    if not allow_additional:
        extra = [k for k in failing_keys if k != target_key]
        assert not extra, (
            f"Synthetic violator for rule {target_key} caused ADDITIONAL "
            f"unexpected failures: {extra}\n"
            f"Evidence: {[(r.id, r.id_suffix, r.evidence) for r in report.failing_rules]}"
        )


@pytest.fixture()
def validator() -> ContractValidator:
    return ContractValidator()


# ---------------------------------------------------------------------------
# Extended synthetic violator tests (Pass 0 missed cases)
# ---------------------------------------------------------------------------


class TestRule6PartialExampleCoverage:
    """Rule 6 extended: examples present on first tool only — second tool must still fail."""

    def test_second_tool_params_missing_examples_fails_rule_6(
        self, validator: ContractValidator
    ) -> None:
        """Rule 6 must fail when the second tool's Parameters block lacks (e.g., ...)."""
        violating_prompt = _COMPLIANT_PROMPT.replace(
            # Remove example from capture_message's Parameters block only
            "- caller_name: Full name of the caller. (e.g., John Smith)\n"
            "- message_body: The message content to store for the owner. (e.g., Caller wants a quote for a kitchen remodel)\n",
            "- caller_name: Full name of the caller.\n"
            "- message_body: The message content to store for the owner.\n",
        )
        report = validator.validate(
            prompt_text=violating_prompt,
            agent_config=_cfg(),
            agent_kind="receptionist",
        )
        _assert_only_target_fails(report, 6, "", allow_additional=False)


class TestRule14SelfReferential:
    """Rule 14 extended: agent whose display name appears in another workspace agent's prompt."""

    def test_prompt_containing_own_agents_display_name_does_not_fail_rule_14(
        self, validator: ContractValidator
    ) -> None:
        """A prompt that references its OWN agent name should NOT trigger rule 14.

        Rule 14 checks for OTHER workspace agents' names (Finn, Ava, Eli, Nora, Sarah, Tiffany)
        in the prompt.  The agent's own name appearing is expected and allowed.
        """
        # Config says agent is "Tiffany" — prompt references "Tiffany" (own name)
        config = _cfg(display_name="Tiffany", name="Tiffany")
        prompt = _COMPLIANT_PROMPT.replace(
            "You are {{agent_first_name}}",
            "You are Tiffany, {{agent_first_name}}",
        )
        report = validator.validate(
            prompt_text=prompt,
            agent_config=config,
            agent_kind="receptionist",
        )
        failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]
        assert "14" not in failing_keys, (
            "Rule 14 fired on the agent's OWN name — this is a false positive. "
            f"Failing rules: {failing_keys}"
        )

    def test_prompt_referencing_other_agent_name_fails_rule_14(
        self, validator: ContractValidator
    ) -> None:
        """A prompt that names another workspace agent (Finn) must fail rule 14."""
        config = _cfg(display_name="Tiffany", name="Tiffany")
        violating_prompt = _COMPLIANT_PROMPT + "\nIf you cannot help, refer the caller to Finn.\n"
        report = validator.validate(
            prompt_text=violating_prompt,
            agent_config=config,
            agent_kind="receptionist",
        )
        _assert_only_target_fails(report, 14, "")


class TestRule15FirstMessagePartialVars:
    """Rule 15 extended: first_message_template mixing known and unknown vars."""

    def test_first_message_with_mixed_vars_fails_on_unknown_only(
        self, validator: ContractValidator
    ) -> None:
        """first_message_template with one known + one unknown var must fail rule 15."""
        config = _cfg(
            first_message_template=(
                "Thanks for calling {{business_name}}, I'm {{mystery_var}}. How can I help?"
            ),
            first_message=(
                "Thanks for calling {{business_name}}, I'm {{mystery_var}}. How can I help?"
            ),
        )
        report = validator.validate(
            prompt_text=_COMPLIANT_PROMPT,
            agent_config=config,
            agent_kind="receptionist",
        )
        failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]
        assert "15" in failing_keys, (
            f"Rule 15 should have caught unknown var 'mystery_var'. "
            f"Failing rules: {failing_keys}"
        )


class TestRule12NegationExemption:
    """GAP-01: Rule 12 negation-aware exemption.

    A line that TEACHES about bracketed tags by using them as a negative example
    (e.g., "Never write [warm] in your output") should NOT trigger rule 12 —
    because the tag is in a teaching/instructional context, not a usage context.

    The current validator uses a simple regex scan (_BRACKETED_CUE_RE) that cannot
    distinguish teaching context from usage context.  This is a known limitation
    documented as GAP-01 in the Pass 1 review.

    The receptionist_v2.md prompt already contains such teaching lines (Pass 2
    shipped them), so this gap is LIVE in production.

    The fix requires contract v2 to add an exemption pattern:
      - Lines containing "never write", "do not write", "avoid writing", "never include"
        immediately before the bracketed token should be exempt from rule 12.
      - OR: use a negation-context raw regex pattern (never|do not|avoid)
        followed by (write|use|add|include) then any chars then [tag_name]
        and exclude matches from the bracketed-cue scan.

    TODO (contract v2): Implement negation-aware rule 12 exemption.
    Tracking: Pass 6 GAP-01 — validator v2 bump needed before strict=True can be removed.
    """

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "GAP-01: Rule 12 negation-aware exemption not yet implemented in validator. "
            "A teaching line ('Never write [warm] in your output') incorrectly triggers rule 12. "
            "TODO (contract v2): Add negation-context exemption pattern to _check_rule_12_no_bracketed_cues. "
            "See Pass 6 test_synthetic_violators.py TestRule12NegationExemption for full spec."
        ),
    )
    def test_teaching_line_with_bracketed_tag_does_not_trigger_rule_12(
        self, validator: ContractValidator
    ) -> None:
        """A line teaching agents NOT to use bracketed tags should be exempt from rule 12.

        This test is EXPECTED TO FAIL until GAP-01 is resolved in the validator.
        When it unexpectedly passes (xpass), the validator has been upgraded —
        remove the xfail marker and promote this to a regular assertion.
        """
        # This exact pattern exists in receptionist_v2.md (Pass 2 shipped it).
        teaching_prompt = _COMPLIANT_PROMPT.replace(
            "Shape your tone through word choice alone — never write bracketed annotations into your output.",
            (
                "Shape your tone through word choice alone — never write bracketed annotations into your output.\n"
                "For example, never write [warm] or [empathetic] in your dialogue — those tokens are spoken aloud."
            ),
        )
        report = validator.validate(
            prompt_text=teaching_prompt,
            agent_config=_cfg(),
            agent_kind="receptionist",
        )
        failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]
        # This assertion FAILS today (hence xfail): rule 12 fires on [warm].
        # When GAP-01 is fixed, rule 12 should NOT fire on teaching lines.
        assert "12" not in failing_keys, (
            "GAP-01: Rule 12 fired on a teaching line that explains what NOT to do. "
            "This is a false positive. Negation-aware exemption needed in validator v2."
        )

    def test_non_teaching_bracketed_tag_still_fails_rule_12(
        self, validator: ContractValidator
    ) -> None:
        """A bracketed tag in a usage context (not a teaching line) must still fail rule 12.

        This is the baseline check that ensures the GAP-01 fix does not create
        a broader exemption that lets real violations slip through.
        """
        violating_prompt = _COMPLIANT_PROMPT + "\n[warm] Hello there.\n"
        report = validator.validate(
            prompt_text=violating_prompt,
            agent_config=_cfg(),
            agent_kind="receptionist",
        )
        _assert_only_target_fails(report, 12, "")


class TestRule12bEdgeCases:
    """Rule 12b extended: edge cases for per-tag description length."""

    def test_tag_with_exactly_20_chars_passes(self, validator: ContractValidator) -> None:
        """A tag description of exactly 20 chars is the minimum — must pass rule 12b."""
        config = _voice_cfg(
            suggested_audio_tags=[
                {"name": "Warmly", "description": "12345678901234567890"},  # exactly 20
            ]
        )
        report = validator.validate(
            prompt_text=_COMPLIANT_PROMPT,
            agent_config=config,
            agent_kind="receptionist",
        )
        failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]
        assert "12b" not in failing_keys, (
            f"Tag with exactly 20-char description should pass rule 12b. "
            f"Failing: {failing_keys}"
        )

    def test_tag_with_19_chars_fails(self, validator: ContractValidator) -> None:
        """A tag description of 19 chars is below minimum — must fail rule 12b."""
        config = _voice_cfg(
            suggested_audio_tags=[
                {"name": "Warmly", "description": "1234567890123456789"},  # 19 chars
            ]
        )
        report = validator.validate(
            prompt_text=_COMPLIANT_PROMPT,
            agent_config=config,
            agent_kind="receptionist",
        )
        failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]
        assert "12b" in failing_keys, (
            f"Tag with 19-char description should fail rule 12b. "
            f"Failing: {failing_keys}"
        )

    def test_empty_suggested_audio_tags_list_passes_12b(
        self, validator: ContractValidator
    ) -> None:
        """An empty suggested_audio_tags list has no entries to violate 12b — must pass."""
        config = _voice_cfg(suggested_audio_tags=[])
        report = validator.validate(
            prompt_text=_COMPLIANT_PROMPT,
            agent_config=config,
            agent_kind="receptionist",
        )
        failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]
        assert "12b" not in failing_keys, (
            f"Empty audio tags list should not fail rule 12b. Failing: {failing_keys}"
        )


class TestRule12cEdgeCases:
    """Rule 12c extended: voice settings must not be set on v3_conversational."""

    def test_non_v3_model_with_stability_passes(self, validator: ContractValidator) -> None:
        """A non-v3 model MAY have stability set — rule 12c must not fire."""
        config = _voice_cfg(model_family="v2", stability=0.65)
        report = validator.validate(
            prompt_text=_COMPLIANT_PROMPT,
            agent_config=config,
            agent_kind="receptionist",
        )
        failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]
        assert "12c" not in failing_keys, (
            f"Rule 12c should not fire on non-v3 models. Failing: {failing_keys}"
        )

    def test_v3_model_without_voice_settings_passes(self, validator: ContractValidator) -> None:
        """v3_conversational with no stability/speed/style fields must pass rule 12c."""
        voice = {
            "model_family": "v3_conversational",
            "suggested_audio_tags": _COMPLIANT_CONFIG["voice"]["suggested_audio_tags"],
        }
        config = _cfg()
        config["voice"] = voice
        report = validator.validate(
            prompt_text=_COMPLIANT_PROMPT,
            agent_config=config,
            agent_kind="receptionist",
        )
        failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]
        assert "12c" not in failing_keys, (
            f"Rule 12c should not fire when no voice settings are present. Failing: {failing_keys}"
        )

    def test_v3_model_with_similarity_boost_fails(self, validator: ContractValidator) -> None:
        """v3_conversational with similarity_boost set must fail rule 12c."""
        config = _voice_cfg(model_family="v3_conversational", similarity_boost=0.8)
        report = validator.validate(
            prompt_text=_COMPLIANT_PROMPT,
            agent_config=config,
            agent_kind="receptionist",
        )
        _assert_only_target_fails(report, 12, "c")


class TestRule19EdgeCases:
    """Rule 19 extended: tool registry uniqueness edge cases."""

    def test_two_different_transfer_tools_passes_rule_19(
        self, validator: ContractValidator
    ) -> None:
        """Two tools with different names must pass rule 19 (only <=1 transfer_to_number check)."""
        config = _cfg(tools=[
            {"name": "look_up_caller"},
            {"name": "capture_message"},
            {"name": "transfer_to_number"},
        ])
        report = validator.validate(
            prompt_text=_COMPLIANT_PROMPT,
            agent_config=config,
            agent_kind="receptionist",
        )
        # Rule 19 should not fire — one transfer_to_number is allowed
        # (rule 24 may fire on capture-first, but rule 19 should pass)
        failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]
        assert "19" not in failing_keys, (
            f"Rule 19 should pass with one transfer_to_number. Failing: {failing_keys}"
        )

    def test_two_transfer_to_number_tools_fails_rule_19(
        self, validator: ContractValidator
    ) -> None:
        """Two transfer_to_number tools (duplicate) must fail rule 19."""
        config = _cfg(tools=[
            {"name": "transfer_to_number"},
            {"name": "transfer_to_number"},
        ])
        report = validator.validate(
            prompt_text=_COMPLIANT_PROMPT,
            agent_config=config,
            agent_kind="receptionist",
        )
        failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]
        assert "19" in failing_keys, (
            f"Rule 19 should fail with two transfer_to_number tools. Failing: {failing_keys}"
        )


class TestRule3EmphasisAllSections:
    """Rule 3 extended: emphasis required in EVERY section with a numbered list."""

    def test_emphasis_in_all_numbered_sections_passes(
        self, validator: ContractValidator
    ) -> None:
        """Prompt with 'This step is important.' in all numbered-list sections passes rule 3."""
        report = validator.validate(
            prompt_text=_COMPLIANT_PROMPT,
            agent_config=_cfg(),
            agent_kind="receptionist",
        )
        failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]
        assert "3" not in failing_keys, (
            f"Compliant prompt should pass rule 3. Failing: {failing_keys}"
        )


class TestOverrideDoesNotMaskUnrelatedRules:
    """Override blocks must not mask rules they don't cover."""

    def test_override_for_rule_18_does_not_suppress_rule_1_failure(
        self, validator: ContractValidator
    ) -> None:
        """An override for rule 18 must not suppress a failure on rule 1."""
        # Rule 1 violation: remove # Environment heading
        violating_prompt = _COMPLIANT_PROMPT.replace("# Environment\n", "")
        config = copy.deepcopy(_COMPLIANT_CONFIG)
        config["contract_overrides"] = []
        config["post_call_webhook_id"] = ""  # Also fail rule 18
        config["contract_overrides"] = [
            {
                "rule": 18,
                "rule_suffix": "",
                "reason": "Legacy agent pending migration.",
                "approved_by": "tonio_scott",
                "approved_at": "2026-05-07",
            }
        ]
        report = validator.validate(
            prompt_text=violating_prompt,
            agent_config=config,
            agent_kind="receptionist",
        )
        failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]
        # Rule 1 must still fail (override for rule 18 doesn't help rule 1)
        assert "1" in failing_keys, (
            f"Rule 1 violation should not be masked by rule 18 override. "
            f"Failing: {failing_keys}"
        )
        # Rule 18 must be in overrides_applied (not in failing_rules)
        assert "18" not in failing_keys, (
            f"Rule 18 should be in overrides_applied, not failing_rules. "
            f"Failing: {failing_keys}"
        )
        override_rule_ids = [r.rule for r in report.overrides_applied]
        assert 18 in override_rule_ids
