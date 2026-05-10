"""Pass 7.5 — Tests for sync_el_tests.py.

Test plan:
  1. test_walks_el_tests_tree_finds_36_files
     Walker discovers exactly 36 YAML files (12 per agent x 3 agents).

  2. test_idempotency_skips_unchanged_yaml
     If a test name already exists on EL, sync skips it and emits skipped receipt.

  3. test_strict_blocks_missing_success_criteria
     With --strict, any test missing success_condition is blocked; exit 2.

  4. test_receipt_per_upload
     Every successful upload emits exactly one el_test_synced receipt.

  5. test_handles_el_api_failure_gracefully
     When EL API returns a non-2xx status, sync continues (partial failure),
     emits api_error receipt, and exits 1.

  6. test_dry_run_emits_no_real_uploads
     --dry-run validates YAML but does not call the EL POST endpoint.

  7. test_all_36_yaml_files_have_required_fields
     Every YAML file on disk has name, agent_id, and type fields.

All tests are offline (no network, no Supabase). HTTP helpers and store_receipts
are fully mocked.

Aspire Laws verified:
    Law #2  — receipt emitted per upload (test 4).
    Law #3  — strict mode blocks missing success_condition (test 3).
    Law #9  — API key never appears in receipts (spot-checked in test 4).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_PATH = _REPO_ROOT / "src"
_SCRIPTS_PATH = _REPO_ROOT / "scripts"

for _p in (_SRC_PATH, _SCRIPTS_PATH):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

import sync_el_tests as _sync  # noqa: E402

# ---------------------------------------------------------------------------
# Constants matching the three target receptionist agents
# ---------------------------------------------------------------------------

_AGENT_IDS = {
    "agent_4801kqtapvsre2gb0gyb1ng631qr",   # Tiffany
    "agent_6501kp71h69jfqysgd055hemqhrq",   # Sarah-Receptionist
    "agent_8901kmqdjnrte7psp6en4f85m4kt",   # Sarah-FrontDesk
}
_EXPECTED_TESTS_PER_AGENT = 11  # t04 deleted 2026-05-09 (infra coverage moved to unit tests)
_EXPECTED_TOTAL = len(_AGENT_IDS) * _EXPECTED_TESTS_PER_AGENT  # 33

_EL_TESTS_DIR = _REPO_ROOT / "src" / "aspire_orchestrator" / "config" / "el_tests"


# ---------------------------------------------------------------------------
# Autouse fixture: suppress real Supabase calls from module import side effects
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _suppress_supabase_import() -> Any:
    """Suppress Supabase calls that may occur at import time (not in tests themselves).
    Tests that capture receipts must patch sync_el_tests.store_receipts directly."""
    with patch("aspire_orchestrator.services.receipt_store.store_receipts"):
        yield


# ---------------------------------------------------------------------------
# Test 1 — Tree walker finds exactly 36 YAML files
# ---------------------------------------------------------------------------


class TestFileDiscovery:
    def test_walks_el_tests_tree_finds_33_files(self) -> None:
        """Walker must discover exactly 33 YAML files across 3 agent dirs (11 per agent; t04 removed)."""
        yaml_files = list(_EL_TESTS_DIR.rglob("*.yaml"))
        agent_dirs = {f.parent.name for f in yaml_files}

        assert len(yaml_files) == _EXPECTED_TOTAL, (
            f"Expected {_EXPECTED_TOTAL} YAML test files, found {len(yaml_files)}. "
            f"Files: {[f.name for f in yaml_files]}"
        )
        assert agent_dirs == _AGENT_IDS, (
            f"Expected agent dirs {_AGENT_IDS}, found {agent_dirs}"
        )

        # Verify exactly 11 per agent.
        for agent_id in _AGENT_IDS:
            agent_files = [f for f in yaml_files if f.parent.name == agent_id]
            assert len(agent_files) == _EXPECTED_TESTS_PER_AGENT, (
                f"Agent {agent_id}: expected {_EXPECTED_TESTS_PER_AGENT} tests, "
                f"got {len(agent_files)}: {[f.name for f in agent_files]}"
            )


# ---------------------------------------------------------------------------
# Test 2 — Idempotency: existing test names are skipped
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_idempotency_skips_unchanged_yaml(self) -> None:
        """If all test names are already on EL, sync skips all and exits 0."""
        # Build a fake "already exists" list containing all 36 test names.
        all_names: set[str] = set()
        for yaml_path in _EL_TESTS_DIR.rglob("*.yaml"):
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            all_names.add(data.get("name", yaml_path.stem))

        def _mock_list(agent_id: str, api_key: str) -> set[str]:
            return all_names

        captured_receipts: list[dict[str, Any]] = []

        def _mock_store(receipts: list[dict[str, Any]]) -> None:
            captured_receipts.extend(receipts)

        mock_post = MagicMock()

        with patch.object(_sync, "store_receipts", side_effect=_mock_store):
            exit_code = _sync.sync_tests(
                api_key="sk_test_fake",
                dry_run=False,
                strict=False,
                el_tests_dir=_EL_TESTS_DIR,
                post_fn=mock_post,
                list_fn=_mock_list,
            )

        # No POST calls when all tests already exist.
        mock_post.assert_not_called()

        # Exit 0 — idempotent skip is not an error.
        assert exit_code == 0, f"Expected exit 0 for idempotent run, got {exit_code}"

        # Each skipped test emits a skipped_idempotent receipt (33 tests, 11 per agent).
        skip_receipts = [
            r for r in captured_receipts
            if r.get("outcome") == "skipped_idempotent"
        ]
        assert len(skip_receipts) == _EXPECTED_TOTAL, (
            f"Expected {_EXPECTED_TOTAL} skip receipts, got {len(skip_receipts)}"
        )


# ---------------------------------------------------------------------------
# Test 3 — Strict mode blocks tests missing success_condition
# ---------------------------------------------------------------------------


class TestStrictMode:
    def test_strict_blocks_missing_success_criteria(self, tmp_path: Path) -> None:
        """--strict: any llm or simulation test missing success_condition blocks sync (exit 2)."""
        # Create a minimal test YAML missing success_condition.
        agent_dir = tmp_path / "agent_fake123"
        agent_dir.mkdir()
        bad_test = agent_dir / "bad_test.yaml"
        bad_test.write_text(
            "name: bad_test\nagent_id: agent_fake123\ntype: llm\n"
            "chat_history:\n  - role: user\n    time_in_call_secs: 0\n    message: hello\n",
            encoding="utf-8",
        )

        captured_receipts: list[dict[str, Any]] = []

        def _mock_store(receipts: list[dict[str, Any]]) -> None:
            captured_receipts.extend(receipts)

        def _mock_list(agent_id: str, api_key: str) -> set[str]:
            return set()

        mock_post = MagicMock()

        with patch.object(_sync, "store_receipts", side_effect=_mock_store):
            exit_code = _sync.sync_tests(
                api_key="sk_test_fake",
                dry_run=False,
                strict=True,
                el_tests_dir=tmp_path,
                post_fn=mock_post,
                list_fn=_mock_list,
            )

        # strict=True -> exit 2 (strict validation failure).
        assert exit_code == 2, f"Expected exit 2 for strict failure, got {exit_code}"

        # No POST should have been called.
        mock_post.assert_not_called()

        # A strict_blocked receipt must have been emitted.
        blocked_receipts = [
            r for r in captured_receipts
            if r.get("outcome") == "strict_blocked"
        ]
        assert len(blocked_receipts) == 1, (
            f"Expected 1 strict_blocked receipt, got {len(blocked_receipts)}"
        )
        assert "strict" in blocked_receipts[0]["redacted_outputs"].get("reason", "").lower() or \
               "success_condition" in blocked_receipts[0]["redacted_outputs"].get("reason", ""), (
            f"Strict block reason unclear: {blocked_receipts[0]}"
        )

    def test_strict_passes_when_all_tests_have_success_condition(self) -> None:
        """All 36 real YAML files must pass --strict validation."""
        # This also validates the YAML files themselves.
        for yaml_path in _EL_TESTS_DIR.rglob("*.yaml"):
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            test_type = data.get("type", "")
            if test_type in ("llm", "simulation"):
                assert data.get("success_condition"), (
                    f"{yaml_path.name}: type={test_type} but missing success_condition"
                )
            elif test_type == "tool":
                assert data.get("tool_call_assertions"), (
                    f"{yaml_path.name}: type=tool but missing tool_call_assertions"
                )


# ---------------------------------------------------------------------------
# Test 4 — Receipt emitted per upload
# ---------------------------------------------------------------------------


class TestReceiptPerUpload:
    def test_receipt_per_upload(self, tmp_path: Path) -> None:
        """Every successful EL API upload emits exactly one el_test_synced receipt."""
        # Create 3 minimal valid test YAMLs in one agent dir.
        agent_dir = tmp_path / "agent_test999"
        agent_dir.mkdir()
        for i in range(3):
            test_file = agent_dir / f"t{i:02d}_test.yaml"
            test_file.write_text(
                f"name: test_{i:02d}\n"
                f"agent_id: agent_test999\n"
                f"type: llm\n"
                f"success_condition: The agent greets the caller professionally.\n"
                f"chat_history:\n"
                f"  - role: user\n"
                f"    time_in_call_secs: 0\n"
                f"    message: Hello\n",
                encoding="utf-8",
            )

        captured_receipts: list[dict[str, Any]] = []

        def _mock_store(receipts: list[dict[str, Any]]) -> None:
            captured_receipts.extend(receipts)

        def _mock_list(agent_id: str, api_key: str) -> set[str]:
            return set()  # Nothing pre-exists.

        def _mock_post(url: str, api_key: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return 201, {"id": f"el_test_{payload['name']}"}

        with patch.object(_sync, "store_receipts", side_effect=_mock_store):
            exit_code = _sync.sync_tests(
                api_key="sk_test_fake",
                dry_run=False,
                strict=False,
                el_tests_dir=tmp_path,
                post_fn=_mock_post,
                list_fn=_mock_list,
            )

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        synced_receipts = [
            r for r in captured_receipts
            if r.get("outcome") == "synced"
        ]
        assert len(synced_receipts) == 3, (
            f"Expected 3 synced receipts, got {len(synced_receipts)}"
        )

        # Law #9: API key must NOT appear in any receipt.
        api_key = "sk_test_fake"
        for r in captured_receipts:
            assert api_key not in str(r), (
                f"API key found in receipt: {r}"
            )

        # Law #2: Each receipt has required fields.
        for r in synced_receipts:
            assert r.get("receipt_type") == "el_test_synced"
            assert r.get("receipt_id")
            assert r["redacted_inputs"].get("agent_id")
            assert r["redacted_inputs"].get("test_name")


# ---------------------------------------------------------------------------
# Test 5 — EL API failure handled gracefully
# ---------------------------------------------------------------------------


class TestApiFailure:
    def test_handles_el_api_failure_gracefully(self, tmp_path: Path) -> None:
        """Non-2xx EL API response -> api_error receipt; sync continues; exit 1."""
        agent_dir = tmp_path / "agent_fail999"
        agent_dir.mkdir()

        # 3 tests: first 2 succeed, third fails.
        for i in range(3):
            test_file = agent_dir / f"t{i:02d}_test.yaml"
            test_file.write_text(
                f"name: test_{i:02d}\n"
                f"agent_id: agent_fail999\n"
                f"type: llm\n"
                f"success_condition: The agent greets the caller.\n"
                f"chat_history:\n"
                f"  - role: user\n"
                f"    time_in_call_secs: 0\n"
                f"    message: Hello\n",
                encoding="utf-8",
            )

        call_count = {"n": 0}

        def _flaky_post(
            url: str, api_key: str, payload: dict[str, Any]
        ) -> tuple[int, dict[str, Any] | None]:
            call_count["n"] += 1
            if call_count["n"] == 3:
                return 500, None  # Third call fails.
            return 201, {"id": f"el_test_{payload['name']}"}

        captured_receipts: list[dict[str, Any]] = []

        def _mock_store(receipts: list[dict[str, Any]]) -> None:
            captured_receipts.extend(receipts)

        def _mock_list(agent_id: str, api_key: str) -> set[str]:
            return set()

        with patch.object(_sync, "store_receipts", side_effect=_mock_store):
            exit_code = _sync.sync_tests(
                api_key="sk_test_fake",
                dry_run=False,
                strict=False,
                el_tests_dir=tmp_path,
                post_fn=_flaky_post,
                list_fn=_mock_list,
            )

        # Exit 1 due to API failure (not 0).
        assert exit_code == 1, f"Expected exit 1 for API failure, got {exit_code}"

        # 2 synced, 1 api_error receipt.
        synced = [r for r in captured_receipts if r.get("outcome") == "synced"]
        errors = [r for r in captured_receipts if r.get("outcome") == "api_error"]
        assert len(synced) == 2, f"Expected 2 synced receipts, got {len(synced)}"
        assert len(errors) == 1, f"Expected 1 api_error receipt, got {len(errors)}"

        # Sync must continue after the error (partial failure is not a hard stop).
        assert call_count["n"] == 3, (
            "Sync must attempt all 3 tests even when one fails"
        )


# ---------------------------------------------------------------------------
# Test 6 — Dry-run emits no real uploads
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_emits_no_real_uploads(self) -> None:
        """--dry-run must not call the EL POST endpoint for any test."""
        mock_post = MagicMock()

        with patch.object(_sync, "store_receipts"):
            exit_code = _sync.sync_tests(
                api_key="sk_test_fake",
                dry_run=True,
                strict=False,
                el_tests_dir=_EL_TESTS_DIR,
                post_fn=mock_post,
                list_fn=lambda a, k: set(),
            )

        mock_post.assert_not_called()
        assert exit_code == 0, f"Dry-run must exit 0, got {exit_code}"


# ---------------------------------------------------------------------------
# Test 7 — All 36 YAML files on disk have required fields
# ---------------------------------------------------------------------------


class TestYamlFileIntegrity:
    def test_all_33_yaml_files_have_required_fields(self) -> None:
        """Every on-disk YAML must have: name, agent_id, type fields (33 files; t04 removed)."""
        yaml_files = list(_EL_TESTS_DIR.rglob("*.yaml"))
        assert len(yaml_files) == _EXPECTED_TOTAL, (
            f"Expected {_EXPECTED_TOTAL} files, found {len(yaml_files)}"
        )

        required = ("name", "agent_id", "type")
        for yaml_path in yaml_files:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            for field in required:
                assert field in data, (
                    f"{yaml_path.name}: missing required field '{field}'"
                )
            # Agent ID in YAML must match the directory name.
            dir_agent_id = yaml_path.parent.name
            yaml_agent_id = data.get("agent_id", "")
            assert yaml_agent_id == dir_agent_id, (
                f"{yaml_path.name}: agent_id in YAML '{yaml_agent_id}' "
                f"does not match directory '{dir_agent_id}'"
            )
            # Test name must not be empty.
            assert data.get("name"), (
                f"{yaml_path.name}: test name is empty or missing"
            )
