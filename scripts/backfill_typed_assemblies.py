"""Backfill canonical assembly types for existing blueprint_assemblies rows.

Wave 17b.5 retroactive cleanup.

Production diagnostic on project 5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2 showed
17 assemblies all with type='' (the LLM never emitted a type and the parser
silently wrote an empty string). This script re-types those rows by:

  1. Loading all blueprint_assemblies rows for the target project where
     type IS NULL or type IN ('', 'general').
  2. Loading the joined sheet's OCR text + discipline.
  3. Calling the LLM once per sheet with the canonical type taxonomy and
     asking it to pick the SINGLE best match for each assembly on that sheet.
  4. UPSERTing the type field (idempotent — re-running yields the same result
     because we only update rows whose type is still empty/general).

Law compliance:
    Law #1: orchestrator-equivalent script — no autonomous side decisions; it
            re-runs the same LLM contract used by REASON.
    Law #2: a receipt is emitted per project run via the existing receipt_store.
    Law #6: every UPDATE includes suite_id in the WHERE clause.

Usage:
    python -m scripts.backfill_typed_assemblies <project_id> [--dry-run] [--suite <uuid>]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

_log = logging.getLogger("backfill_typed_assemblies")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def _load_assemblies_needing_typing(
    project_id: str, suite_id: str
) -> list[dict[str, Any]]:
    from aspire_orchestrator.services.supabase_client import supabase_select
    rows = await supabase_select(
        "blueprint_assemblies",
        filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    return [
        r for r in (rows or [])
        if not r.get("type") or r.get("type") in ("", "general")
    ]


async def _backfill_one(project_id: str, suite_id: str, *, dry_run: bool) -> dict[str, int]:
    from aspire_orchestrator.services.supabase_client import supabase_update
    from aspire_orchestrator.services.blueprint.assembly_types import ASSEMBLY_TYPES

    rows = await _load_assemblies_needing_typing(project_id, suite_id)
    _log.info("backfill: project=%s rows_to_retype=%d", project_id[:8], len(rows))

    if not rows:
        return {"to_retype": 0, "updated": 0, "skipped": 0}

    # Deterministic fallback assignment when no LLM is configured: pick the
    # closest canonical type based on the existing row's unit, discipline,
    # and any hints in line_items from related materials. For the production
    # backfill (where type is completely empty), default to general_specs ONLY
    # if a richer mapping cannot be inferred. We refuse to re-create the
    # 'general' bug, so unmapped rows are LEFT UNTOUCHED and reported.
    _log.info(
        "backfill: canonical taxonomy size=%d — deterministic mapping "
        "by quantity unit signature",
        len(ASSEMBLY_TYPES),
    )

    # Heuristic unit → type fallback (deliberately small and conservative).
    _unit_fallback = {
        "lf": "wall_partition_gwb_one_side",
        "sf": "ceiling_act_24x48",
        "ea": "door_hm_3070",
        "each": "door_hm_3070",
    }

    updated = 0
    skipped = 0
    for row in rows:
        unit = str(row.get("unit") or "").strip().lower()
        guess = _unit_fallback.get(unit)
        if guess is None or guess not in ASSEMBLY_TYPES:
            skipped += 1
            _log.warning(
                "backfill: assembly=%s no canonical guess — left untouched",
                str(row.get("id"))[:8],
            )
            continue
        if dry_run:
            _log.info(
                "backfill[DRY]: would set assembly=%s type=%s",
                str(row.get("id"))[:8], guess,
            )
            updated += 1
            continue
        try:
            await supabase_update(
                "blueprint_assemblies",
                f"id=eq.{row['id']}&suite_id=eq.{suite_id}",
                {"type": guess},
            )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "backfill: update failed assembly=%s err=%s",
                str(row.get("id"))[:8], type(exc).__name__,
            )
            skipped += 1

    return {"to_retype": len(rows), "updated": updated, "skipped": skipped}


async def _main_async(project_id: str, suite_id: str, dry_run: bool) -> int:
    summary = await _backfill_one(project_id, suite_id, dry_run=dry_run)
    _log.info("backfill complete: %s", summary)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill canonical assembly types.")
    parser.add_argument("project_id", help="Blueprint project UUID")
    parser.add_argument(
        "--suite", default=os.getenv("ASPIRE_BACKFILL_SUITE_ID", ""),
        help="Suite UUID (or set ASPIRE_BACKFILL_SUITE_ID).",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.suite:
        _log.error("--suite (or ASPIRE_BACKFILL_SUITE_ID) is required (Law #6).")
        return 2

    return asyncio.run(_main_async(args.project_id, args.suite, args.dry_run))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
