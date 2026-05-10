"""Pass 6 — Daily EL agent runtime audit.

Pulls each of the 7 EL agents via EL API, runs ContractValidator against each,
writes one runtime_audit_drift receipt per agent, and exits 1 on any non-100%
compliance score.

Designed for cron invocation at 03:00 UTC daily:
  Cron expression: 0 3 * * *

Invoke via Railway scheduler or n8n HTTP node with:
  POST /run-script?name=daily_el_audit
Or directly:
  python -m aspire_orchestrator.scripts.daily_el_audit [--dry-run]

Flags:
  --dry-run   Runs all checks but does NOT write receipts to Supabase.
              Exits 0 regardless of compliance (for testing the pipeline).

Exit codes:
  0 — all 7 agents 100% compliant (or --dry-run)
  1 — EL API error (could not fetch one or more agents)
  2 — one or more agents have failing rules

Law #2: one runtime_audit_drift receipt per agent, every run.
Law #3: EL API failure = agent treated as non-compliant; alert emitted.
Law #9: API key redacted from all receipt fields; never logged at INFO.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path wiring
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_ORCHESTRATOR_DIR = _SCRIPT_DIR.parent
_SRC_DIR = _ORCHESTRATOR_DIR / "src"

if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# ---------------------------------------------------------------------------
# Known agent registry
# The 7 EL agents covered by this plan.
# Format: agent_id -> (display_name, agent_kind)
# ---------------------------------------------------------------------------

_AGENTS: dict[str, tuple[str, str]] = {
    "agent_4801kqtapvsre2gb0gyb1ng631qr": ("Tiffany", "receptionist"),
    "agent_6501kp71h69jfqysgd055hemqhrq": ("Sarah-Receptionist", "receptionist"),
    "agent_8901kmqdjnrte7psp6en4f85m4kt": ("Sarah-FrontDesk", "receptionist"),
    "agent_1201kmqdjgxvfxxteedpkvjej7er": ("Ava-EL", "assistant"),
    "agent_2201kmqdjjyben0tyg2t5eexnmzg": ("Finn-EL", "advisor"),
    "agent_4201kmqdjm1tfhfaggnnfjax3m6d": ("Eli", "receptionist"),
    "agent_1901kmqdjmwmfqg9rqr5jngfydnw": ("Nora", "receptionist"),
}

_EL_AGENTS_BASE_URL = "https://api.elevenlabs.io/v1/convai/agents"

# ---------------------------------------------------------------------------
# EL API client (lightweight, no SDK dependency)
# ---------------------------------------------------------------------------


def _fetch_agent(agent_id: str, api_key: str) -> dict[str, Any] | None:
    """Fetch agent config from EL API. Returns None on any error."""
    try:
        import urllib.request
        import urllib.error

        url = f"{_EL_AGENTS_BASE_URL}/{agent_id}"
        req = urllib.request.Request(
            url,
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))  # type: ignore[no-any-return]
    except Exception as exc:
        logger.error(
            "el_audit_fetch_error agent_id=%s error=%s",
            agent_id,
            type(exc).__name__,  # Don't log exc str — may contain API key
        )
        return None


def _normalise_config(raw: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Convert EL API response shape to flat ContractValidator shape."""
    # EL GET /v1/convai/agents/{id} returns a different shape from the workspace JSON.
    # We normalise to the same flat shape used by the pre-commit hook and tests.
    conv = raw.get("conversation_config", {})
    agent_block = conv.get("agent", {})
    prompt_block = agent_block.get("prompt", {})
    tts_block = conv.get("tts", {})

    flat: dict[str, Any] = {
        "agent_id": agent_id,
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


def _extract_prompt(raw: dict[str, Any]) -> str:
    """Extract system prompt string from EL API response."""
    conv = raw.get("conversation_config", {})
    agent_block = conv.get("agent", {})
    prompt_block = agent_block.get("prompt", {})
    return str(prompt_block.get("prompt", ""))


# ---------------------------------------------------------------------------
# Receipt builder
# ---------------------------------------------------------------------------


def _build_receipt(
    agent_id: str,
    display_name: str,
    score: str,
    failing_rules: list[str],
    overrides_applied: list[int],
    outcome: str,
    reason_code: str,
    dry_run: bool,
    fetch_error: bool = False,
) -> dict[str, Any]:
    """Build a runtime_audit_drift receipt dict (Law #2)."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": str(uuid.uuid4()),
        "receipt_type": "runtime_audit_drift",
        "action_type": "runtime_audit_drift",
        "risk_tier": "GREEN",
        "actor_type": "SYSTEM",
        "actor_id": "daily_el_audit",
        "outcome": outcome,
        "reason_code": reason_code,
        "redacted_inputs": {
            "agent_id": agent_id,
            "display_name": display_name,
            "dry_run": dry_run,
        },
        "redacted_outputs": {
            "score": score,
            "failing_rules": failing_rules,
            "overrides_applied": overrides_applied,
            "fetch_error": fetch_error,
        },
        "created_at": now,
    }


# ---------------------------------------------------------------------------
# Main audit loop
# ---------------------------------------------------------------------------


def run_audit(
    api_key: str,
    dry_run: bool = False,
    fetch_agent_fn: Any = None,  # injectable for testing
) -> int:
    """Run the audit loop. Returns exit code (0, 1, or 2)."""
    os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
    os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "audit-stub-key")

    # Suppress Supabase receipt emission during validator construction
    import unittest.mock as _mock
    with _mock.patch(
        "aspire_orchestrator.services.receipt_store.store_receipts",
        side_effect=lambda *a, **kw: None,
    ):
        from aspire_orchestrator.services.el_contract import ContractValidator
        validator = ContractValidator()

    # Import real receipt store for the actual audit receipts
    from aspire_orchestrator.services import receipt_store as _receipt_store

    if fetch_agent_fn is None:
        fetch_agent_fn = _fetch_agent

    exit_code = 0
    all_receipts: list[dict[str, Any]] = []
    drift_summary: list[str] = []

    for agent_id, (display_name, agent_kind) in _AGENTS.items():
        logger.info("el_audit_checking agent_id=%s name=%s", agent_id, display_name)

        raw = fetch_agent_fn(agent_id, api_key)

        if raw is None:
            # Fetch error — treat as non-compliant, emit error receipt
            receipt = _build_receipt(
                agent_id=agent_id,
                display_name=display_name,
                score="FETCH_ERROR",
                failing_rules=["FETCH_ERROR"],
                overrides_applied=[],
                outcome="error",
                reason_code="EL_API_FETCH_FAILED",
                dry_run=dry_run,
                fetch_error=True,
            )
            all_receipts.append(receipt)
            drift_summary.append(f"  {display_name} ({agent_id}): FETCH ERROR")
            if exit_code < 1:
                exit_code = 1
            continue

        prompt_text = _extract_prompt(raw)
        agent_config = _normalise_config(raw, agent_id)

        report = validator.validate(
            prompt_text=prompt_text,
            agent_config=agent_config,
            agent_kind=agent_kind,
        )

        failing_rule_keys = [str(r.id) + r.id_suffix for r in report.failing_rules]
        override_rule_ids = [r.rule for r in report.overrides_applied]

        if report.failing_rules:
            outcome = "drift_detected"
            reason_code = "CONTRACT_RULES_FAILED"
            if exit_code < 2:
                exit_code = 2
            drift_summary.append(
                f"  {display_name} ({agent_id}): {report.score} — "
                f"failing rules: {failing_rule_keys}"
            )
        else:
            outcome = "compliant"
            reason_code = "ALL_RULES_PASSED"

        receipt = _build_receipt(
            agent_id=agent_id,
            display_name=display_name,
            score=report.score,
            failing_rules=failing_rule_keys,
            overrides_applied=override_rule_ids,
            outcome=outcome,
            reason_code=reason_code,
            dry_run=dry_run,
        )
        all_receipts.append(receipt)

    # ── Print summary ─────────────────────────────────────────────────────────
    compliant_count = sum(1 for r in all_receipts if r["outcome"] == "compliant")
    print(
        f"[daily_el_audit] {compliant_count}/{len(_AGENTS)} agents compliant"
        + (" (dry-run)" if dry_run else "")
    )

    if drift_summary:
        print("[daily_el_audit] DRIFT DETECTED:")
        for line in drift_summary:
            print(line)

    # ── Emit receipts (Law #2) ────────────────────────────────────────────────
    if not dry_run:
        try:
            _receipt_store.store_receipts(all_receipts)
            logger.info("el_audit_receipts_stored count=%d", len(all_receipts))
        except Exception as exc:
            logger.error("el_audit_receipt_store_failed: %s", type(exc).__name__)
            # Receipt store failure does not change the exit code — the audit
            # results are already printed; the receipt failure is itself an
            # observability gap to be alerted separately.
    else:
        logger.info(
            "el_audit_dry_run: would have stored %d receipts", len(all_receipts)
        )
        # In dry-run mode, always exit 0
        return 0

    return exit_code


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description=(
            "Daily EL agent runtime audit (Pass 6). "
            "Cron: 0 3 * * * (03:00 UTC daily)."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check agents but do NOT write receipts; always exits 0.",
    )

    args = parser.parse_args(argv)

    api_key = os.environ.get("EL_API_KEY", "")
    if not api_key:
        print(
            "[ERROR] EL_API_KEY environment variable not set.",
            file=sys.stderr,
        )
        return 1

    return run_audit(api_key=api_key, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
