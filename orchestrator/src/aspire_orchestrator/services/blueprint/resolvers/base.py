"""Base resolver contract — Wave 19b.

MissingInputResolver ABC and ResolverResult dataclass.
Every resolver must implement `resolve()` and return a ResolverResult.

Design rules:
  - resolve() NEVER raises. All errors are caught and returned as
    ResolverResult(resolved=False, confidence=0.0, method="<failed>").
  - 15s per-method timeout enforced by the cascade orchestrator.
  - Resolved threshold: confidence >= 0.85.
  - Receipts emitted by cascade (not by individual resolvers).

Law compliance:
  Law #1 — Returns data; cascade decides what to do with it.
  Law #7 — No autonomous decisions inside resolve().
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_log = logging.getLogger(__name__)

RESOLVED_CONFIDENCE_THRESHOLD = 0.85


class MissingInputCategory(str, Enum):
    """Maps a missing_input description to a resolver category.

    Used by the cascade orchestrator to route candidates to the
    correct resolver(s).
    """
    DISCIPLINE = "discipline"
    SCALE = "scale"
    ADDENDA = "addenda"
    DIMENSION = "dimension"
    DETAIL = "detail"
    SCHEDULE = "schedule"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MissingInputCandidate:
    """A missing_input row that has not yet been resolved.

    Populated from the blueprint_missing_inputs table row plus
    enough project context for resolvers to work with.
    """
    # DB row identity
    missing_input_id: str          # UUID of the blueprint_missing_inputs row
    project_id: str
    suite_id: str
    office_id: str | None
    description: str
    suggested_resolution: str | None

    # Routing
    category: MissingInputCategory  # Derived by cascade from description keywords

    # Project context passed through so resolvers don't need a DB call
    sheets: list[dict[str, Any]] = field(default_factory=list)
    symbols_by_sheet: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    ocr_by_sheet: dict[str, str] = field(default_factory=dict)  # sheet_id -> ocr_text

    # Wave 19.1a — Optional read-only access to SEE-stage artifacts (PNGs,
    # YOLO detections). Set by the cascade dispatcher to a per-run
    # ``SheetDataProvider`` instance. Resolvers that need PNG bytes or YOLO
    # bboxes must read through this field; the field is ``None`` for any
    # caller that has not been migrated yet (tests, legacy fixtures), and
    # resolvers MUST treat ``None`` as "data plane not available" and
    # degrade gracefully rather than raise.
    #
    # Typed as ``Any`` to avoid a circular import with ``sheet_data_provider``
    # which itself lives under ``resolvers/``. Concrete type is
    # ``aspire_orchestrator.services.blueprint.resolvers.sheet_data_provider.SheetDataProvider``.
    data_provider: Any | None = None


@dataclass(frozen=True)
class ResolverResult:
    """Outcome of a single resolver attempt.

    resolved=True + confidence >= 0.85 → cascade writes the answer and
                                          marks missing_input resolved.
    resolved=False OR confidence < 0.85 → cascade tries the next method;
                                          if all fail, the missing_input
                                          stays open for contractor.
    """
    resolved: bool
    value: Any | None           # The answer (e.g., discipline string, scale float, etc.)
    confidence: float           # 0.0–1.0
    method: str                 # Which method produced this result (for receipt evidence)
    evidence: dict[str, Any]    # Method-specific evidence (safe to log — no raw OCR)
    category: MissingInputCategory

    @property
    def is_confident(self) -> bool:
        return self.resolved and self.confidence >= RESOLVED_CONFIDENCE_THRESHOLD


class MissingInputResolver(ABC):
    """Base class for all missing-input resolvers."""

    category: MissingInputCategory  # Subclasses declare their category

    @abstractmethod
    async def resolve(self, candidate: MissingInputCandidate) -> list[ResolverResult]:
        """Attempt resolution using all available methods.

        Returns a list of ResolverResult, one per method attempted.
        Order does not imply priority — cascade picks the highest confidence.
        Never raises; swallow errors and return ResolverResult(resolved=False).
        """

    def _safe_result(
        self,
        method: str,
        exc: Exception,
        category: MissingInputCategory | None = None,
    ) -> ResolverResult:
        """Build a failed ResolverResult without propagating the exception."""
        _log.warning(
            "resolver %s.%s failed: %s",
            self.__class__.__name__,
            method,
            type(exc).__name__,
        )
        return ResolverResult(
            resolved=False,
            value=None,
            confidence=0.0,
            method=method,
            evidence={"error": type(exc).__name__},
            category=category or self.category,
        )
