"""Export Provider Calls — Extract provider call data from receipts.

Usage:
    python export_provider_calls.py [--provider PROVIDER_ID] [--output FILE]

Filters receipts for tool execution types and extracts provider metadata.
Used for partner approval evidence (Gusto/Plaid submission packets).

Law #2 compliance: Read-only export from receipt store.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "src"))

from aspire_orchestrator.services.receipt_store import _receipts, _lock


def _get_all_receipts() -> list[dict[str, Any]]:
    """Get all receipts from in-memory store."""
    with _lock:
        return list(_receipts)


def export_provider_calls(
    *,
    provider_id: str | None = None,
    suite_id: str | None = None,
    since: str | None = None,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    """Extract provider call data from tool execution receipts."""
    all_receipts = _get_all_receipts()
    receipts = [r for r in all_receipts if r.get("receipt_type") == "tool.execution"]

    calls: list[dict[str, Any]] = []
    for r in receipts:
        actor = r.get("actor_id", "")
        if provider_id and not actor.startswith(f"provider.{provider_id}"):
            continue
        if suite_id and r.get("suite_id") != suite_id:
            continue
        if since and r.get("created_at", "") < since:
            continue

        calls.append({
            "receipt_id": r.get("id"),
            "correlation_id": r.get("correlation_id"),
            "suite_id": r.get("suite_id"),
            "office_id": r.get("office_id"),
            "provider": actor.replace("provider.", "", 1) if actor.startswith("provider.") else actor,
            "tool_used": r.get("tool_used"),
            "action_type": r.get("action_type"),
            "risk_tier": r.get("risk_tier"),
            "outcome": r.get("outcome"),
            "reason_code": r.get("reason_code"),
            "created_at": r.get("created_at"),
            "provider_metadata": r.get("provider_metadata", {}),
        })

    return calls[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description="Export provider call data from receipts")
    parser.add_argument("--provider", help="Filter by provider_id (e.g., stripe, gusto)")
    parser.add_argument("--suite-id", help="Filter by suite_id")
    parser.add_argument("--since", help="Filter calls after ISO8601 timestamp")
    parser.add_argument("--limit", type=int, default=10000, help="Max calls to export")
    parser.add_argument("--output", "-o", help="Output file (default: stdout)")
    args = parser.parse_args()

    calls = export_provider_calls(
        provider_id=args.provider,
        suite_id=args.suite_id,
        since=args.since,
        limit=args.limit,
    )

    result = {
        "export_timestamp": datetime.now(timezone.utc).isoformat(),
        "exported_count": len(calls),
        "filters": {
            "provider": args.provider,
            "suite_id": args.suite_id,
            "since": args.since,
            "limit": args.limit,
        },
        "provider_calls": calls,
    }

    output = json.dumps(result, indent=2, default=str)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Exported {len(calls)} provider calls to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
