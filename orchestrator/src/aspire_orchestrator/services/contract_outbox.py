"""Contract Outbox — Persist signed contracts to Supabase.

When a contract reaches the SIGNED state (via webhook or manual transition),
the outbox persists the contract metadata to the contracts table in Supabase.

Responsibilities:
  - Write signed contract record to Supabase (suite_id scoped)
  - Idempotent writes: dedup by (document_id, suite_id)
  - Emit receipt for persistence (Law #2)
  - Queue failed writes for retry (graceful degradation)
  - In-memory fallback for tests (auto-detected)

Law compliance:
  - Law #2: Receipt for every outbox operation
  - Law #3: Fail closed on missing required fields
  - Law #6: Suite-scoped — all writes include suite_id
  - Law #7: Outbox is hands (executes write), not brains (doesn't decide)
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

RECEIPT_VERSION = "1.0"
ACTOR_OUTBOX = "service:contract-outbox"


@dataclass
class ContractRecord:
    """A signed contract record for persistence."""

    document_id: str
    template_key: str
    template_lane: str
    suite_id: str
    office_id: str
    correlation_id: str
    parties: list[dict[str, Any]]
    title: str = ""
    signed_at: str = ""
    pandadoc_status: str = "document.completed"
    contract_state: str = "draft"
    risk_tier: str = "yellow"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutboxResult:
    """Result of an outbox operation."""

    success: bool
    record_id: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    queued_for_retry: bool = False


def _build_outbox_receipt(
    *,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    event_type: str,
    outcome: str,
    reason_code: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build receipt for an outbox operation (Law #2)."""
    return {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": f"rcpt-outbox-{uuid.uuid4().hex[:12]}",
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor": ACTOR_OUTBOX,
        "correlation_id": correlation_id,
        "status": "ok" if outcome == "success" else outcome,
        "inputs_hash": hashlib.sha256(
            json.dumps(metadata or {}, sort_keys=True, default=str).encode()
        ).hexdigest(),
        "policy": {
            "decision": "allow" if outcome == "success" else "deny",
            "policy_id": "contract-outbox-v1",
            "reasons": [] if outcome == "success" else [reason_code],
        },
        "metadata": metadata or {},
        "redactions": [],
    }


def _record_to_row(record: ContractRecord) -> dict[str, Any]:
    """Convert ContractRecord to Supabase contracts table row."""
    return {
        "document_id": record.document_id,
        "template_key": record.template_key,
        "template_lane": record.template_lane,
        "suite_id": record.suite_id,
        "office_id": record.office_id,
        "correlation_id": record.correlation_id,
        "parties": json.dumps(record.parties),
        "title": record.title,
        "signed_at": record.signed_at or datetime.now(timezone.utc).isoformat(),
        "pandadoc_status": record.pandadoc_status,
        "contract_state": record.contract_state,
        "risk_tier": record.risk_tier,
        "metadata": json.dumps(record.metadata),
    }


class ContractOutbox:
    """Persist signed contracts with idempotency and retry queue.

    Uses Supabase contracts table in production.
    Falls back to in-memory store when use_supabase=False (tests).
    """

    def __init__(self, *, use_supabase: bool = False) -> None:
        self._use_supabase = use_supabase
        self._contracts: dict[str, ContractRecord] = {}  # in-memory + cache
        self._retry_queue: list[ContractRecord] = []

    async def persist_async(self, record: ContractRecord) -> OutboxResult:
        """Persist a signed contract record (async — calls Supabase if enabled).

        Idempotent: if document_id already exists for this suite, skip with success.
        """
        # Fail closed: validate required fields (Law #3)
        validation_result = self._validate(record)
        if validation_result is not None:
            return validation_result

        dedup_key = f"{record.suite_id}:{record.document_id}"

        # Idempotency: check in-memory cache first
        if dedup_key in self._contracts:
            receipt = _build_outbox_receipt(
                suite_id=record.suite_id,
                office_id=record.office_id,
                correlation_id=record.correlation_id,
                event_type="contract.outbox.persist",
                outcome="success",
                reason_code="IDEMPOTENT_SKIP",
                metadata={"document_id": record.document_id, "already_persisted": True},
            )
            return OutboxResult(success=True, record_id=dedup_key, receipt=receipt)

        if not record.signed_at:
            record.signed_at = datetime.now(timezone.utc).isoformat()

        # Persist to Supabase if enabled
        if self._use_supabase:
            try:
                from aspire_orchestrator.services.supabase_client import (
                    SupabaseClientError,
                    supabase_insert,
                    supabase_select,
                )

                # Check Supabase for existing record (idempotency across restarts)
                existing = await supabase_select(
                    "contracts",
                    f"document_id=eq.{record.document_id}&suite_id=eq.{record.suite_id}",
                )
                if existing:
                    self._contracts[dedup_key] = record  # cache locally
                    receipt = _build_outbox_receipt(
                        suite_id=record.suite_id,
                        office_id=record.office_id,
                        correlation_id=record.correlation_id,
                        event_type="contract.outbox.persist",
                        outcome="success",
                        reason_code="IDEMPOTENT_SKIP",
                        metadata={"document_id": record.document_id, "already_persisted": True},
                    )
                    return OutboxResult(success=True, record_id=dedup_key, receipt=receipt)

                # INSERT into Supabase contracts table
                row = _record_to_row(record)
                await supabase_insert("contracts", row)

            except SupabaseClientError as e:
                logger.error("Supabase write failed for doc=%s: %s", record.document_id[:8], e)
                # Graceful degradation: queue for retry, keep in-memory
                self._retry_queue.append(record)
                self._contracts[dedup_key] = record
                receipt = _build_outbox_receipt(
                    suite_id=record.suite_id,
                    office_id=record.office_id,
                    correlation_id=record.correlation_id,
                    event_type="contract.outbox.persist",
                    outcome="success",
                    reason_code="PERSISTED_WITH_RETRY_QUEUED",
                    metadata={
                        "document_id": record.document_id,
                        "supabase_error": str(e),
                        "queued_for_retry": True,
                    },
                )
                return OutboxResult(
                    success=True, record_id=dedup_key, receipt=receipt,
                    queued_for_retry=True,
                )

        # Store in-memory (always, for cache / non-Supabase mode)
        self._contracts[dedup_key] = record

        receipt = _build_outbox_receipt(
            suite_id=record.suite_id,
            office_id=record.office_id,
            correlation_id=record.correlation_id,
            event_type="contract.outbox.persist",
            outcome="success",
            reason_code="PERSISTED",
            metadata={
                "document_id": record.document_id,
                "template_key": record.template_key,
                "template_lane": record.template_lane,
                "parties_count": len(record.parties),
                "risk_tier": record.risk_tier,
                "supabase": self._use_supabase,
            },
        )

        logger.info(
            "Contract persisted: doc=%s, template=%s, suite=%s, supabase=%s",
            record.document_id[:8], record.template_key,
            record.suite_id[:8] if len(record.suite_id) > 8 else record.suite_id,
            self._use_supabase,
        )

        return OutboxResult(success=True, record_id=dedup_key, receipt=receipt)

    def persist(self, record: ContractRecord) -> OutboxResult:
        """Synchronous persist (in-memory only — for tests and sync callers)."""
        validation_result = self._validate(record)
        if validation_result is not None:
            return validation_result

        dedup_key = f"{record.suite_id}:{record.document_id}"

        if dedup_key in self._contracts:
            receipt = _build_outbox_receipt(
                suite_id=record.suite_id,
                office_id=record.office_id,
                correlation_id=record.correlation_id,
                event_type="contract.outbox.persist",
                outcome="success",
                reason_code="IDEMPOTENT_SKIP",
                metadata={"document_id": record.document_id, "already_persisted": True},
            )
            return OutboxResult(success=True, record_id=dedup_key, receipt=receipt)

        if not record.signed_at:
            record.signed_at = datetime.now(timezone.utc).isoformat()

        self._contracts[dedup_key] = record

        receipt = _build_outbox_receipt(
            suite_id=record.suite_id,
            office_id=record.office_id,
            correlation_id=record.correlation_id,
            event_type="contract.outbox.persist",
            outcome="success",
            reason_code="PERSISTED",
            metadata={
                "document_id": record.document_id,
                "template_key": record.template_key,
                "template_lane": record.template_lane,
                "parties_count": len(record.parties),
                "risk_tier": record.risk_tier,
            },
        )

        logger.info(
            "Contract persisted: doc=%s, template=%s, suite=%s",
            record.document_id[:8], record.template_key,
            record.suite_id[:8] if len(record.suite_id) > 8 else record.suite_id,
        )

        return OutboxResult(success=True, record_id=dedup_key, receipt=receipt)

    def _validate(self, record: ContractRecord) -> OutboxResult | None:
        """Validate required fields. Returns OutboxResult on failure, None on success."""
        if not record.document_id:
            receipt = _build_outbox_receipt(
                suite_id=record.suite_id, office_id=record.office_id,
                correlation_id=record.correlation_id,
                event_type="contract.outbox.persist",
                outcome="denied", reason_code="MISSING_DOCUMENT_ID",
            )
            return OutboxResult(success=False, receipt=receipt, error="Missing document_id")

        if not record.suite_id:
            receipt = _build_outbox_receipt(
                suite_id="", office_id=record.office_id,
                correlation_id=record.correlation_id,
                event_type="contract.outbox.persist",
                outcome="denied", reason_code="MISSING_SUITE_ID",
            )
            return OutboxResult(success=False, receipt=receipt, error="Missing suite_id (Law #6)")

        return None

    async def update_state(
        self,
        document_id: str,
        suite_id: str,
        *,
        contract_state: str,
        pandadoc_status: str,
        correlation_id: str,
    ) -> OutboxResult:
        """Update contract state after webhook event (e.g. sent → signed).

        Idempotent: if contract doesn't exist yet, creates a minimal record.
        If Supabase is enabled, persists the update there.
        """
        if not document_id or not suite_id:
            receipt = _build_outbox_receipt(
                suite_id=suite_id, office_id="",
                correlation_id=correlation_id,
                event_type="contract.outbox.update_state",
                outcome="denied", reason_code="MISSING_REQUIRED_FIELDS",
            )
            return OutboxResult(success=False, receipt=receipt, error="Missing document_id or suite_id")

        dedup_key = f"{suite_id}:{document_id}"

        # Update in-memory cache
        existing = self._contracts.get(dedup_key)
        if existing:
            # Dataclass is not frozen, so we can update fields
            existing.contract_state = contract_state
            existing.pandadoc_status = pandadoc_status
        else:
            # Create minimal record if webhook arrives before outbox.persist
            self._contracts[dedup_key] = ContractRecord(
                document_id=document_id,
                template_key="",
                template_lane="",
                suite_id=suite_id,
                office_id="",
                correlation_id=correlation_id,
                parties=[],
                contract_state=contract_state,
                pandadoc_status=pandadoc_status,
            )

        # Persist to Supabase if enabled
        if self._use_supabase:
            try:
                from aspire_orchestrator.services.supabase_client import (
                    SupabaseClientError,
                    supabase_update,
                )

                await supabase_update(
                    "contracts",
                    f"document_id=eq.{document_id}&suite_id=eq.{suite_id}",
                    {
                        "contract_state": contract_state,
                        "pandadoc_status": pandadoc_status,
                    },
                )
            except (SupabaseClientError, ImportError) as e:
                logger.warning(
                    "Supabase state update failed for doc=%s: %s (in-memory updated)",
                    document_id[:8], e,
                )

        receipt = _build_outbox_receipt(
            suite_id=suite_id,
            office_id=existing.office_id if existing else "",
            correlation_id=correlation_id,
            event_type="contract.outbox.update_state",
            outcome="success",
            reason_code="STATE_UPDATED",
            metadata={
                "document_id": document_id,
                "contract_state": contract_state,
                "pandadoc_status": pandadoc_status,
            },
        )

        logger.info(
            "Contract state updated: doc=%s, state=%s, supabase=%s",
            document_id[:8], contract_state, self._use_supabase,
        )

        return OutboxResult(success=True, record_id=dedup_key, receipt=receipt)

    def queue_for_retry(self, record: ContractRecord) -> OutboxResult:
        """Queue a failed write for retry (graceful degradation)."""
        self._retry_queue.append(record)

        receipt = _build_outbox_receipt(
            suite_id=record.suite_id,
            office_id=record.office_id,
            correlation_id=record.correlation_id,
            event_type="contract.outbox.retry_queued",
            outcome="success",
            reason_code="QUEUED_FOR_RETRY",
            metadata={
                "document_id": record.document_id,
                "queue_depth": len(self._retry_queue),
            },
        )

        return OutboxResult(success=True, receipt=receipt, queued_for_retry=True)

    async def flush_retry_queue(self) -> list[OutboxResult]:
        """Attempt to persist all queued records to Supabase."""
        if not self._use_supabase or not self._retry_queue:
            return []

        results: list[OutboxResult] = []
        remaining: list[ContractRecord] = []

        for record in self._retry_queue:
            result = await self.persist_async(record)
            results.append(result)
            if result.queued_for_retry:
                remaining.append(record)

        self._retry_queue = remaining
        return results

    def get_contract(self, document_id: str, suite_id: str) -> ContractRecord | None:
        """Retrieve a contract by document_id, scoped to suite (Law #6)."""
        dedup_key = f"{suite_id}:{document_id}"
        return self._contracts.get(dedup_key)

    def list_contracts(self, suite_id: str) -> list[ContractRecord]:
        """List all contracts for a suite (Law #6: tenant-scoped)."""
        return [c for c in self._contracts.values() if c.suite_id == suite_id]

    @property
    def retry_queue_depth(self) -> int:
        return len(self._retry_queue)

    def clear_store(self) -> None:
        """Clear all contracts and retry queue. Testing only."""
        self._contracts.clear()
        self._retry_queue.clear()


# Singleton
_contract_outbox: ContractOutbox | None = None


def get_contract_outbox(*, use_supabase: bool | None = None) -> ContractOutbox:
    """Get the singleton ContractOutbox instance.

    Args:
        use_supabase: If provided, controls Supabase usage. If None, auto-detects
                      from ASPIRE_SUPABASE_URL env var presence.
    """
    global _contract_outbox
    if _contract_outbox is None:
        import os
        auto_supabase = use_supabase if use_supabase is not None else bool(
            os.environ.get("ASPIRE_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
        )
        _contract_outbox = ContractOutbox(use_supabase=auto_supabase)
    return _contract_outbox
