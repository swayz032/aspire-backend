"""Backfill legend → symbol naming for an existing project.

Wave 17b.4 retroactive runner.

For the target project:
  1. Open the project's source PDF (preferred from local --source-pdf, else
     downloaded from blueprint-raw-pdfs storage bucket).
  2. For every page, run ``extract_legend_signatures_with_meta`` to recover
     ``(signature_hash -> canonical_name)`` pairs from any legend / schedule
     region present on the sheet.
  3. UPSERT every pair to ``blueprint_symbol_memory`` with
     ``source='legend'``, ``confidence=0.95``.
  4. For every existing row in ``blueprint_symbols`` whose
     ``bbox.signature_hash`` matches a legend-derived signature AND whose
     current class is opaque/fallback, UPDATE the row with the canonical
     name and ``bbox.name_source='legend'``.

Idempotent: re-running yields the same final state because memory upserts
dedupe by signature_hash and row updates only touch opaque names (real
confirmation-sourced names are left alone).

Law compliance:
  Law #2 — emits one ``drew.see.legend_extracted`` receipt per run.
  Law #3 — fails closed: missing --suite aborts before any write.
  Law #6 — every UPDATE / SELECT scopes by suite_id.
  Law #9 — never logs raw OCR text; logs counts only.

Usage:
    python -m scripts.backfill_legend_extraction <project_id> --suite <uuid> [--dry-run]
                                                 [--source-pdf path/to/raw.pdf]
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

_log = logging.getLogger("backfill_legend_extraction")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# Classes we consider "needs naming" — opaque fallback bands and unknown_motif_*.
_OPAQUE_PREFIXES: tuple[str, ...] = (
    "unknown_motif_",
    "sig:",
    "class_",
    "unmapped:",
)
_FALLBACK_BANDS: frozenset[str] = frozenset({
    f"{disc}_symbol_{band}"
    for disc in (
        "architectural", "structural", "mechanical", "electrical", "plumbing",
        "fire_protection", "civil", "landscape", "general",
    )
    for band in ("high", "mid", "low")
})


def _is_renamable(class_name: str) -> bool:
    if not class_name:
        return True
    if class_name.startswith(_OPAQUE_PREFIXES):
        return True
    if class_name in _FALLBACK_BANDS:
        return True
    return False


async def _load_sheets(project_id: str, suite_id: str) -> list[dict[str, Any]]:
    from aspire_orchestrator.services.supabase_client import supabase_select
    rows = await supabase_select(
        "blueprint_sheets",
        filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    return rows or []


async def _load_symbols(sheet_id: str, suite_id: str) -> list[dict[str, Any]]:
    from aspire_orchestrator.services.supabase_client import supabase_select
    rows = await supabase_select(
        "blueprint_symbols",
        filters=f"sheet_id=eq.{sheet_id}&suite_id=eq.{suite_id}",
    )
    return rows or []


async def _download_raw_pdf(suite_id: str, project_id: str) -> bytes | None:
    import httpx
    from aspire_orchestrator.config.settings import settings
    if not settings.supabase_url or not settings.supabase_service_role_key:
        return None
    base = settings.supabase_url.rstrip("/")
    url = f"{base}/storage/v1/object/blueprint-raw-pdfs/{suite_id}/{project_id}/raw.pdf"
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
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
) -> dict[str, int]:
    from aspire_orchestrator.services.blueprint import symbol_memory
    from aspire_orchestrator.services.blueprint.vgse import (
        extract_legend_signatures_with_meta,
    )
    from aspire_orchestrator.services.supabase_client import supabase_update

    summary: dict[str, int] = {
        "sheets_total": 0,
        "pages_scanned": 0,
        "legend_regions_found": 0,
        "legend_pages_with_region": 0,
        "legend_pairs_extracted": 0,
        "memory_upserts": 0,
        "memory_upserts_failed": 0,
        "symbols_total": 0,
        "symbols_renamable": 0,
        "symbols_promoted": 0,
        "symbol_updates_failed": 0,
    }

    sheets = await _load_sheets(project_id, suite_id)
    summary["sheets_total"] = len(sheets)
    _log.info("backfill: project=%s sheets=%d", project_id[:8], len(sheets))

    # Open source PDF
    try:
        import fitz  # PyMuPDF
    except ImportError:
        _log.error("PyMuPDF required for legend backfill")
        return summary

    pdf_doc = None
    if source_pdf_path:
        try:
            pdf_doc = fitz.open(source_pdf_path)
            _log.info(
                "backfill: opened source pdf local=%s pages=%d",
                source_pdf_path, len(pdf_doc),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("backfill: local pdf open failed (%s)", type(exc).__name__)
            pdf_doc = None
    if pdf_doc is None:
        raw = await _download_raw_pdf(suite_id, project_id)
        if raw:
            try:
                pdf_doc = fitz.open(stream=raw, filetype="pdf")
                _log.info("backfill: opened source pdf from storage pages=%d", len(pdf_doc))
            except Exception as exc:  # noqa: BLE001
                _log.warning("backfill: storage pdf open failed (%s)", type(exc).__name__)

    if pdf_doc is None:
        _log.error("backfill: no source PDF reachable — cannot extract legends (fail-closed)")
        return summary

    # Map page_number → sheet metadata for discipline-aware lookup
    sheet_by_page: dict[int, dict[str, Any]] = {}
    for sh in sheets:
        try:
            pn = int(sh.get("sheet_number") or 0)
        except (TypeError, ValueError):
            continue
        if pn >= 1:
            sheet_by_page[pn] = sh

    # ── Pass 1: build project-wide legend map ──────────────────────────
    project_legend_map: dict[str, str] = {}
    legend_office_id: str | None = None
    for page_idx in range(len(pdf_doc)):
        page_number = page_idx + 1
        summary["pages_scanned"] += 1
        page = pdf_doc[page_idx]
        sheet = sheet_by_page.get(page_number)
        discipline = (sheet or {}).get("discipline") if sheet else None
        if sheet and not legend_office_id:
            legend_office_id = sheet.get("office_id")
        try:
            meta = extract_legend_signatures_with_meta(page, discipline=discipline)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "backfill: legend extract failed page=%d (%s)",
                page_number, type(exc).__name__,
            )
            continue
        summary["legend_regions_found"] += int(meta.get("regions_found", 0))
        if meta.get("regions_found", 0):
            summary["legend_pages_with_region"] += 1
        summary["legend_pairs_extracted"] += int(meta.get("pairs_extracted", 0))
        for sig, label in (meta.get("sig_to_label") or {}).items():
            if sig not in project_legend_map:
                project_legend_map[sig] = label

    _log.info(
        "backfill: pass1 complete pages=%d regions=%d pages_with_region=%d pairs=%d distinct_sigs=%d",
        summary["pages_scanned"],
        summary["legend_regions_found"],
        summary["legend_pages_with_region"],
        summary["legend_pairs_extracted"],
        len(project_legend_map),
    )

    # ── Pass 2: upsert into symbol_memory ──────────────────────────────
    for sig, label in project_legend_map.items():
        if dry_run:
            _log.info(
                "backfill[DRY]: memory upsert sig=%s label=%s source=legend",
                sig[:8], label,
            )
            continue
        try:
            await symbol_memory.upsert_observation(
                suite_id=suite_id,
                office_id=legend_office_id,
                signature_hash=sig,
                canonical_name=label,
                category=None,
                source="legend",
                project_id=project_id,
                instances_observed=0,
                confidence=0.95,
            )
            summary["memory_upserts"] += 1
        except Exception as exc:  # noqa: BLE001
            summary["memory_upserts_failed"] += 1
            _log.warning(
                "backfill: memory upsert failed sig=%s err=%s",
                sig[:8], type(exc).__name__,
            )

    # ── Pass 3: promote existing blueprint_symbols rows ────────────────
    for sheet in sheets:
        sheet_id = str(sheet["id"])
        sheet_symbols = await _load_symbols(sheet_id, suite_id)
        summary["symbols_total"] += len(sheet_symbols)
        for srow in sheet_symbols:
            class_name = srow.get("class") or ""
            bbox = srow.get("bbox") or {}
            sig = bbox.get("signature_hash")
            if not sig:
                continue
            if not _is_renamable(class_name):
                continue
            summary["symbols_renamable"] += 1
            new_label = project_legend_map.get(sig)
            if not new_label:
                continue

            new_bbox = dict(bbox)
            new_bbox["name_source"] = "legend"
            new_bbox["legend_confidence"] = 0.95

            if dry_run:
                _log.info(
                    "backfill[DRY]: sheet=%s symbol=%s %s -> %s (legend)",
                    sheet_id[:8], str(srow["id"])[:8], class_name, new_label,
                )
                continue

            try:
                await supabase_update(
                    "blueprint_symbols",
                    f"id=eq.{srow['id']}&suite_id=eq.{suite_id}",
                    {"class": new_label, "confidence": 0.95, "bbox": new_bbox},
                )
                summary["symbols_promoted"] += 1
            except Exception as exc:  # noqa: BLE001
                summary["symbol_updates_failed"] += 1
                _log.warning(
                    "backfill: symbol update failed id=%s err=%s",
                    str(srow["id"])[:8], type(exc).__name__,
                )

    # ── Receipt (Law #2) ──────────────────────────────────────────────
    if not dry_run:
        try:
            from aspire_orchestrator.services.receipt_store import store_receipts
            store_receipts([{
                "id": str(uuid_mod.uuid4()),
                "outcome": "success",
                "action_type": "drew.see.legend_extracted",
                "receipt_type": "drew.see.legend_extracted",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "risk_tier": "GREEN",
                "actor_type": "WORKER",
                "actor_id": "skillpack:drew-blueprint:backfill",
                "correlation_id": correlation_id,
                "suite_id": suite_id,
                "metadata": {
                    "project_id": project_id,
                    "mode": "backfill",
                    **summary,
                },
            }])
        except Exception:  # noqa: BLE001
            pass

    try:
        pdf_doc.close()
    except Exception:  # noqa: BLE001
        pass

    return summary


async def _main_async(
    project_id: str, suite_id: str, dry_run: bool, source_pdf: str | None,
) -> int:
    correlation_id = f"backfill-legend-{uuid_mod.uuid4().hex[:8]}"
    _log.info(
        "backfill start project=%s suite=%s dry_run=%s corr=%s source_pdf=%s",
        project_id[:8], suite_id[:8], dry_run, correlation_id,
        source_pdf or "(auto from storage)",
    )
    summary = await _backfill_one_project(
        project_id, suite_id, dry_run=dry_run, correlation_id=correlation_id,
        source_pdf_path=source_pdf,
    )
    _log.info("backfill complete: %s", summary)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill legend → symbol naming.")
    parser.add_argument("project_id", help="Blueprint project UUID")
    parser.add_argument(
        "--suite", default=os.getenv("ASPIRE_BACKFILL_SUITE_ID", ""),
        help="Suite UUID (or set ASPIRE_BACKFILL_SUITE_ID).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--source-pdf", default=None,
        help="Optional local path to the source PDF. When omitted, attempts to "
             "fetch from blueprint-raw-pdfs storage bucket.",
    )
    args = parser.parse_args()

    if not args.suite:
        _log.error("--suite (or ASPIRE_BACKFILL_SUITE_ID) is required (Law #6).")
        return 2

    return asyncio.run(_main_async(args.project_id, args.suite, args.dry_run, args.source_pdf))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
