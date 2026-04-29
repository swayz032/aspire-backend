"""Memory refinery activity — bridges Temporal to TranscriptEventRefinery.

Pulls a `MemoryEventEnvelope` from `memory_event_inbox` by event_id, runs the
appropriate refiner, writes 1..N memory_objects + 0..M proactive_candidates,
and returns a RefineResult for the workflow to inspect.

Aspire Laws:
  Law #2: every state-change emits a receipt (handled inside MemoryService.write
          and ProactiveCandidateEngine.create_candidate — the activity itself does
          not double-write receipts).
  Law #3: on exception, the refinery sets `memory_event_inbox.status='dead_letter'`
          and reports an incident. The activity re-raises so Temporal records
          the failure for replay.
"""

from __future__ import annotations

import logging
from uuid import UUID

from temporalio import activity

from aspire_orchestrator.schemas.memory_v1 import RefineResult

logger = logging.getLogger(__name__)


@activity.defn
async def memory_refinery_activity(event_id: str) -> dict:
    """Run the refinery for a single inbox event.

    `event_id` is passed as `str` for Temporal serialization stability — it is
    parsed back to UUID inside the activity.

    Returns:
        Plain `dict` (Temporal-serializable) representation of `RefineResult`.

    Raises:
        Any exception from the refiner is re-raised so Temporal can decide
        whether to retry. The refinery itself has already marked the inbox row
        as dead-lettered before re-raising.
    """
    # Lazy import to avoid module-level coupling during worker startup.
    from aspire_orchestrator.services.entity_thread_resolver import EntityThreadResolver
    from aspire_orchestrator.services.memory_service import MemoryService
    from aspire_orchestrator.services.proactive_candidate_engine import (
        ProactiveCandidateEngine,
    )
    from aspire_orchestrator.services.transcript_event_refinery import (
        TranscriptEventRefinery,
    )

    parsed_event_id = UUID(event_id)

    memory_service = MemoryService()
    thread_resolver = EntityThreadResolver()
    candidate_engine = ProactiveCandidateEngine()
    refinery = TranscriptEventRefinery(
        memory_service=memory_service,
        thread_resolver=thread_resolver,
        candidate_engine=candidate_engine,
    )

    try:
        result: RefineResult = await refinery.refine(parsed_event_id)
        logger.info(
            "memory_refinery_activity.success: event_id=%s memories=%d candidates=%d",
            event_id,
            len(result.memory_ids),
            len(result.candidate_ids),
        )
        return result.model_dump(mode="json")
    except Exception:
        logger.exception("memory_refinery_activity.failure: event_id=%s", event_id)
        raise
