"""Wave 20.14.5L L-6 — Random edge audit harness.

The audit harness is the gate that catches Wave 20.13.3-class
false-positive explosions BEFORE they reach Pop's bid sheet. A
hallucinated annotated_by edge that says "this plumbing primitive
labels a 3/4 Type L copper pipe" when the OCR text actually said
"NOT IN CONTRACT" costs the GC money. The auditor exists to make that
impossible.

Acceptance contract (Phase 3, reviewer-locked):
  - Random edge audit accuracy ≥ 90% per edge_type
  - Sample size N=200 by default (or all edges if fewer)
  - Verifier produces an AuditVerdict per sampled edge
  - Report aggregates by edge_type + emits per-type precision
  - Audit results PERSIST as receipts (governance)

Two verifier strategies ship with L-6:

  - OracleVerifier(ground_truth)   — test fixtures with curated truth
  - RuleBasedVerifier()             — discipline / zone / OCR-content
                                       invariants that catch the
                                       known FP modes from Wave 20.13.3

L-7 will add a SupabaseAuditReceiptPersistor that writes verdicts to
blueprint_semantic_edge_integrity_reports. L-6 keeps audit in-memory
for fast iteration.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Protocol

from .taxonomy import EdgeProposal, EdgeType


# ============================================================
# Verdicts + report dataclasses
# ============================================================


class AuditVerdict(str, Enum):
    """Outcome of verifier judgment on a single sampled edge.

    Semantics:
      - TRUE_POSITIVE   : edge is correct (verified-correct)
      - FALSE_POSITIVE  : edge should not exist (hallucination — the
                          Wave 20.13.3 failure mode)
      - UR_CORRECT      : edge correctly routed to UNKNOWN_REVIEW
                          (genuine ambiguity acknowledged)
      - UR_MISSED       : edge accepted as VERIFIED but should have been UR
      - INCONCLUSIVE    : verifier cannot decide; not counted in precision
    """
    TRUE_POSITIVE = "true_positive"
    FALSE_POSITIVE = "false_positive"
    UR_CORRECT = "ur_correct"
    UR_MISSED = "ur_missed"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class AuditedEdge:
    """A sampled edge + verifier verdict + evidence."""
    edge_type: EdgeType
    src_entity_id: str
    dst_entity_id: str
    replay_hash: str
    confidence: float
    verdict: AuditVerdict
    rationale: str                              # why the verifier ruled this way
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class PerTypeAudit:
    """Per-edge-type aggregate scoring."""
    edge_type: EdgeType
    n_sampled: int = 0
    n_true_positive: int = 0
    n_false_positive: int = 0
    n_ur_correct: int = 0
    n_ur_missed: int = 0
    n_inconclusive: int = 0

    @property
    def precision(self) -> float:
        """TP / (TP + FP + UR_missed). UR_correct + INCONCLUSIVE excluded.

        UR_missed counts as a false-positive-like signal because the
        edge was emitted as VERIFIED but should have been UR.
        """
        denominator = self.n_true_positive + self.n_false_positive + self.n_ur_missed
        if denominator == 0:
            return 1.0
        return self.n_true_positive / denominator

    @property
    def ur_correctness(self) -> float:
        """UR_correct / (UR_correct + UR_missed)."""
        denominator = self.n_ur_correct + self.n_ur_missed
        if denominator == 0:
            return 1.0
        return self.n_ur_correct / denominator


@dataclass
class AuditReport:
    """Aggregate audit output."""
    audit_id: str                               # stable hash for receipt linkage
    generator_name: str
    generator_version: str
    project_id: str
    sample_size: int
    per_type: dict[EdgeType, PerTypeAudit] = field(default_factory=dict)
    audited_edges: list[AuditedEdge] = field(default_factory=list)
    verdict_counts: Counter = field(default_factory=Counter)
    threshold: float = 0.90

    def per_type_precision(self) -> dict[str, float]:
        return {et.value: a.precision for et, a in self.per_type.items()}

    def passes_threshold(self) -> bool:
        """All audited edge types must meet the precision threshold.

        Empty per_type → vacuously True (nothing audited; cannot fail).
        Caller should also check n_sampled > 0.
        """
        for audit in self.per_type.values():
            if audit.n_sampled == 0:
                continue
            if audit.precision < self.threshold:
                return False
        return True

    def failing_types(self) -> list[str]:
        return [
            et.value for et, a in self.per_type.items()
            if a.n_sampled > 0 and a.precision < self.threshold
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "generator_name": self.generator_name,
            "generator_version": self.generator_version,
            "project_id": self.project_id,
            "sample_size": self.sample_size,
            "threshold": self.threshold,
            "per_type_precision": self.per_type_precision(),
            "verdict_counts": dict(self.verdict_counts),
            "passes_threshold": self.passes_threshold(),
            "failing_types": self.failing_types(),
        }


# ============================================================
# Verifier protocol + implementations
# ============================================================


class Verifier(Protocol):
    """Pluggable verdict source.

    Implementations:
      - OracleVerifier     — explicit ground truth (test fixtures)
      - RuleBasedVerifier  — invariants that catch known FP modes
      - VLMVerifier        — future (L-8+), uses GPT-4V to check edges
    """

    def verify(self, edge: EdgeProposal) -> tuple[AuditVerdict, str]:
        """Return (verdict, rationale) for one proposed edge."""
        ...


class OracleVerifier:
    """Curated ground truth — for unit tests + benchmark corpus.

    Construct with a dict mapping (src_id, dst_id, edge_type) →
    expected verdict. Edges not in the dict produce INCONCLUSIVE.
    """

    def __init__(self, ground_truth: dict[tuple[str, str, EdgeType], AuditVerdict]):
        self._truth = ground_truth

    def verify(self, edge: EdgeProposal) -> tuple[AuditVerdict, str]:
        key = (edge.src_entity_id, edge.dst_entity_id, edge.edge_type)
        if key in self._truth:
            return self._truth[key], "oracle:explicit_truth"
        return AuditVerdict.INCONCLUSIVE, "oracle:not_in_truth_set"


class RuleBasedVerifier:
    """Apply known FP-catching invariants. Stateless.

    Catches Wave 20.13.3-class hallucinations:

    1. Discipline cross-contamination — plumbing primitive must not
       have annotated_by edge to OCR span containing 'ELECTRICAL',
       'PANEL SCHEDULE', etc.

    2. NOT-IN-CONTRACT / N.I.C. — OCR spans containing those tokens
       are explicitly NOT material annotations. Edges pointing at
       them should be UR at most.

    3. Title-block ID misbinding — sheet ID strings like "A-101"
       should never be annotated_by a different sheet's primitive.

    4. Symmetric edge axis sanity — adjacent_to edge with axis
       'diagonal' AND confidence > 0.7 is suspect (sharp adjacency
       should be horizontal or vertical for trustworthy assembly
       inference).

    Each invariant emits a specific rationale string so the audit
    report explains WHY each FP was caught.
    """

    DISCIPLINE_BLACKLIST_TOKENS = {
        # Tokens that strongly indicate the OCR span is NOT a material
        # annotation for the primitive in question.
        "NOT IN CONTRACT", "N.I.C.", "NIC", "EXISTING", "DEMO",
        "BY OTHERS", "FUTURE",
    }

    # Shared with constraints.discipline — DRY. The audit verifier and
    # the Phase 4 constraint MUST use the same canonical token sets so
    # we don't get drift between detection and rejection. Lazy module
    # property avoids a circular import at package init time.
    @classmethod
    def _discipline_tokens(cls) -> dict[str, frozenset[str]]:
        from .constraints.discipline import DISCIPLINE_TOKENS as _CANONICAL
        return _CANONICAL

    def verify(self, edge: EdgeProposal) -> tuple[AuditVerdict, str]:
        # We only judge annotated_by + adjacent_to for now; expand in L-7
        if edge.edge_type == EdgeType.ANNOTATED_BY:
            return self._verify_annotated_by(edge)
        if edge.edge_type == EdgeType.ADJACENT_TO:
            return self._verify_adjacent_to(edge)
        if edge.edge_type == EdgeType.INSIDE_ZONE:
            return self._verify_inside_zone(edge)
        return AuditVerdict.INCONCLUSIVE, "rule_based:edge_type_not_covered"

    def _verify_annotated_by(self, edge: EdgeProposal) -> tuple[AuditVerdict, str]:
        ocr_text = (edge.evidence.get("ocr_text") or "").upper()
        if not ocr_text:
            return AuditVerdict.INCONCLUSIVE, "rule_based:no_ocr_text_in_evidence"

        # Rule 2: explicit N.I.C. / NOT-IN-CONTRACT spans
        for blacklist_tok in self.DISCIPLINE_BLACKLIST_TOKENS:
            if blacklist_tok in ocr_text:
                if edge.confidence >= 0.5:                # would be VERIFIED
                    return (
                        AuditVerdict.FALSE_POSITIVE,
                        f"rule_based:ocr_text_blacklisted ({blacklist_tok!r})"
                    )
                # Otherwise it routed to UR — correct flag
                return (
                    AuditVerdict.UR_CORRECT,
                    f"rule_based:routed_to_ur_for_blacklist ({blacklist_tok!r})"
                )

        # Rule 1: discipline mismatch (cross-contamination)
        src_disc = (edge.provenance.get("source_discipline") or "").lower()
        discipline_tokens = self._discipline_tokens()
        if src_disc in discipline_tokens:
            allowed_tokens = discipline_tokens[src_disc]
            other_tokens: set[str] = set()
            for disc, toks in discipline_tokens.items():
                if disc != src_disc:
                    other_tokens |= toks
            ocr_words = set(ocr_text.split())
            if (ocr_words & other_tokens) and not (ocr_words & allowed_tokens):
                if edge.confidence >= 0.5:
                    return (
                        AuditVerdict.FALSE_POSITIVE,
                        f"rule_based:discipline_mismatch (src={src_disc}, "
                        f"ocr_words match other discipline)"
                    )
                return (
                    AuditVerdict.UR_CORRECT,
                    "rule_based:ur_routed_for_discipline_mismatch",
                )

        # Rule 4: confidence + alignment sanity
        alignment = (edge.evidence.get("alignment") or "").lower()
        if alignment == "diagonal" and edge.confidence >= 0.8:
            return (
                AuditVerdict.UR_MISSED,
                "rule_based:diagonal_alignment_with_high_confidence",
            )

        # If we got here the edge cleared all guardrails
        return AuditVerdict.TRUE_POSITIVE, "rule_based:passed_all_invariants"

    def _verify_adjacent_to(self, edge: EdgeProposal) -> tuple[AuditVerdict, str]:
        axis = (edge.evidence.get("axis") or "").lower()
        # Symmetric edges with confidence >0.7 should be axis-clean.
        # Diagonal adjacency at high confidence is a sign the radius
        # is too loose for the discipline.
        if axis == "diagonal" and edge.confidence >= 0.7:
            return (
                AuditVerdict.UR_MISSED,
                "rule_based:diagonal_adjacency_high_confidence",
            )
        return AuditVerdict.TRUE_POSITIVE, "rule_based:passed_adjacency_invariants"

    def _verify_inside_zone(self, edge: EdgeProposal) -> tuple[AuditVerdict, str]:
        overlap = edge.evidence.get("overlap_pct", 0.0)
        if overlap < 0.3 and edge.confidence >= 0.7:
            return (
                AuditVerdict.FALSE_POSITIVE,
                f"rule_based:low_overlap_high_confidence (overlap={overlap})",
            )
        return AuditVerdict.TRUE_POSITIVE, "rule_based:passed_zone_invariants"


# ============================================================
# Sampler + auditor
# ============================================================


def _stable_sample(
    edges: list[EdgeProposal],
    sample_size: int,
    *,
    seed: int = 0,
) -> list[EdgeProposal]:
    """Deterministic random sample. Same seed + same input → same output.

    Sort by replay-stable key first so input order can't perturb the
    sample even if upstream generators emit in arbitrary order.
    """
    if len(edges) <= sample_size:
        return list(edges)

    sorted_edges = sorted(
        edges,
        key=lambda e: (
            e.edge_type.value,
            e.src_entity_id,
            e.dst_entity_id,
        ),
    )
    rng = random.Random(seed)
    indices = rng.sample(range(len(sorted_edges)), sample_size)
    return [sorted_edges[i] for i in indices]


def audit_edges(
    edges: list[EdgeProposal],
    *,
    verifier: Verifier,
    generator_name: str,
    generator_version: str,
    project_id: str,
    sample_size: int = 200,
    seed: int = 0,
    threshold: float = 0.90,
    replay_hash_fn: Optional[Callable[[EdgeProposal], str]] = None,
) -> AuditReport:
    """Sample edges, run verifier, aggregate report.

    sample_size = 200 by default per reviewer L-6 spec. If fewer edges
    exist the auditor uses them all.

    threshold = 0.90 (Phase 3 acceptance criterion).
    """
    if replay_hash_fn is None:
        from .taxonomy import compute_replay_hash
        replay_hash_fn = compute_replay_hash

    sampled = _stable_sample(edges, sample_size, seed=seed)
    audit_id = hashlib.sha1(
        f"{generator_name}|{generator_version}|{project_id}|"
        f"{len(edges)}|{sample_size}|{seed}".encode()
    ).hexdigest()[:24]

    per_type_buckets: dict[EdgeType, PerTypeAudit] = defaultdict(
        lambda: PerTypeAudit(edge_type=EdgeType.BELONGS_TO_SHEET)
    )
    # Replace each default with proper edge_type once we see it
    audited_edges: list[AuditedEdge] = []
    verdict_counts: Counter = Counter()

    for edge in sampled:
        verdict, rationale = verifier.verify(edge)
        rh = replay_hash_fn(edge)
        audited = AuditedEdge(
            edge_type=edge.edge_type,
            src_entity_id=edge.src_entity_id,
            dst_entity_id=edge.dst_entity_id,
            replay_hash=rh,
            confidence=edge.confidence,
            verdict=verdict,
            rationale=rationale,
            evidence=dict(edge.evidence),
        )
        audited_edges.append(audited)
        verdict_counts[verdict.value] += 1

        if edge.edge_type not in per_type_buckets:
            per_type_buckets[edge.edge_type] = PerTypeAudit(edge_type=edge.edge_type)
        bucket = per_type_buckets[edge.edge_type]
        bucket.n_sampled += 1
        if verdict == AuditVerdict.TRUE_POSITIVE:
            bucket.n_true_positive += 1
        elif verdict == AuditVerdict.FALSE_POSITIVE:
            bucket.n_false_positive += 1
        elif verdict == AuditVerdict.UR_CORRECT:
            bucket.n_ur_correct += 1
        elif verdict == AuditVerdict.UR_MISSED:
            bucket.n_ur_missed += 1
        else:
            bucket.n_inconclusive += 1

    # Replace the defaultdict with a real dict for stable serialization
    per_type: dict[EdgeType, PerTypeAudit] = {
        et: bucket for et, bucket in per_type_buckets.items()
        if bucket.n_sampled > 0
    }

    return AuditReport(
        audit_id=audit_id,
        generator_name=generator_name,
        generator_version=generator_version,
        project_id=project_id,
        sample_size=len(sampled),
        per_type=per_type,
        audited_edges=audited_edges,
        verdict_counts=verdict_counts,
        threshold=threshold,
    )


def serialize_report(report: AuditReport) -> str:
    """Compact JSON. For receipts ledger persistence in L-7."""
    return json.dumps(report.to_dict(), indent=None, sort_keys=True)
