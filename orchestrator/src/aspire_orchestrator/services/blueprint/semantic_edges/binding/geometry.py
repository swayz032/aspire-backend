"""N-2.7 — Entity geometry classifier + Stage 1 gate matrix.

Reviewer-locked (2026-05-26):

    GeometryClass = {POINT, LINEAR, POLYGON, NONE, UNKNOWN}

Why NONE matters
----------------
Live audit on Gulfstream surfaced an entire population of "glossary"
legend rows that are NOT physical entities at all — they are
abbreviation dictionaries inside the legend table:

    CONT  = CONTINUOUS
    ADD   = ADDITIONAL
    EXIST = EXISTING
    CONTR = CONTRACTOR
    NIC   = NOT IN CONTRACT
    TYP   = TYPICAL

A rookie GC doesn't order materials for "CONTINUOUS". Tagging these
as NONE kills them at Stage 1 instead of letting them generate noise
through every downstream scorer.

Fail-open posture
-----------------
UNKNOWN on either side passes the gate. The system errs toward
admitting candidates when it can't classify confidently — Stage 2/3
scorers will still reject low-affinity bindings, but a real but
weird symbol never gets silently dropped just because we couldn't
classify it.

Gate matrix (motif × legend):
                          | POINT | LINEAR | POLYGON | NONE  | UNKNOWN |
    POINT                 | allow | REJECT | REJECT  | REJECT| allow   |
    LINEAR                | REJECT| allow  | REJECT  | REJECT| allow   |
    POLYGON               | REJECT| REJECT | allow   | REJECT| allow   |
    NONE                  | REJECT| REJECT | REJECT  | REJECT| REJECT  |   (motif=NONE never happens in practice)
    UNKNOWN               | allow | allow  | allow   | REJECT| allow   |

Note: legend=NONE is ALWAYS rejected. A glossary row is not a bind
target regardless of how the motif was classified.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class GeometryClass(str, Enum):
    """Five-way geometry classification for binding gate."""
    POINT = "point"        # zero-dim fixture symbol (sink, outlet, sprinkler head)
    LINEAR = "linear"      # one-dim line/curve (pipe, duct, conduit, wire run)
    POLYGON = "polygon"    # two-dim area (room, zone, slab, hatch region)
    NONE = "none"          # NOT a graphical entity — glossary / note / abbreviation
    UNKNOWN = "unknown"    # cannot determine — passes the gate (fail-open)


# ============================================================
# Legend keyword dictionaries (rule-based classifier)
# ============================================================
# Comprehensive coverage for the 5 trades currently in Drew's
# Gulfstream test corpus: plumbing, fire, mechanical, electrical,
# architectural. Each set is the union of legend-label vocabulary
# observed in the Gulfstream 288-legend dump PLUS standard CAD
# legend conventions across construction disciplines.

# --- POINT: discrete fixtures, equipment, accessories ----------
POINT_KEYWORDS: frozenset[str] = frozenset({
    # Plumbing fixtures
    "sink", "lavatory", "lav", "toilet", "water closet", "wc",
    "urinal", "bidet", "faucet", "drain", "floor drain", "fd",
    "cleanout", "co", "fco", "trap", "valve", "shut off",
    "gate valve", "ball valve", "check valve", "backflow",
    "hose bibb", "hb", "shower", "shower head", "tub",
    "water heater", "hwh", "expansion tank", "eyewash",
    "emergency shower", "mop sink", "service sink",
    "lavoratory", "fixture",
    # Fire protection
    "sprinkler", "head", "pendant", "upright", "sidewall",
    "fire extinguisher", "extinguisher", "hose station",
    "fire department connection", "fdc", "siamese",
    "alarm bell", "horn strobe", "smoke detector", "heat detector",
    "pull station", "tamper switch", "flow switch",
    "control valve", "ocv",
    # Mechanical / HVAC
    "diffuser", "register", "grille", "supply diffuser",
    "return register", "exhaust fan", "ef", "supply fan",
    "rtu", "ahu", "vav", "fcu", "fan coil", "hp",
    "heat pump", "condenser", "thermostat", "stat",
    "damper", "fire damper", "smoke damper", "louver",
    "gas unit heater", "guh", "unit heater",
    # Electrical
    "receptacle", "outlet", "duplex", "quad", "gfci",
    "switch", "dimmer", "junction box", "j-box", "jbox",
    "panel", "panelboard", "transformer", "xfmr",
    "disconnect", "starter", "vfd", "luminaire", "fixture",
    "light", "led", "troffer", "wall pack", "exit sign",
    "emergency light", "battery", "ups",
    "data outlet", "phone outlet", "tv outlet", "camera",
    "speaker", "horn", "strobe",
    # Generic
    "equipment", "unit", "device", "appliance", "machine",
    "ice machine", "refrigerator", "freezer", "dishwasher",
})

# --- LINEAR: lines, runs, paths ------------------------------
LINEAR_KEYWORDS: frozenset[str] = frozenset({
    # Plumbing piping
    "line", "pipe", "piping", "supply", "return", "main",
    "branch", "riser", "drop", "drain line", "vent line",
    "sewer", "sewer line", "sanitary", "sanitary line",
    "waste", "waste line", "cold water", "hot water",
    "cw", "hw", "hwr", "recirculating", "domestic",
    "storm", "gas", "gas line", "fuel oil",
    # Fire piping
    "fire main", "fire line", "fp main", "sprinkler line",
    "branch line", "feed main", "cross main",
    "standpipe", "standpipe riser",
    # Mechanical
    "duct", "ductwork", "supply duct", "return duct",
    "exhaust duct", "outside air", "refrigerant",
    "condensate line", "drain pan",
    # Electrical
    "conduit", "raceway", "wire", "wiring", "circuit",
    "feeder", "bus", "busway", "homerun",
    "cable tray", "cable", "low voltage", "lv",
    # Common modifiers
    "linetype", "line type",
})

# --- POLYGON: areas, zones, regions --------------------------
POLYGON_KEYWORDS: frozenset[str] = frozenset({
    "room", "area", "zone", "space", "slab", "floor",
    "ceiling", "roof", "wall", "partition", "deck",
    "hatch", "region", "boundary", "footprint",
    "tenant area", "leased area", "occupiable area",
    "warehouse", "office area", "gse area", "business area",
    "storage area", "restroom area", "shower area",
    # Sheet/zone-scoped labels
    "north wing", "south wing", "east wing", "west wing",
})

# --- NONE: glossary / abbreviations / notes ------------------
NONE_KEYWORDS: frozenset[str] = frozenset({
    # Construction abbreviations (the big audit finding)
    "continuous", "additional", "existing", "contractor",
    "typical", "alternate", "specification", "spec",
    "approximate", "approx", "verify", "field verify", "fv",
    "not in contract", "nic", "not to scale", "nts",
    "above finished floor", "aff", "below finished floor", "bff",
    "above finished grade", "afg", "centerline", "center line",
    "elevation", "section", "detail",
    "match line", "matchline",
    "callout", "tag", "marker", "reference",
    "north arrow", "scale bar", "note", "general note",
    "hex note", "keyed note", "key note", "schedule",
    # Drawing meta
    "legend", "linetype legend", "sheet index", "drawing index",
    "abbreviation", "abbreviations", "symbols list",
    "revision", "addendum", "issued",
    "by others", "by gc", "by owner", "by tenant", "by mc",
    "by ec", "by pc", "by fp",
    "required", "total required", "total",
    # Quantity/measurement words that aren't fixtures
    "net total", "subtotal", "sum",
    # N-4.7 TITLE-BLOCK additions — discipline/division abbreviations
    # that the N-4.5 forensics surfaced as residual UR cases.
    "bldg", "building",
    "fp",                       # "FP = - FIRE PROTECTION" discipline header
    "fire protection",          # discipline label, not a fixture
    "discipline",
    "drawing", "drawings", "dwg",
    "project", "project info", "info",
    "scale",                    # title-block scale field
    "scope of work", "scope",
    "name", "title",
    "room area", "if applicable",   # the '150 SF - ROOM AREA' near-miss
})


# ============================================================
# Legend classifier (rule-based, fast)
# ============================================================


# Priority: NONE > POLYGON > LINEAR > POINT > UNKNOWN
# Rationale: a row containing both "EXISTING SINK" should classify as
# POINT (sink wins) — but a pure "EXISTING" row classifies as NONE.
# We handle this by checking NONE only when there's no positive match
# in the other classes.


_PRIORITY_ORDER: tuple[tuple[GeometryClass, frozenset[str]], ...] = (
    # Check physical-entity classes first; only fall to NONE if none match
    (GeometryClass.POINT, POINT_KEYWORDS),
    (GeometryClass.LINEAR, LINEAR_KEYWORDS),
    (GeometryClass.POLYGON, POLYGON_KEYWORDS),
)


def _normalize(text: Optional[str]) -> str:
    return (text or "").lower().strip()


def classify_legend_geometry(
    symbol_id: Optional[str] = None,
    label: Optional[str] = None,
) -> GeometryClass:
    """Classify a legend row by scanning its symbol_id + label text.

    Strategy:
      1. If any physical-entity keyword (POINT/LINEAR/POLYGON) is
         present anywhere → return that class. Physical-entity wins
         over glossary even if BOTH are present (the row IS the
         physical entity; "existing" is just a modifier).
      2. Else if any NONE keyword present → return NONE.
      3. Else UNKNOWN (fail-open).
    """
    combined = (_normalize(symbol_id) + " " + _normalize(label)).strip()
    if not combined:
        return GeometryClass.UNKNOWN

    # Pass 1: physical entities (priority order)
    for cls, keywords in _PRIORITY_ORDER:
        for kw in keywords:
            # word-boundary match to avoid 'fan' matching 'fanout'
            # but allow phrases like "service sink" / "fire extinguisher"
            if " " in kw:
                if kw in combined:
                    return cls
            else:
                # whole-token match via word boundaries
                if re.search(rf"\b{re.escape(kw)}\b", combined):
                    return cls

    # Pass 2: glossary / notes
    for kw in NONE_KEYWORDS:
        if " " in kw:
            if kw in combined:
                return GeometryClass.NONE
        else:
            if re.search(rf"\b{re.escape(kw)}\b", combined):
                return GeometryClass.NONE

    return GeometryClass.UNKNOWN


# ============================================================
# Motif classifier (heuristic on bbox + primitives)
# ============================================================


@dataclass(frozen=True)
class MotifGeometryFeatures:
    """The minimal features needed to heuristically classify a motif.

    All fields except primitive_count default to safe values so callers
    can fill in what they have without breaking the function.
    """
    primitive_count: int = 0
    bbox_width: float = 0.0
    bbox_height: float = 0.0
    closed_area_ratio: float = 0.0     # 0..1 — what fraction of bbox is enclosed
    has_oriented_bbox: bool = False
    oriented_aspect_ratio: float = 0.0  # only valid if has_oriented_bbox=True


# Tunable thresholds. Conservative defaults — favor UNKNOWN over
# wrong classification.
LINEAR_ASPECT_RATIO: float = 6.0   # bbox max/min > 6 ⇒ very elongated
POLYGON_AREA_THRESHOLD: float = 5000.0  # sq pt; rooms typically much larger
POINT_AREA_THRESHOLD: float = 3000.0   # sq pt; fixtures typically small
POINT_PRIMITIVE_MAX: int = 12       # fixtures have few primitives
POLYGON_CLOSED_RATIO: float = 0.6   # if 60%+ of bbox is enclosed


def classify_motif_geometry(features: MotifGeometryFeatures) -> GeometryClass:
    """Classify a motif by its geometric features.

    Decision order (most-decisive feature first):
      1. Closed area dominates → POLYGON
      2. Very elongated bbox → LINEAR
      3. Small bbox + few primitives → POINT
      4. Otherwise → UNKNOWN (fail-open)

    Caller is expected to fill in whichever features are available;
    missing features get default values that don't trigger their rule.
    """
    w, h = features.bbox_width, features.bbox_height
    if w <= 0 or h <= 0:
        return GeometryClass.UNKNOWN

    area = w * h
    aspect = max(w, h) / max(min(w, h), 1e-6)

    # If we have an oriented bounding box (handles diagonal pipes),
    # prefer that aspect ratio over the axis-aligned one
    effective_aspect = (
        features.oriented_aspect_ratio
        if features.has_oriented_bbox and features.oriented_aspect_ratio > 0
        else aspect
    )

    # Rule 1: enclosed-area dominance → POLYGON
    if features.closed_area_ratio >= POLYGON_CLOSED_RATIO and area >= POLYGON_AREA_THRESHOLD:
        return GeometryClass.POLYGON

    # Rule 2: very elongated → LINEAR
    if effective_aspect >= LINEAR_ASPECT_RATIO:
        return GeometryClass.LINEAR

    # Rule 3: small + few primitives → POINT
    if (
        features.primitive_count > 0
        and features.primitive_count <= POINT_PRIMITIVE_MAX
        and area <= POINT_AREA_THRESHOLD
    ):
        return GeometryClass.POINT

    # Rule 4: a large bbox with many primitives AND high closed-area
    # ratio is also POLYGON (different fallback path)
    if features.closed_area_ratio >= POLYGON_CLOSED_RATIO:
        return GeometryClass.POLYGON

    return GeometryClass.UNKNOWN


# ============================================================
# Gate matrix
# ============================================================


def geometry_gate_compatible(
    motif: GeometryClass,
    legend: GeometryClass,
) -> bool:
    """Return True if the (motif, legend) geometry pair survives Gate #4.

    Implements the reviewer-locked matrix:
      - legend=NONE   → always REJECT (glossary rows are never bind targets)
      - UNKNOWN on either side → ALLOW (fail-open)
      - Same physical-entity class → ALLOW
      - Mismatched physical-entity classes → REJECT
    """
    # Rule 1: glossary legend rows are never bind targets
    if legend == GeometryClass.NONE:
        return False
    # (Motif=NONE shouldn't occur but be defensive)
    if motif == GeometryClass.NONE:
        return False

    # Rule 2: fail-open on UNKNOWN
    if motif == GeometryClass.UNKNOWN or legend == GeometryClass.UNKNOWN:
        return True

    # Rule 3: physical-entity classes — must match
    return motif == legend
