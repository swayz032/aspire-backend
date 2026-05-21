"""Addenda resolver — Wave 19b.

Builds a supersession DAG from addendum cover sheet, sheet index, and
per-sheet revision blocks to resolve addendum/revision hierarchy ambiguity.

Methods:
  1. cover_sheet_index   — parse "Sheet Index" or "Drawing Index" from addendum
                            cover sheet OCR; extract supersession relationships.
  2. revision_block_scan — for each sheet, parse the revision block for Rev/Addendum
                            numbers and their descriptions; detect DAG.
  3. discipline_tagger_style — use discipline_tagger-style keyword matching on
                            addenda-sheet OCR to detect version markers.

Confidence:
  Full DAG resolved (cover + revision blocks agree)  → 0.92
  Partial DAG (revision blocks only)                 → 0.80 (below threshold)
  Only cover sheet index                             → 0.85

Law compliance:
  Law #3 — Never raises; each method returns confidence=0.0 on failure.
  Law #9 — No raw OCR logged; only matched labels and version tokens.
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

# Revision block patterns: "Rev 1", "Addendum 2", "ASI-3", "RFI-04"
_REV_PATTERN = re.compile(
    r"(?:rev(?:ision)?|addend(?:um|a)|asi|rfi|bulletin|delta)\s*[-#]?\s*(\d{1,3})",
    re.IGNORECASE,
)
_SUPERSEDES_PATTERN = re.compile(
    r"(?:supersedes?|replaces?|cancels?)\s+([A-Z0-9\-]+)",
    re.IGNORECASE,
)
# Sheet index: "A-101 Architectural Floor Plan Rev 2"
_SHEET_INDEX_LINE = re.compile(
    r"([A-Z]{1,3}-\d{1,4}[A-Za-z]?)\s+.{0,60}\s+(?:rev|addend)\s*(\d{1,3})",
    re.IGNORECASE,
)


class AddendaResolver(MissingInputResolver):
    """Resolves addendum/revision-hierarchy missing_inputs."""

    category = MissingInputCategory.ADDENDA

    async def resolve(self, candidate: MissingInputCandidate) -> list[ResolverResult]:
        tasks = [
            asyncio.create_task(self._method_cover_sheet_index(candidate)),
            asyncio.create_task(self._method_revision_block_scan(candidate)),
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
                results.append(self._safe_result("addenda_resolve", exc))
        return results

    async def _method_cover_sheet_index(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Parse the addendum cover sheet / sheet-index pages for the supersession map."""
        try:
            dag: dict[str, str] = {}  # sheet_code -> latest_rev
            sheets_with_index = 0

            for sheet in candidate.sheets:
                discipline = str(sheet.get("discipline") or "").upper()
                sheet_number = str(sheet.get("sheet_number") or "").upper()
                # Look for cover/index sheets: discipline="Addenda", sheet_number=0 or "G"
                is_index = (
                    "ADDEND" in discipline
                    or sheet_number in ("G-000", "G-001", "T-001", "0", "INDEX")
                    or "INDEX" in str(sheet.get("sheet_title") or "").upper()
                )
                if not is_index:
                    continue

                ocr = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )[:6000]

                for m in _SHEET_INDEX_LINE.finditer(ocr):
                    sheet_code = m.group(1).upper()
                    rev_num = m.group(2)
                    existing = dag.get(sheet_code, "0")
                    if int(rev_num) > int(existing):
                        dag[sheet_code] = rev_num
                    sheets_with_index += 1

            if not dag:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="cover_sheet_index",
                    evidence={"skip_reason": "no_index_sheet_found_or_parseable"},
                    category=self.category,
                )

            confidence = 0.85 if sheets_with_index >= 2 else 0.70
            return ResolverResult(
                resolved=confidence >= 0.85,
                value=dag,
                confidence=confidence,
                method="cover_sheet_index",
                evidence={
                    "sheets_mapped": len(dag),
                    "index_sheets_parsed": sheets_with_index,
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("cover_sheet_index", exc)

    async def _method_revision_block_scan(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Scan revision blocks on each sheet to build version numbers."""
        try:
            rev_by_sheet: dict[str, int] = {}  # sheet_code -> highest rev number
            supersession_pairs: list[tuple[str, str]] = []

            for sheet in candidate.sheets:
                sheet_code = str(sheet.get("sheet_code") or sheet.get("sheet_number") or "")
                if not sheet_code:
                    continue

                ocr = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )[:4000]

                # Find highest revision number on this sheet
                for m in _REV_PATTERN.finditer(ocr):
                    try:
                        rev = int(m.group(1))
                        current = rev_by_sheet.get(sheet_code, 0)
                        if rev > current:
                            rev_by_sheet[sheet_code] = rev
                    except ValueError:
                        pass

                # Find explicit supersession mentions
                for m in _SUPERSEDES_PATTERN.finditer(ocr):
                    supersession_pairs.append((sheet_code, m.group(1).upper()))

            if not rev_by_sheet:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="revision_block_scan",
                    evidence={"skip_reason": "no_revision_blocks_found"},
                    category=self.category,
                )

            sheets_with_revisions = sum(1 for v in rev_by_sheet.values() if v > 0)
            confidence = 0.80 if sheets_with_revisions >= 3 else 0.60

            return ResolverResult(
                resolved=False,  # Partial DAG alone is below threshold; needs cover sheet
                value={"rev_map": rev_by_sheet, "supersessions": supersession_pairs},
                confidence=confidence,
                method="revision_block_scan",
                evidence={
                    "sheets_with_revisions": sheets_with_revisions,
                    "supersession_pairs": len(supersession_pairs),
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("revision_block_scan", exc)
