"""run_el_tests_canary.py — Daily canary CI job for EL native Tests.

Triggers the ElevenLabs test-run for Sarah-Receptionist (lowest call volume —
safest canary agent), polls until complete, and exits with a non-zero code
if any test fails.

Cron expression: 0 4 * * * UTC  (4:00 AM UTC daily, one hour after the daily
EL contract audit at 03:00 UTC).

On any failure:
    - Emits an el_test_canary_failed receipt (Law #2).
    - Sends a Sentry alert via SENTRY_DSN (if configured).
    - Exits 1.

On success:
    - Emits an el_test_canary_passed receipt.
    - Exits 0.

Usage:
    EL_API_KEY=sk_... python -m scripts.run_el_tests_canary
    EL_API_KEY=sk_... python -m scripts.run_el_tests_canary --dry-run

Flags:
    --dry-run   Skip the actual EL API call; emit a dry_run receipt and exit 0.
                Used for testing the script harness itself.

Exit codes:
    0   All canary tests passed (or dry-run).
    1   One or more canary tests failed, or EL API returned an error.

Aspire Laws:
    Law #2  — every canary run emits a receipt (pass or fail).
    Law #3  — EL API error treated as canary failure (fail closed).
    Law #9  — API key sourced from env, never logged.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_PATH = _REPO_ROOT / "src"
if str(_SRC_PATH) not in sys.path:
    sys.path.insert(0, str(_SRC_PATH))

try:
    from aspire_orchestrator.services.receipt_store import store_receipts
except ImportError:
    def store_receipts(receipts: list[dict[str, Any]]) -> None:  # type: ignore[misc]
        logging.getLogger(__name__).warning(
            "store_receipts not available — receipts: %s",
            json.dumps(receipts, default=str),
        )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sarah-Receptionist is the designated canary: lowest call volume.
_CANARY_AGENT_ID = "agent_6501kp71h69jfqysgd055hemqhrq"
_CANARY_AGENT_NAME = "Sarah-Receptionist"

_EL_API_BASE = "https://api.elevenlabs.io/v1"
_POLL_INTERVAL_SECS = 10
_POLL_TIMEOUT_SECS = 300  # 5 minutes max wait for test-run completion

# Test-run status terminal values.
_TERMINAL_STATUSES = {"completed", "failed", "error", "timed_out"}
_PASSING_STATUS = "completed"


# ---------------------------------------------------------------------------
# EL API helpers (thin HTTP wrappers; injectable for unit tests)
# ---------------------------------------------------------------------------

def _http_post(
    url: str,
    api_key: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any] | None]:
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


def _http_get(url: str, api_key: str) -> dict[str, Any] | None:
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


# ---------------------------------------------------------------------------
# Sentry alert helper
# ---------------------------------------------------------------------------

def _send_sentry_alert(message: str, extra: dict[str, Any]) -> None:
    """Send a Sentry alert if SENTRY_DSN is configured. Silently no-ops if not."""
    dsn = os.environ.get("SENTRY_DSN", "")
    if not dsn:
        log.debug("SENTRY_DSN not set — skipping Sentry alert.")
        return
    try:
        import sentry_sdk  # type: ignore[import]
        sentry_sdk.init(dsn=dsn)
        with sentry_sdk.push_scope() as scope:
            for key, value in extra.items():
                scope.set_extra(key, value)
            sentry_sdk.capture_message(message, level="error")
        log.info("Sentry alert sent: %s", message)
    except Exception as exc:
        log.warning("Failed to send Sentry alert: %s", exc)


# ---------------------------------------------------------------------------
# Receipt builder
# ---------------------------------------------------------------------------

def _build_receipt(
    *,
    agent_id: str,
    agent_name: str,
    outcome: str,
    run_id: str = "",
    failing_tests: list[str] | None = None,
    reason: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    now = time.time()
    receipt_type = (
        "el_test_canary_failed"
        if outcome in ("failed", "api_error", "timeout")
        else "el_test_canary_passed"
    )
    return {
        "receipt_id": str(uuid.uuid4()),
        "receipt_type": receipt_type,
        "actor": "run_el_tests_canary",
        "risk_tier": "GREEN",
        "outcome": outcome,
        "redacted_inputs": {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "dry_run": dry_run,
        },
        "redacted_outputs": {
            "run_id": run_id,
            "failing_tests": failing_tests or [],
            "reason": reason,
        },
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# Core canary run logic
# ---------------------------------------------------------------------------

def run_canary(
    api_key: str,
    dry_run: bool = False,
    agent_id: str = _CANARY_AGENT_ID,
    agent_name: str = _CANARY_AGENT_NAME,
    post_fn: Any = None,
    get_fn: Any = None,
    poll_interval: float = _POLL_INTERVAL_SECS,
    poll_timeout: float = _POLL_TIMEOUT_SECS,
) -> int:
    """Trigger EL canary test-run and poll to completion.

    Returns exit code: 0 (pass), 1 (fail/error).
    """
    _post = post_fn or _http_post
    _get = get_fn or _http_get

    if dry_run:
        log.info("DRY-RUN: would trigger canary test-run for %s (%s).", agent_name, agent_id)
        receipt = _build_receipt(
            agent_id=agent_id,
            agent_name=agent_name,
            outcome="dry_run",
            dry_run=True,
        )
        store_receipts([receipt])
        return 0

    # Step 1: Trigger the test-run.
    trigger_url = f"{_EL_API_BASE}/convai/agents/{agent_id}/runs"
    log.info("Triggering EL canary test-run for %s (%s)...", agent_name, agent_id)
    status_code, trigger_response = _post(trigger_url, api_key, {})

    if status_code not in (200, 201, 202) or not trigger_response:
        log.error(
            "Failed to trigger test-run for %s: HTTP %d", agent_name, status_code
        )
        receipt = _build_receipt(
            agent_id=agent_id,
            agent_name=agent_name,
            outcome="api_error",
            reason=f"Trigger failed with HTTP {status_code}",
        )
        store_receipts([receipt])
        _send_sentry_alert(
            f"EL canary trigger failed for {agent_name}",
            {"agent_id": agent_id, "http_status": status_code},
        )
        return 1

    run_id: str = trigger_response.get("run_id", trigger_response.get("id", ""))
    log.info("Test-run triggered: run_id=%s. Polling for completion...", run_id)

    # Step 2: Poll for completion.
    deadline = time.monotonic() + poll_timeout
    final_status: str = ""
    final_response: dict[str, Any] = {}

    while time.monotonic() < deadline:
        poll_url = f"{_EL_API_BASE}/convai/agents/{agent_id}/runs/{run_id}"
        poll_result = _get(poll_url, api_key)

        if not poll_result:
            log.warning("Poll returned no result for run_id=%s — retrying...", run_id)
            time.sleep(poll_interval)
            continue

        current_status: str = poll_result.get("status", "")
        log.info("Run %s status: %s", run_id, current_status)

        if current_status in _TERMINAL_STATUSES:
            final_status = current_status
            final_response = poll_result
            break

        time.sleep(poll_interval)
    else:
        # Timed out.
        log.error("Canary test-run %s timed out after %ss.", run_id, poll_timeout)
        receipt = _build_receipt(
            agent_id=agent_id,
            agent_name=agent_name,
            outcome="timeout",
            run_id=run_id,
            reason=f"Polling timed out after {poll_timeout}s",
        )
        store_receipts([receipt])
        _send_sentry_alert(
            f"EL canary timed out for {agent_name}",
            {"agent_id": agent_id, "run_id": run_id},
        )
        return 1

    # Step 3: Evaluate results.
    failing_tests: list[str] = []
    test_results = final_response.get("test_results", final_response.get("results", []))
    for result in test_results:
        if result.get("status") != "passed" and result.get("outcome") != "pass":
            test_name = result.get("name", result.get("test_name", "unknown"))
            failing_tests.append(test_name)

    if final_status != _PASSING_STATUS or failing_tests:
        failure_summary = (
            f"status={final_status}, failing_tests={failing_tests}"
        )
        log.error(
            "Canary FAILED for %s: %s", agent_name, failure_summary
        )
        receipt = _build_receipt(
            agent_id=agent_id,
            agent_name=agent_name,
            outcome="failed",
            run_id=run_id,
            failing_tests=failing_tests,
            reason=failure_summary,
        )
        store_receipts([receipt])
        _send_sentry_alert(
            f"EL canary tests FAILED for {agent_name}",
            {
                "agent_id": agent_id,
                "run_id": run_id,
                "failing_tests": failing_tests,
                "final_status": final_status,
            },
        )
        return 1

    log.info("Canary PASSED for %s: run_id=%s", agent_name, run_id)
    receipt = _build_receipt(
        agent_id=agent_id,
        agent_name=agent_name,
        outcome="passed",
        run_id=run_id,
    )
    store_receipts([receipt])
    return 0


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Daily EL native Tests canary run for Sarah-Receptionist."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Emit dry_run receipt and exit 0 without calling EL API.",
    )
    parser.add_argument(
        "--agent-id",
        default=_CANARY_AGENT_ID,
        help="Override canary agent ID (default: Sarah-Receptionist).",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("ASPIRE_ELEVENLABS_API_KEY") or os.environ.get("EL_API_KEY", "")
    if not api_key and not args.dry_run:
        log.error(
            "ASPIRE_ELEVENLABS_API_KEY is not set. "
            "Use: railway run python -m scripts.run_el_tests_canary"
        )
        return 1

    return run_canary(
        api_key=api_key,
        dry_run=args.dry_run,
        agent_id=args.agent_id,
    )


if __name__ == "__main__":
    sys.exit(main())
