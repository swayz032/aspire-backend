"""M-1 — Base Constraint protocol + dataclasses.

Every Phase 4 constraint subclasses BaseConstraint, overrides check(),
and exports an instance through the CONSTRAINT_REGISTRY in __init__.py.

Severity model (locked by reviewer 2026-05-26):
  HARD → adds CONSTRAINT_VIOLATION to RejectionReasons → tier FAILS
         → ResolutionState.REJECTED at top-level validator
  SOFT → tier still PASSES but soft_violations are recorded in
         TierResult.details → top-level validator forces UR even
         when confidence is above the floor

Pure functions only: check() takes EdgeProposal, reads evidence /
provenance, returns ConstraintResult. No I/O, no DB.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol

from ..taxonomy import EdgeProposal, EdgeType


class ConstraintSeverity(str, Enum):
    HARD = "hard"          # → REJECT
    SOFT = "soft"          # → UR-downgrade


@dataclass(frozen=True)
class ConstraintResult:
    """Outcome of one constraint's check.

    passed=True is the happy path (the constraint approves the edge).
    passed=False with severity=HARD blocks the edge entirely.
    passed=False with severity=SOFT routes the edge to UNKNOWN_REVIEW
    even if its confidence would otherwise verify.
    """
    constraint_name: str
    passed: bool
    severity: ConstraintSeverity
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def is_hard_violation(self) -> bool:
        return (not self.passed) and self.severity == ConstraintSeverity.HARD

    def is_soft_violation(self) -> bool:
        return (not self.passed) and self.severity == ConstraintSeverity.SOFT


class Constraint(Protocol):
    """Structural protocol — anything with these attrs + check() works."""

    name: str
    severity: ConstraintSeverity
    applies_to: frozenset[EdgeType]

    def check(self, proposal: EdgeProposal) -> ConstraintResult: ...


class BaseConstraint(ABC):
    """ABC subclasses use to keep boilerplate minimal.

    Override `check()` and set class-level `name`, `severity`, `applies_to`.
    The helper methods produce conventional ConstraintResults so
    subclasses don't repeat the dataclass field names.
    """

    name: str = ""
    severity: ConstraintSeverity = ConstraintSeverity.HARD
    applies_to: frozenset[EdgeType] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Don't enforce on intermediate abstract subclasses
        if getattr(cls, "__abstractmethods__", None):
            return
        if not cls.name:
            raise TypeError(
                f"{cls.__name__}: must set class attr `name`"
            )
        if not cls.applies_to:
            raise TypeError(
                f"{cls.__name__}: must set class attr `applies_to` "
                f"(non-empty frozenset of EdgeType)"
            )

    @abstractmethod
    def check(self, proposal: EdgeProposal) -> ConstraintResult:
        """Return ConstraintResult for one proposed edge."""
        ...

    # ---------- helpers ----------

    def _passing(
        self,
        *,
        reason: str = "ok",
        evidence: Optional[dict[str, Any]] = None,
    ) -> ConstraintResult:
        return ConstraintResult(
            constraint_name=self.name,
            passed=True,
            severity=self.severity,
            reason=reason,
            evidence=evidence or {},
        )

    def _violation(
        self,
        *,
        reason: str,
        evidence: Optional[dict[str, Any]] = None,
        severity: Optional[ConstraintSeverity] = None,
    ) -> ConstraintResult:
        """Emit a violation. Override severity (e.g. downgrade HARD→SOFT
        when context is missing) by passing severity= keyword.
        """
        return ConstraintResult(
            constraint_name=self.name,
            passed=False,
            severity=severity or self.severity,
            reason=reason,
            evidence=evidence or {},
        )

    def _soft_skip(
        self,
        *,
        reason: str = "missing_context",
        evidence: Optional[dict[str, Any]] = None,
    ) -> ConstraintResult:
        """Constraint cannot evaluate (context not in proposal).
        Returns a SOFT violation so the top-level validator routes the
        edge to UR for human/VLM review — fail open, never silently pass.
        """
        return ConstraintResult(
            constraint_name=self.name,
            passed=False,
            severity=ConstraintSeverity.SOFT,
            reason=reason,
            evidence=evidence or {},
        )
