"""Backfill Stripe invoices into memory_objects — Pass 14 Gate Item 1.

Usage:
    python -m tools.backfills.backfill_stripe_invoices \\
        --tenant-id <UUID> --since 2026-01-29 [--dry-run]

    python -m tools.backfills.backfill_stripe_invoices \\
        --all-tenants --since 2026-01-29 --dry-run

Logic:
    1. Look up provider_connections for provider='stripe' (per --tenant-id or all tenants).
    2. For each connection, call Stripe API: GET /v1/invoices?customer=<customer_id>&created[gte]=<ts>.
    3. For each invoice, build the envelope via InvoiceIngestionAdapter.build_envelope.
    4. Idempotency: skip if idempotency_key already exists in memory_objects.
    5. Write via MemoryService.write (cuts receipt internally — Law #2).

Idempotency is enforced by MemoryService.write — re-running is safe.

Aspire Laws:
  Law #2: Receipt cut by MemoryService.write on every insert.
  Law #3: Fail closed — missing provider_connection = skip with warning.
  Law #6: --tenant-id is required (or --all-tenants) — never a default tenant.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


async def _fetch_stripe_invoices(
    customer_id: str,
    since_ts: int,
    stripe_api_key: str,
) -> list[dict[str, Any]]:
    """Fetch invoices from Stripe API for a given customer since `since_ts`."""
    import httpx

    invoices: list[dict[str, Any]] = []
    url = "https://api.stripe.com/v1/invoices"
    params: dict[str, Any] = {
        "customer": customer_id,
        "created[gte]": str(since_ts),
        "limit": "100",
    }
    headers = {"Authorization": f"Bearer {stripe_api_key}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            for invoice in data.get("data", []):
                invoices.append(invoice)
            if not data.get("has_more"):
                break
            # Paginate
            last_id = data["data"][-1]["id"] if data["data"] else None
            if not last_id:
                break
            params["starting_after"] = last_id

    return invoices


async def _run_backfill(
    tenant_ids: list[UUID] | None,
    since: datetime,
    dry_run: bool,
) -> None:
    """Core backfill logic."""
    from aspire_orchestrator.config.settings import settings
    from aspire_orchestrator.services.supabase_client import supabase_select
    from aspire_orchestrator.services.memory_service import MemoryService
    from aspire_orchestrator.services.ingestion.invoice_ingestion import InvoiceIngestionAdapter
    from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

    since_ts = int(since.timestamp())

    # Build filter for provider_connections
    pc_filters: dict[str, Any] = {"provider": "stripe"}
    if tenant_ids:
        # Only one tenant — single lookup (multi-tenant would need OR filter, not supported here)
        pc_filters["tenant_id"] = str(tenant_ids[0])

    rows = await supabase_select(table="provider_connections", filters=pc_filters, limit=1000)
    if not rows:
        logger.warning("No provider_connections found for stripe (filters=%s)", pc_filters)
        return

    adapter = InvoiceIngestionAdapter()
    mem_svc = MemoryService()
    total_written = 0
    total_skipped = 0

    for row in rows:
        customer_id: str = row.get("external_account_id", "")
        if not customer_id:
            logger.warning("Skipping connection with missing external_account_id: %s", row)
            continue

        scope = ScopedIdentity(
            tenant_id=UUID(row["tenant_id"]),
            suite_id=UUID(row["suite_id"]),
            office_id=UUID(row["office_id"]),
        )

        logger.info("Fetching invoices for customer=%s tenant=%s", customer_id, row["tenant_id"])
        try:
            raw_invoices = await _fetch_stripe_invoices(
                customer_id=customer_id,
                since_ts=since_ts,
                stripe_api_key=settings.stripe_api_key,
            )
        except Exception as exc:
            logger.error("Failed to fetch invoices for customer=%s: %s", customer_id, exc)
            continue

        for raw_invoice in raw_invoices:
            # Wrap single invoice into Stripe event-like shape
            event_type = "invoice.created"
            invoice_status = raw_invoice.get("status", "")
            if invoice_status == "paid":
                event_type = "invoice.paid"
            elif invoice_status in ("void", "uncollectible"):
                event_type = "invoice.voided"

            payload = {
                "id": raw_invoice.get("id", ""),
                "type": event_type,
                "created": raw_invoice.get("created", int(time.time())),
                "data": {"object": raw_invoice},
            }

            try:
                envelope = await adapter.build_envelope(payload, scope=scope, thread=None)
            except Exception as exc:
                logger.warning("Envelope build failed for invoice %s: %s", raw_invoice.get("id"), exc)
                continue

            # Idempotency check
            existing = await supabase_select(
                table="memory_objects",
                filters={
                    "tenant_id": str(scope.tenant_id),
                    "idempotency_key": envelope.idempotency_key,
                },
                limit=1,
            )
            if existing:
                logger.debug("Skipping duplicate invoice idempotency_key=%s", envelope.idempotency_key)
                total_skipped += 1
                continue

            if dry_run:
                logger.info("[DRY-RUN] Would write: %s", envelope.idempotency_key)
                total_written += 1
                continue

            try:
                await mem_svc.write(envelope, scope=scope, embed=False)
                logger.info("Written: %s", envelope.idempotency_key)
                total_written += 1
            except Exception as exc:
                logger.error("Write failed for %s: %s", envelope.idempotency_key, exc)

    logger.info(
        "backfill_stripe_invoices complete: written=%d skipped=%d dry_run=%s",
        total_written, total_skipped, dry_run,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill Stripe invoices into memory_objects")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--tenant-id", help="Single tenant UUID to backfill")
    group.add_argument("--all-tenants", action="store_true", help="Backfill all Stripe-connected tenants")
    p.add_argument("--since", required=True, help="ISO-8601 date (e.g. 2026-01-29)")
    p.add_argument("--dry-run", action="store_true", help="Print planned writes without executing")
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

    tenant_ids = None
    if args.tenant_id:
        tenant_ids = [UUID(args.tenant_id)]

    if args.dry_run:
        logger.info("[DRY-RUN] Mode active — no writes will be executed")

    await _run_backfill(tenant_ids=tenant_ids, since=since_dt, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
