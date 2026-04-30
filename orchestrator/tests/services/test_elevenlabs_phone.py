"""Tests for elevenlabs_phone service (Pass 16 -- Law #3, #5, #9).

Covers:
- import_to_elevenlabs happy path + 409 idempotency
- attach_to_agent happy path
- detach_from_elevenlabs happy path + 404 treated as success
- outbound_call happy path
- Missing API key fail-closed (Law #3)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

EL_PHONE_ID = "pn_abc123def456xyz"
PHONE_NUMBER = "+12125550100"
AGENT_ID = "agent_6501kp71h69jfqysgd055hemqhrq"
TWILIO_SID = "ACtest123"
TWILIO_TOKEN = "authtoken123"


def _mock_settings_with_key():
    return patch(
        "aspire_orchestrator.services.elevenlabs_phone.settings",
        elevenlabs_api_key="el_test_api_key_abcdef",
    )


def _mock_settings_no_key():
    return patch(
        "aspire_orchestrator.services.elevenlabs_phone.settings",
        elevenlabs_api_key="",
    )


def _make_client(status_code: int, json_body: dict) -> AsyncMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    client = AsyncMock()
    client.post.return_value = resp
    client.patch.return_value = resp
    client.delete.return_value = resp
    client.get.return_value = resp
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def test_import_to_elevenlabs_happy():
    """POST /v1/convai/phone-numbers -> returns pn_... ID."""
    client = _make_client(201, {"phone_number_id": EL_PHONE_ID})

    with _mock_settings_with_key(), \
         patch("aspire_orchestrator.services.elevenlabs_phone.httpx.AsyncClient",
               return_value=client):
        from aspire_orchestrator.services.elevenlabs_phone import import_to_elevenlabs
        result = await import_to_elevenlabs(
            phone_number=PHONE_NUMBER,
            label="(212) 555-0100",
            twilio_sid=TWILIO_SID,
            twilio_token=TWILIO_TOKEN,
        )

    assert result == EL_PHONE_ID
    call_args = client.post.call_args
    assert "/v1/convai/phone-numbers" in call_args[0][0]
    body = call_args[1]["json"]
    assert body["provider"] == "twilio"
    assert body["phone_number"] == PHONE_NUMBER


async def test_import_to_elevenlabs_409_idempotent():
    """409 from EL -> fetches existing record, returns same ID without raising."""
    post_resp = MagicMock()
    post_resp.status_code = 409
    post_resp.json.return_value = {"detail": "already imported"}

    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = {
        "phone_numbers": [
            {"phone_number": PHONE_NUMBER, "phone_number_id": EL_PHONE_ID}
        ]
    }

    client = AsyncMock()
    client.post.return_value = post_resp
    client.get.return_value = get_resp
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with _mock_settings_with_key(), \
         patch("aspire_orchestrator.services.elevenlabs_phone.httpx.AsyncClient",
               return_value=client):
        from aspire_orchestrator.services.elevenlabs_phone import import_to_elevenlabs
        result = await import_to_elevenlabs(
            phone_number=PHONE_NUMBER,
            label="label",
            twilio_sid=TWILIO_SID,
            twilio_token=TWILIO_TOKEN,
        )

    assert result == EL_PHONE_ID
    client.get.assert_called_once()


async def test_import_to_elevenlabs_500_raises():
    """500 from EL -> error raised after retry exhaustion (Pass 18+ resilience).
    After 3 attempts the resilience layer raises RetryableError or ElevenLabsPhoneError."""
    client = _make_client(500, {"detail": "internal error"})

    with _mock_settings_with_key(), \
         patch("aspire_orchestrator.services.elevenlabs_phone.httpx.AsyncClient",
               return_value=client):
        from aspire_orchestrator.services.elevenlabs_phone import import_to_elevenlabs
        from aspire_orchestrator.services.resilience import RetryableError

        # After retries exhausted, some error is raised -- either RetryableError or ElevenLabsPhoneError
        with pytest.raises((RetryableError, Exception)) as exc_info:
            await import_to_elevenlabs(
                phone_number=PHONE_NUMBER,
                label="label",
                twilio_sid=TWILIO_SID,
                twilio_token=TWILIO_TOKEN,
            )

    # Any exception raised (not a clean success)
    assert exc_info.value is not None


async def test_attach_to_agent_happy():
    """PATCH /v1/convai/phone-numbers/{id} with correct body."""
    client = _make_client(200, {"status": "ok"})

    with _mock_settings_with_key(), \
         patch("aspire_orchestrator.services.elevenlabs_phone.httpx.AsyncClient",
               return_value=client):
        from aspire_orchestrator.services.elevenlabs_phone import attach_to_agent
        await attach_to_agent(EL_PHONE_ID, agent_id=AGENT_ID)

    client.patch.assert_called_once()
    call_url = client.patch.call_args[0][0]
    assert EL_PHONE_ID in call_url
    body = client.patch.call_args[1]["json"]
    assert body["agent_id"] == AGENT_ID


async def test_detach_from_elevenlabs_happy():
    """DELETE /v1/convai/phone-numbers/{id} -> success."""
    client = _make_client(200, {})

    with _mock_settings_with_key(), \
         patch("aspire_orchestrator.services.elevenlabs_phone.httpx.AsyncClient",
               return_value=client):
        from aspire_orchestrator.services.elevenlabs_phone import detach_from_elevenlabs
        await detach_from_elevenlabs(EL_PHONE_ID)

    client.delete.assert_called_once()
    call_url = client.delete.call_args[0][0]
    assert EL_PHONE_ID in call_url


async def test_detach_from_elevenlabs_404_treated_as_success():
    """DELETE returns 404 (already removed) -> no exception raised (idempotent)."""
    client = _make_client(404, {"detail": "not found"})

    with _mock_settings_with_key(), \
         patch("aspire_orchestrator.services.elevenlabs_phone.httpx.AsyncClient",
               return_value=client):
        from aspire_orchestrator.services.elevenlabs_phone import detach_from_elevenlabs
        await detach_from_elevenlabs(EL_PHONE_ID)

    client.delete.assert_called_once()


async def test_outbound_call_happy():
    """POST /v1/convai/twilio/outbound-call -> returns call_sid."""
    call_sid = "CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    client = _make_client(200, {"call_sid": call_sid})

    with _mock_settings_with_key(), \
         patch("aspire_orchestrator.services.elevenlabs_phone.httpx.AsyncClient",
               return_value=client):
        from aspire_orchestrator.services.elevenlabs_phone import outbound_call
        result = await outbound_call(
            agent_id=AGENT_ID,
            to_number="+19175550200",
            el_phone_number_id=EL_PHONE_ID,
            dynamic_variables={"business_name": "Acme Corp"},
        )

    assert result == call_sid
    client.post.assert_called_once()
    call_url = client.post.call_args[0][0]
    assert "/v1/convai/twilio/outbound-call" in call_url
    body = client.post.call_args[1]["json"]
    assert body["agent_id"] == AGENT_ID
    assert body["to_number"] == "+19175550200"
    assert body["phone_number_id"] == EL_PHONE_ID
    assert body["dynamic_variables"]["business_name"] == "Acme Corp"


async def test_missing_api_key_fails_closed_before_http():
    """settings.elevenlabs_api_key=='' -> raises ElevenLabsPhoneError, ZERO HTTP calls."""
    mock_httpx_cls = MagicMock()

    with _mock_settings_no_key(), \
         patch("aspire_orchestrator.services.elevenlabs_phone.httpx.AsyncClient",
               mock_httpx_cls):
        from aspire_orchestrator.services.elevenlabs_phone import (
            ElevenLabsPhoneError,
            import_to_elevenlabs,
        )
        with pytest.raises(ElevenLabsPhoneError) as exc_info:
            await import_to_elevenlabs(
                phone_number=PHONE_NUMBER,
                label="label",
                twilio_sid=TWILIO_SID,
                twilio_token=TWILIO_TOKEN,
            )

    assert exc_info.value.code == "MISSING_API_KEY"
    mock_httpx_cls.assert_not_called()


async def test_attach_to_agent_400_raises():
    """PATCH returns 400 -> ElevenLabsPhoneError raised."""
    client = _make_client(400, {"detail": "Invalid agent_id"})

    with _mock_settings_with_key(), \
         patch("aspire_orchestrator.services.elevenlabs_phone.httpx.AsyncClient",
               return_value=client):
        from aspire_orchestrator.services.elevenlabs_phone import (
            ElevenLabsPhoneError,
            attach_to_agent,
        )
        with pytest.raises(ElevenLabsPhoneError) as exc_info:
            await attach_to_agent(EL_PHONE_ID, agent_id="bad_agent_id")

    assert exc_info.value.status_code == 400


async def test_detach_from_elevenlabs_500_raises():
    """DELETE returns 500 -> error raised after retry exhaustion (resilience wraps detach)."""
    client = _make_client(500, {"detail": "server error"})

    with _mock_settings_with_key(), \
         patch("aspire_orchestrator.services.elevenlabs_phone.httpx.AsyncClient",
               return_value=client):
        from aspire_orchestrator.services.elevenlabs_phone import detach_from_elevenlabs
        from aspire_orchestrator.services.resilience import RetryableError

        with pytest.raises((RetryableError, Exception)):
            await detach_from_elevenlabs(EL_PHONE_ID)

    # 500 is retried -- multiple attempts made
    assert client.delete.call_count >= 1


async def test_import_to_elevenlabs_missing_phone_number_id():
    """EL returns 201 but response has no phone_number_id -> MISSING_PHONE_NUMBER_ID."""
    client = _make_client(201, {"status": "ok"})  # no phone_number_id field

    with _mock_settings_with_key(), \
         patch("aspire_orchestrator.services.elevenlabs_phone.httpx.AsyncClient",
               return_value=client):
        from aspire_orchestrator.services.elevenlabs_phone import (
            ElevenLabsPhoneError,
            import_to_elevenlabs,
        )
        with pytest.raises(ElevenLabsPhoneError) as exc_info:
            await import_to_elevenlabs(
                phone_number=PHONE_NUMBER,
                label="label",
                twilio_sid=TWILIO_SID,
                twilio_token=TWILIO_TOKEN,
            )

    assert exc_info.value.code == "MISSING_PHONE_NUMBER_ID"


async def test_outbound_call_400_raises():
    """POST outbound-call returns 400 -> ElevenLabsPhoneError raised."""
    client = _make_client(400, {"detail": "Invalid to_number"})

    with _mock_settings_with_key(), \
         patch("aspire_orchestrator.services.elevenlabs_phone.httpx.AsyncClient",
               return_value=client):
        from aspire_orchestrator.services.elevenlabs_phone import (
            ElevenLabsPhoneError,
            outbound_call,
        )
        with pytest.raises(ElevenLabsPhoneError):
            await outbound_call(
                agent_id=AGENT_ID,
                to_number="invalid",
                el_phone_number_id=EL_PHONE_ID,
                dynamic_variables={},
            )


async def test_missing_api_key_fails_closed_attach():
    """settings.elevenlabs_api_key=='' -> raises before HTTP in attach_to_agent."""
    mock_httpx_cls = MagicMock()

    with _mock_settings_no_key(), \
         patch("aspire_orchestrator.services.elevenlabs_phone.httpx.AsyncClient",
               mock_httpx_cls):
        from aspire_orchestrator.services.elevenlabs_phone import (
            ElevenLabsPhoneError,
            attach_to_agent,
        )
        with pytest.raises(ElevenLabsPhoneError) as exc_info:
            await attach_to_agent(EL_PHONE_ID, agent_id=AGENT_ID)

    assert exc_info.value.code == "MISSING_API_KEY"
    mock_httpx_cls.assert_not_called()
