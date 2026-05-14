"""Tests for sms_io.send_sms_new (Pass D 2026-05-12).

Covers:
- Happy path: thread memory_object created + send_sms delegated
- Invalid to_phone (< 8 digits, junk chars) → INVALID_TO_PHONE 422
- A2P unregistered → A2P_NOT_REGISTERED 403 (receipt cut, Law #2 + #3)
- idempotency_key flows through to delegated send_sms call
- No from_number for office → NO_SMS_NUMBER 422
- thread memory_object insert failure → THREAD_CREATE_FAILED 500
- Law #6: thread row tenant fields sourced from scope only
- Law #9: phone prefix only in logs/receipts
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SUITE_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))
OFFICE_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000011"))
TENANT_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))
FROM_NUMBER = "+12125550100"
TO_PHONE_RAW = "9175550200"        # 10-digit US — should normalize to E.164
TO_PHONE_E164 = "+19175550200"
MSG_SID = "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
IDEM_KEY = "test-send-new-idem-key-abc12345"


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


def _a2p_registered_row():
    return [{"status": "registered", "tenant_id": TENANT_ID}]


def _a2p_unregistered_row():
    return [{"status": "pending", "tenant_id": TENANT_ID}]


def _phone_numbers_row():
    return [{"phone_number": FROM_NUMBER, "office_id": OFFICE_ID}]


def _send_sms_success_result():
    receipt_id = str(uuid.uuid4())
    return {
        "message_sid": MSG_SID,
        "status": "queued",
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_send_sms_new_happy_path(scoped_identity):
    """Success: thread created, send_sms delegated, thread_memory_id returned."""
    send_result = _send_sms_success_result()
    trace_id = "trace-send-new-001"
    correlation_id = "corr-send-new-001"

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(side_effect=[_a2p_registered_row(), _phone_numbers_row()])), \
         patch("aspire_orchestrator.services.sms_io.supabase_insert",
               new=AsyncMock(return_value={"memory_id": "some-id"})) as mock_insert, \
         patch("aspire_orchestrator.services.sms_io.send_sms",
               new=AsyncMock(return_value=send_result)) as mock_send, \
         patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts"):

        from aspire_orchestrator.services.sms_io import send_sms_new
        result = await send_sms_new(
            to_phone=TO_PHONE_RAW,
            body="Hello from compose!",
            scope=scoped_identity,
            capability_token="cap-tok-xyz",
            idempotency_key=IDEM_KEY,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )

    # Result shape
    assert result["message_sid"] == MSG_SID
    assert result["status"] == "queued"
    assert "receipt_id" in result
    assert "thread_memory_id" in result
    thread_id = result["thread_memory_id"]

    # Thread row inserted into memory_objects
    thread_insert_call = None
    for c in mock_insert.call_args_list:
        if c[0][0] == "memory_objects":
            thread_insert_call = c[0][1]
            break
    assert thread_insert_call is not None, "memory_objects INSERT not called"
    assert thread_insert_call["memory_id"] == thread_id
    assert thread_insert_call["suite_id"] == SUITE_ID
    assert thread_insert_call["office_id"] == OFFICE_ID
    assert thread_insert_call["tenant_id"] == TENANT_ID
    assert thread_insert_call["trace_id"] == trace_id
    assert thread_insert_call["correlation_id"] == correlation_id
    assert thread_insert_call["memory_type"] == "sms_thread"
    assert thread_insert_call["detail"]["from"] == TO_PHONE_E164  # normalized
    assert thread_insert_call["detail"]["origin"] == "compose"

    # send_sms delegated with the new thread_memory_id as a keyword arg
    mock_send.assert_awaited_once()
    call_kw = mock_send.call_args.kwargs
    assert call_kw["thread_memory_id"] == thread_id
    assert call_kw["scope"] is scoped_identity
    assert call_kw["idempotency_key"] == IDEM_KEY
    assert call_kw["trace_id"] == trace_id
    assert call_kw["correlation_id"] == correlation_id


# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------

async def test_send_sms_new_e164_passthrough(scoped_identity):
    """Already-formatted E.164 passes through unchanged."""
    send_result = _send_sms_success_result()

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(side_effect=[_a2p_registered_row(), _phone_numbers_row()])), \
         patch("aspire_orchestrator.services.sms_io.supabase_insert",
               new=AsyncMock(return_value={})) as mock_insert, \
         patch("aspire_orchestrator.services.sms_io.send_sms",
               new=AsyncMock(return_value=send_result)):

        from aspire_orchestrator.services.sms_io import send_sms_new
        result = await send_sms_new(
            to_phone="+19175550200",
            body="E.164 test",
            scope=scoped_identity,
            capability_token="cap",
            idempotency_key=IDEM_KEY + "-e164",
        )

    assert result["thread_memory_id"]
    # Verify detail.from is the exact E.164 value
    for c in mock_insert.call_args_list:
        if c[0][0] == "memory_objects":
            assert c[0][1]["detail"]["from"] == "+19175550200"
            break


@pytest.mark.parametrize("bad_phone", [
    "123",          # too short (< 8 digits)
    "abcdefghij",   # all non-digits
    "+",            # plus with no digits
    "555-abc-1234", # contains non-digit junk after strip
    "",             # empty
    "  ",           # whitespace only
])
async def test_send_sms_new_invalid_to_phone(scoped_identity, bad_phone):
    """Bad to_phone formats → INVALID_TO_PHONE 422 before any DB call."""
    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock()) as mock_select:

        from aspire_orchestrator.services.sms_io import SmsIoError, send_sms_new
        with pytest.raises(SmsIoError) as exc_info:
            await send_sms_new(
                to_phone=bad_phone,
                body="Test body",
                scope=scoped_identity,
                capability_token="cap",
                idempotency_key=IDEM_KEY + "-bad",
            )

    assert exc_info.value.code == "INVALID_TO_PHONE"
    assert exc_info.value.status_code == 422
    # A2P gate must NOT have been called (fail fast before any DB ops)
    mock_select.assert_not_called()


# ---------------------------------------------------------------------------
# A2P gate
# ---------------------------------------------------------------------------

async def test_send_sms_new_a2p_unregistered_blocks(scoped_identity):
    """A2P status != 'registered' → A2P_NOT_REGISTERED 403 + receipt cut."""
    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(return_value=_a2p_unregistered_row())), \
         patch("aspire_orchestrator.services.sms_io.receipt_store.store_receipts") as mock_receipt:

        from aspire_orchestrator.services.sms_io import SmsIoError, send_sms_new
        with pytest.raises(SmsIoError) as exc_info:
            await send_sms_new(
                to_phone=TO_PHONE_RAW,
                body="Test",
                scope=scoped_identity,
                capability_token="cap",
                idempotency_key=IDEM_KEY + "-a2p",
            )

    assert exc_info.value.code == "A2P_NOT_REGISTERED"
    assert exc_info.value.status_code == 403

    # Law #2: receipt cut even on denial
    mock_receipt.assert_called_once()
    receipt = mock_receipt.call_args[0][0][0]
    assert receipt["outcome"] == "denied"
    assert receipt["reason_code"] == "a2p_not_registered"
    assert receipt["action_type"] == "sms_send_new"
    # Law #9: no full phone number in receipt
    assert TO_PHONE_E164 not in str(receipt)


# ---------------------------------------------------------------------------
# No SMS number for office
# ---------------------------------------------------------------------------

async def test_send_sms_new_no_from_number(scoped_identity):
    """No active SMS-enabled number for office → NO_SMS_NUMBER 422."""
    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(side_effect=[_a2p_registered_row(), []])):

        from aspire_orchestrator.services.sms_io import SmsIoError, send_sms_new
        with pytest.raises(SmsIoError) as exc_info:
            await send_sms_new(
                to_phone=TO_PHONE_RAW,
                body="Test",
                scope=scoped_identity,
                capability_token="cap",
                idempotency_key=IDEM_KEY + "-nofrom",
            )

    assert exc_info.value.code == "NO_SMS_NUMBER"
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# Thread insert failure
# ---------------------------------------------------------------------------

async def test_send_sms_new_thread_create_failure(scoped_identity):
    """memory_objects insert failure → THREAD_CREATE_FAILED 500 (fail closed)."""
    from aspire_orchestrator.services.supabase_client import SupabaseClientError

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(side_effect=[_a2p_registered_row(), _phone_numbers_row()])), \
         patch("aspire_orchestrator.services.sms_io.supabase_insert",
               new=AsyncMock(side_effect=SupabaseClientError("DB write failed"))):

        from aspire_orchestrator.services.sms_io import SmsIoError, send_sms_new
        with pytest.raises(SmsIoError) as exc_info:
            await send_sms_new(
                to_phone=TO_PHONE_RAW,
                body="Test",
                scope=scoped_identity,
                capability_token="cap",
                idempotency_key=IDEM_KEY + "-fail",
            )

    assert exc_info.value.code == "THREAD_CREATE_FAILED"
    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# Idempotency key flows through
# ---------------------------------------------------------------------------

async def test_send_sms_new_idempotency_key_flows_through(scoped_identity):
    """idempotency_key is forwarded verbatim to the delegated send_sms call."""
    custom_key = "my-custom-idempotency-key-xyz-9999"
    send_result = _send_sms_success_result()

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(side_effect=[_a2p_registered_row(), _phone_numbers_row()])), \
         patch("aspire_orchestrator.services.sms_io.supabase_insert",
               new=AsyncMock(return_value={})), \
         patch("aspire_orchestrator.services.sms_io.send_sms",
               new=AsyncMock(return_value=send_result)) as mock_send:

        from aspire_orchestrator.services.sms_io import send_sms_new
        await send_sms_new(
            to_phone=TO_PHONE_RAW,
            body="Idempotency test",
            scope=scoped_identity,
            capability_token="cap",
            idempotency_key=custom_key,
        )

    mock_send.assert_awaited_once()
    assert mock_send.call_args.kwargs["idempotency_key"] == custom_key


# ---------------------------------------------------------------------------
# Law #6: tenant isolation — scope fields only in thread row
# ---------------------------------------------------------------------------

async def test_send_sms_new_tenant_fields_from_scope_only(scoped_identity):
    """Thread row must use scope.suite_id/office_id/tenant_id — not request body."""
    send_result = _send_sms_success_result()

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(side_effect=[_a2p_registered_row(), _phone_numbers_row()])), \
         patch("aspire_orchestrator.services.sms_io.supabase_insert",
               new=AsyncMock(return_value={})) as mock_insert, \
         patch("aspire_orchestrator.services.sms_io.send_sms",
               new=AsyncMock(return_value=send_result)):

        from aspire_orchestrator.services.sms_io import send_sms_new
        await send_sms_new(
            to_phone=TO_PHONE_RAW,
            body="Tenant isolation check",
            scope=scoped_identity,
            capability_token="cap",
            idempotency_key=IDEM_KEY + "-iso",
        )

    thread_row = None
    for c in mock_insert.call_args_list:
        if c[0][0] == "memory_objects":
            thread_row = c[0][1]
            break

    assert thread_row is not None
    # Must match scope exactly
    assert thread_row["suite_id"] == SUITE_ID
    assert thread_row["office_id"] == OFFICE_ID
    assert thread_row["tenant_id"] == TENANT_ID
    # Must NOT have drifted to any other value
    assert thread_row["suite_id"] != "00000000-0000-0000-0000-000000000099"  # not TENANT_ID


async def test_send_sms_new_generates_trace_metadata_when_missing(scoped_identity):
    """Thread rows still satisfy the DB contract when middleware IDs are absent."""
    send_result = _send_sms_success_result()

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.sms_io.supabase_select",
               new=AsyncMock(side_effect=[_a2p_registered_row(), _phone_numbers_row()])), \
         patch("aspire_orchestrator.services.sms_io.supabase_insert",
               new=AsyncMock(return_value={})) as mock_insert, \
         patch("aspire_orchestrator.services.sms_io.send_sms",
               new=AsyncMock(return_value=send_result)) as mock_send:

        from aspire_orchestrator.services.sms_io import send_sms_new
        await send_sms_new(
            to_phone=TO_PHONE_RAW,
            body="Trace fallback test",
            scope=scoped_identity,
            capability_token="cap",
            idempotency_key=IDEM_KEY + "-trace-fallback",
        )

    thread_row = None
    for c in mock_insert.call_args_list:
        if c[0][0] == "memory_objects":
            thread_row = c[0][1]
            break

    assert thread_row is not None
    assert thread_row["trace_id"]
    assert thread_row["correlation_id"] == thread_row["trace_id"]
    assert mock_send.call_args.kwargs["trace_id"] == ""
    assert mock_send.call_args.kwargs["correlation_id"] == ""
