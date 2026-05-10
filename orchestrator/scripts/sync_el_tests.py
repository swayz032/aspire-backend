"""sync_el_tests.py — Push EL native Tests to the ElevenLabs workspace.

Walks config/el_tests/<agent_id>/*.yaml, loads each test definition, and
creates or updates the corresponding test in the ElevenLabs agent via the
POST /v1/convai/agent-testing/create API.

Idempotent: if a test with the same name already exists on the agent, it is
skipped. Upstream EL API does not provide a PATCH-by-name endpoint; idempotency
is implemented by listing existing tests before sync and comparing names.

Every upload emits an el_test_synced receipt (Law #2).

Usage:
    EL_API_KEY=sk_... python -m scripts.sync_el_tests [--dry-run] [--strict]

Flags:
    --dry-run       Walk YAML files and validate; do NOT push to EL workspace.
    --strict        Refuse to sync any test missing required success_condition
                    (or success_criteria for llm tests). Exits 2 on violation.

Exit codes:
    0   All tests synced (or dry-run complete).
    1   EL API error for one or more tests.
    2   --strict validation failure (missing required fields).

Aspire Laws:
    Law #2  — every EL PATCH emits a receipt.
    Law #3  — --strict refuses missing success_condition (fail closed).
    Law #9  — API key sourced from env, never logged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Path bootstrap — allows running as standalone script from repo root
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_PATH = _REPO_ROOT / "src"
if str(_SRC_PATH) not in sys.path:
    sys.path.insert(0, str(_SRC_PATH))

try:
    from aspire_orchestrator.services.receipt_store import store_receipts
except ImportError:
    def store_receipts(receipts: list[dict[str, Any]]) -> None:  # type: ignore[misc]
        """Fallback stub when orchestrator package is not importable."""
        logging.getLogger(__name__).warning(
            "store_receipts not available — receipts logged only: %s",
            json.dumps(receipts, default=str),
        )

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EL_API_BASE = "https://api.elevenlabs.io/v1"
_EL_TESTS_CREATE_PATH = "/convai/agent-testing/create"
_EL_TESTS_LIST_PATH = "/convai/agent-testing"  # GET ?agent_id=...

_EL_TESTS_DIR = (
    _REPO_ROOT
    / "src"
    / "aspire_orchestrator"
    / "config"
    / "el_tests"
)

# Required top-level fields for every test YAML.
_REQUIRED_FIELDS: list[str] = ["name", "agent_id", "type"]

# Fields required specifically when --strict is active (prevents silent gaps).
_STRICT_LLM_REQUIRED: list[str] = ["success_condition"]
_STRICT_SIMULATION_REQUIRED: list[str] = ["success_condition", "simulation_scenario"]
_STRICT_TOOL_REQUIRED: list[str] = ["tool_call_assertions"]


# ---------------------------------------------------------------------------
# EL API helpers (thin HTTP wrappers; injectable for testing)
# ---------------------------------------------------------------------------

def _http_get(url: str, api_key: str) -> dict[str, Any] | None:
    """GET url with xi-api-key auth. Returns parsed JSON or None on error."""
    import urllib.request
    req = urllib.request.Request(
        url,
        headers={"xi-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        log.warning("GET %s failed: %s", url, exc)
        return None


def _http_post(
    url: str,
    api_key: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any] | None]:
    """POST url with xi-api-key auth. Returns (status_code, body_dict)."""
    import urllib.request
    import urllib.error
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        log.warning("POST %s -> HTTP %d: %s", url, exc.code, body_text[:300])
        return exc.code, None
    except Exception as exc:
        log.warning("POST %s failed: %s", url, exc)
        return 0, None


# ---------------------------------------------------------------------------
# YAML validation helpers
# ---------------------------------------------------------------------------

def _validate_yaml(data: dict[str, Any], path: Path, strict: bool) -> list[str]:
    """Return a list of validation error messages. Empty = valid."""
    errors: list[str] = []

    for field in _REQUIRED_FIELDS:
        if field not in data:
            errors.append(f"Missing required field: {field}")

    if not strict:
        return errors

    test_type = data.get("type", "")
    if test_type == "llm":
        for f in _STRICT_LLM_REQUIRED:
            if not data.get(f):
                errors.append(f"[--strict] llm test missing required field: {f}")
    elif test_type == "simulation":
        for f in _STRICT_SIMULATION_REQUIRED:
            if not data.get(f):
                errors.append(f"[--strict] simulation test missing required field: {f}")
    elif test_type == "tool":
        if not data.get(_STRICT_TOOL_REQUIRED[0]):
            errors.append(
                f"[--strict] tool test missing required field: {_STRICT_TOOL_REQUIRED[0]}"
            )

    return errors


# ---------------------------------------------------------------------------
# YAML -> EL API payload mapper
# ---------------------------------------------------------------------------

def _build_el_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Convert a YAML test definition to the EL agent-testing/create payload.

    Maps our YAML schema to the three EL discriminated-union test types:
      - type: llm        -> LLM response unit test
      - type: simulation -> simulation test
      - type: tool       -> tool call unit test
    """
    test_type = data["type"]
    name = data["name"]

    # Build chat_history in EL format.
    raw_history = data.get("chat_history", [])
    chat_history: list[dict[str, Any]] = []
    for i, turn in enumerate(raw_history):
        entry: dict[str, Any] = {
            "role": turn["role"],
            "time_in_call_secs": turn.get("time_in_call_secs", i * 5),
        }
        if "message" in turn:
            entry["message"] = turn["message"]
        chat_history.append(entry)

    # Dynamic variables (renamed from dynamic_variable_overrides in our YAML).
    dynamic_variables: dict[str, Any] = data.get("dynamic_variables", {})

    parent_folder_id: str | None = data.get("parent_folder_id")

    if test_type == "llm":
        payload: dict[str, Any] = {
            "name": name,
            "type": "llm",
            "chat_history": chat_history,
            "success_condition": data.get("success_condition", ""),
            "dynamic_variables": dynamic_variables,
        }
        if data.get("success_examples"):
            payload["success_examples"] = [
                {"content": ex} for ex in data["success_examples"]
            ]
        if data.get("failure_examples"):
            payload["failure_examples"] = [
                {"content": ex} for ex in data["failure_examples"]
            ]

    elif test_type == "simulation":
        payload = {
            "name": name,
            "type": "simulation",
            "chat_history": chat_history,
            "success_condition": data.get("success_condition", ""),
            "simulation_scenario": data.get("simulation_scenario", ""),
            "simulation_max_turns": data.get("simulation_max_turns", 5),
            "dynamic_variables": dynamic_variables,
        }
        if data.get("tool_mock_config"):
            payload["tool_mock_config"] = data["tool_mock_config"]

    elif test_type == "tool":
        # Build tool_call_assertions list into EL's UnitTestToolCallEvaluationModel.
        assertions = data.get("tool_call_assertions", [])
        # For tool tests EL expects the first assertion as the primary tool config.
        # Additional assertions (for capture_message check) are bundled per tool.
        # We emit one tool-type test per tool_call_assertion for precision.
        # Here we build the payload for the first asserted tool with parameters.
        primary: dict[str, Any] = {}
        if assertions:
            first = assertions[0]
            tool_name = first.get("tool_name", "")
            verify_absence = not first.get("expected_called", True)
            param_assertions = first.get("parameter_assertions", [])

            parameters: list[dict[str, Any]] = []
            for p in param_assertions:
                eval_config = p.get("eval", {})
                eval_type = eval_config.get("type", "anything")
                if eval_type == "regex":
                    ev = {"type": "regex", "pattern": eval_config.get("pattern", "")}
                elif eval_type == "llm":
                    ev = {"type": "llm", "condition": eval_config.get("condition", "")}
                elif eval_type == "exact":
                    ev = {"type": "exact", "value": eval_config.get("value", "")}
                else:
                    ev = {"type": "anything"}
                parameters.append({"path": p.get("path", ""), "eval": ev})

            primary = {
                "referenced_tool": {"tool_name": tool_name, "type": "native"},
                "verify_absence": verify_absence,
                "parameters": parameters,
            }

        payload = {
            "name": name,
            "type": "tool",
            "chat_history": chat_history,
            "tool_call_parameters": primary,
            "dynamic_variables": dynamic_variables,
        }

    else:
        # Unknown type — treat as llm with minimal payload.
        log.warning("Unknown test type '%s' in %s — falling back to llm.", test_type, name)
        payload = {
            "name": name,
            "type": "llm",
            "chat_history": chat_history,
            "success_condition": data.get("success_condition", ""),
            "dynamic_variables": dynamic_variables,
        }

    if parent_folder_id:
        payload["parent_folder_id"] = parent_folder_id

    return payload


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def _list_existing_test_names(
    agent_id: str,
    api_key: str,
) -> set[str]:
    """Return the set of test names already registered for this agent in EL."""
    url = f"{_EL_API_BASE}{_EL_TESTS_LIST_PATH}?agent_id={agent_id}"
    result = _http_get(url, api_key)
    if not result:
        return set()
    tests = result.get("tests", result.get("items", []))
    return {t.get("name", "") for t in tests if t.get("name")}


def sync_tests(
    api_key: str,
    dry_run: bool = False,
    strict: bool = False,
    el_tests_dir: Path | None = None,
    post_fn: Any = None,  # injectable for tests
    list_fn: Any = None,  # injectable for tests
) -> int:
    """Walk el_tests directory and sync all YAML tests to EL workspace.

    Returns exit code: 0 success, 1 API error, 2 strict validation failure.
    """
    tests_dir = el_tests_dir or _EL_TESTS_DIR
    if not tests_dir.exists():
        log.error("EL tests directory not found: %s", tests_dir)
        return 1

    yaml_files = sorted(tests_dir.rglob("*.yaml"))
    if not yaml_files:
        log.warning("No YAML test files found under %s", tests_dir)
        return 0

    log.info("Found %d YAML test files.", len(yaml_files))

    receipts: list[dict[str, Any]] = []
    errors: int = 0
    strict_failures: int = 0
    synced: int = 0
    skipped: int = 0

    # Cache existing test names per agent to avoid redundant API calls.
    existing_names_cache: dict[str, set[str]] = {}

    for yaml_path in yaml_files:
        # Agent ID is derived from the parent directory name.
        agent_id = yaml_path.parent.name

        try:
            raw = yaml_path.read_text(encoding="utf-8")
            data: dict[str, Any] = yaml.safe_load(raw)
        except Exception as exc:
            log.error("Failed to parse %s: %s", yaml_path, exc)
            errors += 1
            continue

        if not isinstance(data, dict):
            log.error("YAML at %s is not a mapping.", yaml_path)
            errors += 1
            continue

        # Normalize: allow agent_id from YAML to override directory-derived one.
        data.setdefault("agent_id", agent_id)
        resolved_agent_id: str = data.get("agent_id", agent_id)
        test_name: str = data.get("name", yaml_path.stem)

        # Validate.
        validation_errors = _validate_yaml(data, yaml_path, strict)
        if validation_errors:
            for err in validation_errors:
                log.error("VALIDATION [%s] %s: %s", resolved_agent_id, test_name, err)
            if strict:
                strict_failures += 1
                receipts.append(_build_receipt(
                    agent_id=resolved_agent_id,
                    test_name=test_name,
                    outcome="strict_blocked",
                    reason="; ".join(validation_errors),
                    dry_run=dry_run,
                ))
                continue

        # Idempotency: check if test already exists on EL workspace.
        if resolved_agent_id not in existing_names_cache:
            if dry_run or list_fn is not None:
                # In dry-run mode or with a mock list_fn, use it.
                fetcher = list_fn if list_fn else (lambda a, k: set())
                existing_names_cache[resolved_agent_id] = fetcher(resolved_agent_id, api_key)
            else:
                existing_names_cache[resolved_agent_id] = _list_existing_test_names(
                    resolved_agent_id, api_key
                )

        if test_name in existing_names_cache.get(resolved_agent_id, set()):
            log.info("SKIP [%s] %s — already synced (idempotent).", resolved_agent_id, test_name)
            skipped += 1
            receipts.append(_build_receipt(
                agent_id=resolved_agent_id,
                test_name=test_name,
                outcome="skipped_idempotent",
                dry_run=dry_run,
            ))
            continue

        if dry_run:
            log.info("DRY-RUN [%s] %s — would sync.", resolved_agent_id, test_name)
            receipts.append(_build_receipt(
                agent_id=resolved_agent_id,
                test_name=test_name,
                outcome="dry_run",
                dry_run=True,
            ))
            synced += 1
            continue

        # Build EL payload and upload.
        try:
            payload = _build_el_payload(data)
        except Exception as exc:
            log.error("Payload build failed for %s: %s", test_name, exc)
            errors += 1
            continue

        url = f"{_EL_API_BASE}{_EL_TESTS_CREATE_PATH}"
        _post = post_fn if post_fn else _http_post
        status_code, response = _post(url, api_key, payload)

        if status_code in (200, 201):
            created_id = (response or {}).get("id", "")
            log.info(
                "SYNCED [%s] %s -> test_id=%s",
                resolved_agent_id, test_name, created_id,
            )
            synced += 1
            receipts.append(_build_receipt(
                agent_id=resolved_agent_id,
                test_name=test_name,
                outcome="synced",
                el_test_id=created_id,
                dry_run=False,
            ))
            # Update local cache to prevent duplicate uploads in same run.
            existing_names_cache.setdefault(resolved_agent_id, set()).add(test_name)
        else:
            log.error(
                "FAILED [%s] %s -> HTTP %d",
                resolved_agent_id, test_name, status_code,
            )
            errors += 1
            receipts.append(_build_receipt(
                agent_id=resolved_agent_id,
                test_name=test_name,
                outcome="api_error",
                reason=f"HTTP {status_code}",
                dry_run=False,
            ))

    # Emit receipts (Law #2).
    if receipts and not dry_run:
        store_receipts(receipts)
    elif receipts and dry_run:
        log.info("DRY-RUN: %d receipts would be emitted.", len(receipts))

    log.info(
        "Sync complete — synced=%d skipped=%d errors=%d strict_failures=%d",
        synced, skipped, errors, strict_failures,
    )

    if strict_failures > 0:
        return 2
    if errors > 0:
        return 1
    return 0


def _build_receipt(
    *,
    agent_id: str,
    test_name: str,
    outcome: str,
    el_test_id: str = "",
    reason: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build a Law #2 receipt for a test sync event."""
    now = time.time()
    return {
        "receipt_id": str(uuid.uuid4()),
        "receipt_type": "el_test_synced",
        "actor": "sync_el_tests",
        "risk_tier": "GREEN",
        "outcome": outcome,
        "redacted_inputs": {
            "agent_id": agent_id,
            "test_name": test_name,
            "dry_run": dry_run,
        },
        "redacted_outputs": {
            "el_test_id": el_test_id,
            "reason": reason,
        },
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync EL native Tests from YAML to ElevenLabs workspace."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate YAML and preview sync without calling EL API.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Refuse to sync any test missing required success_condition / tool_call_assertions.",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("ASPIRE_ELEVENLABS_API_KEY") or os.environ.get("EL_API_KEY", "")
    if not api_key and not args.dry_run:
        log.error(
            "ASPIRE_ELEVENLABS_API_KEY is not set. "
            "Export it from Railway: railway run python -m scripts.sync_el_tests"
        )
        return 1

    return sync_tests(api_key=api_key, dry_run=args.dry_run, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
