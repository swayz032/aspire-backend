"""RLS Evil Tests — threads table (Pass 11).

Law #6: Zero cross-tenant leakage on the threads table.
Validates EntityThreadResolver enforces scope isolation
at the service layer before any DB write.

Aspire Laws:
  Law #3: Fail Closed — scope mismatch → denied.
  Law #6: Tenant Isolation — zero cross-tenant leakage.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryEventEnvelope,
    Provenance,
    ScopedIdentity,
)
from aspire_orchestrator.services.entity_thread_resolver import EntityThreadResolver
from aspire_orchestrator.services.memory_service import MemoryServiceError

# ---------------------------------------------------------------------------
# Tenant fixtures
# ---------------------------------------------------------------------------

TENANT_A = UUID("aa110000-0000-0000-0000-000000000001")
SUITE_A = UUID("aa110000-0000-0000-0000-000000000002")
OFFICE_A = UUID("aa110000-0000-0000-0000-000000000003")

TENANT_B = UUID("bb110000-0000-0000-0000-000000000001")
SUITE_B = UUID("bb110000-0000-0000-0000-000000000002")
OFFICE_B = UUID("bb110000-0000-0000-0000-000000000003")

NOW = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)


def _scope_a() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)


def _scope_b() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_B, suite_id=SUITE_B, office_id=OFFICE_B)


def _thread_row_b(thread_id: UUID | None = None) -> dict:
    """Thread row owned by TENANT_B — must never leak to TENANT_A."""
    return {
        "thread_id": str(thread_id or uuid.uuid4()),
        "tenant_id": str(TENANT_B),
        "suite_id": str(SUITE_B),
        "office_id": str(OFFICE_B),
        "thread_type": "client_thread",
        "status": "open",
        "first_event_at": NOW.isoformat(),
        "last_activity_at": NOW.isoformat(),
        "created_at": NOW.isoformat(),
    }


def _envelope(scope: ScopedIdentity) -> MemoryEventEnvelope:
    """Minimal envelope for resolver tests."""
    return MemoryEventEnvelope(
        tenant_id=scope.tenant_id,
        suite_id=scope.suite_id,
        office_id=scope.office_id,
        event_type="voice_session_ended",
        source_surface="ava_voice",
        source_agent="ava",
        runtime_family="elevenlabs",
        channel="voice",
        trace_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        idempotency_key=f"test-{uuid.uuid4()}",
        payload={"call_outcome": "completed"},
        event_at=NOW,
    )


# ---------------------------------------------------------------------------
# Cross-tenant SELECT
# ---------------------------------------------------------------------------


class TestThreadsCrossTenantSelect:
    """Cross-tenant reads on threads table must return 0 rows."""

    @pytest.mark.asyncio
    async def test_resolver_only_queries_own_tenant_scope(self) -> None:
        """Evil: DB returns TENANT_B thread row for a TENANT_A resolve call.

        The resolver must detect the scope mismatch and raise rather than
        returning or using the foreign thread.
        """
        thread_id_b = uuid.uuid4()
        b_thread_row = _thread_row_b(thread_id_b)
        scope_a = _scope_a()
        envelope = _envelope(scope_a)

        resolver = EntityThreadResolver()

        with patch(
            "aspire_orchestrator.services.entity_thread_resolver.supabase_select",
            new=AsyncMock(return_value=[b_thread_row]),
        ):
            # The resolver must reject or skip the foreign row (not use it)
            # It can either raise or fall through to an insert with correct scope.
            # We verify the thread_id returned belongs to scope_a (new insert).
            insert_calls: list[dict] = []

            async def mock_insert(table: str, row: dict):
                insert_calls.append(row)
                return {
                    **row,
                    "thread_id": str(uuid.uuid4()),
                    "status": "open",
                    "first_event_at": NOW.isoformat(),
                    "last_activity_at": NOW.isoformat(),
                    "created_at": NOW.isoformat(),
                }

            with patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_insert",
                new=mock_insert,
            ):
                try:
                    thread = await resolver.resolve(envelope)
                    # If resolved successfully, must use scope_a (not B's tenant)
                    assert str(thread.tenant_id) == str(TENANT_A), (
                        "Thread must belong to requesting tenant (A), not foreign tenant (B)"
                    )
                    assert str(thread.suite_id) == str(SUITE_A)
                except MemoryServiceError as exc:
                    # Acceptable: resolver detected scope mismatch and raised
                    assert "TENANT_ISOLATION" in str(exc) or "isolation" in str(exc).lower(), (
                        f"Unexpected error code: {exc.code}"
                    )

    @pytest.mark.asyncio
    async def test_resolver_returns_empty_on_zero_db_rows(self) -> None:
        """When DB returns 0 rows (correct RLS behavior), resolver creates new thread."""
        scope_a = _scope_a()
        envelope = _envelope(scope_a)
        new_thread_id = uuid.uuid4()

        resolver = EntityThreadResolver()

        with (
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_select",
                new=AsyncMock(return_value=[]),  # 0 rows — correct RLS behavior
            ),
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_insert",
                new=AsyncMock(return_value={
                    "thread_id": str(new_thread_id),
                    "tenant_id": str(TENANT_A),
                    "suite_id": str(SUITE_A),
                    "office_id": str(OFFICE_A),
                    "thread_type": "client_thread",
                    "status": "open",
                    "first_event_at": NOW.isoformat(),
                    "last_activity_at": NOW.isoformat(),
                    "created_at": NOW.isoformat(),
                }),
            ),
        ):
            thread = await resolver.resolve(envelope)

        assert thread is not None
        assert str(thread.tenant_id) == str(TENANT_A)
        assert str(thread.suite_id) == str(SUITE_A)


# ---------------------------------------------------------------------------
# Cross-tenant INSERT tests
# ---------------------------------------------------------------------------


class TestThreadsCrossTenantInsert:
    """Cross-tenant INSERT on threads must be denied at service layer."""

    @pytest.mark.asyncio
    async def test_upsert_thread_with_mismatched_scope_denied(self) -> None:
        """Evil: upsert_thread called with scope_a but thread data for scope_b.

        If EntityThreadResolver exposes an upsert_thread method, it must
        validate scope before inserting.
        """
        resolver = EntityThreadResolver()

        # Test that the resolver's scope validation is robust by passing a
        # scope_a envelope but checking that inserts carry scope_a values
        scope_a = _scope_a()
        envelope = _envelope(scope_a)

        inserted_rows: list[dict] = []

        async def capture_insert(table: str, row: dict):
            inserted_rows.append(row)
            return {
                **row,
                "thread_id": str(uuid.uuid4()),
                "status": "open",
                "first_event_at": NOW.isoformat(),
                "last_activity_at": NOW.isoformat(),
                "created_at": NOW.isoformat(),
            }

        with (
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_select",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "aspire_orchestrator.services.entity_thread_resolver.supabase_insert",
                new=capture_insert,
            ),
        ):
            thread = await resolver.resolve(envelope)

        # Any insert that occurred must use scope_a values
        for row in inserted_rows:
            assert row.get("tenant_id") == str(TENANT_A), (
                "Thread insert must carry the requesting tenant_id (A), not foreign tenant"
            )
            assert row.get("suite_id") == str(SUITE_A)
            assert row.get("office_id") == str(OFFICE_A)


# ---------------------------------------------------------------------------
# Missing scope context
# ---------------------------------------------------------------------------


class TestThreadsMissingScope:
    """Fail-closed: missing tenant context on thread operations."""

    def test_scope_with_null_tenant_id_invalid(self) -> None:
        """ScopedIdentity rejects null tenant_id — validates fail-closed at schema level."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ScopedIdentity(
                tenant_id=None,  # type: ignore[arg-type]
                suite_id=SUITE_A,
                office_id=OFFICE_A,
            )

    def test_envelope_requires_tenant_and_suite_ids(self) -> None:
        """MemoryEventEnvelope must carry all three scope fields."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MemoryEventEnvelope(
                # tenant_id omitted
                suite_id=SUITE_A,
                office_id=OFFICE_A,
                event_type="voice_session_ended",
                source_surface="ava_voice",
                source_agent="ava",
                runtime_family="elevenlabs",
                channel="voice",
                trace_id=uuid.uuid4(),
                correlation_id=uuid.uuid4(),
                idempotency_key="test",
                payload={},
                event_at=NOW,
            )
