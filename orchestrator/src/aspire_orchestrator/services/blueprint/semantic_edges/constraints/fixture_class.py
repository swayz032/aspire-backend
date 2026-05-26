"""M-4 substrate — canonical fixture taxonomy + keyword classifier.

Two orthogonal enums compose to represent MEP entities:

  FixtureRole  — what an entity IS (source/terminal/distribution/control/...)
  FlowStream   — what FLOWS through it (supply air, waste water, power, ...)

A fixture has ONE role per "side" (can be SUPPLY_TERMINAL + RETURN_TERMINAL
for things like sinks) and a set of streams it participates in. This
keeps the connectivity rule simple: edges must share a stream AND must
respect direction (when both roles are known and neither is DISTRIBUTION).

FIXTURE_KEYWORDS is the canonical source-of-truth label → (roles, streams)
map. ~35 entries spanning plumbing / HVAC / electrical / low-voltage /
fire / metering. Drew's legend labels (e.g. "QUAD RECEPTACLE", "RETURN
GRILLE", "EXISTING UPRIGHT SPRINKLER") are matched via case-insensitive
substring against the keyword.

Drift detector: M-4 connectivity constraint AND any future audit
verifier must import from THIS module. Single source of truth.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


# ============================================================
# FixtureRole — what an entity IS
# ============================================================


class FixtureRole(str, Enum):
    """Functional role an entity plays in an MEP system."""
    SUPPLY_SOURCE     = "supply_source"     # panel, AHU, water heater, water main
    SUPPLY_TERMINAL   = "supply_terminal"   # outlet, faucet, diffuser, light, sprinkler head
    RETURN_TERMINAL   = "return_terminal"   # return grille, floor drain, waste tie-in
    RETURN_SINK       = "return_sink"       # sewer main, sump, grease trap
    DISTRIBUTION      = "distribution"      # pipe, duct, conduit, raceway
    CONTROL           = "control"           # switch, valve, damper, VFD, thermostat
    PROTECTION        = "protection"        # breaker, disconnect, OS&Y valve, backflow preventer
    METERING          = "metering"          # electric meter, water meter, flow meter
    UNKNOWN           = "unknown"


# ============================================================
# FlowStream — what FLOWS through it
# ============================================================


class FlowStream(str, Enum):
    """Substance / signal that flows. Used for compatibility intersection."""
    SUPPLY_AIR        = "supply_air"
    RETURN_AIR        = "return_air"
    SUPPLY_WATER_HOT  = "supply_water_hot"
    SUPPLY_WATER_COLD = "supply_water_cold"
    WASTE_WATER       = "waste_water"
    VENT_GAS          = "vent_gas"
    POWER_HIGH_VOLT   = "power_high_volt"     # > 50V — typical 120/240/480
    POWER_LOW_VOLT    = "power_low_volt"      # < 50V — controls / fire alarm
    SIGNAL_FIRE       = "signal_fire"         # fire alarm / detection wiring
    SIGNAL_DATA       = "signal_data"         # network / telecom


# Streams whose natural direction is SOURCE → TERMINAL ("feeds" something)
SUPPLY_STREAMS: frozenset[FlowStream] = frozenset({
    FlowStream.SUPPLY_AIR,
    FlowStream.SUPPLY_WATER_HOT,
    FlowStream.SUPPLY_WATER_COLD,
    FlowStream.POWER_HIGH_VOLT,
    FlowStream.POWER_LOW_VOLT,
    FlowStream.SIGNAL_FIRE,
    FlowStream.SIGNAL_DATA,
})

# Streams whose natural direction is TERMINAL → SINK ("returns to" something)
RETURN_STREAMS: frozenset[FlowStream] = frozenset({
    FlowStream.RETURN_AIR,
    FlowStream.WASTE_WATER,
    FlowStream.VENT_GAS,
})

# Source-side roles for FEEDS edges (where supply originates or transits)
SOURCE_SIDE_ROLES: frozenset[FixtureRole] = frozenset({
    FixtureRole.SUPPLY_SOURCE,
    FixtureRole.DISTRIBUTION,
    FixtureRole.CONTROL,        # switches / VAVs sit in-line
    FixtureRole.PROTECTION,     # breakers / disconnects sit in-line
})

# Sink-side roles for FEEDS edges (where supply terminates)
SINK_SIDE_ROLES: frozenset[FixtureRole] = frozenset({
    FixtureRole.SUPPLY_TERMINAL,
    FixtureRole.DISTRIBUTION,
    FixtureRole.CONTROL,
    FixtureRole.PROTECTION,
})

# Source-side for RETURNS_TO edges
RETURN_SOURCE_ROLES: frozenset[FixtureRole] = frozenset({
    FixtureRole.RETURN_TERMINAL,
    FixtureRole.DISTRIBUTION,
    FixtureRole.SUPPLY_TERMINAL,  # sinks / toilets are both supply AND return start
})

# Sink-side for RETURNS_TO
RETURN_SINK_ROLES: frozenset[FixtureRole] = frozenset({
    FixtureRole.RETURN_SINK,
    FixtureRole.DISTRIBUTION,
})


# ============================================================
# Fixture profile — roles + streams for a given fixture type
# ============================================================


class FixtureProfile:
    """Immutable bundle of (roles, streams). Compared by value."""

    __slots__ = ("roles", "streams")

    def __init__(self, roles: frozenset[FixtureRole], streams: frozenset[FlowStream]):
        self.roles = roles
        self.streams = streams

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FixtureProfile):
            return NotImplemented
        return self.roles == other.roles and self.streams == other.streams

    def __hash__(self) -> int:
        return hash((self.roles, self.streams))

    def __repr__(self) -> str:
        rs = sorted(r.value for r in self.roles)
        ss = sorted(s.value for s in self.streams)
        return f"FixtureProfile(roles={rs}, streams={ss})"


def _profile(*roles: FixtureRole, streams: frozenset[FlowStream]) -> FixtureProfile:
    return FixtureProfile(frozenset(roles), streams)


# ============================================================
# Canonical FIXTURE_KEYWORDS — single source of truth
# ============================================================
# Match strategy: case-insensitive substring against the label/text.
# Order matters: more-specific keywords first so "RETURN GRILLE" beats
# "GRILLE" alone.

_R = FixtureRole
_S = FlowStream

FIXTURE_KEYWORDS: list[tuple[str, FixtureProfile]] = [

    # ---------- Plumbing (12 entries) ----------
    ("WATER HEATER", _profile(
        _R.SUPPLY_SOURCE, _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SUPPLY_WATER_HOT, _S.SUPPLY_WATER_COLD}),
    )),
    ("BACKFLOW PREVENTER", _profile(
        _R.PROTECTION,
        streams=frozenset({_S.SUPPLY_WATER_COLD}),
    )),
    ("GREASE TRAP", _profile(
        _R.RETURN_SINK,
        streams=frozenset({_S.WASTE_WATER}),
    )),
    ("GREASE INTERCEPTOR", _profile(
        _R.RETURN_SINK,
        streams=frozenset({_S.WASTE_WATER}),
    )),
    ("WATER METER", _profile(
        _R.METERING, _R.SUPPLY_SOURCE,
        streams=frozenset({_S.SUPPLY_WATER_COLD}),
    )),
    ("FLOOR DRAIN", _profile(
        _R.RETURN_TERMINAL,
        streams=frozenset({_S.WASTE_WATER}),
    )),
    ("FLOOR SINK", _profile(
        _R.RETURN_TERMINAL,
        streams=frozenset({_S.WASTE_WATER}),
    )),
    # Lavatory / sink — supply + return on hot/cold/waste/vent
    ("LAVATORY", _profile(
        _R.SUPPLY_TERMINAL, _R.RETURN_TERMINAL,
        streams=frozenset({
            _S.SUPPLY_WATER_HOT, _S.SUPPLY_WATER_COLD,
            _S.WASTE_WATER, _S.VENT_GAS,
        }),
    )),
    ("SINK", _profile(
        _R.SUPPLY_TERMINAL, _R.RETURN_TERMINAL,
        streams=frozenset({
            _S.SUPPLY_WATER_HOT, _S.SUPPLY_WATER_COLD,
            _S.WASTE_WATER, _S.VENT_GAS,
        }),
    )),
    # Toilet / WC — cold + waste + vent (no hot)
    ("WATER CLOSET", _profile(
        _R.SUPPLY_TERMINAL, _R.RETURN_TERMINAL,
        streams=frozenset({
            _S.SUPPLY_WATER_COLD, _S.WASTE_WATER, _S.VENT_GAS,
        }),
    )),
    ("TOILET", _profile(
        _R.SUPPLY_TERMINAL, _R.RETURN_TERMINAL,
        streams=frozenset({
            _S.SUPPLY_WATER_COLD, _S.WASTE_WATER, _S.VENT_GAS,
        }),
    )),
    ("URINAL", _profile(
        _R.SUPPLY_TERMINAL, _R.RETURN_TERMINAL,
        streams=frozenset({
            _S.SUPPLY_WATER_COLD, _S.WASTE_WATER, _S.VENT_GAS,
        }),
    )),
    ("SHOWER", _profile(
        _R.SUPPLY_TERMINAL, _R.RETURN_TERMINAL,
        streams=frozenset({
            _S.SUPPLY_WATER_HOT, _S.SUPPLY_WATER_COLD,
            _S.WASTE_WATER, _S.VENT_GAS,
        }),
    )),
    ("BATHTUB", _profile(
        _R.SUPPLY_TERMINAL, _R.RETURN_TERMINAL,
        streams=frozenset({
            _S.SUPPLY_WATER_HOT, _S.SUPPLY_WATER_COLD,
            _S.WASTE_WATER, _S.VENT_GAS,
        }),
    )),
    ("FAUCET", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SUPPLY_WATER_HOT, _S.SUPPLY_WATER_COLD}),
    )),
    ("HOSE BIBB", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SUPPLY_WATER_COLD}),
    )),
    ("WASTE PIPE", _profile(
        _R.DISTRIBUTION,
        streams=frozenset({_S.WASTE_WATER}),
    )),
    ("SANITARY PIPE", _profile(
        _R.DISTRIBUTION,
        streams=frozenset({_S.WASTE_WATER}),
    )),
    ("VENT PIPE", _profile(
        _R.DISTRIBUTION,
        streams=frozenset({_S.VENT_GAS}),
    )),

    # ---------- HVAC (10 entries) ----------
    ("AIR HANDLING UNIT", _profile(
        _R.SUPPLY_SOURCE, _R.RETURN_SINK,
        streams=frozenset({_S.SUPPLY_AIR, _S.RETURN_AIR}),
    )),
    ("ROOFTOP UNIT", _profile(
        _R.SUPPLY_SOURCE, _R.RETURN_SINK,
        streams=frozenset({_S.SUPPLY_AIR, _S.RETURN_AIR}),
    )),
    ("VAV", _profile(
        _R.CONTROL,
        streams=frozenset({_S.SUPPLY_AIR}),
    )),
    ("SUPPLY DIFFUSER", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SUPPLY_AIR}),
    )),
    ("DIFFUSER", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SUPPLY_AIR}),
    )),
    ("RETURN GRILLE", _profile(
        _R.RETURN_TERMINAL,
        streams=frozenset({_S.RETURN_AIR}),
    )),
    ("EXHAUST GRILLE", _profile(
        _R.RETURN_TERMINAL,
        streams=frozenset({_S.RETURN_AIR}),
    )),
    ("REGISTER", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SUPPLY_AIR}),
    )),
    ("DAMPER", _profile(
        _R.CONTROL,
        streams=frozenset({_S.SUPPLY_AIR, _S.RETURN_AIR}),
    )),
    ("THERMOSTAT", _profile(
        _R.CONTROL,
        streams=frozenset({_S.SIGNAL_DATA, _S.POWER_LOW_VOLT}),
    )),
    ("SUPPLY DUCT", _profile(
        _R.DISTRIBUTION,
        streams=frozenset({_S.SUPPLY_AIR}),
    )),
    ("RETURN DUCT", _profile(
        _R.DISTRIBUTION,
        streams=frozenset({_S.RETURN_AIR}),
    )),

    # ---------- Electrical (10 entries) ----------
    ("MAIN PANEL", _profile(
        _R.SUPPLY_SOURCE,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("PANELBOARD", _profile(
        _R.SUPPLY_SOURCE,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("PANEL", _profile(
        _R.SUPPLY_SOURCE,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("ELECTRIC METER", _profile(
        _R.METERING, _R.SUPPLY_SOURCE,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("TRANSFORMER", _profile(
        _R.SUPPLY_SOURCE, _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("DISCONNECT", _profile(
        _R.PROTECTION,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("BREAKER", _profile(
        _R.PROTECTION,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("RECEPTACLE", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("OUTLET", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("DUPLEX", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("QUAD RECEPTACLE", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("GFCI", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("LIGHT FIXTURE", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("LUMINAIRE", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("JUNCTION BOX", _profile(
        _R.DISTRIBUTION,
        streams=frozenset({_S.POWER_HIGH_VOLT, _S.POWER_LOW_VOLT}),
    )),
    ("J-BOX", _profile(
        _R.DISTRIBUTION,
        streams=frozenset({_S.POWER_HIGH_VOLT, _S.POWER_LOW_VOLT}),
    )),
    # NOTE: a bare "SWITCH" is ambiguous (electrical vs network). Default
    # to electrical CONTROL since the network case is almost always
    # written as NETWORK SWITCH.
    ("SWITCH", _profile(
        _R.CONTROL,
        streams=frozenset({_S.POWER_HIGH_VOLT}),
    )),
    ("CONDUIT", _profile(
        _R.DISTRIBUTION,
        streams=frozenset({_S.POWER_HIGH_VOLT, _S.POWER_LOW_VOLT}),
    )),

    # ---------- Low-voltage / data (5 entries) ----------
    ("DATA JACK", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SIGNAL_DATA}),
    )),
    ("ACCESS POINT", _profile(
        _R.SUPPLY_SOURCE, _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SIGNAL_DATA, _S.POWER_LOW_VOLT}),
    )),
    ("NETWORK SWITCH", _profile(
        _R.SUPPLY_SOURCE,
        streams=frozenset({_S.SIGNAL_DATA}),
    )),
    ("IDF", _profile(
        _R.SUPPLY_SOURCE,
        streams=frozenset({_S.SIGNAL_DATA}),
    )),
    ("MDF", _profile(
        _R.SUPPLY_SOURCE,
        streams=frozenset({_S.SIGNAL_DATA}),
    )),

    # ---------- Fire protection (6 entries) ----------
    ("FACP", _profile(
        _R.SUPPLY_SOURCE,
        streams=frozenset({_S.SIGNAL_FIRE, _S.POWER_LOW_VOLT}),
    )),
    ("FIRE ALARM CONTROL PANEL", _profile(
        _R.SUPPLY_SOURCE,
        streams=frozenset({_S.SIGNAL_FIRE, _S.POWER_LOW_VOLT}),
    )),
    ("HORN STROBE", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SIGNAL_FIRE}),
    )),
    ("SMOKE DETECTOR", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SIGNAL_FIRE}),
    )),
    ("HEAT DETECTOR", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SIGNAL_FIRE}),
    )),
    ("PULL STATION", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SIGNAL_FIRE}),
    )),
    ("SPRINKLER HEAD", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SUPPLY_WATER_COLD}),
    )),
    ("SPRINKLER", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SUPPLY_WATER_COLD}),
    )),
    ("PENDANT", _profile(
        _R.SUPPLY_TERMINAL,
        streams=frozenset({_S.SUPPLY_WATER_COLD}),
    )),
    ("STANDPIPE", _profile(
        _R.DISTRIBUTION,
        streams=frozenset({_S.SUPPLY_WATER_COLD}),
    )),
]


# ============================================================
# Classifier
# ============================================================


def classify_fixture(label: Optional[str]) -> Optional[FixtureProfile]:
    """Match a label/text against FIXTURE_KEYWORDS, returning the
    first matching FixtureProfile.

    Case-insensitive substring match. Order in FIXTURE_KEYWORDS matters:
    longer/more-specific keywords are listed first so 'RETURN GRILLE'
    matches before 'GRILLE' alone.

    Returns None if no keyword matches (caller issues SOFT_SKIP).
    """
    if not label:
        return None
    text_up = label.upper()
    for keyword, profile in FIXTURE_KEYWORDS:
        if keyword in text_up:
            return profile
    return None


def streams_share(
    a: Optional[frozenset[FlowStream]],
    b: Optional[frozenset[FlowStream]],
    *,
    restrict_to: Optional[frozenset[FlowStream]] = None,
) -> bool:
    """True iff a and b share at least one stream (optionally filtered).

    When restrict_to is provided, the intersection is narrowed first
    (e.g. for FEEDS edges we only care about SUPPLY_STREAMS).
    """
    if not a or not b:
        return False
    inter = a & b
    if restrict_to is not None:
        inter = inter & restrict_to
    return bool(inter)


def role_compatible_for_feeds(
    src_roles: frozenset[FixtureRole],
    dst_roles: frozenset[FixtureRole],
) -> bool:
    """True iff src + dst roles align for a FEEDS edge (Option C
    conditional strictness — caller decides whether to invoke this)."""
    src_ok = bool(src_roles & SOURCE_SIDE_ROLES)
    dst_ok = bool(dst_roles & SINK_SIDE_ROLES)
    return src_ok and dst_ok


def role_compatible_for_returns(
    src_roles: frozenset[FixtureRole],
    dst_roles: frozenset[FixtureRole],
) -> bool:
    src_ok = bool(src_roles & RETURN_SOURCE_ROLES)
    dst_ok = bool(dst_roles & RETURN_SINK_ROLES)
    return src_ok and dst_ok
