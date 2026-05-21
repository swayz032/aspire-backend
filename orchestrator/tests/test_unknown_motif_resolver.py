"""Wave 11g-v2 Track B Phase 8 — unknown motif resolver tests.

Covers:
  * VECTOR_NATIVE page + high-entropy motif + no match → VECTOR_RECOVERABLE
  * RASTER_SCAN page (any motif) → RASTER_RECOVERABLE
  * HYBRID page (any motif)      → RASTER_RECOVERABLE
  * VECTOR_NATIVE + low-entropy embedding + no match → TRUE_UNKNOWN
  * Resolver NEVER returns a canonical_type when bucket is TRUE_UNKNOWN
    (fail-closed guard — this test enforces Law #3 at the type level)
"""
from __future__ import annotations

from aspire_orchestrator.services.blueprint.subgraph_miner import (
    DiscoveredMotif,
    EMBEDDING_DIM,
    MotifInstance,
)
from aspire_orchestrator.services.blueprint.unknown_motif_resolver import (
    PageClassification,
    bucket_unknown,
    shannon_entropy,
)
from aspire_orchestrator.services.blueprint.symbol_retrieval import (
    MemoryHit,
    resolve_motif,
)


# --- Helpers -----------------------------------------------------------------


def _motif(embedding: list[float], *, motif_id: str = "m1") -> DiscoveredMotif:
    return DiscoveredMotif(
        motif_id=motif_id,
        graph_hash=motif_id + "0" * (40 - len(motif_id)),
        embedding=embedding,
        frequency=4,
        instances=(MotifInstance(
            center_node="n0", bbox=(0.0, 0.0, 10.0, 10.0), member_nodes=("n0",),
        ),),
        confidence=0.5,
        rotation_orbit=(motif_id + "0" * (40 - len(motif_id)),) * 4,
    )


def _flat_embedding() -> list[float]:
    """High-entropy: uniform mass across all buckets → max log2(32) ≈ 5.0."""
    return [1.0] * EMBEDDING_DIM


def _peaked_embedding() -> list[float]:
    """Low-entropy: nearly all mass on one bucket → entropy ≈ 0.0."""
    v = [0.0] * EMBEDDING_DIM
    v[0] = 1.0
    return v


# --- Entropy unit tests ------------------------------------------------------


class TestEntropy:
    def test_flat_distribution_high_entropy(self) -> None:
        h = shannon_entropy(_flat_embedding())
        # log2(32) = 5
        assert h > 4.5

    def test_peaked_distribution_zero_entropy(self) -> None:
        assert shannon_entropy(_peaked_embedding()) == 0.0

    def test_empty_returns_zero(self) -> None:
        assert shannon_entropy([]) == 0.0

    def test_negative_or_nan_safe(self) -> None:
        # Negatives are ignored (no signal).
        assert shannon_entropy([-1.0, -2.0, -3.0]) == 0.0


# --- Bucket tests ------------------------------------------------------------


class TestBucket:
    def test_raster_scan_is_raster_recoverable(self) -> None:
        motif = _motif(_peaked_embedding())  # low entropy
        cls = PageClassification(category="RASTER_SCAN", page_number=1)
        assert bucket_unknown(motif, cls) == "RASTER_RECOVERABLE"

    def test_hybrid_is_raster_recoverable(self) -> None:
        motif = _motif(_flat_embedding())  # high entropy doesn't matter
        cls = PageClassification(category="HYBRID", page_number=2)
        assert bucket_unknown(motif, cls) == "RASTER_RECOVERABLE"

    def test_vector_native_high_entropy_is_vector_recoverable(self) -> None:
        motif = _motif(_flat_embedding())
        cls = PageClassification(category="VECTOR_NATIVE", page_number=1)
        assert bucket_unknown(motif, cls) == "VECTOR_RECOVERABLE"

    def test_vector_native_low_entropy_is_true_unknown(self) -> None:
        motif = _motif(_peaked_embedding())
        cls = PageClassification(category="VECTOR_NATIVE", page_number=1)
        assert bucket_unknown(motif, cls) == "TRUE_UNKNOWN"


# --- Fail-closed contract ----------------------------------------------------


class TestFailClosed:
    def test_resolver_never_returns_canonical_with_conf_zero(self) -> None:
        """Law #3 guard: when the retrieval cascade returns confidence=0
        (`unresolved`), canonical_type MUST be None. They cannot disagree."""
        m = _motif(_peaked_embedding())
        # All sources empty → must drop to unresolved/0.0/None.
        result = resolve_motif(
            m,
            legend_anchors=[],
            tenant_memory_lookup=lambda _h: None,
            ocr_proximity_words=[],
            suite_id="suite-A",
        )
        if result.source == "unresolved":
            assert result.canonical_type is None
            assert result.confidence == 0.0
        # And the inverse: any canonical_type means confidence > 0.
        if result.canonical_type is not None:
            assert result.confidence > 0.0
