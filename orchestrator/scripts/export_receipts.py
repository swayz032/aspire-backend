"""Export Receipts — Query receipt store and output JSON for evidence bundles.

Usage:
    python export_receipts.py [--suite-id UUID] [--since ISO8601] [--output FILE]

Outputs all receipts matching the filter criteria as a JSON array.
Used for partner approval evidence, compliance audits, and incident investigation.

Law #2 compliance: Read-only export, never modifies receipts.
Law #6 compliance: suite_id filter enforces tenant scoping.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

# Allow running from project root or scripts/ dir
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "src"))

from aspire_orchestrator.services.receipt_store import (
    get_receipt_count,
    query_receipts,
    _receipts,
    _lock,
)


def _get_all_receipts() -> list[dict[str, Any]]:
    """Get all receipts from in-memory store (no suite filter)."""
    with _lock:
        return list(_receipts)


def export_receipts(
    *,
    suite_id: str | None = None,
    since: str | None = None,
    receipt_type: str | None = None,
    outcome: str | None = None,
    limit: int = 10000,
    admin_token: str | None = None,
) -> list[dict[str, Any]]:
    """Query and return receipts matching filter criteria."""
    if suite_id:
        # Use the proper API when suite_id is available (Law #6)
        receipts = query_receipts(suite_id=suite_id, limit=limit)
    else:
        # No suite filter — cross-tenant export requires admin token (Law #6)
        token = admin_token or __import__("os").environ.get("ASPIRE_ADMIN_TOKEN")
        if not token:
            raise ValueError(
                "Cross-tenant export requires ASPIRE_ADMIN_TOKEN env var "
                "or --admin-token argument (Law #6: tenant isolation)"
            )
        receipts = _get_all_receipts()

    # Apply additional filters
    if receipt_type:
        receipts = [r for r in receipts if r.get("receipt_type") == receipt_type]
    if outcome:
        receipts = [r for r in receipts if r.get("outcome") == outcome]

    # Apply time filter if specified
    if since:
        since_dt = datetime.fromisoformat(since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        receipts = [
            r for r in receipts
            if r.get("created_at", "") >= since_dt.isoformat()
        ]

    return receipts[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Aspire receipts to JSON")
    parser.add_argument("--suite-id", help="Filter by suite_id (Law #6 scoping)")
    parser.add_argument("--since", help="Filter receipts created after ISO8601 timestamp")
    parser.add_argument("--receipt-type", help="Filter by receipt_type")
    parser.add_argument("--outcome", help="Filter by outcome (success/denied/failed)")
    parser.add_argument("--limit", type=int, default=10000, help="Max receipts to export")
    parser.add_argument("--admin-token", help="Admin token for cross-tenant exports (Law #6)")
    parser.add_argument("--output", "-o", help="Output file (default: stdout)")
    args = parser.parse_args()

    receipts = export_receipts(
        suite_id=args.suite_id,
        since=args.since,
        receipt_type=args.receipt_type,
        outcome=args.outcome,
        limit=args.limit,
        admin_token=args.admin_token,
    )

    result = {
        "export_timestamp": datetime.now(timezone.utc).isoformat(),
        "total_in_store": get_receipt_count(),
        "exported_count": len(receipts),
        "filters": {
            "suite_id": args.suite_id,
            "since": args.since,
            "receipt_type": args.receipt_type,
            "outcome": args.outcome,
            "limit": args.limit,
        },
        "receipts": receipts,
    }

    output = json.dumps(result, indent=2, default=str)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Exported {len(receipts)} receipts to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
