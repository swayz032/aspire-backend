"""Tests for sms_io service (Pass 16 + Pass 18 -- Law #2, #3, #4, #9).

Covers:
- send_sms happy path: receipt with to_prefix (THREAT-017), body_preview, idempotency_key
- send_sms: idempotency_key persisted to DB row (Pass 18+ Lane 2)
- send_sms: Twilio 4xx -> SmsIoError raised, no receipt
- send_sms: no tenant_phone_numbers row -> fail closed
- update_sms_status: terminal 'delivered' -> row updated + receipt
- update_sms_status: intermediate 'sending' -> row updated, NO receipt
- update_sms_status: same callback twice -> 2 receipts (stateless service)
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SUITE_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))
OFFICE_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000011"))
TENANT_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))
THREAD_MEM_ID = "mem-abc123def456"
FROM_NUMBER = "+12125550100"
TO_NUMBER = "+19175550200"
MSG_SID = "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
IDEM_KEY = "test-sms-idempotency-key-12345"


@pytest.fixture
def scoped_identity():
    from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
    return ScopedIdentity(
        tenant_id=uuid.UUID(TENANT_ID),
        suite_id=uuid.UUID(SUITE_ID),
        office_id=uuid.UUID(OFFICE_ID),
    )


def _mock_twilio_settings():
    return patch(
        "aspire_orchestrator.services.sms_io.settings",
        twilio_account_sid="ACtest123",
        twilio_auth_token="authtoken123",
    )


def _phone_numbers_row():
    return [{"phone_number": FROM_NUMBER, "office_id": OFFICE_ID, "twilio_sid": "PNtest123"}]


def _a2p_registered_row():
    """A2P registration row used by the A2P gate (Pass 19). Mock as 'registered'
    so the existing happy-path tests can proceed past the gate to the actual
    send logic. Tests that target the gate itself construct this differently."""
    return [{"status": "registered", "tenant_id": TENANT_ID}]


def _thread_row():
    return [{
        "memory_id": THREAD_MEM_ID,
        "suite_id": SUITE_ID,
        "detail": {"from": TO_NUMBER, "direction": "inbound"},
    }]


def _sms_row():
    return [{
        "id": str(uuid.uuid4()),
        "message_sid": MSG_SID,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "tenant_id": TENANT_ID,
        "status": "queued",
    }]


def _twilio_send_success():
    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {"sid": MSG_SID, "status": "queued"}
    return resp


def _make_send_client(send_resp=None):
    if send_resp is None:
        send_resp = _twilio_send_success()
    client = AsyncMock()
    client.post.return_value = send_resp
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def test_send_sms_happy(scoped_identity):
    """Happy path: receipt cut with to_prefix (THREAT-017), body_preview, idempotency_key."""
    body_text = "Hello, your appointment is confirmed for tomorrow at 2pm."
    trace_id = "trace-send-001"
    correlation_id = "corr-send-001"

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(side_effect=[_a2p_registered_row(), [], _phone_numbers_row(), _thread_row()])), \
         patch("aspire_orchestrator.services.sms_io.httpx.AsyncClient",
               return_value=_make_send_client()), \
         patch("aspire_orchestrator.services.sms_io.supabase_insert",
               new=AsyncMock(return_value={"id": str(uuid.uuid4())})) as mock_insert, \
         patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts") as mock_receipt:

        from aspire_orchestrator.services.sms_io import send_sms
        result = await send_sms(
            thread_memory_id=THREAD_MEM_ID,
            body=body_text,
            scope=scoped_identity,
            capability_token="cap-tok-abc",
            idempotency_key=IDEM_KEY,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )

    assert result["message_sid"] == MSG_SID
    assert result["status"] == "queued"
    assert result["receipt_id"]

    mock_receipt.assert_called_once()
    receipt = mock_receipt.call_args[0][0][0]
    assert receipt["receipt_type"] == "sms_outbound"
    assert receipt["outcome"] == "success"
    assert receipt["risk_tier"] == "yellow"
    assert receipt["suite_id"] == SUITE_ID

    inputs = receipt["redacted_inputs"]
    # THREAT-017: to must be prefix, NOT full number
    to_prefix = inputs["to_prefix"]
    assert to_prefix == TO_NUMBER[:6] + "..."
    assert TO_NUMBER not in str(inputs)
    # body_preview <= 80 chars
    assert len(inputs["body_preview"]) <= 81
    # idempotency_key present
    assert inputs["idempotency_key"] == IDEM_KEY

    outbound_memory = None
    for c in mock_insert.call_args_list:
        if c[0][0] == "memory_objects":
            outbound_memory = c[0][1]
            break
    assert outbound_memory is not None, "memory_objects INSERT not called"
    assert outbound_memory["trace_id"] == trace_id
    assert outbound_memory["correlation_id"] == correlation_id
    assert outbound_memory["thread_id"] == THREAD_MEM_ID


async def test_send_sms_idempotency_key_in_twilio_header(scoped_identity):
    """idempotency_key must be persisted on sms_messages DB row (Pass 18+ Lane 2)."""
    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(side_effect=[_a2p_registered_row(), [], _phone_numbers_row(), _thread_row()])), \
         patch("aspire_orchestrator.services.sms_io.httpx.AsyncClient",
               return_value=_make_send_client()), \
         patch("aspire_orchestrator.services.sms_io.supabase_insert",
               new=AsyncMock(return_value={"id": str(uuid.uuid4())})) as mock_insert, \
         patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts"):

        from aspire_orchestrator.services.sms_io import send_sms
        await send_sms(
            thread_memory_id=THREAD_MEM_ID,
            body="Test SMS body",
            scope=scoped_identity,
            capability_token="cap-tok-abc",
            idempotency_key=IDEM_KEY,
        )

    # Find sms_messages insert call
    sms_row = None
    for c in mock_insert.call_args_list:
        if c[0][0] == "sms_messages":
            sms_row = c[0][1]
            break
    assert sms_row is not None, "sms_messages INSERT not called"
    assert sms_row["idempotency_key"] == IDEM_KEY


async def test_send_sms_twilio_4xx_raises_sms_failed_receipt(scoped_identity):
    """Twilio returns 400 -> SmsIoError raised AND sms_failed receipt cut (Pass I Law #2 fix).

    The receipt ensures 100% coverage of Law #2 — every outbound attempt has a
    receipt regardless of outcome.  The old test expectation (assert_not_called)
    was written before this fix was merged and is now incorrect.
    """
    err_resp = MagicMock()
    err_resp.status_code = 400
    err_resp.json.return_value = {"message": "Phone number is not SMS-capable", "code": 21606}

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(side_effect=[_a2p_registered_row(), [], _phone_numbers_row(), _thread_row()])), \
         patch("aspire_orchestrator.services.sms_io.httpx.AsyncClient",
               return_value=_make_send_client(err_resp)), \
         patch("aspire_orchestrator.services.sms_io.supabase_insert",
               new=AsyncMock(return_value={})), \
         patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts") as mock_receipt:

        from aspire_orchestrator.services.sms_io import SmsIoError, send_sms
        with pytest.raises(SmsIoError) as exc_info:
            await send_sms(
                thread_memory_id=THREAD_MEM_ID,
                body="Test SMS",
                scope=scoped_identity,
                capability_token="cap-tok",
                idempotency_key=IDEM_KEY + "-4xx",
            )

    assert exc_info.value.code == "TWILIO_SEND_FAILED"
    # Law #2: sms_failed receipt IS cut on 4xx (Pass I fix)
    mock_receipt.assert_called_once()
    receipt = mock_receipt.call_args[0][0][0]
    assert receipt["receipt_type"] == "sms_failed"
    assert receipt["outcome"] == "failed"
    assert receipt["reason_code"] == "TWILIO_SEND_FAILED"


async def test_send_sms_no_from_number_for_office_fails_closed(scoped_identity):
    """No tenant_phone_numbers row for the office -> SmsIoError NO_SMS_NUMBER."""
    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(side_effect=[_a2p_registered_row(), [], []])):

        from aspire_orchestrator.services.sms_io import SmsIoError, send_sms
        with pytest.raises(SmsIoError) as exc_info:
            await send_sms(
                thread_memory_id=THREAD_MEM_ID,
                body="Test",
                scope=scoped_identity,
                capability_token="cap",
                idempotency_key=IDEM_KEY + "-no-num",
            )

    assert exc_info.value.code == "NO_SMS_NUMBER"
    assert exc_info.value.status_code == 422


async def test_update_sms_status_terminal_delivered():
    """MessageStatus=delivered -> row updated + terminal receipt cut."""
    with patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(return_value=_sms_row())), \
         patch("aspire_orchestrator.services.sms_io.supabase_update",
               new=AsyncMock(return_value={})) as mock_update, \
         patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts") as mock_receipt:

        from aspire_orchestrator.services.sms_io import update_sms_status
        await update_sms_status(MSG_SID, "delivered")

    mock_update.assert_called_once()
    assert mock_update.call_args[0][2]["status"] == "delivered"

    mock_receipt.assert_called_once()
    receipt = mock_receipt.call_args[0][0][0]
    assert receipt["receipt_type"] == "sms_status_update"
    assert receipt["outcome"] == "success"
    assert receipt["redacted_outputs"]["status"] == "delivered"


async def test_update_sms_status_intermediate_no_receipt():
    """MessageStatus=sending -> row updated, NO receipt (intermediate only)."""
    with patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(return_value=_sms_row())), \
         patch("aspire_orchestrator.services.sms_io.supabase_update",
               new=AsyncMock(return_value={})) as mock_update, \
         patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts") as mock_receipt:

        from aspire_orchestrator.services.sms_io import update_sms_status
        await update_sms_status(MSG_SID, "sending")

    mock_update.assert_called_once()
    assert mock_update.call_args[0][2]["status"] == "sending"
    mock_receipt.assert_not_called()


async def test_update_sms_status_idempotent_on_message_sid():
    """Same status callback twice -> both calls succeed; receipt cut each time.
    Service is stateless -- idempotency enforced at DB layer (unique constraint)."""
    with patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(return_value=_sms_row())), \
         patch("aspire_orchestrator.services.sms_io.supabase_update",
               new=AsyncMock(return_value={})), \
         patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts") as mock_receipt:

        from aspire_orchestrator.services.sms_io import update_sms_status
        await update_sms_status(MSG_SID, "delivered")
        await update_sms_status(MSG_SID, "delivered")

    assert mock_receipt.call_count == 2
    for c in mock_receipt.call_args_list:
        r = c[0][0][0]
        assert r["receipt_type"] == "sms_status_update"
        assert r["outcome"] == "success"


async def test_send_sms_missing_creds_fails_closed(scoped_identity):
    """MISSING_TWILIO_CREDENTIALS -> SmsIoError before any HTTP call (Law #3)."""
    with patch(
        "aspire_orchestrator.services.sms_io.settings",
        twilio_account_sid="",
        twilio_auth_token="",
    ):
        from aspire_orchestrator.services.sms_io import SmsIoError, send_sms
        with pytest.raises(SmsIoError) as exc_info:
            await send_sms(
                thread_memory_id=THREAD_MEM_ID,
                body="Test",
                scope=scoped_identity,
                capability_token="cap",
                idempotency_key=IDEM_KEY + "-no-creds",
            )

    assert exc_info.value.code == "MISSING_TWILIO_CREDENTIALS"


async def test_send_sms_thread_not_found_fails_closed(scoped_identity):
    """Phone number found but thread_memory_id has no DB row -> THREAD_NOT_FOUND."""
    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(side_effect=[_a2p_registered_row(), [], _phone_numbers_row(), []])):  # empty thread

        from aspire_orchestrator.services.sms_io import SmsIoError, send_sms
        with pytest.raises(SmsIoError) as exc_info:
            await send_sms(
                thread_memory_id=THREAD_MEM_ID,
                body="Test",
                scope=scoped_identity,
                capability_token="cap",
                idempotency_key=IDEM_KEY + "-no-thread",
            )

    assert exc_info.value.code == "THREAD_NOT_FOUND"
    assert exc_info.value.status_code == 404


async def test_send_sms_cannot_resolve_to_number(scoped_identity):
    """Thread row exists but detail has no from/to -> CANNOT_RESOLVE_TO_NUMBER."""
    thread_no_contact = [{
        "memory_id": THREAD_MEM_ID,
        "suite_id": SUITE_ID,
        "detail": {},  # no 'from' or 'to'
    }]

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(side_effect=[_a2p_registered_row(), [], _phone_numbers_row(), thread_no_contact])):

        from aspire_orchestrator.services.sms_io import SmsIoError, send_sms
        with pytest.raises(SmsIoError) as exc_info:
            await send_sms(
                thread_memory_id=THREAD_MEM_ID,
                body="Test",
                scope=scoped_identity,
                capability_token="cap",
                idempotency_key=IDEM_KEY + "-no-contact",
            )

    assert exc_info.value.code == "CANNOT_RESOLVE_TO_NUMBER"
    assert exc_info.value.status_code == 422


async def test_update_sms_status_undelivered_outcome_failed():
    """MessageStatus=undelivered -> terminal receipt with outcome='failed'."""
    with patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(return_value=_sms_row())), \
         patch("aspire_orchestrator.services.sms_io.supabase_update",
               new=AsyncMock(return_value={})), \
         patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts") as mock_receipt:

        from aspire_orchestrator.services.sms_io import update_sms_status
        await update_sms_status(MSG_SID, "undelivered", error_code="30007")

    mock_receipt.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["receipt_type"] == "sms_status_update"
    assert r["outcome"] == "failed"  # undelivered = failed
    assert r["redacted_outputs"]["error_code"] == "30007"


async def test_update_sms_status_unknown_message_sid_logs_only():
    """Status callback for unknown MessageSid -> no crash, no receipt (warning logged)."""
    with patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(return_value=[])), \
         patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts") as mock_receipt:

        from aspire_orchestrator.services.sms_io import update_sms_status
        await update_sms_status("SM_UNKNOWN_123", "delivered")

    mock_receipt.assert_not_called()
