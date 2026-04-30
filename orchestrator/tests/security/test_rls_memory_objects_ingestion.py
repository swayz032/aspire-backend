"""RLS Evil Tests — memory_objects ingestion cross-tenant isolation (Pass 14).

Law #6: Zero cross-tenant leakage. Tests that the ingestion adapters CANNOT
write memory_objects to a foreign tenant even if the payload claims to belong
to that tenant. The scope resolution path is the enforcement gate.

Tests use mocked supabase_select to simulate:
  - DB returning rows for TENANT_B when adapter was invoked with TENANT_A context
  - Adapter resolving scope from payload (not from caller identity)
  - MemoryService rejecting cross-tenant writes via scope assertion

Aspire Laws:
  Law #3: Fail Closed — scope mismatch must raise, not silently continue.
  Law #6: Tenant Isolation — zero cross-tenant writes through ingestion path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    Provenance,
    ScopedIdentity,
)
from aspire_orchestrator.services.ingestion.base import IngestionError
from aspire_orchestrator.services.ingestion.sms_ingestion import SMSIngestionAdapter
from aspire_orchestrator.services.ingestion.invoice_ingestion import InvoiceIngestionAdapter
from aspire_orchestrator.services.ingestion.zoom_ingestion import ZoomRecordingIngestionAdapter
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


def _scope_a() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)


def _scope_b() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_B, suite_id=SUITE_B, office_id=OFFICE_B)


def _prov() -> Provenance:
    return Provenance(
        source_surface="system",
        runtime_family="provider_webhook",
        channel="voice",
        trace_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# Cross-tenant write via MemoryService (core isolation gate)
# ---------------------------------------------------------------------------


class TestCrossTenantIngestWrite:
    """Evil: adapter builds envelope with SCOPE_A but MemoryService called with SCOPE_B."""

    @pytest.mark.asyncio
    async def test_envelope_scope_a_called_with_scope_b_raises_isolation_error(self) -> None:
        """MemoryService.write must reject when envelope.scope != call scope."""
        mem_svc = MemoryService()

        envelope = MemoryObjectIn(
            scope=_scope_a(),
            provenance=_prov(),
            memory_type="sms_thread",
            title="Evil SMS",
            summary="Cross-tenant injection attempt",
            detail={"from": "+15551234567", "to": "+12125550198", "body": "test"},
            idempotency_key="evil-cross-tenant-key-001",
        )

        # Simulate DB returning a TENANT_A row (correct) but caller passes SCOPE_B
        # MemoryService should assert scope match and raise before DB write.
        mock_insert = AsyncMock()
        with patch(
            "aspire_orchestrator.services.memory_service.supabase_insert",
            new=mock_insert,
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                # envelope.scope = TENANT_A, but we pass scope = TENANT_B
                await mem_svc.write(envelope, scope=_scope_b(), embed=False)

        mock_insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_scope_write_proceeds_to_db(self) -> None:
        """Positive test: envelope scope matches call scope → DB write allowed (insert called)."""
        mem_svc = MemoryService()
        scope = _scope_a()
        prov = _prov()

        envelope = MemoryObjectIn(
            scope=scope,
            provenance=prov,
            memory_type="sms_thread",
            title="Valid SMS",
            summary="Legit write",
            detail={"from": "+15551234567", "to": "+12125550198", "body": "hello"},
            idempotency_key="valid-write-key-001",
        )

        fake_db_row = {
            "memory_id": str(uuid.uuid4()),
            "tenant_id": str(TENANT_A),
            "suite_id": str(SUITE_A),
            "office_id": str(OFFICE_A),
            "actor_id": None,
            "user_id": None,
            "memory_type": "sms_thread",
            "title": "Valid SMS",
            "summary": "Legit write",
            "detail": {},
            "idempotency_key": "valid-write-key-001",
            "status": None,
            "entity_type": None,
            "entity_id": None,
            "thread_id": None,
            "visibility_scope": "office",
            "confidence": None,
            "event_at": datetime.now(timezone.utc).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            # _row_to_memory_out requires these flat provenance fields:
            "source_surface": "system",
            "source_agent": None,
            "runtime_family": "provider_webhook",
            "channel": "voice",
            "session_provider": None,
            "transcript_provider": None,
            "recording_provider": None,
            "external_session_id": None,
            "source_record_id": None,
            "trace_id": str(prov.trace_id),
            "correlation_id": str(prov.correlation_id),
            "artifact_origin": None,
            "summary_origin": None,
            "schema_version": "v1",
            "last_activity_at": datetime.now(timezone.utc).isoformat(),
        }

        mock_insert = AsyncMock(return_value=fake_db_row)
        with patch(
            "aspire_orchestrator.services.memory_service.supabase_insert",
            new=mock_insert,
        ), patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            result = await mem_svc.write(envelope, scope=scope, embed=False)

        mock_insert.assert_called_once()
        assert result.scope.tenant_id == TENANT_A


# ---------------------------------------------------------------------------
# Cross-tenant scope resolution in SMS adapter
# ---------------------------------------------------------------------------


class TestSMSAdapterCrossTenantGuard:
    """Evil: SMS adapter's resolve_scope must never return TENANT_B rows for TENANT_A's number."""

    @pytest.mark.asyncio
    async def test_scope_resolution_returns_db_row_tenant_not_caller_tenant(self) -> None:
        """Adapter resolves scope from DB, not from caller. Verify TENANT_B row
        is returned from DB and MemoryService rejects the write."""
        adapter = SMSIngestionAdapter()

        # DB lookup for "+12125550198" returns TENANT_B row (simulates misconfiguration)
        phone_row_b = {
            "tenant_id": str(TENANT_B),
            "suite_id": str(SUITE_B),
            "office_id": str(OFFICE_B),
            "phone_number": "+12125550198",
        }

        payload = {
            "MessageSid": "SMevil001",
            "From": "+15559999999",
            "To": "+12125550198",
            "Body": "Cross-tenant inject",
            "NumMedia": "0",
        }

        with patch(
            "aspire_orchestrator.services.ingestion.sms_ingestion.supabase_select",
            new=AsyncMock(return_value=[phone_row_b]),
        ):
            scope = await adapter.resolve_scope(payload)

        # Scope should be TENANT_B (from DB row)
        assert scope.tenant_id == TENANT_B

        # Now if MemoryService is called with TENANT_A scope while envelope has TENANT_B
        # it must be rejected
        mem_svc = MemoryService()
        envelope = await adapter.build_envelope(payload, scope=scope, thread=None)

        mock_insert = AsyncMock()
        with patch(
            "aspire_orchestrator.services.memory_service.supabase_insert",
            new=mock_insert,
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                # Caller passes TENANT_A scope but envelope declares TENANT_B
                await mem_svc.write(envelope, scope=_scope_a(), embed=False)

        mock_insert.assert_not_called()


# ---------------------------------------------------------------------------
# Cross-tenant scope resolution in Invoice adapter
# ---------------------------------------------------------------------------


class TestInvoiceAdapterCrossTenantGuard:

    @pytest.mark.asyncio
    async def test_invoice_cross_tenant_write_rejected(self) -> None:
        """Stripe invoice for TENANT_B customer cannot be written to TENANT_A memory."""
        import time
        adapter = InvoiceIngestionAdapter()
        scope_b = _scope_b()

        invoice_payload = {
            "id": "evt_evil_001",
            "type": "invoice.created",
            "created": int(time.time()),
            "data": {
                "object": {
                    "id": "in_evil001",
                    "number": "INV-EVIL",
                    "customer": "cus_tenant_b",
                    "customer_name": "Evil Corp",
                    "amount_due": 9999,
                    "total": 9999,
                    "lines": {"data": []},
                }
            },
        }

        envelope = await adapter.build_envelope(invoice_payload, scope=scope_b, thread=None)

        mem_svc = MemoryService()
        mock_insert = AsyncMock()

        with patch(
            "aspire_orchestrator.services.memory_service.supabase_insert",
            new=mock_insert,
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                # Envelope scope = TENANT_B, call scope = TENANT_A
                await mem_svc.write(envelope, scope=_scope_a(), embed=False)

        mock_insert.assert_not_called()


# ---------------------------------------------------------------------------
# Missing tenant context
# ---------------------------------------------------------------------------


class TestMissingTenantContext:
    """Law #3: Missing scope context must fail closed."""

    @pytest.mark.asyncio
    async def test_sms_resolve_scope_missing_to_number_fails_closed(self) -> None:
        """SMS payload with no 'To' field → MISSING_TO_NUMBER, not silent pass."""
        adapter = SMSIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.resolve_scope({"From": "+15551234567", "Body": "test"})
        assert exc_info.value.code == "MISSING_TO_NUMBER"
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_zoom_resolve_scope_missing_account_id_fails_closed(self) -> None:
        """Zoom payload with no account_id → MISSING_ACCOUNT_ID."""
        adapter = ZoomRecordingIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.resolve_scope({"payload": {"object": {}}})
        assert exc_info.value.status_code == 422

    def test_scoped_identity_rejects_none_tenant_id(self) -> None:
        """ScopedIdentity schema enforces tenant_id is not None."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ScopedIdentity(
                tenant_id=None,  # type: ignore[arg-type]
                suite_id=SUITE_A,
                office_id=OFFICE_A,
            )
