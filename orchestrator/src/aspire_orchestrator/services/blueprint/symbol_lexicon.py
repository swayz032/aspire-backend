"""Wave 17b.2 — Curated symbol lexicon for spatial OCR → symbol binding.

Maps blueprint text labels (the strings the architect actually wrote next to
the symbol — "GFCI", "WC", "PNL", "RECEPT", "DUPLEX", "HB", "SD", ...) to
canonical Aspire symbol names.

The lexicon is discipline-grouped so the binder can prefer labels that match
the sheet's tagged discipline when two candidates tie on distance/confidence.

Design notes
------------
* All lookups are normalized: uppercased, stripped of trailing punctuation,
  collapsed whitespace. The caller does NOT need to pre-normalize.
* Modifier-bearing terms ("GFCI RECEPT" vs "RECEPT") resolve longest-match
  first via ``lookup``. The longest matching substring of the OCR label wins.
* This file is dependency-free. No I/O, no DB. Pure data + pure functions.

Law compliance:
  #1 — Pure data. No autonomous decisions. The binder applies these mappings.
  #6 — Lexicon is global; binding to a tenant happens upstream.
  #9 — No PII; just industry terms.
"""
from __future__ import annotations

import re
from typing import Final

# Discipline buckets ---------------------------------------------------------
# E (electrical), P (plumbing), M (mechanical/HVAC), FP (fire protection),
# A (architectural), S (structural), L (low-voltage / data / security)
#
# Each entry maps a raw label (uppercase, possibly with spaces) to a canonical
# Aspire symbol name. Order within a discipline does not matter; longest-match
# is computed at lookup time.

_ELECTRICAL: Final[dict[str, str]] = {
    "GFCI": "gfci_receptacle",
    "GFCI RECEPT": "gfci_receptacle",
    "GFCI RECEPTACLE": "gfci_receptacle",
    "GFI": "gfci_receptacle",
    "IG": "isolated_ground_receptacle",
    "IG RECEPT": "isolated_ground_receptacle",
    "WP": "weatherproof_receptacle",
    "WP GFCI": "weatherproof_gfci_receptacle",
    "DUPLEX": "duplex_receptacle",
    "DPLX": "duplex_receptacle",
    "RECEPT": "duplex_receptacle",
    "RECEPTACLE": "duplex_receptacle",
    "OUTLET": "duplex_receptacle",
    "QUAD": "quad_receptacle",
    "QUADPLEX": "quad_receptacle",
    "220V": "240v_receptacle",
    "240V": "240v_receptacle",
    "USB": "usb_receptacle",
    "FLOOR RECEPT": "floor_receptacle",
    "FLR": "floor_receptacle",
    "PNL": "electrical_panel",
    "PANEL": "electrical_panel",
    "MDP": "main_distribution_panel",
    "MAIN PANEL": "main_distribution_panel",
    "SUB PNL": "sub_panel",
    "SUBPANEL": "sub_panel",
    "XFMR": "transformer",
    "XFRMR": "transformer",
    "TRANSFORMER": "transformer",
    "DISC": "disconnect_switch",
    "DISCONNECT": "disconnect_switch",
    "SW": "switch",
    "SWITCH": "switch",
    "3WAY": "three_way_switch",
    "3-WAY": "three_way_switch",
    "4WAY": "four_way_switch",
    "4-WAY": "four_way_switch",
    "DIMMER": "dimmer_switch",
    "DIM": "dimmer_switch",
    "OCC": "occupancy_sensor",
    "OS": "occupancy_sensor",
    "VAC": "vacancy_sensor",
    "TS": "thermostat",
    "THERM": "thermostat",
    "JB": "junction_box",
    "JBOX": "junction_box",
    "J-BOX": "junction_box",
    "PB": "pull_box",
    "EM": "emergency_light",
    "EXIT": "exit_sign",
    "EX": "exit_sign",
    "LT": "light_fixture",
    "LIGHT": "light_fixture",
    "FLUOR": "fluorescent_fixture",
    "LED": "led_fixture",
    "CL": "ceiling_light",
    "WS": "wall_sconce",
    "DL": "downlight",
    "RECESS": "recessed_light",
    "TRACK": "track_light",
    "FAN": "ceiling_fan",
    "CF": "ceiling_fan",
    "PAD": "pad_mount",
    "METER": "electric_meter",
    "EV": "ev_charger",
    "EVSE": "ev_charger",
    "GEN": "generator",
    "ATS": "automatic_transfer_switch",
    "MTS": "manual_transfer_switch",
    "GFCI WP": "weatherproof_gfci_receptacle",
}

_PLUMBING: Final[dict[str, str]] = {
    "WC": "plumbing_fixture_wc",
    "W.C.": "plumbing_fixture_wc",
    "TOILET": "plumbing_fixture_wc",
    "URINAL": "plumbing_fixture_urinal",
    "UR": "plumbing_fixture_urinal",
    "LAV": "plumbing_fixture_lavatory",
    "LAVATORY": "plumbing_fixture_lavatory",
    "BIDET": "plumbing_fixture_bidet",
    "SHOWER": "plumbing_fixture_shower",
    "SH": "plumbing_fixture_shower",
    "TUB": "plumbing_fixture_tub",
    "BT": "plumbing_fixture_tub",
    "SINK": "plumbing_fixture_sink",
    "SK": "plumbing_fixture_sink",
    "KS": "kitchen_sink",
    "KIT SINK": "kitchen_sink",
    "MS": "mop_sink",
    "MOP SINK": "mop_sink",
    "DW": "dishwasher",
    "DISHWASHER": "dishwasher",
    "GD": "garbage_disposal",
    "DISPOSAL": "garbage_disposal",
    "WH": "water_heater",
    "WTR HTR": "water_heater",
    "WATER HEATER": "water_heater",
    "HB": "hose_bibb",
    "HOSE BIBB": "hose_bibb",
    "FD": "floor_drain",
    "FLR DRAIN": "floor_drain",
    "FLOOR DRAIN": "floor_drain",
    "RD": "roof_drain",
    "ROOF DRAIN": "roof_drain",
    "CO": "cleanout",
    "C.O.": "cleanout",
    "CLEANOUT": "cleanout",
    "FCO": "floor_cleanout",
    "WCO": "wall_cleanout",
    "TD": "trench_drain",
    "AD": "area_drain",
    "BFP": "backflow_preventer",
    "RPZ": "backflow_preventer_rpz",
    "PRV": "pressure_reducing_valve",
    "GV": "gas_valve",
    "BV": "ball_valve",
    "CV": "check_valve",
    "GATE V": "gate_valve",
    "VTR": "vent_through_roof",
    "FCO+": "floor_cleanout",
    "EWC": "drinking_fountain",
    "DF": "drinking_fountain",
}

_MECHANICAL: Final[dict[str, str]] = {
    "VAV": "vav_box",
    "AHU": "air_handler",
    "RTU": "rooftop_unit",
    "FCU": "fan_coil_unit",
    "CU": "condensing_unit",
    "EF": "exhaust_fan",
    "SF": "supply_fan",
    "RF": "return_fan",
    "ERV": "energy_recovery_ventilator",
    "HRV": "heat_recovery_ventilator",
    "SD": "supply_diffuser",
    "SG": "supply_grille",
    "RG": "return_grille",
    "RA": "return_air",
    "EG": "exhaust_grille",
    "EA": "exhaust_air",
    "TG": "transfer_grille",
    "REG": "register",
    "DAMPER": "damper",
    "FSD": "fire_smoke_damper",
    "FD": "fire_damper",  # disambiguated by discipline; in P this is floor_drain
    "MD": "motorized_damper",
    "VFD": "variable_frequency_drive",
    "PUMP": "pump",
    "CHILLER": "chiller",
    "BOILER": "boiler",
    "CT": "cooling_tower",
    "MAU": "makeup_air_unit",
    "DOAS": "dedicated_outdoor_air_system",
    "SPLIT": "mini_split",
    "MINI-SPLIT": "mini_split",
    "DUCTLESS": "mini_split",
}

_FIRE_PROTECTION: Final[dict[str, str]] = {
    "SP": "sprinkler_head",
    "SPRK": "sprinkler_head",
    "SPRINKLER": "sprinkler_head",
    "PSH": "pendant_sprinkler",
    "USH": "upright_sprinkler",
    "SWH": "sidewall_sprinkler",
    "FACP": "fire_alarm_control_panel",
    "FA": "fire_alarm",
    "SD": "smoke_detector",  # SD in FP context overrides M supply_diffuser
    "HD": "heat_detector",
    "PS": "pull_station",
    "PULL": "pull_station",
    "AV": "audio_visual_alarm",
    "HORN": "horn_strobe",
    "STROBE": "strobe",
    "FE": "fire_extinguisher",
    "FH": "fire_hydrant",
    "FDC": "fire_department_connection",
    "STANDPIPE": "standpipe",
    "RISER": "fire_riser",
}

_LOW_VOLTAGE: Final[dict[str, str]] = {
    "DATA": "data_outlet",
    "TEL": "telephone_outlet",
    "PHONE": "telephone_outlet",
    "CATV": "cable_tv_outlet",
    "TV": "tv_outlet",
    "AP": "wireless_access_point",
    "WAP": "wireless_access_point",
    "CAM": "security_camera",
    "CAMERA": "security_camera",
    "CCTV": "security_camera",
    "CR": "card_reader",
    "READER": "card_reader",
    "INT": "intercom",
    "INTERCOM": "intercom",
    "SPK": "speaker",
    "SPEAKER": "speaker",
    "MOTION": "motion_sensor",
    "MS": "motion_sensor",  # overrides P mop_sink in L discipline
    "DC": "door_contact",
    "GBR": "glass_break",
}

_ARCHITECTURAL: Final[dict[str, str]] = {
    "DR": "door",
    "DOOR": "door",
    "WIN": "window",
    "WINDOW": "window",
    "STR": "stair",
    "STAIR": "stair",
    "ELEV": "elevator",
    "ELEVATOR": "elevator",
    "RAMP": "ramp",
    "CORR": "corridor",
}

# Discipline → lexicon table
_BY_DISCIPLINE: Final[dict[str, dict[str, str]]] = {
    "E": _ELECTRICAL,
    "P": _PLUMBING,
    "M": _MECHANICAL,
    "FP": _FIRE_PROTECTION,
    "L": _LOW_VOLTAGE,
    "A": _ARCHITECTURAL,
}

# Global flat lookup — every entry from every discipline. Used when sheet has
# no discipline tag, or as fallback when the discipline lookup misses.
_GLOBAL: Final[dict[str, str]] = {}
for _disc_map in _BY_DISCIPLINE.values():
    for _k, _v in _disc_map.items():
        # Earlier disciplines win on collision; intentional — order below.
        _GLOBAL.setdefault(_k, _v)


# Pre-compute sorted-by-length key lists so longest-match wins.
_GLOBAL_KEYS_SORTED: Final[list[str]] = sorted(_GLOBAL.keys(), key=len, reverse=True)
_KEYS_BY_DISCIPLINE: Final[dict[str, list[str]]] = {
    disc: sorted(table.keys(), key=len, reverse=True)
    for disc, table in _BY_DISCIPLINE.items()
}


_PUNCT_TRIM_RE: Final[re.Pattern[str]] = re.compile(r"[^\w\s\.\+\-/]+")
_WS_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


def normalize_label(raw: str) -> str:
    """Uppercase, strip surrounding punctuation, collapse whitespace.

    Pure function. Safe for empty / whitespace-only input.
    """
    if not raw:
        return ""
    s = _PUNCT_TRIM_RE.sub(" ", raw).strip().upper()
    s = _WS_RE.sub(" ", s)
    return s


def lookup(label: str, discipline: str | None = None) -> str | None:
    """Resolve a raw OCR label to a canonical Aspire symbol name.

    Args:
        label: raw OCR text near a symbol (case / punctuation tolerant).
        discipline: optional sheet discipline ("E", "P", "M", "FP", "L", "A").
            When provided, the discipline-scoped table is consulted FIRST;
            on a miss we fall back to the global table.

    Returns:
        The canonical symbol name, or None if no entry matches.

    Matching policy:
      * Exact normalized match wins immediately.
      * Otherwise we longest-match a known key as a whole-word substring.
        ("GFCI 20A DUPLEX" → 'gfci_receptacle' before 'duplex_receptacle'.)
      * Token boundaries are enforced; "BRECEPT" will NOT match "RECEPT".
    """
    norm = normalize_label(label)
    if not norm:
        return None

    # Exact match — fastest path
    if discipline:
        table = _BY_DISCIPLINE.get(discipline.upper())
        if table and norm in table:
            return table[norm]
    if norm in _GLOBAL:
        return _GLOBAL[norm]

    # Whole-token substring longest match within the discipline (preferred)
    if discipline:
        keys = _KEYS_BY_DISCIPLINE.get(discipline.upper())
        if keys:
            hit = _longest_substring_match(norm, keys, _BY_DISCIPLINE[discipline.upper()])
            if hit is not None:
                return hit

    # Global longest-substring fallback
    return _longest_substring_match(norm, _GLOBAL_KEYS_SORTED, _GLOBAL)


def _longest_substring_match(
    norm: str, keys_sorted_by_len_desc: list[str], table: dict[str, str]
) -> str | None:
    """Return the canonical name for the longest key that appears as a
    whole-token substring of ``norm``. ``keys_sorted_by_len_desc`` must be
    pre-sorted longest-first so the first hit is the longest match."""
    tokens = norm.split(" ")
    token_set = set(tokens)
    for key in keys_sorted_by_len_desc:
        if " " in key:
            # Multi-token key — substring check against the joined string with
            # explicit space-padding so partial-token bleed cannot match.
            padded = f" {norm} "
            if f" {key} " in padded:
                return table[key]
        else:
            if key in token_set:
                return table[key]
    return None


def known_disciplines() -> tuple[str, ...]:
    """Return the discipline codes that have a curated lexicon."""
    return tuple(_BY_DISCIPLINE.keys())


def lexicon_size() -> int:
    """Total distinct lexicon entries (post-collision-merge across disciplines)."""
    return len(_GLOBAL)
