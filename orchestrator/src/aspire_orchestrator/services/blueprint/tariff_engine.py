"""Tariff engine — Section 232 (steel/aluminum) + Canadian softwood lumber detection.

Pattern-matches blueprint_materials.line_item text against keyword sets derived from
drew-tariff-rules.md and returns a TariffFlag enum value + estimated impact rate.

Law compliance:
  Law #1: No autonomous decisions — this module classifies only; Drew.procure() acts.
  Law #7: Pure functions — no side effects, no DB calls, no external I/O.
  Law #9: line_item text is input data; never log raw PII or full supplier blocks.

Detection priority: steel → aluminum → softwood → none.
The first matching category wins. Case-insensitive throughout.
"""

from __future__ import annotations

import re
from decimal import Decimal
from functools import lru_cache

from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag

# ---------------------------------------------------------------------------
# Tariff rates (as Decimal for precision in dollar-impact math)
# ---------------------------------------------------------------------------

_TARIFF_RATE: dict[TariffFlag, Decimal] = {
    TariffFlag.SECTION_232_STEEL: Decimal("50.0"),
    TariffFlag.SECTION_232_ALUMINUM: Decimal("35.2"),  # aluminum re-mapped below
    TariffFlag.SOFTWOOD_LUMBER: Decimal("35.2"),
    TariffFlag.NONE: Decimal("0.0"),
}

# Correct the aluminum rate to 50% per the KB
_TARIFF_RATE[TariffFlag.SECTION_232_ALUMINUM] = Decimal("50.0")

# ---------------------------------------------------------------------------
# Keyword sets (from drew-tariff-rules.md § Trigger Logic)
# ---------------------------------------------------------------------------

_STEEL_KEYWORDS: tuple[str, ...] = (
    "rebar",
    "deformed bar",
    "reinforcing bar",
    r"#\d+\s*bar",          # regex: #4 bar, #5 bar, etc.
    "structural steel",
    "wide flange",
    r"w-beam",
    r"s-beam",
    r"\bhss\b",
    "steel joist",
    "open web joist",
    r"\bowj\b",
    "lh series",
    "dlh series",
    "steel decking",
    "composite deck",
    "form deck",
    "roof deck",
    "metal stud",
    "cold-formed steel",
    r"\bcfs\b",
    "galvanized duct",
    "galvanized ductwork",
    "galvanized steel",
    "galvanized sheet",
    "steel pipe",
    r"steel.*schedule\s*40",
    r"steel.*schedule\s*80",
    r"schedule\s*40.*steel",
    r"schedule\s*80.*steel",
    "black iron pipe",
    "rigid metal conduit",
    r"\brmc\b",
    "intermediate metal conduit",
    r"\bimc\b",
    "wire mesh",
    "welded wire reinforcement",
    r"\bwwr\b",
    "wire lath",
    "metal lath",
    "steel grating",
    "bar grating",
    "unistrut",
    "strut channel",
    "threaded rod",
    "anchor bolt",
    "structural bolt",
    r"\ba325\b",
    r"\ba490\b",
    "huck bolt",
    "pipe pile",
    r"\bh-pile\b",
    "steel sheet pile",
    "steel handrail",
    "steel guardrail",
    "steel beam",
    "steel column",
    "steel plate",
    "steel angle",
    "steel channel",
    "steel tube",
    "hss tube",
    "corrugated metal",
    "steel nail",
    "steel nailer",
    "steel hardware",
    "steel fastener",
    "steel reinforcing",
    "steel reinforcement",
)

_ALUMINUM_KEYWORDS: tuple[str, ...] = (
    r"alum[iu]num storefront",
    r"alum[iu]num curtain wall",
    r"alum[iu]num window",
    r"alum[iu]num door",
    r"alum[iu]num frame",
    r"emt\s+alum[iu]num",
    r"alum[iu]num\s+emt",
    r"alum[iu]num conduit",
    "rigid aluminum conduit",
    r"\brac\b",
    r"alum[iu]num wire",
    r"alum[iu]num cable",
    r"alum[iu]num conductor",
    "service entrance aluminum",
    "sea cable",
    "ser aluminum",
    r"alum[iu]num roofing",
    r"alum[iu]num panel",
    "acp panel",
    "aluminum composite",
    r"alum[iu]num flashing",
    r"alum[iu]num handrail",
    r"alum[iu]num guardrail",
    r"alum[iu]num louver",
    r"alum[iu]num ladder",
    r"alum[iu]num coping",
    r"alum[iu]num soffit",
    r"alum[iu]num cable tray",
)

_SOFTWOOD_KEYWORDS: tuple[str, ...] = (
    "framing lumber",
    "dimensional lumber",
    "softwood lumber",
    r"\bspf\b",
    "spruce-pine-fir",
    "douglas fir",
    "hem-fir",
    "hemlock-fir",
    r"2\s*x\s*4",
    r"2\s*x\s*6",
    r"2\s*x\s*8",
    r"2\s*x\s*10",
    r"2\s*x\s*12",
    "wood stud",
    "lumber stud",
    "plate lumber",
    "sill plate",
    "top plate",
    "bottom plate",
    "header lumber",
    "roof rafter",
    "ceiling joist",
    "floor joist",
    "ridge board",
    "hip rafter",
    "valley rafter",
    "blocking lumber",
    "bridging lumber",
    "ledger board",
    "plywood sheathing",
    "structural plywood",
    "cdx plywood",
    "osb sheathing",
    "osb panel",
    "oriented strand board",
    r"\blvl\b",
    "laminated veneer lumber",
    r"\blsl\b",
    "laminated strand lumber",
    r"\bpsl\b",
    "parallel strand lumber",
    "wood i-joist",
    r"\btji\b",
    "glulam",
    "glue-laminated",
    "engineered lumber",
    "engineered wood",
    "wood beam",
    "wood header",
    "wood nailer",
    "deck board",
    "wood decking",
    "framing lumber",
    "lumber",        # broad catch — checked last within softwood group
)


# ---------------------------------------------------------------------------
# Compiled pattern cache (one-time cost, thread-safe via lru_cache)
# ---------------------------------------------------------------------------

def _compile(keywords: tuple[str, ...]) -> re.Pattern[str]:
    """Compile a union regex from keyword/pattern strings."""
    parts = "|".join(f"(?:{kw})" for kw in keywords)
    return re.compile(parts, re.IGNORECASE)


@lru_cache(maxsize=1)
def _steel_pattern() -> re.Pattern[str]:
    return _compile(_STEEL_KEYWORDS)


@lru_cache(maxsize=1)
def _aluminum_pattern() -> re.Pattern[str]:
    return _compile(_ALUMINUM_KEYWORDS)


@lru_cache(maxsize=1)
def _softwood_pattern() -> re.Pattern[str]:
    return _compile(_SOFTWOOD_KEYWORDS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_tariff_flag(line_item: str) -> TariffFlag:
    """Pattern-match a material line_item against Section 232 + softwood lumber rules.

    Detection priority: steel → aluminum → softwood → none.
    Case-insensitive. First match wins.

    Args:
        line_item: The material description text from blueprint_materials.line_item.

    Returns:
        TariffFlag enum value indicating which tariff regime applies, or NONE.

    Law #1: Pure classification — no autonomous actions. Caller decides what to do.
    Law #7: No DB access, no external I/O.
    """
    if not line_item:
        return TariffFlag.NONE

    text = line_item.strip()
    if not text:
        return TariffFlag.NONE

    if _steel_pattern().search(text):
        return TariffFlag.SECTION_232_STEEL

    if _aluminum_pattern().search(text):
        return TariffFlag.SECTION_232_ALUMINUM

    if _softwood_pattern().search(text):
        return TariffFlag.SOFTWOOD_LUMBER

    return TariffFlag.NONE


def estimate_tariff_impact_pct(flag: TariffFlag) -> Decimal:
    """Return the tariff rate percentage for a given flag.

    Args:
        flag: TariffFlag enum value.

    Returns:
        Decimal percentage (e.g., Decimal("50.0") for steel, Decimal("35.2") for softwood).

    Law #7: Pure function — no side effects.
    """
    return _TARIFF_RATE.get(flag, Decimal("0.0"))


def estimate_tariff_impact_usd(
    *,
    flag: TariffFlag,
    quantity: float | None,
    unit_cost_usd: float | None,
) -> float | None:
    """Compute estimated tariff dollar impact for a single material line.

    Formula: quantity × unit_cost_usd × (tariff_rate / 100)

    Returns None if either quantity or unit_cost_usd is missing/zero, so
    callers can distinguish "no tariff" (flag=NONE) from "tariff but no price yet."

    Args:
        flag: The tariff flag for this material.
        quantity: Material quantity (e.g., LF, SF, EA).
        unit_cost_usd: Unit cost in USD from supplier price lookup.

    Returns:
        Dollar impact float rounded to 2 decimal places, or None if inputs insufficient.

    Law #7: Pure function.
    """
    if flag == TariffFlag.NONE:
        return 0.0
    if quantity is None or unit_cost_usd is None:
        return None
    if quantity <= 0 or unit_cost_usd <= 0:
        return None
    rate = estimate_tariff_impact_pct(flag)
    impact = Decimal(str(quantity)) * Decimal(str(unit_cost_usd)) * (rate / Decimal("100"))
    return float(round(impact, 2))
