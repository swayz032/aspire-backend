"""M-3 — LayerSeparationConstraint (demo vs new-work isolation).

If Drew counts a TO-BE-DEMOLISHED wall in the drywall takeoff, the GC
overbids and loses the job. If Drew misses a NEW-work fixture because
it shared a layer with an EXISTING one, the GC underbids and eats the
loss. This constraint catches both modes.

Severity: SOFT (UR-downgrade) per reviewer lock 2026-05-26.
Architects make drafting mistakes — a primitive accidentally placed on
the wrong layer is common. We flag for human review rather than HARD
reject.

Detection (in priority order):
  1. provenance['source_layer_state'] / 'destination_layer_state' — set
     by generators when known from CAD layer parsing
  2. evidence['layer'] / 'src_layer' / 'dst_layer' — raw CAD layer name
     (e.g., 'P-PIPE-DEMO', 'E-LITE-EXST') — parsed by classify_layer_name
  3. evidence['ocr_text'] — text annotation shorthand ((E), DEMO,
     EXISTING, T.B.D., (N), NEW) — parsed by classify_layer_text

Incompatible pairs (SOFT-flagged, symmetric):
  DEMO ↔ NEW          — going away cannot connect to going in
  EXISTING ↔ NEW      — tie-in scenarios are legitimate but worth review

Compatible pairs:
  DEMO ↔ DEMO         — both going away
  EXISTING ↔ EXISTING — both staying
  NEW ↔ NEW           — both being built
  DEMO ↔ EXISTING     — both pre-bid scope (no bid impact)
  RELOCATE ↔ *        — relocated items are transitional, no flag

Edge applicability:
  Applies to FUNCTIONAL edges (FEEDS, CONNECTED_TO, etc.) and structural
  family-binding edges (MEMBER_OF_FAMILY, LEGEND_ENTRY_FOR). Skips
  ANNOTATED_BY (text-labels are the SOURCE of layer info, not subject
  to the rule) and ADJACENT_TO (geometry only — demo wall next to new
  wall is normal during construction phasing).
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional

from ..taxonomy import EdgeProposal, EdgeType
from .base import BaseConstraint, ConstraintResult, ConstraintSeverity


# ============================================================
# Layer state taxonomy
# ============================================================


class LayerState(str, Enum):
    """Work classification of an entity."""
    DEMO = "demo"
    EXISTING = "existing"
    NEW = "new"
    RELOCATE = "relocate"
    UNKNOWN = "unknown"


# Symmetric SOFT-incompatible pairs
_SOFT_INCOMPATIBLE: frozenset[tuple[LayerState, LayerState]] = frozenset({
    (LayerState.DEMO, LayerState.NEW),
    (LayerState.NEW, LayerState.DEMO),
    (LayerState.EXISTING, LayerState.NEW),
    (LayerState.NEW, LayerState.EXISTING),
})


def layer_states_incompatible(a: Optional[LayerState], b: Optional[LayerState]) -> bool:
    """True iff (a, b) is in the SOFT-incompatible set.

    Returns False when either side is None / UNKNOWN — caller is
    expected to issue SOFT_SKIP, not a violation.
    """
    if a in (None, LayerState.UNKNOWN) or b in (None, LayerState.UNKNOWN):
        return False
    return (a, b) in _SOFT_INCOMPATIBLE


# ============================================================
# CAD layer name parsing
# ============================================================
# Industry-standard suffix conventions for CAD layers. Examples:
#   P-PIPE-DEMO, A-WALL-DEMO, E-LITE-EXST, M-DUCT-EXIST
#   P-PIPE-NEW, A-WALL-N, E-LITE-NEW
# Some firms use prefixes: EXST-WALL, DEMO-PIPE. Handle both.

_DEMO_PAT = re.compile(
    r"(?:^|[-_\s])(DEMO|DEM|DEMOLISH(?:ED)?|DEMOLITION|TBD|TBR)(?:$|[-_\s])",
    re.IGNORECASE,
)
_EXISTING_PAT = re.compile(
    r"(?:^|[-_\s])(EXST|EXIST(?:ING)?|EX|E)(?:$|[-_\s])",
    re.IGNORECASE,
)
_NEW_PAT = re.compile(
    r"(?:^|[-_\s])(NEW|N|NW|NW-WK|NEW-WORK|NEWWORK)(?:$|[-_\s])",
    re.IGNORECASE,
)
_RELOCATE_PAT = re.compile(
    r"(?:^|[-_\s])(RELO|RELOC(?:ATE(?:D)?)?|R)(?:$|[-_\s])",
    re.IGNORECASE,
)


def classify_layer_name(layer: Optional[str]) -> LayerState:
    """Parse a CAD layer name into a LayerState.

    Order matters: DEMO checked before EXISTING (since a layer named
    DEMO-EXST is being demolished, not existing). RELOCATE before NEW
    (RELO suffix beats N suffix).
    """
    if not layer:
        return LayerState.UNKNOWN
    s = layer.upper()
    if _DEMO_PAT.search(s):
        return LayerState.DEMO
    if _RELOCATE_PAT.search(s):
        return LayerState.RELOCATE
    if _EXISTING_PAT.search(s):
        return LayerState.EXISTING
    if _NEW_PAT.search(s):
        return LayerState.NEW
    return LayerState.UNKNOWN


# ============================================================
# OCR text-annotation parsing
# ============================================================
# Architects + engineers use compact shorthand in drawing annotations.

_OCR_TEXT_PATTERNS: list[tuple[re.Pattern[str], LayerState]] = [
    # Order: most specific / unambiguous first
    (re.compile(r"\bT\.?B\.?D\.?\b", re.IGNORECASE), LayerState.DEMO),
    (re.compile(r"\bT\.?B\.?R\.?\b", re.IGNORECASE), LayerState.DEMO),
    (re.compile(r"\bDEMOLISH(?:ED)?\b|\bDEMOLITION\b|\bDEMO\b", re.IGNORECASE),
     LayerState.DEMO),
    (re.compile(r"\bTO\s+BE\s+DEMOLISHED\b", re.IGNORECASE), LayerState.DEMO),
    (re.compile(r"\bTO\s+BE\s+REMOVED\b", re.IGNORECASE), LayerState.DEMO),
    (re.compile(r"\(D\)"), LayerState.DEMO),

    (re.compile(r"\bRELOCATE(?:D)?\b", re.IGNORECASE), LayerState.RELOCATE),
    (re.compile(r"\(R\)"), LayerState.RELOCATE),

    (re.compile(r"\bEXISTING\b|\bEXIST\.?\b|\bEXST\.?\b", re.IGNORECASE),
     LayerState.EXISTING),
    (re.compile(r"\(E\)"), LayerState.EXISTING),

    (re.compile(r"\bNEW\s+WORK\b|\bNEW\b", re.IGNORECASE), LayerState.NEW),
    (re.compile(r"\(N\)"), LayerState.NEW),
]


def classify_layer_text(text: Optional[str]) -> LayerState:
    """Detect layer state from an OCR text annotation.

    Returns UNKNOWN if no marker found. (E), DEMO, etc. produce strong
    signals; ambiguous text falls through to UNKNOWN.
    """
    if not text:
        return LayerState.UNKNOWN
    for pattern, state in _OCR_TEXT_PATTERNS:
        if pattern.search(text):
            return state
    return LayerState.UNKNOWN


# ============================================================
# The constraint
# ============================================================

# Applies to functional + structural-family edges where a work-class
# mismatch implies a real bid error. Skips ANNOTATED_BY (text is the
# source of layer info), ADJACENT_TO + ALIGNED_WITH + INSIDE_ZONE
# (pure geometry, no work-class implication).
_LAYER_APPLICABLE_EDGES: frozenset[EdgeType] = frozenset({
    EdgeType.FEEDS,
    EdgeType.RETURNS_TO,
    EdgeType.CONNECTED_TO,
    EdgeType.PROTECTED_BY,
    EdgeType.CONTROLS,
    EdgeType.SERVES,
    EdgeType.TERMINATES_AT,
    EdgeType.MEMBER_OF_FAMILY,
    EdgeType.LEGEND_ENTRY_FOR,
})


class LayerSeparationConstraint(BaseConstraint):
    """Flag edges that bridge incompatible work-classes (DEMO ↔ NEW).

    SOFT severity — never HARD reject. Architects make drafting
    mistakes and we want the GC to see the flag, not have Drew silently
    delete the connection and cause a material shortage.
    """

    name = "layer_separation"
    severity = ConstraintSeverity.SOFT
    applies_to = _LAYER_APPLICABLE_EDGES

    def check(self, proposal: EdgeProposal) -> ConstraintResult:
        src_state, src_source = self._resolve_state(proposal, side="src")
        dst_state, dst_source = self._resolve_state(proposal, side="dst")

        # No usable signal on either side → SOFT skip (fail open)
        if src_state == LayerState.UNKNOWN and dst_state == LayerState.UNKNOWN:
            return self._soft_skip(
                reason="missing_layer_context",
                evidence={
                    "src_state": src_state.value,
                    "dst_state": dst_state.value,
                    "src_source": src_source,
                    "dst_source": dst_source,
                },
            )

        # Only one side known — can't compare, but log the partial signal
        if LayerState.UNKNOWN in (src_state, dst_state):
            return self._soft_skip(
                reason="partial_layer_context",
                evidence={
                    "src_state": src_state.value,
                    "dst_state": dst_state.value,
                    "src_source": src_source,
                    "dst_source": dst_source,
                },
            )

        # Both sides known — apply the rule
        if layer_states_incompatible(src_state, dst_state):
            return self._violation(
                reason=(
                    f"incompatible_layers: {src_state.value} ↔ {dst_state.value}"
                ),
                evidence={
                    "src_state": src_state.value,
                    "dst_state": dst_state.value,
                    "src_source": src_source,
                    "dst_source": dst_source,
                },
            )

        return self._passing(
            reason=f"compatible_layers: {src_state.value} ↔ {dst_state.value}",
            evidence={
                "src_state": src_state.value,
                "dst_state": dst_state.value,
            },
        )

    # ---------- resolution ----------

    @staticmethod
    def _resolve_state(
        proposal: EdgeProposal, *, side: str
    ) -> tuple[LayerState, str]:
        """Resolve LayerState for src or dst from proposal data.

        Priority:
          1. provenance['source_layer_state' | 'destination_layer_state']
          2. evidence['layer' | 'src_layer' | 'dst_layer']
          3. evidence['ocr_text'] (cross-side fallback — best-effort)

        Returns (state, source_path). source_path tells the audit
        receipts which signal was actually used.
        """
        # Path 1 — explicit enum-string in provenance
        prov_key = (
            "source_layer_state" if side == "src" else "destination_layer_state"
        )
        prov_val = proposal.provenance.get(prov_key)
        if prov_val:
            try:
                return LayerState(str(prov_val).strip().lower()), f"provenance.{prov_key}"
            except ValueError:
                pass  # malformed value — fall through

        # Path 2 — CAD layer name in evidence
        layer_keys = (
            ("src_layer", "evidence.src_layer"),
            ("layer", "evidence.layer"),
        ) if side == "src" else (
            ("dst_layer", "evidence.dst_layer"),
            ("layer", "evidence.layer"),
        )
        for k, source_label in layer_keys:
            v = proposal.evidence.get(k)
            if v:
                state = classify_layer_name(str(v))
                if state != LayerState.UNKNOWN:
                    return state, source_label

        # Path 3 — OCR text fallback (only when applicable side has text)
        ocr_text = proposal.evidence.get("ocr_text")
        if ocr_text:
            state = classify_layer_text(str(ocr_text))
            if state != LayerState.UNKNOWN:
                return state, "evidence.ocr_text"

        return LayerState.UNKNOWN, "none"
