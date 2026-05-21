"""Sheet-Type Atlas — deterministic pre-classifier for Drew CLASSIFY (Wave 17b.1).

Runs BEFORE the LLM discipline tagger. Maps sheet_code + ocr_text + sheet_title
to a (discipline, confidence) tuple using four tiers of evidence:

  Tier 1 — sheet_code prefix (confidence 1.00).      Sheet code is canonical.
  Tier 2 — title-block keyword (confidence 0.95).    "ELECTRICAL SITE PLAN".
  Tier 3 — keyword density heuristic (confidence 0.80). >= 8 hits, 2x runner-up.
  Tier 4 — no match. None returned; LLM takes over.

Disciplines map to Drew's existing taxonomy: A, S, M, E, P, FP, C, L, Specs,
Schedules, Addenda, plus the auxiliary codes already used in sheet_code_extractor
(HC, SD, General). Auxiliary codes resolve to the closest taxonomy bucket — HC
and SD both surface as architectural details (A); General resolves to Specs.

Law compliance:
  #2 — Atlas matches drive a stage receipt `drew.classify.atlas.matched` upstream.
  #4 — Atlas classification is GREEN. Read-only, deterministic.
  #7 — No autonomous decisions outside the documented taxonomy.
  #9 — Pure function. ocr_text is never logged. Evidence strings name patterns
       only ("title_block: ELECTRICAL SITE PLAN") — never raw OCR slices.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Discipline buckets aligned with discipline_tagger valid_disciplines.
_VALID_DISCIPLINES = {
    "A", "S", "M", "E", "P", "FP", "C", "L", "Specs", "Schedules", "Addenda",
}


@dataclass(frozen=True)
class AtlasMatch:
    """Result of a single sheet classification by the atlas.

    Attributes:
        discipline: One of _VALID_DISCIPLINES.
        sheet_kind: Optional human-readable subtype (e.g. "site_plan",
            "panel_schedule", "addendum", "demolition_plan"). Informational
            only; not persisted yet.
        confidence: 0.80 (Tier 3), 0.95 (Tier 2), or 1.00 (Tier 1).
        evidence: Short, redaction-safe pattern label. Never contains raw OCR.
    """

    discipline: str
    sheet_kind: str | None
    confidence: float
    evidence: str


# ---------------------------------------------------------------------------
# Tier 1 — sheet_code prefix patterns
# ---------------------------------------------------------------------------
# Order matters: most specific first so LP- beats L-, FP- beats F-, etc.
_TIER1_CODE_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    # Prefixed multi-letter codes
    (re.compile(r"^LP-?", re.IGNORECASE), "L", "light_pole"),
    (re.compile(r"^FP-?\d", re.IGNORECASE), "FP", "fire_protection"),
    (re.compile(r"^HC-?", re.IGNORECASE), "A", "handicap_detail"),
    (re.compile(r"^SD-?", re.IGNORECASE), "A", "site_detail"),
    (re.compile(r"^GN-?|^G-?\d", re.IGNORECASE), "Specs", "general_notes"),
    (re.compile(r"^AD-?\d", re.IGNORECASE), "Addenda", "addendum"),
    # Single-letter discipline codes
    (re.compile(r"^E-?\d", re.IGNORECASE), "E", "electrical"),
    (re.compile(r"^P-?\d", re.IGNORECASE), "P", "plumbing"),
    (re.compile(r"^M-?\d", re.IGNORECASE), "M", "mechanical"),
    (re.compile(r"^A-?\d", re.IGNORECASE), "A", "architectural"),
    (re.compile(r"^S-?\d", re.IGNORECASE), "S", "structural"),
    (re.compile(r"^C-?\d|^C\d\.\d", re.IGNORECASE), "C", "civil"),
    (re.compile(r"^L-?\d", re.IGNORECASE), "L", "lighting"),
    (re.compile(r"^T-?\d", re.IGNORECASE), "Specs", "title_sheet"),
]


# ---------------------------------------------------------------------------
# Tier 2 — title-block keyword patterns (must hit in the FIRST 800 chars
# of OCR text OR in the sheet_title field — the title-block zone).
# ---------------------------------------------------------------------------
# Each tuple: (compiled regex, discipline, sheet_kind, label-for-evidence).
_TIER2_TITLE_PATTERNS: list[tuple[re.Pattern[str], str, str, str]] = [
    # Schedules — match BEFORE generic discipline phrases so "PANEL SCHEDULE"
    # doesn't get picked up as Electrical.
    (
        re.compile(
            r"\b(?:DOOR|WINDOW|ROOM\s+FINISH|HARDWARE|PANEL|FIXTURE|LIGHT(?:ING)?|EQUIPMENT|PLUMBING\s+FIXTURE)\s+SCHEDULE\b",
            re.IGNORECASE,
        ),
        "Schedules", "schedule", "schedule",
    ),
    # Specifications — guard against "PER SPECIFICATIONS" (body reference).
    (
        re.compile(
            r"(?<!PER\s)\bSPECIFICATIONS?\b(?!\s+(?:NOTED|SHOWN|REQUIRED))",
            re.IGNORECASE,
        ),
        "Specs", "specifications", "specifications",
    ),
    # Cover / Title / Index
    (
        re.compile(r"\b(?:TITLE\s+SHEET|COVER\s+SHEET|SHEET\s+INDEX|DRAWING\s+INDEX)\b", re.IGNORECASE),
        "Specs", "cover_sheet", "cover_sheet",
    ),
    # Addenda — must be standalone, not "ADDENDUM REQUIREMENTS" inside specs body.
    (
        re.compile(r"\bADDEND[AU]M\s+\d", re.IGNORECASE),
        "Addenda", "addendum", "addendum",
    ),
    # General notes
    (
        re.compile(r"\bGENERAL\s+NOTES\b", re.IGNORECASE),
        "Specs", "general_notes", "general_notes",
    ),
    # Electrical
    (
        re.compile(
            r"\bELECTRICAL\s+(?:SITE|PLAN|RISER|ONE-?LINE|DETAIL|LEGEND|SYMBOL)\b",
            re.IGNORECASE,
        ),
        "E", "electrical_plan", "electrical_plan",
    ),
    # Plumbing
    (
        re.compile(r"\bPLUMBING\s+(?:SITE|PLAN|RISER|ISO|DETAIL|LEGEND)\b", re.IGNORECASE),
        "P", "plumbing_plan", "plumbing_plan",
    ),
    # Mechanical
    (
        re.compile(r"\bMECHANICAL\s+(?:PLAN|EQUIPMENT|DETAIL|LEGEND)\b", re.IGNORECASE),
        "M", "mechanical_plan", "mechanical_plan",
    ),
    # Fire Protection — only at top of OCR (excluded from specs body via offset guard).
    (
        re.compile(r"\bFIRE\s+PROTECTION\s+(?:PLAN|RISER|DETAIL|LEGEND)\b", re.IGNORECASE),
        "FP", "fire_protection_plan", "fire_protection_plan",
    ),
    # Civil / Site
    (
        re.compile(r"\bSITE\s+(?:PLAN|UTILITY|GRADING|DRAINAGE|PAVING)\b", re.IGNORECASE),
        "C", "site_plan", "site_plan",
    ),
    (
        re.compile(r"\bCIVIL\s+(?:PLAN|DETAIL|LEGEND)\b", re.IGNORECASE),
        "C", "civil_plan", "civil_plan",
    ),
    # Lighting
    (
        re.compile(r"\bLIGHTING\s+(?:PLAN|FIXTURE\s+PLAN|PHOTOMETRIC)\b", re.IGNORECASE),
        "L", "lighting_plan", "lighting_plan",
    ),
    # Structural
    (
        re.compile(r"\bSTRUCTURAL\s+(?:PLAN|FRAMING|FOUNDATION|DETAIL)\b", re.IGNORECASE),
        "S", "structural_plan", "structural_plan",
    ),
    # Architectural (lower priority — most A sheets caught by Tier 1)
    (
        re.compile(r"\bARCHITECTURAL\s+(?:PLAN|FLOOR\s+PLAN|ROOF\s+PLAN|DETAIL)\b", re.IGNORECASE),
        "A", "architectural_plan", "architectural_plan",
    ),
    (
        re.compile(r"\bFLOOR\s+PLAN\b", re.IGNORECASE),
        "A", "floor_plan", "floor_plan",
    ),
    (
        re.compile(r"\bROOF\s+PLAN\b", re.IGNORECASE),
        "A", "roof_plan", "roof_plan",
    ),
    (
        re.compile(r"\bREFLECTED\s+CEILING\s+PLAN\b", re.IGNORECASE),
        "A", "rcp", "reflected_ceiling_plan",
    ),
]

# Title-block zone: first 800 chars of OCR (or the sheet_title field).
_TITLE_BLOCK_CHARS = 800


# ---------------------------------------------------------------------------
# Tier 3 — keyword density heuristic. Used as a last resort when neither
# the sheet_code nor the title block yielded a hit. Counts whole-word
# occurrences of discipline-specific terms across the full OCR.
# ---------------------------------------------------------------------------
_TIER3_KEYWORDS: dict[str, list[str]] = {
    "E": [
        "outlet", "receptacle", "gfci", "panel", "conduit", "breaker", "mcb",
        "conductor", "junction", "switch", "circuit", "amperage", "voltage",
        "kva", "kilowatt", "wattage", "luminaire",
    ],
    "P": [
        "lavatory", "water closet", "urinal", "supply", "waste", "vent",
        "riser", "trap", "valve", "cleanout", "backflow", "p-trap",
        "domestic water", "sanitary",
    ],
    "M": [
        "hvac", "ahu", "rtu", "diffuser", "supply air", "return air",
        "damper", "vav", "ductwork", "exhaust fan", "condenser", "thermostat",
        "refrigerant",
    ],
    "A": [
        "room", "partition", "ceiling", "floor finish", "gwb", "drywall",
        "wall type", "door type", "millwork", "casework", "baseboard",
    ],
    "S": [
        "column", "footing", "slab", "rebar", "joist", "lintel", "girder",
        "anchor bolt", "concrete strength", "shear wall", "deck",
    ],
    "C": [
        "grading", "drainage", "stormwater", "right-of-way", "easement",
        "curb", "asphalt", "swale", "detention pond", "manhole",
    ],
    "FP": [
        "sprinkler", "standpipe", "fire pump", "fdc", "alarm valve",
        "preaction", "deluge", "wet pipe", "dry pipe",
    ],
}

_TIER3_MIN_HITS = 8
_TIER3_DOMINANCE_RATIO = 2.0  # winner must have 2x runner-up


# ---------------------------------------------------------------------------
# False-positive guards
# ---------------------------------------------------------------------------
# "WC" alone is dangerous (could be "WC-1" plumbing fixture but also "W/C")
# — handled by Tier 3 only matching word boundaries.
# Dates like "03/2026" or "2026-05" should never trigger anything because they
# don't match the patterns above. Just sanity here.
_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_LICENSE_RE = re.compile(r"\bLIC(?:ENSE)?\s*(?:NO|#|:)?\s*[A-Z0-9-]+\b", re.IGNORECASE)
_PAGE_COUNTER_RE = re.compile(r"\b\d+\s*OF\s*\d+\b", re.IGNORECASE)


def _strip_noise(text: str) -> str:
    """Remove dates / license numbers / page counters before keyword scanning.

    This prevents incidental matches in title blocks where "1 OF 39" or
    "License #12345" could throw off keyword density.
    """
    cleaned = _DATE_RE.sub(" ", text)
    cleaned = _LICENSE_RE.sub(" ", cleaned)
    cleaned = _PAGE_COUNTER_RE.sub(" ", cleaned)
    return cleaned


def _tier1_match(sheet_code: str | None) -> AtlasMatch | None:
    if not sheet_code:
        return None
    for pattern, discipline, kind in _TIER1_CODE_PATTERNS:
        if pattern.match(sheet_code):
            return AtlasMatch(
                discipline=discipline,
                sheet_kind=kind,
                confidence=1.00,
                evidence=f"sheet_code:{kind}",
            )
    return None


def _tier2_match(
    *,
    sheet_title: str | None,
    ocr_text: str | None,
) -> AtlasMatch | None:
    """Scan the title-block zone (first 800 chars) and sheet_title for a phrase."""
    if not ocr_text and not sheet_title:
        return None

    head = (ocr_text or "")[:_TITLE_BLOCK_CHARS]
    title_zone = f"{sheet_title or ''}\n{head}"

    for pattern, discipline, kind, label in _TIER2_TITLE_PATTERNS:
        if pattern.search(title_zone):
            return AtlasMatch(
                discipline=discipline,
                sheet_kind=kind,
                confidence=0.95,
                evidence=f"title_block:{label}",
            )
    return None


def _tier3_match(ocr_text: str | None) -> AtlasMatch | None:
    """Keyword-density fallback. Whole-word, lowercase, full-OCR scan."""
    if not ocr_text or len(ocr_text) < 200:
        return None

    cleaned = _strip_noise(ocr_text).lower()
    counts: dict[str, int] = {}
    for discipline, keywords in _TIER3_KEYWORDS.items():
        total = 0
        for kw in keywords:
            # Whole-word match — \b around multi-word phrases works in Python re.
            pat = r"\b" + re.escape(kw) + r"\b"
            total += len(re.findall(pat, cleaned))
        if total > 0:
            counts[discipline] = total

    if not counts:
        return None

    sorted_hits = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    winner, top = sorted_hits[0]
    if top < _TIER3_MIN_HITS:
        return None
    runner_up = sorted_hits[1][1] if len(sorted_hits) > 1 else 0
    if runner_up > 0 and top < runner_up * _TIER3_DOMINANCE_RATIO:
        return None

    return AtlasMatch(
        discipline=winner,
        sheet_kind=None,
        confidence=0.80,
        evidence=f"keyword_density:{winner}:{top}",
    )


def classify(
    *,
    sheet_number: str | None,
    sheet_code: str | None,
    sheet_title: str | None,
    ocr_text: str | None,
) -> AtlasMatch | None:
    """Run the four-tier deterministic classifier.

    Args:
        sheet_number: Display label ("Sheet 1", "1 of 39"). Unused today;
            kept in the signature so future tiers can lean on it.
        sheet_code: Architectural identifier such as "E-1", "A-101". Drives Tier 1.
        sheet_title: Extracted title phrase, e.g. "Electrical Site Plan".
        ocr_text: Full OCR text of the sheet (may be very long).

    Returns:
        AtlasMatch when a tier fires; None when no tier matched and the LLM
        should classify the sheet.
    """
    del sheet_number  # reserved for future tiers

    tier1 = _tier1_match(sheet_code)
    if tier1 is not None:
        return tier1

    tier2 = _tier2_match(sheet_title=sheet_title, ocr_text=ocr_text)
    if tier2 is not None:
        return tier2

    tier3 = _tier3_match(ocr_text)
    if tier3 is not None:
        return tier3

    return None


__all__ = ["AtlasMatch", "classify"]
