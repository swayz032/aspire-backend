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
