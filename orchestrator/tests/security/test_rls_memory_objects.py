"""RLS Evil Tests — memory_objects table (Pass 11).

Law #6: Zero cross-tenant leakage. Every test verifies that tenant isolation
is enforced at the service layer (defense-in-depth) AND simulates what RLS
policies enforce at the DB layer.

Tests use the MemoryService's own scope validation and mocked DB responses
to ensure the service NEVER returns cross-tenant data — even if the DB
were to erroneously include it (belt-and-suspenders).

RLS simulation approach:
  - Mock supabase_select to return rows scoped to TENANT_B
  - Assert that MemoryService._assert_scope_match raises before returning the row
  - Verify DB is never called on cross-tenant writes (scope check is pre-DB)

For INSERT/UPDATE tests:
  - Pass scope_b as caller scope with an envelope declaring scope_a
  - Assert MemoryServiceError(code='TENANT_ISOLATION_VIOLATION') raised
  - Assert supabase_insert/update never called

Aspire Laws:
  Law #3: Fail Closed — any scope mismatch → deny, emit reason.
  Law #6: Tenant Isolation — zero cross-tenant reads/writes at every layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    MemoryObjectOut,
    Provenance,
    ScopedIdentity,
)
from aspire_orchestrator.services.memory_service import MemoryService, MemoryServiceError

# ---------------------------------------------------------------------------
# Tenant fixtures
# ---------------------------------------------------------------------------

TENANT_A = UUID("aa000000-0000-0000-0000-000000000001")
SUITE_A = UUID("aa000000-0000-0000-0000-000000000002")
OFFICE_A = UUID("aa000000-0000-0000-0000-000000000003")

TENANT_B = UUID("bb000000-0000-0000-0000-000000000001")
SUITE_B = UUID("bb000000-0000-0000-0000-000000000002")
OFFICE_B = UUID("bb000000-0000-0000-0000-000000000003")

NOW = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)


def _scope(tenant: UUID, suite: UUID, office: UUID) -> ScopedIdentity:
    return ScopedIdentity(tenant_id=tenant, suite_id=suite, office_id=office)


def _scope_a() -> ScopedIdentity:
    return _scope(TENANT_A, SUITE_A, OFFICE_A)


def _scope_b() -> ScopedIdentity:
    return _scope(TENANT_B, SUITE_B, OFFICE_B)


def _prov_a() -> Provenance:
    return Provenance(
        source_surface="ava_voice",
        source_agent="ava",
        runtime_family="elevenlabs",
        channel="voice",
        trace_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
    )


def _memory_row_b(memory_id: UUID | None = None) -> dict:
    """DB row belonging to TENANT_B — should NEVER be returned to TENANT_A."""
    return {
        "memory_id": str(memory_id or uuid.uuid4()),
        "tenant_id": str(TENANT_B),
        "suite_id": str(SUITE_B),
        "office_id": str(OFFICE_B),
        "trace_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "memory_type": "session_summary",
        "summary": "Tenant B private session notes.",
        "created_at": NOW.isoformat(),
        "last_activity_at": NOW.isoformat(),
    }


# ---------------------------------------------------------------------------
# Cross-tenant SELECT tests
# ---------------------------------------------------------------------------


class TestCrossTenantSelect:
    """Law #6: cross-tenant SELECT must return 0 rows (never tenant B's data to A)."""

    @pytest.mark.asyncio
    async def test_cross_tenant_get_by_id_returns_none(self) -> None:
        """Evil: Tenant A requests memory_id owned by Tenant B → None (not a row)."""
        b_memory_id = uuid.uuid4()
        b_row = _memory_row_b(b_memory_id)
        mem_svc = MemoryService()
        scope_a = _scope_a()

        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new=AsyncMock(return_value=[b_row]),  # DB erroneously returns B's row
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await mem_svc.get(b_memory_id, scope=scope_a)

    @pytest.mark.asyncio
    async def test_cross_tenant_list_by_thread_filters_out_foreign_rows(self) -> None:
        """Evil: list_by_thread for Thread-A returns rows owned by B → raises isolation error."""
        thread_id = uuid.uuid4()
        b_rows = [_memory_row_b() for _ in range(3)]
        mem_svc = MemoryService()
        scope_a = _scope_a()

        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new=AsyncMock(return_value=b_rows),  # DB erroneously returns B's rows
        ):
            # First row's scope mismatch will raise before returning the page
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await mem_svc.list_by_thread(
                    thread_id=thread_id,
                    scope=scope_a,
                    limit=10,
                )

    @pytest.mark.asyncio
    async def test_cross_tenant_list_by_entity_raises_isolation_error(self) -> None:
        """Evil: list_by_entity for Entity-A returns B's rows → isolation error."""
        entity_id = uuid.uuid4()
        b_rows = [_memory_row_b() for _ in range(2)]
        mem_svc = MemoryService()
        scope_a = _scope_a()

        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new=AsyncMock(return_value=b_rows),
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await mem_svc.list_by_entity(
                    entity_type="caller",
                    entity_id=entity_id,
                    scope=scope_a,
                    limit=10,
                )


# ---------------------------------------------------------------------------
# Cross-tenant INSERT tests
# ---------------------------------------------------------------------------


class TestCrossTenantInsert:
    """Law #6: cross-tenant INSERT must fail BEFORE any DB I/O."""

    @pytest.mark.asyncio
    async def test_cross_tenant_write_raises_before_db_insert(self) -> None:
        """Evil: Tenant B actor attempts to INSERT a memory_object into Tenant A scope.

        The envelope declares scope_a. The caller provides scope_b.
        MemoryService._assert_scope_match must deny before calling supabase_insert.
        """
        scope_a = _scope_a()
        scope_b = _scope_b()

        # Envelope belongs to A, but caller supplies B's scope
        envelope = MemoryObjectIn(
            scope=scope_a,  # tenant A's scope in the envelope
            provenance=_prov_a(),
            memory_type="session_summary",
            summary="Cross-tenant write injection attempt.",
            idempotency_key=f"evil-{uuid.uuid4()}",
        )

        mock_insert = AsyncMock()
        mem_svc = MemoryService()

        with patch(
            "aspire_orchestrator.services.memory_service.supabase_insert",
            new=mock_insert,
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await mem_svc.write(envelope, scope=scope_b, embed=False)

        # Critical: DB must NOT be called
        mock_insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_reversed_cross_tenant_write_also_denied(self) -> None:
        """Evil: Tenant A actor with envelope declaring scope_b — also denied."""
        scope_a = _scope_a()
        scope_b = _scope_b()

        envelope = MemoryObjectIn(
            scope=scope_b,  # tenant B's scope in the envelope
            provenance=Provenance(
                source_surface="ava_voice",
                source_agent="ava",
                runtime_family="elevenlabs",
                channel="voice",
                trace_id=uuid.uuid4(),
                correlation_id=uuid.uuid4(),
            ),
            memory_type="session_summary",
            summary="Reversed cross-tenant injection.",
            idempotency_key=f"evil-rev-{uuid.uuid4()}",
        )

        mock_insert = AsyncMock()
        mem_svc = MemoryService()

        with patch(
            "aspire_orchestrator.services.memory_service.supabase_insert",
            new=mock_insert,
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await mem_svc.write(envelope, scope=scope_a, embed=False)

        mock_insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_partial_scope_mismatch_denied(self) -> None:
        """Evil: tenant_id matches but suite_id differs — still denied."""
        scope_correct = _scope_a()
        scope_wrong_suite = ScopedIdentity(
            tenant_id=TENANT_A,
            suite_id=SUITE_B,  # wrong suite
            office_id=OFFICE_A,
        )

        envelope = MemoryObjectIn(
            scope=scope_correct,
            provenance=_prov_a(),
            memory_type="session_summary",
            summary="Partial scope mismatch test.",
            idempotency_key=f"evil-partial-{uuid.uuid4()}",
        )

        mock_insert = AsyncMock()
        mem_svc = MemoryService()

        with patch(
            "aspire_orchestrator.services.memory_service.supabase_insert",
            new=mock_insert,
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await mem_svc.write(envelope, scope=scope_wrong_suite, embed=False)

        mock_insert.assert_not_called()


# ---------------------------------------------------------------------------
# UPDATE immutability tests
# ---------------------------------------------------------------------------


class TestImmutabilityViolation:
    """Law #2: Receipts and terminal memory_objects are append-only — no update/delete."""

    @pytest.mark.asyncio
    async def test_update_status_on_executed_row_raises(self) -> None:
        """Evil: attempt to update memory_object with status='executed' → denied.

        The MemoryService pre-checks _TERMINAL_STATUS before hitting the DB.
        """
        memory_id = uuid.uuid4()
        scope_a = _scope_a()

        executed_row = {
            "memory_id": str(memory_id),
            "tenant_id": str(TENANT_A),
            "suite_id": str(SUITE_A),
            "office_id": str(OFFICE_A),
            "trace_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "memory_type": "pending_intent",
            "summary": "Already executed intent.",
            "status": "executed",  # terminal state
            "created_at": NOW.isoformat(),
            "last_activity_at": NOW.isoformat(),
        }

        mock_update = AsyncMock()
        mem_svc = MemoryService()

        with (
            patch(
                "aspire_orchestrator.services.memory_service.supabase_select",
                new=AsyncMock(return_value=[executed_row]),
            ),
            patch(
                "aspire_orchestrator.services.memory_service.supabase_update",
                new=mock_update,
            ),
        ):
            with pytest.raises(MemoryServiceError, match="IMMUTABLE_STATE_TRANSITION"):
                await mem_svc.update_status(
                    memory_id=memory_id,
                    new_status="approved",
                    scope=scope_a,
                )

        # DB update must NOT be called on terminal rows
        mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_status_cross_tenant_denied_before_db(self) -> None:
        """Evil: update_status for TENANT_B row using TENANT_A scope → isolation error."""
        memory_id = uuid.uuid4()
        scope_a = _scope_a()
        b_row = _memory_row_b(memory_id)
        b_row["status"] = "drafted"

        mock_update = AsyncMock()
        mem_svc = MemoryService()

        with (
            patch(
                "aspire_orchestrator.services.memory_service.supabase_select",
                new=AsyncMock(return_value=[b_row]),
            ),
            patch(
                "aspire_orchestrator.services.memory_service.supabase_update",
                new=mock_update,
            ),
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await mem_svc.update_status(
                    memory_id=memory_id,
                    new_status="approved",
                    scope=scope_a,
                )

        mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# Missing scope / missing context tests
# ---------------------------------------------------------------------------


class TestMissingScope:
    """Law #3: Fail Closed — missing required scope fields → denied."""

    def test_scoped_identity_requires_all_three_fields(self) -> None:
        """Missing office_id → ValidationError (schema enforces all three)."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="office_id"):
            ScopedIdentity(
                tenant_id=TENANT_A,
                suite_id=SUITE_A,
                # office_id omitted
            )

    def test_scoped_identity_rejects_null_tenant_id(self) -> None:
        """Null tenant_id → ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ScopedIdentity(
                tenant_id=None,  # type: ignore[arg-type]
                suite_id=SUITE_A,
                office_id=OFFICE_A,
            )

    @pytest.mark.asyncio
    async def test_empty_rows_returned_for_valid_empty_scope(self) -> None:
        """When DB returns 0 rows for a valid scope, service returns empty — no error."""
        thread_id = uuid.uuid4()
        scope_a = _scope_a()
        mem_svc = MemoryService()

        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            items, cursor = await mem_svc.list_by_thread(
                thread_id=thread_id,
                scope=scope_a,
                limit=10,
            )

        assert items == []
        assert cursor is None
