"""M-4 — ConnectivityConsistencyConstraint.

The MEP-domain firewall. M-2 (Discipline) catches cross-discipline
mismatches; this catches intra-discipline ones: waste pipe FEEDS sink
supply, supply duct FEEDS return grille, outlet FEEDS panel (wrong
direction), etc.

Severity: SOFT (UR-downgrade) per reviewer 2026-05-26.
  2D schematic drafting is messy. A waste line that visually clips
  a hot water supply line is usually crossing-not-connecting. We flag
  for review rather than HARD reject and risk deleting valid material.

Two checks per edge:
  (1) Stream compatibility — src and dst must share at least one
      relevant FlowStream
  (2) Role direction (conditional, Option C locked by reviewer)
      Enforced ONLY when BOTH roles are explicit AND neither is
      DISTRIBUTION. Distribution lines are bidirectional in 2D, so
      strict directionality on pipes/conduits drowns in false
      positives.

Data sourcing (priority order):
  1. provenance['src_fixture_role'] / 'dst_fixture_role'   (enum value)
     + provenance['src_streams'] / 'dst_streams'           (list of stream values)
  2. evidence['src_legend_label'] / 'dst_legend_label'     (free text)
  3. evidence['ocr_text']                                  (cross-side fallback)
  4. Missing context on either side                        → SOFT_SKIP
"""
from __future__ import annotations

from typing import Optional

from ..taxonomy import EdgeProposal, EdgeType
from .base import BaseConstraint, ConstraintResult, ConstraintSeverity
from .fixture_class import (
    RETURN_STREAMS,
    SUPPLY_STREAMS,
    FixtureProfile,
    FixtureRole,
    FlowStream,
    classify_fixture,
    role_compatible_for_feeds,
    role_compatible_for_returns,
    streams_share,
)


# Edges that imply physical connection (subject to this constraint)
_CONNECTIVITY_APPLICABLE_EDGES: frozenset[EdgeType] = frozenset({
    EdgeType.FEEDS,
    EdgeType.RETURNS_TO,
    EdgeType.CONNECTED_TO,
    EdgeType.PROTECTED_BY,
    EdgeType.CONTROLS,
    EdgeType.SERVES,
    EdgeType.TERMINATES_AT,
})


class ConnectivityConsistencyConstraint(BaseConstraint):
    """Enforce MEP connection sanity using fixture role + flow streams.

    Lit-but-unfired in Phase 4: no L-4/L-5 generator currently emits
    FEEDS/RETURNS_TO/CONNECTED_TO/etc. The constraint sits ready for
    Phase 5+ generators that produce these edges.
    """

    name = "connectivity_consistency"
    severity = ConstraintSeverity.SOFT
    applies_to = _CONNECTIVITY_APPLICABLE_EDGES

    def check(self, proposal: EdgeProposal) -> ConstraintResult:
        src_profile, src_source = self._resolve_profile(proposal, side="src")
        dst_profile, dst_source = self._resolve_profile(proposal, side="dst")

        # Both sides unknown → SOFT skip (fail open)
        if src_profile is None and dst_profile is None:
            return self._soft_skip(
                reason="missing_fixture_context",
                evidence={"src_source": src_source, "dst_source": dst_source},
            )

        # Only one side known → SOFT skip (can't compare)
        if src_profile is None or dst_profile is None:
            return self._soft_skip(
                reason="partial_fixture_context",
                evidence={
                    "src_resolved": src_profile is not None,
                    "dst_resolved": dst_profile is not None,
                    "src_source": src_source,
                    "dst_source": dst_source,
                },
            )

        # Both known — apply rules per edge type
        edge_type = proposal.edge_type

        if edge_type == EdgeType.FEEDS:
            return self._check_feeds(proposal, src_profile, dst_profile,
                                      src_source, dst_source)
        if edge_type == EdgeType.RETURNS_TO:
            return self._check_returns(proposal, src_profile, dst_profile,
                                        src_source, dst_source)
        if edge_type == EdgeType.TERMINATES_AT:
            return self._check_terminates(proposal, src_profile, dst_profile,
                                           src_source, dst_source)
        if edge_type == EdgeType.PROTECTED_BY:
            return self._check_protected_by(proposal, src_profile, dst_profile,
                                             src_source, dst_source)
        if edge_type == EdgeType.CONTROLS:
            return self._check_controls(proposal, src_profile, dst_profile,
                                         src_source, dst_source)
        # CONNECTED_TO + SERVES — symmetric or directional-light; just
        # stream-share check
        return self._check_stream_share(proposal, src_profile, dst_profile,
                                         src_source, dst_source)

    # ---------- per-edge-type rules ----------

    def _check_feeds(
        self, proposal, src: FixtureProfile, dst: FixtureProfile,
        src_source, dst_source,
    ) -> ConstraintResult:
        # Stream check restricted to SUPPLY streams
        if not streams_share(src.streams, dst.streams, restrict_to=SUPPLY_STREAMS):
            return self._violation(
                reason="feeds_streams_disjoint",
                evidence=self._evidence(src, dst, src_source, dst_source,
                                         "FEEDS requires shared supply stream"),
            )

        # Role direction — Option C conditional: only when both roles known
        # AND neither is DISTRIBUTION (distribution is bidirectional in 2D)
        if self._strict_direction_applicable(src, dst):
            if not role_compatible_for_feeds(src.roles, dst.roles):
                return self._violation(
                    reason="feeds_role_direction_wrong",
                    evidence=self._evidence(src, dst, src_source, dst_source,
                                             "FEEDS source/sink role mismatch"),
                )

        return self._passing(reason="feeds_compatible",
                              evidence=self._evidence(src, dst, src_source, dst_source))

    def _check_returns(
        self, proposal, src: FixtureProfile, dst: FixtureProfile,
        src_source, dst_source,
    ) -> ConstraintResult:
        if not streams_share(src.streams, dst.streams, restrict_to=RETURN_STREAMS):
            return self._violation(
                reason="returns_streams_disjoint",
                evidence=self._evidence(src, dst, src_source, dst_source,
                                         "RETURNS_TO requires shared return stream"),
            )

        if self._strict_direction_applicable(src, dst):
            if not role_compatible_for_returns(src.roles, dst.roles):
                return self._violation(
                    reason="returns_role_direction_wrong",
                    evidence=self._evidence(src, dst, src_source, dst_source,
                                             "RETURNS_TO source/sink role mismatch"),
                )

        return self._passing(reason="returns_compatible",
                              evidence=self._evidence(src, dst, src_source, dst_source))

    def _check_terminates(
        self, proposal, src: FixtureProfile, dst: FixtureProfile,
        src_source, dst_source,
    ) -> ConstraintResult:
        # TERMINATES_AT: a line ends at a fixture. Either direction valid;
        # just need stream compatibility (any stream).
        if not streams_share(src.streams, dst.streams):
            return self._violation(
                reason="terminates_streams_disjoint",
                evidence=self._evidence(src, dst, src_source, dst_source,
                                         "TERMINATES_AT requires shared stream"),
            )
        return self._passing(reason="terminates_compatible",
                              evidence=self._evidence(src, dst, src_source, dst_source))

    def _check_protected_by(
        self, proposal, src: FixtureProfile, dst: FixtureProfile,
        src_source, dst_source,
    ) -> ConstraintResult:
        # dst MUST be PROTECTION (breaker, disconnect, BFP)
        if FixtureRole.PROTECTION not in dst.roles:
            return self._violation(
                reason="protected_by_dst_not_protection",
                evidence=self._evidence(src, dst, src_source, dst_source,
                                         "PROTECTED_BY dst must be a PROTECTION role"),
            )
        # And streams must overlap
        if not streams_share(src.streams, dst.streams):
            return self._violation(
                reason="protected_by_streams_disjoint",
                evidence=self._evidence(src, dst, src_source, dst_source,
                                         "PROTECTED_BY requires shared stream"),
            )
        return self._passing(reason="protected_by_compatible",
                              evidence=self._evidence(src, dst, src_source, dst_source))

    def _check_controls(
        self, proposal, src: FixtureProfile, dst: FixtureProfile,
        src_source, dst_source,
    ) -> ConstraintResult:
        # src MUST be CONTROL
        if FixtureRole.CONTROL not in src.roles:
            return self._violation(
                reason="controls_src_not_control",
                evidence=self._evidence(src, dst, src_source, dst_source,
                                         "CONTROLS src must be a CONTROL role"),
            )
        if not streams_share(src.streams, dst.streams):
            return self._violation(
                reason="controls_streams_disjoint",
                evidence=self._evidence(src, dst, src_source, dst_source,
                                         "CONTROLS requires shared stream"),
            )
        return self._passing(reason="controls_compatible",
                              evidence=self._evidence(src, dst, src_source, dst_source))

    def _check_stream_share(
        self, proposal, src: FixtureProfile, dst: FixtureProfile,
        src_source, dst_source,
    ) -> ConstraintResult:
        # Generic stream intersection — used for CONNECTED_TO, SERVES
        if not streams_share(src.streams, dst.streams):
            return self._violation(
                reason=f"{proposal.edge_type.value}_streams_disjoint",
                evidence=self._evidence(src, dst, src_source, dst_source,
                                         f"{proposal.edge_type.value} requires shared stream"),
            )
        return self._passing(
            reason=f"{proposal.edge_type.value}_compatible",
            evidence=self._evidence(src, dst, src_source, dst_source),
        )

    # ---------- helpers ----------

    @staticmethod
    def _strict_direction_applicable(
        src: FixtureProfile, dst: FixtureProfile,
    ) -> bool:
        """Option C: enforce direction only when BOTH roles are non-UNKNOWN
        AND NEITHER is DISTRIBUTION (which is bidirectional in 2D).
        """
        src_known = bool(src.roles - {FixtureRole.UNKNOWN})
        dst_known = bool(dst.roles - {FixtureRole.UNKNOWN})
        if not (src_known and dst_known):
            return False
        if FixtureRole.DISTRIBUTION in src.roles or FixtureRole.DISTRIBUTION in dst.roles:
            return False
        return True

    @staticmethod
    def _evidence(
        src: FixtureProfile, dst: FixtureProfile,
        src_source: str, dst_source: str,
        note: str = "",
    ) -> dict:
        d = {
            "src_roles": sorted(r.value for r in src.roles),
            "dst_roles": sorted(r.value for r in dst.roles),
            "src_streams": sorted(s.value for s in src.streams),
            "dst_streams": sorted(s.value for s in dst.streams),
            "src_source": src_source,
            "dst_source": dst_source,
        }
        if note:
            d["note"] = note
        return d

    # ---------- resolution ----------

    @staticmethod
    def _resolve_profile(
        proposal: EdgeProposal, *, side: str,
    ) -> tuple[Optional[FixtureProfile], str]:
        """Return (FixtureProfile, source_label) for src or dst.

        Source labels for audit forensics:
          'provenance.explicit'    — role + streams set directly in provenance
          'evidence.legend_label'  — inferred from src/dst legend label
          'evidence.ocr_text'      — inferred from OCR text (cross-side fallback)
          'none'                   — no usable signal
        """
        # Path 1: explicit role + streams in provenance
        role_key = "src_fixture_role" if side == "src" else "dst_fixture_role"
        streams_key = "src_streams" if side == "src" else "dst_streams"
        role_val = proposal.provenance.get(role_key)
        streams_val = proposal.provenance.get(streams_key)
        if role_val and streams_val:
            try:
                role = FixtureRole(str(role_val))
                streams = frozenset(
                    FlowStream(str(s)) for s in streams_val
                )
                return (
                    FixtureProfile(frozenset({role}), streams),
                    "provenance.explicit",
                )
            except (ValueError, TypeError):
                pass  # malformed — fall through

        # Path 2: legend label in evidence
        label_key = "src_legend_label" if side == "src" else "dst_legend_label"
        label = proposal.evidence.get(label_key)
        if label:
            profile = classify_fixture(str(label))
            if profile is not None:
                return profile, "evidence.legend_label"

        # Path 3: OCR text fallback (cross-side — only useful when ANNOTATED
        # nearby; not always tied to src/dst specifically)
        ocr_text = proposal.evidence.get("ocr_text")
        if ocr_text:
            profile = classify_fixture(str(ocr_text))
            if profile is not None:
                return profile, "evidence.ocr_text"

        return None, "none"
