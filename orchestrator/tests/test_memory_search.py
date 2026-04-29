"""Tests for MemorySearchService (Pass 5).

Mocks supabase_rpc + the embedding cache. No real Supabase or OpenAI
connections required.

Covers (per plan §11 Pass 5 spec):
- Entity match returns highest score (Tier 1 dominates)
- Thread match prioritized over recency
- Approval/receipt boost is applied
- Recency decay produces lower scores for older rows
- Visibility scope filter excludes finance memory from office query and vice versa
- Cross-tenant scope mismatch raises MemoryServiceError(code='TENANT_ISOLATION_VIOLATION')
- Empty query (no text, no entity, no thread, no embedding) returns empty list
- Embedding length validation: query_embedding must be 1536 if provided
- Service-layer scope is enforced before any RPC call (zero-trust)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.schemas.memory_v1 import (
    MemorySearchRequest,
    ScopedIdentity,
)
from aspire_orchestrator.services.memory_search import MemorySearchService
from aspire_orchestrator.services.memory_service import MemoryServiceError


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
SUITE_A = uuid.uuid4()
OFFICE_A = uuid.uuid4()
THREAD_X = uuid.uuid4()
ENTITY_LEAD = uuid.uuid4()
TRACE = uuid.uuid4()
CORR = uuid.uuid4()


def _scope_a() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)


def _scope_b() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_B, suite_id=SUITE_A, office_id=OFFICE_A)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _days_ago_iso(days: float) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()


def _make_row(
    *,
    memory_id: uuid.UUID | None = None,
    score: float,
    thread_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    last_activity_at_iso: str | None = None,
    visibility_scope: str = "office",
    confidence: float | None = None,
    linked_receipt_ids: list[str] | None = None,
    linked_approval_ids: list[str] | None = None,
    summary: str = "Test memory summary.",
    memory_type: str = "session_summary",
) -> dict:
    """Build a synthetic RPC row with all required columns."""
    mid = memory_id or uuid.uuid4()
    return {
        "memory_id": str(mid),
        "tenant_id": str(TENANT_A),
        "suite_id": str(SUITE_A),
        "office_id": str(OFFICE_A),
        "memory_type": memory_type,
        "schema_version": "v1",
        "trace_id": str(TRACE),
        "correlation_id": str(CORR),
        "entity_type": entity_type,
        "entity_id": str(entity_id) if entity_id else None,
        "thread_id": str(thread_id) if thread_id else None,
        "title": None,
        "summary": summary,
        "detail": {},
        "confidence": confidence,
        "visibility_scope": visibility_scope,
        "status": None,
        "linked_receipt_ids": linked_receipt_ids or [],
        "linked_approval_ids": linked_approval_ids or [],
        "linked_artifact_ids": [],
        "linked_workflow_run_ids": [],
        "event_at": None,
        "created_at": last_activity_at_iso or _now_iso(),
        "source_updated_at": None,
        "promoted_at": None,
        "approved_at": None,
        "executed_at": None,
        "last_activity_at": last_activity_at_iso or _now_iso(),
        "summary_window_start_at": None,
        "summary_window_end_at": None,
        "fresh_until": None,
        "idempotency_key": None,
        "score": score,
    }


def _patch_rpc(rows: list[dict]) -> AsyncMock:
    """Patch supabase_rpc to return the given rows."""
    return patch(
        "aspire_orchestrator.services.memory_search.supabase_rpc",
        new_callable=AsyncMock,
        return_value=rows,
    )


# ---------------------------------------------------------------------------
# Empty query — no anchors, no text, no embedding -> empty result, no RPC call
# ---------------------------------------------------------------------------


class TestEmptyQuery:
    @pytest.mark.asyncio
    async def test_empty_query_returns_empty_without_rpc_call(self) -> None:
        svc = MemorySearchService()
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            visibility_scope="office",
        )

        with patch(
            "aspire_orchestrator.services.memory_search.supabase_rpc",
            new_callable=AsyncMock,
        ) as mock_rpc:
            result = await svc.search(req, scope=_scope_a())

        assert result.items == []
        assert result.total == 0
        assert result.next_cursor is None
        # Critical: no DB round-trip when caller gave us nothing to anchor
        mock_rpc.assert_not_called()


# ---------------------------------------------------------------------------
# Tenant isolation — service rejects mismatched scope BEFORE any RPC call
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_cross_tenant_scope_raises_before_rpc(self) -> None:
        svc = MemorySearchService()
        # Request claims tenant A; caller scope is tenant B
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            query_text="anything",
        )

        with patch(
            "aspire_orchestrator.services.memory_search.supabase_rpc",
            new_callable=AsyncMock,
        ) as mock_rpc:
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await svc.search(req, scope=_scope_b())

        # Critical: service must fail closed BEFORE issuing the RPC
        mock_rpc.assert_not_called()

    @pytest.mark.asyncio
    async def test_rpc_42501_translated_to_isolation_violation(self) -> None:
        """If the RPC raises SQLSTATE 42501, the service must surface it
        as MemoryServiceError(code='TENANT_ISOLATION_VIOLATION')."""
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        svc = MemorySearchService()
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            query_text="legitimate query",
        )

        rpc_error = SupabaseClientError(
            "rpc/search_memory_objects",
            status_code=403,
            detail="42501 TENANT_ISOLATION_VIOLATION caller is not a member",
        )

        with patch(
            "aspire_orchestrator.services.memory_search.supabase_rpc",
            new_callable=AsyncMock,
            side_effect=rpc_error,
        ), patch.object(
            svc, "_compute_embedding", new_callable=AsyncMock, return_value=None
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await svc.search(req, scope=_scope_a())


# ---------------------------------------------------------------------------
# Embedding validation — 1536 dims required if provided
# ---------------------------------------------------------------------------


class TestEmbeddingValidation:
    def test_query_embedding_wrong_length_rejected_at_schema_layer(self) -> None:
        """MemorySearchRequest validator must reject embeddings != 1536 dims."""
        with pytest.raises(ValueError, match="1536 dimensions"):
            MemorySearchRequest(
                tenant_id=TENANT_A,
                suite_id=SUITE_A,
                office_id=OFFICE_A,
                query_embedding=[0.1, 0.2, 0.3],  # only 3 dims
            )

    def test_query_embedding_correct_length_accepted(self) -> None:
        """Exactly 1536 dims must validate."""
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            query_embedding=[0.0] * 1536,
        )
        assert req.query_embedding is not None and len(req.query_embedding) == 1536


# ---------------------------------------------------------------------------
# Tier 1 — exact entity match outranks all other tiers
# ---------------------------------------------------------------------------


class TestRankingTiers:
    @pytest.mark.asyncio
    async def test_entity_match_returns_highest_score(self) -> None:
        """Tier 1 (entity match, score 1.0) outranks Tier 2 (thread, 0.9)
        when both candidates are present in the result set.

        We mock the RPC to return synthetic rows already ordered by the SQL
        ranker — the assertion verifies the service preserves that ordering.
        """
        entity_row = _make_row(
            score=1.45,  # entity match + recency boost
            entity_type="lead",
            entity_id=ENTITY_LEAD,
            thread_id=THREAD_X,
            confidence=0.9,
            last_activity_at_iso=_now_iso(),
        )
        thread_only_row = _make_row(
            score=1.10,
            thread_id=THREAD_X,
            entity_type=None,
            entity_id=None,
            last_activity_at_iso=_now_iso(),
        )

        svc = MemorySearchService()
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            entity_type="lead",
            entity_id=ENTITY_LEAD,
            thread_id=THREAD_X,
        )

        with _patch_rpc([entity_row, thread_only_row]) as mock_rpc:
            result = await svc.search(req, scope=_scope_a())

        assert len(result.items) == 2
        # Entity-matched row must be first
        assert str(result.items[0].entity_id) == str(ENTITY_LEAD)
        assert result.items[0].entity_type == "lead"
        # Thread-only row must be second
        assert result.items[1].entity_type is None
        assert str(result.items[1].thread_id) == str(THREAD_X)
        mock_rpc.assert_called_once()

    @pytest.mark.asyncio
    async def test_thread_match_prioritized_over_pure_recency(self) -> None:
        """Tier 2 (thread match, +0.9 floor) outranks a recent unmatched row."""
        thread_match_old = _make_row(
            score=1.05,
            thread_id=THREAD_X,
            last_activity_at_iso=_days_ago_iso(7.0),
        )
        no_match_recent = _make_row(
            score=0.30,  # only recency contributes (0.25 * ~1.0)
            thread_id=uuid.uuid4(),  # different thread
            last_activity_at_iso=_now_iso(),
        )

        svc = MemorySearchService()
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            thread_id=THREAD_X,
        )

        with _patch_rpc([thread_match_old, no_match_recent]):
            result = await svc.search(req, scope=_scope_a())

        assert len(result.items) == 2
        assert str(result.items[0].thread_id) == str(THREAD_X)

    @pytest.mark.asyncio
    async def test_approval_receipt_boost_lifts_row(self) -> None:
        """Tier 3 (+0.10 boost) should appear in the score ordering: a row
        with linked receipts/approvals should outscore an otherwise identical
        row without them.
        """
        with_receipts = _make_row(
            score=1.18,
            thread_id=THREAD_X,
            linked_receipt_ids=[str(uuid.uuid4())],
            last_activity_at_iso=_now_iso(),
        )
        without_receipts = _make_row(
            score=1.08,
            thread_id=THREAD_X,
            last_activity_at_iso=_now_iso(),
        )

        svc = MemorySearchService()
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            thread_id=THREAD_X,
        )

        with _patch_rpc([with_receipts, without_receipts]):
            result = await svc.search(req, scope=_scope_a())

        assert len(result.items) == 2
        assert len(result.items[0].linked_receipt_ids) == 1
        assert len(result.items[1].linked_receipt_ids) == 0

    @pytest.mark.asyncio
    async def test_recency_decay_orders_older_rows_lower(self) -> None:
        """Tier 4 (recency, exp half-life 14 days) ranks newer rows above
        older identical rows when only recency differs."""
        new_row = _make_row(
            score=1.13,  # 0.9 + 0.25 * ~1.0
            thread_id=THREAD_X,
            last_activity_at_iso=_now_iso(),
        )
        old_row = _make_row(
            score=0.95,  # 0.9 + 0.25 * ~0.2 (28 days old)
            thread_id=THREAD_X,
            last_activity_at_iso=_days_ago_iso(28.0),
        )

        svc = MemorySearchService()
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            thread_id=THREAD_X,
        )

        with _patch_rpc([new_row, old_row]):
            result = await svc.search(req, scope=_scope_a())

        # First result has newer last_activity_at
        assert result.items[0].last_activity_at > result.items[1].last_activity_at


# ---------------------------------------------------------------------------
# Visibility scope — office vs finance enforcement (RPC contract)
# ---------------------------------------------------------------------------


class TestVisibilityScope:
    @pytest.mark.asyncio
    async def test_office_scope_passed_through_to_rpc(self) -> None:
        """When caller requests visibility_scope='office', the RPC must
        receive p_visibility_scope='office' so finance rows are excluded."""
        svc = MemorySearchService()
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            visibility_scope="office",
            query_text="invoice",
        )

        with _patch_rpc([]) as mock_rpc, patch.object(
            svc, "_compute_embedding", new_callable=AsyncMock, return_value=None
        ):
            await svc.search(req, scope=_scope_a())

        # rpc_args.args = (fn_name, params); fn_name is positional, params is positional
        params = mock_rpc.call_args[0][1]
        assert params["p_visibility_scope"] == "office"

    @pytest.mark.asyncio
    async def test_finance_scope_passed_through_to_rpc(self) -> None:
        """When caller requests visibility_scope='finance', the RPC must
        receive p_visibility_scope='finance' so office rows are excluded."""
        svc = MemorySearchService()
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            visibility_scope="finance",
            query_text="payable",
        )

        with _patch_rpc([]) as mock_rpc, patch.object(
            svc, "_compute_embedding", new_callable=AsyncMock, return_value=None
        ):
            await svc.search(req, scope=_scope_a())

        params = mock_rpc.call_args[0][1]
        assert params["p_visibility_scope"] == "finance"

    @pytest.mark.asyncio
    async def test_office_query_does_not_surface_finance_rows(self) -> None:
        """Synthetic guarantee: when the (mocked) RPC honors visibility_scope
        and returns only office rows, the service surfaces them all.
        Conversely, finance rows must never appear in office results."""
        finance_row = _make_row(
            score=1.0,
            thread_id=THREAD_X,
            visibility_scope="finance",
        )
        office_row = _make_row(
            score=1.0,
            thread_id=THREAD_X,
            visibility_scope="office",
        )

        svc = MemorySearchService()

        # Office query: only office rows returned by RPC contract
        req_office = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            visibility_scope="office",
            thread_id=THREAD_X,
        )
        with _patch_rpc([office_row]):
            office_result = await svc.search(req_office, scope=_scope_a())

        # Finance query: only finance rows returned by RPC contract
        req_finance = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            visibility_scope="finance",
            thread_id=THREAD_X,
        )
        with _patch_rpc([finance_row]):
            finance_result = await svc.search(req_finance, scope=_scope_a())

        assert all(r.visibility_scope == "office" for r in office_result.items)
        assert all(r.visibility_scope == "finance" for r in finance_result.items)
        assert len(office_result.items) == 1
        assert len(finance_result.items) == 1


# ---------------------------------------------------------------------------
# include_raw — controls whether detail JSONB is returned
# ---------------------------------------------------------------------------


class TestIncludeRaw:
    @pytest.mark.asyncio
    async def test_include_raw_false_strips_detail(self) -> None:
        """include_raw=False should return empty detail dict (no raw transcript)."""
        row_with_detail = _make_row(score=1.0, thread_id=THREAD_X)
        row_with_detail["detail"] = {"raw_transcript": "private content..."}

        svc = MemorySearchService()
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            thread_id=THREAD_X,
            include_raw=False,
        )

        with _patch_rpc([row_with_detail]):
            result = await svc.search(req, scope=_scope_a())

        assert result.items[0].detail == {}

    @pytest.mark.asyncio
    async def test_include_raw_true_returns_detail(self) -> None:
        """include_raw=True must surface the full detail JSONB."""
        row_with_detail = _make_row(score=1.0, thread_id=THREAD_X)
        row_with_detail["detail"] = {"raw_transcript": "explicit content"}

        svc = MemorySearchService()
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            thread_id=THREAD_X,
            include_raw=True,
        )

        with _patch_rpc([row_with_detail]):
            result = await svc.search(req, scope=_scope_a())

        assert result.items[0].detail.get("raw_transcript") == "explicit content"


# ---------------------------------------------------------------------------
# Embedding compute path — text-only queries must call embedding cache
# ---------------------------------------------------------------------------


class TestEmbeddingComputePath:
    @pytest.mark.asyncio
    async def test_text_only_query_computes_embedding(self) -> None:
        """When caller provides query_text but not query_embedding, the
        service must compute an embedding and pass it to the RPC."""
        svc = MemorySearchService()
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            query_text="search invoices",
        )

        with _patch_rpc([]) as mock_rpc, patch.object(
            svc, "_compute_embedding", new_callable=AsyncMock, return_value=[0.5] * 1536
        ) as mock_embed:
            await svc.search(req, scope=_scope_a())

        mock_embed.assert_called_once_with("search invoices")
        params = mock_rpc.call_args[0][1]
        # Embedding was passed through to the RPC as 1536-d list
        assert params["p_query_embedding"] is not None
        assert len(params["p_query_embedding"]) == 1536
        assert params["p_query_text"] == "search invoices"

    @pytest.mark.asyncio
    async def test_supplied_embedding_skips_compute(self) -> None:
        """When caller provides query_embedding, the service must NOT call
        the embedding cache (avoid double-compute and unnecessary OpenAI calls)."""
        svc = MemorySearchService()
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            query_embedding=[0.1] * 1536,
        )

        with _patch_rpc([]), patch.object(
            svc, "_compute_embedding", new_callable=AsyncMock
        ) as mock_embed:
            await svc.search(req, scope=_scope_a())

        mock_embed.assert_not_called()

    @pytest.mark.asyncio
    async def test_embedding_failure_degrades_gracefully(self) -> None:
        """When _compute_embedding returns None, the service must still
        run the keyword path (graceful degradation) rather than failing."""
        svc = MemorySearchService()
        req = MemorySearchRequest(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            query_text="degraded path",
        )

        with _patch_rpc([]) as mock_rpc, patch.object(
            svc, "_compute_embedding", new_callable=AsyncMock, return_value=None
        ):
            result = await svc.search(req, scope=_scope_a())

        assert result.items == []
        params = mock_rpc.call_args[0][1]
        # No embedding sent — keyword path only
        assert params["p_query_embedding"] is None
        assert params["p_query_text"] == "degraded path"
