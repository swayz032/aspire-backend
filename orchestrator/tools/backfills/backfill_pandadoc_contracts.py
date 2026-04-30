"""Backfill PandaDoc contracts into memory_objects — Pass 14 expansion.

Usage:
    python -m tools.backfills.backfill_pandadoc_contracts \\
        --tenant-id <UUID> --since 2026-01-01 [--dry-run]
    python -m tools.backfills.backfill_pandadoc_contracts \\
        --all-tenants --since 2026-01-01 [--dry-run]

Logic:
    1. Look up provider_connections for provider='pandadoc'.
    2. For each connection (workspace_id), call PandaDoc API:
       GET /public/v1/documents?workspace=<id>&count=100&page=1
    3. Filter to documents with tag 'contract' (or document_type=contract).
    4. Build envelope via ContractIngestionAdapter.build_envelope.
    5. Idempotency: skip if idempotency_key already in memory_objects.
    6. Write via MemoryService.write (embed=False for bulk backfill).

Idempotent: safe to re-run. Already-written rows are detected via
idempotency_key and skipped without error.
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


async def _fetch_pandadoc_documents(
    workspace_id: str,
    since: datetime,
    api_key: str,
) -> list[dict[str, Any]]:
    """Fetch PandaDoc documents for a workspace modified since `since`."""
    import httpx

    documents: list[dict[str, Any]] = []
    page = 1
    since_str = since.strftime("%Y-%m-%d")

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                "https://api.pandadoc.com/public/v1/documents",
                params={
                    "workspace": workspace_id,
                    "count": "100",
                    "page": str(page),
                    "modified_from": since_str,
                },
                headers={"Authorization": f"API-Key {api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if not results:
                break
            documents.extend(results)
            if len(results) < 100:
                break
            page += 1

    return documents


def _is_contract(doc: dict[str, Any]) -> bool:
    """Return True if this PandaDoc document should be treated as a contract."""
    tags: list[str] = [str(t).lower() for t in (doc.get("tags") or [])]
    if "contract" in tags:
        return True
    doc_type: str = str(doc.get("document_type") or "").lower()
    if "contract" in doc_type:
        return True
    return False


async def _run_backfill(
    tenant_ids: list[UUID] | None,
    since: datetime,
    dry_run: bool,
) -> None:
    from aspire_orchestrator.config.settings import settings
    from aspire_orchestrator.services.supabase_client import supabase_select
    from aspire_orchestrator.services.memory_service import MemoryService
    from aspire_orchestrator.services.ingestion.contract_ingestion import ContractIngestionAdapter
    from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

    pc_filters: dict[str, Any] = {"provider": "pandadoc"}
    if tenant_ids:
        pc_filters["tenant_id"] = str(tenant_ids[0])

    rows = await supabase_select(table="provider_connections", filters=pc_filters, limit=1000)
    if not rows:
        logger.warning("No PandaDoc provider_connections found")
        return

    adapter = ContractIngestionAdapter()
    mem_svc = MemoryService()
    api_key: str = getattr(settings, "pandadoc_api_key", "")
    total_written = 0
    total_skipped = 0
    total_non_contract = 0

    for row in rows:
        workspace_id: str = row.get("external_account_id", "")
        scope = ScopedIdentity(
            tenant_id=UUID(row["tenant_id"]),
            suite_id=UUID(row["suite_id"]),
            office_id=UUID(row["office_id"]),
        )

        logger.info("Fetching PandaDoc documents for workspace=%s", workspace_id)
        try:
            documents = await _fetch_pandadoc_documents(workspace_id, since, api_key)
        except Exception as exc:
            logger.error("Failed to fetch PandaDoc docs for workspace=%s: %s", workspace_id, exc)
            continue

        for doc in documents:
            if not _is_contract(doc):
                total_non_contract += 1
                continue

            doc_status = doc.get("status", "")
            # Normalize status prefix
            normalized = doc_status.removeprefix("document.").strip()
            contract_states = {"sent", "viewed", "completed", "rejected", "expired", "voided"}
            if normalized not in contract_states:
                logger.debug("Skipping contract %s with state=%s", doc.get("id"), doc_status)
                continue

            payload: dict[str, Any] = {
                "event_id": doc.get("id", ""),
                "action": "document_state_changed",
                "workspace_id": workspace_id,
                "data": {
                    **doc,
                    "status": normalized,
                },
            }

            try:
                envelope = await adapter.build_envelope(payload, scope=scope, thread=None)
            except Exception as exc:
                logger.warning("Envelope build failed for contract %s: %s", doc.get("id"), exc)
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
                logger.info("[DRY-RUN] Would write: %s", envelope.idempotency_key)
                total_written += 1
                continue

            try:
                await mem_svc.write(envelope, scope=scope, embed=False)
                total_written += 1
            except Exception as exc:
                logger.error("Write failed for contract %s: %s", doc.get("id"), exc)

    logger.info(
        "backfill_pandadoc_contracts complete: written=%d skipped=%d non_contract=%d dry_run=%s",
        total_written, total_skipped, total_non_contract, dry_run,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill PandaDoc contracts into memory_objects")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--tenant-id", help="Single tenant UUID")
    group.add_argument("--all-tenants", action="store_true", help="Backfill all tenants")
    p.add_argument("--since", required=True, help="ISO date (YYYY-MM-DD) — process docs modified since this date")
    p.add_argument("--dry-run", action="store_true", help="Print plan without writing")
    return p.parse_args()


async def _main() -> int:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        since_dt = datetime.fromisoformat(args.since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        logger.error("Invalid --since: %s (expected YYYY-MM-DD)", args.since)
        return 1

    tenant_ids: list[UUID] | None = [UUID(args.tenant_id)] if args.tenant_id else None
    if args.dry_run:
        logger.info("[DRY-RUN] Mode active — no writes will occur")

    await _run_backfill(tenant_ids=tenant_ids, since=since_dt, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
