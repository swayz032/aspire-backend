"""Schedule resolver — Wave 19b (cap=2, broken provider call), 19.1b (wired).

Re-OCRs schedule regions with Azure Document Intelligence for sheets that
had truncated or illegible schedule data.

Methods:
  1. azure_premium_ocr — re-OCR using Azure Doc Intel's prebuilt-layout
                          model (which is already the premium tier — see
                          ``providers/azure_doc_intel_client.py:174``).
                          ``ASPIRE_AZURE_OCR_TIER`` is recorded as evidence
                          but does not currently change provider behavior.
  2. table_structure_parse — fallback: parse tabular structure from existing
                              OCR text using whitespace/pipe alignment.

Wave 19.1b changes
------------------
* PNG source: pulls from ``candidate.data_provider.get_sheet_png(sheet_id)``
  (Supabase Storage) instead of the always-empty
  ``sheet["image_bytes"]`` legacy field. Legacy field is still consulted
  as a fallback for pre-19.1 unit-test fixtures that hand-build candidates
  without a provider — those callers continue to work unchanged.
* Cap raised: 2 → 8 schedule sheets per cascade run (pre-authorized by
  founder; ~$0.08-0.24 per cascade at Azure prebuilt-layout rates).
* API bug fixes: the prior implementation called ``client.execute()`` with
  a ``ProviderRequest(action=..., parameters=..., capability_token=...)``
  that does not match either ``ProviderRequest``'s real fields nor any
  method on ``AzureDocIntelClient``. The method was guarded by an
  always-empty sheet filter so the bug never fired in production. Now
  rewritten to call ``client.analyze_layout(pdf_bytes, correlation_id,
  suite_id, office_id)`` and read ``response.body["analyzeResult"]``.
* Cost counter: ``azure_calls_made`` is recorded in ``ResolverResult.evidence``
  and lifted onto the cascade receipt by the receipt allow-list in
  ``missing_input_cascade._safe_evidence_for_receipt``.

Confidence (unchanged from Wave 19b):
  Azure succeeded, >= 3 schedule rows extracted  → 0.90 (resolves)
  Azure succeeded, 1-2 rows                      → 0.75 (below threshold)
  Table structure parse, >= 5 rows               → 0.80 (below threshold)

Env:
  ASPIRE_AZURE_OCR_TIER: "base" (default) | "premium"
    Recorded as evidence only; current provider always uses prebuilt-layout.

Law compliance:
  Law #3 — Fail-closed on missing Azure creds; skip gracefully.
  Law #6 — Provider scopes PNG reads to suite_id/project_id; resolver does
           not bypass that scoping when falling back to the legacy field.
  Law #9 — Schedule row values not logged; only row counts.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCandidate,
    MissingInputCategory,
    MissingInputResolver,
    ResolverResult,
)

_log = logging.getLogger(__name__)
_TIMEOUT_S: float = 15.0

# Wave 19.1b: raise per-cascade Azure call cap from 2 → 8. Each call hits the
# Azure prebuilt-layout async pipeline (submit + poll). At list pricing this
# is ~$0.01-0.03 USD per page; 8 sheets ≈ $0.08-0.24/cascade. Pre-authorized.
_AZURE_SHEETS_PER_CASCADE_CAP: int = 8

# Detect schedule sheets by title keywords
_SCHEDULE_TITLE_PATTERN = re.compile(
    r"\bschedule\b|\blegend\b|\bequipment\s+list\b|\bfixture\s+schedule\b",
    re.IGNORECASE,
)

# Table row detection: lines with multiple whitespace-separated or pipe-separated columns
_TABLE_ROW_PATTERN = re.compile(
    r"^[\w\s\-#]+(?:\s{2,}|\|)[\w\s\-#,/\"\']+(?:\s{2,}|\|)[\w\s\-#,/\"\']+",
    re.MULTILINE,
)


class ScheduleResolver(MissingInputResolver):
    """Resolves schedule-legibility missing_inputs."""

    category = MissingInputCategory.SCHEDULE

    async def resolve(self, candidate: MissingInputCandidate) -> list[ResolverResult]:
        tasks = [
            asyncio.create_task(self._method_azure_premium_ocr(candidate)),
            asyncio.create_task(self._method_table_structure_parse(candidate)),
        ]
        results: list[ResolverResult] = []
        for coro in asyncio.as_completed(tasks):
            try:
                result = await asyncio.wait_for(coro, timeout=_TIMEOUT_S)
                results.append(result)
            except asyncio.TimeoutError:
                results.append(ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="timeout", evidence={"timeout_s": _TIMEOUT_S},
                    category=self.category,
                ))
            except Exception as exc:  # noqa: BLE001
                results.append(self._safe_result("schedule_resolve", exc))
        return results

    async def _method_azure_premium_ocr(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Re-OCR schedule sheets with Azure Doc Intel prebuilt-layout.

        Wave 19.1b rewrite:
        * Pulls PNG bytes from ``candidate.data_provider`` (Supabase Storage)
          when available. Falls back to the legacy ``sheet["image_bytes"]``
          field for pre-19.1 fixtures that hand-build candidates without a
          provider.
        * Calls ``AzureDocIntelClient.analyze_layout(pdf_bytes, ...)`` —
          the real provider API. The prior implementation called a
          non-existent ``client.execute()`` with a misshapen
          ``ProviderRequest``; that bug was masked by an always-empty
          sheet filter.
        * Caps Azure calls at :data:`_AZURE_SHEETS_PER_CASCADE_CAP` (8).
        * Records ``azure_calls_made`` and ``png_source`` on
          ``ResolverResult.evidence`` for receipt audit.
        """
        try:
            from aspire_orchestrator.config.settings import settings

            endpoint = settings.azure_doc_intel_endpoint
            api_key_env = settings.azure_doc_intel_key
            if not endpoint or not api_key_env:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="azure_premium_ocr",
                    evidence={
                        "skip_reason": "azure_creds_missing",
                        "azure_calls_made": 0,
                    },
                    category=self.category,
                )

            ocr_tier = os.getenv("ASPIRE_AZURE_OCR_TIER", "base")

            # ----------------------------------------------------------
            # Pick candidate schedule sheets and resolve PNG source per sheet.
            # ----------------------------------------------------------
            # Title/OCR match for schedule-ish sheets. We do NOT pre-filter
            # on whether image_bytes exists like the old code did — that
            # gate excluded everything because no row carries image_bytes
            # in production. Instead we walk through and try each PNG source
            # (provider first, legacy field second) and skip if neither
            # gives us bytes.
            candidate_sheets = [
                s for s in candidate.sheets
                if _SCHEDULE_TITLE_PATTERN.search(
                    str(s.get("sheet_title") or "")
                    + str(s.get("ocr_text") or "")[:200]
                )
            ]

            if not candidate_sheets:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="azure_premium_ocr",
                    evidence={
                        "skip_reason": "no_schedule_sheets_matched",
                        "azure_calls_made": 0,
                        "schedule_sheets_found": 0,
                    },
                    category=self.category,
                )

            # Cap BEFORE issuing PNG downloads / Azure calls so we don't
            # pay for sheets we'll discard.
            capped_sheets = candidate_sheets[:_AZURE_SHEETS_PER_CASCADE_CAP]

            from aspire_orchestrator.providers.azure_doc_intel_client import (
                get_azure_doc_intel_client,
            )

            client = get_azure_doc_intel_client()
            all_schedule_rows: list[dict[str, Any]] = []
            sheets_processed = 0
            azure_calls_made = 0
            png_source_used: str = "none"  # "provider" | "legacy" | "none"

            for sheet in capped_sheets:
                # --- Resolve PNG bytes for this sheet ---
                image_bytes: bytes | None = None

                provider = candidate.data_provider
                sheet_id = str(sheet.get("id") or "")
                if provider is not None and sheet_id:
                    try:
                        image_bytes = await provider.get_sheet_png(sheet_id)
                        if image_bytes:
                            png_source_used = "provider"
                    except Exception as exc:  # noqa: BLE001
                        _log.warning(
                            "schedule_resolver.png provider fetch failed sheet=%s err=%s",
                            sheet_id[:8] if sheet_id else "", type(exc).__name__,
                        )
                        image_bytes = None

                if not image_bytes:
                    legacy = sheet.get("image_bytes")
                    if isinstance(legacy, (bytes, bytearray)) and legacy:
                        image_bytes = bytes(legacy)
                        if png_source_used == "none":
                            png_source_used = "legacy"

                if not image_bytes:
                    continue

                # --- Call Azure ---
                azure_calls_made += 1
                response = await client.analyze_layout(
                    pdf_bytes=image_bytes,
                    correlation_id="",
                    suite_id=candidate.suite_id,
                    office_id=candidate.office_id or "",
                )

                if not response.success:
                    _log.info(
                        "schedule_resolver.azure non-success sheet=%s status=%d err=%s",
                        sheet_id[:8] if sheet_id else "",
                        response.status_code,
                        response.error_code.value if response.error_code else "n/a",
                    )
                    continue

                analyze_result = response.body.get("analyzeResult") or {}
                tables = analyze_result.get("tables") or []
                for table in tables:
                    cells = table.get("cells") or []
                    if cells:
                        # row_count/column_count are top-level on the Azure
                        # ``DocumentTable`` shape. Cells carry rowIndex /
                        # columnIndex if we ever need them, but we keep
                        # the receipt-safe summary here.
                        all_schedule_rows.append({
                            "row_count": int(table.get("rowCount") or table.get("row_count") or 0),
                            "column_count": int(table.get("columnCount") or table.get("column_count") or 0),
                            "cell_count": len(cells),
                        })
                sheets_processed += 1

            row_count = sum(r.get("row_count", 0) for r in all_schedule_rows)

            if row_count == 0:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="azure_premium_ocr",
                    evidence={
                        "sheets_processed": sheets_processed,
                        "ocr_tier": ocr_tier,
                        "skip_reason": (
                            "no_table_rows_extracted"
                            if azure_calls_made > 0
                            else "no_png_bytes_available"
                        ),
                        "azure_calls_made": azure_calls_made,
                        "azure_calls_capped_at": _AZURE_SHEETS_PER_CASCADE_CAP,
                        "schedule_sheets_found": len(candidate_sheets),
                        "schedule_sheets_with_png": sheets_processed,
                        "png_source": png_source_used,
                    },
                    category=self.category,
                )

            confidence = 0.90 if row_count >= 3 else 0.75
            return ResolverResult(
                resolved=confidence >= 0.85,
                value=all_schedule_rows if confidence >= 0.85 else None,
                confidence=confidence,
                method="azure_premium_ocr",
                evidence={
                    "ocr_tier": ocr_tier,
                    "sheets_processed": sheets_processed,
                    "total_rows_extracted": row_count,
                    "tables_found": len(all_schedule_rows),
                    "azure_calls_made": azure_calls_made,
                    "azure_calls_capped_at": _AZURE_SHEETS_PER_CASCADE_CAP,
                    "schedule_sheets_found": len(candidate_sheets),
                    "schedule_sheets_with_png": sheets_processed,
                    "png_source": png_source_used,
                },
                category=self.category,
            )

        except Exception as exc:  # noqa: BLE001
            return self._safe_result("azure_premium_ocr", exc)

    async def _method_table_structure_parse(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Parse tabular structure from existing OCR using whitespace alignment."""
        try:
            total_rows = 0
            schedule_sheets_found = 0

            for sheet in candidate.sheets:
                sheet_title = str(sheet.get("sheet_title") or "")
                ocr = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )
                if not _SCHEDULE_TITLE_PATTERN.search(sheet_title + ocr[:200]):
                    continue

                schedule_sheets_found += 1
                rows = _TABLE_ROW_PATTERN.findall(ocr)
                total_rows += len(rows)

            if total_rows == 0:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="table_structure_parse",
                    evidence={"skip_reason": "no_schedule_sheets_or_table_rows"},
                    category=self.category,
                )

            confidence = 0.80 if total_rows >= 5 else 0.60
            return ResolverResult(
                resolved=False,  # OCR table parse alone is below threshold
                value=None,
                confidence=confidence,
                method="table_structure_parse",
                evidence={
                    "schedule_sheets_found": schedule_sheets_found,
                    "table_rows_parsed": total_rows,
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("table_structure_parse", exc)
