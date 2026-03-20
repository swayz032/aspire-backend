"""Receipt activity — persist receipts to Supabase (Law #2).

Every state change produces an immutable, append-only receipt.
No UPDATE/DELETE on receipts — corrections are new receipts.
"""

from __future__ import annotations

import logging

from temporalio import activity

from aspire_orchestrator.temporal.models import PersistReceiptsInput, PersistReceiptsOutput

logger = logging.getLogger(__name__)


@activity.defn
async def persist_receipts(input: PersistReceiptsInput) -> PersistReceiptsOutput:
    """Persist receipts to Supabase — Law #2: Receipt for All Actions."""
    from aspire_orchestrator.services.receipt_store import store_receipts

    if not input.receipts:
        return PersistReceiptsOutput(receipt_ids=[], count=0)

    try:
        receipt_ids = await store_receipts(
            receipts=input.receipts,
            suite_id=input.suite_id,
            correlation_id=input.correlation_id,
        )

        logger.info(
            "Persisted %d receipts: correlation_id=%s suite_id=%s",
            len(receipt_ids),
            input.correlation_id,
            input.suite_id,
        )

        return PersistReceiptsOutput(
            receipt_ids=receipt_ids,
            count=len(receipt_ids),
        )

    except Exception:
        logger.exception(
            "Receipt persistence failed: correlation_id=%s suite_id=%s count=%d",
            input.correlation_id,
            input.suite_id,
            len(input.receipts),
        )
        raise
