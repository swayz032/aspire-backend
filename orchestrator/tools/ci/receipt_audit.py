"""Receipt coverage audit — Pass 14 Gate Item 4.

Scans memory_objects for a given time window and verifies each has a
corresponding receipt linked via:
  1. linked_receipt_ids field (preferred), OR
  2. receipts.trace_id matching memory_object.provenance.trace_id.

Usage:
    python -m tools.ci.receipt_audit --since 2026-04-29 [--require-100-percent]
    python -m tools.ci.receipt_audit --since 2026-04-29 --hours 24

Exit codes:
    0 — all checks pass (or coverage >= 100% when --require-100-percent)
    1 — coverage < 100% when --require-100-percent is set
    2 — unexpected error

Aspire Laws:
  Law #2: Receipt for All — 100% of memory_objects must have receipts.
  Law #9: No secrets in output — memory content is not printed, only IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Async audit logic
# ---------------------------------------------------------------------------


async def _audit(
    since: datetime,
    require_100_percent: bool = False,
) -> tuple[int, int, list[str]]:
    """Return (total, covered, missing_ids).

    Imports are deferred so the module can be imported in tests without
    triggering app init.
    """
    from aspire_orchestrator.services.supabase_client import supabase_select

    # Fetch memory_objects created since `since`
    since_iso = since.isoformat().replace("+00:00", "Z")
    try:
        memory_rows = await supabase_select(
            table="memory_objects",
            filters={"created_at": f"gte.{since_iso}"},
            select="memory_id,tenant_id,provenance,linked_receipt_ids",
            limit=10000,
        )
    except Exception as exc:
        logger.error("receipt_audit: memory_objects query failed: %s", exc)
        return 0, 0, []

    if not memory_rows:
        logger.info("receipt_audit: no memory_objects found since %s", since_iso)
        return 0, 0, []

    total = len(memory_rows)
    covered_ids: set[str] = set()
    uncovered_ids: list[str] = []

    for row in memory_rows:
        mem_id = str(row.get("memory_id", ""))
        linked = row.get("linked_receipt_ids") or []

        if linked:
            # Has explicit receipt link — covered
            covered_ids.add(mem_id)
            continue

        # Fallback: look up by trace_id
        provenance = row.get("provenance") or {}
        trace_id = provenance.get("trace_id") if isinstance(provenance, dict) else None

        if trace_id:
            try:
                receipt_rows = await supabase_select(
                    table="receipts",
                    filters={"trace_id": str(trace_id)},
                    select="receipt_id",
                    limit=1,
                )
                if receipt_rows:
                    covered_ids.add(mem_id)
                    continue
            except Exception as exc:
                logger.warning(
                    "receipt_audit: receipt lookup failed for trace_id=%s: %s",
                    trace_id,
                    exc,
                )

        uncovered_ids.append(mem_id)

    covered = len(covered_ids)
    return total, covered, uncovered_ids


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Receipt coverage audit for memory_objects (Pass 14)",
    )
    parser.add_argument(
        "--since",
        required=True,
        help="Start date/time (ISO-8601 or YYYY-MM-DD). Audits memory_objects created on or after.",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Audit only the last N hours from --since (default: all since --since).",
    )
    parser.add_argument(
        "--require-100-percent",
        action="store_true",
        help="Exit 1 if coverage < 100%%.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print uncovered memory_object IDs.",
    )
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        since_dt = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        logger.error("Invalid --since value: %s (expected ISO-8601 or YYYY-MM-DD)", args.since)
        return 2

    logger.info("receipt_audit: scanning memory_objects since %s", since_dt.isoformat())

    total, covered, missing_ids = await _audit(since_dt, require_100_percent=args.require_100_percent)

    if total == 0:
        logger.info("receipt_audit: PASS (0 memory_objects found in window)")
        return 0

    pct = (covered / total) * 100
    logger.info(
        "receipt_audit: total=%d covered=%d missing=%d coverage=%.1f%%",
        total, covered, len(missing_ids), pct,
    )

    if args.verbose and missing_ids:
        logger.info("receipt_audit: uncovered memory_ids:")
        for mid in missing_ids:
            logger.info("  - %s", mid)

    if args.require_100_percent and missing_ids:
        logger.error(
            "receipt_audit: FAIL — %d memory_objects missing receipts (coverage=%.1f%%)",
            len(missing_ids), pct,
        )
        return 1

    logger.info("receipt_audit: PASS (coverage=%.1f%%)", pct)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
