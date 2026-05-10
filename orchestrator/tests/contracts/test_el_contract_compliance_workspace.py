"""Pass 6 — Workspace-wide EL contract compliance enforcement tests.

Purpose: Verify that every persona file and agent config in the Aspire workspace
satisfies the 26-rule EL Agent Contract.  Three coverage axes:

1. test_every_persona_in_repo_validates
   Discovers all *.md files under config/personas/ and all agent_configs/*.json
   under Aspire-desktop/agent_configs/, pairs them by agent_id, and runs
   ContractValidator.  Agents not yet rewritten (Pass 8 deferred) are marked
   xfail(strict=True) so they are tracked without blocking CI.

2. test_no_audio_tag_orphans_in_audio_tags_yaml
   For config/audio_tags/receptionist_audio_tags_v1.yaml (created in Pass 2.5),
   asserts every entry has a description field of >= 20 chars.
   Gracefully skips if the file does not exist yet (Pass 2.5 not yet complete).

3. test_default_dyn_vars_match_personalization_payload
   Loads _DEFAULT_DYN_VARS from el_contract.py and the actual _DEFAULT_DYN_VARS
   from routes/sarah.py and asserts they share all keys that the webhook
   guarantees to inject.  Catches GAP-A4: a dyn_var registered in the validator
   but not sent by the webhook leaves the agent with an unresolvable template
   slot.

Law compliance:
  Law #3: Fail Closed — non-100% agents xfail rather than silently pass.
  Law #6: Workspace-scoped validation; no cross-tenant persona leakage.
  Law #9: No agent API keys accessed in these tests (offline only).

Run with the combined-suite command:
  python -m pytest tests/contracts/ tests/scripts/ \\
    tests/routes/test_sarah_agent_name_fallback.py \\
    tests/routes/test_sarah_personalization_hardening.py -q --tb=line
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path wiring — same pattern as test_el_contract_validator.py
# ---------------------------------------------------------------------------

_TESTS_DIR = Path(__file__).parent.parent
_REPO_ROOT = _TESTS_DIR.parent
_SRC_PATH = _REPO_ROOT / "src"
_SCRIPTS_PATH = _REPO_ROOT / "scripts"

for _p in (_SRC_PATH, _SCRIPTS_PATH):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Workspace root is two levels above orchestrator/
_WORKSPACE_ROOT = _REPO_ROOT.parent.parent  # myapp/

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

from aspire_orchestrator.services.el_contract import (  # noqa: E402
    ContractValidator,
    _DEFAULT_DYN_VARS as _CONTRACT_DYN_VARS,
)

# ---------------------------------------------------------------------------
# Well-known paths
# ---------------------------------------------------------------------------

_PERSONAS_DIR = (
    _SRC_PATH
    / "aspire_orchestrator"
    / "config"
    / "personas"
)

_AGENT_CONFIGS_DIR = _WORKSPACE_ROOT / "Aspire-desktop" / "agent_configs"

_AUDIO_TAGS_FILE = (
    _SRC_PATH
    / "aspire_orchestrator"
    / "config"
    / "audio_tags"
    / "receptionist_audio_tags_v1.yaml"
)

# ---------------------------------------------------------------------------
# Agent registry: agent_id -> (persona_md_path, agent_config_path, xfail_reason)
#
# ALL agents are currently xfail because:
#   - Pass 2 rewritten receptionist_v2.md (prompt), but the AGENT CONFIG JSONs
#     (Aspire-desktop/agent_configs/*.json) have not yet been patched with
#     model_rationale, post_call_webhook_id, deduplicated tools, etc.
#   - The prompt itself uses {{time_of_day}} which is in sarah.py's payload
#     but not yet in el_contract._DEFAULT_DYN_VARS (GAP-A4).
#   - Ava/Finn/Eli/Nora configs and personas are fully deferred to Pass 8.
#
# Promotion path:
#   - Receptionist agents (Tiffany, Sarah-R, Sarah-FD): promote to COMPLIANT
#     after Pass 2 patches agent config JSONs + GAP-A4 fix lands.
#   - Ava/Finn/Eli/Nora: promote to COMPLIANT after Pass 8 ships.
#
# TODO (Pass 2 config patch): Move receptionist agents to _COMPLIANT_AGENTS
#   when agent config JSONs are updated with model_rationale, webhook fields,
#   deduplicated tools, and receipts_emitted.
# TODO (Pass 8): Move ava/finn/eli/nora here when their persona files land.
# ---------------------------------------------------------------------------

_RECEPTIONIST_PERSONA = _PERSONAS_DIR / "receptionist_v2.md"

# Agents that should be 100% compliant NOW — promoted from deferred when
# their config JSONs and persona files are fully patched.
# Currently empty: no agent has completed both persona rewrite AND config patch.
_COMPLIANT_AGENTS: dict[str, tuple[Path, str, str]] = {}

# Agents deferred (xfail) — all current agents pending Pass 2 config patch
# and/or Pass 8 persona rewrite.
# Each entry: agent_id -> (persona_path_or_None, config_filename, agent_kind, xfail_reason)
_DEFERRED_AGENTS: dict[str, tuple[Path | None, str, str, str]] = {
    "agent_4801kqtapvsre2gb0gyb1ng631qr": (
        _RECEPTIONIST_PERSONA,
        "Aspire-Tiffany-Receptionist.json",
        "receptionist",
        (
            "Pass 2 config patch pending: agent config JSON lacks model_rationale, "
            "post_call_webhook_id, deduped tools, receipts_emitted. "
            "Also: {{time_of_day}} not in el_contract._DEFAULT_DYN_VARS (GAP-A4). "
            "Promote when config JSONs are patched and GAP-A4 is resolved."
        ),
    ),
    "agent_6501kp71h69jfqysgd055hemqhrq": (
        _RECEPTIONIST_PERSONA,
        "Aspire-Sarah-Receptionist.json",
        "receptionist",
        (
            "Pass 2 config patch pending: agent config JSON lacks model_rationale, "
            "post_call_webhook_id, deduped tools, receipts_emitted. "
            "Also: {{time_of_day}} not in el_contract._DEFAULT_DYN_VARS (GAP-A4). "
            "Promote when config JSONs are patched and GAP-A4 is resolved."
        ),
    ),
    "agent_8901kmqdjnrte7psp6en4f85m4kt": (
        _RECEPTIONIST_PERSONA,
        "Aspire-Sarah-FrontDesk.json",
        "receptionist",
        (
            "Pass 2 config patch pending: agent config JSON lacks model_rationale, "
            "post_call_webhook_id, receipts_emitted, text_normalisation_type. "
            "Also: {{time_of_day}} not in el_contract._DEFAULT_DYN_VARS (GAP-A4). "
            "Promote when config JSONs are patched and GAP-A4 is resolved."
        ),
    ),
    "agent_1201kmqdjgxvfxxteedpkvjej7er": (
        None,  # No dedicated persona md yet; uses inline prompt in agent config
        "Aspire-Ava.json",
        "assistant",
        "Pass 8 deferred: Ava-EL persona not yet rewritten to el_agent_contract_v1",
    ),
    "agent_2201kmqdjjyben0tyg2t5eexnmzg": (
        None,
        "Aspire-Finn.json",
        "advisor",
        "Pass 8 deferred: Finn-EL persona not yet rewritten to el_agent_contract_v1",
    ),
    "agent_4201kmqdjm1tfhfaggnnfjax3m6d": (
        None,
        "Aspire-Eli.json",
        "receptionist",
        "Pass 8 deferred: Eli persona not yet rewritten to el_agent_contract_v1",
    ),
    "agent_1901kmqdjmwmfqg9rqr5jngfydnw": (
        None,
        "Aspire-Nora.json",
        "receptionist",
        "Pass 8 deferred: Nora persona not yet rewritten to el_agent_contract_v1",
    ),
}

# ---------------------------------------------------------------------------
# Fixture: suppress ContractValidator's receipt emission to Supabase
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_receipt_store() -> Any:
    """Prevent ContractValidator.__init__ from touching Supabase."""
    with patch("aspire_orchestrator.services.receipt_store.store_receipts"):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_agent_config(config_filename: str) -> dict[str, Any]:
    """Load an agent config JSON from Aspire-desktop/agent_configs/.

    Returns an empty dict (not raises) if the file is missing — the calling
    test is responsible for skipping or failing appropriately.
    """
    path = _AGENT_CONFIGS_DIR / config_filename
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        raw: Any = json.load(fh)

    # Normalise the EL nested shape into the flat shape ContractValidator expects.
    # EL workspace JSON has: conversation_config.agent.prompt.prompt for the prompt
    # and conversation_config.tts.* for voice settings.
    # ContractValidator expects top-level keys (as per _COMPLIANT_CONFIG in Pass 0 tests).
    # We produce a merged flat dict suitable for validation.
    flat: dict[str, Any] = {
        "agent_id": raw.get("agent_id", ""),
        "display_name": raw.get("name", ""),
        "name": raw.get("name", ""),
    }

    conv = raw.get("conversation_config", {})
    agent_block = conv.get("agent", {})
    prompt_block = agent_block.get("prompt", {})
    tts_block = conv.get("tts", {})

    flat["text_normalisation_type"] = tts_block.get("text_normalisation_type", None)
    flat["model_rationale"] = raw.get("model_rationale", None)
    flat["enable_conversation_initiation_client_data_from_webhook"] = raw.get(
        "enable_conversation_initiation_client_data_from_webhook",
        agent_block.get("enable_conversation_initiation_client_data_from_webhook", False),
    )
    flat["post_call_webhook_id"] = raw.get("post_call_webhook_id", "")
    flat["receipts_emitted"] = raw.get("receipts_emitted", None)
    flat["contract_overrides"] = raw.get("contract_overrides", [])
    flat["first_message"] = agent_block.get("first_message", "")
    flat["first_message_template"] = agent_block.get(
        "first_message", ""  # EL stores as first_message; we alias it
    )
    flat["tools"] = prompt_block.get("tools", raw.get("tools", []))

    # Voice sub-dict for rules 12b, 12c
    voice: dict[str, Any] = {
        "model_family": tts_block.get("model_id", ""),
        "suggested_audio_tags": tts_block.get("suggested_audio_tags", []),
    }
    # Pass through voice settings so rule 12c can catch them
    for vkey in ("stability", "similarity_boost", "speed", "style", "use_speaker_boost"):
        if vkey in tts_block:
            voice[vkey] = tts_block[vkey]
    flat["voice"] = voice

    return flat


def _load_prompt_from_config(config_filename: str) -> str:
    """Extract the system prompt string from a raw EL agent config JSON.

    Returns empty string if not present.
    """
    path = _AGENT_CONFIGS_DIR / config_filename
    if not path.exists():
        return ""
    with path.open("r", encoding="utf-8") as fh:
        raw: Any = json.load(fh)
    conv = raw.get("conversation_config", {})
    agent_block = conv.get("agent", {})
    prompt_block = agent_block.get("prompt", {})
    return str(prompt_block.get("prompt", ""))


# ---------------------------------------------------------------------------
# Test 1 — Every persona in repo validates (Pass 2 compliant; Pass 8 xfail)
# ---------------------------------------------------------------------------


def _make_compliant_agent_cases() -> list[tuple[str, Path, str, str]]:
    """Build parametrize list for currently-compliant agents."""
    cases = []
    for agent_id, (persona_path, config_file, agent_kind) in _COMPLIANT_AGENTS.items():
        cases.append((agent_id, persona_path, config_file, agent_kind))
    return cases


def _make_deferred_agent_cases() -> list[tuple[str, Path | None, str, str, str]]:
    """Build parametrize list for Pass-8-deferred agents."""
    cases = []
    for agent_id, (persona_path, config_file, agent_kind, xfail_reason) in _DEFERRED_AGENTS.items():
        cases.append((agent_id, persona_path, config_file, agent_kind, xfail_reason))
    return cases


@pytest.mark.parametrize(
    "agent_id,persona_path,config_file,agent_kind",
    _make_compliant_agent_cases(),
    ids=[f"compliant_{aid}" for aid in _COMPLIANT_AGENTS],
)
def test_every_persona_in_repo_validates_compliant(
    agent_id: str,
    persona_path: Path,
    config_file: str,
    agent_kind: str,
) -> None:
    """Every fully-rewritten agent persona must score 28/28 (or 28/28 with documented overrides).

    These agents completed Pass 2; they must NOT fail.
    """
    if not persona_path.exists():
        pytest.skip(f"Persona file not yet written: {persona_path}")

    config_path = _AGENT_CONFIGS_DIR / config_file
    if not config_path.exists():
        pytest.skip(f"Agent config not found: {config_path}")

    prompt_text = persona_path.read_text(encoding="utf-8")
    agent_config = _load_agent_config(config_file)
    agent_config["agent_id"] = agent_id  # ensure ID is set

    validator = ContractValidator()
    report = validator.validate(
        prompt_text=prompt_text,
        agent_config=agent_config,
        agent_kind=agent_kind,
    )

    failing_summary = [
        f"Rule {r.id}{r.id_suffix} ({r.name}): {r.evidence}"
        for r in report.failing_rules
    ]
    assert report.failing_rules == [], (
        f"Agent {agent_id} ({config_file}) scored {report.score} — "
        f"expected 28/28 (or with documented overrides).\n"
        f"Failing rules:\n" + "\n".join(f"  {s}" for s in failing_summary)
    )


@pytest.mark.parametrize(
    "agent_id,persona_path,config_file,agent_kind,xfail_reason",
    _make_deferred_agent_cases(),
    ids=[f"deferred_{aid}" for aid in _DEFERRED_AGENTS],
)
@pytest.mark.xfail(
    strict=True,
    reason="Pass 8 deferred: these agents are not yet rewritten to el_agent_contract_v1 compliance",
)
def test_every_persona_in_repo_validates_deferred(
    agent_id: str,
    persona_path: Path | None,
    config_file: str,
    agent_kind: str,
    xfail_reason: str,
) -> None:
    """Pass-8-deferred agents are expected to fail contract validation.

    They are tracked here with strict=True xfail so:
    - CI stays green (failure expected, not a regression)
    - The moment Pass 8 ships and an agent reaches 28/28, this test will XPASS
      (strict=True) and alert the team to remove the xfail marker.

    TODO (Pass 8): When each agent's persona is rewritten, move it from
    _DEFERRED_AGENTS to _COMPLIANT_AGENTS and remove the xfail.
    """
    config_path = _AGENT_CONFIGS_DIR / config_file
    if not config_path.exists():
        pytest.skip(f"Agent config not found (Pass 8 not yet started): {config_path}")

    # Use inline prompt from agent config when no dedicated persona file exists.
    if persona_path is not None and persona_path.exists():
        prompt_text = persona_path.read_text(encoding="utf-8")
    else:
        prompt_text = _load_prompt_from_config(config_file)

    if not prompt_text.strip():
        pytest.skip(f"No prompt text available for {agent_id} — skipping deferred validation")

    agent_config = _load_agent_config(config_file)
    agent_config["agent_id"] = agent_id

    validator = ContractValidator()
    report = validator.validate(
        prompt_text=prompt_text,
        agent_config=agent_config,
        agent_kind=agent_kind,
    )

    # This assertion is EXPECTED TO FAIL for deferred agents.
    # When Pass 8 makes an agent compliant, the xfail becomes xpass
    # (strict=True), alerting the team.
    assert report.failing_rules == [], (
        f"[xfail expected] Agent {agent_id} has {len(report.failing_rules)} failing rules "
        f"({xfail_reason})"
    )


# ---------------------------------------------------------------------------
# Test 2 — No audio tag orphans in audio_tags YAML (Pass 2.5 guard)
# ---------------------------------------------------------------------------


def test_no_audio_tag_orphans_in_audio_tags_yaml() -> None:
    """Every entry in receptionist_audio_tags_v1.yaml must have a description >= 20 chars.

    Validates contract rule 12b at the source-of-truth YAML level.
    Gracefully skips if the file has not been created yet (Pass 2.5 not yet shipped).
    """
    if not _AUDIO_TAGS_FILE.exists():
        pytest.skip(
            f"Audio tags YAML not yet created: {_AUDIO_TAGS_FILE} "
            "(expected after Pass 2.5 ships)"
        )

    import yaml

    with _AUDIO_TAGS_FILE.open("r", encoding="utf-8") as fh:
        tags_data: Any = yaml.safe_load(fh)

    assert tags_data is not None, "Audio tags YAML is empty"

    # Support three YAML shapes:
    #   1. Top-level dict with a "tags" key (Pass 2.5 format — contract_version + tags list)
    #   2. Flat dict of {tag_name: {description: ...}} (legacy format)
    #   3. List of {tag: ..., description: ...} dicts (flat list format)
    if isinstance(tags_data, dict):
        if "tags" in tags_data:
            # Pass 2.5 format: {contract_version, schema_version, applies_to_agent_ids, tags: [...]}
            raw_tags = tags_data["tags"]
            entries: list[Any] = list(raw_tags) if isinstance(raw_tags, list) else list(raw_tags.values())
        else:
            # Legacy flat-dict format: each value is a tag config dict
            entries = list(tags_data.values())
    elif isinstance(tags_data, list):
        entries = tags_data
    else:
        pytest.fail(f"Unexpected audio tags YAML shape: {type(tags_data)}")

    orphans: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            orphans.append(f"non-dict entry: {entry!r}")
            continue
        name = entry.get("name") or entry.get("tag") or "<unnamed>"
        description = str(entry.get("description") or "")
        if len(description) < 20:
            orphans.append(
                f"Tag '{name}': description is {len(description)} chars "
                f"(min 20 required by rule 12b): {description!r}"
            )

    assert not orphans, (
        f"Audio tag entries with missing or too-short descriptions ({len(orphans)} violations):\n"
        + "\n".join(f"  - {o}" for o in orphans)
    )


# ---------------------------------------------------------------------------
# Test 3 — _DEFAULT_DYN_VARS in el_contract.py matches personalization payload
# ---------------------------------------------------------------------------


def test_default_dyn_vars_match_personalization_payload() -> None:
    """GAP-A4 tracker: compare el_contract._DEFAULT_DYN_VARS vs sarah.py _DEFAULT_DYN_VARS.

    The ContractValidator uses _DEFAULT_DYN_VARS (a frozenset) to validate that
    every {{var}} in a prompt has a known provider.  The actual runtime values are
    sent by routes/sarah.py _DEFAULT_DYN_VARS (a dict).

    If a var appears in the contract registry but not in the webhook payload dict,
    the EL agent will receive an unresolvable template slot — the LLM will speak
    the literal '{{var_name}}' to callers (Law #3 violation).

    KNOWN STATUS (as of Pass 6): el_contract._DEFAULT_DYN_VARS contains vars that
    are NOT in sarah.py (e.g., 'agent_first_name' is resolved by EL from the agent
    config display_name, not injected by our webhook).  The test DOCUMENTS these
    gaps rather than failing hard, because some vars are EL-system-resolved.

    The test WILL hard-fail if:
      - sarah.py is missing vars that ARE expected to come from the webhook
        (i.e., vars that prompt templates reference via {{...}} syntax).

    Gap Resolution Strategy:
      1. Vars that EL resolves automatically (agent_first_name, language, etc.)
         should be REMOVED from el_contract._DEFAULT_DYN_VARS and documented.
      2. Vars that must come from the webhook but are missing in sarah.py
         should be ADDED with safe defaults.

    TODO (contract v2): Audit el_contract._DEFAULT_DYN_VARS to remove EL-system
    vars and document the split: EL-resolved vs webhook-provided.
    """
    # Import sarah.py's _DEFAULT_DYN_VARS via dynamic import to avoid loading
    # the full FastAPI application and all its middleware.
    import importlib.util
    import types

    sarah_path = (
        _SRC_PATH
        / "aspire_orchestrator"
        / "routes"
        / "sarah.py"
    )
    assert sarah_path.exists(), f"sarah.py not found at {sarah_path}"

    # Extract _DEFAULT_DYN_VARS by loading the module with minimal side effects.
    # We stub heavy dependencies so the module-level code can execute.
    _stub_modules = {
        "aspire_orchestrator.config.settings": types.ModuleType("settings_stub"),
        "aspire_orchestrator.middleware.correlation": types.ModuleType("correlation_stub"),
        "aspire_orchestrator.services": types.ModuleType("services_stub"),
        "aspire_orchestrator.services.receipt_store": types.ModuleType("receipt_store_stub"),
        "aspire_orchestrator.services.metrics": types.ModuleType("metrics_stub"),
        "aspire_orchestrator.services.supabase_client": types.ModuleType("supabase_stub"),
        "aspire_orchestrator.services.personalization_cache": types.ModuleType("cache_stub"),
    }

    # Provide minimal attributes for stubs that are accessed at module import time.
    settings_stub = _stub_modules["aspire_orchestrator.config.settings"]
    settings_stub.settings = type("Settings", (), {  # type: ignore[attr-defined]
        "aspire_env": "test",
        "disable_personalization_hmac": True,
        "personalization_webhook_secret": "test",
        "aspire_rate_limit": 100000,
    })()

    correlation_stub = _stub_modules["aspire_orchestrator.middleware.correlation"]
    correlation_stub.get_correlation_id = lambda: "test-correlation"  # type: ignore[attr-defined]
    correlation_stub.get_trace_id = lambda: "test-trace"  # type: ignore[attr-defined]

    metrics_stub = _stub_modules["aspire_orchestrator.services.metrics"]
    metrics_stub.METRICS = type("Metrics", (), {  # type: ignore[attr-defined]
        "__getattr__": lambda self, name: (lambda *a, **kw: None),
    })()

    supabase_stub = _stub_modules["aspire_orchestrator.services.supabase_client"]
    supabase_stub.SupabaseClientError = Exception  # type: ignore[attr-defined]
    supabase_stub.supabase_rpc = None  # type: ignore[attr-defined]
    supabase_stub.supabase_select = None  # type: ignore[attr-defined]

    receipt_stub = _stub_modules["aspire_orchestrator.services.receipt_store"]
    receipt_stub.store_receipts = lambda *a, **kw: None  # type: ignore[attr-defined]

    cache_stub = _stub_modules["aspire_orchestrator.services.personalization_cache"]
    cache_stub.get = lambda *a, **kw: None  # type: ignore[attr-defined]
    cache_stub.set = lambda *a, **kw: None  # type: ignore[attr-defined]

    # Also patch fastapi so we don't need the full web framework at module-load.
    import fastapi

    # Attempt to load sarah.py with stubs injected.
    # If it fails (e.g. due to heavy import chains), fall back to direct file parse.
    sarah_dyn_vars: frozenset[str] | None = None

    try:
        with patch.dict("sys.modules", _stub_modules):
            spec = importlib.util.spec_from_file_location(
                "aspire_orchestrator.routes.sarah_test_load",
                sarah_path,
            )
            assert spec is not None
            assert spec.loader is not None
            sarah_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(sarah_mod)  # type: ignore[union-attr]
            sarah_default: dict[str, Any] = sarah_mod._DEFAULT_DYN_VARS  # type: ignore[attr-defined]
            sarah_dyn_vars = frozenset(sarah_default.keys())
    except Exception as exc:
        # Fallback: parse sarah.py source for the _DEFAULT_DYN_VARS dict keys
        # using regex rather than execution.  This is less precise but avoids
        # import-chain failures in restricted CI environments.
        import re
        sarah_source = sarah_path.read_text(encoding="utf-8")
        # Extract keys of the form "key_name": ... within the _DEFAULT_DYN_VARS block
        # Match from _DEFAULT_DYN_VARS: dict[str, Any] = { to the closing }
        block_match = re.search(
            r"_DEFAULT_DYN_VARS:\s*dict\[.*?\]\s*=\s*\{(.+?)^}", sarah_source,
            re.DOTALL | re.MULTILINE,
        )
        if block_match:
            key_matches = re.findall(r'"([a-z_]+)":', block_match.group(1))
            sarah_dyn_vars = frozenset(key_matches)
        else:
            pytest.skip(
                f"Could not load or parse sarah.py _DEFAULT_DYN_VARS "
                f"(import error: {exc}; regex fallback also failed). "
                "Manual verification required."
            )

    if sarah_dyn_vars is None:
        pytest.skip("Could not extract sarah.py _DEFAULT_DYN_VARS")

    # ── GAP-A4 specific check: vars that PROMPT TEMPLATES reference ───────────
    # el_contract._DEFAULT_DYN_VARS is the UNION of all vars that might appear
    # in any agent prompt.  It includes EL-system-resolved vars (agent_first_name,
    # language, etc.) that EL itself substitutes — not the webhook.
    #
    # The specific check that matters for Law #3 compliance:
    # Every var used in the receptionist_v2.md prompt must be provided by EITHER:
    #   a) The webhook payload (sarah.py _DEFAULT_DYN_VARS), OR
    #   b) EL's own system var injection.
    #
    # EL system vars (not from webhook) — documented here so rule 16 can be
    # updated in contract v2 to exclude them:
    _EL_SYSTEM_VARS: frozenset[str] = frozenset({
        "agent_first_name",   # EL resolves from agent config display_name
        "language",           # EL resolves from conversation_config.language
        # The remaining vars in el_contract not in sarah.py are either:
        #   - Internal receipts context vars (call_sid, called_number, caller_id_prefix)
        #     not injected as dyn_vars
        #   - Future vars not yet wired (timezone, trade_id)
        #   - Computed vars the webhook uses internally but doesn't expose as dyn_vars
        #     (conversation_config_override, fallback_reason)
        # Documenting all known non-webhook vars here:
        "call_sid",
        "called_number",
        "caller_id_prefix",
        "conversation_config_override",
        "fallback_reason",
        "timezone",
        "trade_id",
        "agent_id",
        "business_address",
        "business_hours",
        "business_phone",
        "owner_first_name",
        "owner_last_name",
        "suite_id",
    })

    # Vars the contract registry expects to be in the webhook payload
    # (contract_keys minus EL-system vars).
    webhook_expected: frozenset[str] = _CONTRACT_DYN_VARS - _EL_SYSTEM_VARS

    # Confirmed core vars that MUST be in sarah.py to avoid unresolved template slots.
    # These are the vars that receptionist_v2.md actually uses via {{...}}.
    _CORE_WEBHOOK_VARS: frozenset[str] = frozenset({
        "business_name",
        "industry",
        "industry_specialty",
        "caller_is_known",
        "caller_first_name",
        "caller_last_call_summary",
        "trade_primary_term",
        "trade_emergency_keywords",
        "trade_intake_fields_json",
        "time_of_day",
        "is_open_now",
        "is_after_hours",
    })

    # Hard assertion: core vars MUST be in sarah.py
    missing_core = _CORE_WEBHOOK_VARS - sarah_dyn_vars
    assert not missing_core, (
        f"GAP-A4 CRITICAL: Core dyn_vars used by prompt templates are NOT in "
        f"sarah.py _DEFAULT_DYN_VARS:\n"
        f"  Missing: {sorted(missing_core)}\n"
        f"Fix: add these to sarah.py _DEFAULT_DYN_VARS with safe defaults."
    )

    # Soft assertion: document remaining gaps between contract registry and webhook
    # (these may be EL-system vars or future work — reported, not failed).
    residual_gaps = webhook_expected - sarah_dyn_vars - _EL_SYSTEM_VARS
    if residual_gaps:
        import warnings
        warnings.warn(
            f"GAP-A4 INFO: {len(residual_gaps)} vars in el_contract registry not "
            f"in sarah.py _DEFAULT_DYN_VARS and not classified as EL-system vars: "
            f"{sorted(residual_gaps)}. "
            "Review whether these need webhook wiring or EL-system classification. "
            "TODO (contract v2): Audit and resolve.",
            stacklevel=2,
        )

    # Verify sarah.py has the vars it claims — sanity check on the parse.
    assert len(sarah_dyn_vars) >= 20, (
        f"sarah.py _DEFAULT_DYN_VARS parse returned only {len(sarah_dyn_vars)} keys — "
        f"the regex may have failed to extract all keys. Check regex pattern."
    )
