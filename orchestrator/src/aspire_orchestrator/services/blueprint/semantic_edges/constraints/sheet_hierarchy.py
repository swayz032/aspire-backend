"""M-5 — SheetHierarchyConstraint (detail callout resolution).

Catches the classic RFI-trigger pattern: an architect types `4/M-101`
but the latest addendum moved Detail 4 to M-102, or M-101 doesn't even
exist in the set. Drew flags the broken reference to UNKNOWN_REVIEW so
the GC can submit an RFI BEFORE bidding around a missing detail.

Severity: SOFT (UR-downgrade) per reviewer 2026-05-26.
  A broken sheet reference is high-value information the GC needs to
  see — HARD reject would silently delete the callout and the GC would
  never know to ask the architect.

What this catches:
  REFERENCES_DETAIL edges where:
    - evidence['callout_text'] contains a callout-shaped pattern
    - the parsed sheet code is NOT in the project's known sheet registry
    OR
    - dst is an explicit sheet entity that doesn't match the parsed
      callout target

  CALLS_OUT edges where the OCR text contains a sheet reference
  pointing to a non-existent sheet.

Callout formats supported (case-insensitive):
  4/A-501         — detail/sheet (most common)
  4/A501          — no separator
  4-A-501         — dash separator
  4/M101.1        — alphanumeric sheet codes with decimals
  DETAIL 4 ON A-501       — verbose
  SEE A-501               — sheet-only (no detail number)
  SEE DETAIL 4 / A-501    — verbose with dual indicator
  A-501/4                 — reversed format (rare but seen)

Provenance contract — generators must populate:
  provenance['project_sheet_codes'] = ['A-101', 'A-201', 'M-101', ...]
                                      — normalized list of all known
                                        sheet codes in the project

If project_sheet_codes is absent, the constraint can't validate and
returns SOFT_SKIP (fail open, matches the M-2/M-3/M-4 pattern).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from ..taxonomy import EdgeProposal, EdgeType
from .base import BaseConstraint, ConstraintResult, ConstraintSeverity


_SHEET_HIERARCHY_EDGES: frozenset[EdgeType] = frozenset({
    EdgeType.REFERENCES_DETAIL,
    EdgeType.CALLS_OUT,
})


# ============================================================
# Callout text parsing
# ============================================================
# Order matters: more-specific patterns first. Each pattern's first
# capture group is the detail id (or None) and second is the sheet
# code. We normalize sheet codes by stripping whitespace + uppercasing.

_CALLOUT_PATTERNS: list[tuple[re.Pattern[str], int, int]] = [
    # "SEE DETAIL 4 / A-501" or "SEE DETAIL 4 A-501"
    (
        re.compile(
            r"SEE\s*DETAIL\s*(\d+[A-Z]?)\s*[/\-\s]\s*([A-Z]{1,4}[-\.]?\d+(?:\.\d+)?)",
            re.IGNORECASE,
        ),
        1, 2,
    ),
    # "DETAIL 4 ON A-501" / "DETAIL 4, A-501"
    (
        re.compile(
            r"DETAIL\s*(\d+[A-Z]?)\s*(?:ON|,|\sAT\s)\s*([A-Z]{1,4}[-\.]?\d+(?:\.\d+)?)",
            re.IGNORECASE,
        ),
        1, 2,
    ),
    # "SEE A-501" (no detail number — sheet-only reference)
    (
        re.compile(
            r"SEE\s+([A-Z]{1,4}[-\.]?\d+(?:\.\d+)?)\b",
            re.IGNORECASE,
        ),
        0, 1,    # 0 means "no detail id"
    ),
    # "4/A-501" / "4/A501" / "4-A-501" — most common
    (
        re.compile(
            r"\b(\d+[A-Z]?)\s*[/\-]\s*([A-Z]{1,4}[-\.]?\d+(?:\.\d+)?)\b",
        ),
        1, 2,
    ),
    # "A-501/4" — reversed
    (
        re.compile(
            r"\b([A-Z]{1,4}[-\.]?\d+(?:\.\d+)?)\s*/\s*(\d+[A-Z]?)\b",
        ),
        2, 1,    # detail is group 2, sheet is group 1
    ),
]


def normalize_sheet_code(code: str) -> str:
    """Canonical form: uppercase, strip whitespace, normalize separator."""
    if not code:
        return ""
    s = code.strip().upper()
    # Normalize 'A501' → 'A-501' (insert dash between letters and digits)
    # but only when no dash present and no decimals follow
    if "-" not in s and "." not in s:
        m = re.match(r"^([A-Z]{1,4})(\d+)$", s)
        if m:
            s = f"{m.group(1)}-{m.group(2)}"
    return s


def parse_callout(text: Optional[str]) -> Optional[tuple[Optional[str], str]]:
    """Try each callout pattern; return (detail_id_or_None, sheet_code) or None.

    detail_id is None for sheet-only references ("SEE A-501").
    sheet_code is normalized via normalize_sheet_code.
    """
    if not text:
        return None
    for pattern, detail_grp, sheet_grp in _CALLOUT_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        detail = m.group(detail_grp) if detail_grp > 0 else None
        sheet = m.group(sheet_grp)
        if not sheet:
            continue
        return (
            detail.strip().upper() if detail else None,
            normalize_sheet_code(sheet),
        )
    return None


# ============================================================
# Constraint
# ============================================================


class SheetHierarchyConstraint(BaseConstraint):
    """Validate that detail-callout edges reference real sheets in the project.

    Severity SOFT — broken references are RFI candidates, not silent
    deletes.
    """

    name = "sheet_hierarchy"
    severity = ConstraintSeverity.SOFT
    applies_to = _SHEET_HIERARCHY_EDGES

    def check(self, proposal: EdgeProposal) -> ConstraintResult:
        # Sheet registry is mandatory context. Without it we can't
        # validate; SOFT_SKIP (fail open).
        registry = self._get_sheet_registry(proposal)
        if not registry:
            return self._soft_skip(
                reason="missing_sheet_registry",
                evidence={
                    "context_required": "provenance.project_sheet_codes",
                    "edge_type": proposal.edge_type.value,
                },
            )

        # Pull the callout text — try evidence keys most likely to
        # contain it depending on edge type
        callout_text = self._get_callout_text(proposal)
        if not callout_text:
            return self._soft_skip(
                reason="missing_callout_text",
                evidence={
                    "edge_type": proposal.edge_type.value,
                    "context_required": "evidence.callout_text or text",
                },
            )

        parsed = parse_callout(callout_text)
        if parsed is None:
            return self._soft_skip(
                reason="callout_text_format_unrecognized",
                evidence={
                    "callout_text": callout_text,
                    "expected_formats": "'4/A-501' / 'DETAIL 4 ON A-501' / 'SEE A-501'",
                },
            )

        detail_id, parsed_sheet = parsed

        # Check parsed sheet exists in registry
        if parsed_sheet not in registry:
            return self._violation(
                reason="broken_sheet_reference",
                evidence={
                    "callout_text": callout_text,
                    "parsed_detail_id": detail_id,
                    "parsed_sheet_code": parsed_sheet,
                    "registry_size": len(registry),
                    "rfi_recommended": True,
                },
            )

        # If we also know what dst sheet the generator bound to, check
        # the binding matches the parsed callout
        dst_sheet_code = self._get_dst_sheet_code(proposal)
        if dst_sheet_code and normalize_sheet_code(dst_sheet_code) != parsed_sheet:
            return self._violation(
                reason="callout_dst_mismatch",
                evidence={
                    "callout_text": callout_text,
                    "parsed_sheet_code": parsed_sheet,
                    "bound_dst_sheet": normalize_sheet_code(dst_sheet_code),
                    "parsed_detail_id": detail_id,
                },
            )

        return self._passing(
            reason="callout_resolves",
            evidence={
                "callout_text": callout_text,
                "parsed_detail_id": detail_id,
                "parsed_sheet_code": parsed_sheet,
            },
        )

    # ---------- helpers ----------

    @staticmethod
    def _get_sheet_registry(proposal: EdgeProposal) -> frozenset[str]:
        """Read the project's sheet code registry from provenance.

        Accepts list/tuple/set of strings under provenance.project_sheet_codes.
        Returns a frozenset of normalized codes for O(1) lookup.
        """
        raw = proposal.provenance.get("project_sheet_codes")
        if not raw:
            return frozenset()
        if not isinstance(raw, (list, tuple, set, frozenset)):
            return frozenset()
        out: set[str] = set()
        for code in raw:
            if code:
                out.add(normalize_sheet_code(str(code)))
        return frozenset(out)

    @staticmethod
    def _get_callout_text(proposal: EdgeProposal) -> Optional[str]:
        """REFERENCES_DETAIL stores callout in evidence.callout_text;
        CALLS_OUT stores it in evidence.text. Try both."""
        for key in ("callout_text", "text", "ocr_text"):
            v = proposal.evidence.get(key)
            if v:
                return str(v)
        return None

    @staticmethod
    def _get_dst_sheet_code(proposal: EdgeProposal) -> Optional[str]:
        """The dst-side sheet code, if the generator populated it.
        Generators that resolve callouts to actual sheet entities
        should set evidence.target_sheet_code.
        """
        v = proposal.evidence.get("target_sheet_code")
        if v:
            return str(v)
        return None
