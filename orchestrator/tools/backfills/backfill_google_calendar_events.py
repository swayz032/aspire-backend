"""Backfill Google Calendar events into memory_objects — Pass 14 expansion.

Usage:
    python -m tools.backfills.backfill_google_calendar_events \\
        --tenant-id <UUID> [--dry-run]
    python -m tools.backfills.backfill_google_calendar_events \\
        --all-tenants [--dry-run]

Logic:
    1. Look up provider_connections for provider='google_calendar'.
    2. For each connection, call Google Calendar API events.list from 90 days
       ago with the stored access_token.
    3. Build envelope via GoogleCalendarIngestionAdapter.build_envelope.
    4. Idempotency: skip if idempotency_key already in memory_objects.
    5. Write via MemoryService.write (embed=False for bulk backfill).

Idempotent: safe to re-run. Existing rows skip via idempotency_key check.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


async def _fetch_calendar_events(
    access_token: str,
    calendar_id: str,
    since: datetime,
) -> list[dict[str, Any]]:
    """Fetch Google Calendar events since `since` (no syncToken — full backfill)."""
    import httpx

    events: list[dict[str, Any]] = []
    params: dict[str, str] = {
        "maxResults": "250",
        "singleEvents": "true",
        "orderBy": "startTime",
        "timeMin": since.isoformat(),
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        page_token: str | None = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            resp = await client.get(
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 401:
                logger.warning("google_calendar access_token expired for calendar=%s", calendar_id)
                break
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items") or []
            events.extend(items)
            page_token = data.get("nextPageToken")
            if not page_token:
                break

    return events


async def _run_backfill(
    tenant_ids: list[UUID] | None,
    dry_run: bool,
) -> None:
    from aspire_orchestrator.services.supabase_client import supabase_select
    from aspire_orchestrator.services.memory_service import MemoryService
    from aspire_orchestrator.services.ingestion.calendar_ingestion import GoogleCalendarIngestionAdapter
    from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

    pc_filters: dict[str, Any] = {"provider": "google_calendar"}
    if tenant_ids:
        pc_filters["tenant_id"] = str(tenant_ids[0])

    rows = await supabase_select(table="provider_connections", filters=pc_filters, limit=1000)
    if not rows:
        logger.warning("No google_calendar provider_connections found")
        return

    adapter = GoogleCalendarIngestionAdapter()
    mem_svc = MemoryService()
    since = datetime.now(timezone.utc) - timedelta(days=90)
    total_written = 0
    total_skipped = 0

    for row in rows:
        channel_id: str = row.get("external_account_id", "")
        access_token: str = row.get("access_token") or row.get("credentials", {}).get("access_token", "")
        calendar_id: str = row.get("calendar_id") or "primary"
        scope = ScopedIdentity(
            tenant_id=UUID(row["tenant_id"]),
            suite_id=UUID(row["suite_id"]),
            office_id=UUID(row["office_id"]),
        )

        if not access_token:
            logger.warning("No access_token for channel=%s — skipping", channel_id)
            continue

        logger.info("Fetching Google Calendar events for channel=%s calendar=%s", channel_id, calendar_id)
        try:
            events = await _fetch_calendar_events(access_token, calendar_id, since)
        except Exception as exc:
            logger.error("Failed to fetch events for channel=%s: %s", channel_id, exc)
            continue

        logger.info("Found %d events for channel=%s", len(events), channel_id)

        for event in events:
            event_id: str = event.get("id") or ""
            if not event_id:
                continue

            payload: dict[str, Any] = {
                "channel_id": channel_id,
                "calendar_id": calendar_id,
                "event": event,
            }

            try:
                envelope = await adapter.build_envelope(payload, scope=scope, thread=None)
            except Exception as exc:
                logger.warning("Envelope build failed for event %s: %s", event_id, exc)
                continue

            existing = await supabase_select(
                table="memory_objects",
                filters={
                    "tenant_id": str(scope.tenant_id),
                    "idempotency_key": envelope.idempotency_key,
                },
                limit=1,
            )
            if existing:
                total_skipped += 1
                continue

            if dry_run:
                logger.info("[DRY-RUN] Would write: %s title=%s", envelope.idempotency_key, envelope.title)
                total_written += 1
                continue

            try:
                await mem_svc.write(envelope, scope=scope, embed=False)
                total_written += 1
            except Exception as exc:
                logger.error("Write failed for event %s: %s", event_id, exc)

    logger.info(
        "backfill_google_calendar_events complete: written=%d skipped=%d dry_run=%s",
        total_written, total_skipped, dry_run,
    )


async def _run_backfill_aspire(
    tenant_ids: list[UUID] | None,
    dry_run: bool,
) -> None:
    """Backfill Aspire internal calendar_events table into memory_objects."""
    from aspire_orchestrator.services.supabase_client import supabase_select
    from aspire_orchestrator.services.memory_service import MemoryService
    from aspire_orchestrator.services.ingestion.calendar_ingestion import AspireCalendarIngestionAdapter
    from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

    # Get all tenants to backfill
    tenant_filters: dict[str, Any] = {}
    if tenant_ids:
        tenant_filters["id"] = str(tenant_ids[0])
    tenant_rows = await supabase_select(table="tenants", filters=tenant_filters, limit=1000)

    # For each tenant, find suite + office from suite_profiles
    adapter = AspireCalendarIngestionAdapter()
    mem_svc = MemoryService()
    since = datetime.now(timezone.utc) - timedelta(days=90)
    since_str = since.isoformat()
    total_written = 0
    total_skipped = 0

    for tenant in tenant_rows:
        tenant_id_str: str = tenant.get("id", "")
        if not tenant_id_str:
            continue

        # Get suite_profiles for this tenant
        suite_rows = await supabase_select(
            table="suite_profiles",
            filters={"tenant_id": tenant_id_str},
            limit=100,
        )
        if not suite_rows:
            continue

        for sp in suite_rows:
            suite_id_str: str = sp.get("id", "")
            office_id_str: str = sp.get("office_id") or suite_id_str  # fallback
            if not suite_id_str:
                continue

            scope = ScopedIdentity(
                tenant_id=UUID(tenant_id_str),
                suite_id=UUID(suite_id_str),
                office_id=UUID(office_id_str),
            )

            # Fetch calendar_events for this suite from last 90 days
            try:
                events = await supabase_select(
                    table="calendar_events",
                    filters={"suite_id": suite_id_str},
                    limit=10000,
                )
            except Exception as exc:
                logger.error("Failed to fetch calendar_events for suite=%s: %s", suite_id_str, exc)
                continue

            # Filter to last 90 days
            events = [
                e for e in events
                if _event_in_window(e.get("start_time"), since_str)
            ]

            logger.info(
                "Backfilling %d Aspire calendar events for suite=%s",
                len(events), suite_id_str,
            )

            for ev in events:
                event_id: str = ev.get("id", "")
                if not event_id:
                    continue

                payload: dict[str, Any] = {
                    "tenant_id": tenant_id_str,
                    "suite_id": suite_id_str,
                    "office_id": office_id_str,
                    "event_id": event_id,
                    "action": "created",
                    "title": ev.get("title") or "Untitled event",
                    "description": ev.get("description"),
                    "start_at": _to_iso(ev.get("start_time")),
                    "end_at": _to_iso(ev.get("end_time")),
                    "timezone": "UTC",
                    "location": ev.get("location"),
                    "attendees": [{"email": p} for p in (ev.get("participants") or [])],
                    "created_by_user_id": ev.get("created_by") or "",
                    "recurrence_rule": None,
                }

                try:
                    envelope = await adapter.build_envelope(payload, scope=scope, thread=None)
                except Exception as exc:
                    logger.warning("Envelope build failed for aspire event %s: %s", event_id, exc)
                    continue

                existing = await supabase_select(
                    table="memory_objects",
                    filters={
                        "tenant_id": tenant_id_str,
                        "idempotency_key": envelope.idempotency_key,
                    },
                    limit=1,
                )
                if existing:
                    total_skipped += 1
                    continue

                if dry_run:
                    logger.info("[DRY-RUN] Aspire event: %s", envelope.idempotency_key)
                    total_written += 1
                    continue

                try:
                    await mem_svc.write(envelope, scope=scope, embed=False)
                    total_written += 1
                except Exception as exc:
                    logger.error("Write failed for aspire event %s: %s", event_id, exc)

    logger.info(
        "backfill_aspire_calendar_events complete: written=%d skipped=%d dry_run=%s",
        total_written, total_skipped, dry_run,
    )


def _event_in_window(ts: Any, since_str: str) -> bool:
    if not ts:
        return False
    try:
        ts_str = str(ts).replace(" ", "T")
        if "+" not in ts_str and not ts_str.endswith("Z"):
            ts_str += "+00:00"
        return ts_str >= since_str
    except Exception:
        return False


def _to_iso(ts: Any) -> str | None:
    if not ts:
        return None
    s = str(ts)
    return s.replace(" ", "T").replace("+00:00", "Z") if "+" in s else s


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill Google Calendar + Aspire calendar events into memory_objects"
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--tenant-id", help="Single tenant UUID")
    group.add_argument("--all-tenants", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Print plan without writing")
    p.add_argument(
        "--source",
        choices=["google", "aspire", "both"],
        default="both",
        help="Which calendar source to backfill (default: both)",
    )
    return p.parse_args()


async def _main() -> int:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    tenant_ids: list[UUID] | None = [UUID(args.tenant_id)] if args.tenant_id else None
    if args.dry_run:
        logger.info("[DRY-RUN] Mode active — no writes will occur")

    if args.source in ("google", "both"):
        await _run_backfill(tenant_ids=tenant_ids, dry_run=args.dry_run)
    if args.source in ("aspire", "both"):
        await _run_backfill_aspire(tenant_ids=tenant_ids, dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
