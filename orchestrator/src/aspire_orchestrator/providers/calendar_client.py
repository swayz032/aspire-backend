"""Calendar Provider — CRUD operations on calendar_events via Supabase PostgREST.

Uses the Supabase client (services/supabase_client.py) for all DB operations.
RLS-scoped by suite_id — tenant isolation is enforced at the DB layer (Law #6).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)


def _validate_uuid(value: str, field_name: str) -> None:
    """Defense-in-depth: validate UUID format before filter interpolation (prevent injection)."""
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"Invalid UUID for {field_name}: {value!r}")


async def execute_calendar_event_create(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str, office_id: str = "", **kwargs: Any,
) -> ToolExecutionResult:
    """INSERT a new calendar event via Supabase PostgREST."""
    from aspire_orchestrator.services.supabase_client import supabase_insert

    tool_id = "calendar.event.create"
    try:
        data = {
            "id": str(uuid.uuid4()),
            "suite_id": suite_id,
            "title": payload.get("title", "Untitled Event"),
            "description": payload.get("description", ""),
            "event_type": payload.get("event_type", "meeting"),
            "start_time": payload.get("start_time"),
            "duration_minutes": payload.get("duration_minutes", 30),
            "location": payload.get("location"),
            "status": "scheduled",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if payload.get("participants"):
            data["participants"] = payload["participants"]

        result = await supabase_insert("calendar_events", data)
        receipt = {
            "tool_used": tool_id,
            "suite_id": suite_id,
            "correlation_id": correlation_id,
            "outcome": "success",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS, tool_id=tool_id,
            data=result if isinstance(result, dict) else {"created": True, "event_id": data["id"]},
            receipt_data=receipt,
        )
    except Exception as e:
        logger.error("Calendar event create failed: %s", e)
        receipt = {
            "tool_used": tool_id,
            "suite_id": suite_id,
            "correlation_id": correlation_id,
            "outcome": "failed",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error=str(e), receipt_data=receipt,
        )


async def execute_calendar_event_list(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str, **kwargs: Any,
) -> ToolExecutionResult:
    """SELECT today's events for suite (RLS-scoped)."""
    from aspire_orchestrator.services.supabase_client import supabase_select

    tool_id = "calendar.event.list"
    try:
        _validate_uuid(suite_id, "suite_id")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filters = f"suite_id=eq.{suite_id}&start_time=gte.{today}T00:00:00&start_time=lte.{today}T23:59:59&order=start_time.asc"
        result = await supabase_select("calendar_events", filters)
        receipt = {
            "tool_used": tool_id,
            "suite_id": suite_id,
            "correlation_id": correlation_id,
            "outcome": "success",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS, tool_id=tool_id,
            data={"events": result} if isinstance(result, list) else {"events": []},
            receipt_data=receipt,
        )
    except Exception as e:
        logger.error("Calendar event list failed: %s", e)
        receipt = {
            "tool_used": tool_id,
            "suite_id": suite_id,
            "correlation_id": correlation_id,
            "outcome": "failed",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error=str(e), receipt_data=receipt,
        )


async def execute_calendar_event_complete(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str, **kwargs: Any,
) -> ToolExecutionResult:
    """UPDATE status='completed' for an event."""
    from aspire_orchestrator.services.supabase_client import supabase_update

    tool_id = "calendar.event.complete"
    event_id = payload.get("event_id")
    if not event_id:
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error="event_id required",
            receipt_data={
                "tool_used": tool_id,
                "suite_id": suite_id,
                "correlation_id": correlation_id,
                "outcome": "failed",
                "error": "event_id required",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    try:
        _validate_uuid(event_id, "event_id")
        _validate_uuid(suite_id, "suite_id")
        result = await supabase_update(
            "calendar_events",
            f"id=eq.{event_id}&suite_id=eq.{suite_id}",
            {"status": "completed"},
        )
        receipt = {
            "tool_used": tool_id,
            "suite_id": suite_id,
            "correlation_id": correlation_id,
            "outcome": "success",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS, tool_id=tool_id,
            data=result if isinstance(result, dict) else {"completed": True},
            receipt_data=receipt,
        )
    except Exception as e:
        logger.error("Calendar event complete failed: %s", e)
        receipt = {
            "tool_used": tool_id,
            "suite_id": suite_id,
            "correlation_id": correlation_id,
            "outcome": "failed",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error=str(e), receipt_data=receipt,
        )
