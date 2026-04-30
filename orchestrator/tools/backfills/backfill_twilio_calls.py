"""Backfill Twilio voice calls into memory_objects — Pass 14 Gate Item 1.

Usage:
    python -m tools.backfills.backfill_twilio_calls \\
        --tenant-id <UUID> --since 2026-01-29 [--dry-run]

Logic:
    1. Look up tenant_phone_numbers for the given tenant(s).
    2. For each phone number, call Twilio API:
       GET /2010-04-01/Accounts/{AccountSid}/Calls?To=<number>&StartTime>=<date>
       AND GET .../Calls?From=<number>&StartTime>=<date> (for outbound)
    3. For calls with recordings: build envelope via CallRecordingIngestionAdapter.
    4. Idempotency: skip if idempotency_key already in memory_objects.
    5. Write via MemoryService.write.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


async def _fetch_twilio_calls(
    phone_number: str,
    since: datetime,
    account_sid: str,
    auth_token: str,
    direction_field: str = "To",
) -> list[dict[str, Any]]:
    """Fetch Twilio calls for a number (inbound or outbound) since `since`."""
    import httpx

    calls: list[dict[str, Any]] = []
    since_str = since.strftime("%Y-%m-%d")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"
    creds = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    params: dict[str, Any] = {
        direction_field: phone_number,
        "StartTime>": since_str,
        "PageSize": "100",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Basic {creds}"},
            )
            resp.raise_for_status()
            data = resp.json()
            calls.extend(data.get("calls", []))
            next_page_uri = data.get("next_page_uri")
            if not next_page_uri:
                break
            url = f"https://api.twilio.com{next_page_uri}"
            params = {}  # URL contains all params for next page

    return calls


async def _run_backfill(
    tenant_ids: list[UUID] | None,
    since: datetime,
    dry_run: bool,
) -> None:
    from aspire_orchestrator.config.settings import settings
    from aspire_orchestrator.services.supabase_client import supabase_select
    from aspire_orchestrator.services.memory_service import MemoryService
    from aspire_orchestrator.services.ingestion.call_ingestion import CallRecordingIngestionAdapter
    from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

    phone_filters: dict[str, Any] = {}
    if tenant_ids:
        phone_filters["tenant_id"] = str(tenant_ids[0])

    phone_rows = await supabase_select(table="tenant_phone_numbers", filters=phone_filters, limit=1000)
    if not phone_rows:
        logger.warning("No tenant_phone_numbers found")
        return

    adapter = CallRecordingIngestionAdapter()
    mem_svc = MemoryService()
    account_sid = getattr(settings, "twilio_account_sid", "")
    auth_token = getattr(settings, "twilio_auth_token", "")
    total_written = 0
    total_skipped = 0

    for phone_row in phone_rows:
        phone_number: str = phone_row["phone_number"]
        scope = ScopedIdentity(
            tenant_id=UUID(phone_row["tenant_id"]),
            suite_id=UUID(phone_row["suite_id"]),
            office_id=UUID(phone_row["office_id"]),
        )

        logger.info("Fetching Twilio calls for number=%s", phone_number)
        all_calls: list[dict[str, Any]] = []
        for direction_field in ("To", "From"):
            try:
                calls = await _fetch_twilio_calls(phone_number, since, account_sid, auth_token, direction_field)
                all_calls.extend(calls)
            except Exception as exc:
                logger.error("Failed to fetch calls for %s dir=%s: %s", phone_number, direction_field, exc)

        seen_call_sids: set[str] = set()
        for call in all_calls:
            call_sid: str = call.get("sid", "")
            if call_sid in seen_call_sids:
                continue
            seen_call_sids.add(call_sid)

            # Only process calls that have a recording
            if call.get("subresource_uris", {}).get("recordings"):
                recording_sid = f"RE-backfill-{call_sid}"  # Placeholder until recording list is fetched
            else:
                continue

            payload = {
                "CallSid": call_sid,
                "RecordingSid": recording_sid,
                "From": call.get("from", ""),
                "To": call.get("to", phone_number),
                "RecordingUrl": f"https://api.twilio.com/Accounts/{account_sid}/Recordings/{recording_sid}",
                "RecordingStatus": "completed",
                "RecordingDuration": str(call.get("duration", "0")),
                "RecordingChannels": "1",
                "AccountSid": account_sid,
            }

            try:
                envelope = await adapter.build_envelope(payload, scope=scope, thread=None)
            except Exception as exc:
                logger.warning("Envelope build failed for call %s: %s", call_sid, exc)
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
                logger.error("Write failed: %s", exc)

    logger.info(
        "backfill_twilio_calls complete: written=%d skipped=%d dry_run=%s",
        total_written, total_skipped, dry_run,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill Twilio calls into memory_objects")
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
        logger.info("[DRY-RUN] Mode active")
    await _run_backfill(tenant_ids=tenant_ids, since=since_dt, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
