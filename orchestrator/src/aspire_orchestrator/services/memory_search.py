"""Memory Search Service — hybrid keyword + vector search with §3.4 ranking.

Single read path for /v1/memory/search and the office/finance memory pages.
All retrieval logic + scoring lives in the SQL RPC public.search_memory_objects
(migration 099) — this Python service is a thin orchestration layer that:

  1. Validates scope (Law #6 defense-in-depth above the SQL gate)
  2. Computes the query embedding when caller passed query_text only
  3. Calls the RPC with all filters
  4. Maps DB rows → MemoryObjectOut and strips the embedding column unless
     the caller explicitly requested raw payloads (include_raw=True)
  5. Emits a structured log (no PII) — search reads do NOT cut receipts
     (Law #2 applies to state changes, not reads)

Law compliance:
  Law #2: Reads do not emit receipts (read-only operation).
  Law #3: Empty queries return empty results — never full-scan a tenant.
  Law #6: tenant_id / suite_id / office_id pre-checked at the service layer
          AND inside the RPC (defense in depth, two independent gates).
  Law #9: Query text is truncated in log lines; embeddings are never logged.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any
from uuid import UUID

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectOut,
    MemorySearchRequest,
    MemorySearchResponse,
    Provenance,
    ScopedIdentity,
)
from aspire_orchestrator.services.memory_service import MemoryServiceError
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_rpc,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal row → schema mapping
# ---------------------------------------------------------------------------


def _coerce_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    # PostgREST returns ISO-8601 strings for TIMESTAMPTZ columns.
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _coerce_uuid_list(value: Any) -> list[UUID]:
    if not value:
        return []
    return [UUID(str(x)) for x in value]


def _row_to_memory_out(row: dict[str, Any], *, include_raw: bool) -> MemoryObjectOut:
    """Map a DB row from search_memory_objects to MemoryObjectOut.

    Reconstructs the nested ScopedIdentity and Provenance objects from the
    flattened column layout.  The synthetic ``score`` column from the RPC is
    intentionally dropped — callers receive ranked rows, not raw scores.
    Embedding is always stripped (the RPC does not return it).
    """
    scope = ScopedIdentity(
        tenant_id=UUID(str(row["tenant_id"])),
        suite_id=UUID(str(row["suite_id"])),
        office_id=UUID(str(row["office_id"])),
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
        trace_id=UUID(str(row["trace_id"])),
        correlation_id=UUID(str(row["correlation_id"])),
        artifact_origin=row.get("artifact_origin"),
        summary_origin=row.get("summary_origin"),
    )

    # When include_raw is False we discard the (already empty) embedding +
    # any large optional fields that callers can refetch by memory_id.
    summary_text = str(row["summary"]) if row.get("summary") is not None else ""
    detail_payload: dict[str, Any] = (row.get("detail") or {}) if include_raw else {}

    return MemoryObjectOut(
        memory_id=UUID(str(row["memory_id"])),
        scope=scope,
        provenance=provenance,
        memory_type=row["memory_type"],
        schema_version=row.get("schema_version") or "v1",
        entity_type=row.get("entity_type"),
        entity_id=_coerce_uuid(row.get("entity_id")),
        thread_id=_coerce_uuid(row.get("thread_id")),
        title=row.get("title"),
        summary=summary_text,
        detail=detail_payload,
        confidence=row.get("confidence"),
        visibility_scope=row.get("visibility_scope") or "office",
        status=row.get("status"),
        linked_receipt_ids=_coerce_uuid_list(row.get("linked_receipt_ids")),
        linked_approval_ids=_coerce_uuid_list(row.get("linked_approval_ids")),
        linked_artifact_ids=_coerce_uuid_list(row.get("linked_artifact_ids")),
        linked_workflow_run_ids=_coerce_uuid_list(row.get("linked_workflow_run_ids")),
        event_at=_coerce_datetime(row.get("event_at")),
        created_at=_coerce_datetime(row["created_at"]) or datetime.fromtimestamp(0),
        source_updated_at=_coerce_datetime(row.get("source_updated_at")),
        promoted_at=_coerce_datetime(row.get("promoted_at")),
        approved_at=_coerce_datetime(row.get("approved_at")),
        executed_at=_coerce_datetime(row.get("executed_at")),
        last_activity_at=_coerce_datetime(row["last_activity_at"]) or datetime.fromtimestamp(0),
        summary_window_start_at=_coerce_datetime(row.get("summary_window_start_at")),
        summary_window_end_at=_coerce_datetime(row.get("summary_window_end_at")),
        fresh_until=_coerce_datetime(row.get("fresh_until")),
        embedding=None,  # never returned from search RPC
        idempotency_key=row.get("idempotency_key"),
    )


# ---------------------------------------------------------------------------
# MemorySearchService
# ---------------------------------------------------------------------------


class MemorySearchService:
    """Hybrid memory search executor.

    Stateless; safe to instantiate per-request or as a shared singleton.
    All DB work is delegated to the SECURITY DEFINER RPC
    public.search_memory_objects (migration 099).
    """

    async def search(
        self,
        req: MemorySearchRequest,
        *,
        scope: ScopedIdentity,
    ) -> MemorySearchResponse:
        """Run a hybrid memory search and return ranked items.

        Args:
            req: Validated MemorySearchRequest. Embeddings, if supplied, must
                 be exactly 1536 dimensions (validated at the schema layer).
            scope: Caller scope from the auth gate (X-Tenant-Id headers).
                   Must match req.tenant_id / suite_id / office_id (Law #6).

        Returns:
            MemorySearchResponse with items ordered by §3.4 final score DESC,
            ties broken by last_activity_at DESC.

        Raises:
            MemoryServiceError(code='TENANT_ISOLATION_VIOLATION') on scope mismatch.
            MemoryServiceError(code='DB_RPC_FAILED') on Supabase RPC failure.
        """
        started_at = time.perf_counter()

        # --- Law #6: scope match (defense-in-depth above SQL gate) ---
        if (
            str(scope.tenant_id) != str(req.tenant_id)
            or str(scope.suite_id) != str(req.suite_id)
            or str(scope.office_id) != str(req.office_id)
        ):
            raise MemoryServiceError(
                "Search scope does not match caller scope",
                code="TENANT_ISOLATION_VIOLATION",
                tenant_id=scope.tenant_id,
            )

        # --- Empty-search guard (Law #3 fail-closed: never full-scan) ---
        has_text = bool(req.query_text and req.query_text.strip())
        has_embedding = req.query_embedding is not None
        has_entity = req.entity_type is not None and req.entity_id is not None
        has_thread = req.thread_id is not None

        if not (has_text or has_embedding or has_entity or has_thread):
            logger.info(
                "memory_search.empty: tenant=%s scope=%s — returning empty",
                str(scope.tenant_id)[:8],
                req.visibility_scope,
            )
            return MemorySearchResponse(items=[], total=0, next_cursor=None)

        # --- Compute embedding from text when caller didn't supply one ---
        embedding_for_rpc: list[float] | None = req.query_embedding
        if embedding_for_rpc is None and has_text:
            embedding_for_rpc = await self._compute_embedding(req.query_text or "")

        # --- Build RPC params ---
        # Important: PostgREST RPC requires JSON-serializable values; UUIDs must
        # be stringified, datetimes ISO-8601, embedding stays as a list of floats.
        params: dict[str, Any] = {
            "p_tenant_id": str(req.tenant_id),
            "p_suite_id": str(req.suite_id),
            "p_office_id": str(req.office_id),
            "p_visibility_scope": req.visibility_scope,
            "p_query_text": req.query_text if has_text else None,
            "p_query_embedding": embedding_for_rpc,
            "p_entity_type": req.entity_type,
            "p_entity_id": str(req.entity_id) if req.entity_id else None,
            "p_thread_id": str(req.thread_id) if req.thread_id else None,
            "p_memory_types": list(req.memory_types) if req.memory_types else None,
            "p_tags": list(req.tags) if req.tags else None,
            "p_date_range_start": req.date_range_start.isoformat() if req.date_range_start else None,
            "p_date_range_end": req.date_range_end.isoformat() if req.date_range_end else None,
            "p_min_confidence": req.min_confidence,
            "p_limit": req.limit,
        }

        # --- Execute RPC ---
        try:
            rpc_result = await supabase_rpc("search_memory_objects", params)
        except SupabaseClientError as exc:
            # Map structured Postgres SQLSTATE 42501 (TENANT_ISOLATION_VIOLATION)
            # back to a MemoryServiceError so callers see the canonical code.
            detail = (exc.detail or "").upper()
            if "TENANT_ISOLATION_VIOLATION" in detail or "42501" in detail:
                raise MemoryServiceError(
                    "Tenant isolation violation reported by RPC",
                    code="TENANT_ISOLATION_VIOLATION",
                    tenant_id=scope.tenant_id,
                ) from exc
            raise MemoryServiceError(
                f"search_memory_objects RPC failed: {exc.detail}",
                code="DB_RPC_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc

        rows: list[dict[str, Any]]
        if rpc_result is None:
            rows = []
        elif isinstance(rpc_result, list):
            rows = rpc_result
        elif isinstance(rpc_result, dict):
            # Defensive: PostgREST sometimes wraps single-row results
            rows = [rpc_result]
        else:
            rows = []

        items = [_row_to_memory_out(r, include_raw=req.include_raw) for r in rows]

        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        logger.info(
            "memory_search.done: tenant=%s scope=%s qlen=%d candidates=%d ms=%.1f",
            str(scope.tenant_id)[:8],
            req.visibility_scope,
            len(req.query_text or ""),
            len(items),
            elapsed_ms,
        )

        return MemorySearchResponse(
            items=items,
            total=len(items),
            next_cursor=None,  # cursor pagination is not yet implemented in RPC
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _compute_embedding(self, text: str) -> list[float] | None:
        """Compute (or fetch from Redis cache) an embedding for the query text.

        Returns None on failure rather than raising — search degrades to
        keyword-only retrieval rather than failing the whole request.
        """
        text = text.strip()
        if not text:
            return None
        try:
            from aspire_orchestrator.services.embedding_cache import (
                get_embedding_cache,
            )
            from aspire_orchestrator.services.legal_embedding_service import (
                embed_text,
            )

            cache = get_embedding_cache()
            vec = await cache.get_or_embed(
                text, embed_text, model="text-embedding-3-large"
            )
            if not isinstance(vec, list) or len(vec) != 1536:
                logger.warning(
                    "memory_search: embedding wrong dim=%d (expected 1536) — degrading to keyword-only",
                    len(vec) if isinstance(vec, list) else -1,
                )
                return None
            return vec
        except Exception as exc:  # noqa: BLE001 — fail-soft, log + continue
            logger.warning(
                "memory_search: embedding compute failed (%s) — degrading to keyword-only",
                type(exc).__name__,
            )
            return None
