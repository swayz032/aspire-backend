"""Wave 11g-v2 Track B Phase 6 — symbol retrieval cascade tests.

Covers:
  * Legend hit (anchor matches motif) → source="legend", conf=0.95
  * No legend, tenant memory hit by hash → source="tenant_memory"
  * No legend or memory, OCR proximity with "GFCI" → source="ocr"
  * Nothing matches → source="unresolved", conf=0.0
  * Audit trail (retrieval_matches) records ALL queried sources, not only the
    winner — receipt-grade provenance.
"""
from __future__ import annotations

from typing import Any, Callable

from aspire_orchestrator.services.blueprint.legend_anchor_v2 import LegendAnchor
from aspire_orchestrator.services.blueprint.spatial_ocr_binder import OcrWord
from aspire_orchestrator.services.blueprint.subgraph_miner import (
    DiscoveredMotif,
    EMBEDDING_DIM,
    MotifInstance,
)
from aspire_orchestrator.services.blueprint.symbol_retrieval import (
    CONF_LEGEND,
    CONF_OCR,
    CONF_TENANT_MEMORY_EXACT,
    CONF_UNRESOLVED,
    MemoryHit,
    resolve_motif,
)


# --- Helpers -----------------------------------------------------------------


def _embedding(seed: int = 1) -> list[float]:
    """Deterministic 32-dim embedding for tests. Not all-equal so cosine is
    meaningful."""
    return [float((seed * (i + 1)) % 7) / 7.0 for i in range(EMBEDDING_DIM)]


def _make_motif(
    *,
    motif_id: str = "abc123",
    graph_hash: str = "deadbeef" * 5,
    frequency: int = 5,
    embedding: list[float] | None = None,
    bbox: tuple[float, float, float, float] = (100.0, 100.0, 110.0, 110.0),
) -> DiscoveredMotif:
    e = embedding or _embedding(1)
    instance = MotifInstance(
        center_node="n0", bbox=bbox, member_nodes=("n0", "n1", "n2")
    )
    return DiscoveredMotif(
        motif_id=motif_id,
        graph_hash=graph_hash,
        embedding=e,
        frequency=frequency,
        instances=(instance,),
        confidence=0.9,
        rotation_orbit=(graph_hash, graph_hash, graph_hash, graph_hash),
    )


def _empty_memory_lookup() -> Callable[[str], MemoryHit | None]:
    return lambda _h: None


# --- Tests -------------------------------------------------------------------


class TestLegendHit:
    def test_motif_hashes_match_legend_anchor(self) -> None:
        motif = _make_motif()
        # Anchor's legend_motif shares the SAME graph_hash → guaranteed match.
        anchor = LegendAnchor(
            region_bbox=(0.0, 0.0, 100.0, 100.0),
            canonical_label="gfci_receptacle",
            legend_motif=_make_motif(),  # same hash
            confidence=1.0,
            page_number=1,
            lexicon_known=True,
        )
        resolved = resolve_motif(
            motif,
            legend_anchors=[anchor],
            tenant_memory_lookup=_empty_memory_lookup(),
            ocr_proximity_words=[],
            suite_id="suite-A",
        )
        assert resolved.source == "legend"
        assert resolved.canonical_type == "gfci_receptacle"
        assert resolved.confidence == CONF_LEGEND
        # Audit: legend match recorded.
        legend_records = [m for m in resolved.retrieval_matches if m["source"] == "legend"]
        assert legend_records and legend_records[0]["hit"] is True


class TestTenantMemoryHit:
    def test_exact_graph_hash_match(self) -> None:
        motif = _make_motif()
        mem = MemoryHit(
            canonical_type="duplex_receptacle",
            graph_hash=motif.graph_hash,
            embedding=motif.embedding,
            confidence=1.0,
        )

        def lookup(key: str) -> MemoryHit | None:
            return mem if key == motif.graph_hash else None

        resolved = resolve_motif(
            motif,
            legend_anchors=[],
            tenant_memory_lookup=lookup,
            ocr_proximity_words=[],
            suite_id="suite-A",
        )
        assert resolved.source == "tenant_memory"
        assert resolved.canonical_type == "duplex_receptacle"
        assert resolved.confidence == CONF_TENANT_MEMORY_EXACT
        # Legend was queried and missed — must appear in audit trail.
        sources = [m["source"] for m in resolved.retrieval_matches]
        assert "legend" in sources
        assert "tenant_memory" in sources


class TestOcrHit:
    def test_nearby_gfci_word_resolves_via_lexicon(self) -> None:
        motif = _make_motif(bbox=(100.0, 100.0, 110.0, 110.0))
        # Word "GFCI" at (115, 105) — center distance ~10pt, within 40pt.
        word = OcrWord(text="GFCI", x0=115.0, y0=100.0, x1=130.0, y1=110.0, confidence=1.0)
        resolved = resolve_motif(
            motif,
            legend_anchors=[],
            tenant_memory_lookup=_empty_memory_lookup(),
            ocr_proximity_words=[word],
            suite_id="suite-A",
            discipline="E",
        )
        assert resolved.source == "ocr"
        assert resolved.canonical_type == "gfci_receptacle"
        assert resolved.confidence == CONF_OCR

    def test_far_word_does_not_match(self) -> None:
        motif = _make_motif(bbox=(0.0, 0.0, 10.0, 10.0))
        word = OcrWord(text="GFCI", x0=500.0, y0=500.0, x1=520.0, y1=510.0, confidence=1.0)
        resolved = resolve_motif(
            motif,
            legend_anchors=[],
            tenant_memory_lookup=_empty_memory_lookup(),
            ocr_proximity_words=[word],
            suite_id="suite-A",
        )
        assert resolved.source == "unresolved"


class TestUnresolved:
    def test_nothing_matches_returns_unresolved(self) -> None:
        motif = _make_motif()
        resolved = resolve_motif(
            motif,
            legend_anchors=[],
            tenant_memory_lookup=_empty_memory_lookup(),
            ocr_proximity_words=[],
            suite_id="suite-A",
        )
        assert resolved.source == "unresolved"
        assert resolved.canonical_type is None
        assert resolved.confidence == CONF_UNRESOLVED


class TestAuditTrail:
    def test_audit_records_every_source_queried(self) -> None:
        motif = _make_motif()
        resolved = resolve_motif(
            motif,
            legend_anchors=[],
            tenant_memory_lookup=_empty_memory_lookup(),
            ocr_proximity_words=[],
            suite_id="suite-A",
        )
        sources = [m["source"] for m in resolved.retrieval_matches]
        # Even the LOSING sources are recorded — receipt-grade provenance.
        assert "legend" in sources
        assert "tenant_memory" in sources
        assert "ocr" in sources
        assert "unresolved" in sources

    def test_audit_records_winning_source_with_detail(self) -> None:
        motif = _make_motif()
        anchor = LegendAnchor(
            region_bbox=(0.0, 0.0, 100.0, 100.0),
            canonical_label="gfci_receptacle",
            legend_motif=_make_motif(),
            confidence=1.0,
            page_number=1,
            lexicon_known=True,
        )
        resolved = resolve_motif(
            motif,
            legend_anchors=[anchor],
            tenant_memory_lookup=_empty_memory_lookup(),
            ocr_proximity_words=[],
            suite_id="suite-A",
        )
        winning = [m for m in resolved.retrieval_matches if m["source"] == "legend"]
        assert winning
        assert winning[0]["detail"]["anchor_label"] == "gfci_receptacle"
        assert winning[0]["detail"]["lexicon_known"] is True
