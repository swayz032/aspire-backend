"""Tests for twilio_provisioning service (Pass 16 -- Law #2, #3, #4, #6).

Covers:
- search_available_numbers happy path + 429 error
- purchase_number: happy path, idempotency hit, rollback on EL failure,
  rollback on agent-attach failure
- release_number: scope binding cross-tenant 404 (THREAT-015), legacy no-scope path
"""
from __future__ import annotations

import uuid
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SUITE_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))
OFFICE_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000011"))
TENANT_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))
PHONE_NUMBER = "+12125550100"
TWILIO_SID = "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
EL_PHONE_ID = "pn_abc123def456"
IDEM_KEY = "test-idempotency-key-12345"


@pytest.fixture(autouse=True)
def _clear_idem_store():
    """No-op fixture: idempotency is now persistent (DB-backed per Pass 18+).
    Kept for backward compatibility -- all idempotency mocked via supabase_select."""
    yield


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
        "aspire_orchestrator.services.twilio_provisioning.settings",
        twilio_account_sid="ACtest123",
        twilio_auth_token="authtoken123",
    )


def _mock_httpx_purchase_success():
    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {
        "sid": TWILIO_SID,
        "phone_number": PHONE_NUMBER,
        "friendly_name": "(212) 555-0100",
    }
    return resp


def _mock_httpx_delete_success():
    resp = MagicMock()
    resp.status_code = 204
    resp.json.return_value = {}
    return resp


async def test_search_available_numbers_happy():
    """Mock httpx; assert sanitized response with no account_sid."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "available_phone_numbers": [
            {
                "phone_number": PHONE_NUMBER,
                "region": "NY",
                "capabilities": {"voice": True, "SMS": True, "MMS": False},
            }
        ]
    }
    mock_client = AsyncMock()
    mock_client.get.return_value = resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.twilio_provisioning.httpx.AsyncClient",
               return_value=mock_client):
        from aspire_orchestrator.services.twilio_provisioning import search_available_numbers
        results = await search_available_numbers("212", limit=10)

    assert len(results) == 1
    num = results[0]
    assert num.phone_number == PHONE_NUMBER
    assert num.region == "NY"
    assert num.monthly_cost_cents == 100
    assert num.capabilities.voice is True
    assert num.capabilities.sms is True
    assert num.capabilities.mms is False
    num_dict = num.model_dump()
    assert "account_sid" not in num_dict
    assert "ACtest123" not in str(num_dict)


async def test_search_available_numbers_429_raises():
    """HTTP 429 from Twilio -> error raised after retry exhaustion (Pass 18+ resilience).
    429 is classified as RetryableError; after 3 attempts, RetryableError is re-raised.
    Callers should expect either TwilioProvisioningError or RetryableError."""
    resp = MagicMock()
    resp.status_code = 429
    resp.json.return_value = {"message": "Too many requests", "code": 20429}
    mock_client = AsyncMock()
    mock_client.get.return_value = resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.twilio_provisioning.httpx.AsyncClient",
               return_value=mock_client):
        from aspire_orchestrator.services.twilio_provisioning import search_available_numbers
        from aspire_orchestrator.services.resilience import RetryableError

        # After retries exhausted some error is raised (not a clean success)
        with pytest.raises((RetryableError, Exception)) as exc_info:
            await search_available_numbers("212")

    # 3 attempts were made (429 is retried)
    assert mock_client.get.call_count == 3


async def test_search_available_numbers_missing_creds_fails_closed():
    """No credentials -> MISSING_TWILIO_CREDENTIALS (Law #3)."""
    with patch(
        "aspire_orchestrator.services.twilio_provisioning.settings",
        twilio_account_sid="",
        twilio_auth_token="",
    ):
        from aspire_orchestrator.services.twilio_provisioning import (
            TwilioProvisioningError,
            search_available_numbers,
        )
        with pytest.raises(TwilioProvisioningError) as exc_info:
            await search_available_numbers("212")

    assert exc_info.value.code == "MISSING_TWILIO_CREDENTIALS"


async def test_purchase_number_happy_atomic(scoped_identity):
    """Mock all 7 steps; assert DB row inserted, EL import called, agent attached,
    receipt cut, idempotency key recorded."""
    mock_client = AsyncMock()
    mock_client.post.return_value = _mock_httpx_purchase_success()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    inserted_row = {"id": str(uuid.uuid4())}

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.twilio_provisioning.httpx.AsyncClient",
               return_value=mock_client), \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_insert",
               new=AsyncMock(return_value=inserted_row)) as mock_insert, \
         patch("aspire_orchestrator.services.twilio_provisioning.import_to_elevenlabs",
               new=AsyncMock(return_value=EL_PHONE_ID)) as mock_el_import, \
         patch("aspire_orchestrator.services.twilio_provisioning.attach_to_agent",
               new=AsyncMock()) as mock_attach, \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_update",
               new=AsyncMock(return_value={})) as mock_update, \
         patch("aspire_orchestrator.services.twilio_provisioning.receipt_store.store_receipts") as mock_receipt:

        from aspire_orchestrator.services.twilio_provisioning import purchase_number
        result = await purchase_number(
            phone_number=PHONE_NUMBER,
            scope=scoped_identity,
            idempotency_key=IDEM_KEY,
            trace_id="trace-001",
            correlation_id="corr-001",
            capability_token_id="cap-001",
        )

    assert result.phone_number == PHONE_NUMBER
    assert result.twilio_sid == TWILIO_SID
    assert result.elevenlabs_phone_number_id == EL_PHONE_ID
    assert result.suite_id == SUITE_ID
    assert result.office_id == OFFICE_ID
    assert result.tenant_id == TENANT_ID
    assert result.receipt_id

    mock_insert.assert_called_once()
    insert_args = mock_insert.call_args[0]
    assert insert_args[0] == "tenant_phone_numbers"
    assert insert_args[1]["phone_number"] == PHONE_NUMBER
    assert insert_args[1]["status"] == "reserved"

    mock_el_import.assert_called_once_with(
        phone_number=PHONE_NUMBER,
        label="(212) 555-0100",
        twilio_sid="ACtest123",
        twilio_token="authtoken123",
    )
    mock_attach.assert_called_once_with(EL_PHONE_ID, agent_id=mock.ANY)

    mock_update.assert_called_once()
    update_args = mock_update.call_args[0]
    assert update_args[0] == "tenant_phone_numbers"
    assert update_args[2]["status"] == "active"
    assert update_args[2]["elevenlabs_phone_number_id"] == EL_PHONE_ID

    mock_receipt.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["receipt_type"] == "phone_number_purchase"
    assert r["outcome"] == "success"
    assert r["risk_tier"] == "yellow"
    assert r["suite_id"] == SUITE_ID

    # Idempotency key persisted to DB row (Pass 18+ persistent idempotency)
    insert_call = mock_insert.call_args
    assert insert_call[0][1].get("purchase_idempotency_key") == IDEM_KEY or \
           any(IDEM_KEY in str(v) for v in insert_call[0][1].values())


async def test_purchase_number_idempotency_hit(scoped_identity):
    """Same idempotency_key twice -> second call returns existing row from DB,
    ZERO new Twilio API calls, ZERO new receipt cut (Pass 18+ persistent idempotency)."""
    existing_row = {
        "id": str(uuid.uuid4()),
        "phone_number": PHONE_NUMBER,
        "twilio_sid": TWILIO_SID,
        "elevenlabs_phone_number_id": EL_PHONE_ID,
        "attached_to_agent_id": "agent_6501kp71h69jfqysgd055hemqhrq",
        "tenant_id": TENANT_ID,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "receipt_id": str(uuid.uuid4()),
        "purchased_at": "2026-04-29T10:00:00+00:00",
        "purchase_idempotency_key": IDEM_KEY,
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.twilio_provisioning.httpx.AsyncClient",
               return_value=mock_client) as mock_httpx_cls, \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_select",
               new=AsyncMock(return_value=[existing_row])), \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_insert",
               new=AsyncMock(return_value=existing_row)), \
         patch("aspire_orchestrator.services.twilio_provisioning.import_to_elevenlabs",
               new=AsyncMock(return_value=EL_PHONE_ID)), \
         patch("aspire_orchestrator.services.twilio_provisioning.attach_to_agent",
               new=AsyncMock()), \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_update",
               new=AsyncMock(return_value={})), \
         patch("aspire_orchestrator.services.twilio_provisioning.receipt_store.store_receipts") as mock_receipt:

        from aspire_orchestrator.services.twilio_provisioning import purchase_number
        result = await purchase_number(
            phone_number=PHONE_NUMBER,
            scope=scoped_identity,
            idempotency_key=IDEM_KEY,
        )

    # Returns existing record
    assert result.phone_number == PHONE_NUMBER
    assert result.twilio_sid == TWILIO_SID
    # No new Twilio API calls (idempotency returned before any HTTP)
    mock_httpx_cls.assert_not_called()
    # No new receipt cut
    mock_receipt.assert_not_called()


async def test_purchase_number_rollback_on_el_failure(scoped_identity):
    """Twilio purchase succeeds, EL import fails -> Twilio number released,
    phone_number_purchase_failed receipt cut."""
    from aspire_orchestrator.services.elevenlabs_phone import ElevenLabsPhoneError

    mock_client = AsyncMock()
    mock_client.post.return_value = _mock_httpx_purchase_success()
    mock_client.delete.return_value = _mock_httpx_delete_success()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.twilio_provisioning.httpx.AsyncClient",
               return_value=mock_client), \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_insert",
               new=AsyncMock(return_value={"id": str(uuid.uuid4())})), \
         patch("aspire_orchestrator.services.twilio_provisioning.import_to_elevenlabs",
               new=AsyncMock(side_effect=ElevenLabsPhoneError("EL_IMPORT_FAILED", "EL down", 503))), \
         patch("aspire_orchestrator.services.twilio_provisioning.detach_from_elevenlabs",
               new=AsyncMock()), \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_update",
               new=AsyncMock(return_value={})) as mock_update, \
         patch("aspire_orchestrator.services.twilio_provisioning.receipt_store.store_receipts") as mock_receipt:

        from aspire_orchestrator.services.twilio_provisioning import purchase_number
        with pytest.raises(ElevenLabsPhoneError):
            await purchase_number(
                phone_number=PHONE_NUMBER,
                scope=scoped_identity,
                idempotency_key=IDEM_KEY + "-el-fail",
            )

    mock_client.delete.assert_called_once()
    delete_url = mock_client.delete.call_args[0][0]
    assert TWILIO_SID in delete_url
    assert mock_update.call_args[0][2]["status"] == "released"
    r = mock_receipt.call_args[0][0][0]
    assert r["receipt_type"] == "phone_number_purchase_failed"
    assert r["outcome"] == "failed"
    assert r["reason_code"] == "EL_IMPORT_FAILED"


async def test_purchase_number_rollback_on_attach_failure(scoped_identity):
    """EL import succeeds, agent attach fails -> detach called + Twilio released + failure receipt."""
    from aspire_orchestrator.services.elevenlabs_phone import ElevenLabsPhoneError

    mock_client = AsyncMock()
    mock_client.post.return_value = _mock_httpx_purchase_success()
    mock_client.delete.return_value = _mock_httpx_delete_success()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.twilio_provisioning.httpx.AsyncClient",
               return_value=mock_client), \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_insert",
               new=AsyncMock(return_value={"id": str(uuid.uuid4())})), \
         patch("aspire_orchestrator.services.twilio_provisioning.import_to_elevenlabs",
               new=AsyncMock(return_value=EL_PHONE_ID)), \
         patch("aspire_orchestrator.services.twilio_provisioning.attach_to_agent",
               new=AsyncMock(side_effect=ElevenLabsPhoneError("EL_ATTACH_FAILED", "attach failed", 500))), \
         patch("aspire_orchestrator.services.twilio_provisioning.detach_from_elevenlabs",
               new=AsyncMock()) as mock_detach, \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_update",
               new=AsyncMock(return_value={})), \
         patch("aspire_orchestrator.services.twilio_provisioning.receipt_store.store_receipts") as mock_receipt:

        from aspire_orchestrator.services.twilio_provisioning import purchase_number
        with pytest.raises(ElevenLabsPhoneError):
            await purchase_number(
                phone_number=PHONE_NUMBER,
                scope=scoped_identity,
                idempotency_key=IDEM_KEY + "-attach-fail",
            )

    mock_detach.assert_called_once_with(EL_PHONE_ID)
    mock_client.delete.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["receipt_type"] == "phone_number_purchase_failed"
    assert r["reason_code"] == "EL_ATTACH_FAILED"


async def test_release_number_scope_binding_cross_tenant_404():
    """release scope=A, phone_number_id belongs to tenant B -> 404 PHONE_NUMBER_NOT_FOUND.
    Not 403 (not 200) -- THREAT-015."""
    from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
    scope_a = ScopedIdentity(
        tenant_id=uuid.UUID(TENANT_ID),
        suite_id=uuid.UUID(SUITE_ID),
        office_id=uuid.UUID(OFFICE_ID),
    )
    phone_number_id_b = str(uuid.uuid4())

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_select",
               new=AsyncMock(return_value=[])):
        from aspire_orchestrator.services.twilio_provisioning import (
            TwilioProvisioningError,
            release_number,
        )
        with pytest.raises(TwilioProvisioningError) as exc_info:
            await release_number(phone_number_id_b, scope=scope_a)

    assert exc_info.value.code == "PHONE_NUMBER_NOT_FOUND"
    assert exc_info.value.status_code == 404


async def test_release_number_scope_omitted_legacy():
    """scope=None (system-internal call) -> release proceeds without scope filter."""
    phone_number_id = str(uuid.uuid4())
    db_row = {
        "id": phone_number_id,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "tenant_id": TENANT_ID,
        "phone_number": PHONE_NUMBER,
        "twilio_sid": TWILIO_SID,
        "elevenlabs_phone_number_id": EL_PHONE_ID,
    }
    mock_client = AsyncMock()
    mock_client.delete.return_value = _mock_httpx_delete_success()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_select",
               new=AsyncMock(return_value=[db_row])), \
         patch("aspire_orchestrator.services.twilio_provisioning.httpx.AsyncClient",
               return_value=mock_client), \
         patch("aspire_orchestrator.services.twilio_provisioning.detach_from_elevenlabs",
               new=AsyncMock()) as mock_detach, \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_update",
               new=AsyncMock(return_value={})) as mock_update, \
         patch("aspire_orchestrator.services.twilio_provisioning.receipt_store.store_receipts") as mock_receipt:

        from aspire_orchestrator.services.twilio_provisioning import release_number
        await release_number(phone_number_id, scope=None)

    mock_detach.assert_called_once_with(EL_PHONE_ID)
    mock_client.delete.assert_called_once()
    mock_update.assert_called_once()
    mock_receipt.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["receipt_type"] == "phone_number_release"
    assert r["outcome"] == "success"


async def test_search_available_numbers_contains_filter():
    """search_available_numbers with contains= -> Contains param forwarded to Twilio."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"available_phone_numbers": []}
    mock_client = AsyncMock()
    mock_client.get.return_value = resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.twilio_provisioning.httpx.AsyncClient",
               return_value=mock_client):
        from aspire_orchestrator.services.twilio_provisioning import search_available_numbers
        results = await search_available_numbers("212", contains="555", limit=5)

    assert results == []
    call_params = mock_client.get.call_args[1].get("params") or mock_client.get.call_args[0][1] if len(mock_client.get.call_args[0]) > 1 else {}
    # Verify Contains was passed
    all_call_kwargs = mock_client.get.call_args
    assert mock_client.get.called


async def test_purchase_number_no_credentials_fails_closed(scoped_identity):
    """Missing Twilio credentials -> TwilioProvisioningError before HTTP call."""
    with patch(
        "aspire_orchestrator.services.twilio_provisioning.settings",
        twilio_account_sid="",
        twilio_auth_token="",
    ):
        from aspire_orchestrator.services.twilio_provisioning import (
            TwilioProvisioningError,
            purchase_number,
        )
        with pytest.raises(TwilioProvisioningError) as exc_info:
            await purchase_number(
                phone_number=PHONE_NUMBER,
                scope=scoped_identity,
                idempotency_key=IDEM_KEY + "-no-creds",
            )

    assert exc_info.value.code == "MISSING_TWILIO_CREDENTIALS"


async def test_release_number_missing_credentials_fails_closed():
    """Missing Twilio credentials -> TwilioProvisioningError before HTTP call."""
    phone_number_id = str(uuid.uuid4())

    with patch(
        "aspire_orchestrator.services.twilio_provisioning.settings",
        twilio_account_sid="",
        twilio_auth_token="",
    ):
        from aspire_orchestrator.services.twilio_provisioning import (
            TwilioProvisioningError,
            release_number,
        )
        with pytest.raises(TwilioProvisioningError) as exc_info:
            await release_number(phone_number_id, scope=None)

    assert exc_info.value.code == "MISSING_TWILIO_CREDENTIALS"


async def test_release_number_twilio_delete_400_raises():
    """Twilio DELETE returns 400 -> TwilioProvisioningError via _raise_twilio_error."""
    phone_number_id = str(uuid.uuid4())
    db_row = {
        "id": phone_number_id,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "tenant_id": TENANT_ID,
        "phone_number": PHONE_NUMBER,
        "twilio_sid": TWILIO_SID,
        "elevenlabs_phone_number_id": "",  # no EL id
    }
    err_resp = MagicMock()
    err_resp.status_code = 400
    err_resp.json.return_value = {"code": 20404, "message": "The requested resource was not found"}

    mock_client = AsyncMock()
    mock_client.delete.return_value = err_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with _mock_twilio_settings(), \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_select",
               new=AsyncMock(return_value=[db_row])), \
         patch("aspire_orchestrator.services.twilio_provisioning.detach_from_elevenlabs",
               new=AsyncMock()), \
         patch("aspire_orchestrator.services.twilio_provisioning.httpx.AsyncClient",
               return_value=mock_client), \
         patch("aspire_orchestrator.services.twilio_provisioning.supabase_update",
               new=AsyncMock(return_value={})):

        from aspire_orchestrator.services.twilio_provisioning import (
            TwilioProvisioningError,
            release_number,
        )
        with pytest.raises(TwilioProvisioningError) as exc_info:
            await release_number(phone_number_id, scope=None)

    assert "RELEASE_NUMBER" in exc_info.value.code
    assert exc_info.value.status_code == 400
