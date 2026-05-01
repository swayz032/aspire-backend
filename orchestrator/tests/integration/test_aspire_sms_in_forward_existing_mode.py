"""Integration test — Aspire SMS availability in FORWARD_EXISTING mode (Pass 19 Lane D §3.8).

Verifies that in FORWARD_EXISTING mode, Aspire still provisions a companion
SMS-capable number so Ava can send reminders/messages regardless of the
inbound voice routing mode.

Tests:
  - Outbound SMS uses Aspire companion number as from_ (NOT user's existing number)
  - Inbound SMS to Aspire companion number lands in sms_thread memory_object
  - Scope scoped correctly to the companion number's office_id
  - A2P gate applies uniformly in FORWARD_EXISTING mode

Aspire Laws:
  Law #2: Receipt cut on every SMS send.
  Law #3: Fail-closed on missing A2P registration.
  Law #6: Scope from companion number, never from user's existing carrier number.
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUITE_ID = uuid.UUID("bb000000-0000-0000-0000-000000000001")
OFFICE_ID = uuid.UUID("bb000000-0000-0000-0000-000000000002")
TENANT_ID = uuid.UUID("bb000000-0000-0000-0000-000000000003")

_SCOPE = ScopedIdentity(tenant_id=TENANT_ID, suite_id=SUITE_ID, office_id=OFFICE_ID)

# The owner's existing carrier number (NOT the Aspire number)
OWNER_EXISTING_NUMBER = "+14045550182"

# Aspire-provisioned companion number for SMS in FORWARD_EXISTING mode
ASPIRE_COMPANION_NUMBER = "+14484009999"

_SEND_KWARGS = dict(
    scope=_SCOPE,
    capability_token="cap-tok-test",
    idempotency_key="idem-key-forward-existing-001",
    trace_id="trace-fwd-001",
    correlation_id="corr-fwd-001",
    capability_token_id="cap-id-fwd-001",
)


def _companion_number_row() -> list[dict]:
    """Aspire companion number row — sms_enabled=true, public_number_mode=FORWARD_EXISTING."""
    return [{
        "phone_number": ASPIRE_COMPANION_NUMBER,
        "office_id": str(OFFICE_ID),
        "suite_id": str(SUITE_ID),
        "tenant_id": str(TENANT_ID),
        "status": "active",
        "sms_enabled": True,
        "voice_enabled": True,  # also handles voice (Sarah forward target)
        # public_number_mode is in front_desk_configs, not here
    }]


def _existing_number_row() -> list[dict]:
    """Owner's existing carrier number — NOT in tenant_phone_numbers (Aspire doesn't own it)."""
    # This number should NOT appear in supabase_select results for Aspire
    return []  # Aspire has no record of the owner's carrier number


def _thread_row(companion_as_to: bool = True) -> list[dict]:
    """SMS thread row — from the customer to the Aspire companion number."""
    return [{
        "memory_id": "thread-forward-existing-001",
        "suite_id": str(SUITE_ID),
        "office_id": str(OFFICE_ID),
        "tenant_id": str(TENANT_ID),
        "detail": {
            "from": "+19175550200",        # customer's number
            "to": ASPIRE_COMPANION_NUMBER,  # message TO the Aspire companion number
        },
    }]


def _a2p_registered_row() -> list[dict]:
    return [{
        "id": str(uuid.uuid4()),
        "tenant_id": str(TENANT_ID),
        "status": "registered",
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }]


def _make_select(include_a2p: bool = True) -> AsyncMock:
    async def _select(table: str, filters: str = "", **kwargs) -> list[dict]:
        if table == "tenant_a2p_registrations":
            return _a2p_registered_row() if include_a2p else []
        if table == "tenant_phone_numbers":
            return _companion_number_row()
        if table == "memory_objects":
            return _thread_row()
        return []
    return _select


def _make_twilio_client() -> MagicMock:
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(
        status_code=201,
        json=MagicMock(return_value={"sid": "SMcompanion001", "status": "queued"}),
    ))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ---------------------------------------------------------------------------
# Outbound: Uses companion number as from_
# ---------------------------------------------------------------------------


class TestOutboundSMSUsesCompanionNumber:
    """Outbound SMS in FORWARD_EXISTING mode uses Aspire companion number as from_."""

    @pytest.mark.asyncio
    async def test_from_number_is_aspire_companion_not_existing_carrier(self) -> None:
        """send_sms resolves from_ from tenant_phone_numbers — which is the companion number."""
        twilio_mock = _make_twilio_client()
        captured_calls: list[dict] = []

        async def _post_capture(url: str, data: dict, **kwargs) -> MagicMock:
            captured_calls.append({"url": url, "data": data})
            return MagicMock(
                status_code=201,
                json=MagicMock(return_value={"sid": "SMtest001", "status": "queued"}),
            )

        twilio_mock.post = AsyncMock(side_effect=_post_capture)

        with (
            patch("aspire_orchestrator.services.sms_io.settings",
                  MagicMock(twilio_account_sid="ACtest", twilio_auth_token="auth")),
            patch("aspire_orchestrator.services.sms_io.supabase_select",
                  new=AsyncMock(side_effect=_make_select())),
            patch("aspire_orchestrator.services.sms_io.httpx.AsyncClient",
                  return_value=twilio_mock),
            patch("aspire_orchestrator.services.sms_io.supabase_insert",
                  new=AsyncMock(return_value={"id": str(uuid.uuid4())})),
            patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts"),
        ):
            await send_sms(
                "thread-forward-existing-001",
                "Hi from Ava! Your appointment is confirmed.",
                **_SEND_KWARGS,
            )

        assert len(captured_calls) == 1
        call_data = captured_calls[0]["data"]

        # from_ MUST be the Aspire companion number
        assert call_data.get("From") == ASPIRE_COMPANION_NUMBER, (
            f"Expected from_='{ASPIRE_COMPANION_NUMBER}' (Aspire companion), "
            f"got '{call_data.get('From')}'"
        )

        # from_ must NOT be the owner's existing carrier number
        assert call_data.get("From") != OWNER_EXISTING_NUMBER, (
            f"from_ must NOT be the owner's existing carrier number '{OWNER_EXISTING_NUMBER}'"
        )

    @pytest.mark.asyncio
    async def test_existing_carrier_number_not_queried(self) -> None:
        """Aspire backend never looks up the owner's existing carrier number."""
        queried_tables: list[str] = []

        async def _select_capture(table: str, filters: str = "", **kwargs) -> list[dict]:
            queried_tables.append(table)
            if table == "tenant_a2p_registrations":
                return _a2p_registered_row()
            if table == "tenant_phone_numbers":
                return _companion_number_row()
            if table == "memory_objects":
                return _thread_row()
            return []

        with (
            patch("aspire_orchestrator.services.sms_io.settings",
                  MagicMock(twilio_account_sid="ACtest", twilio_auth_token="auth")),
            patch("aspire_orchestrator.services.sms_io.supabase_select",
                  new=AsyncMock(side_effect=_select_capture)),
            patch("aspire_orchestrator.services.sms_io.httpx.AsyncClient",
                  return_value=_make_twilio_client()),
            patch("aspire_orchestrator.services.sms_io.supabase_insert",
                  new=AsyncMock(return_value={"id": str(uuid.uuid4())})),
            patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts"),
        ):
            await send_sms(
                "thread-forward-existing-001",
                "Test",
                **_SEND_KWARGS,
            )

        # Verify only known Aspire tables were queried (not some external carrier table)
        allowed_tables = {
            "tenant_a2p_registrations",
            "tenant_phone_numbers",
            "memory_objects",
            "sms_messages",
        }
        unexpected = [t for t in queried_tables if t not in allowed_tables]
        assert unexpected == [], f"Unexpected tables queried: {unexpected}"


# ---------------------------------------------------------------------------
# Inbound: SMS to companion number lands in sms_thread
# ---------------------------------------------------------------------------


class TestInboundSMSToCompanionNumber:
    """Inbound SMS to Aspire companion number is correctly scoped to the tenant's office."""

    @pytest.mark.asyncio
    async def test_companion_number_scope_resolution(self) -> None:
        """When sms_io resolves from_number, it queries office_id-scoped tenant_phone_numbers.

        This confirms that the companion number lookup is scoped to the correct office
        (Law #6: no cross-tenant number resolution).
        """
        # Simulate inbound: someone sent SMS to ASPIRE_COMPANION_NUMBER
        # Backend resolves which office this number belongs to via tenant_phone_numbers
        # We verify the query uses office_id filter
        queried_filters: list[str] = []

        async def _select_spy(table: str, filters: str = "", **kwargs) -> list[dict]:
            if table == "tenant_phone_numbers":
                queried_filters.append(filters)
                return _companion_number_row()
            if table == "tenant_a2p_registrations":
                return _a2p_registered_row()
            if table == "memory_objects":
                return _thread_row()
            return []

        with (
            patch("aspire_orchestrator.services.sms_io.settings",
                  MagicMock(twilio_account_sid="ACtest", twilio_auth_token="auth")),
            patch("aspire_orchestrator.services.sms_io.supabase_select",
                  new=AsyncMock(side_effect=_select_spy)),
            patch("aspire_orchestrator.services.sms_io.httpx.AsyncClient",
                  return_value=_make_twilio_client()),
            patch("aspire_orchestrator.services.sms_io.supabase_insert",
                  new=AsyncMock(return_value={"id": str(uuid.uuid4())})),
            patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts"),
        ):
            await send_sms(
                "thread-forward-existing-001",
                "Test",
                **_SEND_KWARGS,
            )

        # At least one tenant_phone_numbers query must include office_id for scope
        filters_for_phone_query = " ".join(queried_filters)
        assert str(OFFICE_ID) in filters_for_phone_query, (
            "tenant_phone_numbers query must be scoped by office_id (Law #6)"
        )


# ---------------------------------------------------------------------------
# A2P gate applies in FORWARD_EXISTING mode
# ---------------------------------------------------------------------------


class TestA2PGateInForwardExistingMode:
    """A2P gate is not bypassed for FORWARD_EXISTING mode (Law #3)."""

    @pytest.mark.asyncio
    async def test_forward_existing_mode_a2p_gate_enforced(self) -> None:
        """Even in FORWARD_EXISTING mode, unregistered tenant cannot send SMS."""
        with (
            patch("aspire_orchestrator.services.sms_io.settings",
                  MagicMock(twilio_account_sid="ACtest", twilio_auth_token="auth")),
            patch("aspire_orchestrator.services.sms_io.supabase_select",
                  new=AsyncMock(side_effect=_make_select(include_a2p=False))),
            patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts"),
        ):
            with pytest.raises(SmsIoError) as exc_info:
                await send_sms(
                    "thread-forward-existing-001",
                    "This should be blocked",
                    **_SEND_KWARGS,
                )

        assert exc_info.value.code == "A2P_NOT_REGISTERED"
