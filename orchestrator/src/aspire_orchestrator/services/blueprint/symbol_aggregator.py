"""Symbol → material aggregator (Wave 17b.5).

Reads `blueprint_symbols` for a project (joined through `blueprint_sheets` by
sheet_id, since symbols have no project_id column) and rolls them up into
distinct material lines keyed by `(discipline, symbol_class, spec)`.

Why this exists:
    Production diagnostic on project 5b0adf45 showed 494 symbols collapsing to
    52 placeholder material rows with identical empty `line_item`. The REASON
    LLM was emitting empty materials and the parser was permissively writing
    them. The real source of truth for "how many X are on these sheets" is the
    symbol table, not the LLM. PROCURE now aggregates from symbols directly so
    the count is deterministic and verifiable.

Law compliance:
    Law #1: pure data transformation — no autonomous decisions.
    Law #6: caller passes suite_id; all queries are tenant-scoped.
    Law #7: no I/O initiation here beyond the supplied async DB callers.
    Law #9: never log raw symbol pixel data or bbox coords; only counts.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# Mapping from coarse symbol classes to (discipline_label, unit, base_line_item).
# Conservative — the YOLO classifier is not yet trained so most production
# symbols come in as `unknown_motif_*` or `unmapped:*`. Those are handled
# explicitly and routed to missing_inputs rather than being silently dropped.
_CLASS_TO_SPEC: dict[str, tuple[str, str, str]] = {
    # Plumbing
    "plumbing_fixture_lav": ("Plumbing", "each", "Lavatory fixture"),
    "plumbing_fixture_wc": ("Plumbing", "each", "Water closet fixture"),
    "plumbing_fixture_shower": ("Plumbing", "each", "Shower fixture"),
    "plumbing_fixture_sink": ("Plumbing", "each", "Sink fixture"),
    "plumbing_drain_floor": ("Plumbing", "each", "Floor drain"),
    # Electrical
    "electrical_receptacle_duplex": ("Electrical", "each", "Duplex receptacle 20A 125V"),
    "electrical_receptacle_gfci": ("Electrical", "each", "GFCI receptacle 20A 125V duplex"),
    "electrical_switch_single_pole": ("Electrical", "each", "Single-pole switch"),
    "electrical_light_fixture_2x4_led": ("Electrical", "each", "2x4 LED light fixture"),
    "electrical_light_fixture_recessed_led": ("Electrical", "each", "Recessed LED light fixture"),
    "electrical_panel_42_circuit": ("Electrical", "each", "42-circuit panelboard"),
    # HVAC
    "hvac_diffuser_24x24_supply": ("HVAC", "each", "24x24 supply diffuser"),
    "hvac_diffuser_24x24_return": ("HVAC", "each", "24x24 return diffuser"),
    "hvac_vav_box": ("HVAC", "each", "VAV terminal box"),
    "hvac_rtu": ("HVAC", "each", "Rooftop unit"),
    "hvac_exhaust_fan": ("HVAC", "each", "Exhaust fan"),
    # Fire protection
    "fp_sprinkler_pendant": ("Fire Protection", "each", "Pendant sprinkler head"),
    "fp_sprinkler_upright": ("Fire Protection", "each", "Upright sprinkler head"),
    "fp_alarm_pull_station": ("Fire Protection", "each", "Fire alarm pull station"),
    # Doors
    "door_hm_3070": ("Architectural", "each", "Hollow metal door 3070"),
    "door_wood_solid_core": ("Architectural", "each", "Wood solid-core door"),
}

# Discipline keyword fallback for un-mapped but recognizable classes like "PLUMBING".
_DISCIPLINE_HINT: dict[str, tuple[str, str, str]] = {
    "plumbing": ("Plumbing", "each", "Plumbing fixture (class TBD)"),
    "electrical": ("Electrical", "each", "Electrical device (class TBD)"),
    "hvac": ("HVAC", "each", "HVAC device (class TBD)"),
    "mechanical": ("HVAC", "each", "Mechanical device (class TBD)"),
    "fp": ("Fire Protection", "each", "Fire protection device (class TBD)"),
    "fire": ("Fire Protection", "each", "Fire protection device (class TBD)"),
}


@dataclass
class AggregatedMaterial:
    """One distinct material line aggregated from N source symbols."""

    discipline: str
    symbol_class: str
    line_item: str
    quantity: float
    unit: str
    mean_confidence: float
    source_symbol_ids: list[str] = field(default_factory=list)
    truth: str = "derived"  # symbols → assemblies → materials = derived

    def to_row(
        self,
        *,
        row_id: str,
        suite_id: str,
        office_id: str | None,
        project_id: str,
        created_at: str,
        tariff_flag: str = "none",
        assembly_id: str | None = None,
    ) -> dict[str, Any]:
        """Render this aggregate as a blueprint_materials INSERT row."""
        return {
            "id": row_id,
            "suite_id": suite_id,
            "office_id": office_id,
            "project_id": project_id,
            "line_item": self.line_item,
            "quantity": float(self.quantity),
            "unit": self.unit,
            "truth": self.truth,
            "tariff_flag": tariff_flag,
            "supplier_id": None,
            "supersedes_id": None,
            "assembly_id": assembly_id,
            "created_at": created_at,
        }


@dataclass
class AggregationResult:
    """Output of aggregate_symbols_to_materials()."""

    materials: list[AggregatedMaterial]
    unmapped_classes: dict[str, int]
    total_symbols_processed: int


def _resolve_spec(symbol_class: str) -> tuple[str, str, str] | None:
    """Return (discipline, unit, line_item_base) for a symbol class or None.

    Strategy:
        1. Exact lookup in _CLASS_TO_SPEC.
        2. Strip "unmapped:" prefix and retry exact lookup.
        3. Discipline keyword hint (e.g. "PLUMBING" → Plumbing).
        4. None — caller produces a missing_input.
    """
    cls = symbol_class.strip()
    if cls in _CLASS_TO_SPEC:
        return _CLASS_TO_SPEC[cls]

    if cls.startswith("unmapped:"):
        stripped = cls.split(":", 1)[1]
        if stripped in _CLASS_TO_SPEC:
            return _CLASS_TO_SPEC[stripped]

    lower = cls.lower()
    for hint, spec in _DISCIPLINE_HINT.items():
        if hint in lower:
            return spec

    return None


def aggregate_symbols_to_materials(
    symbols: list[dict[str, Any]],
) -> AggregationResult:
    """Roll N source symbols up into M distinct material aggregates (M <= N).

    Key invariants:
        - Symbols with the same `class` aggregate into ONE material row whose
          quantity is the count of source symbols.
        - Unknown / unmapped symbol classes are NOT silently dropped — they are
          surfaced via AggregationResult.unmapped_classes so the caller writes
          missing_inputs.
        - Mean confidence is the arithmetic mean of source-symbol confidences;
          downstream logic decides whether to demote to assumed/missing.

    Args:
        symbols: list of blueprint_symbols rows (dicts with class, confidence, id).

    Returns:
        AggregationResult — see dataclass.
    """
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sym in symbols:
        cls = str(sym.get("class") or "").strip()
        if not cls:
            continue
        buckets[cls].append(sym)

    materials: list[AggregatedMaterial] = []
    unmapped: dict[str, int] = {}
    total = 0
    for cls, syms in buckets.items():
        total += len(syms)
        spec = _resolve_spec(cls)
        if spec is None:
            unmapped[cls] = len(syms)
            continue

        discipline, unit, base_line_item = spec
        # Mean confidence (best-effort — symbols with NULL confidence skipped).
        confs = [float(s.get("confidence") or 0.0) for s in syms if s.get("confidence") is not None]
        mean_conf = sum(confs) / len(confs) if confs else 0.0

        # Truth tag derived from mean confidence band.
        if mean_conf >= 0.85:
            truth = "derived"
        elif mean_conf >= 0.55:
            truth = "assumed"
        else:
            truth = "assumed"  # caller will demote to missing_input via floor check

        materials.append(
            AggregatedMaterial(
                discipline=discipline,
                symbol_class=cls,
                line_item=f"{discipline} — {base_line_item}",
                quantity=float(len(syms)),
                unit=unit,
                mean_confidence=round(mean_conf, 4),
                source_symbol_ids=[str(s.get("id")) for s in syms if s.get("id") is not None],
                truth=truth,
            )
        )

    return AggregationResult(
        materials=materials,
        unmapped_classes=unmapped,
        total_symbols_processed=total,
    )
