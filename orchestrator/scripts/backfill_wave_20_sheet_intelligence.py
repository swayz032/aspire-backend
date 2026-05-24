"""Backfill script — Wave 20 Universal Sheet Intelligence.

Run via Railway for a specific project:

    railway run python scripts/backfill_wave_20_sheet_intelligence.py \
        --project-id <uuid>

What it does:
  1. Loads all active blueprint_sheets for the given project.
  2. Downloads the cached sheet PNG from Supabase Storage for each sheet.
  3. Calls extract_sheet_intelligence (Azure prebuilt-layout + region classifier).
  4. Upserts results into blueprint_sheet_intelligence (skips rows that already exist).
  5. Reports: sheets processed / skipped / failed.

Requirements:
  - SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY in env (via railway run or .env).
  - AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT + AZURE_DOCUMENT_INTELLIGENCE_KEY in env.
  - The project must have completed INGEST (sheet PNGs must exist in Storage).

Law compliance:
  #2  — Emits per-sheet provider.azure.sheet_layout receipts (via extract_sheet_intelligence).
  #3  — Fails closed on missing env vars.
  #6  — All queries include suite_id from the project row (tenant isolation).
  #9  — No raw image bytes logged; logs only sheet_id + size_bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
_log = logging.getLogger("backfill_wave_20")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill blueprint_sheet_intelligence for an existing project."
    )
    parser.add_argument(
        "--project-id",
        required=True,
        help="UUID of the blueprint project to backfill.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("ASPIRE_DREW_OCR_CONCURRENCY", "15")),
        help="Max parallel Azure calls (default: ASPIRE_DREW_OCR_CONCURRENCY or 15).",
    )
    return parser.parse_args()


async def _run(project_id: str, concurrency: int) -> int:
    """Execute the backfill. Returns exit code (0=ok, 1=error)."""
    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_select,
        supabase_upsert,
    )
    from aspire_orchestrator.services.blueprint.sheet_image_storage import (
        download_sheet_image,
    )
    from aspire_orchestrator.services.blueprint.universal_sheet_extractor import (
        extract_sheet_intelligence,
    )
    from aspire_orchestrator.providers.base_client import ProviderError

    correlation_id = str(uuid.uuid4())
    _log.info(
        "backfill_wave_20: start project=%s concurrency=%d corr=%s",
        project_id[:8], concurrency, correlation_id[:8],
    )

    # Load the project row to get suite_id (required for Law #6 isolation)
    try:
        project_rows = await supabase_select(
            "blueprint_projects",
            filters=f"id=eq.{project_id}",
        )
    except SupabaseClientError as exc:
        _log.error("backfill_wave_20: failed to load project: %s", exc)
        return 1

    if not project_rows:
        _log.error("backfill_wave_20: project %s not found", project_id)
        return 1

    project = project_rows[0]
    suite_id: str = str(project.get("suite_id") or "")
    office_id: str | None = str(project["office_id"]) if project.get("office_id") else None

    if not suite_id:
        _log.error("backfill_wave_20: project %s has no suite_id — cannot proceed", project_id)
        return 1

    _log.info("backfill_wave_20: project=%s suite=%s", project_id[:8], suite_id[:8])

    # Load all active sheets
    try:
        all_sheets = await supabase_select(
            "blueprint_sheets",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            order_by="created_at.asc",
        )
    except SupabaseClientError as exc:
        _log.error("backfill_wave_20: failed to load sheets: %s", exc)
        return 1

    active_sheets = [s for s in (all_sheets or []) if not s.get("supersedes_id")]
    sheets_total = len(active_sheets)

    if sheets_total == 0:
        _log.warning("backfill_wave_20: no active sheets found for project=%s", project_id[:8])
        return 0

    _log.info("backfill_wave_20: found %d active sheets", sheets_total)

    # Load existing blueprint_sheet_intelligence rows to determine what already exists
    try:
        existing_rows = await supabase_select(
            "blueprint_sheet_intelligence",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        )
    except SupabaseClientError as exc:
        _log.warning(
            "backfill_wave_20: could not load existing intel rows (will re-process all): %s", exc
        )
        existing_rows = []

    already_done: set[str] = {str(r.get("sheet_id") or "") for r in (existing_rows or [])}
    _log.info(
        "backfill_wave_20: %d sheets already have intel rows (will skip)",
        len(already_done),
    )

    sem = asyncio.Semaphore(concurrency)

    sheets_processed = 0
    sheets_skipped = 0
    sheets_failed = 0

    async def _process_one(sheet: dict[str, Any]) -> None:
        nonlocal sheets_processed, sheets_skipped, sheets_failed

        sheet_id = str(sheet.get("id") or "")
        if not sheet_id:
            sheets_failed += 1
            return

        # Idempotent: skip if already exists
        if sheet_id in already_done:
            _log.info("backfill_wave_20: skip sheet=%s (already exists)", sheet_id[:8])
            sheets_skipped += 1
            return

        async with sem:
            # Download the cached PNG from Storage
            png_bytes = await download_sheet_image(
                suite_id=suite_id,
                project_id=project_id,
                sheet_id=sheet_id,
                correlation_id=correlation_id,
            )
            if not png_bytes:
                _log.warning(
                    "backfill_wave_20: no PNG for sheet=%s — skipping", sheet_id[:8]
                )
                sheets_failed += 1
                return

            _log.info(
                "backfill_wave_20: processing sheet=%s size_bytes=%d",
                sheet_id[:8], len(png_bytes),
            )

            try:
                row = await extract_sheet_intelligence(
                    suite_id=suite_id,
                    office_id=office_id,
                    project_id=project_id,
                    sheet_id=sheet_id,
                    sheet_png_bytes=png_bytes,
                    correlation_id=correlation_id,
                )
            except ProviderError as exc:
                _log.warning(
                    "backfill_wave_20: Azure failed sheet=%s: %s", sheet_id[:8], exc
                )
                sheets_failed += 1
                return
            except Exception as exc:
                _log.warning(
                    "backfill_wave_20: unexpected error sheet=%s: %s",
                    sheet_id[:8], type(exc).__name__,
                )
                sheets_failed += 1
                return

            # Upsert (idempotent on project_id+sheet_id unique constraint)
            try:
                await supabase_upsert(
                    "blueprint_sheet_intelligence",
                    row,
                    on_conflict="project_id,sheet_id",
                )
                _log.info(
                    "backfill_wave_20: inserted sheet=%s azure_ms=%d",
                    sheet_id[:8], row.get("azure_call_ms", 0),
                )
                sheets_processed += 1
            except SupabaseClientError as exc:
                _log.warning(
                    "backfill_wave_20: upsert failed sheet=%s: %s", sheet_id[:8], exc
                )
                sheets_failed += 1

    tasks = [_process_one(sheet) for sheet in active_sheets]
    await asyncio.gather(*tasks, return_exceptions=True)

    _log.info(
        "backfill_wave_20: DONE project=%s "
        "total=%d processed=%d skipped=%d failed=%d",
        project_id[:8],
        sheets_total,
        sheets_processed,
        sheets_skipped,
        sheets_failed,
    )

    print(
        f"\nBackfill complete — project {project_id}\n"
        f"  Total sheets : {sheets_total}\n"
        f"  Processed    : {sheets_processed}\n"
        f"  Skipped      : {sheets_skipped} (already had intel rows)\n"
        f"  Failed       : {sheets_failed}\n"
    )

    return 0 if sheets_failed == 0 else 1


def main() -> None:
    args = _parse_args()

    # Validate project_id format
    try:
        uuid.UUID(args.project_id)
    except ValueError:
        _log.error("backfill_wave_20: --project-id must be a valid UUID")
        sys.exit(1)

    exit_code = asyncio.run(_run(project_id=args.project_id, concurrency=args.concurrency))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
