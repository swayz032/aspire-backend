"""Wave 20.14.5L — Canonical Edge Taxonomy + Validation Tiers.

This module is the SINGLE SOURCE OF TRUTH for what a semantic edge is.
The DB CHECK constraints in migration 20260525000001 are derived from
EDGE_CONTRACTS below; if you ever change the taxonomy, regenerate the
CHECK SQL via `generate_db_check_sql()` and emit a follow-up migration.

KEY ARCHITECTURAL RULES (locked by reviewer 2026-05-25):

1. Python is canonical; DB CHECK is the operational gate. No Postgres ENUM.
   Taxonomy evolves; ENUM mutations are operationally painful.

2. Validator emits STRUCTURED ValidationResult, never boolean. Every
   rejection produces a receipt-ready record (reason code, violated
   contract, evidence missing, replay hash).

3. THREE tiers, never collapsed:
   - structural — entity types fit the edge's contract
   - semantic   — locality / confidence / evidence shape valid
   - constraint — Phase 4 hookpoint (returns PASS in Phase 3; Phase 4
                  injects discipline + connectivity + cardinality rules)

4. Generators do NOT self-police locality. Validator enforces it
   centrally. Generators are untrusted producers; validator is the
   enforcement authority.

5. UNKNOWN_REVIEW is a first-class governed state. Binary VERIFIED/
   REJECTED would recreate the Wave 20.13.3 false-positive explosion.

6. Symmetric vs directional is explicit on each contract. Graph
   traversal semantics depend on it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ============================================================
# Enumerations (mirror the DB CHECK values exactly)
# ============================================================


class EdgeType(str, Enum):
    """20 edge types across 4 semantic classes.

    Class membership is metadata only — see EdgeContract.edge_class.
    """
    # Referential (4) — how things refer to other things
    ANNOTATED_BY = "annotated_by"
    REFERENCES_DETAIL = "references_detail"
    CALLS_OUT = "calls_out"
    LEGEND_ENTRY_FOR = "legend_entry_for"

    # Structural (5) — how things are organized geometrically
    MEMBER_OF_FAMILY = "member_of_family"
    BELONGS_TO_SHEET = "belongs_to_sheet"
    INSIDE_ZONE = "inside_zone"
    ADJACENT_TO = "adjacent_to"
    ALIGNED_WITH = "aligned_with"

    # Functional (7) — how things behave (the engineering verbs)
    CONNECTED_TO = "connected_to"
    FEEDS = "feeds"
    RETURNS_TO = "returns_to"
    PROTECTED_BY = "protected_by"
    CONTROLS = "controls"
    SERVES = "serves"
    TERMINATES_AT = "terminates_at"

    # Constraint (4) — the rule layer (Phase 4 fills these in)
    CONSTRAINED_BY = "constrained_by"
    DISCIPLINE_COMPATIBLE_WITH = "discipline_compatible_with"
    REQUIRES = "requires"
    EXCLUDES = "excludes"


class EntityType(str, Enum):
    """6 polymorphic edge endpoints."""
    PRIMITIVE = "primitive"
    COMPOSED_SYMBOL = "composed_symbol"
    LEGEND = "legend"
    SHEET = "sheet"
    ZONE = "zone"
    OCR_SPAN = "ocr_span"


class LocalityScope(str, Enum):
    """5 mandatory anti-explosion scopes (DB CHECK enforced)."""
    SAME_ZONE = "same_zone"
    SAME_SHEET = "same_sheet"
    ADJACENT_ZONE = "adjacent_zone"
    CROSS_SHEET_REF = "cross_sheet_ref"
    PROJECT_GLOBAL = "project_global"


class ResolutionState(str, Enum):
    """Tri-state edge resolution. NEVER binary."""
    VERIFIED = "verified"               # all tiers passed; edge accepted
    UNKNOWN_REVIEW = "unknown_review"   # ambiguous; needs human/VLM review
    REJECTED = "rejected"               # hard fail; do not persist


class RejectionReason(str, Enum):
    """Structured rejection codes. Every rejected edge gets one."""
    INVALID_EDGE_TYPE = "invalid_edge_type"
    INVALID_SRC_TYPE = "invalid_src_type"
    INVALID_DST_TYPE = "invalid_dst_type"
    INVALID_LOCALITY_SCOPE = "invalid_locality_scope"
    MISSING_LOCALITY = "missing_locality"
    CONFIDENCE_BELOW_FLOOR = "confidence_below_floor"
    CONFIDENCE_OUT_OF_RANGE = "confidence_out_of_range"
    EVIDENCE_MISSING_KEYS = "evidence_missing_keys"
    PROVENANCE_MISSING_KEYS = "provenance_missing_keys"
    SPATIAL_DISTANCE_REQUIRED = "spatial_distance_required"
    SELF_LOOP_DISALLOWED = "self_loop_disallowed"
    SYMMETRIC_DUPLICATE_DIRECTION = "symmetric_duplicate_direction"
    CONSTRAINT_VIOLATION = "constraint_violation"     # Phase 4
    SEMANTIC_AMBIGUITY = "semantic_ambiguity"         # routes to UNKNOWN_REVIEW


# ============================================================
# Contract: per-edge-type metadata
# ============================================================


@dataclass(frozen=True)
class EdgeContract:
    """Immutable contract per edge type.

    The validator consults this contract to decide structural fit,
    locality, evidence shape, and direction. New edge types require
    BOTH (a) a value in EdgeType AND (b) an entry in EDGE_CONTRACTS,
    AND (c) DB CHECK regenerated via generate_db_check_sql().
    """
    edge_type: EdgeType
    edge_class: str                                 # 'referential' | 'structural' | 'functional' | 'constraint'
    valid_src_types: frozenset[EntityType]
    valid_dst_types: frozenset[EntityType]

    # Locality
    requires_locality: bool
    valid_locality_scopes: frozenset[LocalityScope]
    spatial_distance_required: bool                 # if True, edge.spatial_distance MUST be set

    # Direction semantics
    symmetric: bool                                 # if True, (A→B) == (B→A); validator dedups
    directional: bool                               # mutually exclusive with symmetric

    # Evidence + confidence policy
    required_evidence_keys: frozenset[str]
    required_provenance_keys: frozenset[str]
    confidence_floor: float                         # below floor → REJECTED
    ambiguity_floor: float                          # below floor but above ambiguity → UNKNOWN_REVIEW
    allow_self_loop: bool = False                   # True only for self-referential edges (none currently)

    def __post_init__(self):
        # Invariants — frozen dataclass __post_init__ is allowed for validation
        if self.symmetric == self.directional:
            raise ValueError(
                f"{self.edge_type}: must be exactly one of symmetric/directional, got "
                f"symmetric={self.symmetric}, directional={self.directional}"
            )
        if not (0.0 <= self.ambiguity_floor <= self.confidence_floor <= 1.0):
            raise ValueError(
                f"{self.edge_type}: require 0 <= ambiguity_floor ({self.ambiguity_floor}) "
                f"<= confidence_floor ({self.confidence_floor}) <= 1"
            )
        if self.requires_locality and not self.valid_locality_scopes:
            raise ValueError(
                f"{self.edge_type}: requires_locality=True but valid_locality_scopes is empty"
            )


# Universal provenance keys every edge must carry (reviewer mandate)
_PROVENANCE_REQUIRED = frozenset({
    "extractor", "wave", "pipeline_version", "sheet_id",
    "generator_window", "bbox",
})


def _contract(
    edge_type: EdgeType,
    edge_class: str,
    valid_src: set[EntityType],
    valid_dst: set[EntityType],
    *,
    locality_scopes: set[LocalityScope],
    spatial_required: bool = False,
    symmetric: bool = False,
    directional: bool = True,
    evidence_keys: set[str],
    confidence_floor: float = 0.5,
    ambiguity_floor: Optional[float] = None,
) -> EdgeContract:
    """Constructor sugar — defaults match the common case."""
    if ambiguity_floor is None:
        # ambiguity floor = floor - 0.2, clamped to 0
        ambiguity_floor = max(0.0, confidence_floor - 0.2)
    return EdgeContract(
        edge_type=edge_type,
        edge_class=edge_class,
        valid_src_types=frozenset(valid_src),
        valid_dst_types=frozenset(valid_dst),
        requires_locality=bool(locality_scopes),
        valid_locality_scopes=frozenset(locality_scopes),
        spatial_distance_required=spatial_required,
        symmetric=symmetric,
        directional=directional,
        required_evidence_keys=frozenset(evidence_keys),
        required_provenance_keys=_PROVENANCE_REQUIRED,
        confidence_floor=confidence_floor,
        ambiguity_floor=ambiguity_floor,
    )


# Shorthand for entity types used in contracts
_PRIM = EntityType.PRIMITIVE
_COMP = EntityType.COMPOSED_SYMBOL
_LEG = EntityType.LEGEND
_SHEET = EntityType.SHEET
_ZONE = EntityType.ZONE
_OCR = EntityType.OCR_SPAN

# Shorthand for locality scopes
_SAME_Z = LocalityScope.SAME_ZONE
_SAME_S = LocalityScope.SAME_SHEET
_ADJ_Z = LocalityScope.ADJACENT_ZONE
_CROSS = LocalityScope.CROSS_SHEET_REF
_GLOBAL = LocalityScope.PROJECT_GLOBAL


# ============================================================
# THE REGISTRY — single source of truth for 20 edge types
# ============================================================

EDGE_CONTRACTS: dict[EdgeType, EdgeContract] = {

    # ---------- Referential (4) ----------
    EdgeType.ANNOTATED_BY: _contract(
        EdgeType.ANNOTATED_BY, "referential",
        valid_src={_PRIM, _COMP},
        valid_dst={_OCR},
        locality_scopes={_SAME_S, _SAME_Z},
        spatial_required=True,
        directional=True,
        evidence_keys={"ocr_text", "distance_px", "alignment", "zone"},
        confidence_floor=0.5,
    ),
    EdgeType.REFERENCES_DETAIL: _contract(
        EdgeType.REFERENCES_DETAIL, "referential",
        valid_src={_OCR, _PRIM},
        valid_dst={_SHEET},
        locality_scopes={_CROSS},
        directional=True,
        evidence_keys={"callout_text", "target_sheet_code"},
        confidence_floor=0.65,
    ),
    EdgeType.CALLS_OUT: _contract(
        EdgeType.CALLS_OUT, "referential",
        valid_src={_OCR},
        valid_dst={_PRIM, _COMP},
        locality_scopes={_SAME_S, _SAME_Z},
        spatial_required=True,
        directional=True,
        evidence_keys={"text", "target_type", "distance_px"},
        confidence_floor=0.55,
    ),
    EdgeType.LEGEND_ENTRY_FOR: _contract(
        EdgeType.LEGEND_ENTRY_FOR, "referential",
        valid_src={_COMP},
        valid_dst={_LEG},
        # Legend rows span many sheets; this is the ONLY edge where
        # PROJECT_GLOBAL is the natural scope.
        locality_scopes={_GLOBAL},
        directional=True,
        evidence_keys={"topology_similarity", "ocr_match", "family_id"},
        confidence_floor=0.7,
    ),

    # ---------- Structural (5) ----------
    EdgeType.MEMBER_OF_FAMILY: _contract(
        EdgeType.MEMBER_OF_FAMILY, "structural",
        valid_src={_PRIM, _COMP},
        valid_dst={_COMP, _LEG},
        locality_scopes={_SAME_S, _SAME_Z, _CROSS, _GLOBAL},
        directional=True,
        evidence_keys={"family_id", "similarity"},
        confidence_floor=0.6,
    ),
    EdgeType.BELONGS_TO_SHEET: _contract(
        EdgeType.BELONGS_TO_SHEET, "structural",
        valid_src={_PRIM, _COMP, _LEG, _ZONE, _OCR},
        valid_dst={_SHEET},
        locality_scopes={_SAME_S},
        directional=True,
        evidence_keys={"page_index"},
        confidence_floor=0.99,   # trivially deterministic
        ambiguity_floor=0.99,
    ),
    EdgeType.INSIDE_ZONE: _contract(
        EdgeType.INSIDE_ZONE, "structural",
        valid_src={_PRIM, _COMP, _OCR},
        valid_dst={_ZONE},
        locality_scopes={_SAME_Z},
        directional=True,
        evidence_keys={"containment_method", "overlap_pct"},
        confidence_floor=0.7,
    ),
    EdgeType.ADJACENT_TO: _contract(
        EdgeType.ADJACENT_TO, "structural",
        valid_src={_PRIM},
        valid_dst={_PRIM},
        locality_scopes={_SAME_S},
        spatial_required=True,
        symmetric=True, directional=False,
        evidence_keys={"distance_px", "axis"},
        confidence_floor=0.5,
    ),
    EdgeType.ALIGNED_WITH: _contract(
        EdgeType.ALIGNED_WITH, "structural",
        valid_src={_PRIM},
        valid_dst={_PRIM},
        locality_scopes={_SAME_S},
        spatial_required=True,
        symmetric=True, directional=False,
        evidence_keys={"axis", "alignment_tolerance_px"},
        confidence_floor=0.6,
    ),

    # ---------- Functional (7) ----------
    EdgeType.CONNECTED_TO: _contract(
        EdgeType.CONNECTED_TO, "functional",
        valid_src={_PRIM, _COMP},
        valid_dst={_PRIM, _COMP},
        locality_scopes={_SAME_S},
        spatial_required=True,
        symmetric=True, directional=False,
        evidence_keys={"contact_method", "distance_px"},
        confidence_floor=0.55,
    ),
    EdgeType.FEEDS: _contract(
        EdgeType.FEEDS, "functional",
        valid_src={_PRIM, _COMP},
        valid_dst={_PRIM, _COMP},
        locality_scopes={_SAME_S, _CROSS},
        directional=True,
        evidence_keys={"flow_direction", "discipline"},
        confidence_floor=0.6,
    ),
    EdgeType.RETURNS_TO: _contract(
        EdgeType.RETURNS_TO, "functional",
        valid_src={_PRIM, _COMP},
        valid_dst={_PRIM, _COMP},
        locality_scopes={_SAME_S, _CROSS},
        directional=True,
        evidence_keys={"flow_direction", "discipline"},
        confidence_floor=0.6,
    ),
    EdgeType.PROTECTED_BY: _contract(
        EdgeType.PROTECTED_BY, "functional",
        valid_src={_PRIM, _COMP},
        valid_dst={_PRIM, _COMP},
        locality_scopes={_SAME_S, _CROSS},
        directional=True,
        evidence_keys={"protection_type"},
        confidence_floor=0.55,
    ),
    EdgeType.CONTROLS: _contract(
        EdgeType.CONTROLS, "functional",
        valid_src={_PRIM, _COMP},
        valid_dst={_PRIM, _COMP},
        locality_scopes={_SAME_S, _CROSS},
        directional=True,
        evidence_keys={"control_type"},
        confidence_floor=0.55,
    ),
    EdgeType.SERVES: _contract(
        EdgeType.SERVES, "functional",
        valid_src={_PRIM, _COMP},
        valid_dst={_ZONE, _SHEET},
        locality_scopes={_SAME_S, _CROSS, _GLOBAL},
        directional=True,
        evidence_keys={"service_type"},
        confidence_floor=0.5,
    ),
    EdgeType.TERMINATES_AT: _contract(
        EdgeType.TERMINATES_AT, "functional",
        valid_src={_PRIM},
        valid_dst={_PRIM, _COMP},
        locality_scopes={_SAME_S},
        spatial_required=True,
        directional=True,
        evidence_keys={"termination_geometry"},
        confidence_floor=0.6,
    ),

    # ---------- Constraint (4) — Phase 4 will fill validate_constraint ----------
    EdgeType.CONSTRAINED_BY: _contract(
        EdgeType.CONSTRAINED_BY, "constraint",
        valid_src={_PRIM, _COMP, _LEG},
        valid_dst={_PRIM, _COMP, _LEG, _ZONE},
        locality_scopes={_GLOBAL, _SAME_S, _CROSS},
        directional=True,
        evidence_keys={"constraint_rule", "source"},
        confidence_floor=0.5,
    ),
    EdgeType.DISCIPLINE_COMPATIBLE_WITH: _contract(
        EdgeType.DISCIPLINE_COMPATIBLE_WITH, "constraint",
        valid_src={_PRIM, _COMP},
        valid_dst={_LEG, _ZONE},
        locality_scopes={_GLOBAL},
        symmetric=True, directional=False,
        evidence_keys={"discipline_src", "discipline_dst"},
        confidence_floor=0.5,
    ),
    EdgeType.REQUIRES: _contract(
        EdgeType.REQUIRES, "constraint",
        valid_src={_PRIM, _COMP, _LEG},
        valid_dst={_PRIM, _COMP, _LEG},
        locality_scopes={_GLOBAL, _CROSS},
        directional=True,
        evidence_keys={"requirement_type"},
        confidence_floor=0.5,
    ),
    EdgeType.EXCLUDES: _contract(
        EdgeType.EXCLUDES, "constraint",
        valid_src={_PRIM, _COMP, _LEG},
        valid_dst={_PRIM, _COMP, _LEG},
        locality_scopes={_GLOBAL, _CROSS},
        symmetric=True, directional=False,
        evidence_keys={"exclusion_reason"},
        confidence_floor=0.5,
    ),
}

# Assertion: registry covers every EdgeType
assert set(EDGE_CONTRACTS.keys()) == set(EdgeType), (
    f"EDGE_CONTRACTS missing entries for: "
    f"{set(EdgeType) - set(EDGE_CONTRACTS.keys())}"
)


# ============================================================
# Proposal + result dataclasses (the validator I/O contract)
# ============================================================


@dataclass(frozen=True)
class EdgeProposal:
    """The input shape generators emit to the validator.

    NOTE: This is the proposed edge — not yet persisted. The validator
    decides whether to accept, reject, or route to UNKNOWN_REVIEW. Only
    accepted edges (ResolutionState.VERIFIED) should be written to the
    blueprint_semantic_edges table.
    """
    edge_type: EdgeType
    suite_id: str
    project_id: str
    src_entity_id: str
    src_entity_type: EntityType
    dst_entity_id: str
    dst_entity_type: EntityType
    locality_scope: Optional[LocalityScope]
    confidence: float
    evidence: dict[str, Any]
    provenance: dict[str, Any]
    extraction_method: str
    src_entity_version: int = 1
    dst_entity_version: int = 1
    spatial_distance: Optional[float] = None
    office_id: Optional[str] = None


@dataclass(frozen=True)
class TierResult:
    """Output of a single validation tier (structural / semantic / constraint)."""
    tier: str                                       # 'structural' | 'semantic' | 'constraint'
    passed: bool
    rejection_reasons: tuple[RejectionReason, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    """Final validator output. Always emitted; receipts derive from this.

    A REJECTED result is NOT a logging miss — it's the architecture's way
    of refusing certainty. Persist the rejection alongside the proposal's
    replay_hash for forensic reconstruction.
    """
    resolution_state: ResolutionState
    edge_type: EdgeType
    replay_hash: str
    structural: TierResult
    semantic: TierResult
    constraint: TierResult
    final_rejection_reasons: tuple[RejectionReason, ...] = ()
    final_violated_contract: Optional[str] = None    # edge_type.value when rejected
    evidence_missing: tuple[str, ...] = ()
    provenance_missing: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.resolution_state == ResolutionState.VERIFIED

    @property
    def review_required(self) -> bool:
        return self.resolution_state == ResolutionState.UNKNOWN_REVIEW


# ============================================================
# Replay hash (deterministic, receipt-link-able)
# ============================================================


def compute_replay_hash(proposal: EdgeProposal) -> str:
    """Deterministic SHA-1 over the proposal's identity fields.

    Two proposals with identical (edge_type, src_id+ver+type, dst_id+ver+type,
    extraction_method, evidence) produce the same hash. This is the join key
    between blueprint_semantic_edges (output_edge_hash) and
    blueprint_semantic_edge_generation_receipts (output_edge_hash).
    """
    canonical = json.dumps(
        {
            "edge_type": proposal.edge_type.value,
            "src": [
                str(proposal.src_entity_id),
                proposal.src_entity_type.value,
                int(proposal.src_entity_version),
            ],
            "dst": [
                str(proposal.dst_entity_id),
                proposal.dst_entity_type.value,
                int(proposal.dst_entity_version),
            ],
            "extraction_method": proposal.extraction_method,
            "evidence": proposal.evidence,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:24]


# ============================================================
# Validation tiers
# ============================================================


def validate_structural(
    proposal: EdgeProposal,
    contract: EdgeContract,
) -> TierResult:
    """Tier 1: entity types fit the contract. Cheap O(1) checks."""
    reasons: list[RejectionReason] = []
    details: dict[str, Any] = {}

    if proposal.src_entity_type not in contract.valid_src_types:
        reasons.append(RejectionReason.INVALID_SRC_TYPE)
        details["src_type_given"] = proposal.src_entity_type.value
        details["src_types_allowed"] = sorted(t.value for t in contract.valid_src_types)

    if proposal.dst_entity_type not in contract.valid_dst_types:
        reasons.append(RejectionReason.INVALID_DST_TYPE)
        details["dst_type_given"] = proposal.dst_entity_type.value
        details["dst_types_allowed"] = sorted(t.value for t in contract.valid_dst_types)

    # Self-loop check
    if proposal.src_entity_id == proposal.dst_entity_id and not contract.allow_self_loop:
        reasons.append(RejectionReason.SELF_LOOP_DISALLOWED)

    return TierResult(
        tier="structural",
        passed=not reasons,
        rejection_reasons=tuple(reasons),
        details=details,
    )


def validate_semantic(
    proposal: EdgeProposal,
    contract: EdgeContract,
) -> TierResult:
    """Tier 2: locality, confidence range, evidence shape.

    Locality enforcement is CENTRALIZED here — generators do not self-police.
    """
    reasons: list[RejectionReason] = []
    details: dict[str, Any] = {}

    # Confidence range
    if not (0.0 <= proposal.confidence <= 1.0):
        reasons.append(RejectionReason.CONFIDENCE_OUT_OF_RANGE)
        details["confidence_given"] = proposal.confidence

    # Locality (mandatory anti-explosion guard)
    if contract.requires_locality:
        if proposal.locality_scope is None:
            reasons.append(RejectionReason.MISSING_LOCALITY)
        elif proposal.locality_scope not in contract.valid_locality_scopes:
            reasons.append(RejectionReason.INVALID_LOCALITY_SCOPE)
            details["locality_given"] = proposal.locality_scope.value
            details["locality_allowed"] = sorted(
                s.value for s in contract.valid_locality_scopes
            )

    # Spatial distance required for some edges
    if contract.spatial_distance_required and proposal.spatial_distance is None:
        reasons.append(RejectionReason.SPATIAL_DISTANCE_REQUIRED)

    # Evidence keys present
    missing_evidence = contract.required_evidence_keys - set(proposal.evidence.keys())
    if missing_evidence:
        reasons.append(RejectionReason.EVIDENCE_MISSING_KEYS)
        details["evidence_missing"] = sorted(missing_evidence)

    # Provenance keys present
    missing_provenance = contract.required_provenance_keys - set(proposal.provenance.keys())
    if missing_provenance:
        reasons.append(RejectionReason.PROVENANCE_MISSING_KEYS)
        details["provenance_missing"] = sorted(missing_provenance)

    return TierResult(
        tier="semantic",
        passed=not reasons,
        rejection_reasons=tuple(reasons),
        details=details,
    )


def validate_constraint(
    proposal: EdgeProposal,
    contract: EdgeContract,
) -> TierResult:
    """Tier 3: Phase 4 — iterate CONSTRAINT_REGISTRY.

    HARD violation → tier FAILS → REJECTED at top-level resolver.
    SOFT violation → tier PASSES but soft_violations recorded in
    details → top-level resolver forces UR even if confidence verifies.
    """
    # Lazy import — constraints package depends back on this module
    from .constraints import CONSTRAINT_REGISTRY

    reasons: list[RejectionReason] = []
    hard_violations: list[dict[str, Any]] = []
    soft_violations: list[dict[str, Any]] = []
    constraints_evaluated: list[str] = []

    for constraint in CONSTRAINT_REGISTRY:
        if proposal.edge_type not in constraint.applies_to:
            continue
        constraints_evaluated.append(constraint.name)
        result = constraint.check(proposal)
        if result.passed:
            continue
        record = {
            "name": result.constraint_name,
            "reason": result.reason,
            "evidence": result.evidence,
        }
        if result.is_hard_violation():
            hard_violations.append(record)
            reasons.append(RejectionReason.CONSTRAINT_VIOLATION)
        else:  # SOFT
            soft_violations.append(record)

    details: dict[str, Any] = {
        "constraints_evaluated": constraints_evaluated,
        "hard_violations": hard_violations,
        "soft_violations": soft_violations,
    }
    return TierResult(
        tier="constraint",
        passed=not hard_violations,
        rejection_reasons=tuple(reasons),
        details=details,
    )


# ============================================================
# Top-level validator
# ============================================================


def validate_edge(
    proposal: EdgeProposal,
    contracts: Optional[dict[EdgeType, EdgeContract]] = None,
) -> ValidationResult:
    """Run all 3 tiers + final resolution decision.

    Resolution logic:
      - structural FAIL                                    -> REJECTED
      - semantic FAIL                                      -> REJECTED
      - constraint HARD FAIL                               -> REJECTED
      - all 3 pass AND SOFT constraint violation           -> UNKNOWN_REVIEW
        (Phase 4 — SOFT constraints downgrade verifiable
        edges to UR even if confidence is above floor)
      - all 3 pass AND confidence >= contract.confidence_floor          -> VERIFIED
      - all 3 pass AND ambiguity_floor <= confidence < confidence_floor -> UNKNOWN_REVIEW
      - all 3 pass AND confidence < ambiguity_floor                     -> REJECTED
    """
    registry = contracts or EDGE_CONTRACTS

    # Unknown edge_type is the earliest possible rejection
    contract = registry.get(proposal.edge_type)
    if contract is None:
        replay = compute_replay_hash(proposal)
        return ValidationResult(
            resolution_state=ResolutionState.REJECTED,
            edge_type=proposal.edge_type,
            replay_hash=replay,
            structural=TierResult(tier="structural", passed=False,
                                  rejection_reasons=(RejectionReason.INVALID_EDGE_TYPE,)),
            semantic=TierResult(tier="semantic", passed=False),
            constraint=TierResult(tier="constraint", passed=False),
            final_rejection_reasons=(RejectionReason.INVALID_EDGE_TYPE,),
            final_violated_contract=proposal.edge_type.value if isinstance(
                proposal.edge_type, EdgeType
            ) else str(proposal.edge_type),
        )

    structural = validate_structural(proposal, contract)
    semantic = validate_semantic(proposal, contract)
    constraint = validate_constraint(proposal, contract)
    replay = compute_replay_hash(proposal)

    all_reasons: list[RejectionReason] = []
    all_reasons.extend(structural.rejection_reasons)
    all_reasons.extend(semantic.rejection_reasons)
    all_reasons.extend(constraint.rejection_reasons)

    evidence_missing = tuple(semantic.details.get("evidence_missing", ()))
    provenance_missing = tuple(semantic.details.get("provenance_missing", ()))

    if not (structural.passed and semantic.passed and constraint.passed):
        return ValidationResult(
            resolution_state=ResolutionState.REJECTED,
            edge_type=proposal.edge_type,
            replay_hash=replay,
            structural=structural,
            semantic=semantic,
            constraint=constraint,
            final_rejection_reasons=tuple(all_reasons),
            final_violated_contract=proposal.edge_type.value,
            evidence_missing=evidence_missing,
            provenance_missing=provenance_missing,
        )

    # Phase 4 SOFT constraint check — forces UR even if confidence verifies.
    # Locked by reviewer 2026-05-26: HARD = REJECT (handled above);
    # SOFT = UR-downgrade so the GC sees the flag instead of a silent ship.
    soft_violations = constraint.details.get("soft_violations", []) or []

    # Tiers all pass — confidence + SOFT-constraint state decide resolution
    if soft_violations:
        # SOFT constraint trumps confidence-pass — explicit UR with
        # CONSTRAINT_VIOLATION reason so audits can trace the cause.
        state = ResolutionState.UNKNOWN_REVIEW
        reasons: tuple[RejectionReason, ...] = (
            RejectionReason.CONSTRAINT_VIOLATION,
        )
    elif proposal.confidence >= contract.confidence_floor:
        state = ResolutionState.VERIFIED
        reasons = ()
    elif proposal.confidence >= contract.ambiguity_floor:
        state = ResolutionState.UNKNOWN_REVIEW
        reasons = (RejectionReason.SEMANTIC_AMBIGUITY,)
    else:
        state = ResolutionState.REJECTED
        reasons = (RejectionReason.CONFIDENCE_BELOW_FLOOR,)

    return ValidationResult(
        resolution_state=state,
        edge_type=proposal.edge_type,
        replay_hash=replay,
        structural=structural,
        semantic=semantic,
        constraint=constraint,
        final_rejection_reasons=reasons,
        final_violated_contract=(
            proposal.edge_type.value if state == ResolutionState.REJECTED else None
        ),
    )


# ============================================================
# DB CHECK constraint generator (single canonical source)
# ============================================================


def generate_db_check_sql() -> str:
    """Emit the SQL CHECK clause for edge_type, derived from EdgeType.

    Use this when adding/removing edge types: regenerate the CHECK string
    and emit a follow-up migration that DROPs the old constraint and ADDs
    the new one.
    """
    values = ",".join(f"'{e.value}'" for e in EdgeType)
    return f"edge_type IN ({values})"


def generate_db_check_sql_locality() -> str:
    """Emit the CHECK clause for locality_scope."""
    values = ",".join(f"'{s.value}'" for s in LocalityScope)
    return f"locality_scope IN ({values})"


def generate_db_check_sql_entity_type(column: str = "src_entity_type") -> str:
    """Emit the CHECK clause for an entity_type column."""
    values = ",".join(f"'{e.value}'" for e in EntityType)
    return f"{column} IN ({values})"
