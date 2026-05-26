"""Wave 20.14.5M (CSIR Phase 4) — Constraint Extraction (the moat layer).

This subpackage fills the `validate_constraint()` Phase 4 hookpoint that
L-2 reserved (it returned PASS unconditionally in Phase 3). Each module
in this package owns ONE constraint family. The registry below pins the
load order; the validator iterates it cheap-to-expensive.

Architectural rules locked by reviewer 2026-05-26:

  1. **HARD violations → REJECT.** Reserved for absolute physical /
     logical impossibilities. Example: plumbing primitive annotated by
     an electrical legend.

  2. **SOFT violations → UR-downgrade.** Suspicious or context-missing
     scenarios. Example: demo layer overlapping new-work layer.
     Architects make drafting mistakes; the GC makes the final call.

  3. **Constraints are PURE functions.** No DB calls inside check().
     A constraint reads only the EdgeProposal (evidence + provenance).
     If context is missing the constraint returns SOFT pass with a
     `missing_context` reason — fail open, audit catches it later.

  4. **Each constraint owns its own data.** Discipline matrix lives in
     discipline.py. Layer rules live in layer.py. No global state.

  5. **DRY with L-6 RuleBasedVerifier.** The discipline matrix +
     DISCIPLINE_TOKENS live HERE; audit.py imports from this module.
     Single source of truth for the FP-killer rules.

CONSTRAINT_REGISTRY ordering: cheap discipline check first (O(1) set
lookup), then layer (set lookup + provenance read), then connectivity
(graph-aware logic — Phase 4 M-4), then sheet hierarchy (M-5).
"""
from .base import (  # noqa: F401
    BaseConstraint,
    Constraint,
    ConstraintResult,
    ConstraintSeverity,
)
from .discipline import (  # noqa: F401
    DISCIPLINE_TOKENS,
    HARD_INCOMPATIBLE_DISCIPLINES,
    DisciplineCompatibilityConstraint,
)
from .layer import (  # noqa: F401
    LayerSeparationConstraint,
    LayerState,
    classify_layer_name,
    classify_layer_text,
    layer_states_incompatible,
)
from .fixture_class import (  # noqa: F401
    FIXTURE_KEYWORDS,
    FixtureProfile,
    FixtureRole,
    FlowStream,
    RETURN_STREAMS,
    SUPPLY_STREAMS,
    classify_fixture,
    role_compatible_for_feeds,
    role_compatible_for_returns,
    streams_share,
)
from .connectivity import ConnectivityConsistencyConstraint  # noqa: F401

# The registry. Order matters: cheap → expensive.
# M-5 will append additional constraint instances here.
CONSTRAINT_REGISTRY: list[Constraint] = [
    DisciplineCompatibilityConstraint(),       # HARD — trade isolation
    LayerSeparationConstraint(),               # SOFT — demo/new-work review
    ConnectivityConsistencyConstraint(),       # SOFT — MEP connection sanity
]
