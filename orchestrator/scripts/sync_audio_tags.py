"""
sync_audio_tags.py — Push the 16-tag audio matrix to all 3 receptionist EL agents.

Reads `config/audio_tags/receptionist_audio_tags_v1.yaml` (the canonical tag matrix).
For each agent_id in `applies_to_agent_ids`:
  1. Fetches the current agent config via EL GET /v1/convai/agents/{agent_id}.
  2. Computes diff against the YAML tags.
  3. Only PATCHes if the diff is non-empty (idempotent).
  4. Emits an `audio_tags_synced` receipt per agent (Law #2).
  5. Tiffany is canary — synced first. On any failure, stops immediately.

Usage:
    EL_API_KEY=sk_... python scripts/sync_audio_tags.py
    EL_API_KEY=sk_... python scripts/sync_audio_tags.py --dry-run

Aspire Laws:
    Law #2  — receipt per agent (success, skipped, or failed).
    Law #3  — fail closed on missing key or contract violation.
    Law #7  — no autonomous decisions; returns result, does not retry.
    Law #9  — API key sourced from env, never logged.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path bootstrap — ensure aspire_orchestrator is importable when run directly
# ---------------------------------------------------------------------------

_SRC_PATH = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_PATH) not in sys.path:
    sys.path.insert(0, str(_SRC_PATH))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TAGS_FILE = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "aspire_orchestrator"
    / "config"
    / "audio_tags"
    / "receptionist_audio_tags_v1.yaml"
)

_API_BASE = "https://api.elevenlabs.io/v1/convai/agents"

# Contract rule 12b: every tag description must be at least this many chars.
_MIN_DESCRIPTION_CHARS = 20


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioTag:
    tag: str
    description: str


@dataclass
class TagDiff:
    tags_added: list[str]
    tags_removed: list[str]
    tags_modified: list[str]

    @property
    def is_empty(self) -> bool:
        return not self.tags_added and not self.tags_removed and not self.tags_modified


# ---------------------------------------------------------------------------
# YAML + contract validation
# ---------------------------------------------------------------------------


def _load_tags(path: Path) -> tuple[list[str], list[AudioTag]]:
    """Load and validate the audio tags YAML.

    Returns (agent_ids, tags).

    Raises SystemExit(1) on any contract violation (Law #3: fail closed).
    """
    with path.open("r", encoding="utf-8") as fh:
        data: dict[str, object] = yaml.safe_load(fh)

    agent_ids: list[str] = list(data.get("applies_to_agent_ids", []))  # type: ignore[arg-type]
    raw_tags: list[dict[str, str]] = list(data.get("tags", []))  # type: ignore[arg-type]

    if not agent_ids:
        print("ERROR: applies_to_agent_ids is empty in tags YAML.", file=sys.stderr)
        sys.exit(1)

    if not raw_tags:
        print("ERROR: tags list is empty in tags YAML.", file=sys.stderr)
        sys.exit(1)

    # Contract rule 12b: every tag must have a description >= 20 chars.
    violations: list[str] = []
    tags: list[AudioTag] = []
    for entry in raw_tags:
        tag_name = str(entry.get("tag", "")).strip()
        description = str(entry.get("description", "")).strip()

        if not tag_name:
            violations.append("tag entry missing 'tag' field")
            continue

        if len(description) < _MIN_DESCRIPTION_CHARS:
            violations.append(
                f"tag '{tag_name}' description is {len(description)} chars "
                f"(minimum {_MIN_DESCRIPTION_CHARS}): '{description}'"
            )
            continue

        tags.append(AudioTag(tag=tag_name, description=description))

    if violations:
        print("CONTRACT VIOLATION (rule 12b) — refusing to proceed:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        sys.exit(1)

    print(f"Tags YAML loaded: {len(tags)} tags across {len(agent_ids)} agents.")
    return agent_ids, tags


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http_get(url: str, api_key: str) -> dict[str, object]:
    import urllib.request

    req = urllib.request.Request(url, headers={"xi-api-key": api_key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())  # type: ignore[no-any-return]


def _http_patch(url: str, api_key: str, body: dict[str, object]) -> dict[str, object]:
    import urllib.request

    req = urllib.request.Request(
        url,
        method="PATCH",
        data=json.dumps(body).encode(),
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Diff logic
# ---------------------------------------------------------------------------


def _compute_diff(current: list[dict[str, object]], desired: list[AudioTag]) -> TagDiff:
    """Compute the diff between current EL tags and the desired YAML tags.

    current: list of dicts from EL API (keys: "tag", "description" or similar)
    desired: list of AudioTag from YAML
    """
    # Normalize current tags to a dict keyed by tag name
    current_map: dict[str, str] = {}
    for entry in current:
        tag_name = str(entry.get("tag", entry.get("name", ""))).strip()
        desc = str(entry.get("description", "")).strip()
        if tag_name:
            current_map[tag_name] = desc

    desired_map: dict[str, str] = {t.tag: t.description for t in desired}

    added = [name for name in desired_map if name not in current_map]
    removed = [name for name in current_map if name not in desired_map]
    modified = [
        name
        for name in desired_map
        if name in current_map and current_map[name] != desired_map[name]
    ]

    return TagDiff(tags_added=added, tags_removed=removed, tags_modified=modified)


# ---------------------------------------------------------------------------
# Receipts (Law #2)
# ---------------------------------------------------------------------------


def _emit_receipt(receipt: dict[str, object]) -> None:
    """Non-blocking receipt emission via receipt_store. Logs on failure."""
    try:
        from aspire_orchestrator.services.receipt_store import store_receipts

        store_receipts([receipt])
    except Exception as exc:
        logger.warning("Could not emit receipt (non-fatal): %s", exc)


def _build_receipt(
    *,
    agent_id: str,
    tag_count: int,
    tags_added: list[str],
    tags_removed: list[str],
    tags_modified: list[str],
    deployed_or_skipped: str,
    outcome: str,
    reason: Optional[str] = None,
) -> dict[str, object]:
    receipt_id = str(uuid.uuid4())
    return {
        "id": receipt_id,
        "receipt_type": "audio_tags_synced",
        "action_type": "audio_tags_synced",
        "risk_tier": "YELLOW",
        "actor_type": "SYSTEM",
        "actor_id": "sync_audio_tags",
        "outcome": outcome,
        "reason_code": reason or outcome,
        "redacted_inputs": {
            "agent_id": agent_id,
            "tag_count": tag_count,
        },
        "redacted_outputs": {
            "tags_added": tags_added,
            "tags_removed": tags_removed,
            "tags_modified": tags_modified,
            "deployed_or_skipped": deployed_or_skipped,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------


def _sync_agent(
    *,
    agent_id: str,
    desired_tags: list[AudioTag],
    api_key: str,
    dry_run: bool,
) -> tuple[bool, str]:
    """Sync audio tags for a single agent.

    Returns (success, receipt_id).
    Prints progress; never raises (returns False on failure per Law #7).
    """
    import urllib.error as _urllib_error

    # Fetch current agent config
    try:
        current_agent = _http_get(f"{_API_BASE}/{agent_id}", api_key)
    except _urllib_error.HTTPError as exc:
        reason = f"GET {agent_id} failed: HTTP {exc.code}"
        print(f"  FAIL: {reason}", file=sys.stderr)
        receipt = _build_receipt(
            agent_id=agent_id,
            tag_count=len(desired_tags),
            tags_added=[],
            tags_removed=[],
            tags_modified=[],
            deployed_or_skipped="failed",
            outcome="failed",
            reason=reason,
        )
        _emit_receipt(receipt)
        return False, receipt["id"]  # type: ignore[return-value]
    except Exception as exc:
        reason = f"GET {agent_id} failed: {exc}"
        print(f"  FAIL: {reason}", file=sys.stderr)
        receipt = _build_receipt(
            agent_id=agent_id,
            tag_count=len(desired_tags),
            tags_added=[],
            tags_removed=[],
            tags_modified=[],
            deployed_or_skipped="failed",
            outcome="failed",
            reason=reason,
        )
        _emit_receipt(receipt)
        return False, receipt["id"]  # type: ignore[return-value]

    # Extract current suggested_audio_tags
    current_tags_raw: list[dict[str, object]] = (
        current_agent.get("conversation_config", {})  # type: ignore[union-attr]
        .get("tts", {})
        .get("suggested_audio_tags", [])
    )

    diff = _compute_diff(current_tags_raw, desired_tags)

    if diff.is_empty:
        print(f"  SKIP {agent_id}: no diff detected — tags already up to date.")
        receipt = _build_receipt(
            agent_id=agent_id,
            tag_count=len(desired_tags),
            tags_added=[],
            tags_removed=[],
            tags_modified=[],
            deployed_or_skipped="skipped",
            outcome="success",
            reason="idempotent_no_diff",
        )
        _emit_receipt(receipt)
        return True, receipt["id"]  # type: ignore[return-value]

    print(
        f"  Diff for {agent_id}: "
        f"+{len(diff.tags_added)} added, "
        f"-{len(diff.tags_removed)} removed, "
        f"~{len(diff.tags_modified)} modified."
    )

    if dry_run:
        print(f"  [DRY-RUN] Would PATCH {agent_id} — skipping.")
        receipt = _build_receipt(
            agent_id=agent_id,
            tag_count=len(desired_tags),
            tags_added=diff.tags_added,
            tags_removed=diff.tags_removed,
            tags_modified=diff.tags_modified,
            deployed_or_skipped="dry_run",
            outcome="success",
            reason="dry_run",
        )
        _emit_receipt(receipt)
        return True, receipt["id"]  # type: ignore[return-value]

    # Build the EL API payload — each tag is {"tag": name, "description": desc}
    desired_payload: list[dict[str, str]] = [
        {"tag": t.tag, "description": t.description} for t in desired_tags
    ]

    body: dict[str, object] = {
        "conversation_config": {
            "tts": {
                "suggested_audio_tags": desired_payload
            }
        }
    }

    try:
        _http_patch(f"{_API_BASE}/{agent_id}", api_key, body)
    except _urllib_error.HTTPError as exc:
        err_body = exc.read().decode()[:500]
        reason = f"PATCH {agent_id} failed: HTTP {exc.code} — {err_body}"
        print(f"  FAIL: {reason}", file=sys.stderr)
        receipt = _build_receipt(
            agent_id=agent_id,
            tag_count=len(desired_tags),
            tags_added=diff.tags_added,
            tags_removed=diff.tags_removed,
            tags_modified=diff.tags_modified,
            deployed_or_skipped="failed",
            outcome="failed",
            reason=reason[:200],
        )
        _emit_receipt(receipt)
        return False, receipt["id"]  # type: ignore[return-value]
    except Exception as exc:
        reason = f"PATCH {agent_id} failed: {exc}"
        print(f"  FAIL: {reason}", file=sys.stderr)
        receipt = _build_receipt(
            agent_id=agent_id,
            tag_count=len(desired_tags),
            tags_added=diff.tags_added,
            tags_removed=diff.tags_removed,
            tags_modified=diff.tags_modified,
            deployed_or_skipped="failed",
            outcome="failed",
            reason=reason[:200],
        )
        _emit_receipt(receipt)
        return False, receipt["id"]  # type: ignore[return-value]

    print(
        f"  PATCH {agent_id} OK — {len(desired_tags)} tags deployed."
    )

    receipt = _build_receipt(
        agent_id=agent_id,
        tag_count=len(desired_tags),
        tags_added=diff.tags_added,
        tags_removed=diff.tags_removed,
        tags_modified=diff.tags_modified,
        deployed_or_skipped="deployed",
        outcome="success",
        reason="deployed",
    )
    _emit_receipt(receipt)
    return True, receipt["id"]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Post-PATCH verification
# ---------------------------------------------------------------------------


def _verify_agent_tags(
    *,
    agent_id: str,
    expected_tags: list[AudioTag],
    api_key: str,
) -> bool:
    """Re-fetch agent and verify tag count + all descriptions >= 20 chars."""
    import urllib.error as _urllib_error

    try:
        agent_data = _http_get(f"{_API_BASE}/{agent_id}", api_key)
    except (_urllib_error.HTTPError, Exception) as exc:
        print(f"  VERIFY FAIL {agent_id}: GET failed: {exc}", file=sys.stderr)
        return False

    live_tags: list[dict[str, object]] = (
        agent_data.get("conversation_config", {})  # type: ignore[union-attr]
        .get("tts", {})
        .get("suggested_audio_tags", [])
    )

    if len(live_tags) != len(expected_tags):
        print(
            f"  VERIFY FAIL {agent_id}: expected {len(expected_tags)} tags, "
            f"got {len(live_tags)} from EL.",
            file=sys.stderr,
        )
        return False

    desc_violations: list[str] = []
    for entry in live_tags:
        tag_name = str(entry.get("tag", entry.get("name", ""))).strip()
        desc = str(entry.get("description", "")).strip()
        if len(desc) < _MIN_DESCRIPTION_CHARS:
            desc_violations.append(
                f"tag '{tag_name}' description is only {len(desc)} chars in EL"
            )

    if desc_violations:
        print(f"  VERIFY FAIL {agent_id}: description violations:", file=sys.stderr)
        for v in desc_violations:
            print(f"    - {v}", file=sys.stderr)
        return False

    print(f"  VERIFY OK {agent_id}: {len(live_tags)} tags, all descriptions >= 20 chars.")
    return True


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync receptionist_audio_tags_v1.yaml to ElevenLabs agents."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate and compute diffs but do NOT PATCH EL.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    api_key = (
        os.environ.get("EL_API_KEY")
        or os.environ.get("ELEVENLABS_API_KEY")
        or os.environ.get("ASPIRE_ELEVENLABS_API_KEY")
    )
    if not api_key:
        print(
            "ERROR: set EL_API_KEY, ELEVENLABS_API_KEY, or ASPIRE_ELEVENLABS_API_KEY in environment.",
            file=sys.stderr,
        )
        return 1

    # Load and validate YAML (contract rule 12b enforced here — fail closed)
    agent_ids, desired_tags = _load_tags(_TAGS_FILE)

    print(f"Syncing {len(desired_tags)} tags to {len(agent_ids)} agents.")
    if args.dry_run:
        print("[DRY-RUN MODE] No EL PATCHes will be issued.")

    # Process agents in order: Tiffany first (canary), then Sarah-Receptionist,
    # then Sarah-FrontDesk. If any PATCH fails, stop — do not proceed.
    receipt_ids: dict[str, str] = {}

    for agent_id in agent_ids:
        print(f"\nProcessing agent: {agent_id}")
        success, receipt_id = _sync_agent(
            agent_id=agent_id,
            desired_tags=desired_tags,
            api_key=api_key,
            dry_run=args.dry_run,
        )
        receipt_ids[agent_id] = receipt_id

        if not success:
            print(
                f"\nSTOP: PATCH failed for {agent_id}. "
                f"Halting — remaining agents were NOT patched.",
                file=sys.stderr,
            )
            _print_summary(receipt_ids, agent_ids)
            return 3

    # Post-PATCH verification (only in non-dry-run mode)
    if not args.dry_run:
        print("\nPost-PATCH verification:")
        for agent_id in agent_ids:
            ok = _verify_agent_tags(
                agent_id=agent_id,
                expected_tags=desired_tags,
                api_key=api_key,
            )
            if not ok:
                print(
                    f"\nVERIFICATION FAILED for {agent_id}.",
                    file=sys.stderr,
                )
                _print_summary(receipt_ids, agent_ids)
                return 4

    _print_summary(receipt_ids, agent_ids)

    if args.dry_run:
        print("\nDry-run complete. No EL agents were modified.")
    else:
        print(
            "\nAll agents synced and verified. "
            f"{len(desired_tags)} audio tags live on {len(agent_ids)} receptionist agents."
        )

    return 0


def _print_summary(
    receipt_ids: dict[str, str],
    agent_ids: list[str],
) -> None:
    print("\n" + "=" * 70)
    print(f"{'AGENT ID':<44}  {'RECEIPT ID'}")
    print("-" * 70)
    for agent_id in agent_ids:
        rid = receipt_ids.get(agent_id, "(not reached)")
        print(f"{agent_id:<44}  {rid}")
    print("=" * 70)


if __name__ == "__main__":
    sys.exit(main())
