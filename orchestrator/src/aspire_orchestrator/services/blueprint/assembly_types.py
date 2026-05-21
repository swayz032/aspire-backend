"""Canonical assembly type taxonomy (Wave 17b.5).

The LLM REASON pass must emit one of these exact snake_case strings as
`assembly.type`. Anything that doesn't match — after a single light
normalization pass — is written to `blueprint_missing_inputs` rather than
becoming a `type='general'` placeholder.

Naming convention:
    <discipline>_<element>_<spec>

  discipline   architectural | structural | civil | plumbing | electrical |
               mechanical/hvac | fp (fire protection) | general
  element      wall, door, fixture, branch_circuit, ...
  spec         hint at the dominant material / size / rating

Law compliance:
  Law #1: pure data — no autonomous decisions.
  Law #3: fail-closed — unknown types do NOT silently become 'general'; they
          become missing_inputs so a human reviews them.
  Law #7: no I/O.
"""
from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Canonical type set
# ---------------------------------------------------------------------------

ASSEMBLY_TYPES: Final[frozenset[str]] = frozenset({
    # Architectural — walls
    "wall_demising_gwb_mr_both_sides",
    "wall_partition_gwb_one_side",
    "wall_partition_gwb_both_sides",
    "wall_furring",
    "wall_shaft_gwb_2hr",
    "wall_exterior_cmu",
    "wall_exterior_brick_veneer",
    # Architectural — ceilings
    "ceiling_act_24x48",
    "ceiling_act_24x24",
    "ceiling_gwb",
    "ceiling_open_to_structure",
    # Architectural — floors
    "floor_finish_porcelain_tile",
    "floor_finish_carpet_tile",
    "floor_finish_lvt",
    "floor_finish_sealed_concrete",
    # Architectural — doors / hardware
    "door_hm_3070",
    "door_hm_3070_with_frame",
    "door_wood_solid_core",
    "door_storefront_alum",
    "hardware_set_lockset",
    "hardware_set_panic",
    # Plumbing
    "plumbing_fixture_wc",
    "plumbing_fixture_lav",
    "plumbing_fixture_shower",
    "plumbing_fixture_sink",
    "plumbing_riser_4in_pvc",
    "plumbing_riser_2in_cu",
    "plumbing_drain_floor",
    "plumbing_water_heater_electric",
    # Electrical
    "electrical_branch_circuit_20a_120v",
    "electrical_branch_circuit_20a_277v",
    "electrical_panel_42_circuit",
    "electrical_panel_main_distribution",
    "electrical_receptacle_duplex",
    "electrical_receptacle_gfci",
    "electrical_light_fixture_2x4_led",
    "electrical_light_fixture_recessed_led",
    "electrical_switch_single_pole",
    # HVAC
    "hvac_diffuser_24x24_supply",
    "hvac_diffuser_24x24_return",
    "hvac_grille_lay_in_return",
    "hvac_duct_rect",
    "hvac_duct_round",
    "hvac_vav_box",
    "hvac_rtu",
    "hvac_ahu",
    "hvac_exhaust_fan",
    # Fire protection
    "fp_sprinkler_pendant",
    "fp_sprinkler_upright",
    "fp_alarm_pull_station",
    "fp_main_riser",
    # Structural
    "structural_column_steel_w",
    "structural_beam_steel_w",
    "structural_footing_continuous",
    "structural_footing_spread",
    "structural_slab_on_grade",
    "structural_cmu_loadbearing",
    # Civil
    "civil_pipe_storm_12in",
    "civil_inlet_yard_drain",
    "civil_basin_catch",
    "civil_curb_concrete",
    # General (intentionally narrow — most "general" candidates → missing_input)
    "general_demo",
    "general_notes",
    "general_specs",
})


def normalize_assembly_type(raw: str | None) -> str | None:
    """Return a canonical assembly type or None if the input cannot be matched.

    Normalization steps:
      1. lowercase
      2. strip whitespace
      3. collapse internal whitespace and "-" to "_"
      4. exact-match against ASSEMBLY_TYPES

    Intentionally STRICT: this is not fuzzy matching. "Wall - Demising" will
    normalize to "wall___demising" → no match → caller writes a missing_input
    rather than silently degrading taxonomy quality.

    Args:
        raw: LLM-emitted type string (may be None or junk).

    Returns:
        Canonical snake_case type if it matches ASSEMBLY_TYPES, else None.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    # Collapse separators to underscore
    for ch in (" ", "-", "/", "."):
        text = text.replace(ch, "_")
    # Collapse runs of underscores
    while "__" in text:
        text = text.replace("__", "_")
    if text in ASSEMBLY_TYPES:
        return text
    return None


def is_canonical_type(value: str | None) -> bool:
    """Return True iff value is exactly in ASSEMBLY_TYPES (no normalization)."""
    return value in ASSEMBLY_TYPES
