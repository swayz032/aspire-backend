"""Pass 7.5 — Tests for run_el_tests_canary.py.

Test plan (>= 4 tests as specified):
  1. test_all_pass_exits_zero_and_emits_passed_receipt
     All tests in the run return passed -> exit 0, canary_passed receipt emitted.

  2. test_single_fail_exits_one_and_emits_failed_receipt
     One test fails -> exit 1, el_test_canary_failed receipt with failing test name.

  3. test_multi_fail_lists_all_failing_tests_in_receipt
     Multiple test failures -> exit 1, all failing test names in receipt.

  4. test_el_api_error_on_trigger_exits_one
     EL API returns non-2xx on trigger -> exit 1, api_error receipt, Sentry alert.

  5. test_dry_run_exits_zero_and_emits_dry_run_receipt
     --dry-run -> exit 0, dry_run receipt emitted, no EL API call.

  6. test_poll_timeout_exits_one
     EL run never reaches terminal status within timeout -> exit 1, timeout receipt.

All tests are offline (no network, no Supabase). HTTP helpers are replaced
by injected mock functions.

Aspire Laws verified:
    Law #2  — receipt emitted every run (tests 1–6).
    Law #3  — API error treated as canary failure (test 4).
    Law #9  — API key sourced from env, never in receipts (spot-checked in test 1).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

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

import run_el_tests_canary as _canary  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CANARY_AGENT_ID = _canary._CANARY_AGENT_ID
_CANARY_AGENT_NAME = _canary._CANARY_AGENT_NAME
_FAST_POLL = 0.01  # Very short poll interval for unit tests.
_SHORT_TIMEOUT = 1.0  # Short timeout for timeout test.


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_trigger_response(run_id: str = "run_test_001") -> tuple[int, dict[str, Any]]:
    """Successful trigger response."""
    return 202, {"run_id": run_id, "status": "pending"}


def _make_poll_response(
    run_id: str,
    status: str,
    test_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": status,
        "test_results": test_results or [],
    }


def _make_passed_result(name: str = "t01_greeting") -> dict[str, Any]:
    return {"name": name, "status": "passed", "outcome": "pass"}


def _make_failed_result(name: str = "t01_greeting") -> dict[str, Any]:
    return {"name": name, "status": "failed", "outcome": "fail"}


# ---------------------------------------------------------------------------
# Autouse: suppress Supabase at import time
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _suppress_supabase_import() -> Any:
    """Suppress Supabase calls at import time. Tests patch run_el_tests_canary.store_receipts directly."""
    with patch("aspire_orchestrator.services.receipt_store.store_receipts"):
        yield


# ---------------------------------------------------------------------------
# Test 1 — All tests pass
# ---------------------------------------------------------------------------


class TestAllPass:
    def test_all_pass_exits_zero_and_emits_passed_receipt(self) -> None:
        """All EL tests pass -> exit 0, el_test_canary_passed receipt with correct fields."""
        run_id = "run_all_pass_001"
        poll_responses = [
            _make_poll_response(
                run_id, "completed",
                [_make_passed_result("t01"), _make_passed_result("t02")]
            )
        ]
        poll_iter = iter(poll_responses)

        def _mock_post(url: str, api_key: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return _make_trigger_response(run_id)

        def _mock_get(url: str, api_key: str) -> dict[str, Any]:
            return next(poll_iter, poll_responses[-1])

        captured_receipts: list[dict[str, Any]] = []

        def _mock_store(receipts: list[dict[str, Any]]) -> None:
            captured_receipts.extend(receipts)

        with patch.object(_canary, "store_receipts", side_effect=_mock_store), \
             patch.object(_canary, "_send_sentry_alert"):
            exit_code = _canary.run_canary(
                api_key="sk_test_fake",
                dry_run=False,
                agent_id=_CANARY_AGENT_ID,
                agent_name=_CANARY_AGENT_NAME,
                post_fn=_mock_post,
                get_fn=_mock_get,
                poll_interval=_FAST_POLL,
                poll_timeout=10.0,
            )

        assert exit_code == 0, f"Expected exit 0 for all-pass, got {exit_code}"

        # Receipt must exist and be of type canary_passed.
        assert len(captured_receipts) == 1
        r = captured_receipts[0]
        assert r["receipt_type"] == "el_test_canary_passed"
        assert r["outcome"] == "passed"
        assert r["redacted_outputs"]["run_id"] == run_id
        assert r["redacted_outputs"]["failing_tests"] == []

        # Law #9: API key must NOT appear in receipt.
        assert "sk_test_fake" not in str(r)


# ---------------------------------------------------------------------------
# Test 2 — Single test failure
# ---------------------------------------------------------------------------


class TestSingleFail:
    def test_single_fail_exits_one_and_emits_failed_receipt(self) -> None:
        """One failing EL test -> exit 1, el_test_canary_failed receipt with test name."""
        run_id = "run_single_fail_001"
        failing_test_name = "t03_greeting_with_blank_business_name"

        poll_response = _make_poll_response(
            run_id, "completed",
            [
                _make_passed_result("t01"),
                _make_failed_result(failing_test_name),
                _make_passed_result("t02"),
            ]
        )

        def _mock_post(url: str, api_key: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return _make_trigger_response(run_id)

        def _mock_get(url: str, api_key: str) -> dict[str, Any]:
            return poll_response

        captured_receipts: list[dict[str, Any]] = []

        def _mock_store(receipts: list[dict[str, Any]]) -> None:
            captured_receipts.extend(receipts)

        with patch.object(_canary, "store_receipts", side_effect=_mock_store), \
             patch.object(_canary, "_send_sentry_alert"):
            exit_code = _canary.run_canary(
                api_key="sk_test_fake",
                dry_run=False,
                agent_id=_CANARY_AGENT_ID,
                agent_name=_CANARY_AGENT_NAME,
                post_fn=_mock_post,
                get_fn=_mock_get,
                poll_interval=_FAST_POLL,
                poll_timeout=10.0,
            )

        assert exit_code == 1, f"Expected exit 1 for single fail, got {exit_code}"

        r = captured_receipts[0]
        assert r["receipt_type"] == "el_test_canary_failed"
        assert r["outcome"] == "failed"
        assert failing_test_name in r["redacted_outputs"]["failing_tests"], (
            f"Failing test name not in receipt: {r['redacted_outputs']['failing_tests']}"
        )


# ---------------------------------------------------------------------------
# Test 3 — Multiple test failures
# ---------------------------------------------------------------------------


class TestMultiFail:
    def test_multi_fail_lists_all_failing_tests_in_receipt(self) -> None:
        """Multiple failures -> all failing test names in receipt."""
        run_id = "run_multi_fail_001"
        failing_names = [
            "t02_greeting_new_caller_open_hours",
            "t03_greeting_with_blank_business_name",
            "t10_ai_disclosure_only_when_asked",
        ]

        results = [_make_passed_result("t01")]
        results += [_make_failed_result(n) for n in failing_names]
        results.append(_make_passed_result("t04"))

        def _mock_post(url: str, api_key: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return _make_trigger_response(run_id)

        def _mock_get(url: str, api_key: str) -> dict[str, Any]:
            return _make_poll_response(run_id, "completed", results)

        captured_receipts: list[dict[str, Any]] = []

        def _mock_store(receipts: list[dict[str, Any]]) -> None:
            captured_receipts.extend(receipts)

        with patch.object(_canary, "store_receipts", side_effect=_mock_store), \
             patch.object(_canary, "_send_sentry_alert"):
            exit_code = _canary.run_canary(
                api_key="sk_test_fake",
                dry_run=False,
                agent_id=_CANARY_AGENT_ID,
                agent_name=_CANARY_AGENT_NAME,
                post_fn=_mock_post,
                get_fn=_mock_get,
                poll_interval=_FAST_POLL,
                poll_timeout=10.0,
            )

        assert exit_code == 1, f"Expected exit 1 for multi-fail, got {exit_code}"

        r = captured_receipts[0]
        assert r["receipt_type"] == "el_test_canary_failed"
        actual_failing = set(r["redacted_outputs"]["failing_tests"])
        expected_failing = set(failing_names)
        assert actual_failing == expected_failing, (
            f"Receipt failing_tests mismatch. Expected {expected_failing}, got {actual_failing}"
        )


# ---------------------------------------------------------------------------
# Test 4 — EL API error on trigger
# ---------------------------------------------------------------------------


class TestTriggerApiError:
    def test_el_api_error_on_trigger_exits_one(self) -> None:
        """EL API returns non-2xx on trigger -> exit 1, api_error receipt, Sentry called."""
        def _mock_post(url: str, api_key: str, payload: dict[str, Any]) -> tuple[int, None]:
            return 503, None  # EL service unavailable.

        captured_receipts: list[dict[str, Any]] = []
        sentry_alerts: list[tuple[str, dict[str, Any]]] = []

        def _mock_store(receipts: list[dict[str, Any]]) -> None:
            captured_receipts.extend(receipts)

        def _mock_sentry(message: str, extra: dict[str, Any]) -> None:
            sentry_alerts.append((message, extra))

        with patch.object(_canary, "store_receipts", side_effect=_mock_store), \
             patch.object(_canary, "_send_sentry_alert", side_effect=_mock_sentry):
            exit_code = _canary.run_canary(
                api_key="sk_test_fake",
                dry_run=False,
                agent_id=_CANARY_AGENT_ID,
                agent_name=_CANARY_AGENT_NAME,
                post_fn=_mock_post,
                get_fn=MagicMock(),
                poll_interval=_FAST_POLL,
                poll_timeout=10.0,
            )

        assert exit_code == 1, f"Expected exit 1 for trigger API error, got {exit_code}"

        r = captured_receipts[0]
        assert r["receipt_type"] == "el_test_canary_failed"
        assert r["outcome"] == "api_error"

        # Sentry must have been called.
        assert len(sentry_alerts) == 1, (
            f"Expected 1 Sentry alert, got {len(sentry_alerts)}"
        )
        sentry_msg, sentry_extra = sentry_alerts[0]
        assert _CANARY_AGENT_NAME in sentry_msg or "canary" in sentry_msg.lower()


# ---------------------------------------------------------------------------
# Test 5 — Dry-run exits 0 and emits dry_run receipt
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_exits_zero_and_emits_dry_run_receipt(self) -> None:
        """--dry-run must exit 0 and emit a dry_run receipt without calling EL API."""
        mock_post = MagicMock()
        mock_get = MagicMock()
        captured_receipts: list[dict[str, Any]] = []

        def _mock_store(receipts: list[dict[str, Any]]) -> None:
            captured_receipts.extend(receipts)

        with patch.object(_canary, "store_receipts", side_effect=_mock_store):
            exit_code = _canary.run_canary(
                api_key="sk_test_fake",
                dry_run=True,
                agent_id=_CANARY_AGENT_ID,
                agent_name=_CANARY_AGENT_NAME,
                post_fn=mock_post,
                get_fn=mock_get,
                poll_interval=_FAST_POLL,
                poll_timeout=10.0,
            )

        assert exit_code == 0, f"Dry-run must exit 0, got {exit_code}"
        mock_post.assert_not_called()
        mock_get.assert_not_called()

        assert len(captured_receipts) == 1
        r = captured_receipts[0]
        assert r["outcome"] == "dry_run"
        assert r["redacted_inputs"]["dry_run"] is True


# ---------------------------------------------------------------------------
# Test 6 — Poll timeout
# ---------------------------------------------------------------------------


class TestPollTimeout:
    def test_poll_timeout_exits_one(self) -> None:
        """EL run never completes within timeout -> exit 1, timeout receipt."""
        run_id = "run_timeout_001"

        def _mock_post(url: str, api_key: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return _make_trigger_response(run_id)

        def _mock_get(url: str, api_key: str) -> dict[str, Any]:
            # Always return "running" — never completes.
            return {"run_id": run_id, "status": "running", "test_results": []}

        captured_receipts: list[dict[str, Any]] = []

        def _mock_store(receipts: list[dict[str, Any]]) -> None:
            captured_receipts.extend(receipts)

        with patch.object(_canary, "store_receipts", side_effect=_mock_store), \
             patch.object(_canary, "_send_sentry_alert"):
            exit_code = _canary.run_canary(
                api_key="sk_test_fake",
                dry_run=False,
                agent_id=_CANARY_AGENT_ID,
                agent_name=_CANARY_AGENT_NAME,
                post_fn=_mock_post,
                get_fn=_mock_get,
                poll_interval=_FAST_POLL,
                poll_timeout=_SHORT_TIMEOUT,  # 1 second — will timeout quickly.
            )

        assert exit_code == 1, f"Expected exit 1 for timeout, got {exit_code}"

        r = captured_receipts[0]
        assert r["receipt_type"] == "el_test_canary_failed"
        assert r["outcome"] == "timeout", f"Expected 'timeout' outcome, got {r['outcome']}"
