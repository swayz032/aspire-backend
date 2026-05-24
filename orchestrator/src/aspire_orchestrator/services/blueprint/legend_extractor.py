"""Wave 20.6.1a — Per-discipline legend table extractor.

Reads ``blueprint_sheet_intelligence`` rows where ``regions['legend']`` is
non-empty, resolves each legend region to its backing Azure table, parses
table rows into structured symbol definitions, and upserts to
``blueprint_project_legends`` (created in migration 20.6.1b).

The legend region schema written by universal_sheet_extractor is:
    {bbox: [...], table_index: int}
The referenced table entry (tables[table_index]) has:
    {bbox: [...], headers: [...], rows: [[cell, ...], ...], region_type: str}

Parsing convention (deterministic, no LLM):
  - Column 0  → symbol_id   (e.g. "QR", "AHU-1", "001")
  - Column 1  → label       (e.g. "QR Pendant V2708", "MOTHER'S ROOM")
  - Column 2+ → specification text, columns joined with space

Rows with an empty or whitespace-only symbol_id (col 0) are skipped — these
are typically header repetitions or blank separators that Azure emits.

vector_signature starts NULL — Stage 3 (Symbol Family Embeddings) populates it.
representation_bbox carries the table-cell bbox from the legend region.

Law compliance:
  #1  Returns data; never decides or retries.
  #2  Emits blueprint.legend_extraction.summary receipt on completion.
  #3  Fail closed: missing suite_id raises immediately.
  #6  All DB reads + writes scoped by suite_id. Cross-tenant returns empty.
  #9  Never logs raw symbol labels or specification text; only counts and IDs.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.receipt_store import store_receipts
from aspire_orchestrator.services.supabase_client import (
    supabase_insert_batch_ignore_conflicts,
    supabase_select,
)

_log = logging.getLogger(__name__)

ACTOR = "service:legend-extractor"

# Discipline codes derived from sheet_number prefix (see §2.7 plan v7).
# Priority: use blueprint_sheets.discipline when non-null; fall back to
# sheet_number prefix regex when null.
_PREFIX_TO_DISCIPLINE: dict[str, str] = {
    "FP": "fire_protection",
    "E":  "electrical",
    "M":  "mechanical",
    "P":  "plumbing",
    "A":  "architectural",
    "S":  "structural",
    "C":  "civil",
}

# Order matters — longer prefixes must be checked first (FP before F).
_PREFIX_ORDER: list[str] = ["FP", "E", "M", "P", "A", "S", "C"]

# Canonical discipline names for the DB enum-like field.
_ENUM_TO_DISCIPLINE: dict[str, str] = {
    "A":   "architectural",
    "S":   "structural",
    "M":   "mechanical",
    "E":   "electrical",
    "P":   "plumbing",
    "FP":  "fire_protection",
    "C":   "civil",
    "L":   "civil",           # Landscape → civil family
}


def _detect_discipline(
    sheet_number: str | None,
    discipline_enum: str | None,
) -> str:
    """Return a stable discipline string for legend rows on this sheet.

    Priority:
      1. blueprint_sheets.discipline (set by Drew classify stage) when non-null.
      2. sheet_number prefix regex against _PREFIX_ORDER.
      3. "unknown" as last resort.
    """
    if discipline_enum:
        canonical = _ENUM_TO_DISCIPLINE.get(discipline_enum.upper())
        if canonical:
            return canonical

    if sheet_number:
        sn = sheet_number.strip().upper()
        for prefix in _PREFIX_ORDER:
            if sn.startswith(prefix):
                return _PREFIX_TO_DISCIPLINE[prefix]

    return "unknown"


def _parse_legend_table(
    table: dict[str, Any],
) -> list[dict[str, Any]]:
    """Parse an Azure table dict into a list of symbol definition dicts.

    Each returned dict has:
        symbol_id, label, specification, representation_bbox

    Rows with empty symbol_id (col 0) are silently skipped.
    """
    rows: list[list[Any]] = table.get("rows") or []
    table_bbox: Any = table.get("bbox")

    symbols: list[dict[str, Any]] = []

    for row in rows:
        if not row:
            continue
        # col 0 → symbol_id
        symbol_id = str(row[0]).strip() if row[0] is not None else ""
        if not symbol_id:
            continue

        # col 1 → label
        label: str | None = None
        if len(row) > 1 and row[1] is not None:
            raw_label = str(row[1]).strip()
            label = raw_label if raw_label else None

        # col 2+ → specification (joined, stripped)
        spec_parts: list[str] = []
        for cell in row[2:]:
            if cell is not None:
                s = str(cell).strip()
                if s:
                    spec_parts.append(s)
        specification: str | None = " ".join(spec_parts) if spec_parts else None

        symbols.append(
            {
                "symbol_id": symbol_id,
                "label": label,
                "specification": specification,
                "representation_bbox": table_bbox,
            }
        )

    return symbols


async def extract_project_legends(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    correlation_id: str,
) -> dict[str, Any]:
    """Walk blueprint_sheet_intelligence legend regions for a project.

    For each legend region table:
      1. Identify discipline from source sheet (FP/E/M/P/A/S/C)
      2. Parse table rows into {symbol_id, label, specification, vector_signature_placeholder}
      3. Insert into blueprint_project_legends (upsert on (project_id, discipline, symbol_id))

    Returns summary: {legends_processed, symbols_extracted, disciplines_covered, total_azure_ms}.
    Emits blueprint.legend_extraction.summary receipt (Law #2). Suite-bound (Law #6).
    """
    if not suite_id:
        raise ValueError(
            "extract_project_legends requires non-empty suite_id (Law #6)"
        )
    if not project_id:
        raise ValueError("extract_project_legends requires non-empty project_id")

    _log.info(
        "legend_extractor.start: project=%s suite=%s corr=%s",
        project_id[:8],
        suite_id[:8],
        correlation_id[:8],
    )

    t0 = time.monotonic()

    # ------------------------------------------------------------------
    # 1. Load all blueprint_sheet_intelligence rows for the project.
    # ------------------------------------------------------------------
    intel_rows: list[dict[str, Any]] = await supabase_select(
        "blueprint_sheet_intelligence",
        filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )

    # Filter to only sheets with non-empty legend regions up-front.
    rows_with_legends = [
        r for r in (intel_rows or [])
        if r.get("regions") and r["regions"].get("legend")
    ]

    if not rows_with_legends:
        _log.info(
            "legend_extractor.no_legends: project=%s suite=%s — 0 sheets with legend regions",
            project_id[:8],
            suite_id[:8],
        )
        summary: dict[str, Any] = {
            "legends_processed": 0,
            "symbols_extracted": 0,
            "disciplines_covered": [],
            "total_azure_ms": 0,
        }
        _emit_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            project_id=project_id,
            summary=summary,
        )
        return summary

    # ------------------------------------------------------------------
    # 2. Load blueprint_sheets for discipline + sheet_number resolution.
    #    Keyed by sheet_id → {sheet_number, discipline}.
    # ------------------------------------------------------------------
    sheet_ids: list[str] = [
        str(r.get("sheet_id") or "") for r in rows_with_legends if r.get("sheet_id")
    ]

    sheets_meta: dict[str, dict[str, Any]] = {}
    if sheet_ids:
        # Batch select — filter by project + suite (Law #6).
        sheet_rows: list[dict[str, Any]] = await supabase_select(
            "blueprint_sheets",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        )
        for sr in (sheet_rows or []):
            sid = str(sr.get("id") or "")
            if sid:
                sheets_meta[sid] = {
                    "sheet_number": sr.get("sheet_number"),
                    "discipline": sr.get("discipline"),
                }

    # ------------------------------------------------------------------
    # 3. Walk every legend region, parse table rows, collect upsert rows.
    # ------------------------------------------------------------------
    upsert_rows: list[dict[str, Any]] = []
    legends_processed = 0
    disciplines_seen: set[str] = set()

    for intel_row in rows_with_legends:
        sheet_id = str(intel_row.get("sheet_id") or "")
        tables: list[dict[str, Any]] = intel_row.get("tables") or []
        legend_regions: list[dict[str, Any]] = (
            intel_row.get("regions") or {}
        ).get("legend") or []

        meta = sheets_meta.get(sheet_id, {})
        discipline = _detect_discipline(
            sheet_number=meta.get("sheet_number"),
            discipline_enum=meta.get("discipline"),
        )
        disciplines_seen.add(discipline)

        for region_idx, region in enumerate(legend_regions):
            table_index: int | None = region.get("table_index")
            if table_index is None:
                _log.debug(
                    "legend_extractor: region %d on sheet=%s has no table_index — skipping",
                    region_idx,
                    sheet_id[:8],
                )
                continue

            # Bounds-check table_index against the tables list.
            if table_index < 0 or table_index >= len(tables):
                _log.warning(
                    "legend_extractor: table_index=%d out of range (tables=%d) sheet=%s — skipping",
                    table_index,
                    len(tables),
                    sheet_id[:8],
                )
                continue

            table = tables[table_index]
            symbols = _parse_legend_table(table)

            legends_processed += 1

            for sym in symbols:
                upsert_rows.append(
                    {
                        "suite_id": suite_id,
                        "office_id": office_id,
                        "project_id": project_id,
                        "discipline": discipline,
                        "symbol_id": sym["symbol_id"],
                        "label": sym["label"],
                        "specification": sym["specification"],
                        "vector_signature": None,
                        "representation_bbox": sym["representation_bbox"],
                        "source_sheet_id": sheet_id if sheet_id else None,
                        "source_region_index": region_idx,
                    }
                )

    # ------------------------------------------------------------------
    # 4. Batch upsert to blueprint_project_legends (idempotent).
    # ------------------------------------------------------------------
    symbols_inserted = 0
    symbols_duplicate = 0

    if upsert_rows:
        inserted_rows, dup_count = await supabase_insert_batch_ignore_conflicts(
            "blueprint_project_legends",
            upsert_rows,
            conflict_target="project_id,discipline,symbol_id",
        )
        symbols_inserted = len(inserted_rows)
        symbols_duplicate = dup_count

    total_azure_ms = int((time.monotonic() - t0) * 1000)

    summary = {
        "legends_processed": legends_processed,
        "symbols_extracted": len(upsert_rows),
        "symbols_inserted": symbols_inserted,
        "symbols_duplicate": symbols_duplicate,
        "disciplines_covered": sorted(disciplines_seen),
        "total_azure_ms": total_azure_ms,
    }

    _log.info(
        "legend_extractor.done: project=%s suite=%s legends=%d symbols=%d disciplines=%s ms=%d corr=%s",
        project_id[:8],
        suite_id[:8],
        legends_processed,
        len(upsert_rows),
        sorted(disciplines_seen),
        total_azure_ms,
        correlation_id[:8],
    )

    _emit_receipt(
        correlation_id=correlation_id,
        suite_id=suite_id,
        project_id=project_id,
        summary=summary,
    )

    return summary


def _emit_receipt(
    *,
    correlation_id: str,
    suite_id: str,
    project_id: str,
    summary: dict[str, Any],
) -> None:
    """Emit a Law #2 receipt for the legend extraction run. Best-effort (never blocks)."""
    try:
        store_receipts(
            [
                {
                    "id": str(uuid.uuid4()),
                    "outcome": "success",
                    "action_type": "blueprint.legend_extraction.summary",
                    "receipt_type": "blueprint.legend_extraction.summary",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "risk_tier": "GREEN",
                    "actor_type": "WORKER",
                    "actor_id": ACTOR,
                    "correlation_id": correlation_id,
                    "suite_id": suite_id,
                    "metadata": {
                        "project_id": project_id,
                        "legends_processed": summary.get("legends_processed", 0),
                        "symbols_extracted": summary.get("symbols_extracted", 0),
                        "symbols_inserted": summary.get("symbols_inserted", 0),
                        "symbols_duplicate": summary.get("symbols_duplicate", 0),
                        "disciplines_covered": summary.get("disciplines_covered", []),
                        "total_azure_ms": summary.get("total_azure_ms", 0),
                    },
                }
            ]
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "legend_extractor: receipt emit failed (%s) corr=%s",
            type(exc).__name__,
            correlation_id[:8],
        )


__all__ = ["extract_project_legends"]
