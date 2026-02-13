"""Receipt Write Node — Immutable receipt chain entry (Law #2).

Responsibilities:
1. Collect all receipts generated during the pipeline
2. Redact PII via Presidio DLP before hashing (Law #9, Gate 5)
3. Assign chain_id (suite_id) and sequence numbers
4. Compute receipt hashes for the chain (SHA-256)
5. Persist receipts to the database (append-only)
6. If receipt write fails: fail closed, degrade to draft-only mode

Per receipt_emission_rules.md:
  "If receipts cannot be written: fail closed and degrade to draft-only"

Per receipt_chain_spec.md:
  - chain_id = suite_id (one chain per suite)
  - sequence = monotonically increasing integer within a chain
  - receipt_hash = sha256_hex(prev_hash + "\\n" + canonical_receipt)
  - genesis_prev_hash = "0" * 64
  - Insert must be atomic (transaction lock for prev_hash lookup)
  - Canonical JSON: UTF-8, keys sorted, no whitespace, exclude derived fields

Uses the shared assign_chain_metadata from receipt_chain service
to ensure consistent canonical form across hashing and verification.
DLP redaction runs BEFORE chain hashing so the hash covers the redacted form.
"""

from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.services.receipt_chain import assign_chain_metadata
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)


def receipt_write_node(state: OrchestratorState) -> dict[str, Any]:
    """Compute receipt hashes and prepare for persistence.

    Collects all pipeline receipts, assigns chain metadata via the
    shared assign_chain_metadata function, and persists to store.

    Fail-closed: if any receipt hash computation fails, the node
    sets error_code to RECEIPT_WRITE_FAILED and returns empty receipt_ids.
    """
    pipeline_receipts = list(state.get("pipeline_receipts", []))

    if not pipeline_receipts:
        return {"receipt_ids": []}

    suite_id = state.get("suite_id", "unknown")

    try:
        # DLP Pass: Redact PII before chain hashing (Law #9)
        # Redaction runs BEFORE assign_chain_metadata so the hash covers
        # the redacted form — no PII in the canonical receipt content.
        from aspire_orchestrator.services.dlp import get_dlp_service

        dlp = get_dlp_service()
        if dlp.available:
            redact_fields = state.get("redact_fields", [])
            pipeline_receipts = dlp.redact_receipts(
                pipeline_receipts, redact_fields=redact_fields,
            )

        # Use shared chain metadata assignment for consistent hashing
        assign_chain_metadata(pipeline_receipts, chain_id=suite_id)

        receipt_ids = [r["id"] for r in pipeline_receipts if "id" in r]

        # Phase 1: Persist to in-memory receipt store
        # Phase 2+: Moves to Supabase (atomic INSERT under transaction lock)
        from aspire_orchestrator.services.receipt_store import store_receipts
        store_receipts(pipeline_receipts)

        final_hash = pipeline_receipts[-1].get("receipt_hash", "")[:16]
        logger.info(
            "Receipt chain computed: chain_id=%s, count=%d, final_hash=%s",
            suite_id, len(receipt_ids), final_hash,
        )

        return {
            "receipt_ids": receipt_ids,
            "pipeline_receipts": pipeline_receipts,
        }

    except Exception as e:
        # Fail closed — Law #2 + receipt_emission_rules.md
        logger.error("Receipt write failed: %s", e)
        return {
            "receipt_ids": [],
            "error_code": "RECEIPT_WRITE_FAILED",
            "error_message": f"Receipt chain computation failed: {e}",
        }
