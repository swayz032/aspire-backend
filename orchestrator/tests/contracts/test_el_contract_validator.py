"""Pass 0 — ElevenLabs Agent Contract Validator tests.

Test plan:
  - test_compliant_synthetic_prompt_returns_26_of_26
  - test_each_rule_fails_on_targeted_violation (parametrized, one violator per rule)
  - test_override_marks_rule_applied_not_failing
  - test_unsigned_override_is_rejected

All tests are offline (no network, no Supabase).  The receipt store is patched
so the validator's __init__ does not require a live DB.

Law #2: ContractValidator emits receipt on construction — patched in tests to
avoid hitting Supabase.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the orchestrator source tree is importable without installing.
# In CI the package is installed editably; locally via PYTHONPATH.
# ---------------------------------------------------------------------------
SRC_PATH = Path(__file__).parent.parent.parent / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

# Required env var consumed at import time by middleware
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

from aspire_orchestrator.services.el_contract import (  # noqa: E402
    ContractReport,
    ContractValidator,
    FailedRule,
    OverrideRecord,
    _DEFAULT_DYN_VARS,
)

# ---------------------------------------------------------------------------
# Synthetic compliant prompt and config (covers all 26 rules).
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

_COMPLIANT_CONFIG: dict = {
    "agent_id": "agent_test_0001",
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
            {"name": "Warmly", "description": "Use when greeting the caller at the start of the call to set a friendly tone"},
            {"name": "Empathetically", "description": "Use when the caller reports a problem or expresses frustration or distress"},
            {"name": "Confidently", "description": "Use when stating firm policies such as service hours or pricing information"},
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

_AGENT_KIND = "receptionist"


# ---------------------------------------------------------------------------
# Fixture: patch receipt store so constructor doesn't hit Supabase in tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_receipt_store():
    """Prevent ContractValidator.__init__ from touching Supabase."""
    with patch("aspire_orchestrator.services.receipt_store.store_receipts"):
        yield


@pytest.fixture()
def validator(tmp_path):
    """ContractValidator loaded from the real contract YAML."""
    return ContractValidator()


# ---------------------------------------------------------------------------
# Test 1 — Compliant synthetic prompt → 26/26
# ---------------------------------------------------------------------------


def test_compliant_synthetic_prompt_returns_26_of_26(validator):
    """A synthetic minimal compliant prompt + config must score 26/26 (with any overrides)."""
    report: ContractReport = validator.validate(
        prompt_text=_COMPLIANT_PROMPT,
        agent_config=_COMPLIANT_CONFIG,
        agent_kind=_AGENT_KIND,
    )
    assert report.failing_rules == [], (
        f"Expected 0 failing rules; got {len(report.failing_rules)}:\n"
        + "\n".join(f"  Rule {r.id}{r.id_suffix} ({r.name}): {r.evidence}" for r in report.failing_rules)
    )
    # 26 numbered rules + 12b + 12c = 28 distinct checks
    assert "28/28" in report.score, f"Unexpected score: {report.score}"


# ---------------------------------------------------------------------------
# Test 2 — Parametrized: one violating config per rule fails the correct rule
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> dict:
    """Return a copy of _COMPLIANT_CONFIG with the given overrides applied."""
    import copy
    cfg = copy.deepcopy(_COMPLIANT_CONFIG)
    # Remove existing contract_overrides so targeted violations actually fail
    cfg["contract_overrides"] = []
    for key, value in overrides.items():
        cfg[key] = value
    return cfg


def _make_voice_config(**voice_overrides) -> dict:
    """Return a config with voice sub-dict overridden."""
    import copy
    cfg = _make_config()
    voice = copy.deepcopy(cfg.get("voice", {}))
    voice.update(voice_overrides)
    cfg["voice"] = voice
    return cfg


# Each entry: (rule_id, rule_suffix, violating_prompt_or_None, violating_config_or_None, agent_kind)
# None means "use the compliant value".
_VIOLATION_CASES: list[tuple] = [
    # Rule 1: missing # Environment heading
    (
        1, "",
        _COMPLIANT_PROMPT.replace("# Environment\n", ""),
        None,
        "receptionist",
    ),
    # Rule 2: banned filler phrase
    (
        2, "",
        _COMPLIANT_PROMPT + "\nI'd be happy to assist you with that today.\n",
        None,
        "receptionist",
    ),
    # Rule 3: numbered list without "This step is important."
    (
        3, "",
        _COMPLIANT_PROMPT.replace("This step is important.", ""),
        None,
        "receptionist",
    ),
    # Rule 4: Guardrails section with only 2 list items
    (
        4, "",
        _COMPLIANT_PROMPT.replace(
            "- Never reveal confidential pricing or personnel information.\n"
            "- Do not make promises about availability or response times beyond what the owner has authorized.\n"
            "- If you are uncertain about any detail, say you will pass the message along rather than guessing.\n"
            "- Disclose that you are an AI only when directly asked by the caller.\n"
            "- Only when asked directly, acknowledge that you are an AI assistant, not a person.\n",
            "- Never reveal pricing.\n- Do not promise availability.\n",
        ),
        None,
        "receptionist",
    ),
    # Rule 5: Tools section missing **Parameters:** label
    (
        5, "",
        _COMPLIANT_PROMPT.replace("**Parameters:**", "Parameters:"),
        None,
        "receptionist",
    ),
    # Rule 6: Parameters block missing (e.g., …)
    (
        6, "",
        _COMPLIANT_PROMPT.replace("(e.g., before greeting the caller)", "").replace(
            "(e.g., +14045551234)", ""
        ).replace(
            "(e.g., after caller declines to wait on hold)", ""
        ).replace(
            "(e.g., John Smith)", ""
        ).replace(
            "(e.g., Caller wants a quote for HVAC service)", ""
        ),
        None,
        "receptionist",
    ),
    # Rule 7: Error handling block too short
    (
        7, "",
        _COMPLIANT_PROMPT.replace(
            "If the tool returns an error, proceed without caller history and treat the caller as unknown. Always capture name and purpose manually in this case.",
            "N/A",
        ),
        None,
        "receptionist",
    ),
    # Rule 8: Goal section exceeds 7 steps
    (
        8, "",
        _COMPLIANT_PROMPT.replace(
            "7. Log the call outcome via the appropriate tool.\n",
            "7. Log the call outcome via the appropriate tool.\n"
            "8. Send a follow-up SMS.\n"
            "9. Archive the call.\n",
        ),
        None,
        "receptionist",
    ),
    # Rule 9: H1 heading in Title Case (not sentence case)
    (
        9, "",
        _COMPLIANT_PROMPT.replace("# Error handling\n", "# Error Handling And Recovery\n"),
        None,
        "receptionist",
    ),
    # Rule 10: text_normalisation_type missing
    (
        10, "",
        None,
        _make_config(text_normalisation_type=None),
        "receptionist",
    ),
    # Rule 11: model_rationale too short
    (
        11, "",
        None,
        _make_config(model_rationale="short"),
        "receptionist",
    ),
    # Rule 12: bracketed audio cue in prompt body
    (
        12, "",
        _COMPLIANT_PROMPT + "\n[warm] Hello there.\n",
        None,
        "receptionist",
    ),
    # Rule 12b: audio tag with short description
    (
        12, "b",
        None,
        _make_voice_config(
            suggested_audio_tags=[
                {"name": "Warmly", "description": "Hi"},  # < 20 chars
            ]
        ),
        "receptionist",
    ),
    # Rule 12c: v3_conversational with stability set
    (
        12, "c",
        None,
        _make_voice_config(model_family="v3_conversational", stability=0.65),
        "receptionist",
    ),
    # Rule 13: "read back" phrase in prompt
    (
        13, "",
        _COMPLIANT_PROMPT + "\nPlease read back the address to the caller.\n",
        None,
        "receptionist",
    ),
    # Rule 14: another agent's name appears in prompt
    (
        14, "",
        _COMPLIANT_PROMPT + "\nIf you cannot help, transfer to Finn.\n",
        _make_config(display_name="Tiffany", name="Tiffany"),
        "receptionist",
    ),
    # Rule 15: first_message_template references unregistered var
    (
        15, "",
        None,
        _make_config(first_message_template="Hello from {{unknown_custom_var}}."),
        "receptionist",
    ),
    # Rule 16: prompt uses unregistered dyn var
    (
        16, "",
        _COMPLIANT_PROMPT + "\nContact {{mystery_var}} for details.\n",
        None,
        "receptionist",
    ),
    # Rule 17: receptionist missing webhook flag
    (
        17, "",
        None,
        _make_config(enable_conversation_initiation_client_data_from_webhook=False),
        "receptionist",
    ),
    # Rule 18: post_call_webhook_id missing
    (
        18, "",
        None,
        _make_config(post_call_webhook_id=""),
        "receptionist",
    ),
    # Rule 19: duplicate tool name
    (
        19, "",
        None,
        _make_config(tools=[{"name": "look_up_caller"}, {"name": "look_up_caller"}]),
        "receptionist",
    ),
    # Rule 20: no AI disclosure in prompt
    (
        20, "",
        _COMPLIANT_PROMPT.replace(
            "- Disclose that you are an AI only when directly asked by the caller.\n"
            "- Only when asked directly, acknowledge that you are an AI assistant, not a person.\n",
            "- Never reveal confidential information.\n",
        ),
        None,
        "receptionist",
    ),
    # Rule 21: no closing single-utterance instruction
    (
        21, "",
        _COMPLIANT_PROMPT.replace(
            "6. Close the call with a single warm farewell and stop talking.\n",
            "6. Wrap up the call graciously.\n",
        ),
        None,
        "receptionist",
    ),
    # Rule 22: state-changing tool but no identity verification step
    (
        22, "",
        # Remove ALL identity verification language (steps 2, 3, 5 all reference verify/capture/confirm)
        _COMPLIANT_PROMPT.replace(
            "2. Identify whether the caller is known using the caller_is_known variable. This step is important.\n",
            "2. Check the caller record. This step is important.\n",
        ).replace(
            "3. Capture the caller name and reason for calling if not already known.\n",
            "3. Ask for the reason for calling.\n",
        ).replace(
            "5. Confirm the caller contact number before ending the call.\n",
            "5. Ask for any additional information needed.\n",
        ),
        _make_config(tools=[{"name": "schedule_appointment"}]),
        "receptionist",
    ),
    # Rule 23: no escalation path in Guardrails or Error handling
    (
        23, "",
        _COMPLIANT_PROMPT.replace(
            "# Error handling\n\n"
            "If a tool call fails, acknowledge the issue calmly and escalate to message capture or transfer to the owner directly.\n"
            "If the caller reports an emergency, capture their name and number immediately and initiate a callback request.\n"
            "If you cannot resolve the caller need, offer to take a message and ensure a human reviews it within one business day.\n"
            "If the caller asks to speak to a person, attempt a transfer or voicemail capture before ending the call.\n",
            "# Error handling\n\nHandle errors gracefully.\n",
        ),
        None,
        "receptionist",
    ),
    # Rule 24: transfer_to_number tool but no capture-first rule
    (
        24, "",
        # Remove capture-first language
        _COMPLIANT_PROMPT.replace(
            "3. Capture the caller name and reason for calling if not already known.\n",
            "3. Ask for the reason for calling.\n",
        ),
        _make_config(tools=[{"name": "transfer_to_number"}]),
        "receptionist",
    ),
    # Rule 25: state-changing tool but no receipts_emitted
    (
        25, "",
        None,
        _make_config(tools=[{"name": "schedule_appointment"}], receipts_emitted=None),
        "receptionist",
    ),
    # Rule 26: receptionist prompt missing {{industry}}
    (
        26, "",
        _COMPLIANT_PROMPT.replace("{{industry}}", "HVAC").replace("{{industry_specialty}}", "residential cooling"),
        None,
        "receptionist",
    ),
]

_VIOLATION_IDS = [
    f"rule_{rule_id}{rule_suffix}" for rule_id, rule_suffix, *_ in _VIOLATION_CASES
]


@pytest.mark.parametrize(
    "rule_id,rule_suffix,violating_prompt,violating_config,agent_kind",
    _VIOLATION_CASES,
    ids=_VIOLATION_IDS,
)
def test_each_rule_fails_on_targeted_violation(
    validator,
    rule_id: int,
    rule_suffix: str,
    violating_prompt,
    violating_config,
    agent_kind: str,
):
    """Each targeted violation must fail exactly the targeted rule (and no others may be introduced by the fixture)."""
    import copy

    prompt = violating_prompt if violating_prompt is not None else _COMPLIANT_PROMPT
    config = copy.deepcopy(violating_config) if violating_config is not None else _make_config()

    report: ContractReport = validator.validate(
        prompt_text=prompt,
        agent_config=config,
        agent_kind=agent_kind,
    )

    rule_key = str(rule_id) + rule_suffix
    failing_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]

    assert rule_key in failing_keys, (
        f"Rule {rule_key} should have failed but did not.\n"
        f"Failing rules: {failing_keys}\n"
        f"Evidence dump: {[(r.id, r.id_suffix, r.evidence) for r in report.failing_rules]}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Override moves rule from failing to overrides_applied
# ---------------------------------------------------------------------------


def test_override_marks_rule_applied_not_failing(validator):
    """A rule that would fail is moved to overrides_applied when a valid signed override exists."""
    import copy

    # Rule 18 will fail (no post_call_webhook_id)
    cfg = copy.deepcopy(_COMPLIANT_CONFIG)
    cfg["contract_overrides"] = []
    cfg["post_call_webhook_id"] = ""
    # Add a valid signed override for rule 18
    cfg["contract_overrides"] = [
        {
            "rule": 18,
            "rule_suffix": "",
            "reason": "Legacy agent pre-dates post-call webhook; pending migration.",
            "approved_by": "tonio_scott",
            "approved_at": "2026-05-07",
        }
    ]

    report: ContractReport = validator.validate(
        prompt_text=_COMPLIANT_PROMPT,
        agent_config=cfg,
        agent_kind=_AGENT_KIND,
    )

    failing_ids = [str(r.id) + r.id_suffix for r in report.failing_rules]
    override_rule_ids = [r.rule for r in report.overrides_applied]

    assert 18 not in failing_ids, (
        f"Rule 18 should be in overrides_applied, not failing_rules. failing={failing_ids}"
    )
    assert 18 in override_rule_ids, (
        f"Rule 18 override should appear in overrides_applied. overrides={override_rule_ids}"
    )
    assert "override" in report.score.lower(), (
        f"Score should mention overrides when they are applied: {report.score}"
    )


# ---------------------------------------------------------------------------
# Test 4 — Unsigned override is rejected (rule still fails)
# ---------------------------------------------------------------------------


def test_unsigned_override_is_rejected(validator):
    """An override missing approved_by or approved_at is invalid; the rule still fails."""
    import copy

    cfg = copy.deepcopy(_COMPLIANT_CONFIG)
    cfg["contract_overrides"] = []
    cfg["post_call_webhook_id"] = ""
    # Override missing approved_by — must be rejected
    cfg["contract_overrides"] = [
        {
            "rule": 18,
            "rule_suffix": "",
            "reason": "Unsigned override attempt.",
            "approved_by": "",  # Missing signatory
            "approved_at": "2026-05-07",
        }
    ]

    report: ContractReport = validator.validate(
        prompt_text=_COMPLIANT_PROMPT,
        agent_config=cfg,
        agent_kind=_AGENT_KIND,
    )

    failing_ids = [str(r.id) + r.id_suffix for r in report.failing_rules]
    assert "18" in failing_ids, (
        f"Unsigned override for rule 18 should not waive the rule; "
        f"failing_rules={failing_ids}"
    )
    override_rule_ids = [r.rule for r in report.overrides_applied]
    assert 18 not in override_rule_ids, (
        f"Invalid override should not appear in overrides_applied: {override_rule_ids}"
    )

    # Also test missing approved_at
    cfg2 = copy.deepcopy(_COMPLIANT_CONFIG)
    cfg2["contract_overrides"] = []
    cfg2["post_call_webhook_id"] = ""
    cfg2["contract_overrides"] = [
        {
            "rule": 18,
            "rule_suffix": "",
            "reason": "Unsigned override attempt.",
            "approved_by": "tonio_scott",
            "approved_at": "",  # Missing date
        }
    ]

    report2: ContractReport = validator.validate(
        prompt_text=_COMPLIANT_PROMPT,
        agent_config=cfg2,
        agent_kind=_AGENT_KIND,
    )

    failing_ids2 = [str(r.id) + r.id_suffix for r in report2.failing_rules]
    assert "18" in failing_ids2, (
        f"Override with missing approved_at should not waive rule 18; failing={failing_ids2}"
    )
