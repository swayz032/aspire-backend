"""Revision Detector — title-block heuristic for Drew Blueprint Engine.

Identifies superseding/superseded relationships between blueprint sheets:
  - Sheets marked "REV N", "Revision N", or "Addendum N" in their title block
  - Duplicate sheet numbers (newer revision supersedes older)
  - Addendum sheets supersede the base sheets they reference

Law compliance:
  #7 — Pure function. Returns RevisionLink pairs; Drew.classify() persists.
  #9 — Never logs OCR content. Logs only sheet IDs and revision markers.

Pattern: stateless, synchronous, deterministic heuristic — no I/O.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Regex patterns for common title-block revision markers
_REV_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bREV(?:ISION)?\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bADDENDUM\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bBULLETIN\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bASI[-\s]*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bSK[-\s]*(\d+)\b", re.IGNORECASE),
]

# Addendum marker — sheet belongs to an addendum set
_ADDENDUM_RE = re.compile(r"\bADDENDUM\b", re.IGNORECASE)


@dataclass(frozen=True)
class RevisionLink:
    """A superseding/superseded relationship between two sheets.

    Attributes:
        superseding_sheet_id: The NEWER sheet (the one that replaces).
        superseded_sheet_id: The OLDER sheet (the one being replaced).
        revision_number: Parsed revision number (int), or None if not deterministic.
        reason: Human-readable explanation of why this relationship was detected.
    """

    superseding_sheet_id: str
    superseded_sheet_id: str
    revision_number: int | None
    reason: str


def _extract_revision_number(text: str) -> int | None:
    """Return the highest revision number found in text, or None."""
    best: int | None = None
    for pattern in _REV_PATTERNS:
        for match in pattern.finditer(text):
            try:
                n = int(match.group(1))
                if best is None or n > best:
                    best = n
            except (IndexError, ValueError):
                continue
    return best


def _is_addendum_sheet(ocr_text: str) -> bool:
    return bool(_ADDENDUM_RE.search(ocr_text or ""))


def detect_revisions(sheets: list[dict]) -> list[RevisionLink]:
    """Detect superseding/superseded pairs among a project's sheets.

    Args:
        sheets: List of sheet row dicts. Each must have:
                "id" (str), "sheet_number" (str|None), "ocr_text" (str|None).
                Rows should be ordered by created_at ASC so earlier rows are older.

    Returns:
        List of RevisionLink pairs. Caller must UPDATE supersedes_id in the DB.
        Never mutates input.

    Algorithm:
      1. Group sheets by sheet_number.
      2. Within each group, extract revision numbers from ocr_text.
      3. Pair each sheet with the next-lower revision (or creation order if no
         explicit revision number found).
      4. Addendum sheets additionally supersede any base sheet with same sheet_number.
    """
    links: list[RevisionLink] = []

    # Group by sheet_number (None-keyed sheets are unclassified — skip pairing)
    groups: dict[str, list[dict]] = {}
    for sheet in sheets:
        snum = sheet.get("sheet_number")
        if not snum:
            continue
        groups.setdefault(snum, []).append(sheet)

    for sheet_number, group in groups.items():
        if len(group) < 2:
            continue  # No revisions if only one sheet with this number

        # Annotate each sheet with its revision number (or positional index as fallback)
        annotated: list[tuple[int | None, int, dict]] = []
        for idx, sheet in enumerate(group):
            rev_num = _extract_revision_number(sheet.get("ocr_text") or "")
            annotated.append((rev_num, idx, sheet))

        # Sort: sheets with explicit revision number come first (by rev_num),
        # then by creation order (idx). This handles mixed-notation sets gracefully.
        def sort_key(item: tuple[int | None, int, dict]) -> tuple[int, int, int]:
            rev, idx, _ = item
            # Explicit rev numbers: sort ascending. None → treat as rev 0 if only one, else by idx
            explicit = rev if rev is not None else -1
            return (0 if rev is not None else 1, explicit, idx)

        annotated.sort(key=sort_key)

        # Each sheet supersedes the one directly before it in sort order
        for i in range(1, len(annotated)):
            older_rev, older_idx, older_sheet = annotated[i - 1]
            newer_rev, newer_idx, newer_sheet = annotated[i]
            older_id = str(older_sheet["id"])
            newer_id = str(newer_sheet["id"])

            reason = (
                f"sheet_number={sheet_number!r}, "
                f"rev {older_rev!r} → {newer_rev!r} "
                f"(creation order {older_idx} → {newer_idx})"
            )
            if _is_addendum_sheet(newer_sheet.get("ocr_text") or ""):
                reason = f"addendum supersedes base: {reason}"

            links.append(
                RevisionLink(
                    superseding_sheet_id=newer_id,
                    superseded_sheet_id=older_id,
                    revision_number=newer_rev,
                    reason=reason,
                )
            )

            logger.info(
                "revision_detector: %s supersedes %s (%s)",
                newer_id[:8],
                older_id[:8],
                reason,
            )

    logger.info(
        "revision_detector: detected %d revision links across %d sheet groups",
        len(links),
        len(groups),
    )
    return links
