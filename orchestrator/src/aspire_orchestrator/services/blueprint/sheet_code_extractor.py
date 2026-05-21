"""Sheet-code extractor — Wave 11p.

Pulls architectural sheet identifiers (E-1, P-1, A-101, C2.2, LP-R1, etc.)
and sheet titles ("Electrical Site Plan") from OCR text of a blueprint
title block.

Deterministic regex-based. No LLM cost. Runs at CLASSIFY time alongside
discipline tagging, and also drives the one-shot backfill for existing
projects.

Title-block convention: the sheet identifier is almost always in the
bottom-right corner. PyMuPDF returns OCR text in reading order, so the
title block tends to land in the LAST few hundred characters. We scan
both ends to handle PDFs with non-standard layouts.

Law #9: function is pure — no logging of OCR content, no PII surface.
"""
from __future__ import annotations

import re
from typing import NamedTuple

# Sheet code patterns — ordered most-specific first so prefixed codes
# (LP-R1, GSM-2) beat the generic letter+digit catch-all.
_SHEET_CODE_PATTERNS: list[re.Pattern[str]] = [
    # Prefixed: LP-R1, FP-2, AD-3, GSM-2, ENG-C2.2
    re.compile(r"\b(?:LP|FP|AD|GSM|ENG|GN|HC)-(?:R?\d+(?:\.\d+)?)\b", re.IGNORECASE),
    # Discipline letter + dash + number(s) with optional decimal: E-1, A-101, C-2.2
    re.compile(r"\b([ASEPCMLT]|FP|HC|SD)-\d+(?:\.\d+)?\b", re.IGNORECASE),
    # Discipline letter immediately followed by number with decimal: C2.2, A101
    re.compile(r"\b([ASEPCML])\d+(?:\.\d+)?\b"),
]

# Disqualify common false-positives. These look like sheet codes but
# never are (dates, OCR garbage, page counters).
_FALSE_POSITIVES = {
    # Years and date fragments
    "2024", "2025", "2026", "2027", "2028", "2029", "2030",
    # Sheet ordinals like "1OF1", "2OF5"
}
_FALSE_POSITIVE_RE = re.compile(r"^\d+OF\d+$", re.IGNORECASE)

# Title patterns — common phrases on the line after the sheet code in a
# title block. We don't try to extract free-form titles (too unreliable);
# instead we look for canonical phrases.
_TITLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b((?:ELECTRICAL|PLUMBING|MECHANICAL|STRUCTURAL|CIVIL|ARCHITECTURAL|LIGHTING|LANDSCAPE|FIRE\s+PROTECTION)(?:\s+(?:SITE|FLOOR|ROOF|FOUNDATION|RISER|DETAIL|SCHEDULE|LEGEND))?\s+PLAN)\b", re.IGNORECASE),
    re.compile(r"\b((?:GENERAL|COVER|TITLE)\s+(?:NOTES|SHEET|PAGE))\b", re.IGNORECASE),
    re.compile(r"\b(ADDEND[AU]M(?:\s+\d+)?)\b", re.IGNORECASE),
    re.compile(r"\b(SCHEDULES?)\b", re.IGNORECASE),
]


class SheetCodeResult(NamedTuple):
    sheet_code: str | None
    sheet_title: str | None


def _looks_like_false_positive(candidate: str) -> bool:
    upper = candidate.upper().replace(" ", "")
    if upper in _FALSE_POSITIVES:
        return True
    if _FALSE_POSITIVE_RE.match(upper):
        return True
    return False


def _normalize_code(raw: str) -> str:
    """Title-block codes appear in many casings — normalize to upper, hyphenated."""
    return raw.strip().upper()


def extract_sheet_code_title(ocr_text: str | None) -> SheetCodeResult:
    """Extract sheet_code and sheet_title from OCR text.

    Strategy:
      1. Scan the FIRST 800 chars and LAST 800 chars (title-block zones).
      2. Try each pattern in priority order; first non-false-positive wins.
      3. Extract a sheet title if a canonical phrase is present anywhere.

    Returns:
        SheetCodeResult(sheet_code=str|None, sheet_title=str|None)
    """
    if not ocr_text:
        return SheetCodeResult(None, None)

    text = ocr_text
    head = text[:800]
    tail = text[-800:] if len(text) > 800 else ""
    scan_zones = [tail, head]  # title blocks are usually at the END

    sheet_code: str | None = None
    for zone in scan_zones:
        if sheet_code:
            break
        for pattern in _SHEET_CODE_PATTERNS:
            for match in pattern.finditer(zone):
                candidate = match.group(0)
                if _looks_like_false_positive(candidate):
                    continue
                sheet_code = _normalize_code(candidate)
                break
            if sheet_code:
                break

    # Title — scan the whole text, take the first canonical match.
    sheet_title: str | None = None
    for pattern in _TITLE_PATTERNS:
        m = pattern.search(text)
        if m:
            # Normalize whitespace + title-case for display
            raw_title = m.group(1)
            sheet_title = " ".join(raw_title.split()).title()
            break

    return SheetCodeResult(sheet_code=sheet_code, sheet_title=sheet_title)
