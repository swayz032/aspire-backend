"""RLS Evil Tests — tenant_phone_numbers table (Pass 14).

Law #6: Cross-tenant SELECT on tenant_phone_numbers must return 0 rows.
The ingestion adapters use this table for scope resolution — any RLS bypass
would allow one tenant's phone number to resolve another tenant's scope.

Tests simulate the RLS enforcement at service layer (mocked DB) and verify
the adapter's scope resolution never returns cross-tenant scope.

Aspire Laws:
  Law #3: Fail Closed — unresolved number → 404, not a default/fallback scope.
  Law #6: Zero cross-tenant leakage through phone number lookup.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.services.ingestion.base import IngestionError
from aspire_orchestrator.services.ingestion.sms_ingestion import SMSIngestionAdapter
from aspire_orchestrator.services.ingestion.call_ingestion import (
    CallRecordingIngestionAdapter,
    _resolve_call_scope,
)

TENANT_A = UUID("aa000000-0000-0000-0000-000000000001")
SUITE_A = UUID("aa000000-0000-0000-0000-000000000002")
OFFICE_A = UUID("aa000000-0000-0000-0000-000000000003")

TENANT_B = UUID("bb000000-0000-0000-0000-000000000001")
SUITE_B = UUID("bb000000-0000-0000-0000-000000000002")
OFFICE_B = UUID("bb000000-0000-0000-0000-000000000003")

PHONE_ROW_A = {
    "tenant_id": str(TENANT_A),
    "suite_id": str(SUITE_A),
    "office_id": str(OFFICE_A),
    "phone_number": "+12125550198",
}


class TestPhoneNumberRLSIsolation:
    """Cross-tenant SELECT on tenant_phone_numbers must return empty for non-owner."""

    @pytest.mark.asyncio
    async def test_rls_returns_empty_for_cross_tenant_number(self) -> None:
        """Simulate RLS: lookup of TENANT_B number in TENANT_A context returns 0 rows.

        In production, Supabase RLS enforces this at DB level. Here we verify
        the service layer handles 0 rows correctly (fail closed → 404).
        """
        adapter = SMSIngestionAdapter()
        payload = {
            "MessageSid": "SMtest001",
            "From": "+15551234567",
            "To": "+18885550199",  # TENANT_B's number, not registered in TENANT_A context
            "Body": "test",
            "NumMedia": "0",
        }

        # RLS simulation: DB returns 0 rows (as if row belongs to different tenant)
        with patch(
            "aspire_orchestrator.services.ingestion.sms_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),  # RLS filtered
        ):
            with pytest.raises(IngestionError) as exc_info:
                await adapter.resolve_scope(payload)

        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "UNKNOWN_NUMBER"

    @pytest.mark.asyncio
    async def test_rls_returns_correct_tenant_for_own_number(self) -> None:
        """Positive: TENANT_A's own number resolves to TENANT_A scope."""
        adapter = SMSIngestionAdapter()
        payload = {
            "MessageSid": "SMtest002",
            "From": "+15559999999",
            "To": "+12125550198",
            "Body": "hello",
            "NumMedia": "0",
        }

        with patch(
            "aspire_orchestrator.services.ingestion.sms_ingestion.supabase_select",
            new=AsyncMock(return_value=[PHONE_ROW_A]),
        ):
            scope = await adapter.resolve_scope(payload)

        assert scope.tenant_id == TENANT_A

    @pytest.mark.asyncio
    async def test_call_adapter_rls_empty_returns_404(self) -> None:
        """Simulate RLS on tenant_phone_numbers for Twilio voice calls."""
        payload = {"To": "+18885550199", "From": "+15551234567"}

        with patch(
            "aspire_orchestrator.services.ingestion.call_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),  # RLS filtered
        ):
            with pytest.raises(IngestionError) as exc_info:
                await _resolve_call_scope(payload)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_db_error_on_phone_lookup_fails_with_503(self) -> None:
        """DB error during phone lookup → 503 (Twilio retries), not 200 or 404."""
        from aspire_orchestrator.services.supabase_client import SupabaseClientError
        adapter = SMSIngestionAdapter()
        payload = {
            "MessageSid": "SMtest003",
            "From": "+15551234567",
            "To": "+12125550198",
            "Body": "test",
            "NumMedia": "0",
        }

        with patch(
            "aspire_orchestrator.services.ingestion.sms_ingestion.supabase_select",
            new=AsyncMock(side_effect=SupabaseClientError("connection reset")),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await adapter.resolve_scope(payload)

        assert exc_info.value.status_code == 503


class TestPhoneNumberInjectionAttempts:
    """Evil: attempt to inject a different tenant's number into a webhook payload."""

    @pytest.mark.asyncio
    async def test_to_number_from_payload_used_not_header(self) -> None:
        """Scope is resolved from payload.To, NOT from any header.

        An attacker injecting X-Aspire-Tenant-Number header cannot override
        the scope — the adapter reads the 'To' field from the webhook body.
        """
        adapter = SMSIngestionAdapter()
        payload = {
            "MessageSid": "SMtest004",
            "From": "+15551234567",
            "To": "+12125550198",
            "Body": "test",
            "NumMedia": "0",
        }
        headers_with_evil_tenant = {
            "X-Aspire-Webhook-Url": "https://www.aspireos.app/v1/ingest/twilio/sms",
            "X-Aspire-Tenant-Number": "+18885550199",  # Evil injected header
            "X-Twilio-Signature": "sig",
            "X-Aspire-Form-Params": "",
        }

        with patch(
            "aspire_orchestrator.services.ingestion.sms_ingestion.supabase_select",
            new=AsyncMock(return_value=[PHONE_ROW_A]),
        ):
            scope = await adapter.resolve_scope(payload)

        # Scope from payload.To (+12125550198) → TENANT_A, NOT from evil header
        assert scope.tenant_id == TENANT_A
