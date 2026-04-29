"""Tests for EntityThreadResolver.

Covers:
- resolve with explicit thread_id returns existing thread.
- resolve with explicit thread_id not found → MemoryServiceError (fail closed).
- resolve with entity_type + entity_id finds existing canonical thread.
- resolve with entity_type + entity_id creates new thread when none exists.
- resolve with neither falls back to internal_thread keyed by correlation_id.
- upsert_thread idempotent: same canonical entity returns same thread_id on second call.
- upsert_thread handles concurrent insert race by falling back to SELECT.
- get validates scope (cross-tenant raises MemoryServiceError).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, call, patch

import pytest

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryEventEnvelope,
    ScopedIdentity,
    ThreadIn,
)
from aspire_orchestrator.services.entity_thread_resolver import EntityThreadResolver
from aspire_orchestrator.services.memory_service import MemoryServiceError
from aspire_orchestrator.services.supabase_client import SupabaseClientError


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
SUITE_A = uuid.uuid4()
SUITE_B = uuid.uuid4()
OFFICE_A = uuid.uuid4()
TRACE = uuid.uuid4()
CORR = uuid.uuid4()
THREAD_ID = uuid.uuid4()
NOW_ISO = datetime.now(tz=timezone.utc).isoformat()


def _scope_a() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)


def _scope_b() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_B, suite_id=SUITE_B, office_id=OFFICE_A)


def _base_envelope(**kwargs) -> dict:
    return dict(
        tenant_id=TENANT_A,
        suite_id=SUITE_A,
        office_id=OFFICE_A,
        event_type="test_event",
        trace_id=TRACE,
        correlation_id=CORR,
        event_at=datetime.now(tz=timezone.utc),
        idempotency_key=f"test-{uuid.uuid4()}",
        **kwargs,
    )


def _fake_thread_row(
    *,
    thread_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    suite_id: uuid.UUID | None = None,
    office_id: uuid.UUID | None = None,
    canonical_entity_type: str | None = None,
    canonical_entity_id: uuid.UUID | None = None,
    thread_type: str = "internal_thread",
) -> dict:
    return {
        "thread_id": str(thread_id or THREAD_ID),
        "tenant_id": str(tenant_id or TENANT_A),
        "suite_id": str(suite_id or SUITE_A),
        "office_id": str(office_id or OFFICE_A),
        "thread_type": thread_type,
        "finance_thread_subtype": None,
        "canonical_entity_type": canonical_entity_type,
        "canonical_entity_id": str(canonical_entity_id) if canonical_entity_id else None,
        "title": "Test thread",
        "status": "open",
        "first_event_at": NOW_ISO,
        "last_activity_at": NOW_ISO,
        "latest_memory_id": None,
        "latest_receipt_id": None,
        "latest_approval_id": None,
        "participants": [],
        "tags": [],
        "created_at": NOW_ISO,
    }


# ---------------------------------------------------------------------------
# resolve: explicit thread_id
# ---------------------------------------------------------------------------


class TestResolveExplicitThreadId:
    @pytest.mark.asyncio
    async def test_explicit_thread_id_returns_existing_thread(self) -> None:
        resolver = EntityThreadResolver()
        fake_row = _fake_thread_row()
        env = MemoryEventEnvelope(**_base_envelope(thread_id=THREAD_ID))

        with patch(
            "aspire_orchestrator.services.entity_thread_resolver.supabase_select",
            new_callable=AsyncMock,
            return_value=[fake_row],
        ):
            result = await resolver.resolve(env)

        assert result.thread_id == uuid.UUID(fake_row["thread_id"])

    @pytest.mark.asyncio
    async def test_explicit_thread_id_not_found_raises(self) -> None:
        """Fail closed: explicit thread_id that doesn't exist → MemoryServiceError."""
        resolver = EntityThreadResolver()
        env = MemoryEventEnvelope(**_base_envelope(thread_id=THREAD_ID))

        with patch(
            "aspire_orchestrator.services.entity_thread_resolver.supabase_select",
            new_callable=AsyncMock,
            return_value=[],  # not found
        ):
            with pytest.raises(MemoryServiceError, match="THREAD_NOT_FOUND"):
                await resolver.resolve(env)


# ---------------------------------------------------------------------------
# resolve: entity_type + entity_id
# ---------------------------------------------------------------------------


class TestResolveByEntity:
    @pytest.mark.asyncio
    async def test_finds_existing_canonical_thread(self) -> None:
        resolver = EntityThreadResolver()
        entity_id = uuid.uuid4()
        fake_row = _fake_thread_row(
            canonical_entity_type="customer",
            canonical_entity_id=entity_id,
            thread_type="customer_thread",
        )
        env = MemoryEventEnvelope(**_base_envelope(
            entity_type="customer",
            entity_id=entity_id,
        ))

        with (
            # _find_canonical_thread returns a row → thread exists
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_select",
                new_callable=AsyncMock,
                return_value=[fake_row],
            ),
            # _touch_thread calls supabase_update
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_update",
                new_callable=AsyncMock,
                return_value=fake_row,
            ),
        ):
            result = await resolver.resolve(env)

        assert result.canonical_entity_type == "customer"
        assert result.canonical_entity_id == entity_id

    @pytest.mark.asyncio
    async def test_creates_new_thread_when_entity_not_found(self) -> None:
        resolver = EntityThreadResolver()
        entity_id = uuid.uuid4()
        new_thread_id = uuid.uuid4()
        fake_row = _fake_thread_row(
            thread_id=new_thread_id,
            canonical_entity_type="customer",
            canonical_entity_id=entity_id,
            thread_type="customer_thread",
        )
        env = MemoryEventEnvelope(**_base_envelope(
            entity_type="customer",
            entity_id=entity_id,
        ))

        with (
            # SELECT returns empty → no existing thread
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_select",
                new_callable=AsyncMock,
                return_value=[],
            ),
            # INSERT succeeds
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_insert",
                new_callable=AsyncMock,
                return_value=fake_row,
            ),
        ):
            result = await resolver.resolve(env)

        assert result.thread_id == new_thread_id
        assert result.thread_type == "customer_thread"

    @pytest.mark.parametrize(
        "entity_type,expected_thread_type",
        [
            ("lead", "lead_thread"),
            ("customer", "customer_thread"),
            ("invoice", "invoice_thread"),
            ("payment", "finance_thread"),
            ("unknown_type", "internal_thread"),
        ],
    )
    @pytest.mark.asyncio
    async def test_infer_thread_type_mapping(
        self, entity_type: str, expected_thread_type: str
    ) -> None:
        from aspire_orchestrator.services.entity_thread_resolver import _infer_thread_type

        assert _infer_thread_type(entity_type) == expected_thread_type


# ---------------------------------------------------------------------------
# resolve: fallback to internal_thread
# ---------------------------------------------------------------------------


class TestResolveFallbackInternalThread:
    @pytest.mark.asyncio
    async def test_no_thread_no_entity_creates_internal_thread(self) -> None:
        resolver = EntityThreadResolver()
        new_thread_id = uuid.uuid4()
        fake_row = _fake_thread_row(
            thread_id=new_thread_id,
            thread_type="internal_thread",
        )
        env = MemoryEventEnvelope(**_base_envelope())
        # No thread_id, no entity_type, no entity_id

        with (
            # SELECT: no canonical entity anchor → no dedup check
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_select",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_insert",
                new_callable=AsyncMock,
                return_value=fake_row,
            ),
        ):
            result = await resolver.resolve(env)

        assert result.thread_type == "internal_thread"
        assert result.thread_id == new_thread_id


# ---------------------------------------------------------------------------
# upsert_thread: idempotency
# ---------------------------------------------------------------------------


class TestUpsertThreadIdempotency:
    @pytest.mark.asyncio
    async def test_same_canonical_entity_returns_same_thread_id(self) -> None:
        """Two calls with the same canonical entity must return the same thread."""
        resolver = EntityThreadResolver()
        entity_id = uuid.uuid4()
        fake_row = _fake_thread_row(
            canonical_entity_type="deal",
            canonical_entity_id=entity_id,
            thread_type="deal_thread",
        )
        thread_in = ThreadIn(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            thread_type="deal_thread",
            canonical_entity_type="deal",
            canonical_entity_id=entity_id,
        )

        with (
            # Both calls: SELECT returns existing thread → no INSERT
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_select",
                new_callable=AsyncMock,
                return_value=[fake_row],
            ),
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_update",
                new_callable=AsyncMock,
                return_value=fake_row,
            ),
        ):
            first = await resolver.upsert_thread(thread_in)
            second = await resolver.upsert_thread(thread_in)

        assert first.thread_id == second.thread_id

    @pytest.mark.asyncio
    async def test_concurrent_insert_race_falls_back_to_select(self) -> None:
        """If INSERT fails with a conflict (concurrent race), fall back to SELECT."""
        resolver = EntityThreadResolver()
        entity_id = uuid.uuid4()
        fake_row = _fake_thread_row(
            canonical_entity_type="job",
            canonical_entity_id=entity_id,
            thread_type="job_thread",
        )
        thread_in = ThreadIn(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            thread_type="job_thread",
            canonical_entity_type="job",
            canonical_entity_id=entity_id,
        )

        conflict_err = SupabaseClientError(
            "insert/threads", status_code=409, detail="23505 unique_violation"
        )

        select_call_count = 0

        async def mock_select(table, filters, **kwargs):
            nonlocal select_call_count
            select_call_count += 1
            if select_call_count == 1:
                # First SELECT (_find_canonical_thread): nothing found → trigger INSERT
                return []
            # Second SELECT (fallback after conflict): returns the row
            return [fake_row]

        with (
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_select",
                new_callable=AsyncMock,
                side_effect=mock_select,
            ),
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_insert",
                new_callable=AsyncMock,
                side_effect=conflict_err,
            ),
        ):
            result = await resolver.upsert_thread(thread_in)

        assert result.thread_id == uuid.UUID(fake_row["thread_id"])
        assert select_call_count == 2  # first for dedup check, second for fallback


# ---------------------------------------------------------------------------
# get: scope isolation
# ---------------------------------------------------------------------------


class TestEntityThreadResolverGet:
    @pytest.mark.asyncio
    async def test_get_cross_tenant_raises(self) -> None:
        """Row belongs to tenant A; caller presents scope B → MemoryServiceError."""
        resolver = EntityThreadResolver()
        fake_row = _fake_thread_row(tenant_id=TENANT_A, suite_id=SUITE_A)

        with patch(
            "aspire_orchestrator.services.entity_thread_resolver.supabase_select",
            new_callable=AsyncMock,
            return_value=[fake_row],
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await resolver.get(THREAD_ID, scope=_scope_b())

    @pytest.mark.asyncio
    async def test_get_returns_none_when_not_found(self) -> None:
        resolver = EntityThreadResolver()
        with patch(
            "aspire_orchestrator.services.entity_thread_resolver.supabase_select",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await resolver.get(THREAD_ID, scope=_scope_a())
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_thread_when_found(self) -> None:
        resolver = EntityThreadResolver()
        fake_row = _fake_thread_row()
        with patch(
            "aspire_orchestrator.services.entity_thread_resolver.supabase_select",
            new_callable=AsyncMock,
            return_value=[fake_row],
        ):
            result = await resolver.get(THREAD_ID, scope=_scope_a())
        assert result is not None
        assert result.thread_id == uuid.UUID(fake_row["thread_id"])
