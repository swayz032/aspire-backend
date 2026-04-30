"""Calendar ingestion — two adapters write `calendar_event` memory_objects.

Pass 14 expansion. Both adapters produce `memory_type='calendar_event'`.
`detail.calendar_source` ('aspire' vs 'google') distinguishes origin so
Pass 15 UI and Pass 5 search treat them uniformly without branching on
memory_type.

---

## GoogleCalendarIngestionAdapter

Google Calendar uses push notifications (channel-based). Each ping has NO body
— the adapter reads X-Goog-Channel-ID / X-Goog-Resource-ID /
X-Goog-Resource-State headers, then fetches changed events from the
events.list API using the syncToken stored in provider_connections.

Auth / signature: Channel ID validated against `provider_connections` table
(provider='google_calendar'). Google does NOT HMAC-sign these pings; channel
ID match IS the authentication.

Sync flow:
  1. `sync` state → initial channel confirmation, no-op (return 200).
  2. `exists` state → call events.list with syncToken, write one memory_object
     per changed event.
  3. Store new syncToken in provider_connections after successful sweep.

Idempotency: `gcal-event-{event_id}` per event. Updates create new rows via
supersede pattern (linked_memory_ids refs prior version) — append-only (Law #2).

---

## AspireCalendarIngestionAdapter

Internal Aspire calendar events (source='manual', 'ava', 'booking') write
memory_objects when created, updated, or deleted. Route layer enforces JWT auth
+ capability token before invoking. `verify_signature` returns True — security
boundary is the route, not this adapter.

Idempotency:
  - created:  `aspire-cal-{event_id}`
  - updated:  `aspire-cal-{event_id}-v{updated_at_ts}` (new row per edit)
  - deleted:  `aspire-cal-{event_id}-deleted`

memory_type = 'calendar_event' per migration 103 / plan §14 expansion.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    Provenance,
    ScopedIdentity,
    ThreadOut,
)
from aspire_orchestrator.services.ingestion.base import (
    BaseIngestionAdapter,
    IngestionError,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# Google Calendar push notification adapter
# ===========================================================================


class GoogleCalendarIngestionAdapter(BaseIngestionAdapter):
    """Google Calendar push notification → `calendar_event` memory_objects.

    The route reads X-Goog-* headers and passes them in the payload dict:
      {
        "channel_id": "...",
        "resource_id": "...",
        "resource_state": "sync" | "exists" | "not_exists",
      }
    Raw body is empty (Google sends no body on push pings).
    """

    provider_name = "google_calendar"
    memory_type = "calendar_event"

    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """Validate Google Calendar channel ID against provider_connections.

        Google does not HMAC-sign push notifications. Channel ID match against
        a known provider_connections row is the authentication mechanism.
        """
        channel_id = (
            headers.get("x-goog-channel-id")
            or headers.get("X-Goog-Channel-ID")
            or ""
        )
        if not channel_id:
            logger.warning("google_calendar ping missing X-Goog-Channel-ID")
            return False
        try:
            rows = await supabase_select(
                table="provider_connections",
                filters={
                    "provider": "google_calendar",
                    "external_account_id": channel_id,
                },
                limit=1,
            )
        except SupabaseClientError as exc:
            logger.warning("google_calendar channel lookup failed: %s", exc)
            return False
        return bool(rows)

    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        """Resolve tenant from channel_id via provider_connections."""
        channel_id: str = payload.get("channel_id") or ""
        if not channel_id:
            raise IngestionError(
                "Google Calendar payload missing channel_id",
                code="MISSING_CHANNEL_ID",
                status_code=422,
            )
        try:
            rows = await supabase_select(
                table="provider_connections",
                filters={
                    "provider": "google_calendar",
                    "external_account_id": channel_id,
                },
                limit=1,
            )
        except SupabaseClientError as exc:
            raise IngestionError(
                f"provider_connections query failed: {exc.detail}",
                code="PROVIDER_CONNECTIONS_UNAVAILABLE",
                status_code=503,
            ) from exc
        if not rows:
            raise IngestionError(
                f"Google Calendar channel {channel_id} not linked to any tenant",
                code="UNKNOWN_CHANNEL",
                status_code=404,
            )
        row = rows[0]
        return ScopedIdentity(
            tenant_id=UUID(row["tenant_id"]),
            suite_id=UUID(row["suite_id"]),
            office_id=UUID(row["office_id"]),
        )

    async def build_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
        thread: ThreadOut | None,
    ) -> MemoryObjectIn:
        """Build a memory_objects row for one Google Calendar event.

        Called once per event returned by events.list. The route layer must
        expand multi-event batches and call ingest() per event.
        """
        event: dict[str, Any] = payload.get("event") or {}
        if not event:
            raise IngestionError(
                "Google Calendar payload missing 'event' dict",
                code="MISSING_EVENT",
                status_code=422,
            )

        event_id: str = event.get("id") or ""
        if not event_id:
            raise IngestionError(
                "Google Calendar event missing 'id'",
                code="MISSING_EVENT_ID",
                status_code=422,
            )

        summary: str = event.get("summary") or "Untitled event"
        description: str | None = event.get("description")
        location: str | None = event.get("location")
        html_link: str = event.get("htmlLink") or ""

        # Start / end (may be date-only for all-day events)
        start_block: dict[str, Any] = event.get("start") or {}
        end_block: dict[str, Any] = event.get("end") or {}
        start_dt_str: str | None = start_block.get("dateTime") or start_block.get("date")
        end_dt_str: str | None = end_block.get("dateTime") or end_block.get("date")
        tz: str = start_block.get("timeZone") or "UTC"

        start_dt = _parse_iso(start_dt_str) or datetime.now(timezone.utc)
        end_dt = _parse_iso(end_dt_str)

        # Duration string for summary
        duration_str = ""
        if end_dt and start_dt:
            mins = int((end_dt - start_dt).total_seconds() / 60)
            if mins < 60:
                duration_str = f" • {mins}min"
            else:
                duration_str = f" • {mins // 60}h{(mins % 60):02d}m" if mins % 60 else f" • {mins // 60}h"

        loc_part = f" • {location}" if location else ""
        start_display = start_dt.strftime("%Y-%m-%d %H:%M")

        # Attendees
        attendees: list[dict[str, Any]] = []
        for a in event.get("attendees") or []:
            if isinstance(a, dict):
                attendees.append({
                    "email": a.get("email", ""),
                    "responseStatus": a.get("responseStatus", "needsAction"),
                    "self": a.get("self", False),
                    "organizer": a.get("organizer", False),
                })
        attendee_count = len(attendees)
        attendee_part = f" • {attendee_count} attendee{'s' if attendee_count != 1 else ''}" if attendees else ""

        # Organizer
        organizer: dict[str, Any] = event.get("organizer") or {}
        organizer_email: str = organizer.get("email") or ""

        # Video link
        conference_data: dict[str, Any] = event.get("conferenceData") or {}
        video_link: str | None = None
        for ep in conference_data.get("entryPoints") or []:
            if isinstance(ep, dict) and ep.get("entryPointType") == "video":
                video_link = ep.get("uri")
                break

        # Recurring event reference
        recurring_event_id: str | None = event.get("recurringEventId")

        # Prior version (supersede pattern)
        prior_memory_id: str | None = payload.get("prior_memory_id")

        # Idempotency
        idempotency_key = f"gcal-event-{event_id}"
        linked_memory_ids: list[str] = [prior_memory_id] if prior_memory_id else []

        ns = uuid.NAMESPACE_URL
        trace_id = uuid.uuid5(ns, f"google-cal:trace:{idempotency_key}")
        correlation_id = uuid.uuid5(ns, f"google-cal:corr:{event_id}")

        title = summary
        mem_summary = f"{start_display}{duration_str}{loc_part}{attendee_part}"

        detail: dict[str, Any] = {
            "event_id": event_id,
            "calendar_id": payload.get("calendar_id") or "primary",
            "summary": summary,
            "description": (description or "")[:80] or None,  # Law #9: first 80 chars
            "start": {"dateTime": start_dt_str, "timeZone": tz},
            "end": {"dateTime": end_dt_str, "timeZone": tz},
            "location": location,
            "attendees": attendees,
            "organizer": organizer_email,
            "status": event.get("status") or "confirmed",
            "recurring_event_id": recurring_event_id,
            "html_link": html_link,
            "video_link": video_link,
            "calendar_source": "google",
        }
        if linked_memory_ids:
            detail["supersedes_memory_ids"] = linked_memory_ids

        return MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="google_calendar",
                runtime_family="provider_webhook",
                channel="webhook",
                source_record_id=event_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="calendar_event",
            entity_type=None,
            entity_id=None,
            thread_id=thread.thread_id if thread else None,
            title=title,
            summary=mem_summary,
            detail=detail,
            confidence=None,
            visibility_scope="office",
            status="executed",
            event_at=start_dt,
            idempotency_key=idempotency_key,
        )

    async def ingest(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        payload: dict[str, Any],
    ) -> "IngestionResult":  # type: ignore[name-defined]  # noqa: F821
        """Override: handle sync no-op + per-event loop for Google Calendar.

        Google sends a push ping with NO body. The route extracts the
        X-Goog-* headers into payload before calling us. We:
          1. Validate channel ID (via verify_signature calling resolve_scope).
          2. Return no-op on 'sync' state.
          3. Fetch events via events.list with syncToken.
          4. Write one memory_object per changed event.
          5. Update syncToken in provider_connections.
          6. Return the last written IngestionResult (or a synthetic no-op).
        """
        from aspire_orchestrator.services.ingestion.base import IngestionResult

        resource_state: str = payload.get("resource_state") or ""
        channel_id: str = payload.get("channel_id") or ""

        # 1. Signature / channel validation
        if not await self.verify_signature(body=body, headers=headers):
            logger.warning("google_calendar channel_id invalid or unknown: %s", channel_id)
            raise IngestionError(
                "Google Calendar channel ID not recognized",
                code="SIGNATURE_INVALID",
                status_code=401,
            )

        # 2. Initial sync confirmation — no-op
        if resource_state == "sync":
            logger.info("google_calendar sync_confirmation channel=%s", channel_id)
            raise IngestionError(
                "Google Calendar sync ping — no memory written",
                code="SYNC_PING_NO_OP",
                status_code=200,
            )

        # 3. Resolve scope from channel_id
        scope = await self.resolve_scope(payload)

        # 4. Fetch changed events from Google API
        events = await _fetch_google_events(channel_id=channel_id, scope=scope)
        if not events:
            logger.info("google_calendar no_changed_events channel=%s", channel_id)
            raise IngestionError(
                "No changed Google Calendar events",
                code="NO_EVENTS",
                status_code=200,
            )

        # 5. Write one memory_object per event
        last_result: IngestionResult | None = None
        for event_item in events:
            try:
                event_payload = {**payload, "event": event_item}
                envelope = await self.build_envelope(event_payload, scope=scope, thread=None)
                memory = await self._memory_service.write(envelope, scope=scope, embed=True)
                last_result = IngestionResult(memory=memory, deduplicated=False)
                logger.info(
                    "google_calendar event_written event_id=%s memory_id=%s",
                    event_item.get("id"),
                    memory.memory_id,
                )
            except IngestionError:
                raise
            except Exception as exc:
                logger.warning(
                    "google_calendar event_write_failed event_id=%s error=%s",
                    event_item.get("id"),
                    exc,
                )
                continue

        if last_result is None:
            raise IngestionError(
                "All Google Calendar events failed to write",
                code="ALL_EVENTS_FAILED",
                status_code=500,
            )

        return last_result


# ===========================================================================
# Aspire internal calendar adapter
# ===========================================================================


class AspireCalendarIngestionAdapter(BaseIngestionAdapter):
    """Aspire internal calendar events → `calendar_event` memory_objects.

    Invoked from the authenticated /v1/ingest/aspire-calendar route. The route
    layer enforces JWT auth + capability token. `verify_signature` returns True.

    Payload shape:
      {
        tenant_id, suite_id, office_id,
        event_id,          # Aspire UUID for the calendar_events row
        action,            # "created" | "updated" | "deleted"
        title,
        description?,
        start_at,          # ISO-8601
        end_at,            # ISO-8601
        timezone,
        location?,
        attendees: [{user_id?, email, response_status?}],
        created_by_user_id,
        recurrence_rule?,
        idempotency_key?,  # client-provided override
      }
    """

    provider_name = "aspire_calendar"
    memory_type = "calendar_event"

    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """No external HMAC — security boundary is the authenticated route layer."""
        _ = (body, headers)
        return True

    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        """Scope is carried in the pre-authenticated payload."""
        tenant_id_raw: str | None = payload.get("tenant_id")
        suite_id_raw: str | None = payload.get("suite_id")
        office_id_raw: str | None = payload.get("office_id")
        if not tenant_id_raw or not suite_id_raw or not office_id_raw:
            raise IngestionError(
                "Aspire calendar payload missing tenant_id/suite_id/office_id",
                code="MISSING_SCOPE_FIELDS",
                status_code=422,
            )
        try:
            return ScopedIdentity(
                tenant_id=UUID(tenant_id_raw),
                suite_id=UUID(suite_id_raw),
                office_id=UUID(office_id_raw),
            )
        except ValueError as exc:
            raise IngestionError(
                f"Invalid scope UUID: {exc}",
                code="INVALID_SCOPE_UUID",
                status_code=422,
            ) from exc

    async def build_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
        thread: ThreadOut | None,
    ) -> MemoryObjectIn:
        """Build a memory_objects row of type='calendar_event'."""
        event_id: str = payload.get("event_id") or ""
        if not event_id:
            raise IngestionError(
                "Aspire calendar payload missing 'event_id'",
                code="MISSING_EVENT_ID",
                status_code=422,
            )

        action: str = payload.get("action") or "created"
        if action not in ("created", "updated", "deleted"):
            raise IngestionError(
                f"Invalid Aspire calendar action: {action!r}",
                code="INVALID_ACTION",
                status_code=422,
            )

        title_raw: str = payload.get("title") or "Untitled event"
        description: str | None = payload.get("description")
        start_at_raw: str | None = payload.get("start_at")
        end_at_raw: str | None = payload.get("end_at")
        tz: str = payload.get("timezone") or "UTC"
        location: str | None = payload.get("location")
        attendees: list[dict[str, Any]] = payload.get("attendees") or []
        created_by: str = payload.get("created_by_user_id") or ""
        recurrence_rule: str | None = payload.get("recurrence_rule")
        client_key: str | None = payload.get("idempotency_key")

        start_dt = _parse_iso(start_at_raw) or datetime.now(timezone.utc)
        end_dt = _parse_iso(end_at_raw)

        # Idempotency key per action
        if action == "deleted":
            idempotency_key = client_key or f"aspire-cal-{event_id}-deleted"
        elif action == "updated":
            # Use updated_at or now as version suffix so each edit is a new row
            version_suffix = start_at_raw or datetime.now(timezone.utc).isoformat()
            idempotency_key = client_key or f"aspire-cal-{event_id}-v{version_suffix}"
        else:
            idempotency_key = client_key or f"aspire-cal-{event_id}"

        # Memory status
        mem_status = "superseded" if action == "deleted" else "executed"

        # Human-readable summary
        start_display = start_dt.strftime("%Y-%m-%d %H:%M")
        duration_str = ""
        if end_dt and start_dt:
            mins = int((end_dt - start_dt).total_seconds() / 60)
            duration_str = f" • {mins}min" if mins < 60 else (
                f" • {mins // 60}h{(mins % 60):02d}m" if mins % 60 else f" • {mins // 60}h"
            )

        loc_part = f" • {location}" if location else ""
        n_att = len(attendees)
        att_part = f" • {n_att} attendee{'s' if n_att != 1 else ''}" if attendees else ""

        if action == "deleted":
            title = f"Event deleted — {title_raw}"
            mem_summary = f"Event '{title_raw}' was deleted by {created_by or 'user'}."
        else:
            title = title_raw
            mem_summary = f"{start_display}{duration_str}{loc_part}{att_part}"

        ns = uuid.NAMESPACE_URL
        trace_id = uuid.uuid5(ns, f"aspire-cal:trace:{idempotency_key}")
        correlation_id = uuid.uuid5(ns, f"aspire-cal:corr:{event_id}")

        detail: dict[str, Any] = {
            "event_id": event_id,
            "action": action,
            "title": title_raw,
            "description": (description or "")[:80] or None,
            "start_at": start_at_raw,
            "end_at": end_at_raw,
            "timezone": tz,
            "location": location,
            "attendees": attendees,
            "organizer": created_by,
            "recurrence_rule": recurrence_rule,
            "calendar_source": "aspire",
        }

        return MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="aspire_calendar",
                runtime_family="ui",
                channel="ui",
                source_record_id=event_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="calendar_event",
            entity_type=None,
            entity_id=None,
            thread_id=thread.thread_id if thread else None,
            title=title,
            summary=mem_summary,
            detail=detail,
            confidence=None,
            visibility_scope="office",
            status=mem_status,
            event_at=start_dt,
            idempotency_key=idempotency_key,
        )


# ---------------------------------------------------------------------------
# Google Calendar API helper
# ---------------------------------------------------------------------------

async def _fetch_google_events(
    *,
    channel_id: str,
    scope: ScopedIdentity,
) -> list[dict[str, Any]]:
    """Fetch changed Google Calendar events using stored syncToken.

    Reads syncToken from provider_connections.sync_token (JSONB field).
    Falls back to `timeMin = 90 days ago` if no syncToken exists.
    Updates syncToken after successful fetch.
    """
    import httpx

    google_client_id = getattr(settings, "google_calendar_client_id", "")
    google_client_secret = getattr(settings, "google_calendar_client_secret", "")

    # Fetch the connection row for this channel to get access_token + sync_token
    try:
        rows = await supabase_select(
            table="provider_connections",
            filters={
                "provider": "google_calendar",
                "external_account_id": channel_id,
            },
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error("google_calendar fetch_events connection lookup failed: %s", exc)
        return []

    if not rows:
        logger.error("google_calendar channel %s not found during events fetch", channel_id)
        return []

    row = rows[0]
    access_token: str = row.get("access_token") or row.get("credentials", {}).get("access_token") or ""
    sync_token: str | None = row.get("sync_token") or None
    calendar_id: str = row.get("calendar_id") or "primary"

    if not access_token:
        logger.warning("google_calendar no access_token for channel=%s", channel_id)
        return []

    # Build events.list params
    params: dict[str, str] = {"maxResults": "250", "singleEvents": "true"}
    if sync_token:
        params["syncToken"] = sync_token
    else:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        params["timeMin"] = cutoff
        params["orderBy"] = "startTime"

    events: list[dict[str, Any]] = []
    new_sync_token: str | None = None

    async with httpx.AsyncClient(timeout=10.0) as client:
        page_token: str | None = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            resp = await client.get(
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 410:
                # Sync token expired — clear it and re-fetch from 90 days ago
                logger.warning("google_calendar syncToken expired for channel=%s — full resync", channel_id)
                params.pop("syncToken", None)
                from datetime import timedelta
                cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
                params["timeMin"] = cutoff
                params["orderBy"] = "startTime"
                page_token = None
                continue
            if resp.status_code != 200:
                logger.warning(
                    "google_calendar events_list failed channel=%s status=%d",
                    channel_id, resp.status_code,
                )
                break
            data = resp.json()
            events.extend(data.get("items") or [])
            new_sync_token = data.get("nextSyncToken")
            page_token = data.get("nextPageToken")
            if not page_token:
                break

    # Persist new syncToken (best-effort — failure is non-fatal)
    if new_sync_token:
        try:
            from aspire_orchestrator.services.supabase_client import supabase_client
            await supabase_client.table("provider_connections").update(
                {"sync_token": new_sync_token}
            ).eq("provider", "google_calendar").eq("external_account_id", channel_id).execute()
        except Exception as exc:
            logger.warning("google_calendar syncToken update failed channel=%s: %s", channel_id, exc)

    return events


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# Convenience alias used by spec smoke test + any import that needs a single name
CalendarIngestionAdapter = GoogleCalendarIngestionAdapter

__all__ = [
    "GoogleCalendarIngestionAdapter",
    "AspireCalendarIngestionAdapter",
    "CalendarIngestionAdapter",
]
