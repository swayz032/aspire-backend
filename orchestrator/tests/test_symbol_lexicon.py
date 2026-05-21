"""Wave 17b.2 — symbol lexicon tests.

Covers:
  * exact matches in electrical / plumbing / mechanical disciplines
  * synonym resolution (RECEPT / RECEPTACLE / OUTLET → duplex_receptacle)
  * modifier longest-match (GFCI RECEPT → gfci_receptacle, not duplex)
  * discipline scoping (SD on E sheet vs M sheet vs FP sheet)
  * normalize_label robustness (punctuation, whitespace, case)
  * miss cases — unknown text returns None
"""
from __future__ import annotations

import pytest

from aspire_orchestrator.services.blueprint import symbol_lexicon
from aspire_orchestrator.services.blueprint.symbol_lexicon import (
    lookup,
    normalize_label,
    known_disciplines,
    lexicon_size,
)


class TestNormalize:
    def test_uppercases(self) -> None:
        assert normalize_label("gfci") == "GFCI"

    def test_strips_trailing_punct(self) -> None:
        assert normalize_label("(GFCI),") == "GFCI"

    def test_collapses_whitespace(self) -> None:
        assert normalize_label("  GFCI   RECEPT  ") == "GFCI RECEPT"

    def test_empty(self) -> None:
        assert normalize_label("") == ""
        assert normalize_label("   ") == ""


class TestExactMatches:
    def test_gfci(self) -> None:
        assert lookup("GFCI") == "gfci_receptacle"

    def test_wc(self) -> None:
        assert lookup("WC") == "plumbing_fixture_wc"

    def test_lav(self) -> None:
        assert lookup("LAV") == "plumbing_fixture_lavatory"

    def test_pnl(self) -> None:
        assert lookup("PNL") == "electrical_panel"

    def test_vav(self) -> None:
        assert lookup("VAV") == "vav_box"

    def test_hb_hose_bibb(self) -> None:
        assert lookup("HB") == "hose_bibb"


class TestSynonyms:
    @pytest.mark.parametrize("term", ["RECEPT", "RECEPTACLE", "OUTLET", "DPLX", "DUPLEX"])
    def test_duplex_synonyms(self, term: str) -> None:
        assert lookup(term) == "duplex_receptacle"

    @pytest.mark.parametrize("term", ["GFCI", "GFI", "GFCI RECEPT", "GFCI RECEPTACLE"])
    def test_gfci_synonyms(self, term: str) -> None:
        assert lookup(term) == "gfci_receptacle"

    @pytest.mark.parametrize("term", ["TOILET", "WC", "W.C."])
    def test_wc_synonyms(self, term: str) -> None:
        assert lookup(term) == "plumbing_fixture_wc"


class TestLongestMatch:
    def test_gfci_receptacle_beats_receptacle(self) -> None:
        # "GFCI RECEPTACLE" must map to gfci_receptacle (longest), not duplex
        assert lookup("GFCI RECEPTACLE") == "gfci_receptacle"

    def test_gfci_wp_beats_either_alone(self) -> None:
        assert lookup("GFCI WP") == "weatherproof_gfci_receptacle"

    def test_modifier_in_phrase(self) -> None:
        # A phrase containing GFCI should resolve to gfci_receptacle
        assert lookup("GFCI 20A") == "gfci_receptacle"


class TestDisciplineScoping:
    def test_sd_in_mechanical_is_supply_diffuser(self) -> None:
        assert lookup("SD", discipline="M") == "supply_diffuser"

    def test_sd_in_fp_is_smoke_detector(self) -> None:
        assert lookup("SD", discipline="FP") == "smoke_detector"

    def test_fd_in_plumbing_is_floor_drain(self) -> None:
        assert lookup("FD", discipline="P") == "floor_drain"

    def test_fd_in_mechanical_is_fire_damper(self) -> None:
        assert lookup("FD", discipline="M") == "fire_damper"

    def test_ms_in_plumbing_is_mop_sink(self) -> None:
        assert lookup("MS", discipline="P") == "mop_sink"

    def test_ms_in_lowvoltage_is_motion_sensor(self) -> None:
        assert lookup("MS", discipline="L") == "motion_sensor"


class TestMisses:
    def test_unknown_text_returns_none(self) -> None:
        assert lookup("ZZZZZZ_NOT_A_SYMBOL") is None

    def test_empty_returns_none(self) -> None:
        assert lookup("") is None

    def test_partial_token_does_not_match(self) -> None:
        # 'BRECEPT' should not match 'RECEPT' as a substring — token boundaries matter
        assert lookup("BRECEPT") is None


class TestSurface:
    def test_known_disciplines_present(self) -> None:
        disciplines = known_disciplines()
        for d in ("E", "P", "M", "FP", "L", "A"):
            assert d in disciplines

    def test_lexicon_size_reasonable(self) -> None:
        # We promised "200+ canonical mappings" — verify the global table is
        # at least ~140 (after collision-merge across disciplines). The raw
        # per-discipline entry count is higher.
        assert lexicon_size() >= 140
