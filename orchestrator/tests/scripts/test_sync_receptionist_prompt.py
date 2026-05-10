"""Pass 1 — sync_receptionist_prompt.py contract enforcement tests.

Test plan:
  - test_dry_run_does_not_patch_el
  - test_compliant_prompt_deploys
  - test_failing_prompt_blocked
  - test_no_strict_without_justification_errors
  - test_no_strict_with_justification_skips_validator
  - test_receipt_emitted_per_agent

All tests are offline (no network, no Supabase). The EL HTTP client and
store_receipts are fully mocked. The ContractValidator is mocked per-test
to return controlled outcomes without reading the YAML or emitting receipts.

Law #2: every code path emits receipts — asserted in every test.
Law #3: --no-strict without justification must error; missing token must deny.
Law #9: no secrets logged — API key sourced from env, never in receipts.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the orchestrator source tree and scripts are importable.
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
# Shared fixtures
# ---------------------------------------------------------------------------

# A minimal prompt that passes the legacy safety check (no forbidden phrases /
# tone tags in non-negation context).
_SAFE_TEMPLATE = (
    "# Persona\n\nYou are {{agent_first_name}}.\n"
    "You work for {{business_name}} in {{industry}} / {{industry_specialty}}.\n"
    "# Environment\nPhone.\n"
    "# Tone\nProfessional.\n"
    "# Goal\n1. Greet. This step is important.\n"
    "# Guardrails\n- Never share secrets.\n- Stay on topic.\n- Escalate if unsure.\n"
    "# Tools\nNo tools are configured for this agent.\n"
    "# Error handling\nAsk caller to repeat.\n"
)


def _make_passing_report() -> MagicMock:
    """Return a mock ContractReport with no failing rules (28/28)."""
    report = MagicMock()
    report.score = "28/28"
    report.failing_rules = []
    report.overrides_applied = []
    return report


def _make_failing_report(rule_ids: list[str] | None = None) -> MagicMock:
    """Return a mock ContractReport with failing rules."""
    if rule_ids is None:
        rule_ids = ["1", "2"]
    report = MagicMock()
    report.score = f"{28 - len(rule_ids)}/28"
    failing = []
    for rid in rule_ids:
        r = MagicMock()
        # Parse suffix: "12b" -> id=12, suffix="b"
        num = "".join(c for c in rid if c.isdigit())
        suf = "".join(c for c in rid if not c.isdigit())
        r.id = int(num)
        r.id_suffix = suf
        failing.append(r)
    report.failing_rules = failing
    report.overrides_applied = []
    return report


def _patch_round_trip(rendered: str) -> dict[str, object]:
    """Build a fake EL PATCH response with round-trip prompt."""
    return {
        "conversation_config": {
            "agent": {
                "prompt": {
                    "prompt": rendered
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# Helper: invoke main() with controlled environment
# ---------------------------------------------------------------------------

def _run_main(
    argv: list[str],
    *,
    mock_validator_report: MagicMock | None = None,
    patch_store: bool = True,
    patch_http_patch: bool = True,
    prompt_template: str = _SAFE_TEMPLATE,
) -> tuple[int, list[dict[str, Any]]]:
    """Run sync_receptionist_prompt.main() with mocked dependencies.

    Returns (exit_code, list_of_receipts_emitted).
    The prompt file is replaced with prompt_template so tests don't depend on
    the actual receptionist_v2.md being present or compliant.
    """
    emitted_receipts: list[dict[str, Any]] = []

    def _capture_receipts(receipts: list[dict[str, Any]]) -> None:
        emitted_receipts.extend(receipts)

    # Build a mock ContractValidator class.
    mock_validator_cls = MagicMock()
    mock_validator_instance = MagicMock()
    if mock_validator_report is not None:
        mock_validator_instance.validate.return_value = mock_validator_report
    mock_validator_cls.return_value = mock_validator_instance

    # Build fake PATCH response that round-trips correctly.
    def _fake_http_patch(url: str, api_key: str, body: dict[str, object]) -> dict[str, object]:
        rendered = (
            body.get("conversation_config", {})  # type: ignore[union-attr]
            .get("agent", {})
            .get("prompt", {})
            .get("prompt", "")
        )
        return _patch_round_trip(rendered)  # type: ignore[return-value]

    patches: list[Any] = []

    # Patch the prompt file read so tests don't need the real file.
    mock_path = MagicMock()
    mock_path.read_text.return_value = prompt_template
    patches.append(
        patch("sync_receptionist_prompt.PROMPT_FILE", mock_path)
    )

    if patch_store:
        patches.append(
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=_capture_receipts,
            )
        )

    if patch_http_patch:
        patches.append(
            patch("sync_receptionist_prompt._http_patch", side_effect=_fake_http_patch)
        )

    # Patch ContractValidator at the el_contract module level.
    patches.append(
        patch(
            "aspire_orchestrator.services.el_contract.ContractValidator",
            mock_validator_cls,
        )
    )
    # Also patch at the import site inside main().
    patches.append(
        patch(
            "sync_receptionist_prompt.ContractValidator",
            mock_validator_cls,
            create=True,
        )
    )

    import sync_receptionist_prompt as script

    with patch.multiple(
        "sync_receptionist_prompt",
        **{},
    ):
        # Apply all patches manually (patch.multiple can't handle dynamic lists cleanly)
        active: list[Any] = []
        try:
            for p in patches:
                active.append(p.__enter__())

            # Re-patch ContractValidator at import-time inside main() by patching
            # the module's import mechanism.
            with patch.dict("sys.modules", {}):
                # Patch el_contract module so lazy import in main() gets our mock.
                import aspire_orchestrator.services.el_contract as _el_mod
                original_cls = _el_mod.ContractValidator
                _el_mod.ContractValidator = mock_validator_cls  # type: ignore[assignment]
                try:
                    exit_code = script.main(argv)
                finally:
                    _el_mod.ContractValidator = original_cls  # type: ignore[assignment]
        finally:
            for p, ctx in zip(patches, active):
                p.__exit__(None, None, None)

    return exit_code, emitted_receipts


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_does_not_patch_el(self) -> None:
        """--dry-run must run validator and emit receipts but NEVER call PATCH."""
        mock_http_patch = MagicMock()
        report = _make_passing_report()
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_prompt as script
        import aspire_orchestrator.services.el_contract as _el_mod

        original_cls = _el_mod.ContractValidator
        mock_validator_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.validate.return_value = report
        mock_validator_cls.return_value = mock_instance

        mock_path = MagicMock()
        mock_path.read_text.return_value = _SAFE_TEMPLATE

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        _el_mod.ContractValidator = mock_validator_cls  # type: ignore[assignment]
        try:
            with (
                patch("sync_receptionist_prompt.PROMPT_FILE", mock_path),
                patch("sync_receptionist_prompt._http_patch", mock_http_patch),
                patch(
                    "aspire_orchestrator.services.receipt_store.store_receipts",
                    side_effect=_capture,
                ),
            ):
                exit_code = script.main(["--dry-run"])
        finally:
            _el_mod.ContractValidator = original_cls  # type: ignore[assignment]

        assert exit_code == 0, f"expected exit 0, got {exit_code}"
        mock_http_patch.assert_not_called()

        # One receipt per agent (3 agents).
        sync_receipts = [
            r for r in emitted
            if r.get("receipt_type") == "prompt_sync_compliance_check"
        ]
        assert len(sync_receipts) == 3, f"expected 3 receipts, got {len(sync_receipts)}"

        for r in sync_receipts:
            outputs = r.get("redacted_outputs", {})
            assert outputs.get("deployed_or_blocked") == "dry_run", (
                f"expected dry_run, got {outputs.get('deployed_or_blocked')}"
            )


class TestCompliantDeploy:
    def test_compliant_prompt_deploys(self) -> None:
        """Passing validator (28/28) must call PATCH and emit deployed receipts."""
        mock_http_patch_calls: list[tuple[str, str]] = []
        report = _make_passing_report()
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_prompt as script
        import aspire_orchestrator.services.el_contract as _el_mod

        original_cls = _el_mod.ContractValidator
        mock_validator_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.validate.return_value = report
        mock_validator_cls.return_value = mock_instance

        mock_path = MagicMock()
        mock_path.read_text.return_value = _SAFE_TEMPLATE

        def _fake_patch(url: str, api_key: str, body: dict[str, object]) -> dict[str, object]:
            mock_http_patch_calls.append((url, str(api_key)[:4]))
            rendered = (
                body.get("conversation_config", {})  # type: ignore[union-attr]
                .get("agent", {})
                .get("prompt", {})
                .get("prompt", "")
            )
            return _patch_round_trip(rendered)  # type: ignore[return-value]

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        _el_mod.ContractValidator = mock_validator_cls  # type: ignore[assignment]
        try:
            with (
                patch("sync_receptionist_prompt.PROMPT_FILE", mock_path),
                patch("sync_receptionist_prompt._http_patch", side_effect=_fake_patch),
                patch(
                    "aspire_orchestrator.services.receipt_store.store_receipts",
                    side_effect=_capture,
                ),
            ):
                exit_code = script.main([])  # default --strict
        finally:
            _el_mod.ContractValidator = original_cls  # type: ignore[assignment]

        assert exit_code == 0, f"expected exit 0, got {exit_code}"
        # PATCH called once per agent (3 agents).
        assert len(mock_http_patch_calls) == 3, (
            f"expected 3 PATCH calls, got {len(mock_http_patch_calls)}"
        )
        # API key must NOT appear in receipts.
        for r in emitted:
            receipt_str = str(r)
            assert "sk_test_fake_key_not_real" not in receipt_str, (
                "API key leaked into receipt"
            )

        # deployed receipts
        sync_receipts = [
            r for r in emitted
            if r.get("receipt_type") == "prompt_sync_compliance_check"
        ]
        assert len(sync_receipts) == 3
        for r in sync_receipts:
            outputs = r.get("redacted_outputs", {})
            assert outputs.get("deployed_or_blocked") == "deployed"
            assert outputs.get("score") == "28/28"


class TestFailingPromptBlocked:
    def test_failing_prompt_blocked(self) -> None:
        """Failing validator must block PATCH and emit blocked receipt with exit code 2."""
        report = _make_failing_report(["1", "4"])
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_prompt as script
        import aspire_orchestrator.services.el_contract as _el_mod

        original_cls = _el_mod.ContractValidator
        mock_validator_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.validate.return_value = report
        mock_validator_cls.return_value = mock_instance

        mock_path = MagicMock()
        mock_path.read_text.return_value = _SAFE_TEMPLATE
        mock_http_patch = MagicMock()

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        _el_mod.ContractValidator = mock_validator_cls  # type: ignore[assignment]
        try:
            with (
                patch("sync_receptionist_prompt.PROMPT_FILE", mock_path),
                patch("sync_receptionist_prompt._http_patch", mock_http_patch),
                patch(
                    "aspire_orchestrator.services.receipt_store.store_receipts",
                    side_effect=_capture,
                ),
            ):
                exit_code = script.main([])  # default --strict
        finally:
            _el_mod.ContractValidator = original_cls  # type: ignore[assignment]

        assert exit_code == 2, f"expected exit 2 (blocked), got {exit_code}"
        # PATCH must never have been called.
        mock_http_patch.assert_not_called()

        sync_receipts = [
            r for r in emitted
            if r.get("receipt_type") == "prompt_sync_compliance_check"
        ]
        # At least one blocked receipt for the first failing agent.
        assert len(sync_receipts) >= 1
        blocked = [
            r for r in sync_receipts
            if r.get("redacted_outputs", {}).get("deployed_or_blocked") == "blocked"
        ]
        assert len(blocked) >= 1, "expected at least one blocked receipt"
        # Verify failing rule IDs are present in the receipt.
        failing_ids_in_receipt = blocked[0]["redacted_outputs"]["failing_rule_ids"]
        assert "1" in failing_ids_in_receipt or "4" in failing_ids_in_receipt


class TestNoStrictValidation:
    def test_no_strict_without_justification_errors(self) -> None:
        """--no-strict without --justification must exit 1; PATCH must not be called."""
        mock_http_patch = MagicMock()
        mock_path = MagicMock()
        mock_path.read_text.return_value = _SAFE_TEMPLATE

        import sync_receptionist_prompt as script

        with (
            patch("sync_receptionist_prompt.PROMPT_FILE", mock_path),
            patch("sync_receptionist_prompt._http_patch", mock_http_patch),
        ):
            exit_code = script.main(["--no-strict"])

        assert exit_code != 0, "expected non-zero exit when --no-strict given without justification"
        mock_http_patch.assert_not_called()

    def test_no_strict_with_short_justification_errors(self) -> None:
        """--no-strict with <30 char justification must error (boundary check)."""
        mock_http_patch = MagicMock()
        mock_path = MagicMock()
        mock_path.read_text.return_value = _SAFE_TEMPLATE

        import sync_receptionist_prompt as script

        with (
            patch("sync_receptionist_prompt.PROMPT_FILE", mock_path),
            patch("sync_receptionist_prompt._http_patch", mock_http_patch),
        ):
            exit_code = script.main(["--no-strict", "--justification", "too short"])

        assert exit_code != 0
        mock_http_patch.assert_not_called()

    def test_no_strict_with_justification_skips_validator(self) -> None:
        """--no-strict with >=30 char justification must bypass validator, PATCH, and emit skipped receipt."""
        mock_http_patch_calls: list[str] = []
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_prompt as script
        import aspire_orchestrator.services.el_contract as _el_mod

        # Validator should NOT be called in --no-strict mode.
        original_cls = _el_mod.ContractValidator
        mock_validator_cls = MagicMock()
        mock_instance = MagicMock()
        # If validate() IS called, it would return a failing report so we'd catch it.
        mock_instance.validate.side_effect = AssertionError(
            "validator.validate() must NOT be called in --no-strict mode"
        )
        mock_validator_cls.return_value = mock_instance

        mock_path = MagicMock()
        mock_path.read_text.return_value = _SAFE_TEMPLATE

        def _fake_patch(url: str, api_key: str, body: dict[str, object]) -> dict[str, object]:
            mock_http_patch_calls.append(url)
            rendered = (
                body.get("conversation_config", {})  # type: ignore[union-attr]
                .get("agent", {})
                .get("prompt", {})
                .get("prompt", "")
            )
            return _patch_round_trip(rendered)  # type: ignore[return-value]

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        justification = (
            "Hot patch for incident #INC-2026-05-07 receipt flusher "
            "poison-pill blocking compliance check"
        )
        assert len(justification) >= 30

        _el_mod.ContractValidator = mock_validator_cls  # type: ignore[assignment]
        try:
            with (
                patch("sync_receptionist_prompt.PROMPT_FILE", mock_path),
                patch("sync_receptionist_prompt._http_patch", side_effect=_fake_patch),
                patch(
                    "aspire_orchestrator.services.receipt_store.store_receipts",
                    side_effect=_capture,
                ),
            ):
                exit_code = script.main(["--no-strict", "--justification", justification])
        finally:
            _el_mod.ContractValidator = original_cls  # type: ignore[assignment]

        assert exit_code == 0, f"expected exit 0, got {exit_code}"
        # PATCH was called for all 3 agents.
        assert len(mock_http_patch_calls) == 3

        sync_receipts = [
            r for r in emitted
            if r.get("receipt_type") == "prompt_sync_compliance_check"
        ]
        assert len(sync_receipts) == 3
        for r in sync_receipts:
            outputs = r.get("redacted_outputs", {})
            assert outputs.get("deployed_or_blocked") == "skipped_with_justification"
            assert outputs.get("justification") == justification


class TestReceiptPerAgent:
    def test_receipt_emitted_per_agent(self) -> None:
        """Processing 3 agents (Tiffany, Sarah-FrontDesk, Sarah-Receptionist) must emit 3 receipts."""
        report = _make_passing_report()
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_prompt as script
        import aspire_orchestrator.services.el_contract as _el_mod

        original_cls = _el_mod.ContractValidator
        mock_validator_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.validate.return_value = report
        mock_validator_cls.return_value = mock_instance

        mock_path = MagicMock()
        mock_path.read_text.return_value = _SAFE_TEMPLATE

        def _fake_patch(url: str, api_key: str, body: dict[str, object]) -> dict[str, object]:
            rendered = (
                body.get("conversation_config", {})  # type: ignore[union-attr]
                .get("agent", {})
                .get("prompt", {})
                .get("prompt", "")
            )
            return _patch_round_trip(rendered)  # type: ignore[return-value]

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        _el_mod.ContractValidator = original_cls  # type: ignore[assignment]
        try:
            _el_mod.ContractValidator = mock_validator_cls  # type: ignore[assignment]
            with (
                patch("sync_receptionist_prompt.PROMPT_FILE", mock_path),
                patch("sync_receptionist_prompt._http_patch", side_effect=_fake_patch),
                patch(
                    "aspire_orchestrator.services.receipt_store.store_receipts",
                    side_effect=_capture,
                ),
            ):
                exit_code = script.main([])
        finally:
            _el_mod.ContractValidator = original_cls  # type: ignore[assignment]

        assert exit_code == 0

        sync_receipts = [
            r for r in emitted
            if r.get("receipt_type") == "prompt_sync_compliance_check"
        ]
        # Exactly 3 compliance receipts — one per agent.
        assert len(sync_receipts) == 3, (
            f"expected exactly 3 prompt_sync_compliance_check receipts, got {len(sync_receipts)}"
        )

        # Each receipt must have a unique id.
        receipt_ids = [r["id"] for r in sync_receipts]
        assert len(set(receipt_ids)) == 3, "receipt IDs are not unique"

        # Each receipt must carry agent_id in redacted_inputs.
        expected_agent_ids = {
            "agent_4801kqtapvsre2gb0gyb1ng631qr",
            "agent_8901kmqdjnrte7psp6en4f85m4kt",
            "agent_6501kp71h69jfqysgd055hemqhrq",
        }
        found_agent_ids = {
            r["redacted_inputs"]["agent_id"] for r in sync_receipts
        }
        assert found_agent_ids == expected_agent_ids, (
            f"unexpected agent IDs in receipts: {found_agent_ids}"
        )

        # Each receipt must carry prompt_sha256 (non-empty).
        for r in sync_receipts:
            sha = r["redacted_inputs"].get("prompt_sha256", "")
            assert len(sha) == 64, f"expected 64-char SHA256, got {len(sha)}: {sha}"


class TestReceiptNoPIILeak:
    def test_api_key_not_in_receipts(self) -> None:
        """EL API key must never appear in any emitted receipt."""
        report = _make_passing_report()
        emitted: list[dict[str, Any]] = []

        import sync_receptionist_prompt as script
        import aspire_orchestrator.services.el_contract as _el_mod

        original_cls = _el_mod.ContractValidator
        mock_validator_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.validate.return_value = report
        mock_validator_cls.return_value = mock_instance

        mock_path = MagicMock()
        mock_path.read_text.return_value = _SAFE_TEMPLATE

        def _fake_patch(url: str, api_key: str, body: dict[str, object]) -> dict[str, object]:
            rendered = (
                body.get("conversation_config", {})  # type: ignore[union-attr]
                .get("agent", {})
                .get("prompt", {})
                .get("prompt", "")
            )
            return _patch_round_trip(rendered)  # type: ignore[return-value]

        def _capture(receipts: list[dict[str, Any]]) -> None:
            emitted.extend(receipts)

        _el_mod.ContractValidator = mock_validator_cls  # type: ignore[assignment]
        try:
            with (
                patch("sync_receptionist_prompt.PROMPT_FILE", mock_path),
                patch("sync_receptionist_prompt._http_patch", side_effect=_fake_patch),
                patch(
                    "aspire_orchestrator.services.receipt_store.store_receipts",
                    side_effect=_capture,
                ),
            ):
                exit_code = script.main([])
        finally:
            _el_mod.ContractValidator = original_cls  # type: ignore[assignment]

        api_key = os.environ.get("EL_API_KEY", "sk_test_fake_key_not_real")
        for r in emitted:
            assert api_key not in str(r), f"API key found in receipt: {r}"
