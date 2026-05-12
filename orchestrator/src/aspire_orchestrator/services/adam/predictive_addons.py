"""Predictive add-on rules for materials search — Pass C.

Rules are hardcoded (no SerpApi budget spent). The category detector uses
regex against the normalised query; add-ons are raw product title stubs that
the route returns as `addon_suggestions` alongside main search results.

Law #7: Tools are hands — this module does NOT call any external provider.
Law #9: No PII in add-on keys or product stubs.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Add-on catalogue keyed by detected category
# Each entry is a list of minimal product stub dicts the FE renders as chips.
# ---------------------------------------------------------------------------

ADDONS_BY_CATEGORY: dict[str, list[dict[str, Any]]] = {
    "paint": [
        {"title": "9-in Roller Cover", "category": "paint", "reason": "recommended with paint"},
        {"title": "Canvas Drop Cloth 9x12", "category": "paint", "reason": "floor protection"},
        {"title": "Painter\'s Tape 1.5-in", "category": "paint", "reason": "clean edge lines"},
        {"title": "Paint Tray + Liner 2pk", "category": "paint", "reason": "required with roller"},
    ],
    "drywall": [
        {"title": "All-Purpose Joint Compound 5-gal", "category": "drywall", "reason": "for taping seams"},
        {"title": "Drywall Paper Tape 300 ft", "category": "drywall", "reason": "standard seam tape"},
        {"title": "Corner Bead 8 ft", "category": "drywall", "reason": "outside corner finish"},
        {"title": "Drywall Screw Box 1lb", "category": "drywall", "reason": "required fasteners"},
    ],
    "roofing": [
        {"title": "Roofing Nails 1lb Coil", "category": "roofing", "reason": "required fasteners"},
        {"title": "Roof Deck Staples 5lb", "category": "roofing", "reason": "deck underlayment"},
        {"title": "Drip Edge Flashing 10 ft", "category": "roofing", "reason": "edge water diversion"},
        {"title": "Roofing Caulk Tube", "category": "roofing", "reason": "flashing seal"},
    ],
    "electrical": [
        {"title": "Wire Connectors 50pk (Marrette)", "category": "electrical", "reason": "required for splicing"},
        {"title": "Electrical Tape 3pk", "category": "electrical", "reason": "insulation standard"},
        {"title": "20A GFCI Outlet", "category": "electrical", "reason": "code-required wet areas"},
        {"title": "Conduit Staples 1/2-in 50pk", "category": "electrical", "reason": "cable management"},
    ],
    "plumbing": [
        {"title": "Teflon Tape 1/2-in 3pk", "category": "plumbing", "reason": "thread seal standard"},
        {"title": "PVC Primer + Cement Combo", "category": "plumbing", "reason": "DWV pipe joining"},
        {"title": "SharkBite Push Fit Coupling 1/2-in", "category": "plumbing", "reason": "quick repair"},
        {"title": "Plumbers Putty 14 oz", "category": "plumbing", "reason": "drain seal"},
    ],
    "flooring": [
        {"title": "Pull Bar + Tapping Block Kit", "category": "flooring", "reason": "LVP install tool"},
        {"title": "Underlayment Roll 100 sqft", "category": "flooring", "reason": "moisture + sound"},
        {"title": "Transition Strip 36-in", "category": "flooring", "reason": "room-to-room finish"},
        {"title": "Flooring Spacers 40pk", "category": "flooring", "reason": "expansion gap tool"},
    ],
    "concrete": [
        {"title": "Concrete Float Magnesium 16-in", "category": "concrete", "reason": "surface finish"},
        {"title": "Concrete Forms Snap Tie 6-in 50pk", "category": "concrete", "reason": "formwork"},
        {"title": "Concrete Bonding Adhesive Qt", "category": "concrete", "reason": "new-to-old bond"},
        {"title": "Plastic Sheeting 10x25 4mil", "category": "concrete", "reason": "vapor barrier"},
    ],
    "hvac": [
        {"title": "HVAC Foil Tape 2.5-in", "category": "hvac", "reason": "duct sealing"},
        {"title": "Fiberglass Duct Wrap R-6", "category": "hvac", "reason": "duct insulation"},
        {"title": "Duct Mastic Sealant 1-gal", "category": "hvac", "reason": "leak sealing"},
        {"title": "Pleated Air Filter MERV-11 20x20x1", "category": "hvac", "reason": "air quality"},
    ],
    "tools": [
        {"title": "Safety Glasses ANSI Z87", "category": "tools", "reason": "PPE required"},
        {"title": "Work Gloves L/XL", "category": "tools", "reason": "PPE standard"},
        {"title": "Measuring Tape 25 ft", "category": "tools", "reason": "layout standard"},
        {"title": "Pencils 12pk", "category": "tools", "reason": "marking standard"},
    ],
}

# ---------------------------------------------------------------------------
# Category detection via regex (query → category key)
# ---------------------------------------------------------------------------

_CATEGORY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("paint", re.compile(r"paint|primer|roller|brush|latex|enamel|stain|sealant", re.I)),
    ("drywall", re.compile(r"drywall|sheetrock|joint|mud|gypsum|plaster", re.I)),
    ("roofing", re.compile(r"roof|shingle|felt|flashing|eave|fascia|ridge", re.I)),
    ("electrical", re.compile(r"electr|wire|conduit|outlet|breaker|romex|switch|panel", re.I)),
    ("plumbing", re.compile(r"plumb|pipe|drain|faucet|valve|pvc|cpvc|pex|fitting", re.I)),
    ("flooring", re.compile(r"floor|tile|vinyl|lvp|laminate|hardwood|grout|underlayment", re.I)),
    ("concrete", re.compile(r"concrete|cement|mortar|grout|rebar|block|masonry|footing", re.I)),
    ("hvac", re.compile(r"hvac|duct|furnace|air\s*handler|compressor|refrigerant|insulation", re.I)),
    ("tools", re.compile(r"drill|saw|hammer|screwdriver|wrench|level|nailer|tool\b", re.I)),
]


def detect_category(query_normalized: str) -> str | None:
    """Detect the primary trade category from a normalised query string.

    Returns the category key (e.g. 'paint') or None if no match.
    First match wins — order in _CATEGORY_PATTERNS is precedence.
    """
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(query_normalized):
            return category
    return None


def get_predictive_addons(query_normalized: str) -> list[dict[str, Any]]:
    """Return predictive add-on product stubs for the given query.

    No provider calls made. Returns empty list when category is undetected.
    Caps at 4 add-ons per query.
    """
    category = detect_category(query_normalized)
    if category is None:
        return []
    return ADDONS_BY_CATEGORY.get(category, [])[:4]
