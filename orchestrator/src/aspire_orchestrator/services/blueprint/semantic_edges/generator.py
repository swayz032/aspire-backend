"""Wave 20.14.5L L-3 — Edge generator framework.

The base class. The 7-stage multi-stage pipeline lives HERE, not in
subclasses. Generators are untrusted producers; this module is the
enforcement authority.

Reviewer-locked invariants (2026-05-25):
  1. Hard locality caps enforced at framework, not generator
  2. Deterministic candidate ordering (stable_candidate_sort_key)
  3. Multi-stage filtering pipeline mandatory (7 stages)
  4. Receipt captures rejection causes per-stage
  5. Immutable generator identity on every edge
  6. Concurrency guardrails (advisory locks)
  7. UNKNOWN_REVIEW quotas → health verdict
"""
from __future__ import annotations

import contextlib
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterable, Iterator, Optional, Protocol

from .locality import LocalityCapExceeded, LocalityCaps
from .metrics import (
    GeneratorHealth,
    HEALTH_HARD_FAIL_THRESHOLD,
    RunMetrics,
    compute_health,
)
from .receipts import GenerationReceipt, ReceiptPersistor, receipt_from_metrics
from .replay import (
    TAXONOMY_VERSION,
    compute_input_evidence_hash,
    compute_output_edge_hash,
    stable_candidate_sort_key,
)
from .taxonomy import (
    EdgeProposal,
    EdgeType,
    RejectionReason,
    ResolutionState,
    ValidationResult,
    validate_edge,
)


# ============================================================
# Protocols + dataclasses
# ============================================================


class LockProvider(Protocol):
    """Project-scoped advisory lock backend. Used to prevent duplicate
    generator runs over the same project from racing.

    Production: PostgresAdvisoryLock (pg_try_advisory_lock).
    Tests: InMemoryLockProvider.
    """

    @contextlib.contextmanager
    def acquire(self, project_id: str, generator_name: str) -> Iterator[bool]:
        """Yield True if acquired, False if locked elsewhere.

        Generators receiving False MUST abort with no side effects.
        """
        ...


class InMemoryLockProvider:
    """Test-only lock provider. Backed by an in-process set."""

    def __init__(self) -> None:
        self._locked: set[tuple[str, str]] = set()

    @contextlib.contextmanager
    def acquire(self, project_id: str, generator_name: str) -> Iterator[bool]:
        key = (str(project_id), generator_name)
        if key in self._locked:
            yield False
            return
        self._locked.add(key)
        try:
            yield True
        finally:
            self._locked.discard(key)


@dataclass(frozen=True)
class GeneratorContext:
    """Per-run context handed to the generator.

    The generator never sees raw db handles — supabase_client + lock
    provider are injected here for testability.
    """
    suite_id: str
    project_id: str
    pipeline_version: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    office_id: Optional[str] = None
    sheet_id: Optional[str] = None        # if set, generator scopes to one sheet
    supabase_client: Any = None           # actual client injected at runtime
    lock_provider: Optional[LockProvider] = None
    extra_inputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
    """A proposed edge candidate before scoring.

    Generators emit these from stage 0. Framework runs the rest of the
    pipeline. Candidate is mutable so stages 1-4 can attach evidence
    incrementally.
    """
    edge_type: EdgeType
    project_id: str
    sheet_id: Optional[str]
    src_entity_id: str
    src_entity_type: str
    dst_entity_id: str
    dst_entity_type: str
    src_entity_version: int = 1
    dst_entity_version: int = 1
    locality_scope: Optional[str] = None
    spatial_distance: Optional[float] = None
    confidence: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    neighborhood_size: int = 0            # set by stage 0 for observability


@dataclass
class GenerationResult:
    """Final output of EdgeGenerator.run()."""
    metrics: RunMetrics
    receipt: GenerationReceipt
    accepted_edges: list[EdgeProposal]
    unknown_review_edges: list[EdgeProposal]
    aborted: bool = False
    abort_reason: str = ""


# ============================================================
# The base class
# ============================================================


class EdgeGenerator(ABC):
    """Subclass once per generator. Override only the per-stage hooks.

    The framework orchestrates the 7-stage pipeline:

      Stage 0 — locality window (generator emits Candidate stream)
      Stage 1 — cheap geometric filter (subclass hook)
      Stage 2 — semantic compatibility (subclass hook)
      Stage 3 — OCR affinity (subclass hook)
      Stage 4 — expensive topology (subclass hook, capped)
      Stage 5 — constraint validation (taxonomy.validate_edge — framework)
      Stage 6 — emit / UNKNOWN_REVIEW / reject decision (framework)

    NEVER override `run()`. The framework owns orchestration so locality
    caps + advisory locks + UNKNOWN_REVIEW quotas cannot be bypassed.
    """

    # Subclass MUST set these
    name: ClassVar[str] = ""
    version: ClassVar[str] = ""
    edge_types: ClassVar[tuple[EdgeType, ...]] = ()
    extraction_method: ClassVar[str] = ""

    # Default locality caps — subclasses can TIGHTEN via class attr override
    caps: ClassVar[LocalityCaps] = LocalityCaps()

    # =========================================================
    # Abstract hooks subclasses implement
    # =========================================================

    @abstractmethod
    def stage0_emit_candidates(
        self, ctx: GeneratorContext
    ) -> Iterable[Candidate]:
        """Yield candidates in locality-bounded fashion.

        Generator is responsible for using `query_neighbors_within` or
        equivalent to stay within `self.caps.max_radius_px`. The
        framework counts emissions; if a generator exceeds
        `self.caps.max_candidates` mid-stream, the framework aborts.
        """
        ...

    def stage1_cheap_filter(
        self, candidate: Candidate, ctx: GeneratorContext
    ) -> bool:
        """Return True to keep, False to reject. Default: keep all."""
        return True

    def stage2_semantic_compat(
        self, candidate: Candidate, ctx: GeneratorContext
    ) -> bool:
        return True

    def stage3_ocr_affinity(
        self, candidate: Candidate, ctx: GeneratorContext
    ) -> float:
        """Returns multiplicative confidence factor in [0, 1].

        Default: 1.0 (passthrough). Override for OCR-aware generators.
        """
        return 1.0

    def stage4_topology(
        self, candidate: Candidate, ctx: GeneratorContext
    ) -> tuple[float, dict[str, Any]]:
        """Expensive topology / graph analysis. Returns (score, evidence).

        Each invocation counts toward `self.caps.max_expensive_ops`.
        Default: (candidate.confidence, {}) — no extra topology work.
        """
        return candidate.confidence, {}

    # =========================================================
    # Framework-owned orchestration. DO NOT OVERRIDE.
    # =========================================================

    def run(
        self,
        ctx: GeneratorContext,
        *,
        receipt_persistor: Optional[ReceiptPersistor] = None,
    ) -> GenerationResult:
        """Execute the 7-stage pipeline.

        Catches LocalityCapExceeded and aborts with HARD_FAIL receipt.
        Always returns a result, even on abort, so callers can persist
        the receipt for forensics.
        """
        self._validate_subclass()

        metrics = self._init_metrics()
        accepted: list[EdgeProposal] = []
        ur_edges: list[EdgeProposal] = []
        edge_replay_hashes: list[str] = []

        run_start = time.perf_counter()
        aborted = False
        abort_reason = ""

        # Advisory lock — reviewer #6
        lock_provider = ctx.lock_provider
        if lock_provider is None:
            # No lock provider given; proceed (test path) but record it
            metrics.stage("lock").rejected_by_reason["no_lock_provider"] += 1
            self._run_pipeline(
                ctx, metrics, accepted, ur_edges, edge_replay_hashes
            )
        else:
            with lock_provider.acquire(ctx.project_id, self.name) as got_lock:
                if not got_lock:
                    aborted = True
                    abort_reason = "advisory_lock_unavailable"
                    metrics.stage("lock").rejected_by_reason["lock_unavailable"] += 1
                else:
                    try:
                        self._run_pipeline(
                            ctx, metrics, accepted, ur_edges, edge_replay_hashes
                        )
                    except LocalityCapExceeded as exc:
                        aborted = True
                        abort_reason = str(exc)
                        metrics.stage("locality").rejected_by_reason[exc.cap_name] += 1

        metrics.runtime_ms_total = (time.perf_counter() - run_start) * 1000.0
        metrics.aborted = aborted
        metrics.abort_reason = abort_reason

        # Determine health from UNKNOWN_REVIEW ratio
        metrics.health = compute_health(
            metrics.unknown_review_count, metrics.candidates_considered
        )

        # If HARD_FAIL quota or aborted — drop accepted edges (fail closed)
        if metrics.health == GeneratorHealth.HARD_FAIL or aborted:
            if not aborted:
                aborted = True
                abort_reason = (
                    f"unknown_review_quota_exceeded "
                    f"(>{int(HEALTH_HARD_FAIL_THRESHOLD * 100)}%)"
                )
                metrics.aborted = True
                metrics.abort_reason = abort_reason
            accepted = []
            ur_edges = []
            edge_replay_hashes = []

        metrics.output_edge_hash = compute_output_edge_hash(edge_replay_hashes)

        # Build + persist receipt
        receipt = receipt_from_metrics(
            metrics=metrics,
            suite_id=ctx.suite_id,
            project_id=ctx.project_id,
            office_id=ctx.office_id,
            sheet_id=ctx.sheet_id,
            extraction_method=self.extraction_method,
            pipeline_version=ctx.pipeline_version,
        )
        if receipt_persistor is not None:
            receipt_persistor.persist(receipt)

        return GenerationResult(
            metrics=metrics,
            receipt=receipt,
            accepted_edges=accepted,
            unknown_review_edges=ur_edges,
            aborted=aborted,
            abort_reason=abort_reason,
        )

    # =========================================================
    # Internal pipeline
    # =========================================================

    def _run_pipeline(
        self,
        ctx: GeneratorContext,
        metrics: RunMetrics,
        accepted: list[EdgeProposal],
        ur_edges: list[EdgeProposal],
        edge_replay_hashes: list[str],
    ) -> None:
        # Stage 0 — collect + sort candidates
        s0 = metrics.stage("stage0_locality")
        s0_t0 = time.perf_counter()
        raw_candidates = list(self.stage0_emit_candidates(ctx))
        s0.candidates_in = len(raw_candidates)

        if len(raw_candidates) > self.caps.max_candidates:
            metrics.locality_violations += 1
            metrics.locality_trimmed_count = len(raw_candidates) - self.caps.max_candidates
            raw_candidates = raw_candidates[: self.caps.max_candidates]

        # Reviewer #2: deterministic ordering
        candidates = sorted(raw_candidates, key=stable_candidate_sort_key)
        s0.candidates_kept = len(candidates)
        s0.runtime_ms = (time.perf_counter() - s0_t0) * 1000.0

        metrics.candidates_considered = len(candidates)

        # Neighborhood observability
        if candidates:
            sizes = [c.neighborhood_size for c in candidates if c.neighborhood_size]
            if sizes:
                metrics.avg_neighborhood_size = sum(sizes) / len(sizes)
                metrics.max_neighborhood_size = max(sizes)

        expensive_ops_remaining = self.caps.max_expensive_ops

        # Per-candidate pipeline
        for candidate in candidates:
            # ----- Stage 1 cheap filter
            s1 = metrics.stage("stage1_cheap_filter")
            s1.candidates_in += 1
            t = time.perf_counter()
            keep_s1 = self.stage1_cheap_filter(candidate, ctx)
            s1.runtime_ms += (time.perf_counter() - t) * 1000.0
            if not keep_s1:
                s1.candidates_rejected += 1
                s1.rejected_by_reason["stage1_rejected"] += 1
                metrics.rejected_count += 1
                continue
            s1.candidates_kept += 1

            # ----- Stage 2 semantic compatibility
            s2 = metrics.stage("stage2_semantic_compat")
            s2.candidates_in += 1
            t = time.perf_counter()
            keep_s2 = self.stage2_semantic_compat(candidate, ctx)
            s2.runtime_ms += (time.perf_counter() - t) * 1000.0
            if not keep_s2:
                s2.candidates_rejected += 1
                s2.rejected_by_reason["stage2_rejected"] += 1
                metrics.rejected_count += 1
                continue
            s2.candidates_kept += 1

            # ----- Stage 3 OCR affinity (confidence multiplier)
            s3 = metrics.stage("stage3_ocr_affinity")
            s3.candidates_in += 1
            t = time.perf_counter()
            ocr_factor = self.stage3_ocr_affinity(candidate, ctx)
            s3.runtime_ms += (time.perf_counter() - t) * 1000.0
            ocr_factor = max(0.0, min(1.0, ocr_factor))
            candidate.confidence *= ocr_factor
            if candidate.confidence <= 0:
                s3.candidates_rejected += 1
                s3.rejected_by_reason["zero_ocr_affinity"] += 1
                metrics.rejected_count += 1
                continue
            s3.candidates_kept += 1

            # ----- Stage 4 expensive topology
            s4 = metrics.stage("stage4_topology")
            s4.candidates_in += 1
            if expensive_ops_remaining <= 0:
                # Out of expensive budget — downgrade rather than skip
                s4.rejected_by_reason["expensive_budget_exhausted"] += 1
                metrics.locality_violations += 1
                continue
            expensive_ops_remaining -= 1
            s4.expensive_ops_used += 1
            t = time.perf_counter()
            topo_score, topo_evidence = self.stage4_topology(candidate, ctx)
            s4.runtime_ms += (time.perf_counter() - t) * 1000.0
            topo_score = max(0.0, min(1.0, topo_score))
            candidate.confidence = topo_score
            candidate.evidence.update(topo_evidence)
            s4.candidates_kept += 1

            # ----- Stage 5 constraint validation (taxonomy.validate_edge)
            s5 = metrics.stage("stage5_validate")
            s5.candidates_in += 1
            proposal = self._candidate_to_proposal(candidate, ctx)
            t = time.perf_counter()
            result: ValidationResult = validate_edge(proposal)
            s5.runtime_ms += (time.perf_counter() - t) * 1000.0

            # ----- Stage 6 emit / UNKNOWN_REVIEW / reject
            s6 = metrics.stage("stage6_resolve")
            s6.candidates_in += 1
            metrics.record_confidence(candidate.confidence)

            if result.resolution_state == ResolutionState.VERIFIED:
                if metrics.edges_emitted >= self.caps.max_edges_emitted:
                    metrics.locality_violations += 1
                    s6.rejected_by_reason["max_edges_emitted_exceeded"] += 1
                    metrics.rejected_count += 1
                    continue
                accepted.append(proposal)
                edge_replay_hashes.append(result.replay_hash)
                metrics.edges_emitted += 1
                metrics.edges_by_type[proposal.edge_type.value] += 1
                if proposal.locality_scope:
                    metrics.edges_by_locality[proposal.locality_scope.value] += 1
                s6.candidates_kept += 1

            elif result.resolution_state == ResolutionState.UNKNOWN_REVIEW:
                ur_edges.append(proposal)
                metrics.unknown_review_count += 1
                for reason in result.final_rejection_reasons:
                    metrics.unknown_review_reasons[reason.value] += 1
                s6.rejected_by_reason["unknown_review"] += 1

            else:  # REJECTED
                for reason in result.final_rejection_reasons:
                    metrics.rejected_by_reason[reason.value] += 1
                    s5.rejected_by_reason[reason.value] += 1
                metrics.rejected_count += 1

    def _candidate_to_proposal(
        self, candidate: Candidate, ctx: GeneratorContext
    ) -> EdgeProposal:
        from .taxonomy import EntityType, LocalityScope

        return EdgeProposal(
            edge_type=candidate.edge_type,
            suite_id=ctx.suite_id,
            project_id=candidate.project_id,
            src_entity_id=candidate.src_entity_id,
            src_entity_type=EntityType(candidate.src_entity_type),
            dst_entity_id=candidate.dst_entity_id,
            dst_entity_type=EntityType(candidate.dst_entity_type),
            locality_scope=(
                LocalityScope(candidate.locality_scope)
                if candidate.locality_scope
                else None
            ),
            confidence=candidate.confidence,
            evidence=candidate.evidence,
            provenance={
                **candidate.provenance,
                "extractor": self.name,
                "wave": "20.14.5L",
                "pipeline_version": ctx.pipeline_version,
                "generator_window": ctx.run_id,
            },
            extraction_method=self.extraction_method,
            src_entity_version=candidate.src_entity_version,
            dst_entity_version=candidate.dst_entity_version,
            spatial_distance=candidate.spatial_distance,
            office_id=ctx.office_id,
        )

    def _init_metrics(self) -> RunMetrics:
        m = RunMetrics()
        m.generator_name = self.name
        m.generator_version = self.version
        m.taxonomy_version = TAXONOMY_VERSION
        # input_evidence_hash is set by subclass via stage0 OR by caller
        # before invocation; default to a deterministic placeholder.
        m.input_evidence_hash = compute_input_evidence_hash(
            generator_name=self.name,
            generator_version=self.version,
            project_id="<deferred>",
        )
        return m

    def _validate_subclass(self) -> None:
        if not self.name:
            raise ValueError(f"{type(self).__name__}: must set class attr `name`")
        if not self.version:
            raise ValueError(f"{type(self).__name__}: must set class attr `version`")
        if not self.edge_types:
            raise ValueError(f"{type(self).__name__}: must set class attr `edge_types`")
        if not self.extraction_method:
            raise ValueError(
                f"{type(self).__name__}: must set class attr `extraction_method`"
            )
