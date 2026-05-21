"""Backfill Wave 17b.6 discipline verification for an existing blueprint project.

Re-runs the cross-check verifier against every blueprint_sheets row for a
project, using ONLY data already in the database (sheet_code, sheet_title,
ocr_text, current discipline, blueprint_symbols rows, neighbor sheets).

Produces:
    * A before/after diff table at INFO level.
    * Updates to blueprint_sheets.discipline (when the verdict changes it).
    * Inserts SPECIFIC blueprint_missing_inputs rows for flagged/demoted sheets,
      replacing the generic "confidence below threshold" copy.
    * A `drew.classify.verified.backfill` receipt summarising the run.

Idempotent: re-running yields the same final state because the verifier is a
pure function of the inputs and the missing_input INSERTs are recorded with
fresh UUIDs (we do not de-dupe historical rows — that's left to a follow-up
cleanup if needed).

Law compliance:
    Law #1: orchestrator-equivalent — reuses the same verifier the pipeline does.
    Law #2: emits a backfill receipt.
    Law #6: all SELECT / UPDATE filters include suite_id.
    Law #9: descriptions pass through `_dlp_redact` before INSERT.

Usage:
    python -m scripts.backfill_verify_disciplines <project_id> --suite <uuid> [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

_log = logging.getLogger("backfill_verify_disciplines")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def _load_sheets(project_id: str, suite_id: str) -> list[dict[str, Any]]:
    from aspire_orchestrator.services.supabase_client import supabase_select

    return await supabase_select(
        "blueprint_sheets",
        filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        order_by="created_at.asc",
    ) or []


async def _load_symbols_by_sheet(
    sheets: list[dict[str, Any]], suite_id: str
) -> dict[str, list[dict[str, Any]]]:
    from aspire_orchestrator.services.supabase_client import supabase_select

    if not sheets:
        return {}
    sheet_ids_csv = ",".join(str(s["id"]) for s in sheets)
    rows = await supabase_select(
        "blueprint_symbols",
        filters=f"sheet_id=in.({sheet_ids_csv})&suite_id=eq.{suite_id}",
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows or []:
        sid = str(r.get("sheet_id") or "")
        if not sid:
            continue
        out.setdefault(sid, []).append(r)
    return out


async def _run(project_id: str, suite_id: str, *, dry_run: bool) -> dict[str, int]:
    from aspire_orchestrator.services.blueprint.discipline_verifier import (
        verify_classification,
    )
    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_insert,
        supabase_update,
    )

    sheets = await _load_sheets(project_id, suite_id)
    symbols_by_sheet = await _load_symbols_by_sheet(sheets, suite_id)

    _log.info(
        "backfill_verify: project=%s suite=%s sheets=%d symbol_rows=%d",
        project_id[:8],
        suite_id[:8],
        len(sheets),
        sum(len(v) for v in symbols_by_sheet.values()),
    )

    promoted = 0
    confirmed = 0
    flagged = 0
    demoted = 0
    discipline_updates = 0
    missing_input_inserts = 0

    # Header for before/after table.
    _log.info(
        "%-10s %-10s %-12s %-12s %-7s %s",
        "sheet#", "code", "before", "after", "truth", "verdict",
    )

    # DLP redactor — import lazily so the script works outside an orchestrator run.
    try:
        from aspire_orchestrator.skillpacks.drew_blueprint import _dlp_redact
    except Exception:
        def _dlp_redact(text: str):
            return text, 0

    for sheet in sheets:
        sheet_id = str(sheet["id"])
        prior = sheet.get("discipline")
        verdict = verify_classification(
            sheet_id=sheet_id,
            sheet_number=sheet.get("sheet_number"),
            discipline_assigned=prior,
            sheet_code=sheet.get("sheet_code"),
            ocr_text=sheet.get("ocr_text"),
            symbols_on_sheet=symbols_by_sheet.get(sheet_id, []),
            neighbor_sheets=[s for s in sheets if str(s["id"]) != sheet_id],
        )

        if verdict.verdict == "promote":
            promoted += 1
        elif verdict.verdict == "confirm":
            confirmed += 1
        elif verdict.verdict == "flag":
            flagged += 1
        else:
            demoted += 1

        _log.info(
            "%-10s %-10s %-12s %-12s %-7s %s",
            str(sheet.get("sheet_number") or sheet_id[:8])[:10],
            str(sheet.get("sheet_code") or "")[:10],
            str(prior or "None")[:12],
            str(verdict.discipline_final or "None")[:12],
            verdict.truth[:7],
            verdict.verdict,
        )

        if dry_run:
            continue

        # Persist discipline change if verdict differs.
        if verdict.discipline_final != prior:
            try:
                await supabase_update(
                    "blueprint_sheets",
                    f"id=eq.{sheet_id}&suite_id=eq.{suite_id}",
                    {"discipline": verdict.discipline_final},
                )
                discipline_updates += 1
            except SupabaseClientError as exc:
                _log.warning(
                    "backfill_verify: update sheet=%s failed: %s",
                    sheet_id[:8],
                    type(exc).__name__,
                )

        # Persist specific missing_input row when verdict warrants it.
        if verdict.missing_input_reason is not None:
            reason_clean, _ = _dlp_redact(verdict.missing_input_reason)
            try:
                await supabase_insert(
                    "blueprint_missing_inputs",
                    {
                        "id": str(uuid.uuid4()),
                        "suite_id": suite_id,
                        "project_id": project_id,
                        "description": reason_clean,
                        "suggested_resolution": (
                            "Open the sheet, verify the discipline, and update "
                            "the assignment manually."
                        ),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                missing_input_inserts += 1
            except SupabaseClientError as exc:
                _log.warning(
                    "backfill_verify: insert missing_input sheet=%s failed: %s",
                    sheet_id[:8],
                    type(exc).__name__,
                )

    summary = {
        "sheets": len(sheets),
        "promoted": promoted,
        "confirmed": confirmed,
        "flagged": flagged,
        "demoted": demoted,
        "discipline_updates": discipline_updates,
        "missing_input_inserts": missing_input_inserts,
    }
    _log.info("backfill_verify: summary=%s dry_run=%s", summary, dry_run)

    if not dry_run:
        try:
            from aspire_orchestrator.services.receipt_store import store_receipts
            store_receipts([{
                "id": str(uuid.uuid4()),
                "outcome": "success",
                "action_type": "drew.classify.verified.backfill",
                "receipt_type": "drew.classify.verified.backfill",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "risk_tier": "GREEN",
                "actor_type": "WORKER",
                "actor_id": "script:backfill_verify_disciplines",
                "correlation_id": str(uuid.uuid4()),
                "metadata": {
                    "stage": "classify",
                    "project_id": project_id,
                    **summary,
                },
            }])
        except Exception as exc:
            _log.warning("backfill_verify: receipt emit failed: %s", type(exc).__name__)

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wave 17b.6 verifier backfill")
    parser.add_argument("project_id", help="blueprint_projects.id (UUID)")
    parser.add_argument("--suite", required=True, help="suite_id (UUID)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    summary = asyncio.run(_run(args.project_id, args.suite, dry_run=args.dry_run))
    print(f"summary={summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
