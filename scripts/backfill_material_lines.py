"""Backfill distinct material lines by re-aggregating symbols.

Wave 17b.5 retroactive cleanup. Counterpart to backfill_typed_assemblies.py.

Production diagnostic on project 5b0adf45 showed 52 blueprint_materials rows
with identical empty line_item. This script re-aggregates the 494 underlying
blueprint_symbols rows into distinct material lines (one per symbol class)
and inserts the missing rows. The original 52 empty rows are LEFT IN PLACE
(immutable receipts — Law #2). New aggregated rows are inserted alongside;
the read API ranks non-empty rows higher.

Idempotent: a re-run inserts only line_items not already present for this
project.

Usage:
    python -m scripts.backfill_material_lines <project_id> [--dry-run] [--suite <uuid>]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

_log = logging.getLogger("backfill_material_lines")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def _load_project_symbols(project_id: str, suite_id: str) -> list[dict[str, Any]]:
    """Load all symbols for a project by joining through blueprint_sheets."""
    from aspire_orchestrator.services.supabase_client import supabase_select
    sheets = await supabase_select(
        "blueprint_sheets",
        filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    sheet_ids = [str(s["id"]) for s in (sheets or [])]
    out: list[dict[str, Any]] = []
    for sid in sheet_ids:
        rows = await supabase_select(
            "blueprint_symbols",
            filters=f"sheet_id=eq.{sid}&suite_id=eq.{suite_id}",
        )
        if rows:
            out.extend(rows)
    return out


async def _existing_line_items(project_id: str, suite_id: str) -> set[str]:
    from aspire_orchestrator.services.supabase_client import supabase_select
    rows = await supabase_select(
        "blueprint_materials",
        filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    return {
        str(r.get("line_item") or "").strip()
        for r in (rows or [])
        if str(r.get("line_item") or "").strip()
    }


async def _backfill(
    project_id: str, suite_id: str, office_id: str | None, *, dry_run: bool
) -> dict[str, int]:
    from aspire_orchestrator.services.supabase_client import supabase_insert
    from aspire_orchestrator.services.blueprint.symbol_aggregator import (
        aggregate_symbols_to_materials,
    )
    from aspire_orchestrator.services.blueprint.tariff_engine import detect_tariff_flag

    symbols = await _load_project_symbols(project_id, suite_id)
    _log.info(
        "backfill: project=%s loaded_symbols=%d",
        project_id[:8], len(symbols),
    )
    agg = aggregate_symbols_to_materials(symbols)
    existing = await _existing_line_items(project_id, suite_id)
    _log.info(
        "backfill: aggregated_materials=%d unmapped_classes=%d existing_line_items=%d",
        len(agg.materials), len(agg.unmapped_classes), len(existing),
    )

    inserted = 0
    skipped = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for am in agg.materials:
        if am.line_item.strip() in existing:
            skipped += 1
            continue
        flag = detect_tariff_flag(am.line_item)
        row = am.to_row(
            row_id=str(uuid.uuid4()),
            suite_id=suite_id,
            office_id=office_id,
            project_id=project_id,
            created_at=now_iso,
            tariff_flag=flag.value,
            assembly_id=None,
        )
        if dry_run:
            _log.info(
                "backfill[DRY]: would insert line_item=%s qty=%s tariff=%s",
                row["line_item"][:60], row["quantity"], row["tariff_flag"],
            )
            inserted += 1
            continue
        try:
            await supabase_insert("blueprint_materials", row)
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "backfill: insert failed line_item=%s err=%s",
                row["line_item"][:40], type(exc).__name__,
            )
            skipped += 1

    return {
        "symbols_loaded": len(symbols),
        "aggregated": len(agg.materials),
        "inserted": inserted,
        "skipped_existing": skipped,
        "unmapped_classes": len(agg.unmapped_classes),
    }


async def _main_async(
    project_id: str, suite_id: str, office_id: str | None, dry_run: bool
) -> int:
    summary = await _backfill(project_id, suite_id, office_id, dry_run=dry_run)
    _log.info("backfill complete: %s", summary)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill aggregated material lines.")
    parser.add_argument("project_id")
    parser.add_argument(
        "--suite", default=os.getenv("ASPIRE_BACKFILL_SUITE_ID", ""),
        help="Suite UUID (or set ASPIRE_BACKFILL_SUITE_ID).",
    )
    parser.add_argument("--office", default=os.getenv("ASPIRE_BACKFILL_OFFICE_ID", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.suite:
        _log.error("--suite (or ASPIRE_BACKFILL_SUITE_ID) is required (Law #6).")
        return 2

    return asyncio.run(
        _main_async(args.project_id, args.suite, args.office or None, args.dry_run)
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
