"""Backfill Wave 11g-v2 SEE motifs for an existing project.

For the target project:
  1. Open the project's source PDF (preferred from local --source-pdf, else
     download from the ``blueprint-raw-pdfs`` storage bucket).
  2. Run :func:`wave_11g2_see.run_wave_11g2_see_for_project` end-to-end.
  3. Before re-inserting, idempotently clear existing wave_11g2-tagged rows
     (``bbox->>'name_source' = 'wl_motif'``) for this project's sheets so
     re-running yields a clean state. Legacy VGSE rows are untouched.

Law compliance:
  Law #2 — emits ``drew.see.11g2.backfill`` receipt at completion.
  Law #3 — fails closed: missing --suite aborts before any write.
  Law #6 — every SELECT / DELETE / INSERT scopes by suite_id.
  Law #9 — never logs raw OCR or primitive coordinates; only counts + hashes.

Usage:
    python -m scripts.backfill_wave_11g2_see <project_id> --suite <uuid> \\
        [--source-pdf path/to/raw.pdf] [--dry-run] [--max-primitives N]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Any

_log = logging.getLogger("backfill_wave_11g2_see")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def _load_sheets(project_id: str, suite_id: str) -> list[dict[str, Any]]:
    from aspire_orchestrator.services.supabase_client import supabase_select
    rows = await supabase_select(
        "blueprint_sheets",
        filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    return rows or []


async def _clear_existing_wl_rows(
    suite_id: str, sheet_ids: list[str], dry_run: bool,
) -> int:
    """Delete existing wave_11g2-tagged rows. Returns count deleted."""
    if not sheet_ids:
        return 0
    from aspire_orchestrator.services.supabase_client import (
        supabase_select,
        supabase_delete,
    )

    id_list = ",".join(sheet_ids)
    # Pull existing rows scoped by suite + sheet_ids — filter wl_motif client-side
    # to avoid PostgREST jsonb-operator quoting headaches.
    try:
        rows = await supabase_select(
            "blueprint_symbols",
            filters=f"sheet_id=in.({id_list})&suite_id=eq.{suite_id}",
        ) or []
    except Exception as exc:  # noqa: BLE001
        _log.warning("backfill: existing-row scan failed (%s)", type(exc).__name__)
        return 0

    wl_rows = [
        r for r in rows
        if isinstance(r.get("bbox"), dict)
        and r["bbox"].get("name_source") == "wl_motif"
    ]
    if not wl_rows:
        return 0
    if dry_run:
        _log.info("backfill[DRY]: would delete %d existing wl_motif rows", len(wl_rows))
        return len(wl_rows)

    # Delete one-by-one so we never touch non-wl rows. Could be parallelized,
    # but backfill is rare and predictability beats speed here.
    deleted = 0
    for r in wl_rows:
        try:
            await supabase_delete(
                "blueprint_symbols",
                f"id=eq.{r['id']}&suite_id=eq.{suite_id}",
            )
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "backfill: delete failed id=%s (%s)",
                str(r['id'])[:8], type(exc).__name__,
            )
    return deleted


async def _download_raw_pdf(suite_id: str, project_id: str) -> bytes | None:
    """Download source PDF from the blueprint-raw-pdfs bucket."""
    import httpx
    from aspire_orchestrator.config.settings import settings
    if not settings.supabase_url or not settings.supabase_service_role_key:
        _log.warning("backfill: supabase settings missing — cannot download raw pdf")
        return None
    base = settings.supabase_url.rstrip("/")
    url = f"{base}/storage/v1/object/blueprint-raw-pdfs/{suite_id}/{project_id}/raw.pdf"
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            _log.warning("backfill: raw pdf download status=%d", resp.status_code)
            return None
        return resp.content
    except Exception as exc:  # noqa: BLE001
        _log.warning("backfill: raw pdf download error (%s)", type(exc).__name__)
        return None


async def _backfill_one_project(
    project_id: str,
    suite_id: str,
    *,
    dry_run: bool,
    correlation_id: str,
    source_pdf_path: str | None = None,
    max_primitives: int | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "project_id": project_id,
        "suite_id": suite_id,
        "dry_run": dry_run,
        "deleted_existing_wl_rows": 0,
        "pipeline": {},
    }

    sheets = await _load_sheets(project_id, suite_id)
    summary["sheets_total"] = len(sheets)
    if not sheets:
        _log.error("backfill: no sheets found for project=%s — aborting (fail-closed)", project_id[:8])
        return summary

    sheet_ids = [str(s["id"]) for s in sheets if s.get("id")]
    sheet_ids_by_page: dict[int, str] = {}
    discipline_by_page: dict[int, str | None] = {}
    office_id: str | None = None
    for s in sheets:
        try:
            pn = int(s.get("sheet_number") or 0)
        except (TypeError, ValueError):
            continue
        if pn >= 1:
            sheet_ids_by_page[pn] = str(s["id"])
            discipline_by_page[pn] = s.get("discipline")
            if office_id is None:
                office_id = s.get("office_id")

    # Source PDF
    pdf_bytes: bytes | None = None
    if source_pdf_path:
        try:
            with open(source_pdf_path, "rb") as f:
                pdf_bytes = f.read()
            _log.info(
                "backfill: loaded source pdf local=%s bytes=%d",
                source_pdf_path, len(pdf_bytes),
            )
        except OSError as exc:
            _log.warning("backfill: local pdf read failed (%s)", exc)
    if pdf_bytes is None:
        pdf_bytes = await _download_raw_pdf(suite_id, project_id)
    if pdf_bytes is None:
        _log.error("backfill: no source PDF reachable — fail-closed")
        return summary
    summary["pdf_bytes"] = len(pdf_bytes)

    # Idempotent clear of existing wl_motif rows.
    deleted = await _clear_existing_wl_rows(suite_id, sheet_ids, dry_run=dry_run)
    summary["deleted_existing_wl_rows"] = deleted

    if dry_run:
        _log.info(
            "backfill[DRY]: would run pipeline project=%s sheets=%d pages=%d",
            project_id[:8], len(sheets), len(sheet_ids_by_page),
        )
        summary["pipeline"] = {"dry_run": True}
        return summary

    if max_primitives is not None:
        os.environ["ASPIRE_11G2_MAX_PRIMITIVES"] = str(max_primitives)
        _log.info("backfill: ASPIRE_11G2_MAX_PRIMITIVES=%d", max_primitives)

    # Re-import after env override so module-level constants pick it up.
    import importlib
    from aspire_orchestrator.services.blueprint import wave_11g2_see
    importlib.reload(wave_11g2_see)

    pipeline_summary = await wave_11g2_see.run_wave_11g2_see_for_project(
        project_id=project_id,
        suite_id=suite_id,
        office_id=office_id,
        pdf_bytes=pdf_bytes,
        sheet_ids_by_page=sheet_ids_by_page,
        correlation_id=correlation_id,
        discipline_by_page=discipline_by_page,
    )
    summary["pipeline"] = pipeline_summary

    # Backfill receipt (Law #2).
    try:
        from aspire_orchestrator.services.receipt_store import store_receipts
        store_receipts([{
            "id": str(uuid_mod.uuid4()),
            "outcome": "success",
            "action_type": "drew.see.11g2.backfill",
            "receipt_type": "drew.see.11g2.backfill",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": "skillpack:drew-blueprint:wave_11g2_backfill",
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "metadata": {
                "project_id": project_id,
                "mode": "backfill",
                "deleted_existing_wl_rows": deleted,
                "rows_inserted": pipeline_summary.get("rows_inserted", 0),
                "total_motifs": pipeline_summary.get("total_motifs", 0),
                "total_instances": pipeline_summary.get("total_instances", 0),
                "elapsed_ms": pipeline_summary.get("elapsed_ms", 0),
            },
        }])
    except Exception:  # noqa: BLE001
        pass

    return summary


async def _main_async(
    project_id: str,
    suite_id: str,
    dry_run: bool,
    source_pdf: str | None,
    max_primitives: int | None,
) -> int:
    correlation_id = f"backfill-11g2-{uuid_mod.uuid4().hex[:8]}"
    _log.info(
        "backfill start project=%s suite=%s dry_run=%s corr=%s source_pdf=%s",
        project_id[:8], suite_id[:8], dry_run, correlation_id,
        source_pdf or "(auto from storage)",
    )
    summary = await _backfill_one_project(
        project_id, suite_id,
        dry_run=dry_run,
        correlation_id=correlation_id,
        source_pdf_path=source_pdf,
        max_primitives=max_primitives,
    )
    _log.info("backfill complete: %s", summary)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill Wave 11g-v2 SEE motifs for an existing project.",
    )
    parser.add_argument("project_id", help="Blueprint project UUID")
    parser.add_argument(
        "--suite", default=os.getenv("ASPIRE_BACKFILL_SUITE_ID", ""),
        help="Suite UUID (or set ASPIRE_BACKFILL_SUITE_ID).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--source-pdf", default=None,
        help="Optional local path to the source PDF. When omitted, fetches "
             "from the blueprint-raw-pdfs storage bucket.",
    )
    parser.add_argument(
        "--max-primitives", type=int, default=None,
        help="Override ASPIRE_11G2_MAX_PRIMITIVES for this run only.",
    )
    args = parser.parse_args()

    if not args.suite:
        _log.error("--suite (or ASPIRE_BACKFILL_SUITE_ID) is required (Law #6).")
        return 2

    return asyncio.run(_main_async(
        args.project_id, args.suite,
        args.dry_run, args.source_pdf, args.max_primitives,
    ))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
