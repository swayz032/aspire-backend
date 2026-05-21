"""Detail resolver — Wave 19b.

Cross-references floor plan callout markers against detail sheets via
lexicon matching on existing OCR text.

Methods:
  1. callout_cross_reference — match callout markers (e.g., "1/A4") on floor
                               plan sheets to detail sheets (e.g., detail 1 on A4).
  2. title_block_detail_index — parse detail index from general-notes/detail sheets.

Confidence:
  >= 5 callouts resolved to detail sheets  → 0.88
  2-4 callouts resolved                    → 0.78 (below threshold)
  0-1 callouts resolved                    → 0.40

Law compliance:
  Law #3 — Never raises; confidence=0.0 is the signal.
  Law #9 — Callout markers (e.g., "1/A4") are safe to use as evidence keys.
"""
from __future__ import annotations

import asyncio
import logging
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

# Callout marker patterns: "1/A4", "3/S2.1", "TYP/A6"
_CALLOUT_PATTERN = re.compile(
    r"\b(\d{1,2}|[A-Z]{1,3})\s*/\s*([A-Z]{1,2}\d{1,3}(?:\.\d{1,2})?)\b",
)
# Detail sheet title indicators
_DETAIL_SHEET_INDICATOR = re.compile(
    r"\bdetail(?:s)?\b|\bsection(?:s)?\b|\bcallout(?:s)?\b",
    re.IGNORECASE,
)


class DetailResolver(MissingInputResolver):
    """Resolves door/partition/detail-reference missing_inputs."""

    category = MissingInputCategory.DETAIL

    async def resolve(self, candidate: MissingInputCandidate) -> list[ResolverResult]:
        tasks = [
            asyncio.create_task(self._method_callout_cross_reference(candidate)),
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
                results.append(self._safe_result("detail_resolve", exc))
        return results

    async def _method_callout_cross_reference(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Match callout markers on plan sheets to detail sheets."""
        try:
            # Build a set of known detail-sheet codes: e.g., "A4", "S2.1"
            detail_sheet_codes: set[str] = set()
            for sheet in candidate.sheets:
                sheet_code = str(sheet.get("sheet_code") or "")
                ocr = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )
                if _DETAIL_SHEET_INDICATOR.search(
                    str(sheet.get("sheet_title") or "")
                ) or _DETAIL_SHEET_INDICATOR.search(ocr[:500]):
                    if sheet_code:
                        detail_sheet_codes.add(sheet_code.upper())

            # Scan plan sheets for callout markers
            callouts_found: list[str] = []
            callouts_resolved: list[str] = []

            for sheet in candidate.sheets:
                ocr = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )[:4000]
                for m in _CALLOUT_PATTERN.finditer(ocr):
                    detail_num = m.group(1)
                    sheet_ref = m.group(2).upper()
                    callout_str = f"{detail_num}/{sheet_ref}"
                    callouts_found.append(callout_str)
                    if sheet_ref in detail_sheet_codes:
                        callouts_resolved.append(callout_str)

            resolved_count = len(callouts_resolved)
            if resolved_count == 0 and not callouts_found:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="callout_cross_reference",
                    evidence={"skip_reason": "no_callout_markers_found"},
                    category=self.category,
                )

            if resolved_count >= 5:
                confidence = 0.88
            elif resolved_count >= 2:
                confidence = 0.78
            else:
                confidence = 0.40

            return ResolverResult(
                resolved=confidence >= 0.85,
                value=callouts_resolved[:20] if confidence >= 0.85 else None,
                confidence=confidence,
                method="callout_cross_reference",
                evidence={
                    "callouts_found": len(callouts_found),
                    "callouts_resolved": resolved_count,
                    "detail_sheets_identified": len(detail_sheet_codes),
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("callout_cross_reference", exc)
