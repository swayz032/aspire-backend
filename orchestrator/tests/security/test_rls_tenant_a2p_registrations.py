"""RLS Evil Tests — tenant_a2p_registrations table (Pass 19 Lane B).

Law #6: Cross-tenant SELECT on tenant_a2p_registrations must return 0 rows.
The SMS gate reads this table to check A2P status; any RLS bypass would allow
one tenant's unregistered status to be ignored by another tenant's SMS send.

Tests simulate RLS enforcement at service layer (mocked DB) and verify the
SMS gate never allows a send based on another tenant's registration status.

Aspire Laws:
  Law #3: Fail Closed — missing row or cross-tenant = deny SMS.
  Law #6: Zero cross-tenant leakage through A2P status lookup.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-ci")

from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services.sms_io import SmsIoError, send_sms

# ---------------------------------------------------------------------------
# Tenant A and B constants
# ---------------------------------------------------------------------------

TENANT_A = uuid.UUID("aa000000-0000-0000-0000-000000000001")
SUITE_A = uuid.UUID("aa000000-0000-0000-0000-000000000002")
OFFICE_A = uuid.UUID("aa000000-0000-0000-0000-000000000003")

TENANT_B = uuid.UUID("bb000000-0000-0000-0000-000000000001")
SUITE_B = uuid.UUID("bb000000-0000-0000-0000-000000000002")
OFFICE_B = uuid.UUID("bb000000-0000-0000-0000-000000000003")

_SCOPE_A = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
_SCOPE_B = ScopedIdentity(tenant_id=TENANT_B, suite_id=SUITE_B, office_id=OFFICE_B)

_REGISTERED_A = {
    "id": str(uuid.uuid4()),
    "tenant_id": str(TENANT_A),
    "status": "registered",
    "registered_at": datetime.now(timezone.utc).isoformat(),
}

_REGISTERED_B = {
    "id": str(uuid.uuid4()),
    "tenant_id": str(TENANT_B),
    "status": "registered",
    "registered_at": datetime.now(timezone.utc).isoformat(),
}

_FROM_NUMBER_A = [
    {
        "phone_number": "+14484001111",
        "office_id": str(OFFICE_A),
        "suite_id": str(SUITE_A),
        "tenant_id": str(TENANT_A),
        "status": "active",
        "sms_enabled": True,
    }
]

_FROM_NUMBER_B = [
    {
        "phone_number": "+14484002222",
        "office_id": str(OFFICE_B),
        "suite_id": str(SUITE_B),
        "tenant_id": str(TENANT_B),
        "status": "active",
        "sms_enabled": True,
    }
]

_THREAD_A = [
    {
        "memory_id": "thread-a-001",
        "suite_id": str(SUITE_A),
        "office_id": str(OFFICE_A),
        "tenant_id": str(TENANT_A),
        "detail": {"from": "+19175550100", "to": "+14484001111"},
    }
]


class TestA2PRLSCrossTenantIsolation:
    """Cross-tenant A2P registration status must not bleed across tenant boundaries."""

    @pytest.mark.asyncio
    async def test_tenant_b_registered_does_not_allow_tenant_a_send(self) -> None:
        """CRITICAL: Tenant B is registered, Tenant A is not.
        A2P gate must still block Tenant A's send.
        RLS simulation: select on tenant_a2p_registrations returns B's row for A's context.
        This must NOT happen — gate reads by tenant_id from scope, not from payload.
        """
        # Simulate RLS bypass: A's request returns B's registered row (evil scenario)
        async def _evil_select(table: str, filters: str, **kwargs) -> list[dict[str, Any]]:
            if table == "tenant_phone_numbers":
                return _FROM_NUMBER_A
            if table == "memory_objects":
                return _THREAD_A
            if table == "tenant_a2p_registrations":
                # Evil: returns TENANT_B's registered row for TENANT_A's request
                return [_REGISTERED_B]
            return []

        # The gate must check that the returned row's tenant_id matches scope.tenant_id
        # If sms_io.send_sms is correctly implemented, it will reject this because
        # the row's tenant_id (TENANT_B) != scope.tenant_id (TENANT_A).
        with (
            patch(
                "aspire_orchestrator.services.sms_io._twilio_auth",
                return_value=("ACtest", "authtest"),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.supabase_select",
                new=AsyncMock(side_effect=_evil_select),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            # CORRECT behavior: service layer detects tenant_id mismatch in returned
            # row and treats it as no-row → blocks send with A2P_NOT_REGISTERED.
            with pytest.raises(SmsIoError) as exc_info:
                await send_sms(
                    thread_memory_id="thread-a-001",
                    body="Test cross-tenant A2P evil",
                    scope=_SCOPE_A,
                    capability_token="cap-tok-test",
                    idempotency_key=str(uuid.uuid4()),
                )
        assert exc_info.value.code == "A2P_NOT_REGISTERED"

    @pytest.mark.asyncio
    async def test_rls_empty_returns_blocked(self) -> None:
        """When RLS returns 0 rows for tenant_a2p_registrations, send must be blocked."""
        async def _select(table: str, filters: str, **kwargs) -> list[dict[str, Any]]:
            if table == "tenant_phone_numbers":
                return _FROM_NUMBER_A
            if table == "memory_objects":
                return _THREAD_A
            if table == "tenant_a2p_registrations":
                return []  # RLS filtered all rows (cross-tenant or unregistered)
            return []

        with (
            patch(
                "aspire_orchestrator.services.sms_io._twilio_auth",
                return_value=("ACtest", "authtest"),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.supabase_select",
                new=AsyncMock(side_effect=_select),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            with pytest.raises(SmsIoError) as exc_info:
                await send_sms(
                    thread_memory_id="thread-a-001",
                    body="Test RLS empty",
                    scope=_SCOPE_A,
                    capability_token="cap-tok-test",
                    idempotency_key=str(uuid.uuid4()),
                )
        assert exc_info.value.code == "A2P_NOT_REGISTERED"

    @pytest.mark.asyncio
    async def test_select_filter_contains_tenant_id_from_scope(self) -> None:
        """Service must build the supabase_select filter using scope.tenant_id
        (not any tenant_id from payload or other source).
        This ensures RLS is correctly leveraged at the service layer too.
        """
        captured_filters: list[str] = []

        async def _capture_select(table: str, filters: str, **kwargs) -> list[dict[str, Any]]:
            if table == "tenant_a2p_registrations":
                captured_filters.append(filters)
                return []
            if table == "tenant_phone_numbers":
                return _FROM_NUMBER_A
            if table == "memory_objects":
                return _THREAD_A
            return []

        with (
            patch(
                "aspire_orchestrator.services.sms_io._twilio_auth",
                return_value=("ACtest", "authtest"),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.supabase_select",
                new=AsyncMock(side_effect=_capture_select),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            with pytest.raises(SmsIoError):
                await send_sms(
                    thread_memory_id="thread-a-001",
                    body="Test filter",
                    scope=_SCOPE_A,
                    capability_token="cap-tok-test",
                    idempotency_key=str(uuid.uuid4()),
                )

        assert len(captured_filters) > 0, "supabase_select was never called for tenant_a2p_registrations"
        # The filter must contain scope.tenant_id
        assert str(TENANT_A) in captured_filters[0], (
            f"A2P status query filter '{captured_filters[0]}' does not contain "
            f"scope.tenant_id={TENANT_A}. RLS isolation may be bypassed."
        )

    @pytest.mark.asyncio
    async def test_own_registered_tenant_can_send(self) -> None:
        """Positive: own tenant registered → SMS send proceeds to Twilio call."""
        from unittest.mock import MagicMock

        mock_twilio_resp = MagicMock()
        mock_twilio_resp.status_code = 201
        mock_twilio_resp.json.return_value = {"sid": "SMtest-rls-ok", "status": "queued"}

        async def _select(table: str, filters: str, **kwargs) -> list[dict[str, Any]]:
            if table == "tenant_phone_numbers":
                return _FROM_NUMBER_A
            if table == "memory_objects":
                return _THREAD_A
            if table == "tenant_a2p_registrations":
                # Correct: own tenant's row
                return [_REGISTERED_A]
            return []

        with (
            patch(
                "aspire_orchestrator.services.sms_io._twilio_auth",
                return_value=("ACtest", "authtest"),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.supabase_select",
                new=AsyncMock(side_effect=_select),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.resilient_call",
                new=AsyncMock(return_value=mock_twilio_resp),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.supabase_insert",
                new=AsyncMock(return_value={"id": str(uuid.uuid4())}),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            result = await send_sms(
                thread_memory_id="thread-a-001",
                body="Registered send",
                scope=_SCOPE_A,
                capability_token="cap-tok-test",
                idempotency_key=str(uuid.uuid4()),
            )

        assert result["message_sid"] == "SMtest-rls-ok"
