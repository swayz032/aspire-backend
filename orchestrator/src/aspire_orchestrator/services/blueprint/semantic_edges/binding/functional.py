"""N-4 — FunctionalRole + ConnectivityScorer.

The orthogonal evidence layer. After OCR (text) and Topology (shape),
N-4 asks: does the motif's functional role match the legend's?

A POINT-shaped sink, a POINT-shaped valve, and a POINT-shaped sprinkler
head all pass the geometry gate identically. But functionally:
  - sink              = SINK
  - valve             = JUNCTION
  - sprinkler head    = SINK
  - panel             = SOURCE
  - pipe              = DISTRIBUTION

Functional role is orthogonal to:
  - discipline (plumbing vs electrical) — different dimension
  - geometry  (point vs linear vs polygon) — different dimension
  - OCR text  (label match) — different dimension
  - topology  (primitive histogram) — different dimension

This scorer slots into the weighted product alongside the others.

Coarse 4-class taxonomy (reviewer-locked 2026-05-26)
---------------------------------------------------
  SOURCE       feeds something  (panel, AHU, water heater, water main)
  SINK         consumes / terminates (sink, outlet, sprinkler head,
                                     drain, fixture, light)
  JUNCTION     connection point (valve, switch, cleanout, J-box, damper)
  DISTRIBUTION transports       (pipe, line, conduit, duct, wire, branch)
  UNKNOWN      cannot determine — fails OPEN (multiplicative identity)

Compatibility matrix (motif × legend):
                | SOURCE | SINK   | JUNCT  | DISTR  | UNKNOWN
    SOURCE      | 1.0    | 0.0    | 0.0    | 0.0    | 1.0
    SINK        | 0.0    | 1.0    | 1.0    | 0.0    | 1.0
    JUNCTION    | 0.0    | 1.0    | 1.0    | 0.0    | 1.0
    DISTRIBUTION| 0.0    | 0.0    | 0.0    | 1.0    | 1.0
    UNKNOWN     | 1.0    | 1.0    | 1.0    | 1.0    | 1.0

SINK and JUNCTION are mutually compatible because in practice a POINT
motif (like a small symbol) can plausibly be either a fixture (sink)
or a valve (junction) — without deeper evidence, we don't differentiate.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from .base import BaseScorer, BindingProposal, ScorerResult
from .geometry import GeometryClass


class FunctionalRole(str, Enum):
    """Coarse 4-class functional taxonomy + UNKNOWN."""
    SOURCE = "source"
    SINK = "sink"
    JUNCTION = "junction"
    DISTRIBUTION = "distribution"
    UNKNOWN = "unknown"


# ============================================================
# Keyword sets — rule-based legend classifier
# ============================================================

SOURCE_KEYWORDS: frozenset[str] = frozenset({
    # Electrical sources
    "panel", "panelboard", "transformer", "xfmr", "switchgear",
    "switchboard", "ups", "battery", "generator", "service",
    # Mechanical sources
    "ahu", "air handler", "rtu", "rooftop unit", "boiler", "chiller",
    "heat pump", "hp", "water heater", "hwh", "furnace",
    # Plumbing sources
    "water main", "service entrance", "tank", "well", "pump",
    # Fire sources
    "fdc", "fire department connection", "fire main", "tank",
})

SINK_KEYWORDS: frozenset[str] = frozenset({
    # Plumbing terminals
    "sink", "lavatory", "lav", "toilet", "wc", "water closet",
    "urinal", "bidet", "faucet", "tub", "shower", "shower head",
    "drain", "floor drain", "fd", "trap", "eyewash",
    "service sink", "mop sink", "emergency shower",
    # Electrical terminals
    "receptacle", "outlet", "duplex", "quad", "gfci",
    "luminaire", "fixture", "light", "led", "troffer",
    "wall pack", "exit sign", "emergency light",
    "data outlet", "phone outlet", "tv outlet",
    # Mechanical terminals
    "diffuser", "register", "grille", "supply diffuser",
    "return register", "exhaust fan", "ef", "vent",
    # Fire terminals
    "sprinkler", "head", "pendant", "upright", "sidewall",
    "alarm bell", "horn", "horn strobe", "speaker", "strobe",
    "smoke detector", "heat detector", "pull station",
    "fire extinguisher", "extinguisher",
})

JUNCTION_KEYWORDS: frozenset[str] = frozenset({
    # Connection / control points
    "valve", "gate valve", "ball valve", "check valve",
    "shut off", "shut-off", "shutoff", "switch", "dimmer",
    "thermostat", "stat", "damper", "fire damper",
    "smoke damper", "cleanout", "co", "fco",
    "junction box", "j-box", "jbox",
    "starter", "disconnect", "vfd", "tamper switch",
    "flow switch", "control valve", "ocv",
    "backflow", "backflow preventer", "hose bibb", "hb",
    # Detail / callouts that mark a connection
    "tee", "elbow", "coupling", "union", "reducer",
})

DISTRIBUTION_KEYWORDS: frozenset[str] = frozenset({
    # Plumbing distribution
    "pipe", "piping", "line", "main", "branch", "riser", "drop",
    "supply", "return", "hot water", "cold water", "cw", "hw", "hwr",
    "recirculating", "domestic", "sanitary", "sewer", "waste",
    "vent line", "drain line", "storm", "gas line",
    # Fire distribution
    "fire main", "sprinkler line", "branch line", "feed main",
    "cross main", "standpipe", "fp main",
    # Mechanical distribution
    "duct", "ductwork", "supply duct", "return duct",
    "exhaust duct", "refrigerant", "condensate line",
    # Electrical distribution
    "conduit", "raceway", "wire", "wiring", "cable", "cable tray",
    "circuit", "feeder", "bus", "busway", "homerun",
    "low voltage", "lv",
    # Generic linetypes
    "linetype",
})


# Priority: physical-function wins over generic words
_PRIORITY: tuple[tuple[FunctionalRole, frozenset[str]], ...] = (
    (FunctionalRole.SINK, SINK_KEYWORDS),
    (FunctionalRole.DISTRIBUTION, DISTRIBUTION_KEYWORDS),
    (FunctionalRole.JUNCTION, JUNCTION_KEYWORDS),
    (FunctionalRole.SOURCE, SOURCE_KEYWORDS),
)


def _norm(text: Optional[str]) -> str:
    return (text or "").lower().strip()


def classify_legend_function(
    symbol_id: Optional[str] = None,
    label: Optional[str] = None,
) -> FunctionalRole:
    """Map legend text to functional role. Priority: SINK > DISTRIBUTION
    > JUNCTION > SOURCE > UNKNOWN. SINK wins because most legend rows
    are terminal fixtures."""
    combined = (_norm(symbol_id) + " " + _norm(label)).strip()
    if not combined:
        return FunctionalRole.UNKNOWN

    for cls, keywords in _PRIORITY:
        for kw in keywords:
            if " " in kw:
                if kw in combined:
                    return cls
            else:
                if re.search(rf"\b{re.escape(kw)}\b", combined):
                    return cls
    return FunctionalRole.UNKNOWN


# ============================================================
# Motif classifier — derived from geometry (fail-open)
# ============================================================


# Mapping motif geometry -> most-likely functional role.
# Deliberately coarse: real differentiation needs more evidence,
# so we use UNKNOWN liberally to fail-open.
_GEOMETRY_TO_FUNCTION: dict[str, FunctionalRole] = {
    "linear": FunctionalRole.DISTRIBUTION,
    # POINT motifs are commonly SINK (terminal fixtures) OR JUNCTION
    # (valves, switches, cleanouts). The compat matrix below treats
    # SINK <-> JUNCTION as compatible, so this mapping is safe.
    "point": FunctionalRole.SINK,
    "polygon": FunctionalRole.UNKNOWN,    # rooms / zones — not classic MEP roles
    "none": FunctionalRole.UNKNOWN,
    "unknown": FunctionalRole.UNKNOWN,
}


def classify_motif_function(geometry: GeometryClass) -> FunctionalRole:
    """Derive a functional role from motif geometry class.

    Coarse mapping — fails open for polygon/none/unknown.
    """
    return _GEOMETRY_TO_FUNCTION.get(geometry.value, FunctionalRole.UNKNOWN)


# ============================================================
# Compatibility matrix
# ============================================================


# Locked matrix (reviewer 2026-05-26):
#   - same role  -> 1.0
#   - SINK <-> JUNCTION -> 1.0 (POINT motifs can be either)
#   - any-vs-UNKNOWN -> 1.0 (fail-open)
#   - any other mismatch -> 0.0
_COMPATIBILITY: dict[tuple[FunctionalRole, FunctionalRole], float] = {}


def _init_matrix() -> None:
    roles = list(FunctionalRole)
    for a in roles:
        for b in roles:
            if a == b:
                _COMPATIBILITY[(a, b)] = 1.0
            elif a == FunctionalRole.UNKNOWN or b == FunctionalRole.UNKNOWN:
                _COMPATIBILITY[(a, b)] = 1.0
            elif {a, b} == {FunctionalRole.SINK, FunctionalRole.JUNCTION}:
                _COMPATIBILITY[(a, b)] = 1.0
            else:
                _COMPATIBILITY[(a, b)] = 0.0


_init_matrix()


def functional_compatibility(
    motif: FunctionalRole, legend: FunctionalRole,
) -> float:
    """Return the compatibility score in {0.0, 1.0} from the locked matrix."""
    return _COMPATIBILITY[(motif, legend)]


# ============================================================
# Scorer
# ============================================================


class ConnectivityScorer(BaseScorer):
    """Score motif↔legend functional compatibility.

    Reads motif geometry from proposal.motif_entity_geometry and
    classifies legend from proposal.legend_label + legend_symbol_id.
    """

    name = "connectivity"
    weight_key = "connectivity"

    def compute(self, proposal: BindingProposal) -> ScorerResult:
        # Motif side — derive from geometry class
        try:
            motif_geom = GeometryClass(proposal.motif_entity_geometry)
        except ValueError:
            motif_geom = GeometryClass.UNKNOWN
        motif_func = classify_motif_function(motif_geom)

        # Legend side — keyword scan
        legend_func = classify_legend_function(
            symbol_id=proposal.legend_symbol_id,
            label=proposal.legend_label,
        )

        score = functional_compatibility(motif_func, legend_func)

        if motif_func == FunctionalRole.UNKNOWN or legend_func == FunctionalRole.UNKNOWN:
            reasoning = "insufficient_data"
        elif score == 1.0:
            reasoning = f"compatible_{motif_func.value}_{legend_func.value}"
        else:
            reasoning = f"mismatch_{motif_func.value}_vs_{legend_func.value}"

        return self._result(
            score, reasoning,
            {
                "motif_function": motif_func.value,
                "legend_function": legend_func.value,
            },
        )
