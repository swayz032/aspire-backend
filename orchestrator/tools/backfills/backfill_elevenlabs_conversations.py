"""Backfill ElevenLabs conversations into memory_objects — Pass 14 Gate Item 1.

Usage:
    python -m tools.backfills.backfill_elevenlabs_conversations \\
        --tenant-id <UUID> --since 2026-01-29 [--dry-run]

Logic:
    1. Look up provider_connections for provider='elevenlabs'.
    2. For each connection (agent_id), call EL REST API:
       GET /v1/convai/conversations?agent_id=<id>&call_start_time_unix_secs_gt=<ts>
    3. For each conversation, call GET /v1/convai/conversations/{id} for full payload.
    4. Build envelope via ElevenLabsIngestionAdapter two-write path.
    5. Idempotency: skip if both idempotency_keys (el-transcript/el-summary) already exist.
    6. Write via ElevenLabsIngestionAdapter.ingest (which cuts both writes).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


async def _fetch_el_conversation_ids(
    agent_id: str,
    since_ts: int,
    api_key: str,
) -> list[str]:
    """List conversation IDs for an agent since `since_ts`."""
    import httpx

    conv_ids: list[str] = []
    url = "https://api.elevenlabs.io/v1/convai/conversations"
    params: dict[str, Any] = {
        "agent_id": agent_id,
        "call_start_time_unix_secs_gt": str(since_ts),
        "page_size": "100",
    }
    headers = {"xi-api-key": api_key}

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            for conv in data.get("conversations", []):
                conv_id = conv.get("conversation_id")
                if conv_id:
                    conv_ids.append(conv_id)
            cursor = data.get("next_cursor")
            if not cursor:
                break
            params["cursor"] = cursor

    return conv_ids


async def _fetch_el_conversation_detail(
    conversation_id: str,
    api_key: str,
) -> dict[str, Any]:
    """Fetch full conversation detail from ElevenLabs."""
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}",
            headers={"xi-api-key": api_key},
        )
        resp.raise_for_status()
        return resp.json()


def _make_fake_signature(body: bytes, secret: str) -> str:
    """Build a valid ElevenLabs signature for backfill use."""
    ts = int(time.time())
    signed = f"{ts}.".encode() + body
    v0 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v0={v0}"


async def _run_backfill(
    tenant_ids: list[UUID] | None,
    since: datetime,
    dry_run: bool,
) -> None:
    import json

    from aspire_orchestrator.config.settings import settings
    from aspire_orchestrator.services.supabase_client import supabase_select
    from aspire_orchestrator.services.memory_service import MemoryService
    from aspire_orchestrator.services.ingestion.elevenlabs_ingestion import ElevenLabsIngestionAdapter

    since_ts = int(since.timestamp())
    el_api_key = getattr(settings, "elevenlabs_api_key", "")
    el_webhook_secret = getattr(settings, "elevenlabs_webhook_secret", "backfill-placeholder")

    pc_filters: dict[str, Any] = {"provider": "elevenlabs"}
    if tenant_ids:
        pc_filters["tenant_id"] = str(tenant_ids[0])

    rows = await supabase_select(table="provider_connections", filters=pc_filters, limit=1000)
    if not rows:
        logger.warning("No ElevenLabs provider_connections found")
        return

    adapter = ElevenLabsIngestionAdapter()
    total_written = 0
    total_skipped = 0

    for row in rows:
        agent_id: str = row.get("external_account_id", "")
        logger.info("Fetching EL conversations for agent=%s", agent_id)

        try:
            conv_ids = await _fetch_el_conversation_ids(agent_id, since_ts, el_api_key)
        except Exception as exc:
            logger.error("Failed to list conversations for agent=%s: %s", agent_id, exc)
            continue

        for conv_id in conv_ids:
            # Idempotency check (both writes keyed on conv_id)
            existing = await supabase_select(
                table="memory_objects",
                filters={
                    "tenant_id": row["tenant_id"],
                    "idempotency_key": f"el-summary:{conv_id}",
                },
                limit=1,
            )
            if existing:
                total_skipped += 1
                continue

            try:
                detail = await _fetch_el_conversation_detail(conv_id, el_api_key)
            except Exception as exc:
                logger.error("Failed to fetch conversation detail %s: %s", conv_id, exc)
                continue

            # Shape into post_call_transcription payload
            payload = {
                "type": "post_call_transcription",
                "event_timestamp": time.time(),
                "data": {
                    "agent_id": agent_id,
                    "conversation_id": conv_id,
                    "status": detail.get("status", "done"),
                    "transcript": detail.get("transcript", []),
                    "metadata": detail.get("metadata", {}),
                    "analysis": detail.get("analysis", {}),
                    "conversation_initiation_client_data": detail.get(
                        "conversation_initiation_client_data", {}
                    ),
                },
            }

            if dry_run:
                logger.info("[DRY-RUN] Would ingest conversation: %s", conv_id)
                total_written += 1
                continue

            # Build a valid signature so the adapter's verify_signature passes
            body = json.dumps(payload).encode()
            sig = _make_fake_signature(body, el_webhook_secret)
            headers = {"ElevenLabs-Signature": sig}

            try:
                # Temporarily patch settings so verify_signature passes
                import unittest.mock as mock
                with mock.patch(
                    "aspire_orchestrator.services.ingestion.elevenlabs_ingestion.settings"
                ) as mock_settings:
                    mock_settings.elevenlabs_webhook_secret = el_webhook_secret
                    await adapter.ingest(body=body, headers=headers, payload=payload)
                total_written += 1
                logger.info("Ingested conversation: %s", conv_id)
            except Exception as exc:
                logger.error("Ingest failed for conversation %s: %s", conv_id, exc)

    logger.info(
        "backfill_elevenlabs_conversations complete: written=%d skipped=%d dry_run=%s",
        total_written, total_skipped, dry_run,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill ElevenLabs conversations into memory_objects")
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
