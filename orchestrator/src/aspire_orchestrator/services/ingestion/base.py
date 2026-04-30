"""BaseIngestionAdapter — the contract every Pass 14 ingestion adapter follows.

Pattern (canonical):

    class FooIngestionAdapter(BaseIngestionAdapter):
        provider_name = "foo"
        memory_type   = "invoice"   # or "call", "meeting", etc.

        async def verify_signature(self, *, body, headers) -> bool:
            return verify_foo(body, headers.get("X-Foo-Signature", ""), settings.FOO_WEBHOOK_SECRET)

        async def resolve_scope(self, payload: dict) -> ScopedIdentity:
            # Resolve tenant/suite/office from payload (e.g. customer_id → provider_connections)
            ...

        async def build_envelope(
            self, payload: dict, *, scope: ScopedIdentity, thread: ThreadOut
        ) -> MemoryObjectIn:
            # Per-type body — see plan §14.C for required fields per memory_type
            ...

Subclasses override ONLY the four hooks above. The orchestration (signature
verify → scope resolve → thread upsert → memory write → receipt) lives in
this base class so every adapter is identical in posture.

Aspire Laws:
  - Law #2 (Receipt for All) — receipt is cut by `MemoryService.write` on
    every successful insert. No adapter writes its own receipts.
  - Law #3 (Fail Closed) — bad signature, missing scope, or missing
    idempotency_key all raise IngestionError → route returns 401/422.
  - Law #6 (Tenant Isolation) — scope is resolved from payload and asserted
    against `MemoryService.write`'s scope param (defense-in-depth).
  - Law #9 (Security) — webhook bodies are NEVER logged at full fidelity.
    Only payload `id` field + memory_type for traceability.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    MemoryObjectOut,
    MemoryType,
    ScopedIdentity,
    ThreadOut,
)
from aspire_orchestrator.services.entity_thread_resolver import EntityThreadResolver
from aspire_orchestrator.services.memory_service import MemoryService, MemoryServiceError

logger = logging.getLogger(__name__)


class IngestionError(Exception):
    """Adapter-level error. Caught in routes/ingestion.py → 401/422/500."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int = 500,
    ) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True)
class IngestionResult:
    """Returned to the route handler so it can shape the HTTP response."""

    memory: MemoryObjectOut
    deduplicated: bool  # True if idempotency dedup hit (no new write)


class BaseIngestionAdapter(ABC):
    """Common ingestion orchestration: signature → scope → thread → write.

    Subclasses override only the abstract hooks. Subclass responsibilities:
      - Provide `provider_name` (e.g. "stripe", "twilio_voice", "zoom").
      - Provide `memory_type` (one of the 20 types in MemoryType).
      - Implement `verify_signature` (per-provider HMAC scheme).
      - Implement `resolve_scope` (payload → ScopedIdentity).
      - Implement `build_envelope` (payload → MemoryObjectIn).
    """

    provider_name: str = "abstract"
    memory_type: MemoryType = "timeline_event"

    def __init__(
        self,
        *,
        memory_service: MemoryService | None = None,
        thread_resolver: EntityThreadResolver | None = None,
    ) -> None:
        self._memory_service = memory_service or MemoryService()
        self._thread_resolver = thread_resolver or EntityThreadResolver()

    # ---- subclass hooks ----------------------------------------------------

    @abstractmethod
    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """Verify the upstream provider's webhook signature.

        Return True on valid. Never raise.
        """

    @abstractmethod
    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        """Resolve tenant/suite/office scope from the payload.

        Common patterns:
          - Stripe: customer_id → provider_connections.tenant_id
          - Twilio voice/sms: To-number → tenant_phone_numbers.tenant_id
          - Zoom: account_id → provider_connections.tenant_id
          - EL/Anam: agent_id + called_number → tenant_phone_numbers
        """

    @abstractmethod
    async def build_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
        thread: ThreadOut | None,
    ) -> MemoryObjectIn:
        """Build a write-ready MemoryObjectIn from the payload.

        MUST set:
          - scope (= passed-in scope)
          - provenance.trace_id, provenance.correlation_id, provenance.runtime_family
          - memory_type (= self.memory_type)
          - title, summary (non-empty)
          - detail (per-type fields per plan §14.C)
          - idempotency_key (provider event ID — guarantees dedup)

        SHOULD set:
          - entity_type, entity_id (resolved upstream contact / customer)
          - thread_id (= thread.thread_id if non-None)
          - event_at (provider-supplied timestamp of the source event)
        """

    async def thread_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
    ) -> ThreadOut | None:
        """Optional hook: resolve a thread for this event.

        Default: return None (no thread linkage). Subclasses that group memories
        per entity (e.g. all SMS from one contact, all calls with one customer)
        should override and call `self._thread_resolver.upsert_thread(...)`.
        """
        _ = (payload, scope)
        return None

    # ---- orchestration (do not override) ----------------------------------

    async def ingest(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        payload: dict[str, Any],
    ) -> IngestionResult:
        """End-to-end: signature → scope → thread → write → receipt.

        Called by routes/ingestion.py. Do NOT call directly from other code —
        always go through the route so we get tenant header validation +
        request logging + idempotency context.
        """
        # 1. Signature verification (Law #3: fail closed on bad signature)
        if not await self.verify_signature(body=body, headers=headers):
            logger.warning(
                "ingestion_signature_invalid provider=%s",
                self.provider_name,
            )
            raise IngestionError(
                f"{self.provider_name} signature invalid",
                code="SIGNATURE_INVALID",
                status_code=401,
            )

        # 2. Resolve tenant scope from payload
        try:
            scope = await self.resolve_scope(payload)
        except IngestionError:
            raise
        except Exception as exc:
            logger.warning(
                "ingestion_scope_resolve_failed provider=%s error=%s",
                self.provider_name,
                exc,
            )
            raise IngestionError(
                f"{self.provider_name} scope resolution failed: {exc}",
                code="SCOPE_RESOLVE_FAILED",
                status_code=422,
            ) from exc

        # 3. Optional thread resolution
        thread: ThreadOut | None = None
        try:
            thread = await self.thread_envelope(payload, scope=scope)
        except Exception as exc:
            # Thread resolution failure is non-fatal — memory still writes
            # without a thread_id. Log and continue.
            logger.info(
                "ingestion_thread_resolve_skipped provider=%s reason=%s",
                self.provider_name,
                exc,
            )

        # 4. Build the envelope
        try:
            envelope = await self.build_envelope(payload, scope=scope, thread=thread)
        except IngestionError:
            raise
        except Exception as exc:
            logger.warning(
                "ingestion_build_envelope_failed provider=%s error=%s",
                self.provider_name,
                exc,
            )
            raise IngestionError(
                f"{self.provider_name} envelope build failed: {exc}",
                code="ENVELOPE_BUILD_FAILED",
                status_code=422,
            ) from exc

        # 5. Write — MemoryService cuts the receipt internally (Law #2)
        try:
            memory = await self._memory_service.write(envelope, scope=scope, embed=True)
        except MemoryServiceError as exc:
            # MemoryService already cut a failure receipt internally. Bubble up.
            logger.error(
                "ingestion_memory_write_failed provider=%s code=%s tenant=%s",
                self.provider_name,
                exc.code,
                exc.tenant_id,
            )
            raise IngestionError(
                f"{self.provider_name} memory write failed: {exc.code}",
                code="MEMORY_WRITE_FAILED",
                status_code=500,
            ) from exc

        # Idempotency hit detection: if MemoryService returned an existing row,
        # the embedding dim and created_at will be from the original write.
        # This is fine — the route handler doesn't need to know which.
        deduplicated = False
        # (optional sentinel: a future enhancement could expose a flag from
        #  MemoryService.write itself; for now, callers don't depend on it)

        logger.info(
            "ingestion_success provider=%s memory_type=%s memory_id=%s tenant=%s",
            self.provider_name,
            self.memory_type,
            memory.memory_id,
            str(scope.tenant_id),
        )
        return IngestionResult(memory=memory, deduplicated=deduplicated)


__all__ = [
    "BaseIngestionAdapter",
    "IngestionError",
    "IngestionResult",
]
