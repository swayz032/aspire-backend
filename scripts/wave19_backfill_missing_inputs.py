"""Wave 19 backfill: run cascade self-resolution on existing missing_inputs.

Runs the Wave 19c cascade against all unresolved blueprint_missing_inputs rows
for a given project_id. Marks resolved rows with resolved_by='cascade:wave19:<method>'
and resolved_at=now().

Typical usage after Wave 19 deploy:
    railway run python -m scripts.wave19_backfill_missing_inputs 5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2

Dry-run (reports outcomes without writing to DB):
    python -m scripts.wave19_backfill_missing_inputs <project_id> --dry-run

Law compliance:
    Law #1: no autonomous decisions; cascade is deterministic per-candidate routing.
    Law #2: receipt emitted per cascade run via resolve_or_emit.
    Law #6: every query/update scoped by suite_id.

Expected result for Gulfstream (project 5b0adf45...):
    Before:  22 rows (post-19a dedup; all 22 are textually unique)
    After:   2-4 rows (legitimate residuals requiring contractor input)
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

_log = logging.getLogger("wave19_backfill")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


async def _load_project_suite_id(project_id: str) -> str:
    """Look up suite_id for the given project_id."""
    from aspire_orchestrator.services.supabase_client import supabase_select
    rows = await supabase_select(
        "blueprint_projects",
        filters=f"id=eq.{project_id}",
    )
    if not rows:
        raise SystemExit(f"Project not found: {project_id}")
    return str(rows[0]["suite_id"])


async def _load_unresolved(project_id: str, suite_id: str) -> list[dict[str, Any]]:
    from aspire_orchestrator.services.supabase_client import supabase_select
    rows = await supabase_select(
        "blueprint_missing_inputs",
        filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}&resolved_at=is.null",
        order_by="created_at.asc",
    )
    return rows or []


async def _load_project_context(
    project_id: str, suite_id: str
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Load active sheets, symbols_by_sheet, and ocr_by_sheet."""
    from aspire_orchestrator.services.supabase_client import supabase_select, SupabaseClientError

    sheets = await supabase_select(
        "blueprint_sheets",
        filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        order_by="created_at.asc",
    )
    active_sheets = [s for s in (sheets or []) if not s.get("supersedes_id")]

    symbols_by_sheet: dict[str, list[dict[str, Any]]] = {}
    sheet_ids_csv = ",".join(str(s["id"]) for s in active_sheets if s.get("id"))
    if sheet_ids_csv:
        try:
            sym_rows = await supabase_select(
                "blueprint_symbols",
                filters=f"sheet_id=in.({sheet_ids_csv})&suite_id=eq.{suite_id}",
            )
            for row in (sym_rows or []):
                sid = str(row.get("sheet_id") or "")
                if sid:
                    symbols_by_sheet.setdefault(sid, []).append(row)
        except SupabaseClientError:
            _log.warning("Failed to load symbols (non-fatal)")

    ocr_by_sheet: dict[str, str] = {
        str(s["id"]): str(s.get("ocr_text") or "")
        for s in active_sheets if s.get("id")
    }
    return active_sheets, symbols_by_sheet, ocr_by_sheet


async def _mark_resolved(
    mi_id: str,
    suite_id: str,
    method: str,
    dry_run: bool,
) -> bool:
    if dry_run:
        _log.info("  [DRY-RUN] would mark resolved: %s via cascade:wave19:%s", mi_id[:8], method)
        return True
    try:
        from aspire_orchestrator.services.supabase_client import supabase_update
        # resolved_by is uuid FK (auth.users) — NULL for automated cascade.
        # resolved_method is the Wave 19 text column for the resolver method.
        await supabase_update(
            "blueprint_missing_inputs",
            f"id=eq.{mi_id}&suite_id=eq.{suite_id}",
            {
                "resolved_method": f"cascade:wave19:{method}",
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return True
    except Exception as exc:
        _log.warning("  Failed to mark resolved %s: %s", mi_id[:8], type(exc).__name__)
        return False


async def run_backfill(project_id: str, *, dry_run: bool = False) -> None:
    """Main backfill entry point."""
    _log.info("=== Wave 19 Backfill — project=%s dry_run=%s ===", project_id[:8], dry_run)

    suite_id = await _load_project_suite_id(project_id)
    _log.info("suite_id=%s", suite_id[:8])

    unresolved = await _load_unresolved(project_id, suite_id)
    _log.info("Unresolved missing_inputs before backfill: %d", len(unresolved))

    if not unresolved:
        _log.info("Nothing to backfill.")
        return

    # Print all current rows for visibility
    for i, row in enumerate(unresolved, 1):
        _log.info("  [%02d] %s: %s", i, row["id"][:8], str(row.get("description") or "")[:80])

    active_sheets, symbols_by_sheet, ocr_by_sheet = await _load_project_context(
        project_id, suite_id
    )
    _log.info("Loaded %d active sheets for context", len(active_sheets))

    # Wave 19.1c (Commit 2.5): use ``resolve_batch`` so the cascade owns
    # SheetDataProvider construction. The earlier Wave 19.1b backfill ran
    # via direct ``resolve_or_emit`` calls and silently degraded every
    # resolver to no-PNG mode (Gulfstream: 0/6 schedule rows resolved).
    from aspire_orchestrator.services.blueprint.missing_input_cascade import (
        build_candidate,
        resolve_batch,
    )

    correlation_id = str(uuid.uuid4())
    _log.info("Cascade correlation_id=%s", correlation_id[:8])

    # Build candidates only for category-routing visibility (logs below).
    # The actual dispatch goes through ``resolve_batch`` which builds its
    # own candidates with the shared provider attached.
    preview_candidates = [
        build_candidate(row, active_sheets, symbols_by_sheet, ocr_by_sheet)
        for row in unresolved
    ]

    # Print category routing
    _log.info("\nCategory routing:")
    for c, row in zip(preview_candidates, unresolved):
        _log.info("  %s -> %s", row["id"][:8], c.category.value)

    _log.info("\nDispatching cascade via resolve_batch (one provider for batch)...")
    batch = await resolve_batch(
        unresolved,
        suite_id=suite_id,
        project_id=project_id,
        correlation_id=correlation_id,
        active_sheets=active_sheets,
        symbols_by_sheet=symbols_by_sheet,
        ocr_by_sheet=ocr_by_sheet,
    )
    outcomes = batch.outcomes
    _log.info(
        "Provider cache stats: %s",
        batch.provider_cache_stats,
    )

    resolved_count = 0
    unresolved_count = 0
    error_count = 0
    category_stats: dict[str, dict[str, int]] = {}

    # ``resolve_batch`` may drop candidates that raised inside the cascade,
    # so outcomes can be shorter than ``unresolved``. Index rows by id for
    # safe lookup. Rows present in ``unresolved`` but absent from outcomes
    # are reported as ERRORs.
    rows_by_id = {str(r["id"]): r for r in unresolved}
    seen_ids: set[str] = set()

    _log.info("\nResults:")
    for outcome in outcomes:
        mi_id = str(outcome.missing_input_id or "")
        seen_ids.add(mi_id)
        row = rows_by_id.get(mi_id, {})
        cat = str(row.get("description") or "")[:40]

        cat_name = outcome.category.value
        if cat_name not in category_stats:
            category_stats[cat_name] = {"resolved": 0, "unresolved": 0}

        if outcome.resolved and outcome.best_result:
            method = outcome.best_result.method
            conf = outcome.best_result.confidence
            _log.info(
                "  RESOLVED %s [%s] via %s (conf=%.2f)",
                mi_id[:8], cat_name, method, conf,
            )
            success = await _mark_resolved(mi_id, suite_id, method, dry_run)
            if success:
                resolved_count += 1
                category_stats[cat_name]["resolved"] += 1
            else:
                error_count += 1
        else:
            _log.info(
                "  OPEN     %s [%s] best_conf=%.2f — requires contractor",
                mi_id[:8],
                cat_name,
                max((r.confidence for r in outcome.all_results), default=0.0),
            )
            unresolved_count += 1
            category_stats[cat_name]["unresolved"] += 1

    # Report any rows dropped by the cascade (raised inside resolve_or_emit
    # or excluded by suite_id mismatch in resolve_batch).
    for mi_id, row in rows_by_id.items():
        if mi_id in seen_ids:
            continue
        _log.error(
            "  ERROR %s: dropped by cascade (no outcome returned)",
            mi_id[:8],
        )
        error_count += 1

    # Emit backfill receipt
    if not dry_run:
        try:
            from aspire_orchestrator.services.receipt_store import store_receipts
            store_receipts([{
                "id": str(uuid.uuid4()),
                "outcome": "success",
                "action_type": "blueprint.cascade.backfill",
                "receipt_type": "blueprint.cascade.backfill",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "risk_tier": "GREEN",
                "actor_type": "WORKER",
                "actor_id": "script:wave19_backfill",
                "correlation_id": correlation_id,
                "suite_id": suite_id,
                # Wave 19.1c (Commit 2.6) — receipt_store harvests
                # ``redacted_outputs`` into the persisted ``result`` jsonb
                # column. ``metadata`` is silently dropped by the mapper.
                "redacted_outputs": {
                    "project_id": project_id,
                    "before_count": len(unresolved),
                    "resolved_count": resolved_count,
                    "unresolved_count": unresolved_count,
                    "error_count": error_count,
                    "category_stats": category_stats,
                    "provider_cache_stats": batch.provider_cache_stats,
                },
                "metadata": {
                    "project_id": project_id,
                    "before_count": len(unresolved),
                    "resolved_count": resolved_count,
                    "unresolved_count": unresolved_count,
                    "error_count": error_count,
                    "category_stats": category_stats,
                    "provider_cache_stats": batch.provider_cache_stats,
                },
            }])
        except Exception as exc:
            _log.warning("Receipt emission failed: %s", type(exc).__name__)

    # Final summary
    remaining = len(unresolved) - resolved_count
    _log.info("\n=== Backfill Summary ===")
    _log.info("Before:    %d unresolved", len(unresolved))
    _log.info("Resolved:  %d", resolved_count)
    _log.info("Remaining: %d (legitimate contractor todos)", remaining)
    _log.info("Errors:    %d", error_count)
    _log.info("")
    _log.info("Per-category resolution:")
    for cat, stats in sorted(category_stats.items()):
        total = stats["resolved"] + stats["unresolved"]
        _log.info("  %-12s %d/%d resolved", cat, stats["resolved"], total)

    if not dry_run:
        # Verify final count in DB
        final_open = await _load_unresolved(project_id, suite_id)
        _log.info("\nDB verification: %d unresolved remaining (was %d)", len(final_open), len(unresolved))
        if final_open:
            _log.info("Legitimate open items:")
            for row in final_open:
                _log.info("  %s: %s", row["id"][:8], str(row.get("description") or "")[:80])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wave 19 backfill: cascade self-resolution on existing missing_inputs"
    )
    parser.add_argument("project_id", help="UUID of the blueprint project to backfill")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report outcomes without writing to DB",
    )
    args = parser.parse_args()

    asyncio.run(run_backfill(args.project_id, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
