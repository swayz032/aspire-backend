"""Wave 17b.2 — spatial OCR binder tests.

Covers:
  * Adjacent word binds: GFCI at (105,105) for symbol at (100,100) → gfci_receptacle.
  * No-word case → unbound.
  * Tie-break by OCR confidence when distances are equal.
  * Tie-break by discipline match when a closer cross-domain word competes.
  * Synonym path: "RECEPT" binds to duplex_receptacle.
  * Radius cutoff: words outside radius_pt produce no bind.
  * Empty inputs handled without crash.
  * summarize_binds counts bound vs unbound correctly.
"""
from __future__ import annotations

from aspire_orchestrator.services.blueprint.spatial_ocr_binder import (
    BindResult,
    OcrWord,
    SymbolBox,
    bind_symbols_to_ocr,
    summarize_binds,
)


def _sym(sid: str, x: float, y: float, w: float = 10, h: float = 10) -> SymbolBox:
    return SymbolBox(symbol_id=sid, x=x, y=y, w=w, h=h, signature_hash=sid)


def _word(text: str, cx: float, cy: float, conf: float = 0.9, half: float = 5.0) -> OcrWord:
    return OcrWord(
        text=text,
        x0=cx - half, y0=cy - half, x1=cx + half, y1=cy + half,
        confidence=conf,
    )


class TestAdjacentBind:
    def test_gfci_adjacent_to_symbol_binds(self) -> None:
        sym = _sym("s1", 100, 100)
        # sym center is at (105,105); place GFCI word centered ~5pt away
        word = _word("GFCI", cx=110, cy=110)
        out = bind_symbols_to_ocr([sym], [word], discipline="E")
        assert len(out) == 1
        assert out[0].label == "gfci_receptacle"
        assert out[0].source == "spatial_ocr"
        assert out[0].confidence >= 0.7
        assert out[0].raw_text.startswith("h:")  # PII-safe hash

    def test_no_words_returns_unbound(self) -> None:
        sym = _sym("s1", 100, 100)
        out = bind_symbols_to_ocr([sym], [], discipline="E")
        assert len(out) == 1
        assert out[0].label is None
        assert out[0].source == ""

    def test_distant_word_outside_radius_unbound(self) -> None:
        sym = _sym("s1", 100, 100)
        far = _word("GFCI", cx=1000, cy=1000)
        out = bind_symbols_to_ocr([sym], [far], discipline="E", radius_pt=60)
        assert out[0].label is None


class TestSynonyms:
    def test_recept_binds_to_duplex(self) -> None:
        sym = _sym("s1", 100, 100)
        word = _word("RECEPT", cx=110, cy=110)
        out = bind_symbols_to_ocr([sym], [word], discipline="E")
        assert out[0].label == "duplex_receptacle"

    def test_wc_binds_to_plumbing_wc(self) -> None:
        sym = _sym("s1", 50, 200)
        word = _word("WC", cx=58, cy=205)
        out = bind_symbols_to_ocr([sym], [word], discipline="P")
        assert out[0].label == "plumbing_fixture_wc"


class TestTieBreaking:
    def test_higher_confidence_wins_at_equal_distance(self) -> None:
        sym = _sym("s1", 100, 100)
        # Two equidistant candidates; one is GFCI@0.95, other is OUTLET@0.5.
        # Both are valid electrical labels but distance is identical → higher
        # OCR confidence wins.
        w_high = _word("GFCI", cx=110, cy=100, conf=0.95)
        w_low = _word("OUTLET", cx=110, cy=100, conf=0.5)
        out = bind_symbols_to_ocr([sym], [w_high, w_low], discipline="E")
        assert out[0].label == "gfci_receptacle"

    def test_discipline_match_breaks_tie_against_closer_offdomain(self) -> None:
        # Symbol on a P sheet. A plumbing-domain word (LAV) is slightly farther
        # than an electrical-domain word (DUPLEX); the discipline-match bonus
        # should pull the plumbing label ahead.
        sym = _sym("s1", 100, 100)
        # Pure-electrical word (DUPLEX is electrical only) at distance ~10
        elec = _word("DUPLEX", cx=110, cy=105, conf=0.9)
        # Plumbing-domain word (LAV) at distance ~20 — farther but disc-match
        plumb = _word("LAV", cx=120, cy=110, conf=0.9)
        out = bind_symbols_to_ocr([sym], [elec, plumb], discipline="P")
        assert out[0].label == "plumbing_fixture_lavatory"


class TestModifierLongestMatch:
    def test_gfci_phrase_resolves_to_gfci(self) -> None:
        sym = _sym("s1", 100, 100)
        # A single OCR token "GFCI" near the symbol — the lexicon's exact match
        # gives gfci_receptacle even though "GFCI 20A DUPLEX" also exists as a
        # phrase. Words come one-at-a-time from OCR, so each is normalized
        # independently. The longest-match policy is exercised in the lexicon
        # tests; here we just verify the binder returns the correct hit.
        w = _word("GFCI", cx=110, cy=110)
        out = bind_symbols_to_ocr([sym], [w], discipline="E")
        assert out[0].label == "gfci_receptacle"


class TestBatch:
    def test_multiple_symbols_each_bind_independently(self) -> None:
        s1 = _sym("s1", 100, 100)
        s2 = _sym("s2", 500, 500)
        w1 = _word("GFCI", cx=110, cy=110)
        w2 = _word("PNL", cx=510, cy=505)
        out = bind_symbols_to_ocr([s1, s2], [w1, w2], discipline="E")
        labels = {r.symbol_id: r.label for r in out}
        assert labels == {"s1": "gfci_receptacle", "s2": "electrical_panel"}

    def test_no_mappable_words_all_unbound(self) -> None:
        s1 = _sym("s1", 100, 100)
        s2 = _sym("s2", 500, 500)
        # None of these tokens are in the lexicon
        words = [_word("FOO", 110, 110), _word("BAR", 510, 510), _word("BAZ", 200, 200)]
        out = bind_symbols_to_ocr([s1, s2], words, discipline="E")
        assert all(r.label is None for r in out)
        assert all(r.source == "" for r in out)


class TestSummary:
    def test_summarize_binds(self) -> None:
        results = [
            BindResult("a", "h_a", "gfci_receptacle", "h:x", 0.8, "spatial_ocr", 5.0),
            BindResult("b", "h_b", None, "", 0.0, "", float("inf")),
            BindResult("c", "h_c", "plumbing_fixture_wc", "h:y", 0.7, "spatial_ocr", 7.5),
        ]
        s = summarize_binds(results)
        assert s == {"bound": 2, "unbound": 1}


class TestEdgeCases:
    def test_empty_symbols(self) -> None:
        out = bind_symbols_to_ocr([], [_word("GFCI", 100, 100)], discipline="E")
        assert out == []

    def test_no_discipline_still_works(self) -> None:
        # Without a discipline, the global table is consulted.
        sym = _sym("s1", 100, 100)
        word = _word("GFCI", cx=110, cy=110)
        out = bind_symbols_to_ocr([sym], [word])
        assert out[0].label == "gfci_receptacle"
