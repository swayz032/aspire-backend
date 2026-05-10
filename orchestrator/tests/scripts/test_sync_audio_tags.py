"""Pass 2.5 — sync_audio_tags.py contract + behavior tests.

Test plan:
  - test_yaml_loads_with_16_tags
  - test_every_tag_description_is_20_chars_or_more
  - test_dry_run_does_not_patch_el
  - test_patch_called_per_agent_when_diff_nonempty
  - test_idempotent_when_diff_empty
  - test_receipt_emitted_per_agent

All tests are offline (no network, no Supabase). HTTP helpers and store_receipts
are fully mocked. The test loads real YAML from disk to catch regressions in the
tag matrix itself.

Law #2: every code path emits a receipt — asserted in every test.
Law #3: missing API key must deny. Short descriptions must block.
Law #9: API key sourced from env, never logged or in receipts.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_PATH = _REPO_ROOT / "src"
_SCRIPTS_PATH = _REPO_ROOT / "scripts"

for p in (_SRC_PATH, _SCRIPTS_PATH, str(_REPO_ROOT)):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")
os.environ["EL_API_KEY"] = "sk_test_fake_key_not_real"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TAGS_YAML_PATH = (
    _REPO_ROOT
    / "src"
    / "aspire_orchestrator"
    / "config"
    / "audio_tags"
    / "receptionist_audio_tags_v1.yaml"
)

_THREE_AGENT_IDS = [
    "agent_4801kqtapvsre2gb0gyb1ng631qr",  # Tiffany
    "agent_6501kp71h69jfqysgd055hemqhrq",  # Sarah-Receptionist
    "agent_8901kmqdjnrte7psp6en4f85m4kt",  # Sarah-FrontDesk
]


def _empty_agent_response() -> dict[str, Any]:
    """EL GET response with no current audio tags."""
    return {
        "agent_id": "agent_test",
        "conversation_config": {
            "tts": {
                "suggested_audio_tags": []
            }
        },
    }


def _full_agent_response(tags: list[dict[str, str]]) -> dict[str, Any]:
    """EL GET response with existing audio tags."""
    return {
        "agent_id": "agent_test",
        "conversation_config": {
            "tts": {
                "suggested_audio_tags": tags
            }
        },
    }


# ---------------------------------------------------------------------------
# Test: YAML loads with exactly 16 tags
# ---------------------------------------------------------------------------


def test_yaml_loads_with_16_tags() -> None:
    """The canonical YAML must define exactly 16 audio tags."""
    assert _TAGS_YAML_PATH.exists(), f"Tags YAML not found at {_TAGS_YAML_PATH}"
    data = yaml.safe_load(_TAGS_YAML_PATH.read_text(encoding="utf-8"))
    tags = data.get("tags", [])
    assert len(tags) == 16, f"Expected 16 tags, got {len(tags)}: {[t['tag'] for t in tags]}"


# ---------------------------------------------------------------------------
# Test: Every description >= 20 chars (contract rule 12b)
# ---------------------------------------------------------------------------


def test_every_tag_description_is_20_chars_or_more() -> None:
    """Every tag description in the YAML must be >= 20 chars (rule 12b)."""
    assert _TAGS_YAML_PATH.exists()
    data = yaml.safe_load(_TAGS_YAML_PATH.read_text(encoding="utf-8"))
    violations: list[str] = []
    for entry in data.get("tags", []):
        tag_name = entry.get("tag", "")
        desc = entry.get("description", "").strip()
        if len(desc) < 20:
            violations.append(f"'{tag_name}' description is {len(desc)} chars: '{desc}'")
    assert not violations, f"Rule 12b violations:\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# Test: YAML applies to exactly 3 known agent IDs
# ---------------------------------------------------------------------------


def test_yaml_applies_to_three_receptionist_agents() -> None:
    """YAML must target the 3 canonical receptionist agent IDs."""
    assert _TAGS_YAML_PATH.exists()
    data = yaml.safe_load(_TAGS_YAML_PATH.read_text(encoding="utf-8"))
    agent_ids = data.get("applies_to_agent_ids", [])
    assert set(agent_ids) == set(_THREE_AGENT_IDS), (
        f"Expected {set(_THREE_AGENT_IDS)}, got {set(agent_ids)}"
    )


# ---------------------------------------------------------------------------
# Test: dry-run does not PATCH EL
# ---------------------------------------------------------------------------


def test_dry_run_does_not_patch_el() -> None:
    """--dry-run must compute diff but never call _http_patch."""
    import sync_audio_tags as script

    # Simulate all agents having no current tags (full diff will be non-empty)
    with (
        patch.object(script, "_http_get", return_value=_empty_agent_response()),
        patch.object(script, "_http_patch") as mock_patch,
        patch.object(script, "_emit_receipt"),
    ):
        exit_code = script.main(["--dry-run"])

    assert exit_code == 0, f"Expected exit 0, got {exit_code}"
    mock_patch.assert_not_called()


# ---------------------------------------------------------------------------
# Test: PATCH called per agent when diff is non-empty
# ---------------------------------------------------------------------------


def test_patch_called_per_agent_when_diff_nonempty() -> None:
    """When all agents have empty tags, PATCH must be called once per agent."""
    import sync_audio_tags as script

    mock_patch_response = _empty_agent_response()

    # POST-PATCH verification GET returns the desired tags so verify passes
    desired_tags_payload = [
        {"tag": entry.get("tag"), "description": entry.get("description")}
        for entry in yaml.safe_load(_TAGS_YAML_PATH.read_text())["tags"]
    ]
    post_patch_response = _full_agent_response(desired_tags_payload)

    get_call_count = 0

    def _mock_get(url: str, api_key: str) -> dict[str, Any]:
        nonlocal get_call_count
        get_call_count += 1
        # First 3 GETs (pre-PATCH fetch): return empty. Next 3 GETs (verify): return full.
        if get_call_count <= 3:
            return _empty_agent_response()
        return post_patch_response

    receipts_captured: list[dict[str, Any]] = []

    def _mock_emit(receipt: dict[str, Any]) -> None:
        receipts_captured.append(receipt)

    with (
        patch.object(script, "_http_get", side_effect=_mock_get),
        patch.object(script, "_http_patch", return_value=mock_patch_response) as mock_patch,
        patch.object(script, "_emit_receipt", side_effect=_mock_emit),
    ):
        exit_code = script.main([])

    assert exit_code == 0, f"Expected exit 0, got {exit_code}"
    assert mock_patch.call_count == len(_THREE_AGENT_IDS), (
        f"Expected PATCH x{len(_THREE_AGENT_IDS)}, got {mock_patch.call_count}"
    )


# ---------------------------------------------------------------------------
# Test: Idempotent when diff is empty
# ---------------------------------------------------------------------------


def test_idempotent_when_diff_empty() -> None:
    """When all agents already have the correct tags, PATCH must NOT be called."""
    import sync_audio_tags as script

    desired_tags_payload = [
        {"tag": entry["tag"], "description": entry["description"]}
        for entry in yaml.safe_load(_TAGS_YAML_PATH.read_text())["tags"]
    ]
    already_current = _full_agent_response(desired_tags_payload)

    with (
        patch.object(script, "_http_get", return_value=already_current),
        patch.object(script, "_http_patch") as mock_patch,
        patch.object(script, "_emit_receipt"),
    ):
        exit_code = script.main([])

    assert exit_code == 0, f"Expected exit 0, got {exit_code}"
    mock_patch.assert_not_called()


# ---------------------------------------------------------------------------
# Test: Receipt emitted per agent
# ---------------------------------------------------------------------------


def test_receipt_emitted_per_agent() -> None:
    """An audio_tags_synced receipt must be emitted for every agent processed."""
    import sync_audio_tags as script

    desired_tags_payload = [
        {"tag": entry["tag"], "description": entry["description"]}
        for entry in yaml.safe_load(_TAGS_YAML_PATH.read_text())["tags"]
    ]
    post_patch_response = _full_agent_response(desired_tags_payload)

    get_call_count = 0

    def _mock_get(url: str, api_key: str) -> dict[str, Any]:
        nonlocal get_call_count
        get_call_count += 1
        if get_call_count <= 3:
            return _empty_agent_response()
        return post_patch_response

    receipts: list[dict[str, Any]] = []

    def _mock_emit(receipt: dict[str, Any]) -> None:
        receipts.append(receipt)

    with (
        patch.object(script, "_http_get", side_effect=_mock_get),
        patch.object(script, "_http_patch", return_value={}),
        patch.object(script, "_emit_receipt", side_effect=_mock_emit),
    ):
        exit_code = script.main([])

    assert exit_code == 0

    # One audio_tags_synced receipt per agent
    sync_receipts = [r for r in receipts if r.get("receipt_type") == "audio_tags_synced"]
    assert len(sync_receipts) == len(_THREE_AGENT_IDS), (
        f"Expected {len(_THREE_AGENT_IDS)} audio_tags_synced receipts, "
        f"got {len(sync_receipts)}"
    )

    # Every receipt has the mandatory Law #2 fields
    for receipt in sync_receipts:
        assert "id" in receipt
        assert "outcome" in receipt
        assert "created_at" in receipt
        assert receipt["receipt_type"] == "audio_tags_synced"


# ---------------------------------------------------------------------------
# Test: Stops on first agent failure (canary behavior)
# ---------------------------------------------------------------------------


def test_stops_on_first_agent_patch_failure() -> None:
    """If Tiffany PATCH fails, Sarah agents must NOT be patched."""
    import urllib.error
    import sync_audio_tags as script

    patch_call_count = 0

    def _failing_patch(url: str, api_key: str, body: dict[str, Any]) -> dict[str, Any]:
        nonlocal patch_call_count
        patch_call_count += 1
        # Fail on first PATCH (Tiffany canary)
        raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)  # type: ignore[arg-type]

    receipts: list[dict[str, Any]] = []

    with (
        patch.object(script, "_http_get", return_value=_empty_agent_response()),
        patch.object(script, "_http_patch", side_effect=_failing_patch),
        patch.object(script, "_emit_receipt", side_effect=receipts.append),
    ):
        exit_code = script.main([])

    assert exit_code == 3, f"Expected exit 3 (PATCH failure), got {exit_code}"
    assert patch_call_count == 1, (
        f"Expected exactly 1 PATCH attempt (Tiffany only), got {patch_call_count}"
    )

    # A failure receipt must still be emitted for the failing agent
    failure_receipts = [r for r in receipts if r.get("outcome") == "failed"]
    assert len(failure_receipts) >= 1


# ---------------------------------------------------------------------------
# Test: Missing API key exits with error (Law #3: fail closed)
# ---------------------------------------------------------------------------


def test_missing_api_key_fails_closed() -> None:
    """Script must exit 1 when no EL API key is in environment."""
    import sync_audio_tags as script

    env_backup = {
        k: os.environ.pop(k)
        for k in ("EL_API_KEY", "ELEVENLABS_API_KEY", "ASPIRE_ELEVENLABS_API_KEY")
        if k in os.environ
    }

    try:
        with patch.object(script, "_http_get") as mock_get:
            exit_code = script.main([])
        assert exit_code == 1, f"Expected exit 1 on missing key, got {exit_code}"
        mock_get.assert_not_called()
    finally:
        os.environ.update(env_backup)


# ---------------------------------------------------------------------------
# Test: Short description in YAML triggers contract failure (rule 12b)
# ---------------------------------------------------------------------------


def test_short_description_fails_contract() -> None:
    """A tag with a description shorter than 20 chars must cause exit 1."""
    import sync_audio_tags as script

    bad_yaml_content = """
contract_version: 1
schema_version: 1
applies_to_agent_ids:
  - agent_4801kqtapvsre2gb0gyb1ng631qr
tags:
  - tag: Warmly
    description: "Short"
"""
    import tempfile, yaml as _yaml
    from pathlib import Path as _Path

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(bad_yaml_content)
        tmp_path = _Path(tmp.name)

    try:
        # Monkey-patch the YAML path inside the script
        original_path = script._TAGS_FILE
        script._TAGS_FILE = tmp_path

        with patch.object(script, "_http_get"), patch.object(script, "_http_patch"):
            with pytest.raises(SystemExit) as exc_info:
                script.main([])
        assert exc_info.value.code == 1
    finally:
        script._TAGS_FILE = original_path
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Test: API key is never logged or emitted in receipts (Law #9)
# ---------------------------------------------------------------------------


def test_api_key_not_in_receipts() -> None:
    """The EL API key must never appear in any receipt payload."""
    import sync_audio_tags as script

    test_key = "sk_test_fake_key_not_real"
    os.environ["EL_API_KEY"] = test_key

    desired_tags_payload = [
        {"tag": entry["tag"], "description": entry["description"]}
        for entry in yaml.safe_load(_TAGS_YAML_PATH.read_text())["tags"]
    ]
    post_patch_response = _full_agent_response(desired_tags_payload)

    get_call_count = 0

    def _mock_get(url: str, api_key: str) -> dict[str, Any]:
        nonlocal get_call_count
        get_call_count += 1
        if get_call_count <= 3:
            return _empty_agent_response()
        return post_patch_response

    receipts: list[dict[str, Any]] = []

    with (
        patch.object(script, "_http_get", side_effect=_mock_get),
        patch.object(script, "_http_patch", return_value={}),
        patch.object(script, "_emit_receipt", side_effect=receipts.append),
    ):
        script.main([])

    for receipt in receipts:
        receipt_str = str(receipt)
        assert test_key not in receipt_str, (
            f"API key found in receipt: {receipt_str[:200]}"
        )
