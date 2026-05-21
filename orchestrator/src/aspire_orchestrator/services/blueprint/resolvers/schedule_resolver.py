"""Schedule resolver — Wave 19b.

Re-OCRs schedule regions with Azure Document Intelligence for sheets that
had truncated or illegible schedule data.

Methods:
  1. azure_premium_ocr — re-OCR using Azure Doc Intel with premium tier if
                          ASPIRE_AZURE_OCR_TIER=premium, else base tier.
  2. table_structure_parse — fallback: parse tabular structure from existing
                              OCR text using whitespace/pipe alignment.

Confidence:
  Azure succeeded, >= 3 schedule rows extracted  → 0.90
  Azure succeeded, 1-2 rows                      → 0.75 (below threshold)
  Table structure parse, >= 5 rows               → 0.80 (below threshold)

Env:
  ASPIRE_AZURE_OCR_TIER: "base" (default) | "premium"
    Set to "premium" in production for higher-accuracy schedule OCR.

Law compliance:
  Law #3 — Fail-closed on missing Azure creds; skip gracefully.
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
        """Re-OCR schedule sheets with Azure Doc Intel (tier from env)."""
        try:
            from aspire_orchestrator.config.settings import settings

            endpoint = settings.azure_doc_intel_endpoint
            api_key_env = settings.azure_doc_intel_key
            if not endpoint or not api_key_env:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="azure_premium_ocr",
                    evidence={"skip_reason": "azure_creds_missing"},
                    category=self.category,
                )

            ocr_tier = os.getenv("ASPIRE_AZURE_OCR_TIER", "base")

            # Find schedule sheets with stored image bytes
            schedule_sheets = [
                s for s in candidate.sheets
                if _SCHEDULE_TITLE_PATTERN.search(
                    str(s.get("sheet_title") or "") + str(s.get("ocr_text") or "")[:200]
                )
                and s.get("image_bytes")
            ][:2]  # Cap at 2 sheets — each Azure call is billed

            if not schedule_sheets:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="azure_premium_ocr",
                    evidence={"skip_reason": "no_schedule_sheets_with_images"},
                    category=self.category,
                )

            from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient
            from aspire_orchestrator.providers.base_client import ProviderRequest

            client = AzureDocIntelClient()
            all_schedule_rows: list[dict[str, Any]] = []
            sheets_processed = 0

            for sheet in schedule_sheets:
                image_bytes = sheet.get("image_bytes") or b""
                if not image_bytes:
                    continue

                req = ProviderRequest(
                    action="analyze_layout",
                    parameters={"pdf_bytes": image_bytes, "tier": ocr_tier},
                    capability_token=None,  # Green-tier internal call; no token required
                    suite_id=candidate.suite_id,
                    office_id=candidate.office_id or "",
                    correlation_id="",
                    idempotency_key=f"schedule_resolver_{sheet.get('id', '')[:8]}",
                )

                result = await client.execute(req)
                if result.success and result.data:
                    tables = result.data.get("tables") or []
                    for table in tables:
                        cells = table.get("cells") or []
                        if cells:
                            all_schedule_rows.append({
                                "row_count": table.get("row_count", 0),
                                "column_count": table.get("column_count", 0),
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
                        "skip_reason": "no_table_rows_extracted",
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
