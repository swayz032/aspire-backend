"""N-2 — OCRAffinityScorer.

The low-hanging fruit of Phase 5. If the text near a motif says
"QR-1" and the legend says "QR-1 = Pendant Sprinkler", that's a
massive affinity spike — pure string overlap, no model required.

Calibrated bands (reviewer-locked):

  Exact match (symbol_id or label equals an OCR span)           → 1.00
  Substring match (legend symbol_id appears inside OCR text)     → 0.85
  Symbol-id token match (any space-split token shared)           → 0.70
  Keyword overlap with label (0.40 + 0.20 × overlap ratio)       → 0.40-0.60
  No overlap at all                                              → 0.00

Returns the highest band that any (ocr_text, legend) pair triggers
across all OCR spans near the motif. We take the MAX, not the AVG —
one strong signal beats ten weak ones.

Implementation notes:
  - Case-insensitive comparison throughout
  - Tokens split on whitespace + dash + underscore + dot
  - Tokens of length < 2 are dropped (filter "a" / "1" noise)
  - 'NIC', 'EXISTING', 'DEMO' words are NOT special-cased here —
    those are the audit verifier's job (L-6 RuleBasedVerifier).
    Phase 5 cares about identity, not work-class.
"""
from __future__ import annotations

import re

from .base import BaseScorer, BindingProposal, ScorerResult


_TOKEN_SPLIT = re.compile(r"[\s\-_./,]+")


def _tokens(text: str) -> set[str]:
    """Split text into normalized tokens (uppercase, len ≥ 2)."""
    if not text:
        return set()
    out: set[str] = set()
    for raw in _TOKEN_SPLIT.split(text.upper()):
        if len(raw) >= 2:
            out.add(raw)
    return out


def _norm(text: str | None) -> str:
    return (text or "").strip().upper()


class OCRAffinityScorer(BaseScorer):
    """Score motif↔legend OCR text affinity in [0, 1]."""

    name = "ocr_affinity"
    weight_key = "ocr_affinity"

    def compute(self, proposal: BindingProposal) -> ScorerResult:
        ocr_spans = tuple(_norm(t) for t in proposal.motif_nearby_ocr_text)
        ocr_spans = tuple(t for t in ocr_spans if t)
        if not ocr_spans:
            return self._zero(reasoning="no_ocr_text_near_motif")

        label = _norm(proposal.legend_label)
        symbol_id = _norm(proposal.legend_symbol_id)

        if not label and not symbol_id:
            return self._zero(reasoning="legend_has_no_text_identity")

        # Pre-tokenize legend sides for keyword-overlap pass
        label_tokens = _tokens(label)
        symbol_tokens = _tokens(symbol_id)

        best_score = 0.0
        best_reason = "no_overlap"
        best_evidence: dict = {}

        for text in ocr_spans:
            # Band 1 — exact equality (highest signal)
            if symbol_id and text == symbol_id:
                return self._result(
                    1.0, "exact_symbol_id_match",
                    {"ocr_text": text, "matched_field": "symbol_id"},
                )
            if label and text == label:
                return self._result(
                    1.0, "exact_label_match",
                    {"ocr_text": text, "matched_field": "label"},
                )

            # Band 2 — legend symbol_id appears as substring inside the OCR
            if symbol_id and symbol_id in text:
                if best_score < 0.85:
                    best_score = 0.85
                    best_reason = "symbol_id_substring_in_ocr"
                    best_evidence = {
                        "ocr_text": text,
                        "matched_field": "symbol_id",
                        "match_kind": "substring",
                    }

            # Band 3 — any symbol_id token shared with the OCR text tokens
            if symbol_tokens:
                ocr_tokens = _tokens(text)
                shared_sym = symbol_tokens & ocr_tokens
                if shared_sym and best_score < 0.70:
                    best_score = 0.70
                    best_reason = "symbol_id_token_in_ocr"
                    best_evidence = {
                        "ocr_text": text,
                        "matched_tokens": sorted(shared_sym),
                    }

            # Band 4 — keyword overlap with legend label (0.40 + 0.20 × ratio)
            if label_tokens:
                ocr_tokens = _tokens(text)
                shared_lbl = label_tokens & ocr_tokens
                if shared_lbl:
                    ratio = len(shared_lbl) / max(len(label_tokens), 1)
                    candidate = 0.40 + 0.20 * ratio        # → 0.40 .. 0.60
                    if candidate > best_score:
                        best_score = candidate
                        best_reason = "label_keyword_overlap"
                        best_evidence = {
                            "ocr_text": text,
                            "matched_tokens": sorted(shared_lbl),
                            "overlap_ratio": round(ratio, 3),
                        }

        return self._result(best_score, best_reason, best_evidence)
