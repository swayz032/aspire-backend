"""Wave 17b.2 — Spatial OCR → Symbol Binding.

Every legitimate construction symbol on a sheet sits within a few PDF-points of
a text label that names it ("GFCI", "WC", "PNL", "RECEPT", "HB", ...). The
upstream VGSE pipeline detects the symbol shape but cannot read the adjacent
text. Azure Doc Intelligence (or PyMuPDF) reads the text but has no idea
which words go with which symbol.

This module is the join. Given:
  * a set of symbol detections, each with a PDF-point bbox,
  * a set of OCR words, each with a PDF-point bbox + confidence,
  * the sheet's tagged discipline (E / P / M / FP / L / A),

it returns one ``BindResult`` per symbol containing the best canonical label
the lexicon could map from the nearest OCR word(s).

Key design choices
------------------
* All coordinates are in PDF points. The caller is responsible for converting
  Azure pixel coords (use ``page.width / page.height`` from the analyzeResult)
  if the OCR words came from Azure. PyMuPDF ``get_text("words")`` already
  returns PDF points.
* Candidate scoring combines three signals:

      score = w_dist * (1 - distance / radius)
            + w_conf * ocr_confidence
            + w_disc * (1 if label canonical name appears in the sheet
                        discipline's lexicon table else 0)

  Weights are tuned conservatively (dist 0.5, conf 0.2, disc 0.3) so that
  distance dominates but discipline can break ties decisively when the
  closest word is from a different domain.
* The radius defaults to 60.0 PDF points (~0.83 inches at 72pt/in), tuned for
  the dataset at 200 DPI raster equivalence. Override per-discipline via
  ``radius_overrides``.
* Pure function — no DB, no I/O, no logging of OCR content (Law #9). The
  caller emits receipts and persists results.

Returned ``BindResult.source`` is always "spatial_ocr" on a hit; the field
exists so callers can pass results uniformly with tenant_memory / legend
sources.
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Iterable

from aspire_orchestrator.services.blueprint import symbol_lexicon

logger = logging.getLogger(__name__)

# Default radius (PDF points). Symbols typically have their label-tag within
# 30-50 pts; we go to 60 to absorb annotation drift.
_DEFAULT_RADIUS_PT: float = 60.0

# Scoring weights — sum need not equal 1. Distance dominates.
_W_DIST: float = 0.5
_W_CONF: float = 0.2
_W_DISC: float = 0.3


@dataclass(frozen=True)
class OcrWord:
    """One OCR word with its PDF-point bbox.

    Attributes:
        text: raw word string (PII-free industry terms only; the caller is
            responsible for ensuring it does not include user-facing PII).
        x0, y0, x1, y1: bbox in PDF points (origin top-left).
        confidence: 0.0–1.0 OCR confidence (Azure default; PyMuPDF passes 1.0).
    """
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    confidence: float = 1.0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0


@dataclass(frozen=True)
class SymbolBox:
    """A symbol detection with its PDF-point bbox.

    Attributes:
        symbol_id: stable identifier (e.g. signature_hash + sheet_id).
        x, y, w, h: bbox geometry in PDF points.
        signature_hash: VGSE-computed shape hash, used by callers to upsert
            into blueprint_symbol_memory.
    """
    symbol_id: str
    x: float
    y: float
    w: float
    h: float
    signature_hash: str

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0


@dataclass(frozen=True)
class BindResult:
    """Outcome of binding one symbol to nearby OCR text.

    Attributes:
        symbol_id: matches the SymbolBox.symbol_id of the input.
        signature_hash: copied from the SymbolBox so the caller can upsert to
            symbol_memory without rejoining.
        label: canonical Aspire symbol name (e.g. 'gfci_receptacle'), or None
            on miss.
        raw_text: the OCR word(s) that produced the label, hashed for
            log-safety (Law #9: never log raw OCR). Form: 'h:<sha1[:12]>'.
        confidence: aggregate binding confidence (0.0–1.0).
        source: always 'spatial_ocr' on hit; '' on miss.
        distance_pt: distance in PDF points between symbol center and word
            center for the chosen candidate.
    """
    symbol_id: str
    signature_hash: str
    label: str | None
    raw_text: str
    confidence: float
    source: str
    distance_pt: float


def _hash_for_log(text: str) -> str:
    """SHA1[:12] — never log raw OCR; this lets ops correlate without leakage."""
    if not text:
        return ""
    return "h:" + hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12]


def _distance(sym: SymbolBox, word: OcrWord) -> float:
    """Euclidean distance between symbol center and word center, in points."""
    dx = sym.cx - word.cx
    dy = sym.cy - word.cy
    return math.sqrt(dx * dx + dy * dy)


def _score(
    distance: float,
    radius: float,
    ocr_conf: float,
    disc_match: bool,
) -> float:
    """Combine distance / confidence / discipline-match into a single score.

    Higher is better. Distance term is normalized to [0, 1] across the radius.
    """
    if radius <= 0:
        return 0.0
    dist_term = max(0.0, 1.0 - (distance / radius))
    return (
        _W_DIST * dist_term
        + _W_CONF * max(0.0, min(1.0, ocr_conf))
        + _W_DISC * (1.0 if disc_match else 0.0)
    )


def _is_discipline_specific(canonical: str, discipline: str | None) -> bool:
    """True if ``canonical`` appears in the lexicon table for ``discipline``."""
    if not discipline:
        return False
    table = symbol_lexicon._BY_DISCIPLINE.get(discipline.upper())  # noqa: SLF001
    if not table:
        return False
    return canonical in set(table.values())


def bind_symbols_to_ocr(
    symbols: list[SymbolBox],
    ocr_words: list[OcrWord],
    *,
    discipline: str | None = None,
    radius_pt: float = _DEFAULT_RADIUS_PT,
) -> list[BindResult]:
    """Spatial bind each symbol to the best nearby OCR label.

    Args:
        symbols: detected symbols on this sheet.
        ocr_words: every OCR word from the sheet, already in PDF points.
        discipline: optional sheet discipline letter (E / P / M / FP / L / A).
            Used to prefer discipline-relevant labels on tie-break.
        radius_pt: max distance (PDF points) to consider a word.

    Returns:
        One BindResult per input symbol, in the same order. Misses carry
        ``label=None`` and ``source=""``.

    Pure function. No I/O, no logging of OCR content.
    """
    if not symbols:
        return []

    results: list[BindResult] = []
    # Pre-pass: only consider words whose lexicon lookup yields ANYTHING. A word
    # that does not map to a canonical name has no chance of becoming the label,
    # so we filter early. This makes the inner loop O(symbols * mapped_words).
    candidate_words: list[tuple[OcrWord, str]] = []
    for w in ocr_words:
        canonical = symbol_lexicon.lookup(w.text, discipline=discipline)
        if canonical is not None:
            candidate_words.append((w, canonical))

    if not candidate_words:
        # No mappable text on this sheet — every symbol misses.
        return [
            BindResult(
                symbol_id=s.symbol_id,
                signature_hash=s.signature_hash,
                label=None,
                raw_text="",
                confidence=0.0,
                source="",
                distance_pt=float("inf"),
            )
            for s in symbols
        ]

    for sym in symbols:
        best: tuple[float, OcrWord, str] | None = None
        for w, canonical in candidate_words:
            d = _distance(sym, w)
            if d > radius_pt:
                continue
            disc_match = _is_discipline_specific(canonical, discipline)
            sc = _score(d, radius_pt, w.confidence, disc_match)
            if best is None or sc > best[0]:
                best = (sc, w, canonical)

        if best is None:
            results.append(BindResult(
                symbol_id=sym.symbol_id,
                signature_hash=sym.signature_hash,
                label=None,
                raw_text="",
                confidence=0.0,
                source="",
                distance_pt=float("inf"),
            ))
            continue

        score, best_word, best_label = best
        d = _distance(sym, best_word)
        # Final confidence: shape the score into a 0.55–0.95 band so spatial
        # binds outrank fallbacks (~0.60) but never falsely claim parity with
        # tenant_memory (0.95) — the cascade owner can still prefer memory.
        conf = max(0.55, min(0.95, 0.55 + 0.4 * score))
        results.append(BindResult(
            symbol_id=sym.symbol_id,
            signature_hash=sym.signature_hash,
            label=best_label,
            raw_text=_hash_for_log(best_word.text),
            confidence=round(conf, 3),
            source="spatial_ocr",
            distance_pt=round(d, 2),
        ))

    return results


def summarize_binds(results: Iterable[BindResult]) -> dict[str, int]:
    """Roll up bind results for receipt metadata.

    Returns:
        {"bound": int, "unbound": int}. Caller decorates with discipline /
        sheet info before persisting.
    """
    bound = 0
    unbound = 0
    for r in results:
        if r.label:
            bound += 1
        else:
            unbound += 1
    return {"bound": bound, "unbound": unbound}


def extract_words_with_pymupdf(page) -> list[OcrWord]:  # type: ignore[no-untyped-def]
    """Extract word-level bboxes from a PyMuPDF page.

    Used when the upstream OCR path discarded word geometry (Azure
    ``analyzeResult.content`` is a flat string; we still need word bboxes for
    binding). PyMuPDF returns ``(x0, y0, x1, y1, word, block_no, line_no, word_no)``
    tuples in PDF points — exactly what the binder expects.

    Args:
        page: ``fitz.Page`` instance. Not typed at module level to avoid a hard
            fitz import (this binder must be importable in test envs without
            PDFs in flight).

    Returns:
        list[OcrWord] with confidence=1.0 (PyMuPDF native text — exact).
        Empty list if the page has no extractable text.

    Law #9: caller is responsible for ensuring the words are not echoed in
    logs. The binder hashes them on its output side.
    """
    try:
        raw_words = page.get_text("words")  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.warning("spatial_ocr_binder: pymupdf word extract failed (%s)", type(exc).__name__)
        return []
    out: list[OcrWord] = []
    for tup in raw_words or []:
        try:
            x0, y0, x1, y1, text, *_ = tup
        except (ValueError, TypeError):
            continue
        if not text:
            continue
        out.append(OcrWord(
            text=str(text),
            x0=float(x0),
            y0=float(y0),
            x1=float(x1),
            y1=float(y1),
            confidence=1.0,
        ))
    return out
