"""Tests for the deterministic Sheet-Type Atlas pre-classifier (Wave 17b.1).

Validates four tiers + false-positive guards. Pure-function tests; no Supabase,
no LLM, no network.
"""
from __future__ import annotations

import pytest

from aspire_orchestrator.services.blueprint import sheet_type_atlas


# ---------------------------------------------------------------------------
# Tier 1 — sheet_code prefix
# ---------------------------------------------------------------------------

class TestTier1SheetCode:
    @pytest.mark.parametrize("code,expected_discipline,expected_kind", [
        ("E-1", "E", "electrical"),
        ("E1.2", "E", "electrical"),
        ("P-3", "P", "plumbing"),
        ("M-2", "M", "mechanical"),
        ("A-101", "A", "architectural"),
        ("S-2", "S", "structural"),
        ("C-1", "C", "civil"),
        ("C2.2", "C", "civil"),
        ("L-1", "L", "lighting"),
        ("LP-R1", "L", "light_pole"),
        ("LP1", "L", "light_pole"),
        ("FP-1", "FP", "fire_protection"),
        ("HC-1", "A", "handicap_detail"),
        ("SD-1", "A", "site_detail"),
        ("GN-1", "Specs", "general_notes"),
        ("G-1", "Specs", "general_notes"),
        ("AD-1", "Addenda", "addendum"),
        ("T-1", "Specs", "title_sheet"),
    ])
    def test_tier1_matches_known_codes(self, code, expected_discipline, expected_kind):
        result = sheet_type_atlas.classify(
            sheet_number="1",
            sheet_code=code,
            sheet_title=None,
            ocr_text=None,
        )
        assert result is not None, f"Expected Tier 1 match for {code}"
        assert result.discipline == expected_discipline
        assert result.sheet_kind == expected_kind
        assert result.confidence == 1.00
        assert result.evidence.startswith("sheet_code:")

    def test_tier1_no_match_on_empty_code(self):
        result = sheet_type_atlas.classify(
            sheet_number="1",
            sheet_code=None,
            sheet_title=None,
            ocr_text=None,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Tier 2 — title-block keyword
# ---------------------------------------------------------------------------

class TestTier2TitleBlock:
    def test_specifications_via_title(self):
        result = sheet_type_atlas.classify(
            sheet_number="42",
            sheet_code=None,
            sheet_title="Specifications",
            ocr_text=None,
        )
        assert result is not None
        assert result.discipline == "Specs"
        assert result.confidence == 0.95
        assert "specifications" in result.evidence

    def test_panel_schedule_routes_to_schedules(self):
        ocr = "PANEL SCHEDULE\nPanel A, 200A, 42 circuits"
        result = sheet_type_atlas.classify(
            sheet_number="9",
            sheet_code=None,
            sheet_title=None,
            ocr_text=ocr,
        )
        assert result is not None
        assert result.discipline == "Schedules"

    def test_door_schedule_routes_to_schedules(self):
        result = sheet_type_atlas.classify(
            sheet_number=None,
            sheet_code=None,
            sheet_title="Door Schedule",
            ocr_text=None,
        )
        assert result is not None
        assert result.discipline == "Schedules"

    def test_electrical_site_plan_via_ocr_head(self):
        ocr = "ELECTRICAL SITE PLAN\nSheet 5 of 39\n" + " ".join(["x"] * 200)
        result = sheet_type_atlas.classify(
            sheet_number="5",
            sheet_code=None,
            sheet_title=None,
            ocr_text=ocr,
        )
        assert result is not None
        assert result.discipline == "E"
        assert result.confidence == 0.95

    def test_addendum_with_number_matches(self):
        result = sheet_type_atlas.classify(
            sheet_number="1",
            sheet_code=None,
            sheet_title="Addendum 1",
            ocr_text=None,
        )
        assert result is not None
        assert result.discipline == "Addenda"

    def test_demolition_via_phrase(self):
        # Demolition is currently routed through architectural (no dedicated
        # sheet_kind), so test that it at least returns SOMETHING when paired
        # with arch context.
        ocr = "ARCHITECTURAL PLAN — DEMOLITION " + " ".join(["x"] * 200)
        result = sheet_type_atlas.classify(
            sheet_number="3",
            sheet_code=None,
            sheet_title=None,
            ocr_text=ocr,
        )
        assert result is not None
        assert result.discipline == "A"


# ---------------------------------------------------------------------------
# Tier 3 — keyword density
# ---------------------------------------------------------------------------

class TestTier3KeywordDensity:
    def test_electrical_density_wins(self):
        ocr = (
            "Notes: see receptacle outlet placement. GFCI required at every "
            "outlet. Panel A breaker map: 42 circuits. Conduit runs to junction "
            "box on switch panel. Conductor sizing per code. Receptacle, panel, "
            "breaker, conduit. Outlet wattage 15A."
        )
        result = sheet_type_atlas.classify(
            sheet_number="?",
            sheet_code=None,
            sheet_title=None,
            ocr_text=ocr * 3,  # boost density
        )
        assert result is not None
        assert result.discipline == "E"
        assert result.confidence == 0.80
        assert result.evidence.startswith("keyword_density:")

    def test_no_density_winner_returns_none(self):
        # Mixed plain English with no discipline keywords
        ocr = "The quick brown fox jumps over the lazy dog. " * 50
        result = sheet_type_atlas.classify(
            sheet_number="?",
            sheet_code=None,
            sheet_title=None,
            ocr_text=ocr,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Tier 4 — no match falls through to LLM
# ---------------------------------------------------------------------------

class TestTier4Fallthrough:
    def test_empty_inputs_return_none(self):
        result = sheet_type_atlas.classify(
            sheet_number=None,
            sheet_code=None,
            sheet_title=None,
            ocr_text=None,
        )
        assert result is None

    def test_too_short_ocr_returns_none(self):
        result = sheet_type_atlas.classify(
            sheet_number="1",
            sheet_code=None,
            sheet_title=None,
            ocr_text="short",
        )
        assert result is None


# ---------------------------------------------------------------------------
# False-positive guards
# ---------------------------------------------------------------------------

class TestFalsePositiveGuards:
    def test_per_specifications_in_body_does_not_match(self):
        # "PER SPECIFICATIONS" inside a notes block should not classify as Specs
        # solely on that phrase — needs OTHER context.
        ocr = "Install all fittings PER SPECIFICATIONS noted." + " ".join(["x"] * 500)
        result = sheet_type_atlas.classify(
            sheet_number="9",
            sheet_code=None,
            sheet_title=None,
            ocr_text=ocr,
        )
        # Should be None or NOT Specs (e.g. Tier-3 might give nothing).
        if result is not None:
            assert result.discipline != "Specs"

    def test_date_in_title_block_does_not_trigger(self):
        # A bare date should not produce a classification.
        result = sheet_type_atlas.classify(
            sheet_number="1",
            sheet_code=None,
            sheet_title=None,
            ocr_text="03/15/2026   Project: ABC   License #12345  " + " " * 800,
        )
        assert result is None

    def test_page_counter_alone_not_matched(self):
        result = sheet_type_atlas.classify(
            sheet_number="1",
            sheet_code=None,
            sheet_title=None,
            ocr_text="1 OF 39",
        )
        assert result is None

    def test_addendum_word_alone_in_body_not_matched(self):
        # "ADDENDUM" without a number, buried in body, should NOT trigger.
        # (The pattern requires "ADDEND[AU]M\s+\d".)
        ocr = "Reference addendum requirements section. " + " ".join(["x"] * 200)
        result = sheet_type_atlas.classify(
            sheet_number="1",
            sheet_code=None,
            sheet_title=None,
            ocr_text=ocr,
        )
        if result is not None:
            assert result.discipline != "Addenda"

    def test_civil_code_not_confused_with_general(self):
        # G-1 is general notes, C-1 is civil. They must not collide.
        g = sheet_type_atlas.classify(
            sheet_number="1", sheet_code="G-1", sheet_title=None, ocr_text=None,
        )
        c = sheet_type_atlas.classify(
            sheet_number="2", sheet_code="C-1", sheet_title=None, ocr_text=None,
        )
        assert g is not None and g.discipline == "Specs"
        assert c is not None and c.discipline == "C"


# ---------------------------------------------------------------------------
# Integration — synthetic GAVNN-shaped fixture batch
# ---------------------------------------------------------------------------

def _make_gavnn_like_fixtures() -> list[dict[str, str | None]]:
    """Approximate the 39-sheet GAVNN composition based on the diagnostic.

    Reconstruction (from the issue brief):
      - 1 cover/title sheet
      - 1 sheet index / general notes
      - 1 addendum
      - 2 specifications
      - 4 door/panel/light schedules
      - 6 electrical (E-1..E-6)
      - 4 plumbing (P-1..P-4)
      - 3 mechanical (M-1..M-3)
      - 4 architectural (A-1..A-4)
      - 3 structural (S-1..S-3)
      - 4 civil (C-1..C-4)
      - 2 lighting (L-1..L-2)
      - 1 fire protection (FP-1)
      - 3 demolition / RCP / floor plan (no sheet_code, title-only)
    Total: 39.
    """
    fixtures: list[dict[str, str | None]] = []
    fixtures.append({"sheet_code": "T-1", "sheet_title": "Cover Sheet", "ocr_text": "COVER SHEET\n" + "x" * 400})
    fixtures.append({"sheet_code": "GN-1", "sheet_title": "General Notes", "ocr_text": "GENERAL NOTES\n" + "x" * 400})
    fixtures.append({"sheet_code": "AD-1", "sheet_title": "Addendum 1", "ocr_text": "ADDENDUM 1\n" + "x" * 400})
    fixtures.append({"sheet_code": None, "sheet_title": "Specifications", "ocr_text": "SPECIFICATIONS\n" + "x" * 400})
    fixtures.append({"sheet_code": None, "sheet_title": "Specifications", "ocr_text": "SPECIFICATIONS\n" + "x" * 400})
    for kind in ["Door Schedule", "Panel Schedule", "Light Fixture Schedule", "Window Schedule"]:
        fixtures.append({"sheet_code": None, "sheet_title": kind, "ocr_text": kind.upper() + "\n" + "x" * 400})
    for i in range(1, 7):
        fixtures.append({"sheet_code": f"E-{i}", "sheet_title": None, "ocr_text": "x" * 400})
    for i in range(1, 5):
        fixtures.append({"sheet_code": f"P-{i}", "sheet_title": None, "ocr_text": "x" * 400})
    for i in range(1, 4):
        fixtures.append({"sheet_code": f"M-{i}", "sheet_title": None, "ocr_text": "x" * 400})
    for i in range(1, 5):
        fixtures.append({"sheet_code": f"A-{i}", "sheet_title": None, "ocr_text": "x" * 400})
    for i in range(1, 4):
        fixtures.append({"sheet_code": f"S-{i}", "sheet_title": None, "ocr_text": "x" * 400})
    for i in range(1, 5):
        fixtures.append({"sheet_code": f"C-{i}", "sheet_title": None, "ocr_text": "x" * 400})
    for i in range(1, 3):
        fixtures.append({"sheet_code": f"L-{i}", "sheet_title": None, "ocr_text": "x" * 400})
    fixtures.append({"sheet_code": "FP-1", "sheet_title": None, "ocr_text": "x" * 400})
    # 3 sheets without sheet_code, title-driven
    fixtures.append({"sheet_code": None, "sheet_title": None, "ocr_text": "REFLECTED CEILING PLAN\n" + "x" * 400})
    fixtures.append({"sheet_code": None, "sheet_title": None, "ocr_text": "FLOOR PLAN\n" + "x" * 400})
    fixtures.append({"sheet_code": None, "sheet_title": None, "ocr_text": "ROOF PLAN\n" + "x" * 400})
    return fixtures


class TestGavnnIntegration:
    def test_atlas_bypass_rate(self):
        fixtures = _make_gavnn_like_fixtures()
        assert len(fixtures) == 39

        matched = 0
        for fx in fixtures:
            r = sheet_type_atlas.classify(
                sheet_number=None,
                sheet_code=fx["sheet_code"],
                sheet_title=fx["sheet_title"],
                ocr_text=fx["ocr_text"],
            )
            if r is not None:
                matched += 1

        # Production target: >=22/39 atlas-matched (56% bypass).
        assert matched >= 22, f"Expected >=22 atlas matches on 39-sheet GAVNN-like batch, got {matched}"
