"""Missing-input resolvers — Wave 19b Drew Self-Resolution Cascade.

Each resolver attempts to answer a category of missing_input without
contractor involvement. Resolvers are pure functions (no DB writes) that
return a ResolverResult. The caller (missing_input_cascade) decides
whether to persist a resolved value or emit a missing_input row.

Law compliance:
  Law #1 — Resolvers return data; cascade + Drew decide persistence.
  Law #3 — Fail soft per method (never raises); confidence=0.0 is the signal.
  Law #6 — All DB queries in resolvers include suite_id (tenant scope).
  Law #9 — OCR text never logged; only matched patterns and counts.
"""
from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCategory,
    MissingInputCandidate,
    ResolverResult,
)
from aspire_orchestrator.services.blueprint.resolvers.discipline_resolver import DisciplineResolver
from aspire_orchestrator.services.blueprint.resolvers.scale_resolver import ScaleResolver
from aspire_orchestrator.services.blueprint.resolvers.addenda_resolver import AddendaResolver
from aspire_orchestrator.services.blueprint.resolvers.dimension_resolver import DimensionResolver
from aspire_orchestrator.services.blueprint.resolvers.detail_resolver import DetailResolver
from aspire_orchestrator.services.blueprint.resolvers.schedule_resolver import ScheduleResolver

__all__ = [
    "MissingInputCategory",
    "MissingInputCandidate",
    "ResolverResult",
    "DisciplineResolver",
    "ScaleResolver",
    "AddendaResolver",
    "DimensionResolver",
    "DetailResolver",
    "ScheduleResolver",
]
