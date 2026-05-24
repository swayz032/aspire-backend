"""Universal Sheet Extractor — Wave 20a/20b: Azure layout cache + region classifier.

Wraps AzureDocIntelClient.analyze_layout and adds deterministic REGION CLASSIFICATION
on top. Region classification runs at INGEST TIME so downstream consumers (Wave 20.6
Construction Semantic Compiler, Wave 20.7 Symbol Truth Engine, REASON stage) can do
O(1) lookups by region type instead of re-running Azure per consumer.

Region types:
  legend   — symbol/mark/tag lookup tables; ground-truth dictionary for this project
  schedule — door/window/fixture/panel schedules with row numbering
  notes    — general notes paragraphs (Azure role or proximity annotation)
  plan     — large bbox plan views (>40% of sheet area)
  detail   — small dense annotation callouts (<15% of sheet area)

Law compliance:
  #2 — Emits provider.azure.sheet_layout receipt on success AND failure.
  #3 — Fail-closed: missing env, Azure error → denial receipt + raises.
  #6 — suite_id bound on every call; never mixes tenants.
  #9 — Never logs image_bytes content; logs only len() + correlation_id.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.providers.azure_doc_intel_client import get_azure_doc_intel_client
from aspire_orchestrator.providers.base_client import ProviderError
from aspire_orchestrator.services.receipt_store import store_receipts

logger = logging.getLogger(__name__)

ACTOR = "service:universal-sheet-extractor"
RECEIPT_VERSION = "1.0"

# Region detection thresholds
_LEGEND_HEADER_KEYWORDS: frozenset[str] = frozenset(
    {"symbol", "legend", "mark", "tag", "description", "abbrev", "abbreviation", "key"}
)
_SCHEDULE_HEADER_KEYWORDS: frozenset[str] = frozenset(
    {"type", "mark", "tag", "no.", "item", "no", "number", "id", "label"}
)
_SCHEDULE_TITLE_KEYWORDS: frozenset[str] = frozenset(
    {"schedule", "door schedule", "window schedule", "fixture schedule",
     "panel schedule", "sprinkler schedule", "plumbing schedule",
     "equipment schedule", "finish schedule", "hardware schedule"}
)
_NOTES_TITLE_KEYWORDS: frozenset[str] = frozenset(
    {"general notes", "notes", "keynotes", "general requirements", "specifications"}
)
_PLAN_AREA_THRESHOLD = 0.40   # bbox covering >40% of sheet area → plan view
_DETAIL_AREA_THRESHOLD = 0.15  # bbox <15% of sheet area AND dense → detail callout
_DETAIL_DENSE_ANNOTATION_MIN = 5  # min text blocks in bbox to be "dense"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def extract_sheet_intelligence(
    *,
    suite_id: str,
    office_id: str | None,
    project_id: str,
    sheet_id: str,
    sheet_png_bytes: bytes,
    correlation_id: str,
) -> dict[str, Any]:
    """Run Azure prebuilt-layout + region classifier on one sheet PNG.

    Returns a row ready for INSERT into blueprint_sheet_intelligence.
    Emits provider.azure.sheet_layout receipt (Law #2).
    Suite-bound (Law #6).

    Raises:
        ProviderError: if Azure call fails and fail-closed (Law #3).
    """
    logger.info(
        "universal_sheet_extractor: start suite=%s sheet=%s corr=%s size_bytes=%d",
        suite_id[:8],
        sheet_id[:8],
        correlation_id[:8],
        len(sheet_png_bytes),
    )

    t0 = time.monotonic()
    client = get_azure_doc_intel_client()

    try:
        response = await client.analyze_layout(
            sheet_png_bytes,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id or "",
        )
    except ProviderError as exc:
        _emit_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            sheet_id=sheet_id,
            project_id=project_id,
            status="failed",
            metadata={
                "reason_code": "AZURE_LAYOUT_FAILED",
                "error": str(exc),
                "sheet_id": sheet_id,
            },
        )
        raise

    azure_call_ms = int((time.monotonic() - t0) * 1000)

    if not response.success:
        _emit_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            sheet_id=sheet_id,
            project_id=project_id,
            status="failed",
            metadata={
                "reason_code": "AZURE_LAYOUT_FAILED",
                "error_code": response.error_code.value if response.error_code else "UNKNOWN",
                "error_message": response.error_message if hasattr(response, "error_message") else "",
                "sheet_id": sheet_id,
                "azure_call_ms": azure_call_ms,
            },
        )
        from aspire_orchestrator.providers.error_codes import InternalErrorCode as _EC
        raise ProviderError(
            code=response.error_code or _EC.SERVER_INTERNAL_ERROR,
            message=f"Azure layout failed for sheet {sheet_id}: {response.error_code}",
            provider_id="azure_doc_intel",
        )

    analyze_result: dict[str, Any] = response.body.get("analyzeResult") or {}

    tables = _extract_tables(analyze_result)
    paragraphs = _extract_paragraphs(analyze_result)
    key_value_pairs = _extract_key_value_pairs(analyze_result)
    page_count = _extract_page_count(analyze_result)
    regions = _classify_regions(tables=tables, paragraphs=paragraphs)

    row: dict[str, Any] = {
        "suite_id": suite_id,
        "office_id": office_id,
        "project_id": project_id,
        "sheet_id": sheet_id,
        "tables": tables,
        "paragraphs": paragraphs,
        "key_value_pairs": key_value_pairs,
        "page_count": page_count,
        "regions": regions,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "azure_model": "prebuilt-layout",
        "azure_call_ms": azure_call_ms,
    }

    _emit_receipt(
        correlation_id=correlation_id,
        suite_id=suite_id,
        sheet_id=sheet_id,
        project_id=project_id,
        status="ok",
        metadata={
            "azure_call_ms": azure_call_ms,
            "table_count": len(tables),
            "paragraph_count": len(paragraphs),
            "region_types": list(regions.keys()),
            "page_count": page_count,
        },
    )

    logger.info(
        "universal_sheet_extractor: done suite=%s sheet=%s tables=%d paragraphs=%d ms=%d",
        suite_id[:8],
        sheet_id[:8],
        len(tables),
        len(paragraphs),
        azure_call_ms,
    )

    return row


# ---------------------------------------------------------------------------
# Azure response parsing
# ---------------------------------------------------------------------------

def _extract_tables(analyze_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract tables from Azure analyzeResult as structured dicts."""
    raw_tables: list[dict[str, Any]] = analyze_result.get("tables", [])
    out: list[dict[str, Any]] = []

    for tbl in raw_tables:
        cells: list[dict[str, Any]] = tbl.get("cells", [])
        row_count: int = tbl.get("rowCount", 0)
        col_count: int = tbl.get("columnCount", 0)
        bounding_regions: list[dict[str, Any]] = tbl.get("boundingRegions", [])

        bbox = bounding_regions[0].get("polygon") if bounding_regions else None

        headers: list[str] = []
        rows: list[list[str]] = [[""] * col_count for _ in range(max(0, row_count - 1))]

        # Preserve per-cell bounding_regions so downstream consumers (legend_extractor)
        # can use individual symbol-cell bboxes instead of the shared outer table bbox.
        extracted_cells: list[dict[str, Any]] = []

        for cell in cells:
            row_idx: int = cell.get("rowIndex", 0)
            col_idx: int = cell.get("columnIndex", 0)
            content: str = cell.get("content", "").strip()
            kind: str = cell.get("kind", "")
            row_span: int = cell.get("rowSpan", 1)
            col_span: int = cell.get("columnSpan", 1)

            # Capture per-cell bounding_regions polygon for downstream bbox use.
            cell_bounding_regions: list[dict[str, Any]] = cell.get("boundingRegions", [])
            cell_bbox: list[float] | None = (
                cell_bounding_regions[0].get("polygon") if cell_bounding_regions else None
            )
            extracted_cells.append({
                "content": content,
                "row_index": row_idx,
                "column_index": col_idx,
                "row_span": row_span,
                "column_span": col_span,
                "bounding_regions": [
                    {
                        "page_number": br.get("pageNumber", 1),
                        "polygon": br.get("polygon"),
                    }
                    for br in cell_bounding_regions
                ],
            })

            if row_idx == 0 or kind == "columnHeader":
                while len(headers) <= col_idx:
                    headers.append("")
                headers[col_idx] = content
            else:
                data_row_idx = row_idx - 1
                while len(rows) <= data_row_idx:
                    rows.append([""] * col_count)
                while len(rows[data_row_idx]) <= col_idx:
                    rows[data_row_idx].append("")
                rows[data_row_idx][col_idx] = content

        out.append({
            "bbox": bbox,
            "headers": headers,
            "rows": rows,
            "cells": extracted_cells,
            "region_type": None,  # filled by _classify_regions
        })

    return out


def _extract_paragraphs(analyze_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract paragraphs from Azure analyzeResult."""
    raw_paragraphs: list[dict[str, Any]] = analyze_result.get("paragraphs", [])
    out: list[dict[str, Any]] = []

    for para in raw_paragraphs:
        bounding_regions: list[dict[str, Any]] = para.get("boundingRegions", [])
        bbox = bounding_regions[0].get("polygon") if bounding_regions else None
        out.append({
            "bbox": bbox,
            "text": para.get("content", "").strip(),
            "role": para.get("role", ""),
        })

    return out


def _extract_key_value_pairs(analyze_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract key-value pairs from Azure analyzeResult."""
    raw_kvps: list[dict[str, Any]] = analyze_result.get("keyValuePairs", [])
    out: list[dict[str, Any]] = []

    for kvp in raw_kvps:
        key_content = (kvp.get("key") or {}).get("content", "").strip()
        value_content = (kvp.get("value") or {}).get("content", "").strip()
        confidence = kvp.get("confidence", 0.0)
        out.append({
            "key": key_content,
            "value": value_content,
            "confidence": confidence,
        })

    return out


def _extract_page_count(analyze_result: dict[str, Any]) -> int | None:
    """Extract page count from Azure analyzeResult."""
    pages: list[dict[str, Any]] = analyze_result.get("pages", [])
    return len(pages) if pages else None


# ---------------------------------------------------------------------------
# Region classifier — deterministic heuristics, no LLM
# ---------------------------------------------------------------------------

def _classify_regions(
    *,
    tables: list[dict[str, Any]],
    paragraphs: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Classify tables and paragraphs into region buckets.

    Returns dict: {legend: [...], schedule: [...], notes: [...], plan: [...], detail: [...]}
    Each entry is a list of {bbox, sheet_context} dicts.

    Heuristics are deterministic — no LLM, no randomness.
    """
    regions: dict[str, list[dict[str, Any]]] = {
        "legend": [],
        "schedule": [],
        "notes": [],
        "plan": [],
        "detail": [],
    }

    # Collect all paragraph texts for proximity annotation checks
    para_texts_lower: list[str] = [p.get("text", "").lower() for p in paragraphs]

    # --- Classify tables ---
    for i, tbl in enumerate(tables):
        headers_lower: list[str] = [h.lower() for h in (tbl.get("headers") or [])]
        rows: list[list[str]] = tbl.get("rows") or []
        bbox = tbl.get("bbox")

        region_type = _classify_table(
            headers_lower=headers_lower,
            rows=rows,
            bbox=bbox,
            para_texts_lower=para_texts_lower,
        )

        if region_type:
            tables[i]["region_type"] = region_type
            regions[region_type].append({"bbox": bbox, "table_index": i})

    # --- Classify paragraphs ---
    for para in paragraphs:
        text_lower = para.get("text", "").lower()
        role = para.get("role", "").lower()
        bbox = para.get("bbox")

        if _is_notes_paragraph(text_lower=text_lower, role=role):
            regions["notes"].append({"bbox": bbox, "text_snippet": para.get("text", "")[:80]})

    # --- Classify plan / detail regions from large bbox paragraphs ---
    # We use paragraphs with large or small bbox area as a proxy for plan / detail.
    # Azure does not always emit explicit region bboxes, so we approximate from paragraph polygons.
    sheet_area = _estimate_sheet_area(paragraphs)
    if sheet_area and sheet_area > 0:
        for para in paragraphs:
            bbox = para.get("bbox")
            if not bbox:
                continue
            area = _polygon_area(bbox)
            ratio = area / sheet_area

            if ratio > _PLAN_AREA_THRESHOLD:
                regions["plan"].append({"bbox": bbox, "area_ratio": round(ratio, 3)})
            elif ratio < _DETAIL_AREA_THRESHOLD:
                text = para.get("text", "")
                # Dense annotation check: count word tokens as proxy for annotation density
                word_count = len(text.split())
                if word_count >= _DETAIL_DENSE_ANNOTATION_MIN:
                    regions["detail"].append({"bbox": bbox, "word_count": word_count})

    return regions


def _classify_table(
    *,
    headers_lower: list[str],
    rows: list[list[str]],
    bbox: Any,
    para_texts_lower: list[str],
) -> str | None:
    """Return the region type for a table, or None if unclassified."""
    if not headers_lower:
        return None

    first_header = headers_lower[0] if headers_lower else ""

    # Legend: first column header is a symbol/mark/tag word or canonical glyph header
    if any(kw in first_header for kw in _LEGEND_HEADER_KEYWORDS):
        return "legend"

    # Legend: any paragraph near this table contains "LEGEND" or "SYMBOLS"
    for pt in para_texts_lower:
        if "legend" in pt or "symbols" in pt or "symbol list" in pt:
            return "legend"

    # Schedule: explicit row numbering (first column of rows contains integers)
    has_row_numbering = _has_row_numbering(rows)
    schedule_header_hit = any(
        any(kw in h for kw in _SCHEDULE_HEADER_KEYWORDS)
        for h in headers_lower
    )
    if has_row_numbering and schedule_header_hit:
        return "schedule"

    # Schedule: title annotation in nearby paragraphs
    for pt in para_texts_lower:
        if any(kw in pt for kw in _SCHEDULE_TITLE_KEYWORDS):
            return "schedule"

    return None


def _has_row_numbering(rows: list[list[str]]) -> bool:
    """Return True if first column contains sequential integers (row numbering pattern)."""
    if not rows:
        return False
    first_col: list[str] = [row[0].strip() if row else "" for row in rows[:10]]
    numeric_count = sum(1 for v in first_col if v.isdigit())
    return numeric_count >= min(2, len(first_col))


def _is_notes_paragraph(*, text_lower: str, role: str) -> bool:
    """Return True if a paragraph is a general notes block."""
    if role in ("generalnotes", "general_notes", "sectionheading"):
        return True
    return any(kw in text_lower for kw in _NOTES_TITLE_KEYWORDS)


def _estimate_sheet_area(paragraphs: list[dict[str, Any]]) -> float | None:
    """Estimate the total sheet area from the bounding polygons of all paragraphs."""
    all_areas = []
    for para in paragraphs:
        bbox = para.get("bbox")
        if bbox:
            a = _polygon_area(bbox)
            if a > 0:
                all_areas.append(a)
    if not all_areas:
        return None
    return sum(all_areas)


def _polygon_area(polygon: list[float] | list[dict[str, float]] | None) -> float:
    """Compute area of a polygon using the shoelace formula.

    Azure returns polygons as flat [x0,y0,x1,y1,...] or [{x,y},...] lists.
    Returns 0.0 on any malformed input (fail-safe).
    """
    if not polygon:
        return 0.0

    try:
        if isinstance(polygon[0], dict):
            pts = [(float(p["x"]), float(p["y"])) for p in polygon]
        else:
            coords = [float(v) for v in polygon]
            pts = [(coords[i], coords[i + 1]) for i in range(0, len(coords) - 1, 2)]

        n = len(pts)
        if n < 3:
            return 0.0

        area = 0.0
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            area += x1 * y2 - x2 * y1
        return abs(area) / 2.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Receipt emission (Law #2)
# ---------------------------------------------------------------------------

_STATUS_TO_OUTCOME: dict[str, str] = {
    "ok": "success",
    "failed": "failed",
    "denied": "denied",
}


def _emit_receipt(
    *,
    correlation_id: str,
    suite_id: str,
    sheet_id: str,
    project_id: str,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    outcome = _STATUS_TO_OUTCOME.get(status, "failed")
    now_iso = datetime.now(timezone.utc).isoformat()
    receipt_id = str(uuid.uuid4())

    receipt: dict[str, Any] = {
        "id": receipt_id,
        "receipt_id": receipt_id,
        "action_type": "provider.azure.sheet_layout",
        "receipt_type": "provider.azure.sheet_layout",
        "outcome": outcome,
        "status": status,
        "created_at": now_iso,
        "ts": now_iso,
        "risk_tier": "GREEN",
        "actor_type": "WORKER",
        "actor_id": ACTOR,
        "actor": ACTOR,
        "correlation_id": correlation_id,
        "receipt_version": RECEIPT_VERSION,
        "suite_id": suite_id,
        "inputs_hash": _inputs_hash(
            suite_id=suite_id, sheet_id=sheet_id, project_id=project_id
        ),
        "policy": {
            "decision": "allow" if status != "denied" else "deny",
            "policy_id": "universal-sheet-extractor-v1",
            "reasons": [],
        },
        "redactions": [],
    }

    if metadata:
        receipt["metadata"] = metadata

    store_receipts([receipt])


def _inputs_hash(*, suite_id: str, sheet_id: str, project_id: str) -> str:
    canonical = json.dumps(
        {"suite_id": suite_id, "sheet_id": sheet_id, "project_id": project_id},
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
