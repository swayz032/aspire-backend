"""Pass 5 — sync_receptionist_kb.py tests.

Test plan:
  - test_walks_kb_tree_finds_10_files
  - test_walks_kb_tree_finds_at_least_10_files (spam/robocall handled in _base.md)
  - test_idempotency_skips_unchanged_sha256
  - test_upload_when_sha_changes
  - test_attach_to_all_three_agents
  - test_strict_mode_blocks_missing_frontmatter
  - test_receipt_per_upload_and_per_attach
  - test_handles_el_api_failure_gracefully
  - test_kb_topic_coverage_matrix
  - test_dry_run_no_upload_called
  - test_no_api_key_fails_closed
  - test_api_key_not_in_receipts

All tests are fully offline (no network, no Supabase). EL HTTP calls and
receipt_store.store_receipts are mocked per-test.

Law #2: every code path emits receipts — asserted in every test.
Law #3: missing EL_API_KEY without --dry-run -> exit 1 (fail closed).
Law #9: API key must never appear in any receipt.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — ensure scripts and src are importable.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_PATH = _REPO_ROOT / "src"
_SCRIPTS_PATH = _REPO_ROOT / "scripts"

for _p in (_SRC_PATH, _SCRIPTS_PATH, str(_REPO_ROOT)):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

_FAKE_API_KEY = "sk_test_fake_el_key_not_real"

# ---------------------------------------------------------------------------
# KB root for the real file tree (used by coverage matrix tests)
# ---------------------------------------------------------------------------

_ACTUAL_KB_ROOT = (
    _REPO_ROOT.parent.parent
    / "Aspire-desktop"
    / "docs"
    / "agents"
    / "sarah-receptionist"
    / "kb"
)

# ---------------------------------------------------------------------------
# Minimal valid frontmatter shared across tests
# ---------------------------------------------------------------------------

_VALID_FM = """\
---
title: Test KB Doc
agent_scope: [tiffany, sarah_receptionist, sarah_frontdesk]
priority: high
business_type: contractor
trade_scope: [shared]
last_reviewed: 2026-05-09
sme_approved_by: tonio_scott
contract_version: 1
---

# Test content

Some body text here.
"""

_INVALID_FM = "# No frontmatter at all\n\nJust body text.\n"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# Helper: run main() with controlled env
# ---------------------------------------------------------------------------

def _run_main(
    argv: list[str],
    *,
    kb_root: Path,
    api_key: str = _FAKE_API_KEY,
    mock_list_docs: list[dict[str, Any]] | None = None,
    mock_upload_return: str = "doc_id_mock_001",
    mock_attach_side_effect: Exception | None = None,
    capture_receipts: list[dict[str, Any]] | None = None,
) -> int:
    """Run sync_receptionist_kb.main() with KB_ROOT patched and HTTP calls mocked."""
    if mock_list_docs is None:
        mock_list_docs = []

    emitted: list[dict[str, Any]] = capture_receipts if capture_receipts is not None else []

    def _capture(receipts: list[dict[str, Any]]) -> None:
        emitted.extend(receipts)

    env_patch = {"EL_API_KEY": api_key}

    import sync_receptionist_kb as script

    def _fake_list_kb_docs(api_key_arg: str) -> list[dict[str, Any]]:
        return mock_list_docs  # type: ignore[return-value]

    def _fake_upload(api_key_arg: str, file_name: str, content: bytes) -> str:
        return mock_upload_return

    def _fake_attach(api_key_arg: str, agent_id: str, doc_id: str, usage_mode: str = "auto") -> None:
        if mock_attach_side_effect is not None:
            raise mock_attach_side_effect

    with (
        patch.dict(os.environ, env_patch),
        patch.object(script, "KB_ROOT", kb_root),
        patch.object(script, "_list_kb_docs", side_effect=_fake_list_kb_docs),
        patch.object(script, "_upload_kb_doc", side_effect=_fake_upload),
        patch.object(script, "_attach_kb_doc_to_agent", side_effect=_fake_attach),
        patch.object(script, "_delete_kb_doc", return_value=None),
        patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
            side_effect=_capture,
        ),
    ):
        return script.main(argv)


def _make_kb_tree(
    tmp_dir: Path,
    files: dict[str, str],  # relative path -> content
) -> Path:
    """Create a temp KB tree. Returns the root path."""
    for rel, content in files.items():
        p = tmp_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_dir


# ---------------------------------------------------------------------------
# Tests: KB tree traversal
# ---------------------------------------------------------------------------


class TestWalksKBTree:
    def test_walks_kb_tree_finds_10_files(self) -> None:
        """Exactly 10 markdown files in the real KB tree (6 shared + 4 trade packs).

        The _base.md file is an 11th shared file. The test verifies >= 10 because
        the spam/robocall behavior was folded into _base.md (per Pass 5 design decision).
        """
        if not _ACTUAL_KB_ROOT.exists():
            pytest.skip("KB root not found — run from workspace root")

        md_files = list(_ACTUAL_KB_ROOT.rglob("*.md"))
        assert len(md_files) >= 10, (
            f"Expected >= 10 KB files, found {len(md_files)}: "
            f"{[f.name for f in md_files]}"
        )

    def test_file_names_match_expected_set(self) -> None:
        """The expected 11 KB files are all present."""
        if not _ACTUAL_KB_ROOT.exists():
            pytest.skip("KB root not found — run from workspace root")

        md_files = {f.name for f in _ACTUAL_KB_ROOT.rglob("*.md")}
        expected = {
            "after-hours-handling.md",
            "common-business-faqs.md",
            "business-hours-detection.md",
            "greeting-and-time-awareness.md",
            "message-capture-canon.md",
            "transfer-policy-and-phrases.md",
            "_base.md",
            "hvac.md",
            "electrician.md",
            "plumber.md",
            "specialty_remodeler.md",
        }
        missing = expected - md_files
        assert not missing, f"Missing KB files: {missing}"


# ---------------------------------------------------------------------------
# Tests: Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_idempotency_skips_unchanged_sha256(self, tmp_path: Path) -> None:
        """When EL already has a doc with matching sha256, no upload call is made."""
        kb_root = _make_kb_tree(tmp_path, {"shared/doc.md": _VALID_FM})
        sha = _sha256(_VALID_FM.encode())

        upload_mock = MagicMock()
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_kb as script

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        existing = [{"id": "doc_123", "name": "doc.md", "sha256": sha}]

        with (
            patch.dict(os.environ, {"EL_API_KEY": _FAKE_API_KEY}),
            patch.object(script, "KB_ROOT", kb_root),
            patch.object(script, "_list_kb_docs", return_value=existing),
            patch.object(script, "_upload_kb_doc", upload_mock),
            patch.object(script, "_attach_kb_doc_to_agent", return_value=None),
            patch.object(script, "_delete_kb_doc", return_value=None),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=_capture,
            ),
        ):
            exit_code = script.main([])

        assert exit_code == 0
        upload_mock.assert_not_called()
        # Receipt must still be emitted (Law #2) even for skipped files
        skip_receipts = [r for r in emitted if r.get("receipt_type") == "kb_doc_upload"]
        assert len(skip_receipts) == 1
        # reason_code lives at the top level of the receipt dict
        assert skip_receipts[0]["reason_code"] == "IDEMPOTENT_SKIP"

    def test_upload_when_sha_changes(self, tmp_path: Path) -> None:
        """When EL has a doc with a DIFFERENT sha, upload is called with the new content."""
        kb_root = _make_kb_tree(tmp_path, {"shared/doc.md": _VALID_FM})
        old_sha = "aabbccddeeff" + "0" * 52  # fake old sha (wrong length — won't match)

        upload_mock = MagicMock(return_value="new_doc_id_456")
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_kb as script

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        existing = [{"id": "old_doc_123", "name": "doc.md", "sha256": old_sha}]

        with (
            patch.dict(os.environ, {"EL_API_KEY": _FAKE_API_KEY}),
            patch.object(script, "KB_ROOT", kb_root),
            patch.object(script, "_list_kb_docs", return_value=existing),
            patch.object(script, "_upload_kb_doc", upload_mock),
            patch.object(script, "_attach_kb_doc_to_agent", return_value=None),
            patch.object(script, "_delete_kb_doc", return_value=None),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=_capture,
            ),
        ):
            exit_code = script.main([])

        assert exit_code == 0
        upload_mock.assert_called_once()
        # Upload receipt should show UPLOADED
        upload_receipts = [r for r in emitted if r.get("receipt_type") == "kb_doc_upload"]
        assert len(upload_receipts) == 1
        assert upload_receipts[0].get("reason_code") == "UPLOADED"


# ---------------------------------------------------------------------------
# Tests: Attach to all three agents
# ---------------------------------------------------------------------------


class TestAttachToAllThreeAgents:
    def test_attach_to_all_three_agents(self, tmp_path: Path) -> None:
        """Each uploaded doc must be attached to all 3 receptionist agents."""
        kb_root = _make_kb_tree(tmp_path, {"shared/doc.md": _VALID_FM})

        attach_calls: list[tuple[str, str, str, str]] = []
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_kb as script

        def _fake_attach(api_key: str, agent_id: str, doc_id: str, usage_mode: str = "auto") -> None:
            attach_calls.append((api_key, agent_id, doc_id, usage_mode))

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        with (
            patch.dict(os.environ, {"EL_API_KEY": _FAKE_API_KEY}),
            patch.object(script, "KB_ROOT", kb_root),
            patch.object(script, "_list_kb_docs", return_value=[]),
            patch.object(script, "_upload_kb_doc", return_value="doc_xyz"),
            patch.object(script, "_attach_kb_doc_to_agent", side_effect=_fake_attach),
            patch.object(script, "_delete_kb_doc", return_value=None),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=_capture,
            ),
        ):
            exit_code = script.main([])

        assert exit_code == 0
        # 3 attach calls, one per agent
        assert len(attach_calls) == 3
        attached_agent_ids = {c[1] for c in attach_calls}
        expected_agent_ids = {aid for aid, _ in script.RECEPTIONIST_AGENTS}
        assert attached_agent_ids == expected_agent_ids

        # usage_mode must be "auto" for all
        for _, _, _, mode in attach_calls:
            assert mode == "auto", f"Expected usage_mode=auto, got {mode}"

        # 3 attach receipts emitted (Law #2)
        attach_receipts = [r for r in emitted if r.get("receipt_type") == "kb_attach"]
        assert len(attach_receipts) == 3


# ---------------------------------------------------------------------------
# Tests: Strict mode frontmatter
# ---------------------------------------------------------------------------


class TestStrictModeFrontmatter:
    def test_strict_mode_blocks_missing_frontmatter(self, tmp_path: Path) -> None:
        """In --strict mode, a file without frontmatter exits 2 and emits a failure receipt."""
        kb_root = _make_kb_tree(tmp_path, {"bad/no_fm.md": _INVALID_FM})

        emitted: list[dict[str, Any]] = []
        upload_mock = MagicMock()

        import sync_receptionist_kb as script

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        with (
            patch.dict(os.environ, {"EL_API_KEY": _FAKE_API_KEY}),
            patch.object(script, "KB_ROOT", kb_root),
            patch.object(script, "_list_kb_docs", return_value=[]),
            patch.object(script, "_upload_kb_doc", upload_mock),
            patch.object(script, "_attach_kb_doc_to_agent", return_value=None),
            patch.object(script, "_delete_kb_doc", return_value=None),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=_capture,
            ),
        ):
            exit_code = script.main([])  # default --strict ON

        assert exit_code == 2, f"Expected exit code 2, got {exit_code}"
        upload_mock.assert_not_called()

        # A failure receipt must be emitted even for blocked files (Law #2)
        fail_receipts = [
            r for r in emitted
            if r.get("receipt_type") == "kb_doc_upload" and r.get("outcome") == "failed"
        ]
        assert len(fail_receipts) >= 1
        assert fail_receipts[0]["reason_code"] == "FRONTMATTER_MISSING"

    def test_no_strict_allows_missing_frontmatter(self, tmp_path: Path) -> None:
        """--no-strict allows a file without frontmatter to proceed."""
        kb_root = _make_kb_tree(tmp_path, {"bad/no_fm.md": _INVALID_FM})

        upload_mock = MagicMock(return_value="doc_abc")
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_kb as script

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        with (
            patch.dict(os.environ, {"EL_API_KEY": _FAKE_API_KEY}),
            patch.object(script, "KB_ROOT", kb_root),
            patch.object(script, "_list_kb_docs", return_value=[]),
            patch.object(script, "_upload_kb_doc", upload_mock),
            patch.object(script, "_attach_kb_doc_to_agent", return_value=None),
            patch.object(script, "_delete_kb_doc", return_value=None),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=_capture,
            ),
        ):
            exit_code = script.main(["--no-strict"])

        assert exit_code == 0
        upload_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: Receipt coverage
# ---------------------------------------------------------------------------


class TestReceiptCoverage:
    def test_receipt_per_upload_and_per_attach(self, tmp_path: Path) -> None:
        """10 docs: each produces 1 upload receipt + 3 attach receipts = 40 total."""
        # Build 10 files (6 shared + 4 trade packs)
        files = {
            f"shared/doc{i}.md": _VALID_FM for i in range(6)
        }
        files.update({
            f"trade_packs/trade{i}.md": _VALID_FM for i in range(4)
        })
        kb_root = _make_kb_tree(tmp_path, files)

        doc_id_counter = {"n": 0}

        def _fake_upload(api_key: str, file_name: str, content: bytes) -> str:
            doc_id_counter["n"] += 1
            return f"doc_{doc_id_counter['n']:03d}"

        emitted: list[dict[str, Any]] = []

        import sync_receptionist_kb as script

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        with (
            patch.dict(os.environ, {"EL_API_KEY": _FAKE_API_KEY}),
            patch.object(script, "KB_ROOT", kb_root),
            patch.object(script, "_list_kb_docs", return_value=[]),
            patch.object(script, "_upload_kb_doc", side_effect=_fake_upload),
            patch.object(script, "_attach_kb_doc_to_agent", return_value=None),
            patch.object(script, "_delete_kb_doc", return_value=None),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=_capture,
            ),
        ):
            exit_code = script.main([])

        assert exit_code == 0

        upload_receipts = [r for r in emitted if r.get("receipt_type") == "kb_doc_upload"]
        attach_receipts = [r for r in emitted if r.get("receipt_type") == "kb_attach"]

        assert len(upload_receipts) == 10, (
            f"Expected 10 upload receipts, got {len(upload_receipts)}"
        )
        assert len(attach_receipts) == 30, (
            f"Expected 30 attach receipts (10 docs x 3 agents), got {len(attach_receipts)}"
        )

    def test_all_receipts_have_unique_ids(self, tmp_path: Path) -> None:
        """Every emitted receipt must have a unique id."""
        files = {f"shared/doc{i}.md": _VALID_FM for i in range(3)}
        kb_root = _make_kb_tree(tmp_path, files)
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_kb as script

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        with (
            patch.dict(os.environ, {"EL_API_KEY": _FAKE_API_KEY}),
            patch.object(script, "KB_ROOT", kb_root),
            patch.object(script, "_list_kb_docs", return_value=[]),
            patch.object(script, "_upload_kb_doc", return_value="doc_001"),
            patch.object(script, "_attach_kb_doc_to_agent", return_value=None),
            patch.object(script, "_delete_kb_doc", return_value=None),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=_capture,
            ),
        ):
            script.main([])

        ids = [r["id"] for r in emitted if "id" in r]
        assert len(ids) == len(set(ids)), "Duplicate receipt IDs found"


# ---------------------------------------------------------------------------
# Tests: Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_handles_el_api_failure_gracefully(self, tmp_path: Path) -> None:
        """EL upload error: logs, emits failure receipt, continues with remaining docs."""
        files = {
            "shared/doc1.md": _VALID_FM,
            "shared/doc2.md": _VALID_FM,
        }
        kb_root = _make_kb_tree(tmp_path, files)

        call_count = {"n": 0}

        def _failing_upload(api_key: str, file_name: str, content: bytes) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("EL API 503 Service Unavailable")
            return "doc_002"

        emitted: list[dict[str, Any]] = []

        import sync_receptionist_kb as script

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        with (
            patch.dict(os.environ, {"EL_API_KEY": _FAKE_API_KEY}),
            patch.object(script, "KB_ROOT", kb_root),
            patch.object(script, "_list_kb_docs", return_value=[]),
            patch.object(script, "_upload_kb_doc", side_effect=_failing_upload),
            patch.object(script, "_attach_kb_doc_to_agent", return_value=None),
            patch.object(script, "_delete_kb_doc", return_value=None),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=_capture,
            ),
        ):
            exit_code = script.main([])

        # Exit 2 because at least one file failed
        assert exit_code == 2

        # The second doc should have been processed despite first failure
        upload_receipts = [r for r in emitted if r.get("receipt_type") == "kb_doc_upload"]
        assert len(upload_receipts) == 2, (
            f"Expected 2 upload receipts (one failed, one succeeded), got {len(upload_receipts)}"
        )

        fail_receipts = [r for r in upload_receipts if r.get("outcome") == "failed"]
        success_receipts = [r for r in upload_receipts if r.get("outcome") == "success"]
        assert len(fail_receipts) == 1
        assert len(success_receipts) == 1

    def test_attach_failure_emits_failure_receipt(self, tmp_path: Path) -> None:
        """Attach failure emits a failure receipt but does not crash the script."""
        kb_root = _make_kb_tree(tmp_path, {"shared/doc.md": _VALID_FM})
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_kb as script

        def _failing_attach(api_key: str, agent_id: str, doc_id: str, usage_mode: str = "auto") -> None:
            raise RuntimeError("EL API 404 Agent Not Found")

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        with (
            patch.dict(os.environ, {"EL_API_KEY": _FAKE_API_KEY}),
            patch.object(script, "KB_ROOT", kb_root),
            patch.object(script, "_list_kb_docs", return_value=[]),
            patch.object(script, "_upload_kb_doc", return_value="doc_001"),
            patch.object(script, "_attach_kb_doc_to_agent", side_effect=_failing_attach),
            patch.object(script, "_delete_kb_doc", return_value=None),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=_capture,
            ),
        ):
            exit_code = script.main([])

        # Attach failures set had_failure = True -> exit 2
        assert exit_code == 2

        fail_attach_receipts = [
            r for r in emitted
            if r.get("receipt_type") == "kb_attach" and r.get("outcome") == "failed"
        ]
        assert len(fail_attach_receipts) == 3  # All 3 agents failed


# ---------------------------------------------------------------------------
# Tests: Dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_no_upload_called(self, tmp_path: Path) -> None:
        """--dry-run must never call _upload_kb_doc or _attach_kb_doc_to_agent."""
        kb_root = _make_kb_tree(tmp_path, {"shared/doc.md": _VALID_FM})

        upload_mock = MagicMock()
        attach_mock = MagicMock()
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_kb as script

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        with (
            patch.dict(os.environ, {"EL_API_KEY": _FAKE_API_KEY}),
            patch.object(script, "KB_ROOT", kb_root),
            patch.object(script, "_list_kb_docs", return_value=[]),
            patch.object(script, "_upload_kb_doc", upload_mock),
            patch.object(script, "_attach_kb_doc_to_agent", attach_mock),
            patch.object(script, "_delete_kb_doc", return_value=None),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=_capture,
            ),
        ):
            exit_code = script.main(["--dry-run"])

        assert exit_code == 0
        upload_mock.assert_not_called()
        attach_mock.assert_not_called()

        # Receipts still emitted even in dry-run mode (Law #2)
        all_receipts = [r for r in emitted if r.get("receipt_type") in ("kb_doc_upload", "kb_attach")]
        assert len(all_receipts) >= 1
        for r in all_receipts:
            assert r.get("redacted_inputs", {}).get("dry_run") is True


# ---------------------------------------------------------------------------
# Tests: Security / Law #3 / Law #9
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_no_api_key_fails_closed(self, tmp_path: Path) -> None:
        """Missing EL_API_KEY without --dry-run must exit 1 (Law #3: fail closed)."""
        kb_root = _make_kb_tree(tmp_path, {"shared/doc.md": _VALID_FM})

        import sync_receptionist_kb as script

        env = os.environ.copy()
        env.pop("EL_API_KEY", None)

        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(script, "KB_ROOT", kb_root),
        ):
            exit_code = script.main([])

        assert exit_code == 1, f"Expected exit 1 (fail closed), got {exit_code}"

    def test_api_key_not_in_receipts(self, tmp_path: Path) -> None:
        """The EL API key must never appear in any emitted receipt (Law #9)."""
        kb_root = _make_kb_tree(tmp_path, {"shared/doc.md": _VALID_FM})
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_kb as script

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        with (
            patch.dict(os.environ, {"EL_API_KEY": _FAKE_API_KEY}),
            patch.object(script, "KB_ROOT", kb_root),
            patch.object(script, "_list_kb_docs", return_value=[]),
            patch.object(script, "_upload_kb_doc", return_value="doc_001"),
            patch.object(script, "_attach_kb_doc_to_agent", return_value=None),
            patch.object(script, "_delete_kb_doc", return_value=None),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=_capture,
            ),
        ):
            script.main([])

        for r in emitted:
            receipt_str = str(r)
            assert _FAKE_API_KEY not in receipt_str, (
                f"API key leaked into receipt: {receipt_str[:200]}"
            )

    def test_dry_run_allowed_without_api_key(self, tmp_path: Path) -> None:
        """--dry-run must work without EL_API_KEY (no network call needed)."""
        kb_root = _make_kb_tree(tmp_path, {"shared/doc.md": _VALID_FM})

        import sync_receptionist_kb as script

        env = os.environ.copy()
        env.pop("EL_API_KEY", None)

        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(script, "KB_ROOT", kb_root),
            patch.object(script, "_list_kb_docs", return_value=[]),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            exit_code = script.main(["--dry-run"])

        assert exit_code == 0


# ---------------------------------------------------------------------------
# Tests: KB topic coverage matrix
# ---------------------------------------------------------------------------


class TestKBTopicCoverageMatrix:
    """Verify the 11 KB files cover all required contractor receptionist topics.

    Topic -> expected KB file basename(s):
      1. After-hours handling         -> after-hours-handling.md
      2. Business hours detection     -> business-hours-detection.md
      3. Greeting and time awareness  -> greeting-and-time-awareness.md
      4. Message capture              -> message-capture-canon.md
      5. Transfer policy              -> transfer-policy-and-phrases.md
      6. FAQ (real Q&A)               -> common-business-faqs.md
      7. HVAC trade pack              -> hvac.md
      8. Electrician trade pack       -> electrician.md
      9. Plumber trade pack           -> plumber.md
     10. Specialty remodeler pack     -> specialty_remodeler.md
     11. Shared base behaviors        -> _base.md
    """

    _EXPECTED_TOPICS: dict[str, str] = {
        "after_hours": "after-hours-handling.md",
        "business_hours_detection": "business-hours-detection.md",
        "greeting_time_awareness": "greeting-and-time-awareness.md",
        "message_capture": "message-capture-canon.md",
        "transfer_policy": "transfer-policy-and-phrases.md",
        "faq": "common-business-faqs.md",
        "hvac": "hvac.md",
        "electrician": "electrician.md",
        "plumber": "plumber.md",
        "specialty_remodeler": "specialty_remodeler.md",
        "shared_base": "_base.md",
    }

    def test_kb_topic_coverage_matrix(self) -> None:
        """All 11 expected topic files exist in the KB tree."""
        if not _ACTUAL_KB_ROOT.exists():
            pytest.skip("KB root not found — run from workspace root")

        existing_files = {f.name for f in _ACTUAL_KB_ROOT.rglob("*.md")}

        missing_topics: list[str] = []
        for topic, expected_file in self._EXPECTED_TOPICS.items():
            if expected_file not in existing_files:
                missing_topics.append(f"{topic} -> {expected_file}")

        assert not missing_topics, (
            f"Missing topic coverage files:\n" + "\n".join(missing_topics)
        )

    def test_all_kb_files_have_required_frontmatter(self) -> None:
        """All KB files in the real tree must have all required frontmatter keys."""
        if not _ACTUAL_KB_ROOT.exists():
            pytest.skip("KB root not found — run from workspace root")

        import sync_receptionist_kb as script

        failures: list[str] = []
        for md_path in _ACTUAL_KB_ROOT.rglob("*.md"):
            content = md_path.read_text(encoding="utf-8")
            fm, _ = script._parse_frontmatter(content)
            errors = script._validate_frontmatter(md_path, fm)
            if errors:
                failures.append(f"{md_path.name}: {'; '.join(errors)}")

        assert not failures, (
            "Frontmatter validation failures:\n" + "\n".join(failures)
        )

    def test_all_agents_covered_in_agent_scope(self) -> None:
        """All KB files must list all 3 receptionist agents in their agent_scope."""
        if not _ACTUAL_KB_ROOT.exists():
            pytest.skip("KB root not found — run from workspace root")

        import sync_receptionist_kb as script

        required_agents = {"tiffany", "sarah_receptionist", "sarah_frontdesk"}
        failures: list[str] = []

        for md_path in _ACTUAL_KB_ROOT.rglob("*.md"):
            content = md_path.read_text(encoding="utf-8")
            fm, _ = script._parse_frontmatter(content)
            scope = fm.get("agent_scope", [])
            if isinstance(scope, list):
                scope_set = {s.strip() for s in scope}
            else:
                scope_set = {scope.strip()}
            missing = required_agents - scope_set
            if missing:
                failures.append(f"{md_path.name}: missing agents {missing}")

        assert not failures, (
            "Agent scope coverage failures:\n" + "\n".join(failures)
        )


# ---------------------------------------------------------------------------
# Tests: Frontmatter parser unit tests
# ---------------------------------------------------------------------------


class TestFrontmatterParser:
    def test_parses_valid_frontmatter(self) -> None:
        import sync_receptionist_kb as script

        fm, body = script._parse_frontmatter(_VALID_FM)
        assert fm["title"] == "Test KB Doc"
        assert fm["priority"] == "high"
        assert isinstance(fm["agent_scope"], list)
        assert "tiffany" in fm["agent_scope"]
        assert "# Test content" in body

    def test_returns_empty_dict_when_no_frontmatter(self) -> None:
        import sync_receptionist_kb as script

        fm, body = script._parse_frontmatter(_INVALID_FM)
        assert fm == {}
        assert "# No frontmatter" in body

    def test_validate_frontmatter_reports_missing_keys(self) -> None:
        import sync_receptionist_kb as script

        fm = {"title": "OK"}  # Missing many required keys
        errors = script._validate_frontmatter(Path("test.md"), fm)
        assert len(errors) >= 1
        assert any("business_type" in e for e in errors)
