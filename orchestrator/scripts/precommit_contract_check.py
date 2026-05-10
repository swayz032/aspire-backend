"""Pass 6 — Pre-commit entrypoint for EL contract compliance check.

Usage (invoked by pre-commit):
  python -m aspire_orchestrator.scripts.precommit_contract_check [--strict] FILE [FILE ...]

Also callable directly:
  python backend/orchestrator/scripts/precommit_contract_check.py src/.../receptionist_v2.md

Each FILE is classified by path pattern:
  - *.md under config/personas/          -> persona prompt file
  - agent_configs/*.json                 -> EL agent config
  - config/audio_tags/*.yaml             -> audio tags (rule 12b check only)

For persona .md files: the script looks up the agent_id by matching the
persona filename to the AGENT_PERSONA_MAP registry, loads the corresponding
agent config JSON, and runs ContractValidator.

For agent config .json files: the script infers the persona file from the
agent_id in the JSON and runs ContractValidator.

Files that cannot be matched to a known agent are WARNED but do not cause
a non-zero exit (avoids blocking unrelated commits).

Exit codes:
  0 — all checked files pass (or no applicable files found)
  1 — configuration error (missing env, corrupt YAML, etc.)
  2 — one or more agents failed contract validation

Design constraints:
  - FAST: must complete in < 2 seconds for a single-file change.
    ContractValidator loads the YAML once; no Supabase, no HTTP calls.
  - Does NOT load the full Pydantic settings object — only the contract YAML.
  - The receipt_store import is conditional — missing Supabase creds don't block
    the hook.

Law #3 (Fail Closed): on unknown agent_id, WARN and skip (do not error) —
  the hook must not block commits on files it can't classify.
Law #9: never log API keys or secrets; only file paths and rule IDs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Path wiring: make the orchestrator source importable without installation.
# The hook runs from the repo root (backend/) so we compute relative to __file__.
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent  # backend/orchestrator/scripts/
_ORCHESTRATOR_DIR = _SCRIPT_DIR.parent         # backend/orchestrator/
_SRC_DIR = _ORCHESTRATOR_DIR / "src"          # backend/orchestrator/src/

if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# Workspace root (for resolving Aspire-desktop/agent_configs/)
_WORKSPACE_ROOT = _ORCHESTRATOR_DIR.parent.parent  # myapp/
_AGENT_CONFIGS_DIR = _WORKSPACE_ROOT / "Aspire-desktop" / "agent_configs"

_PERSONAS_DIR = (
    _SRC_DIR
    / "aspire_orchestrator"
    / "config"
    / "personas"
)

# ---------------------------------------------------------------------------
# Registry: persona filename stem -> (agent_id, config_filename, agent_kind)
# Maintained manually — update when new personas are added.
# ---------------------------------------------------------------------------

_PERSONA_STEM_TO_AGENT: dict[str, list[tuple[str, str, str]]] = {
    "receptionist_v2": [
        ("agent_4801kqtapvsre2gb0gyb1ng631qr", "Aspire-Tiffany-Receptionist.json", "receptionist"),
        ("agent_6501kp71h69jfqysgd055hemqhrq", "Aspire-Sarah-Receptionist.json", "receptionist"),
        ("agent_8901kmqdjnrte7psp6en4f85m4kt", "Aspire-Sarah-FrontDesk.json", "receptionist"),
    ],
    # Pass 8: add ava_el_v1, finn_el_v1, eli_v1, nora_v1 here when those land.
}

_CONFIG_FILENAME_TO_AGENT: dict[str, tuple[str, str, str]] = {
    "Aspire-Tiffany-Receptionist.json": (
        "agent_4801kqtapvsre2gb0gyb1ng631qr", "receptionist_v2.md", "receptionist"
    ),
    "Aspire-Sarah-Receptionist.json": (
        "agent_6501kp71h69jfqysgd055hemqhrq", "receptionist_v2.md", "receptionist"
    ),
    "Aspire-Sarah-FrontDesk.json": (
        "agent_8901kmqdjnrte7psp6en4f85m4kt", "receptionist_v2.md", "receptionist"
    ),
}


# ---------------------------------------------------------------------------
# Normalise EL nested JSON to flat ContractValidator shape
# ---------------------------------------------------------------------------


def _normalise_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert EL workspace JSON (nested) to flat ContractValidator shape."""
    conv = raw.get("conversation_config", {})
    agent_block = conv.get("agent", {})
    prompt_block = agent_block.get("prompt", {})
    tts_block = conv.get("tts", {})

    flat: dict[str, Any] = {
        "agent_id": raw.get("agent_id", ""),
        "display_name": raw.get("name", ""),
        "name": raw.get("name", ""),
        "text_normalisation_type": tts_block.get("text_normalisation_type"),
        "model_rationale": raw.get("model_rationale"),
        "enable_conversation_initiation_client_data_from_webhook": raw.get(
            "enable_conversation_initiation_client_data_from_webhook",
            agent_block.get("enable_conversation_initiation_client_data_from_webhook", False),
        ),
        "post_call_webhook_id": raw.get("post_call_webhook_id", ""),
        "receipts_emitted": raw.get("receipts_emitted"),
        "contract_overrides": raw.get("contract_overrides", []),
        "first_message": agent_block.get("first_message", ""),
        "first_message_template": agent_block.get("first_message", ""),
        "tools": prompt_block.get("tools", raw.get("tools", [])),
    }

    voice: dict[str, Any] = {
        "model_family": tts_block.get("model_id", ""),
        "suggested_audio_tags": tts_block.get("suggested_audio_tags", []),
    }
    for vkey in ("stability", "similarity_boost", "speed", "style", "use_speaker_boost"):
        if vkey in tts_block:
            voice[vkey] = tts_block[vkey]
    flat["voice"] = voice

    return flat


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------


def _classify_file(file_path: Path) -> str | None:
    """Return 'persona_md', 'agent_config_json', 'audio_tags_yaml', or None."""
    suffix = file_path.suffix.lower()
    name = file_path.name

    if suffix == ".md" and "personas" in str(file_path):
        return "persona_md"
    if suffix == ".json" and "agent_configs" in str(file_path):
        return "agent_config_json"
    if suffix in (".yaml", ".yml") and "audio_tags" in str(file_path):
        return "audio_tags_yaml"
    return None


# ---------------------------------------------------------------------------
# Audio tags check (rule 12b, fast path)
# ---------------------------------------------------------------------------


def _check_audio_tags_yaml(file_path: Path) -> list[str]:
    """Return list of violation strings for audio_tags YAML. Empty = pass."""
    try:
        import yaml
    except ImportError:
        return ["[WARN] PyYAML not installed; skipping audio tags check"]

    try:
        with file_path.open("r", encoding="utf-8") as fh:
            data: Any = yaml.safe_load(fh)
    except Exception as exc:
        return [f"[ERROR] Could not parse {file_path}: {exc}"]

    if data is None:
        return []

    entries: list[Any]
    if isinstance(data, dict):
        entries = list(data.values())
    elif isinstance(data, list):
        entries = data
    else:
        return [f"[ERROR] Unexpected YAML shape in {file_path}: {type(data)}"]

    violations: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("tag") or "<unnamed>"
        desc = str(entry.get("description") or "")
        if len(desc) < 20:
            violations.append(
                f"  Rule 12b: tag '{name}' description is {len(desc)} chars "
                f"(min 20): {desc!r}"
            )
    return violations


# ---------------------------------------------------------------------------
# Main check logic
# ---------------------------------------------------------------------------


def _run_checks(files: list[Path], strict: bool) -> int:
    """Run contract checks on classified files. Returns exit code."""
    # Import ContractValidator — patched to suppress receipt emission if no DB.
    # We do NOT load full settings; only the contract YAML is needed.
    try:
        os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
        os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "pre-commit-stub-key")

        # Suppress the Supabase receipt emission so the hook runs offline.
        import unittest.mock as _mock
        with _mock.patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
            side_effect=lambda *a, **kw: None,
        ):
            from aspire_orchestrator.services.el_contract import ContractValidator
            validator = ContractValidator()
    except Exception as exc:
        print(f"[ERROR] Could not load ContractValidator: {exc}", file=sys.stderr)
        return 1

    exit_code = 0
    total_files = 0
    failures: list[str] = []

    for file_path in files:
        kind = _classify_file(file_path)
        if kind is None:
            # Not a monitored file — skip silently
            continue

        total_files += 1

        # ── Audio tags YAML ───────────────────────────────────────────────
        if kind == "audio_tags_yaml":
            violations = _check_audio_tags_yaml(file_path)
            if violations:
                failures.append(f"{file_path}:")
                failures.extend(violations)
                exit_code = 2
            else:
                print(f"[PASS] {file_path.name} (audio tags rule 12b)")
            continue

        # ── Persona .md -> one or more agents ────────────────────────────
        if kind == "persona_md":
            stem = file_path.stem
            agent_entries = _PERSONA_STEM_TO_AGENT.get(stem)
            if not agent_entries:
                print(
                    f"[WARN] {file_path.name}: not in persona registry — skipping "
                    f"(add to precommit_contract_check.py _PERSONA_STEM_TO_AGENT)",
                    file=sys.stderr,
                )
                continue

            prompt_text = file_path.read_text(encoding="utf-8")
            for agent_id, config_filename, agent_kind in agent_entries:
                config_path = _AGENT_CONFIGS_DIR / config_filename
                if not config_path.exists():
                    print(
                        f"[WARN] Agent config not found: {config_path} — "
                        f"skipping {agent_id}",
                        file=sys.stderr,
                    )
                    continue

                with config_path.open("r", encoding="utf-8") as fh:
                    raw_config: dict[str, Any] = json.load(fh)
                agent_config = _normalise_config(raw_config)
                agent_config["agent_id"] = agent_id

                report = validator.validate(
                    prompt_text=prompt_text,
                    agent_config=agent_config,
                    agent_kind=agent_kind,
                )

                if report.failing_rules:
                    label = f"{file_path.name} -> {agent_id} ({config_filename})"
                    failures.append(f"{label}: scored {report.score}")
                    for rule in report.failing_rules:
                        failures.append(
                            f"  Rule {rule.id}{rule.id_suffix} ({rule.name}) [{rule.severity}]: "
                            f"{rule.evidence}"
                        )
                    exit_code = 2
                else:
                    print(
                        f"[PASS] {file_path.name} -> {agent_id}: {report.score}"
                        + (f" ({len(report.overrides_applied)} override(s))" if report.overrides_applied else "")
                    )

        # ── Agent config .json ────────────────────────────────────────────
        elif kind == "agent_config_json":
            config_filename = file_path.name
            agent_entry = _CONFIG_FILENAME_TO_AGENT.get(config_filename)
            if not agent_entry:
                print(
                    f"[WARN] {config_filename}: not in agent config registry — skipping "
                    f"(add to precommit_contract_check.py _CONFIG_FILENAME_TO_AGENT)",
                    file=sys.stderr,
                )
                continue

            agent_id, persona_filename, agent_kind = agent_entry
            persona_path = _PERSONAS_DIR / persona_filename
            if not persona_path.exists():
                print(
                    f"[WARN] Persona file not found: {persona_path} — "
                    f"skipping {config_filename}",
                    file=sys.stderr,
                )
                continue

            prompt_text = persona_path.read_text(encoding="utf-8")
            with file_path.open("r", encoding="utf-8") as fh:
                raw_config = json.load(fh)
            agent_config = _normalise_config(raw_config)
            agent_config["agent_id"] = agent_id

            report = validator.validate(
                prompt_text=prompt_text,
                agent_config=agent_config,
                agent_kind=agent_kind,
            )

            if report.failing_rules:
                label = f"{config_filename} (agent {agent_id})"
                failures.append(f"{label}: scored {report.score}")
                for rule in report.failing_rules:
                    failures.append(
                        f"  Rule {rule.id}{rule.id_suffix} ({rule.name}) [{rule.severity}]: "
                        f"{rule.evidence}"
                    )
                exit_code = 2
            else:
                print(
                    f"[PASS] {config_filename}: {report.score}"
                    + (f" ({len(report.overrides_applied)} override(s))" if report.overrides_applied else "")
                )

    if failures:
        print("\n[FAIL] EL contract compliance violations found:", file=sys.stderr)
        for line in failures:
            print(line, file=sys.stderr)
        print(
            "\nFix the violations above or add a signed contract_override block "
            "to the agent config JSON.",
            file=sys.stderr,
        )

    if total_files == 0:
        # No monitored files in this commit — pass silently
        return 0

    return exit_code


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code."""
    start = time.monotonic()

    parser = argparse.ArgumentParser(
        description="EL contract compliance pre-commit hook (Pass 6)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Files staged for commit (passed by pre-commit framework)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=True,
        help="Fail on any contract violation (default: True)",
    )
    parser.add_argument(
        "--no-strict",
        dest="strict",
        action="store_false",
        help="Warn but do not fail on contract violations (for emergency bypasses)",
    )

    args = parser.parse_args(argv)
    file_paths = [Path(f) for f in args.files]

    result = _run_checks(file_paths, strict=args.strict)

    elapsed = time.monotonic() - start
    if elapsed > 2.0:
        print(
            f"[PERF WARN] Hook took {elapsed:.2f}s (target < 2s) — "
            "consider caching ContractValidator YAML load.",
            file=sys.stderr,
        )

    if not args.strict and result == 2:
        print(
            "[WARN] --no-strict: contract violations found but not blocking commit.",
            file=sys.stderr,
        )
        return 0

    return result


if __name__ == "__main__":
    sys.exit(main())
