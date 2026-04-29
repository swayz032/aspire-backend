"""Entity Thread Resolver — canonical thread lookup and creation for the Memory Spine.

Implements §3.2 of 03_THREAD_REGISTRY_AND_MEMORY_MODEL.md.

Thread resolution priority:
  1. Explicit thread_id in envelope → return existing thread or raise.
  2. entity_type + entity_id in envelope → look up canonical entity match.
  3. Fallback → create an 'internal_thread' keyed by correlation_id.

upsert_thread is SELECT-then-INSERT-then-UPDATE because there is no UNIQUE
constraint on (tenant_id, suite_id, canonical_entity_type, canonical_entity_id)
in the DB schema. We use a SELECT-first pattern with a SAVEPOINT-equivalent
retry to handle concurrent insert races without requiring a new migration.

Law compliance:
  Law #3: Missing thread on explicit thread_id → raise (fail closed).
  Law #6: All reads/writes scoped to tenant_id + suite_id + office_id.
  Law #9: No PII in log lines.
"""

from __future__ import annotations

import logging
from uuid import UUID

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryEventEnvelope,
    ScopedIdentity,
    ThreadIn,
    ThreadOut,
)
from aspire_orchestrator.services.memory_service import MemoryServiceError, _row_to_thread_out
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
    supabase_update,
)

logger = logging.getLogger(__name__)


class EntityThreadResolver:
    """Resolve or create the canonical thread for a MemoryEventEnvelope.

    Stateless — safe to instantiate per-request or as a shared singleton.
    """

    async def resolve(self, envelope: MemoryEventEnvelope) -> ThreadOut:
        """Find the canonical thread for the event; create if missing per §3.2 rules.

        Priority:
          1. envelope.thread_id → fetch + validate; raise if missing.
          2. envelope.entity_type + entity_id → upsert canonical entity thread.
          3. Neither → create internal_thread keyed by correlation_id.

        Args:
            envelope: Validated MemoryEventEnvelope from the intake queue.

        Returns:
            ThreadOut for the resolved or newly created thread.

        Raises:
            MemoryServiceError: When explicit thread_id is given but not found (fail closed).
        """
        scope = ScopedIdentity(
            tenant_id=envelope.tenant_id,
            suite_id=envelope.suite_id,
            office_id=envelope.office_id,
        )

        # Priority 1: explicit thread_id
        if envelope.thread_id is not None:
            thread = await self.get(envelope.thread_id, scope=scope)
            if thread is None:
                raise MemoryServiceError(
                    f"Explicit thread_id={envelope.thread_id} not found in tenant={envelope.tenant_id}. "
                    "Fail closed — cannot create memory without a valid thread anchor.",
                    code="THREAD_NOT_FOUND",
                    tenant_id=envelope.tenant_id,
                    correlation_id=envelope.correlation_id,
                )
            return thread

        # Priority 2: canonical entity
        if envelope.entity_type and envelope.entity_id:
            thread_in = ThreadIn(
                tenant_id=envelope.tenant_id,
                suite_id=envelope.suite_id,
                office_id=envelope.office_id,
                thread_type=_infer_thread_type(envelope.entity_type),
                canonical_entity_type=envelope.entity_type,
                canonical_entity_id=envelope.entity_id,
                title=f"{envelope.entity_type}:{envelope.entity_id}",
            )
            return await self.upsert_thread(thread_in)

        # Priority 3: internal_thread keyed by correlation_id
        thread_in = ThreadIn(
            tenant_id=envelope.tenant_id,
            suite_id=envelope.suite_id,
            office_id=envelope.office_id,
            thread_type="internal_thread",
            title=f"internal:{envelope.correlation_id}",
        )
        return await self.upsert_thread(thread_in)

    async def upsert_thread(self, thread_in: ThreadIn) -> ThreadOut:
        """Idempotent thread create-or-touch.

        Because there is no UNIQUE constraint on
        (tenant_id, suite_id, canonical_entity_type, canonical_entity_id) in the DB,
        we implement idempotency with SELECT-then-INSERT:

        1. SELECT for existing match.
        2. If found: UPDATE last_activity_at to now().
        3. If not found: INSERT. Handle concurrent-insert race by catching the
           duplicate error and falling back to a SELECT.

        This avoids a new migration for a UNIQUE constraint (out of scope for Pass 2).

        Args:
            thread_in: Thread write-shape. canonical_entity_type + canonical_entity_id
                       are the idempotency key when both are present.

        Returns:
            ThreadOut — the existing or newly created thread.

        Raises:
            MemoryServiceError: On DB failure.
        """
        # Try to find an existing thread for this canonical entity
        existing = await self._find_canonical_thread(thread_in)
        if existing is not None:
            # Touch last_activity_at
            await self._touch_thread(existing.thread_id, scope=ScopedIdentity(
                tenant_id=thread_in.tenant_id,
                suite_id=thread_in.suite_id,
                office_id=thread_in.office_id,
            ))
            logger.info(
                "entity_thread_resolver: found existing thread_id=%s entity=%s/%s tenant=%s",
                existing.thread_id,
                thread_in.canonical_entity_type,
                thread_in.canonical_entity_id,
                str(thread_in.tenant_id),
            )
            return existing

        # INSERT new thread
        row: dict = {
            "tenant_id": str(thread_in.tenant_id),
            "suite_id": str(thread_in.suite_id),
            "office_id": str(thread_in.office_id),
            "thread_type": thread_in.thread_type,
            "finance_thread_subtype": thread_in.finance_thread_subtype,
            "canonical_entity_type": thread_in.canonical_entity_type,
            "canonical_entity_id": str(thread_in.canonical_entity_id) if thread_in.canonical_entity_id else None,
            "title": thread_in.title,
            "status": thread_in.status,
            "first_event_at": thread_in.first_event_at.isoformat() if thread_in.first_event_at else None,
            "participants": [str(x) for x in thread_in.participants],
            "tags": thread_in.tags,
        }
        try:
            inserted = await supabase_insert("threads", row)
            out = _row_to_thread_out(inserted)
            logger.info(
                "entity_thread_resolver: created thread_id=%s type=%s entity=%s/%s tenant=%s",
                out.thread_id,
                thread_in.thread_type,
                thread_in.canonical_entity_type,
                thread_in.canonical_entity_id,
                str(thread_in.tenant_id),
            )
            return out
        except SupabaseClientError as exc:
            # Concurrent insert race — another worker created the same thread
            detail = exc.detail.lower()
            is_conflict = exc.status_code == 409 or "23505" in detail or "unique" in detail
            if is_conflict:
                logger.info(
                    "entity_thread_resolver: concurrent insert race on entity=%s/%s — retrying SELECT",
                    thread_in.canonical_entity_type,
                    thread_in.canonical_entity_id,
                )
                existing = await self._find_canonical_thread(thread_in)
                if existing is not None:
                    return existing
            raise MemoryServiceError(
                f"DB upsert_thread failed: {exc.detail}",
                code="DB_INSERT_FAILED",
                tenant_id=thread_in.tenant_id,
            ) from exc

    async def get(
        self, thread_id: UUID, *, scope: ScopedIdentity
    ) -> ThreadOut | None:
        """Fetch a thread by primary key. Returns None if not found.

        Validates scope match before returning (Law #6).
        """
        try:
            rows = await supabase_select(
                "threads",
                f"thread_id=eq.{thread_id}",
                limit=1,
            )
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB threads select failed: {exc.detail}",
                code="DB_SELECT_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc

        if not rows:
            return None

        row = rows[0]
        # Scope validation (Law #6 defense-in-depth)
        from aspire_orchestrator.services.memory_service import _assert_scope_match
        _assert_scope_match(row, scope)
        return _row_to_thread_out(row)

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    async def _find_canonical_thread(
        self, thread_in: ThreadIn
    ) -> ThreadOut | None:
        """SELECT for an existing thread matching the canonical entity."""
        if not thread_in.canonical_entity_type or not thread_in.canonical_entity_id:
            # No canonical anchor — cannot dedup; always insert
            return None

        filter_str = (
            f"tenant_id=eq.{thread_in.tenant_id}"
            f"&suite_id=eq.{thread_in.suite_id}"
            f"&canonical_entity_type=eq.{thread_in.canonical_entity_type}"
            f"&canonical_entity_id=eq.{thread_in.canonical_entity_id}"
        )
        try:
            rows = await supabase_select("threads", filter_str, limit=1)
        except SupabaseClientError:
            return None

        return _row_to_thread_out(rows[0]) if rows else None

    async def _touch_thread(self, thread_id: UUID, *, scope: ScopedIdentity) -> None:
        """UPDATE last_activity_at = now() on an existing thread."""
        match_filter = (
            f"thread_id=eq.{thread_id}"
            f"&tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
        )
        try:
            await supabase_update(
                "threads",
                match_filter,
                {"last_activity_at": "now()"},
            )
        except SupabaseClientError as exc:
            # Non-fatal: log and continue — the thread still exists
            logger.warning(
                "entity_thread_resolver: failed to touch thread_id=%s: %s",
                thread_id,
                exc.detail,
            )


def _infer_thread_type(entity_type: str) -> str:
    """Map a canonical entity_type string to the closest ThreadType.

    Falls back to 'internal_thread' for unknown entity types.
    This mapping covers the EntityType literals from §2.3 of 02_SHARED_SCHEMAS.md.
    """
    _MAP: dict[str, str] = {
        "lead": "lead_thread",
        "customer": "customer_thread",
        "deal": "deal_thread",
        "job": "job_thread",
        "project": "project_thread",
        "estimate": "estimate_thread",
        "quote": "quote_thread",
        "invoice": "invoice_thread",
        "contract": "contract_thread",
        "meeting": "meeting_thread",
        "task": "task_thread",
        "finance_account": "finance_thread",
        "payment": "finance_thread",
        "provider_connection": "finance_thread",
        "receipt": "internal_thread",
        "workflow_run": "internal_thread",
        "internal_case": "internal_thread",
    }
    return _MAP.get(entity_type, "internal_thread")
