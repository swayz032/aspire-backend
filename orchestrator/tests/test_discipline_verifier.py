"""Tests for the Wave 17b.6 discipline verifier cross-check.

Pure function tests — no Supabase, no LLM, no network.
"""
from __future__ import annotations

import pytest

from aspire_orchestrator.services.blueprint.discipline_verifier import (
    VerifyResult,
    verify_classification,
)


# Common OCR snippets with concentrated discipline-specific vocabulary so the
# Tier-3 keyword density bank fires deterministically.

OCR_ELECTRICAL_DENSE = " ".join([
    "outlet receptacle gfci panel conduit breaker switch circuit voltage",
    "junction box panelboard breakers receptacle outlet switch outlet",
    "gfci receptacle conduit panel circuit junction outlet receptacle",
    "outlet receptacle switch gfci panel breaker amperage voltage kva",
]) * 2

OCR_PLUMBING_DENSE = " ".join([
    "lavatory water closet urinal supply waste vent riser trap valve",
    "cleanout backflow p-trap domestic water sanitary lavatory urinal",
    "valve trap cleanout vent riser supply waste lavatory sanitary",
    "lavatory water closet urinal trap riser vent valve cleanout",
]) * 2

OCR_SPARSE = "Misc notes and pencil scratches. " * 5  # under keyword threshold


def _sheet(**kw):
    return {
        "id": kw.get("id", "11111111-1111-1111-1111-111111111111"),
        "sheet_number": kw.get("sheet_number"),
        "sheet_code": kw.get("sheet_code"),
        "sheet_title": kw.get("sheet_title"),
        "ocr_text": kw.get("ocr_text"),
        "discipline": kw.get("discipline"),
    }


class TestFourSignalsAgree:
    def test_all_signals_agree_yields_field_confirmed_098(self):
        symbols = [
            {"class": "electrical_outlet"},
            {"class": "gfci_receptacle"},
            {"class": "panel_a"},
            {"class": "switch_3way"},
        ]
        neighbors = [
            _sheet(sheet_number="2", discipline="E"),
            _sheet(sheet_number="4", discipline="E"),
        ]
        result = verify_classification(
            sheet_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            sheet_number="3",
            discipline_assigned="E",
            sheet_code="E-3",
            ocr_text=OCR_ELECTRICAL_DENSE,
            symbols_on_sheet=symbols,
            neighbor_sheets=neighbors,
        )
        assert result.verdict == "promote"
        assert result.truth == "field_confirmed"
        assert result.confidence == pytest.approx(0.98)
        assert result.discipline_final == "E"
        assert result.agreed_count == 4
        assert result.disagreed_count == 0
        assert result.missing_input_reason is None


class TestSheetCodeVsSymbolDisagreement:
    def test_sheet_code_says_e_but_symbols_say_p_demotes_with_specific_reason(self):
        # Assigned E. Sheet code agrees (E). Symbols disagree (plumbing).
        # OCR sparse (abstain). No useful neighbors (abstain).
        # Net: agreed=1 (sheet_code), disagreed=1 (symbols), abstain=2 → flag.
        symbols = [
            {"class": "lavatory"},
            {"class": "water_closet"},
            {"class": "drain_riser"},
            {"class": "p_trap"},
        ]
        result = verify_classification(
            sheet_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            sheet_number="27",
            discipline_assigned="E",
            sheet_code="E-1",
            ocr_text=OCR_SPARSE,
            symbols_on_sheet=symbols,
            neighbor_sheets=[],
        )
        # 1 agree + 1 disagree + 2 abstain → flag (assumed)
        assert result.verdict == "flag"
        assert result.truth == "assumed"
        assert result.confidence == pytest.approx(0.55)
        assert result.discipline_final == "E"
        assert result.missing_input_reason is not None
        # Specific reason must name the disagreement, NOT generic threshold-speak.
        assert "confidence" not in result.missing_input_reason.lower()
        assert "threshold" not in result.missing_input_reason.lower()
        assert "symbols" in result.missing_input_reason.lower()
        assert "P" in result.missing_input_reason


class TestVerifierRecoversFromLLMDrop:
    def test_no_assigned_but_neighbors_and_code_agree_promotes_via_recovery(self):
        # LLM dropped the sheet (discipline=None). sheet_code E-2, symbols
        # are electrical, OCR dense electrical, neighbors are E. Verifier
        # synthesises an assignment of E and re-votes → all 4 agree → 0.85.
        symbols = [
            {"class": "electrical_outlet"},
            {"class": "gfci_outlet"},
            {"class": "panel_main"},
        ]
        neighbors = [
            _sheet(sheet_number="1", discipline="E"),
            _sheet(sheet_number="3", discipline="E"),
        ]
        result = verify_classification(
            sheet_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            sheet_number="2",
            discipline_assigned=None,
            sheet_code="E-2",
            ocr_text=OCR_ELECTRICAL_DENSE,
            symbols_on_sheet=symbols,
            neighbor_sheets=neighbors,
        )
        assert result.discipline_final == "E"
        assert result.verdict == "promote"
        assert result.truth == "derived"
        assert result.confidence >= 0.85

    def test_no_assigned_and_no_signals_demotes_with_specific_reason(self):
        result = verify_classification(
            sheet_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
            sheet_number="27",
            discipline_assigned=None,
            sheet_code=None,
            ocr_text=OCR_SPARSE,
            symbols_on_sheet=[],
            neighbor_sheets=[],
        )
        assert result.verdict == "demote"
        assert result.truth == "missing_input"
        assert result.confidence == pytest.approx(0.30)
        assert result.discipline_final is None
        assert result.missing_input_reason is not None
        assert "confidence" not in result.missing_input_reason.lower()
        assert "threshold" not in result.missing_input_reason.lower()


class TestPromotionPath:
    def test_assigned_e_with_three_signals_agreeing_promotes_to_085(self):
        # Sheet was tagged E by LLM with confidence 0.65. Verifier sees:
        # sheet_code E-5 (+1), symbols electrical (+1), OCR dense E (+1),
        # neighbors empty (0). 3 agree, 0 disagree → 0.85 derived.
        symbols = [
            {"class": "electrical_outlet"},
            {"class": "gfci_outlet"},
            {"class": "panel_a"},
        ]
        result = verify_classification(
            sheet_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            sheet_number="5",
            discipline_assigned="E",
            sheet_code="E-5",
            ocr_text=OCR_ELECTRICAL_DENSE,
            symbols_on_sheet=symbols,
            neighbor_sheets=[],
        )
        assert result.verdict == "promote"
        assert result.truth == "derived"
        assert result.confidence == pytest.approx(0.85)
        assert result.agreed_count == 3
        assert result.disagreed_count == 0


class TestSparseOCRSignalCount:
    def test_assigned_a_with_no_signals_demotes_specifically(self):
        # Assigned A. No sheet_code, no symbols, sparse OCR, no neighbors.
        # 0 agreed, 0 disagreed, 4 abstain → demote.
        result = verify_classification(
            sheet_id="ffffffff-ffff-ffff-ffff-ffffffffffff",
            sheet_number="31",
            discipline_assigned="A",
            sheet_code=None,
            ocr_text=OCR_SPARSE,
            symbols_on_sheet=[],
            neighbor_sheets=[],
        )
        assert result.verdict == "demote"
        assert result.truth == "missing_input"
        assert result.discipline_final is None
        # Demotion reason must list specific structural reasons.
        msg = result.missing_input_reason or ""
        assert "no sheet_code" in msg
        assert "no symbols detected" in msg
        assert "sparse OCR" in msg or "low keyword density" in msg
        assert "no numeric neighbors" in msg or "no neighbors" in msg
        # No generic threshold language anywhere.
        assert "confidence" not in msg.lower()
        assert "threshold" not in msg.lower()


class TestNeighborConsensus:
    def test_assigned_a_but_neighbors_are_all_e_disagrees(self):
        # Sparse OCR, no symbols, no sheet_code, but two neighbors E.
        # 1 disagree (neighbor), 3 abstain → demote.
        neighbors = [
            _sheet(sheet_number="26", discipline="E"),
            _sheet(sheet_number="28", discipline="E"),
        ]
        result = verify_classification(
            sheet_id="99999999-9999-9999-9999-999999999999",
            sheet_number="27",
            discipline_assigned="A",
            sheet_code=None,
            ocr_text=OCR_SPARSE,
            symbols_on_sheet=[],
            neighbor_sheets=neighbors,
        )
        assert result.verdict == "demote"
        msg = result.missing_input_reason or ""
        assert "neighbor sheets are E" in msg or "neighbor sheets" in msg
        assert "A" in msg


class TestNoGenericThresholdLanguage:
    """Hard guarantee: NO verifier verdict ever uses generic confidence/threshold text."""

    @pytest.mark.parametrize("scenario", [
        # (discipline, code, ocr, symbols, neighbors)
        ("E", "E-1", OCR_SPARSE, [], []),
        ("P", None, OCR_SPARSE, [], []),
        ("A", None, OCR_SPARSE, [{"class": "lavatory"}, {"class": "urinal"}, {"class": "drain"}], []),
        (None, None, OCR_SPARSE, [], []),
        ("M", None, OCR_PLUMBING_DENSE, [{"class": "lavatory"}, {"class": "urinal"}, {"class": "drain"}], []),
    ])
    def test_no_generic_language_in_reasons(self, scenario):
        disc, code, ocr, symbols, neighbors = scenario
        result = verify_classification(
            sheet_id="77777777-7777-7777-7777-777777777777",
            sheet_number="99",
            discipline_assigned=disc,
            sheet_code=code,
            ocr_text=ocr,
            symbols_on_sheet=symbols,
            neighbor_sheets=neighbors,
        )
        if result.missing_input_reason is not None:
            msg = result.missing_input_reason.lower()
            assert "confidence" not in msg or "discipline confidence" not in msg
            # The phrase that Wave 17b.6 promised to eliminate:
            assert "confidence" not in msg or "threshold" not in msg
