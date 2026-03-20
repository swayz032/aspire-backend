"""Event activity — emit client events for real-time UI updates."""

from __future__ import annotations

import logging

from temporalio import activity

from aspire_orchestrator.temporal.models import EmitClientEventInput

logger = logging.getLogger(__name__)


@activity.defn
async def emit_client_event(input: EmitClientEventInput) -> None:
    """Emit a client event to Supabase Realtime for desktop UI updates."""
    from aspire_orchestrator.services.supabase_client import supabase_insert

    try:
        await supabase_insert(
            "client_events",
            {
                "suite_id": input.suite_id,
                "office_id": input.office_id,
                "flow_id": input.correlation_id,
                "event_type": input.event_type,
                "payload": input.payload,
            },
        )

        logger.info(
            "Emitted client event: type=%s correlation_id=%s",
            input.event_type,
            input.correlation_id,
        )

    except Exception:
        logger.exception(
            "Client event emission failed: type=%s correlation_id=%s",
            input.event_type,
            input.correlation_id,
        )
        # Non-critical — don't fail the workflow over a UI notification
        # But still raise so Temporal can retry
        raise
