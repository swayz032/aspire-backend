"""Wave 20.14.5L — CSIR Phase 3 Relational Enrichment.

Public API for the semantic edge fabric. This subpackage owns:

- Canonical edge taxonomy (taxonomy.py)
- Edge contracts (per-edge-type metadata)
- Validation tiers (structural / semantic / constraint)
- Replay hashing for receipt linkage
- Generator framework (L-3, follows)
- Edge persistence helpers (L-4+, follows)

The DB schema lives at supabase/migrations/20260525000001_blueprint_semantic_edges_csir_phase3.sql.
DB CHECK constraints are the operational gatekeeper. This module is the
canonical source-of-truth that those CHECK constraints are derived from
(see taxonomy.generate_db_check_sql).

Architectural lesson driving this layer: Wave 20.13.3 false-positive
explosion came from embedding-similarity binding with no graph
constraints. Phase 3 builds the graph; Phase 4 adds constraints;
Phase 5 grounds binding atop both. DO NOT skip Phase 4.
"""
from .taxonomy import (  # noqa: F401
    EdgeType,
    EntityType,
    LocalityScope,
    ResolutionState,
    RejectionReason,
    EdgeContract,
    EDGE_CONTRACTS,
    EdgeProposal,
    ValidationResult,
    TierResult,
    validate_structural,
    validate_semantic,
    validate_constraint,
    validate_edge,
    compute_replay_hash,
    generate_db_check_sql,
    generate_db_check_sql_locality,
    generate_db_check_sql_entity_type,
)
from .metrics import (  # noqa: F401
    GeneratorHealth,
    HEALTH_HARD_FAIL_THRESHOLD,
    HEALTH_RED_THRESHOLD,
    HEALTH_YELLOW_THRESHOLD,
    RunMetrics,
    StageMetrics,
    compute_health,
)
from .replay import (  # noqa: F401
    TAXONOMY_VERSION,
    compute_input_evidence_hash,
    compute_output_edge_hash,
    compute_taxonomy_version,
    stable_candidate_sort_key,
)
from .locality import (  # noqa: F401
    BBox,
    LocalityCapExceeded,
    LocalityCaps,
    LocalityNeighborhood,
    euclidean_distance,
    query_neighbors_within,
)
from .receipts import (  # noqa: F401
    GenerationReceipt,
    InMemoryReceiptPersistor,
    ReceiptPersistor,
    receipt_from_metrics,
)
from .generator import (  # noqa: F401
    Candidate,
    EdgeGenerator,
    GenerationResult,
    GeneratorContext,
    InMemoryLockProvider,
    LockProvider,
)
from .audit import (  # noqa: F401
    AuditedEdge,
    AuditReport,
    AuditVerdict,
    OracleVerifier,
    PerTypeAudit,
    RuleBasedVerifier,
    Verifier,
    audit_edges,
    serialize_report,
)
from .fp_regression import FP_FIXTURES, FPFixture, fixtures_by_target  # noqa: F401
from .audit_persistor import (  # noqa: F401
    ALLOWED_WRITER_AUTHORITIES,
    AuditReceiptPersistor,
    InMemoryAuditReceiptPersistor,
    SupabaseAuditReceiptPersistor,
    UnauthorizedAuditWrite,
    WriterAuthority,
    persist_audit_report,
    report_to_db_row,
)
from .constraints import (  # noqa: F401
    CONSTRAINT_REGISTRY,
    BaseConstraint,
    Constraint,
    ConstraintResult,
    ConstraintSeverity,
    DISCIPLINE_TOKENS,
    DisciplineCompatibilityConstraint,
    HARD_INCOMPATIBLE_DISCIPLINES,
)
