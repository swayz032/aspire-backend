"""Backfill Zoom cloud recordings into memory_objects — Pass 14 Gate Item 1.

Usage:
    python -m tools.backfills.backfill_zoom_recordings \\
        --tenant-id <UUID> --since 2026-01-29 [--dry-run]

Logic:
    1. Look up provider_connections for provider='zoom'.
    2. For each connection (host_id), call Zoom API:
       GET /v2/users/{host_id}/recordings?from=<date>&to=<date>
    3. For each meeting, build envelope via ZoomRecordingIngestionAdapter.build_envelope.
    4. Idempotency: skip if idempotency_key already exists.
    5. Write via MemoryService.write.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


async def _fetch_zoom_recordings(
    host_id: str,
    since: datetime,
    zoom_access_token: str,
) -> list[dict[str, Any]]:
    """Fetch Zoom cloud recordings for a host since `since`."""
    import httpx

    from_date = since.strftime("%Y-%m-%d")
    to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    meetings: list[dict[str, Any]] = []

    url = f"https://api.zoom.us/v2/users/{host_id}/recordings"
    params: dict[str, Any] = {"from": from_date, "to": to_date, "page_size": "300"}
    headers = {"Authorization": f"Bearer {zoom_access_token}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            meetings.extend(data.get("meetings", []))
            next_page_token = data.get("next_page_token", "")
            if not next_page_token:
                break
            params["next_page_token"] = next_page_token

    return meetings


async def _run_backfill(
    tenant_ids: list[UUID] | None,
    since: datetime,
    dry_run: bool,
) -> None:
    from aspire_orchestrator.config.settings import settings
    from aspire_orchestrator.services.supabase_client import supabase_select
    from aspire_orchestrator.services.memory_service import MemoryService
    from aspire_orchestrator.services.ingestion.zoom_ingestion import ZoomRecordingIngestionAdapter
    from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

    pc_filters: dict[str, Any] = {"provider": "zoom"}
    if tenant_ids:
        pc_filters["tenant_id"] = str(tenant_ids[0])

    rows = await supabase_select(table="provider_connections", filters=pc_filters, limit=1000)
    if not rows:
        logger.warning("No Zoom provider_connections found")
        return

    adapter = ZoomRecordingIngestionAdapter()
    mem_svc = MemoryService()
    total_written = 0
    total_skipped = 0

    # Zoom OAuth token — stored in settings or fetched per-connection
    zoom_token = getattr(settings, "zoom_access_token", "")

    for row in rows:
        host_id: str = row.get("external_account_id", "")
        scope = ScopedIdentity(
            tenant_id=UUID(row["tenant_id"]),
            suite_id=UUID(row["suite_id"]),
            office_id=UUID(row["office_id"]),
        )

        logger.info("Fetching Zoom recordings for host=%s tenant=%s", host_id, row["tenant_id"])
        try:
            meetings = await _fetch_zoom_recordings(host_id, since, zoom_token)
        except Exception as exc:
            logger.error("Failed to fetch Zoom recordings for host=%s: %s", host_id, exc)
            continue

        for meeting in meetings:
            # Wrap meeting into Zoom recording.completed payload shape
            payload = {
                "event": "recording.completed",
                "account_id": row.get("external_account_id", ""),
                "payload": {
                    "account_id": row.get("external_account_id", ""),
                    "object": meeting,
                },
            }

            try:
                envelope = await adapter.build_envelope(payload, scope=scope, thread=None)
            except Exception as exc:
                logger.warning("Envelope build failed for meeting %s: %s", meeting.get("uuid"), exc)
                continue

            existing = await supabase_select(
                table="memory_objects",
                filters={"tenant_id": str(scope.tenant_id), "idempotency_key": envelope.idempotency_key},
                limit=1,
            )
            if existing:
                total_skipped += 1
                continue

            if dry_run:
                logger.info("[DRY-RUN] Would write: %s", envelope.idempotency_key)
                total_written += 1
                continue

            try:
                await mem_svc.write(envelope, scope=scope, embed=False)
                total_written += 1
            except Exception as exc:
                logger.error("Write failed for %s: %s", envelope.idempotency_key, exc)

    logger.info(
        "backfill_zoom_recordings complete: written=%d skipped=%d dry_run=%s",
        total_written, total_skipped, dry_run,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill Zoom recordings into memory_objects")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--tenant-id")
    group.add_argument("--all-tenants", action="store_true")
    p.add_argument("--since", required=True)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


async def _main() -> int:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        since_dt = datetime.fromisoformat(args.since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        logger.error("Invalid --since: %s", args.since)
        return 1

    tenant_ids = [UUID(args.tenant_id)] if args.tenant_id else None
    if args.dry_run:
        logger.info("[DRY-RUN] Mode active — no writes will be executed")

    await _run_backfill(tenant_ids=tenant_ids, since=since_dt, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
