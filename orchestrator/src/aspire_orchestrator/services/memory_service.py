"""Memory Service — single write path for all Office Memory Engine objects.

Every write to memory_objects goes through this class. No code may INSERT into
memory_objects directly.

Law compliance:
  Law #2: Every state-changing method emits an immutable receipt via receipt_store.
  Law #3: Missing scope / missing credentials → raise MemoryServiceError (fail closed).
  Law #6: Tenant isolation pre-checked before every read/write (DB RLS is the backstop,
          this is the defense-in-depth layer above it).
  Law #9: PII never appears in log lines; idempotency_key dedup on every write.

Async pattern mirrors base_retrieval_service.py / supabase_client.py:
  - Uses supabase_client module-level async helpers (httpx under the hood).
  - Never blocks the event loop with sync I/O.
  - All DB calls have explicit <5 s timeout (inherited from supabase_client._TIMEOUT).

Embedding:
  - text-embedding-3-large, 1536 dims (settings.embedding_dimensions).
  - Computed via EmbeddingCache.get_or_embed + legal_embedding_service.embed_text.
  - Pass embed=False to supply a pre-computed vector or skip embedding.

Idempotency:
  - On UNIQUE constraint violation (tenant_id, suite_id, idempotency_key), the
    existing row is fetched and returned without re-emitting a receipt.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    MemoryObjectOut,
    MemoryStatus,
    MemoryType,
    Provenance,
    ScopedIdentity,
    ThreadIn,
    ThreadOut,
)
from aspire_orchestrator.services.receipt_store import store_receipts
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
    supabase_update,
)

logger = logging.getLogger(__name__)

# Statuses that cannot be the source of a transition (immutability guard).
# The DB trigger blocks updates on 'executed' objects; we pre-check for a
# cleaner error message before hitting the DB round-trip.
_TERMINAL_STATUS = "executed"


class MemoryServiceError(Exception):
    """Structured error raised by MemoryService. Never swallowed internally.

    Callers (orchestrator nodes, API handlers) catch this and emit failure receipts.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        tenant_id: UUID | str | None = None,
        correlation_id: UUID | str | None = None,
    ) -> None:
        self.code = code
        self.tenant_id = str(tenant_id) if tenant_id else None
        self.correlation_id = str(correlation_id) if correlation_id else None
        # Prefix code into the message so regex-style matchers (pytest.raises match=...) see it,
        # and observability/log lines always carry the structured code.
        super().__init__(f"[{code}] {message}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _scope_dict(scope: ScopedIdentity) -> dict[str, str]:
    """Flatten scope to a dict of string UUIDs for DB row payloads."""
    return {
        "tenant_id": str(scope.tenant_id),
        "suite_id": str(scope.suite_id),
        "office_id": str(scope.office_id),
    }


def _provenance_dict(prov: Provenance) -> dict[str, Any]:
    """Flatten Provenance to column-level dict for DB row payload."""
    return {
        "source_surface": prov.source_surface,
        "source_agent": prov.source_agent,
        "runtime_family": prov.runtime_family,
        "channel": prov.channel,
        "session_provider": prov.session_provider,
        "transcript_provider": prov.transcript_provider,
        "recording_provider": prov.recording_provider,
        "external_session_id": prov.external_session_id,
        "source_record_id": prov.source_record_id,
        "trace_id": str(prov.trace_id),
        "correlation_id": str(prov.correlation_id),
        "artifact_origin": prov.artifact_origin,
        "summary_origin": prov.summary_origin,
    }


def _row_to_memory_out(row: dict[str, Any]) -> MemoryObjectOut:
    """Map a flat DB row dict to MemoryObjectOut.

    Reconstructs the nested ScopedIdentity and Provenance objects from the
    flattened column layout used in memory_objects.
    """
    scope = ScopedIdentity(
        tenant_id=UUID(row["tenant_id"]),
        suite_id=UUID(row["suite_id"]),
        office_id=UUID(row["office_id"]),
        actor_id=UUID(row["actor_id"]) if row.get("actor_id") else None,
        user_id=UUID(row["user_id"]) if row.get("user_id") else None,
    )
    provenance = Provenance(
        source_surface=row.get("source_surface"),
        source_agent=row.get("source_agent"),
        runtime_family=row.get("runtime_family"),
        channel=row.get("channel"),
        session_provider=row.get("session_provider"),
        transcript_provider=row.get("transcript_provider"),
        recording_provider=row.get("recording_provider"),
        external_session_id=row.get("external_session_id"),
        source_record_id=row.get("source_record_id"),
        trace_id=UUID(row["trace_id"]),
        correlation_id=UUID(row["correlation_id"]),
        artifact_origin=row.get("artifact_origin"),
        summary_origin=row.get("summary_origin"),
    )
    return MemoryObjectOut(
        memory_id=UUID(row["memory_id"]),
        scope=scope,
        provenance=provenance,
        memory_type=row["memory_type"],
        schema_version=row.get("schema_version", "v1"),
        entity_type=row.get("entity_type"),
        entity_id=UUID(row["entity_id"]) if row.get("entity_id") else None,
        thread_id=UUID(row["thread_id"]) if row.get("thread_id") else None,
        title=row.get("title"),
        summary=row["summary"],
        detail=row.get("detail") or {},
        confidence=row.get("confidence"),
        visibility_scope=row.get("visibility_scope", "office"),
        status=row.get("status"),
        linked_receipt_ids=[UUID(x) for x in (row.get("linked_receipt_ids") or [])],
        linked_approval_ids=[UUID(x) for x in (row.get("linked_approval_ids") or [])],
        linked_artifact_ids=[UUID(x) for x in (row.get("linked_artifact_ids") or [])],
        linked_workflow_run_ids=[UUID(x) for x in (row.get("linked_workflow_run_ids") or [])],
        event_at=row.get("event_at"),
        created_at=row["created_at"],
        source_updated_at=row.get("source_updated_at"),
        promoted_at=row.get("promoted_at"),
        approved_at=row.get("approved_at"),
        executed_at=row.get("executed_at"),
        last_activity_at=row["last_activity_at"],
        summary_window_start_at=row.get("summary_window_start_at"),
        summary_window_end_at=row.get("summary_window_end_at"),
        fresh_until=row.get("fresh_until"),
        # Embedding is never returned by default (large payload); callers request explicitly
        embedding=row.get("embedding"),
        idempotency_key=row.get("idempotency_key"),
    )


def _row_to_thread_out(row: dict[str, Any]) -> ThreadOut:
    """Map a flat DB row dict to ThreadOut."""
    return ThreadOut(
        thread_id=UUID(row["thread_id"]),
        tenant_id=UUID(row["tenant_id"]),
        suite_id=UUID(row["suite_id"]),
        office_id=UUID(row["office_id"]),
        thread_type=row["thread_type"],
        finance_thread_subtype=row.get("finance_thread_subtype"),
        canonical_entity_type=row.get("canonical_entity_type"),
        canonical_entity_id=UUID(row["canonical_entity_id"]) if row.get("canonical_entity_id") else None,
        title=row.get("title"),
        status=row.get("status", "open"),
        first_event_at=row["first_event_at"],
        last_activity_at=row["last_activity_at"],
        latest_memory_id=UUID(row["latest_memory_id"]) if row.get("latest_memory_id") else None,
        latest_receipt_id=row.get("latest_receipt_id"),
        latest_approval_id=row.get("latest_approval_id"),
        participants=[UUID(x) for x in (row.get("participants") or [])],
        tags=row.get("tags") or [],
        created_at=row["created_at"],
    )


def _build_write_receipt(
    *,
    receipt_type: str,
    memory_id: UUID,
    memory_type: MemoryType | str,
    scope: ScopedIdentity,
    idempotency_key: str | None,
    trace_id: UUID,
    correlation_id: UUID,
    outcome: str = "success",
    reason_code: str | None = None,
) -> dict[str, Any]:
    """Build a receipt dict for memory_write / memory_status_change operations.

    PII is never included — only IDs, type labels, scope, and idempotency_key.
    """
    return {
        "id": str(uuid.uuid4()),
        "receipt_type": receipt_type,
        "tenant_id": str(scope.tenant_id),
        "suite_id": str(scope.suite_id),
        "office_id": str(scope.office_id),
        "actor_id": str(scope.actor_id) if scope.actor_id else None,
        "actor_type": "WORKER",
        "action_type": receipt_type,
        "tool_used": "memory_service",
        "risk_tier": "green",
        "trace_id": str(trace_id),
        "correlation_id": str(correlation_id),
        "redacted_inputs": {
            "memory_type": memory_type,
            "idempotency_key": idempotency_key,
        },
        "redacted_outputs": {
            "memory_id": str(memory_id),
        },
        "outcome": outcome,
        "reason_code": reason_code,
        "created_at": _now_utc().isoformat(),
    }


def _assert_scope_match(row_scope: dict[str, str], scope: ScopedIdentity) -> None:
    """Raise MemoryServiceError if row scope does not match caller scope."""
    mismatches: list[str] = []
    for field_name in ("tenant_id", "suite_id", "office_id"):
        row_val = str(row_scope.get(field_name, "")).lower()
        expected_val = str(getattr(scope, field_name)).lower()
        if row_val != expected_val:
            mismatches.append(field_name)
    if mismatches:
        raise MemoryServiceError(
            f"Tenant isolation violation: scope mismatch on fields {mismatches}",
            code="TENANT_ISOLATION_VIOLATION",
            tenant_id=scope.tenant_id,
            correlation_id=None,
        )


# ---------------------------------------------------------------------------
# MemoryService
# ---------------------------------------------------------------------------


class MemoryService:
    """Single write path for all Office Memory Engine objects.

    Stateless — safe to instantiate per-request or as a shared singleton.
    All methods are async and use the shared httpx connection pool from
    supabase_client.
    """

    async def write(
        self,
        envelope: MemoryObjectIn,
        *,
        scope: ScopedIdentity,
        embed: bool = True,
    ) -> MemoryObjectOut:
        """Insert a new memory_object. Returns the inserted row.

        Idempotency: if idempotency_key already exists for this tenant+suite,
        the existing row is returned without re-writing or re-emitting a receipt.

        Args:
            envelope: Write-shape Pydantic model (validated at call site).
            scope: Tenant/suite/office scope — must match envelope.scope.
            embed: When True and envelope.embedding is None, compute the
                   1536-dim embedding from title + summary before writing.

        Returns:
            MemoryObjectOut — the DB row after insert (or cached dedup row).

        Raises:
            MemoryServiceError: On scope mismatch, DB failure, or embed failure.
        """
        # --- scope pre-check (Law #6 defense-in-depth) ---
        _assert_scope_match(
            {
                "tenant_id": str(envelope.scope.tenant_id),
                "suite_id": str(envelope.scope.suite_id),
                "office_id": str(envelope.scope.office_id),
            },
            scope,
        )

        # --- compute embedding if needed ---
        embedding_vector: list[float] | None = envelope.embedding
        if embedding_vector is None and embed:
            text = " ".join(
                filter(None, [envelope.title, envelope.summary.strip()])
            )
            try:
                from aspire_orchestrator.services.embedding_cache import get_embedding_cache
                from aspire_orchestrator.services.legal_embedding_service import embed_text

                cache = get_embedding_cache()
                embedding_vector = await cache.get_or_embed(
                    text, embed_text, model="text-embedding-3-large"
                )
            except Exception as exc:
                raise MemoryServiceError(
                    f"Embedding computation failed: {exc}",
                    code="EMBED_FAILED",
                    tenant_id=scope.tenant_id,
                    correlation_id=envelope.provenance.correlation_id,
                ) from exc

        # --- build DB row ---
        row: dict[str, Any] = {
            **_scope_dict(scope),
            **_provenance_dict(envelope.provenance),
            "memory_type": envelope.memory_type,
            "entity_type": envelope.entity_type,
            "entity_id": str(envelope.entity_id) if envelope.entity_id else None,
            "thread_id": str(envelope.thread_id) if envelope.thread_id else None,
            "title": envelope.title,
            "summary": envelope.summary,
            "detail": envelope.detail,
            "confidence": envelope.confidence,
            "visibility_scope": envelope.visibility_scope,
            "status": envelope.status,
            "linked_receipt_ids": [str(x) for x in envelope.linked_receipt_ids],
            "linked_approval_ids": [str(x) for x in envelope.linked_approval_ids],
            "linked_artifact_ids": [str(x) for x in envelope.linked_artifact_ids],
            "linked_workflow_run_ids": [str(x) for x in envelope.linked_workflow_run_ids],
            "event_at": envelope.event_at.isoformat() if envelope.event_at else None,
            "source_updated_at": envelope.source_updated_at.isoformat() if envelope.source_updated_at else None,
            "promoted_at": envelope.promoted_at.isoformat() if envelope.promoted_at else None,
            "approved_at": envelope.approved_at.isoformat() if envelope.approved_at else None,
            "executed_at": envelope.executed_at.isoformat() if envelope.executed_at else None,
            "summary_window_start_at": envelope.summary_window_start_at.isoformat() if envelope.summary_window_start_at else None,
            "summary_window_end_at": envelope.summary_window_end_at.isoformat() if envelope.summary_window_end_at else None,
            "fresh_until": envelope.fresh_until.isoformat() if envelope.fresh_until else None,
            "idempotency_key": envelope.idempotency_key,
        }
        if embedding_vector is not None:
            # pgvector PostgREST accepts a JSON array for vector columns
            row["embedding"] = embedding_vector

        # --- insert with idempotency dedup ---
        try:
            inserted = await supabase_insert("memory_objects", row)
        except SupabaseClientError as exc:
            # PostgreSQL UNIQUE constraint violation → 409 or code 23505
            detail = exc.detail.lower()
            is_conflict = exc.status_code == 409 or "23505" in detail or "unique" in detail
            if is_conflict and envelope.idempotency_key:
                logger.info(
                    "memory_write idempotency hit: key=%s tenant=%s",
                    envelope.idempotency_key,
                    str(scope.tenant_id),
                )
                existing = await self._fetch_by_idempotency_key(
                    envelope.idempotency_key, scope=scope
                )
                if existing:
                    return existing
            # Non-idempotency or un-recoverable DB error
            logger.error(
                "memory_write DB error: memory_type=%s tenant=%s code=%s",
                envelope.memory_type,
                str(scope.tenant_id),
                exc.status_code,
            )
            raise MemoryServiceError(
                f"DB insert failed: {exc.detail}",
                code="DB_INSERT_FAILED",
                tenant_id=scope.tenant_id,
                correlation_id=envelope.provenance.correlation_id,
            ) from exc

        out = _row_to_memory_out(inserted)

        # --- emit receipt (Law #2) ---
        receipt = _build_write_receipt(
            receipt_type="memory_write",
            memory_id=out.memory_id,
            memory_type=out.memory_type,
            scope=scope,
            idempotency_key=envelope.idempotency_key,
            trace_id=envelope.provenance.trace_id,
            correlation_id=envelope.provenance.correlation_id,
        )
        store_receipts([receipt])

        logger.info(
            "memory_write: memory_id=%s memory_type=%s tenant=%s",
            out.memory_id,
            out.memory_type,
            str(scope.tenant_id),
        )
        return out

    async def get(
        self,
        memory_id: UUID,
        *,
        scope: ScopedIdentity,
    ) -> MemoryObjectOut | None:
        """Fetch a single memory_object by primary key.

        Returns None if not found. Raises MemoryServiceError on scope mismatch.
        """
        try:
            rows = await supabase_select(
                "memory_objects",
                f"memory_id=eq.{memory_id}",
                limit=1,
            )
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB select failed: {exc.detail}",
                code="DB_SELECT_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc

        if not rows:
            return None

        row = rows[0]
        _assert_scope_match(row, scope)
        return _row_to_memory_out(row)

    async def list_by_thread(
        self,
        thread_id: UUID,
        *,
        scope: ScopedIdentity,
        memory_types: list[MemoryType] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[MemoryObjectOut], str | None]:
        """List memory objects for a thread, ordered by last_activity_at DESC.

        Returns (items, next_cursor). next_cursor is opaque — pass it as cursor
        on the next call to get the next page. None means no more pages.

        Cursor format: ISO timestamp used as a last_activity_at < pivot filter.
        """
        filter_parts = [
            f"thread_id=eq.{thread_id}",
            f"tenant_id=eq.{scope.tenant_id}",
            f"suite_id=eq.{scope.suite_id}",
            f"office_id=eq.{scope.office_id}",
        ]
        if memory_types:
            # PostgREST IN filter: memory_type=in.(a,b,c)
            type_list = ",".join(memory_types)
            filter_parts.append(f"memory_type=in.({type_list})")
        if cursor:
            filter_parts.append(f"last_activity_at=lt.{cursor}")

        filter_str = "&".join(filter_parts)
        try:
            rows = await supabase_select(
                "memory_objects",
                filter_str,
                order_by="last_activity_at.desc",
                limit=limit + 1,  # over-fetch to detect next page
            )
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB list_by_thread failed: {exc.detail}",
                code="DB_SELECT_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc

        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor: str | None = None
        if has_more and page:
            # Use last_activity_at of the last item as the next cursor
            next_cursor = page[-1].get("last_activity_at")

        items = [_row_to_memory_out(r) for r in page]
        return items, next_cursor

    async def list_by_entity(
        self,
        entity_type: str,
        entity_id: UUID,
        *,
        scope: ScopedIdentity,
        memory_types: list[MemoryType] | None = None,
        limit: int = 50,
    ) -> list[MemoryObjectOut]:
        """List memory objects for a canonical entity, ordered by last_activity_at DESC."""
        filter_parts = [
            f"entity_type=eq.{entity_type}",
            f"entity_id=eq.{entity_id}",
            f"tenant_id=eq.{scope.tenant_id}",
            f"suite_id=eq.{scope.suite_id}",
            f"office_id=eq.{scope.office_id}",
        ]
        if memory_types:
            type_list = ",".join(memory_types)
            filter_parts.append(f"memory_type=in.({type_list})")

        filter_str = "&".join(filter_parts)
        try:
            rows = await supabase_select(
                "memory_objects",
                filter_str,
                order_by="last_activity_at.desc",
                limit=limit,
            )
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB list_by_entity failed: {exc.detail}",
                code="DB_SELECT_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc

        return [_row_to_memory_out(r) for r in rows]

    async def update_status(
        self,
        memory_id: UUID,
        new_status: MemoryStatus,
        *,
        scope: ScopedIdentity,
        reason: str | None = None,
    ) -> MemoryObjectOut:
        """Update the lifecycle status of a memory_object.

        Pre-checks:
        - Source must not already be 'executed' (immutable per Law #2).
        - Target must not be 'executed' via this path — use the canonical execution
          workflow (which sets all timestamp fields atomically).

        Emits a 'memory_status_change' receipt on success.

        Raises:
            MemoryServiceError: On scope mismatch, immutability violation, or DB failure.
        """
        if new_status == _TERMINAL_STATUS:
            raise MemoryServiceError(
                "Cannot set status='executed' via update_status — "
                "use the execution workflow to transition memory objects to terminal state.",
                code="IMMUTABLE_STATE_TRANSITION",
                tenant_id=scope.tenant_id,
            )

        # Fetch current row to verify scope and current status
        current = await self.get(memory_id, scope=scope)
        if current is None:
            raise MemoryServiceError(
                f"memory_id={memory_id} not found",
                code="NOT_FOUND",
                tenant_id=scope.tenant_id,
            )
        if current.status == _TERMINAL_STATUS:
            raise MemoryServiceError(
                f"memory_id={memory_id} is in 'executed' state and cannot be modified "
                f"(Law #2: immutability). Create a new memory_object with status='superseded' instead.",
                code="IMMUTABLE_STATE_TRANSITION",
                tenant_id=scope.tenant_id,
                correlation_id=current.provenance.correlation_id,
            )

        patch_data: dict[str, Any] = {"status": new_status}
        match_filter = (
            f"memory_id=eq.{memory_id}"
            f"&tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
        )
        try:
            updated_row = await supabase_update("memory_objects", match_filter, patch_data)
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB update_status failed: {exc.detail}",
                code="DB_UPDATE_FAILED",
                tenant_id=scope.tenant_id,
                correlation_id=current.provenance.correlation_id,
            ) from exc

        out = _row_to_memory_out(updated_row)

        # Emit receipt (Law #2)
        receipt = _build_write_receipt(
            receipt_type="memory_status_change",
            memory_id=out.memory_id,
            memory_type=out.memory_type,
            scope=scope,
            idempotency_key=out.idempotency_key,
            trace_id=out.provenance.trace_id,
            correlation_id=out.provenance.correlation_id,
            outcome="success",
            reason_code=reason or f"status_changed_to_{new_status}",
        )
        store_receipts([receipt])

        logger.warning(
            "memory_status_change: memory_id=%s %s->%s tenant=%s",
            memory_id,
            current.status,
            new_status,
            str(scope.tenant_id),
        )
        return out

    async def mark_superseded(
        self,
        memory_id: UUID,
        by_id: UUID,
        *,
        scope: ScopedIdentity,
    ) -> MemoryObjectOut:
        """Mark a memory_object as superseded by a newer object.

        Sets status='superseded' and appends by_id to linked_artifact_ids.
        Emits a 'memory_status_change' receipt.

        Raises:
            MemoryServiceError: On scope mismatch, immutability, or DB failure.
        """
        current = await self.get(memory_id, scope=scope)
        if current is None:
            raise MemoryServiceError(
                f"memory_id={memory_id} not found",
                code="NOT_FOUND",
                tenant_id=scope.tenant_id,
            )
        if current.status == _TERMINAL_STATUS:
            raise MemoryServiceError(
                f"memory_id={memory_id} is in 'executed' state and cannot be superseded "
                f"(Law #2). Create a new memory_object instead.",
                code="IMMUTABLE_STATE_TRANSITION",
                tenant_id=scope.tenant_id,
                correlation_id=current.provenance.correlation_id,
            )

        # Merge by_id into linked_artifact_ids (preserve existing links)
        existing_ids = {str(x) for x in current.linked_artifact_ids}
        existing_ids.add(str(by_id))
        updated_artifact_ids = sorted(existing_ids)

        patch_data: dict[str, Any] = {
            "status": "superseded",
            "linked_artifact_ids": updated_artifact_ids,
        }
        match_filter = (
            f"memory_id=eq.{memory_id}"
            f"&tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
        )
        try:
            updated_row = await supabase_update("memory_objects", match_filter, patch_data)
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB mark_superseded failed: {exc.detail}",
                code="DB_UPDATE_FAILED",
                tenant_id=scope.tenant_id,
                correlation_id=current.provenance.correlation_id,
            ) from exc

        out = _row_to_memory_out(updated_row)

        receipt = _build_write_receipt(
            receipt_type="memory_status_change",
            memory_id=out.memory_id,
            memory_type=out.memory_type,
            scope=scope,
            idempotency_key=out.idempotency_key,
            trace_id=out.provenance.trace_id,
            correlation_id=out.provenance.correlation_id,
            outcome="success",
            reason_code=f"superseded_by_{by_id}",
        )
        store_receipts([receipt])

        logger.warning(
            "memory_mark_superseded: memory_id=%s by=%s tenant=%s",
            memory_id,
            by_id,
            str(scope.tenant_id),
        )
        return out

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    async def _fetch_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        scope: ScopedIdentity,
    ) -> MemoryObjectOut | None:
        """Fetch the existing row for a given idempotency_key within scope."""
        filter_str = (
            f"idempotency_key=eq.{idempotency_key}"
            f"&tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
        )
        try:
            rows = await supabase_select("memory_objects", filter_str, limit=1)
        except SupabaseClientError:
            return None
        if not rows:
            return None
        return _row_to_memory_out(rows[0])
