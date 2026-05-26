"""M-2 — DisciplineCompatibilityConstraint + shared discipline matrix.

This module owns the canonical discipline taxonomy + incompatibility
matrix used by BOTH:
  - Phase 4 validate_constraint() (this constraint)
  - L-6 RuleBasedVerifier (imports DISCIPLINE_TOKENS for its OCR-text
    discipline detection)

The HARD_INCOMPATIBLE set encodes the absolute trade-isolation rules
locked by reviewer 2026-05-26. These are the cases where a misbind
guarantees a wrong bid:

  plumbing ↔ electrical : separate trades, separate prints, separate
                          bids. A plumbing primitive labeled by an
                          electrical legend = guaranteed false positive.

  plumbing ↔ fire       : fire protection (sprinkler heads, alarm
                          conduit) is its own trade with FP/FA prefixes
                          and its own legends. Standpipes are the only
                          legitimate cross-reference, and those flow
                          through Phase 5 binding evidence, not edges.

  electrical ↔ fire     : low-voltage fire alarm is sometimes pulled
                          by electrical, but the schedules are
                          discipline-separated. Hard reject.

NOT in HARD_INCOMPATIBLE (intentional):
  mechanical ↔ electrical : HVAC control wiring legitimately crosses.
                            Let confidence + topology decide.
  structural ↔ architectural : highly co-located. Phase 5 binding
                               disambiguates.
  general ↔ *           : general-discipline sheets (cover, index, life
                          safety) reference everything. Cannot be hard.

For SOFT signals (UR-downgrade only):
  - missing source_discipline + OCR text strongly suggests a discipline
    mismatch → SOFT (let human verify)
  - context entirely absent → SOFT skip (audit re-checks downstream)
"""
from __future__ import annotations

from typing import Any, Optional

from ..taxonomy import EdgeProposal, EdgeType
from .base import BaseConstraint, ConstraintResult, ConstraintSeverity


# ============================================================
# Shared canonical discipline data (single source of truth)
# ============================================================
# L-6 audit.py RuleBasedVerifier imports DISCIPLINE_TOKENS from here.
# Phase 3.1 taxonomy.py classify_discipline() already covers sheet-code
# prefixes; this module covers OCR-text disciplines.

DISCIPLINE_TOKENS: dict[str, frozenset[str]] = {
    "plumbing": frozenset({
        "PLUMBING", "PIPE", "VALVE", "DRAIN", "SEWER", "WASTE", "VENT",
        "WATER", "DOMESTIC", "SUPPLY", "RETURN",
    }),
    "electrical": frozenset({
        "ELECTRICAL", "PANEL", "CIRCUIT", "RECEPTACLE", "OUTLET",
        "BREAKER", "CONDUIT", "GFCI", "DISCONNECT", "TRANSFORMER",
    }),
    "mechanical": frozenset({
        "HVAC", "DUCT", "DIFFUSER", "RTU", "REGISTER", "VAV",
        "CHILLER", "BOILER", "AIR HANDLING", "AHU",
    }),
    "fire": frozenset({
        "SPRINKLER", "FIRE", "ALARM", "SMOKE", "FACP", "HORN STROBE",
        "STANDPIPE", "FIRE PROTECTION",
    }),
    "structural": frozenset({
        "STRUCTURAL", "BEAM", "COLUMN", "STEEL", "FRAMING", "JOIST",
        "REBAR", "FOOTING",
    }),
    "architectural": frozenset({
        "FLOOR PLAN", "WALL", "PARTITION", "FINISH", "DRYWALL",
        "GYPSUM", "DOOR", "WINDOW",
    }),
}


# Symmetric pairs that MUST NOT bind. Implementation queries both
# (A, B) and (B, A) directions.
HARD_INCOMPATIBLE_DISCIPLINES: frozenset[tuple[str, str]] = frozenset({
    ("plumbing", "electrical"),
    ("electrical", "plumbing"),
    ("plumbing", "fire"),
    ("fire", "plumbing"),
    ("electrical", "fire"),
    ("fire", "electrical"),
})


def disciplines_hard_incompatible(a: Optional[str], b: Optional[str]) -> bool:
    """True iff (a, b) is in the HARD-incompatible set.

    Returns False if either side is None/empty (missing context →
    cannot HARD-reject; caller may issue SOFT skip).
    """
    if not a or not b:
        return False
    return (a.lower(), b.lower()) in HARD_INCOMPATIBLE_DISCIPLINES


def detect_discipline_from_text(text: str) -> Optional[str]:
    """Best-effort discipline classifier for OCR span text.

    Returns the discipline key whose token set has the most matches,
    or None if no tokens match. Ties broken by alphabetical order
    (deterministic).
    """
    if not text:
        return None
    text_up = text.upper()
    scores: list[tuple[int, str]] = []
    for disc, tokens in DISCIPLINE_TOKENS.items():
        matches = sum(1 for tok in tokens if tok in text_up)
        if matches > 0:
            scores.append((matches, disc))
    if not scores:
        return None
    # Highest match count wins; alpha tiebreak
    scores.sort(key=lambda s: (-s[0], s[1]))
    return scores[0][1]


# ============================================================
# The constraint
# ============================================================

# Discipline rules apply to edges that bind one entity to another's
# semantic identity (annotated_by, legend_entry_for, member_of_family).
# Pure spatial edges (adjacent_to, aligned_with) don't carry discipline
# semantics — they're geometry only.
_DISCIPLINE_APPLICABLE_EDGES: frozenset[EdgeType] = frozenset({
    EdgeType.ANNOTATED_BY,
    EdgeType.LEGEND_ENTRY_FOR,
    EdgeType.MEMBER_OF_FAMILY,
    EdgeType.REFERENCES_DETAIL,
    EdgeType.CALLS_OUT,
    EdgeType.FEEDS,
    EdgeType.RETURNS_TO,
    EdgeType.PROTECTED_BY,
    EdgeType.CONTROLS,
    EdgeType.SERVES,
    EdgeType.DISCIPLINE_COMPATIBLE_WITH,
})


class DisciplineCompatibilityConstraint(BaseConstraint):
    """Reject edges that cross HARD-incompatible trade lines.

    Context sources, in priority order:
      1. proposal.provenance['source_discipline'] + 'destination_discipline'
         (preferred — set by the generator when it knew the disciplines)
      2. OCR-text inference on annotated_by edges (when src has a
         declared discipline and dst is an ocr_span whose text matches
         a DIFFERENT discipline → HARD violation)

    If neither path provides enough context, returns SOFT skip (fail
    open). The audit/regression suite catches missing-context patterns
    over time so we can tighten coverage.
    """

    name = "discipline_compatibility"
    severity = ConstraintSeverity.HARD
    applies_to = _DISCIPLINE_APPLICABLE_EDGES

    def check(self, proposal: EdgeProposal) -> ConstraintResult:
        src_disc = self._discipline_for(proposal, side="src")
        dst_disc = self._discipline_for(proposal, side="dst")

        # Path 1: explicit disciplines on both sides
        if src_disc and dst_disc:
            if disciplines_hard_incompatible(src_disc, dst_disc):
                return self._violation(
                    reason=(
                        f"hard_incompatible_disciplines: "
                        f"{src_disc} ↔ {dst_disc}"
                    ),
                    evidence={
                        "src_discipline": src_disc,
                        "dst_discipline": dst_disc,
                        "detection_path": "provenance_explicit",
                    },
                )
            return self._passing(
                reason=f"compatible: {src_disc} ↔ {dst_disc}",
                evidence={"src_discipline": src_disc, "dst_discipline": dst_disc},
            )

        # Path 2: OCR-text inference (annotated_by only, when one side known)
        if proposal.edge_type == EdgeType.ANNOTATED_BY:
            ocr_text = proposal.evidence.get("ocr_text", "") or ""
            inferred = detect_discipline_from_text(ocr_text)
            known_side = src_disc or dst_disc
            if inferred and known_side:
                if disciplines_hard_incompatible(known_side, inferred):
                    return self._violation(
                        reason=(
                            f"hard_incompatible_disciplines_via_ocr: "
                            f"{known_side} ↔ {inferred} (ocr={ocr_text!r})"
                        ),
                        evidence={
                            "known_discipline": known_side,
                            "ocr_inferred_discipline": inferred,
                            "ocr_text": ocr_text,
                            "detection_path": "ocr_text_inference",
                        },
                    )
                return self._passing(
                    reason=f"ocr_compatible: {known_side} ↔ {inferred}",
                    evidence={
                        "known_discipline": known_side,
                        "ocr_inferred_discipline": inferred,
                    },
                )

        # Path 3: no usable context — SOFT skip (fail open, surface to audit)
        return self._soft_skip(
            reason="missing_discipline_context",
            evidence={
                "src_discipline": src_disc,
                "dst_discipline": dst_disc,
                "has_ocr_text": bool(proposal.evidence.get("ocr_text")),
            },
        )

    @staticmethod
    def _discipline_for(proposal: EdgeProposal, *, side: str) -> Optional[str]:
        """Read discipline for src or dst from provenance keys.

        Conventions (set by generators):
          - 'source_discipline'      → src side
          - 'destination_discipline' → dst side
        """
        key = "source_discipline" if side == "src" else "destination_discipline"
        val = proposal.provenance.get(key)
        if not val:
            return None
        s = str(val).strip().lower()
        return s if s else None
