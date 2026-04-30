"""RLS Evil Tests — sms_messages + sms_thread memory isolation (Pass 14).

Law #6: Cross-tenant SELECT on sms_messages / sms_thread memory_objects must
return 0 rows. SMS threads contain customer PII (phone numbers, message bodies)
that must never leak across tenant boundaries.

Aspire Laws:
  Law #3: Fail Closed — unresolvable scope → deny, not fallback.
  Law #6: Zero cross-tenant leakage through SMS memory path.
  Law #9: SMS body text is PII — must not appear in cross-tenant reads.
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
from aspire_orchestrator.services.memory_service import MemoryService, MemoryServiceError
from aspire_orchestrator.services.ingestion.base import IngestionError
from aspire_orchestrator.services.ingestion.sms_ingestion import SMSIngestionAdapter

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
        channel="sms",
        trace_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
    )


def _sms_row(tenant_id: UUID, suite_id: UUID, office_id: UUID, body: str = "Secret SMS") -> dict:
    return {
        "memory_id": str(uuid.uuid4()),
        "tenant_id": str(tenant_id),
        "suite_id": str(suite_id),
        "office_id": str(office_id),
        "memory_type": "sms_thread",
        "title": "SMS from +15551234567",
        "summary": body[:140],
        "detail": {
            "direction": "inbound",
            "from": "+15551234567",
            "to": "+12125550198",
            "body": body,
            "message_sid": "SM" + uuid.uuid4().hex,
        },
        "idempotency_key": f"twilio-sms-inbound:SM{uuid.uuid4().hex}",
        "status": None,
        "entity_type": "phone_contact",
        "entity_id": None,
        "thread_id": None,
        "visibility_scope": "office",
        "confidence": None,
        "event_at": datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "provenance": {},
    }


class TestSMSCrossTenantSelect:
    """Cross-tenant SELECT: TENANT_A requesting TENANT_B SMS rows → 0 rows."""

    @pytest.mark.asyncio
    async def test_cross_tenant_sms_get_by_id_raises_isolation_error(self) -> None:
        """TENANT_A scope trying to read TENANT_B SMS → TENANT_ISOLATION_VIOLATION (fail closed)."""
        mem_id = uuid.uuid4()
        b_row = _sms_row(TENANT_B, SUITE_B, OFFICE_B, body="Confidential Tenant B SMS")
        b_row["memory_id"] = str(mem_id)

        mem_svc = MemoryService()
        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new=AsyncMock(return_value=[b_row]),
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await mem_svc.get(memory_id=mem_id, scope=_scope_a())

    @pytest.mark.asyncio
    async def test_cross_tenant_sms_list_by_thread_returns_empty(self) -> None:
        """list_by_thread for TENANT_A returns 0 SMS rows when DB (RLS) filters them.

        MemoryService.list_by_thread adds tenant_id filter to query. Mock returns []
        (simulating RLS filtering TENANT_B rows from TENANT_A context).
        """
        thread_id = uuid.uuid4()

        mem_svc = MemoryService()
        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            items, cursor = await mem_svc.list_by_thread(
                thread_id=thread_id,
                scope=_scope_a(),
                limit=20,
            )

        assert len(items) == 0
        assert cursor is None

    @pytest.mark.asyncio
    async def test_cross_tenant_sms_write_rejected_before_db(self) -> None:
        """SMS write with mismatched scope (envelope=B, caller=A) → isolation error."""
        mem_svc = MemoryService()

        envelope = MemoryObjectIn(
            scope=_scope_b(),
            provenance=_prov(),
            memory_type="sms_thread",
            title="SMS from +15559999999",
            summary="Cross-tenant SMS injection",
            detail={
                "direction": "inbound",
                "from": "+15559999999",
                "to": "+18885550199",
                "body": "This belongs to Tenant B",
                "message_sid": "SMevil001",
            },
            idempotency_key="twilio-sms-inbound:SMevil001",
        )

        mock_insert = AsyncMock()
        with patch(
            "aspire_orchestrator.services.memory_service.supabase_insert",
            new=mock_insert,
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                # envelope.scope = TENANT_B, but call scope = TENANT_A
                await mem_svc.write(envelope, scope=_scope_a(), embed=False)

        mock_insert.assert_not_called()


class TestSMSAdapterCrossTenantScopeResolution:
    """SMS adapter must resolve scope from DB phone_number lookup only."""

    @pytest.mark.asyncio
    async def test_adapter_unknown_number_never_defaults_to_any_tenant(self) -> None:
        """If DB returns 0 rows for a number, scope NEVER defaults to any tenant."""
        adapter = SMSIngestionAdapter()
        payload = {
            "MessageSid": "SMtest_evil",
            "From": "+15551234567",
            "To": "+10000000000",  # Number not registered anywhere
            "Body": "Inject me into any tenant",
            "NumMedia": "0",
        }

        with patch(
            "aspire_orchestrator.services.ingestion.sms_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await adapter.resolve_scope(payload)

        # Must be 404, not 200 with a default scope
        assert exc_info.value.status_code == 404
        assert "UNKNOWN_NUMBER" in exc_info.value.code

    @pytest.mark.asyncio
    async def test_sms_pii_body_not_in_idempotency_key(self) -> None:
        """Law #9: SMS body (PII) must not appear in the idempotency_key."""
        adapter = SMSIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        payload = {
            "MessageSid": "SMpii001",
            "From": "+15551234567",
            "To": "+12125550198",
            "Body": "My SSN is 123-45-6789",
            "NumMedia": "0",
        }
        envelope = await adapter.build_envelope(payload, scope=scope, thread=None)
        # idempotency_key must be keyed on MessageSid only, not body
        assert "SSN" not in envelope.idempotency_key
        assert "123-45-6789" not in envelope.idempotency_key
        assert "SMpii001" in envelope.idempotency_key
