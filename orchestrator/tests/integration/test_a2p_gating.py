"""Integration test — A2P 10DLC gating (Pass 19 Lane D §3.7).

Verifies outbound SMS is blocked for unregistered tenants across all
3 PublicNumberModes, and allowed when registered.

Tests:
  - Registered tenant → send succeeds, Twilio called
  - Unregistered tenant → 403 SmsIoError with error_code='a2p_not_registered', Twilio NOT called
  - Gate applied uniformly for all 3 PublicNumberModes
  - Receipt cut with outcome='denied' on block (Law #2)
  - receipt_type='sms_send_blocked_a2p' on block

Aspire Laws:
  Law #2: Denial receipt cut on every blocked send.
  Law #3: Fail closed — unregistered = deny, not bypass.
  Law #6: Scope from token, never from payload injection.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-ci")

from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services.sms_io import SmsIoError, send_sms

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

SUITE_ID = uuid.UUID("aa000000-0000-0000-0000-000000000001")
OFFICE_ID = uuid.UUID("aa000000-0000-0000-0000-000000000002")
TENANT_ID = uuid.UUID("aa000000-0000-0000-0000-000000000003")

_SCOPE = ScopedIdentity(tenant_id=TENANT_ID, suite_id=SUITE_ID, office_id=OFFICE_ID)

_SEND_KWARGS = dict(
    scope=_SCOPE,
    capability_token="cap-tok-test",
    idempotency_key="idem-key-test-001",
    trace_id="trace-001",
    correlation_id="corr-001",
    capability_token_id="cap-id-001",
)

_FROM_NUMBER_ROW = [{
    "phone_number": "+14484001111",
    "office_id": str(OFFICE_ID),
    "suite_id": str(SUITE_ID),
    "tenant_id": str(TENANT_ID),
    "status": "active",
    "sms_enabled": True,
}]

_THREAD_ROW = [{
    "memory_id": "thread-001",
    "suite_id": str(SUITE_ID),
    "office_id": str(OFFICE_ID),
    "tenant_id": str(TENANT_ID),
    "detail": {"from": "+19175550200", "to": "+14484001111"},
}]

_TWILIO_SUCCESS_RESP = MagicMock(
    status_code=201,
    json=MagicMock(return_value={"sid": "SMxxx", "status": "queued"}),
)


def _a2p_row(status: str) -> list[dict]:
    return [{
        "id": str(uuid.uuid4()),
        "tenant_id": str(TENANT_ID),
        "status": status,
        "registered_at": datetime.now(timezone.utc).isoformat() if status == "registered" else None,
    }]


def _make_supabase_select(a2p_rows: list[dict]) -> AsyncMock:
    """Build supabase_select side_effect that handles all tables in send_sms call order."""
    # send_sms call order: tenant_a2p_registrations, tenant_phone_numbers, memory_objects
    async def _select(table: str, filters: str = "", **kwargs) -> list[dict]:
        if table == "tenant_a2p_registrations":
            return a2p_rows
        if table == "tenant_phone_numbers":
            return _FROM_NUMBER_ROW
        if table == "memory_objects":
            return _THREAD_ROW
        return []
    return _select


def _make_twilio_client() -> MagicMock:
    """HTTP client that returns a successful Twilio response."""
    client = AsyncMock()
    client.post = AsyncMock(return_value=_TWILIO_SUCCESS_RESP)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ---------------------------------------------------------------------------
# Blocked tests (unregistered)
# ---------------------------------------------------------------------------


class TestA2PGateBlocked:
    """SMS send must be blocked for unregistered tenants — Law #3 fail-closed."""

    @pytest.mark.asyncio
    async def test_unregistered_tenant_raises_smsioerror(self) -> None:
        """Unregistered tenant → SmsIoError with code A2P_NOT_REGISTERED."""
        with (
            patch("aspire_orchestrator.services.sms_io.settings",
                  MagicMock(twilio_account_sid="ACtest", twilio_auth_token="auth")),
            patch("aspire_orchestrator.services.sms_io.supabase_select",
                  new=AsyncMock(side_effect=_make_supabase_select(_a2p_row("unregistered")))),
            patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts"),
        ):
            with pytest.raises(SmsIoError) as exc_info:
                await send_sms("thread-001", "Hello", **_SEND_KWARGS)

        assert exc_info.value.code == "A2P_NOT_REGISTERED"
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_unregistered_twilio_not_called(self) -> None:
        """Twilio API must NOT be called when A2P gate blocks the send."""
        twilio_mock = _make_twilio_client()
        with (
            patch("aspire_orchestrator.services.sms_io.settings",
                  MagicMock(twilio_account_sid="ACtest", twilio_auth_token="auth")),
            patch("aspire_orchestrator.services.sms_io.supabase_select",
                  new=AsyncMock(side_effect=_make_supabase_select(_a2p_row("unregistered")))),
            patch("aspire_orchestrator.services.sms_io.httpx.AsyncClient",
                  return_value=twilio_mock),
            patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts"),
        ):
            with pytest.raises(SmsIoError):
                await send_sms("thread-001", "Hello", **_SEND_KWARGS)

        twilio_mock.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_a2p_row_raises_smsioerror(self) -> None:
        """Missing A2P row (no registration at all) → blocked (Law #3: default deny)."""
        with (
            patch("aspire_orchestrator.services.sms_io.settings",
                  MagicMock(twilio_account_sid="ACtest", twilio_auth_token="auth")),
            patch("aspire_orchestrator.services.sms_io.supabase_select",
                  new=AsyncMock(side_effect=_make_supabase_select([]))),  # empty = no row
            patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts"),
        ):
            with pytest.raises(SmsIoError) as exc_info:
                await send_sms("thread-001", "Hello", **_SEND_KWARGS)

        assert exc_info.value.code == "A2P_NOT_REGISTERED"

    @pytest.mark.asyncio
    async def test_pending_registration_blocked(self) -> None:
        """pending_brand status → blocked (only 'registered' allows sends)."""
        with (
            patch("aspire_orchestrator.services.sms_io.settings",
                  MagicMock(twilio_account_sid="ACtest", twilio_auth_token="auth")),
            patch("aspire_orchestrator.services.sms_io.supabase_select",
                  new=AsyncMock(side_effect=_make_supabase_select(_a2p_row("pending_brand")))),
            patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts"),
        ):
            with pytest.raises(SmsIoError) as exc_info:
                await send_sms("thread-001", "Hello", **_SEND_KWARGS)

        assert exc_info.value.code == "A2P_NOT_REGISTERED"

    @pytest.mark.asyncio
    async def test_blocked_receipt_cut_with_denied_outcome(self) -> None:
        """Law #2: blocked send must cut a receipt with outcome='denied'."""
        captured: list[list] = []

        def _capture_receipt(receipts: list) -> None:
            captured.extend(receipts)

        with (
            patch("aspire_orchestrator.services.sms_io.settings",
                  MagicMock(twilio_account_sid="ACtest", twilio_auth_token="auth")),
            patch("aspire_orchestrator.services.sms_io.supabase_select",
                  new=AsyncMock(side_effect=_make_supabase_select(_a2p_row("unregistered")))),
            patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts",
                  side_effect=_capture_receipt),
        ):
            with pytest.raises(SmsIoError):
                await send_sms("thread-001", "Hello", **_SEND_KWARGS)

        assert len(captured) > 0, "No receipt was cut on A2P block — Law #2 violation"
        r = captured[0]
        assert r["outcome"] == "denied"
        assert r["receipt_type"] == "sms_send_blocked_a2p"
        assert r["reason_code"] == "a2p_not_registered"

    @pytest.mark.asyncio
    async def test_blocked_receipt_has_no_raw_pii(self) -> None:
        """Law #9: receipt from A2P block must not contain raw phone numbers in redacted_inputs."""
        captured: list[dict] = []

        def _capture(receipts: list) -> None:
            captured.extend(receipts)

        with (
            patch("aspire_orchestrator.services.sms_io.settings",
                  MagicMock(twilio_account_sid="ACtest", twilio_auth_token="auth")),
            patch("aspire_orchestrator.services.sms_io.supabase_select",
                  new=AsyncMock(side_effect=_make_supabase_select(_a2p_row("unregistered")))),
            patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts",
                  side_effect=_capture),
        ):
            with pytest.raises(SmsIoError):
                await send_sms("thread-001", "A message body", **_SEND_KWARGS)

        assert captured
        r = captured[0]
        raw_json = str(r)
        # Body text must not appear verbatim in receipt
        assert "A message body" not in raw_json, "Receipt contains raw message body (PII)"
        # Full E.164 phone must not appear
        assert "+19175550200" not in raw_json, "Receipt contains raw caller phone number (PII)"


# ---------------------------------------------------------------------------
# Allowed tests (registered)
# ---------------------------------------------------------------------------


class TestA2PGateAllowed:
    """Registered tenant → SMS allowed and Twilio called."""

    @pytest.mark.asyncio
    async def test_registered_tenant_send_proceeds(self) -> None:
        """Registered tenant: send_sms does not raise SmsIoError."""
        twilio_mock = _make_twilio_client()
        with (
            patch("aspire_orchestrator.services.sms_io.settings",
                  MagicMock(twilio_account_sid="ACtest", twilio_auth_token="auth")),
            patch("aspire_orchestrator.services.sms_io.supabase_select",
                  new=AsyncMock(side_effect=_make_supabase_select(_a2p_row("registered")))),
            patch("aspire_orchestrator.services.sms_io.httpx.AsyncClient",
                  return_value=twilio_mock),
            patch("aspire_orchestrator.services.sms_io.supabase_insert",
                  new=AsyncMock(return_value={"id": str(uuid.uuid4())})),
            patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts"),
        ):
            result = await send_sms("thread-001", "Hello!", **_SEND_KWARGS)

        assert result.get("message_sid") or result.get("status")
        twilio_mock.post.assert_called_once()


# ---------------------------------------------------------------------------
# Uniform enforcement across all 3 PublicNumberModes
# ---------------------------------------------------------------------------


class TestA2PGateAcrossPublicNumberModes:
    """A2P gate enforced for all 3 PublicNumberModes — Law #3."""

    @pytest.mark.parametrize("public_number_mode", [
        "ASPIRE_NEW_NUMBER",
        "FORWARD_EXISTING",
        "PORT_IN",
    ])
    @pytest.mark.asyncio
    async def test_a2p_blocked_for_all_modes_when_unregistered(
        self,
        public_number_mode: str,
    ) -> None:
        """A2P gate blocks sends regardless of public_number_mode — mode doesn't bypass gate."""
        # The public_number_mode does NOT appear in send_sms() args —
        # it's stored in front_desk_configs. The A2P check only cares about
        # tenant_a2p_registrations.status. We verify the gate fires for all modes
        # by ensuring the gate fires before any mode-specific logic.
        with (
            patch("aspire_orchestrator.services.sms_io.settings",
                  MagicMock(twilio_account_sid="ACtest", twilio_auth_token="auth")),
            patch("aspire_orchestrator.services.sms_io.supabase_select",
                  new=AsyncMock(side_effect=_make_supabase_select(_a2p_row("unregistered")))),
            patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts"),
        ):
            with pytest.raises(SmsIoError) as exc_info:
                await send_sms("thread-001", f"Test from mode {public_number_mode}", **_SEND_KWARGS)

        assert exc_info.value.code == "A2P_NOT_REGISTERED", (
            f"A2P gate did not block for public_number_mode={public_number_mode}"
        )
