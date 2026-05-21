"""Wave 17b.5 — PROCURE symbol-aggregation correctness.

The production bug on project 5b0adf45 was 494 symbols collapsing into 52
identical-empty-string material rows. PROCURE now aggregates symbols → distinct
materials with a defaultdict keyed by symbol class.

These unit tests cover `aggregate_symbols_to_materials` directly — pure-function
behavior, no Supabase round-trip.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4


def _sym(cls: str, conf: float = 0.9) -> dict[str, Any]:
    return {"id": str(uuid4()), "class": cls, "confidence": conf}


class TestSymbolAggregation:
    def test_same_class_across_sheets_aggregates_to_one_row(self) -> None:
        from aspire_orchestrator.services.blueprint.symbol_aggregator import (
            aggregate_symbols_to_materials,
        )
        symbols = (
            [_sym("electrical_receptacle_gfci", 0.92) for _ in range(3)]  # E-1
            + [_sym("electrical_receptacle_gfci", 0.88) for _ in range(2)]  # E-3
        )
        agg = aggregate_symbols_to_materials(symbols)
        gfci = [m for m in agg.materials if "GFCI" in m.line_item]
        assert len(gfci) == 1, f"expected 1 row, got {len(gfci)}"
        assert gfci[0].quantity == 5.0
        assert gfci[0].discipline == "Electrical"
        assert gfci[0].unit == "each"

    def test_different_classes_do_not_collapse(self) -> None:
        from aspire_orchestrator.services.blueprint.symbol_aggregator import (
            aggregate_symbols_to_materials,
        )
        symbols = (
            [_sym("electrical_receptacle_gfci") for _ in range(3)]
            + [_sym("electrical_receptacle_duplex") for _ in range(4)]
            + [_sym("hvac_diffuser_24x24_supply") for _ in range(2)]
        )
        agg = aggregate_symbols_to_materials(symbols)
        assert len(agg.materials) == 3
        line_items = {m.line_item for m in agg.materials}
        assert len(line_items) == 3, "each class should produce a distinct line_item"
        quantities = {m.symbol_class: m.quantity for m in agg.materials}
        assert quantities["electrical_receptacle_gfci"] == 3.0
        assert quantities["electrical_receptacle_duplex"] == 4.0
        assert quantities["hvac_diffuser_24x24_supply"] == 2.0

    def test_unknown_class_surfaces_as_unmapped_not_row(self) -> None:
        from aspire_orchestrator.services.blueprint.symbol_aggregator import (
            aggregate_symbols_to_materials,
        )
        symbols = [_sym("unknown_motif_abc12345") for _ in range(7)]
        agg = aggregate_symbols_to_materials(symbols)
        # No material rows for unmapped classes.
        assert agg.materials == []
        assert agg.unmapped_classes == {"unknown_motif_abc12345": 7}

    def test_unmapped_prefix_strips_and_matches(self) -> None:
        from aspire_orchestrator.services.blueprint.symbol_aggregator import (
            aggregate_symbols_to_materials,
        )
        # Production symbols come in as "unmapped:plumbing_fixture_lav" — the
        # aggregator strips the prefix and looks up the canonical class.
        symbols = [_sym("unmapped:plumbing_fixture_lav") for _ in range(30)]
        agg = aggregate_symbols_to_materials(symbols)
        assert len(agg.materials) == 1
        assert agg.materials[0].quantity == 30.0
        assert agg.materials[0].discipline == "Plumbing"

    def test_discipline_hint_routes_uppercase_label(self) -> None:
        from aspire_orchestrator.services.blueprint.symbol_aggregator import (
            aggregate_symbols_to_materials,
        )
        # Production symbols include bare "PLUMBING" labels — discipline hint routes them.
        symbols = [_sym("PLUMBING") for _ in range(15)]
        agg = aggregate_symbols_to_materials(symbols)
        assert len(agg.materials) == 1
        assert agg.materials[0].discipline == "Plumbing"
        assert agg.materials[0].quantity == 15.0

    def test_empty_input_yields_empty_result(self) -> None:
        from aspire_orchestrator.services.blueprint.symbol_aggregator import (
            aggregate_symbols_to_materials,
        )
        agg = aggregate_symbols_to_materials([])
        assert agg.materials == []
        assert agg.unmapped_classes == {}
        assert agg.total_symbols_processed == 0

    def test_to_row_emits_assembly_id_when_supplied(self) -> None:
        from aspire_orchestrator.services.blueprint.symbol_aggregator import (
            AggregatedMaterial,
        )
        am = AggregatedMaterial(
            discipline="Electrical",
            symbol_class="electrical_receptacle_gfci",
            line_item="Electrical — GFCI Receptacle",
            quantity=5.0,
            unit="each",
            mean_confidence=0.9,
        )
        asm_id = str(uuid4())
        row = am.to_row(
            row_id=str(uuid4()),
            suite_id=str(uuid4()),
            office_id=None,
            project_id=str(uuid4()),
            created_at="2026-05-21T00:00:00+00:00",
            tariff_flag="none",
            assembly_id=asm_id,
        )
        assert row["assembly_id"] == asm_id
        assert row["line_item"].startswith("Electrical")
        assert row["quantity"] == 5.0


class TestCanonicalTypeNormalization:
    """assembly_types.normalize_assembly_type is strict, not fuzzy."""

    def test_canonical_exact_match(self) -> None:
        from aspire_orchestrator.services.blueprint.assembly_types import (
            normalize_assembly_type,
        )
        assert normalize_assembly_type("wall_demising_gwb_mr_both_sides") == (
            "wall_demising_gwb_mr_both_sides"
        )

    def test_human_label_does_not_match(self) -> None:
        from aspire_orchestrator.services.blueprint.assembly_types import (
            normalize_assembly_type,
        )
        # "Wall - Demising" → "wall___demising" → no match.
        assert normalize_assembly_type("Wall - Demising") is None

    def test_none_and_empty_return_none(self) -> None:
        from aspire_orchestrator.services.blueprint.assembly_types import (
            normalize_assembly_type,
        )
        assert normalize_assembly_type(None) is None
        assert normalize_assembly_type("") is None
        assert normalize_assembly_type("   ") is None

    def test_general_is_not_canonical(self) -> None:
        from aspire_orchestrator.services.blueprint.assembly_types import (
            normalize_assembly_type,
        )
        # Production bug used 'general' — we explicitly forbid it.
        assert normalize_assembly_type("general") is None
