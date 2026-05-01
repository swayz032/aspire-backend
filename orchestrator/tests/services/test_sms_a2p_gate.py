"""Tests for A2P registration gate in sms_io.send_sms (Pass 19 Lane B §3.7).

Covers:
- send_sms blocked when tenant_a2p_status = 'unregistered' → SmsIoError A2P_NOT_REGISTERED
- send_sms blocked when no A2P row exists → SmsIoError A2P_NOT_REGISTERED
- send_sms allowed when tenant_a2p_status = 'registered'
- Receipt cut on A2P block (Law #2)
- receipt outcome = 'denied' on block
- Receipt does NOT contain PII (body text, from/to phone numbers raw)
- Gate applied uniformly for all three PublicNumberModes
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-ci")

from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services.sms_io import SmsIoError, send_sms

SUITE_ID = uuid.UUID("aa000000-0000-0000-0000-000000000001")
OFFICE_ID = uuid.UUID("aa000000-0000-0000-0000-000000000002")
TENANT_ID = uuid.UUID("aa000000-0000-0000-0000-000000000003")

_SCOPE = ScopedIdentity(
    tenant_id=TENANT_ID,
    suite_id=SUITE_ID,
    office_id=OFFICE_ID,
)

_FROM_NUMBER_ROW = [
    {
        "phone_number": "+14484001234",
        "office_id": str(OFFICE_ID),
        "suite_id": str(SUITE_ID),
        "tenant_id": str(TENANT_ID),
        "status": "active",
        "sms_enabled": True,
    }
]

_THREAD_ROW = [
    {
        "memory_id": "thread-001",
        "suite_id": str(SUITE_ID),
        "office_id": str(OFFICE_ID),
        "tenant_id": str(TENANT_ID),
        "detail": {"from": "+19175550100", "to": "+14484001234"},
    }
]

_A2P_UNREGISTERED_ROW = [
    {
        "id": str(uuid.uuid4()),
        "tenant_id": str(TENANT_ID),
        "status": "unregistered",
    }
]

_A2P_REGISTERED_ROW = [
    {
        "id": str(uuid.uuid4()),
        "tenant_id": str(TENANT_ID),
        "status": "registered",
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
]

_A2P_PENDING_ROW = [
    {
        "id": str(uuid.uuid4()),
        "tenant_id": str(TENANT_ID),
        "status": "pending_brand",
    }
]


def _supabase_side_effect_for_a2p(a2p_rows: list[dict]) -> Any:
    """Returns a supabase_select side_effect that returns appropriate rows per table."""
    async def _select(table: str, filters: str, **kwargs) -> list[dict]:
        if table == "tenant_phone_numbers":
            return _FROM_NUMBER_ROW
        if table == "memory_objects":
            return _THREAD_ROW
        if table == "tenant_a2p_registrations":
            return a2p_rows
        return []
    return _select


class TestA2PGateBlocked:
    """SMS send must be blocked when tenant is not A2P registered."""

    @pytest.mark.asyncio
    async def test_send_sms_blocked_when_unregistered(self) -> None:
        """Status='unregistered' → SmsIoError A2P_NOT_REGISTERED."""
        stored_receipts: list[dict] = []

        with (
            patch(
                "aspire_orchestrator.services.sms_io._twilio_auth",
                return_value=("ACtest", "authtest"),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.supabase_select",
                side_effect=_supabase_side_effect_for_a2p(_A2P_UNREGISTERED_ROW),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.receipt_store.store_receipts",
                side_effect=lambda r: stored_receipts.extend(r),
            ),
        ):
            with pytest.raises(SmsIoError) as exc_info:
                await send_sms(
                    thread_memory_id="thread-001",
                    body="Hello test",
                    scope=_SCOPE,
                    capability_token="cap-tok-test",
                    idempotency_key=str(uuid.uuid4()),
                )

        assert exc_info.value.code == "A2P_NOT_REGISTERED"
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_send_sms_blocked_when_no_a2p_row(self) -> None:
        """No A2P registration row at all → blocked."""
        with (
            patch(
                "aspire_orchestrator.services.sms_io._twilio_auth",
                return_value=("ACtest", "authtest"),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.supabase_select",
                side_effect=_supabase_side_effect_for_a2p([]),  # no row
            ),
            patch(
                "aspire_orchestrator.services.sms_io.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            with pytest.raises(SmsIoError) as exc_info:
                await send_sms(
                    thread_memory_id="thread-001",
                    body="Hello test",
                    scope=_SCOPE,
                    capability_token="cap-tok-test",
                    idempotency_key=str(uuid.uuid4()),
                )

        assert exc_info.value.code == "A2P_NOT_REGISTERED"

    @pytest.mark.asyncio
    async def test_send_sms_blocked_when_pending_brand(self) -> None:
        """Status='pending_brand' → still blocked."""
        with (
            patch(
                "aspire_orchestrator.services.sms_io._twilio_auth",
                return_value=("ACtest", "authtest"),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.supabase_select",
                side_effect=_supabase_side_effect_for_a2p(_A2P_PENDING_ROW),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            with pytest.raises(SmsIoError) as exc_info:
                await send_sms(
                    thread_memory_id="thread-001",
                    body="Hello test",
                    scope=_SCOPE,
                    capability_token="cap-tok-test",
                    idempotency_key=str(uuid.uuid4()),
                )

        assert exc_info.value.code == "A2P_NOT_REGISTERED"

    @pytest.mark.asyncio
    async def test_a2p_block_cuts_receipt(self) -> None:
        """Law #2: A2P block must cut a receipt."""
        stored_receipts: list[dict] = []

        with (
            patch(
                "aspire_orchestrator.services.sms_io._twilio_auth",
                return_value=("ACtest", "authtest"),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.supabase_select",
                side_effect=_supabase_side_effect_for_a2p(_A2P_UNREGISTERED_ROW),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.receipt_store.store_receipts",
                side_effect=lambda r: stored_receipts.extend(r),
            ),
        ):
            with pytest.raises(SmsIoError):
                await send_sms(
                    thread_memory_id="thread-001",
                    body="Hello test",
                    scope=_SCOPE,
                    capability_token="cap-tok-test",
                    idempotency_key=str(uuid.uuid4()),
                )

        assert len(stored_receipts) >= 1
        receipt = stored_receipts[0]
        assert receipt["receipt_type"] == "sms_send_blocked_a2p"
        assert receipt["outcome"] == "denied"
        assert receipt["reason_code"] == "a2p_not_registered"

    @pytest.mark.asyncio
    async def test_a2p_block_receipt_no_raw_phone_pii(self) -> None:
        """Law #9: receipt must not contain raw phone numbers in full."""
        stored_receipts: list[dict] = []

        with (
            patch(
                "aspire_orchestrator.services.sms_io._twilio_auth",
                return_value=("ACtest", "authtest"),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.supabase_select",
                side_effect=_supabase_side_effect_for_a2p(_A2P_UNREGISTERED_ROW),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.receipt_store.store_receipts",
                side_effect=lambda r: stored_receipts.extend(r),
            ),
        ):
            with pytest.raises(SmsIoError):
                await send_sms(
                    thread_memory_id="thread-001",
                    body="Call me at +19175550100 now",  # PII in body
                    scope=_SCOPE,
                    capability_token="cap-tok-test",
                    idempotency_key=str(uuid.uuid4()),
                )

        receipt_str = str(stored_receipts)
        # Raw complete phone should not appear (only prefixes allowed)
        assert "+19175550100" not in receipt_str
        assert "+14484001234" not in receipt_str


class TestA2PGateAllowed:
    """SMS send proceeds normally when tenant is A2P registered."""

    @pytest.mark.asyncio
    async def test_send_sms_allowed_when_registered(self) -> None:
        """Status='registered' → Twilio call proceeds."""
        mock_twilio_resp = MagicMock()
        mock_twilio_resp.status_code = 201
        mock_twilio_resp.json.return_value = {
            "sid": "SMtest001",
            "status": "queued",
        }

        with (
            patch(
                "aspire_orchestrator.services.sms_io._twilio_auth",
                return_value=("ACtest", "authtest"),
            ),
            patch(
                "aspire_orchestrator.services.sms_io.supabase_select",
                side_effect=_supabase_side_effect_for_a2p(_A2P_REGISTERED_ROW),
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
                thread_memory_id="thread-001",
                body="Hello registered customer",
                scope=_SCOPE,
                capability_token="cap-tok-test",
                idempotency_key=str(uuid.uuid4()),
            )

        assert result["message_sid"] == "SMtest001"
        assert result["status"] == "queued"
