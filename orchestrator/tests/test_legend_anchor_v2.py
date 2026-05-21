"""Wave 11g-v2 Track B Phase 7 — legend anchoring v2 tests.

Covers:
  * 3 paired symbol rows  → 3 LegendAnchors
  * Topology match: project motif WL-hash equals legend motif WL-hash → anchor
    similarity = 1.0 (above the 0.85 floor)
  * Many-to-one aliases: "RECEPT" / "RECEPTACLE" / "DUPLEX OUTLET" all collapse
    to ``duplex_receptacle``
  * Unknown legend label (not in lexicon) → kept raw, lexicon_known=False
"""
from __future__ import annotations

from typing import Any

from aspire_orchestrator.services.blueprint.legend_anchor_v2 import (
    LegendAnchor,
    extract_legend_anchors,
    match_motif_to_anchors,
)
from aspire_orchestrator.services.blueprint.subgraph_miner import (
    DiscoveredMotif,
    EMBEDDING_DIM,
    MotifInstance,
)


# --- Helpers -----------------------------------------------------------------


def _motif(graph_hash: str, *, motif_id: str | None = None) -> DiscoveredMotif:
    """Build a DiscoveredMotif with a deterministic embedding so cosine match
    is predictable."""
    e = [float((i + 1) % 5) / 5.0 for i in range(EMBEDDING_DIM)]
    return DiscoveredMotif(
        motif_id=motif_id or graph_hash[:12],
        graph_hash=graph_hash,
        embedding=e,
        frequency=1,
        instances=(MotifInstance(
            center_node="leg", bbox=(0.0, 0.0, 10.0, 10.0), member_nodes=("a",),
        ),),
        confidence=1.0,
        rotation_orbit=(graph_hash, graph_hash, graph_hash, graph_hash),
    )


def _row(label: str, motif: DiscoveredMotif, *, bbox: tuple[float, float, float, float] | None = None) -> dict[str, Any]:
    return {
        "label": label,
        "bbox": bbox or (0.0, 0.0, 100.0, 100.0),
        "motif": motif,
    }


# --- Tests -------------------------------------------------------------------


class TestThreeRows:
    def test_three_legend_rows_yield_three_anchors(self) -> None:
        rows = [
            _row("GFCI", _motif("h_gfci" + "0" * 34)),
            _row("DUPLEX", _motif("h_duplex" + "0" * 32)),
            _row("PANEL", _motif("h_panel" + "0" * 33)),
        ]
        anchors = extract_legend_anchors(
            page=None,  # type: ignore[arg-type] — test path uses legend_rows directly
            primitives=[],
            discipline="E",
            legend_rows=rows,
        )
        assert len(anchors) == 3
        labels = {a.canonical_label for a in anchors}
        assert labels == {"gfci_receptacle", "duplex_receptacle", "electrical_panel"}
        assert all(a.lexicon_known for a in anchors)
        assert all(a.confidence == 1.0 for a in anchors)


class TestTopologyMatch:
    def test_exact_hash_match_returns_similarity_1(self) -> None:
        graph_hash = "deadbeef" * 5
        anchor_motif = _motif(graph_hash)
        anchor = LegendAnchor(
            region_bbox=(0.0, 0.0, 100.0, 100.0),
            canonical_label="gfci_receptacle",
            legend_motif=anchor_motif,
            confidence=1.0,
            page_number=1,
            lexicon_known=True,
        )
        project_motif = _motif(graph_hash, motif_id="proj_motif")
        hit = match_motif_to_anchors(project_motif, [anchor])
        assert hit is not None
        matched_anchor, sim = hit
        assert sim == 1.0
        assert matched_anchor.canonical_label == "gfci_receptacle"

    def test_cosine_match_above_floor(self) -> None:
        anchor_motif = _motif("anchor" + "f" * 34)
        # Project motif: identical embedding but DIFFERENT hash → must match
        # via cosine (sim >= 0.85).
        project = DiscoveredMotif(
            motif_id="proj",
            graph_hash="project" + "f" * 33,
            embedding=anchor_motif.embedding,
            frequency=1,
            instances=anchor_motif.instances,
            confidence=1.0,
            rotation_orbit=("project" + "f" * 33,) * 4,
        )
        anchor = LegendAnchor(
            region_bbox=(0.0, 0.0, 10.0, 10.0),
            canonical_label="duplex_receptacle",
            legend_motif=anchor_motif,
            confidence=1.0,
            page_number=1,
            lexicon_known=True,
        )
        hit = match_motif_to_anchors(project, [anchor])
        assert hit is not None
        _a, sim = hit
        assert sim >= 0.85

    def test_no_match_returns_none(self) -> None:
        a_motif = _motif("AAAAAA" + "1" * 34)
        # Project motif has an embedding orthogonal to the anchor's.
        proj_embedding = [0.0] * EMBEDDING_DIM
        proj_embedding[0] = 1.0
        anchor_embedding = [0.0] * EMBEDDING_DIM
        anchor_embedding[-1] = 1.0
        project = DiscoveredMotif(
            motif_id="proj",
            graph_hash="proj_hash" + "0" * 31,
            embedding=proj_embedding,
            frequency=1,
            instances=a_motif.instances,
            confidence=1.0,
            rotation_orbit=("proj_hash" + "0" * 31,) * 4,
        )
        anchor_motif = DiscoveredMotif(
            motif_id="anc",
            graph_hash="anc_hash" + "0" * 32,
            embedding=anchor_embedding,
            frequency=1,
            instances=a_motif.instances,
            confidence=1.0,
            rotation_orbit=("anc_hash" + "0" * 32,) * 4,
        )
        anchor = LegendAnchor(
            region_bbox=(0.0, 0.0, 1.0, 1.0),
            canonical_label="duplex_receptacle",
            legend_motif=anchor_motif,
            confidence=1.0,
            page_number=1,
            lexicon_known=True,
        )
        assert match_motif_to_anchors(project, [anchor]) is None


class TestAliasing:
    def test_many_to_one_aliases_collapse(self) -> None:
        # Three different raw labels for the same canonical Aspire symbol.
        rows = [
            _row("RECEPT", _motif("h1" + "0" * 38)),
            _row("RECEPTACLE", _motif("h2" + "0" * 38)),
            _row("DUPLEX OUTLET", _motif("h3" + "0" * 38)),
        ]
        anchors = extract_legend_anchors(
            page=None,  # type: ignore[arg-type]
            primitives=[],
            discipline="E",
            legend_rows=rows,
        )
        canonical_labels = {a.canonical_label for a in anchors}
        # All three SHOULD canonicalise to the same Aspire symbol.
        assert canonical_labels == {"duplex_receptacle"}
        assert all(a.lexicon_known for a in anchors)


class TestUnknownLegendLabel:
    def test_unknown_label_kept_raw(self) -> None:
        rows = [_row("Z9-CUSTOM-WIDGET", _motif("h_x" + "0" * 37))]
        anchors = extract_legend_anchors(
            page=None,  # type: ignore[arg-type]
            primitives=[],
            discipline="E",
            legend_rows=rows,
        )
        assert len(anchors) == 1
        a = anchors[0]
        assert a.lexicon_known is False
        # Raw label is preserved (case preserved up to normalisation).
        assert a.canonical_label == "Z9-CUSTOM-WIDGET"
        # Confidence is lower than the lexicon-confirmed path.
        assert a.confidence < 1.0
