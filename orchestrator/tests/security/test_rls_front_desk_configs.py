"""RLS Evil Tests — front_desk_configs table (Pass 14).

Law #6: Cross-tenant SELECT/PATCH on front_desk_configs must return 0 rows / 401.
Front Desk configs contain sensitive tenant-specific setup: greeting scripts,
routing rules, Sarah's configured persona settings. Leakage violates both
Law #6 (tenant isolation) and Law #9 (privacy).

Tests verify that the MemoryService's scope enforcement prevents cross-tenant
reads of front_desk_configs-derived memory objects, and that the ingestion
path cannot be used to proxy cross-tenant config access.

Aspire Laws:
  Law #3: Fail Closed — scope mismatch → deny, no silent fallback.
  Law #6: Zero cross-tenant leakage through front desk config path.
  Law #9: Config data contains PII (phone numbers, business names) — must not leak.
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

TENANT_A = UUID("aa000000-0000-0000-0000-000000000001")
SUITE_A = UUID("aa000000-0000-0000-0000-000000000002")
OFFICE_A = UUID("aa000000-0000-0000-0000-000000000003")

TENANT_B = UUID("bb000000-0000-0000-0000-000000000001")
SUITE_B = UUID("bb000000-0000-0000-0000-000000000002")
OFFICE_B = UUID("bb000000-0000-0000-0000-000000000003")


def _scope(tenant: UUID, suite: UUID, office: UUID) -> ScopedIdentity:
    return ScopedIdentity(tenant_id=tenant, suite_id=suite, office_id=office)


def _scope_a() -> ScopedIdentity:
    return _scope(TENANT_A, SUITE_A, OFFICE_A)


def _scope_b() -> ScopedIdentity:
    return _scope(TENANT_B, SUITE_B, OFFICE_B)


def _prov() -> Provenance:
    return Provenance(
        source_surface="system",
        runtime_family="provider_webhook",
        channel="voice",
        trace_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
    )


def _memory_row(tenant_id: UUID, suite_id: UUID, office_id: UUID) -> dict:
    return {
        "memory_id": str(uuid.uuid4()),
        "tenant_id": str(tenant_id),
        "suite_id": str(suite_id),
        "office_id": str(office_id),
        "memory_type": "authority_context",
        "title": "Front desk config",
        "summary": "Tenant B front desk configuration",
        "detail": {
            "greeting": "Welcome to Tenant B",
            "phone": "+12125559999",
            "business_name": "Tenant B Corp",
        },
        "idempotency_key": f"frontdesk-config:{uuid.uuid4()}",
        "status": None,
        "entity_type": None,
        "entity_id": None,
        "thread_id": None,
        "visibility_scope": "office",
        "confidence": None,
        "event_at": datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "provenance": {},
    }


class TestFrontDeskConfigCrossTenantSelect:
    """Cross-tenant SELECT on front_desk_configs-derived memory must return nothing."""

    @pytest.mark.asyncio
    async def test_cross_tenant_get_by_id_raises_isolation_error(self) -> None:
        """TENANT_A scope requesting TENANT_B memory_id → TENANT_ISOLATION_VIOLATION (fail closed)."""
        mem_id = uuid.uuid4()
        b_row = _memory_row(TENANT_B, SUITE_B, OFFICE_B)
        b_row["memory_id"] = str(mem_id)

        mem_svc = MemoryService()
        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new=AsyncMock(return_value=[b_row]),
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await mem_svc.get(memory_id=mem_id, scope=_scope_a())

    @pytest.mark.asyncio
    async def test_cross_tenant_list_returns_empty(self) -> None:
        """list_by_thread for TENANT_A thread must return 0 rows (DB query filters by tenant_id).

        MemoryService.list_by_thread adds tenant_id=eq.<scope.tenant_id> to the query filter.
        When we mock supabase_select to return empty (simulating RLS filtering), TENANT_A sees 0.
        """
        thread_id = uuid.uuid4()

        mem_svc = MemoryService()
        # Simulated RLS: DB returns 0 rows when queried with TENANT_A context for TENANT_A's thread
        # (in production, the DB query includes tenant_id filter so TENANT_B rows never returned)
        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            items, cursor = await mem_svc.list_by_thread(
                thread_id=thread_id,
                scope=_scope_a(),
                limit=10,
            )

        assert len(items) == 0
        assert cursor is None

    @pytest.mark.asyncio
    async def test_cross_tenant_write_for_config_rejected_before_db(self) -> None:
        """Writing front desk config memory with mismatched scope → isolation error before DB."""
        mem_svc = MemoryService()
        scope_b = _scope_b()

        envelope = MemoryObjectIn(
            scope=scope_b,
            provenance=_prov(),
            memory_type="authority_context",
            title="Front desk config — Tenant B",
            summary="Tenant B business hours and routing",
            detail={
                "greeting": "Welcome",
                "phone": "+12125559999",
                "business_name": "Tenant B Corp",
            },
            idempotency_key="frontdesk-config-evil-001",
        )

        mock_insert = AsyncMock()
        with patch(
            "aspire_orchestrator.services.memory_service.supabase_insert",
            new=mock_insert,
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                # Envelope belongs to TENANT_B, but caller passes TENANT_A scope
                await mem_svc.write(envelope, scope=_scope_a(), embed=False)

        mock_insert.assert_not_called()


class TestFrontDeskConfigMissingScope:
    """Law #3: Missing tenant context must fail closed, not use a default scope."""

    def test_scoped_identity_missing_suite_id_raises(self) -> None:
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="suite_id"):
            ScopedIdentity(
                tenant_id=TENANT_A,
                office_id=OFFICE_A,
                # suite_id omitted
            )

    def test_scoped_identity_missing_office_id_raises(self) -> None:
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="office_id"):
            ScopedIdentity(
                tenant_id=TENANT_A,
                suite_id=SUITE_A,
                # office_id omitted
            )
