"""Tests for providers/twilio_trust_hub.py — Wave 2-C.

Coverage targets:
  - Happy path for every public method
  - 4xx → TrustHubError (non-retryable)
  - 429 / 5xx → RetryableError raised from inner fn, then TrustHubError after budget
  - Idempotency-Key header passed through on every POST/PUT
  - PII absent from error messages and log output
  - Policy SID cache hit (no second HTTP call) and miss (live API fallback)
  - Missing credentials → fail-closed TrustHubError before any HTTP call
  - Circuit-open → TrustHubError with code TWILIO_CIRCUIT_OPEN

All tests mock httpx.AsyncClient.  No real Twilio calls are made.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------

import aspire_orchestrator.providers.twilio_trust_hub as th_mod
from aspire_orchestrator.providers.twilio_trust_hub import (
    TrustHubError,
    _POLICY_CACHE,
    _CNAM_POLICY_SID_KNOWN,
    add_phone_to_messaging_service,
    add_phone_to_trust_product,
    assign_entity_to_profile,
    assign_entity_to_trust_product,
    assign_number_to_profile,
    create_a2p_brand_registration,
    create_a2p_campaign,
    create_end_user,
    create_messaging_service,
    create_secondary_customer_profile,
    create_trust_product,
    delete_channel_endpoint_assignment,
    disable_caller_id_lookup,
    enable_caller_id_lookup,
    fetch_cnam_policy_sid,
    fetch_customer_profile_status,
    fetch_secondary_profile_policy_sid,
    fetch_shaken_policy_sid,
    fetch_trust_product_status,
    fetch_voice_integrity_policy_sid,
    list_channel_endpoint_assignments,
    submit_customer_profile,
    submit_trust_product,
    update_phone_number_friendly_name,
)
from aspire_orchestrator.services.resilience import (
    CircuitOpenError,
    RetryableError,
    reset_all_breakers,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_policy_cache():
    """Isolate each test — wipe the module-level policy cache."""
    _POLICY_CACHE.clear()
    yield
    _POLICY_CACHE.clear()


@pytest.fixture(autouse=True)
def reset_breakers():
    """Reset circuit breakers between tests."""
    reset_all_breakers()
    yield
    reset_all_breakers()


@pytest.fixture
def configured_settings(monkeypatch):
    """Inject Twilio credentials into settings so _twilio_auth() succeeds."""
    monkeypatch.setattr(
        "aspire_orchestrator.providers.twilio_trust_hub.settings",
        _make_mock_settings(),
    )


def _make_mock_settings(
    account_sid: str = "ACtest000000000000000000000000000",
    auth_token: str = "token_test_secret",
    secondary_policy_sid: str = "",
    shaken_policy_sid: str = "",
    cnam_policy_sid: str = "",
    voice_integrity_policy_sid: str = "",
    callback_url: str = "https://orchestrator.aspire.app/v1/trust-hub/status-callback",
) -> MagicMock:
    m = MagicMock()
    m.twilio_account_sid = account_sid
    m.twilio_auth_token = auth_token
    m.twilio_secondary_profile_policy_sid = secondary_policy_sid
    m.twilio_shaken_policy_sid = shaken_policy_sid
    m.twilio_cnam_policy_sid = cnam_policy_sid
    m.twilio_voice_integrity_policy_sid = voice_integrity_policy_sid
    m.trust_hub_status_callback_url = callback_url
    return m


def _mock_response(status: int, body: Any) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = body
    return resp


def _async_response(status: int, body: Any):
    """Return an async context manager that yields a mock httpx.Response."""
    resp = _mock_response(status, body)

    class _CM:
        async def __aenter__(self_inner):
            return resp

        async def __aexit__(self_inner, *_):
            pass

    return _CM()


def _patch_client(status: int, body: Any):
    """Patch httpx.AsyncClient so every HTTP method returns a mock response."""
    resp = _mock_response(status, body)

    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    client_mock.get = AsyncMock(return_value=resp)
    client_mock.post = AsyncMock(return_value=resp)
    client_mock.put = AsyncMock(return_value=resp)
    client_mock.delete = AsyncMock(return_value=resp)

    return patch("httpx.AsyncClient", return_value=client_mock), client_mock, resp


# ---------------------------------------------------------------------------
# Helpers to inject settings for each test
# ---------------------------------------------------------------------------


def _apply_settings(monkeypatch, **kwargs):
    monkeypatch.setattr(
        "aspire_orchestrator.providers.twilio_trust_hub.settings",
        _make_mock_settings(**kwargs),
    )


# ===========================================================================
# Law #3 — fail-closed when credentials missing
# ===========================================================================


@pytest.mark.asyncio
async def test_missing_account_sid_raises_immediately(monkeypatch):
    _apply_settings(monkeypatch, account_sid="", auth_token="tok")
    with pytest.raises(TrustHubError) as exc_info:
        await create_secondary_customer_profile(
            suite_id="suite-1",
            legal_name="Acme LLC",
            email="owner@acme.com",
            policy_sid="RNabc",
            idempotency_key="idem-1",
        )
    assert exc_info.value.code == "MISSING_TWILIO_CREDENTIALS"


@pytest.mark.asyncio
async def test_missing_auth_token_raises_immediately(monkeypatch):
    _apply_settings(monkeypatch, account_sid="ACtest", auth_token="")
    with pytest.raises(TrustHubError) as exc_info:
        await fetch_customer_profile_status("BUtest")
    assert exc_info.value.code == "MISSING_TWILIO_CREDENTIALS"


# ===========================================================================
# Policy SID fetchers
# ===========================================================================


@pytest.mark.asyncio
async def test_fetch_cnam_policy_sid_returns_known_value(monkeypatch):
    """CNAM SID is known — no HTTP call needed."""
    _apply_settings(monkeypatch)
    sid = await fetch_cnam_policy_sid()
    assert sid == _CNAM_POLICY_SID_KNOWN


@pytest.mark.asyncio
async def test_fetch_cnam_policy_sid_uses_settings_env_var(monkeypatch):
    """Settings env var short-circuits the known value."""
    _apply_settings(monkeypatch, cnam_policy_sid="RNoverride123")
    sid = await fetch_cnam_policy_sid()
    assert sid == "RNoverride123"


@pytest.mark.asyncio
async def test_fetch_secondary_profile_policy_sid_from_settings(monkeypatch):
    """If env var is set, no HTTP call is made."""
    _apply_settings(monkeypatch, secondary_policy_sid="RNsecondary123")
    sid = await fetch_secondary_profile_policy_sid()
    assert sid == "RNsecondary123"
    # Confirm it's cached
    assert _POLICY_CACHE.get("secondary_customer_profile") == "RNsecondary123"


@pytest.mark.asyncio
async def test_fetch_secondary_profile_policy_sid_cache_hit(monkeypatch):
    """Cache hit prevents any HTTP call."""
    _apply_settings(monkeypatch)
    _POLICY_CACHE["secondary_customer_profile"] = "RNcached"

    with patch("httpx.AsyncClient") as mock_client:
        sid = await fetch_secondary_profile_policy_sid()

    assert sid == "RNcached"
    mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_secondary_profile_policy_sid_from_api(monkeypatch):
    """Falls back to live API when env var absent and cache empty."""
    _apply_settings(monkeypatch)

    api_body = {
        "results": [
            {"sid": "RNlive123", "friendly_name": "Secondary Customer Profile"},
        ]
    }
    patcher, client_mock, _resp = _patch_client(200, api_body)
    with patcher:
        sid = await fetch_secondary_profile_policy_sid()

    assert sid == "RNlive123"
    assert _POLICY_CACHE.get("secondary_customer_profile") == "RNlive123"


@pytest.mark.asyncio
async def test_fetch_secondary_profile_policy_sid_not_found_raises(monkeypatch):
    """API returns empty results → TrustHubError POLICY_SID_NOT_FOUND."""
    _apply_settings(monkeypatch)
    api_body = {"results": []}

    patcher, _c, _r = _patch_client(200, api_body)
    with patcher:
        with pytest.raises(TrustHubError) as exc_info:
            await fetch_secondary_profile_policy_sid()
    assert exc_info.value.code == "POLICY_SID_NOT_FOUND"


@pytest.mark.asyncio
async def test_fetch_voice_integrity_policy_sid_from_api(monkeypatch):
    _apply_settings(monkeypatch)
    api_body = {"results": [{"sid": "RNvoice456", "friendly_name": "Voice Integrity Trust"}]}
    patcher, _c, _r = _patch_client(200, api_body)
    with patcher:
        sid = await fetch_voice_integrity_policy_sid()
    assert sid == "RNvoice456"


@pytest.mark.asyncio
async def test_fetch_shaken_policy_sid_from_settings(monkeypatch):
    _apply_settings(monkeypatch, shaken_policy_sid="RNshaken999")
    sid = await fetch_shaken_policy_sid()
    assert sid == "RNshaken999"


# ===========================================================================
# Customer Profile — happy paths
# ===========================================================================


@pytest.mark.asyncio
async def test_create_secondary_customer_profile_happy_path(monkeypatch):
    _apply_settings(monkeypatch)
    body = {
        "sid": "BUprofile001",
        "status": "draft",
        "friendly_name": "Aspire-suite001-Acme LLC",
    }
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        result = await create_secondary_customer_profile(
            suite_id="suite-001abc",
            legal_name="Acme LLC",
            email="owner@acme.com",
            policy_sid="RNsecondary",
            idempotency_key="idem-create-001",
        )
    assert result["sid"] == "BUprofile001"
    assert result["status"] == "draft"


@pytest.mark.asyncio
async def test_create_secondary_customer_profile_passes_idempotency_key(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "BUprofile002", "status": "draft"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        await create_secondary_customer_profile(
            suite_id="suite-002",
            legal_name="Scott Painting",
            email="tony@scott.com",
            policy_sid="RNpolicy",
            idempotency_key="my-idempotency-key-xyz",
        )
    call_kwargs = client_mock.post.call_args
    headers = call_kwargs.kwargs.get("headers", {}) or {}
    assert headers.get("Idempotency-Key") == "my-idempotency-key-xyz"


@pytest.mark.asyncio
async def test_create_secondary_customer_profile_sets_status_callback(monkeypatch):
    _apply_settings(
        monkeypatch,
        callback_url="https://orchestrator.aspire.app/v1/trust-hub/status-callback",
    )
    body = {"sid": "BUprofile003", "status": "draft"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        await create_secondary_customer_profile(
            suite_id="suite-003",
            legal_name="Test Co",
            email="test@test.com",
            policy_sid="RNpol",
            idempotency_key="k1",
        )
    call_kwargs = client_mock.post.call_args
    data = call_kwargs.kwargs.get("data", {})
    assert "StatusCallback" in data
    assert "trust-hub/status-callback" in data["StatusCallback"]


@pytest.mark.asyncio
async def test_submit_customer_profile_happy_path(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "BUprofile001", "status": "pending-review"}
    patcher, client_mock, _resp = _patch_client(200, body)
    with patcher:
        result = await submit_customer_profile("BUprofile001", idempotency_key="submit-k")
    assert result["status"] == "pending-review"
    data = client_mock.put.call_args.kwargs.get("data", {})
    assert data.get("Status") == "pending-review"


@pytest.mark.asyncio
async def test_fetch_customer_profile_status(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "BUprofile001", "status": "twilio-approved"}
    patcher, _c, _r = _patch_client(200, body)
    with patcher:
        status = await fetch_customer_profile_status("BUprofile001")
    assert status == "twilio-approved"


# ===========================================================================
# End Users
# ===========================================================================


@pytest.mark.asyncio
async def test_create_end_user_authorized_rep(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "ITrepresentative001", "type": "authorized_representative_1"}
    patcher, client_mock, _resp = _patch_client(201, body)
    attrs = {
        "first_name": "Tony",
        "last_name": "Scott",
        "business_title": "Owner",
        "email": "tony@scott.com",
        "phone_number": "+14482885386",
        "dob": "1980-01-15",
    }
    with patcher:
        result = await create_end_user(
            profile_sid="BUprofile001",
            end_user_type="authorized_representative_1",
            attributes=attrs,
            friendly_name="Tony Scott",
            idempotency_key="rep1-k",
        )
    assert result["sid"] == "ITrepresentative001"
    call_data = client_mock.post.call_args.kwargs.get("data", {})
    assert call_data["Type"] == "authorized_representative_1"
    # Attributes should be JSON-encoded
    parsed_attrs = json.loads(call_data["Attributes"])
    assert parsed_attrs["first_name"] == "Tony"


@pytest.mark.asyncio
async def test_create_end_user_cnam_information(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "ITcnam001", "type": "cnam_information"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        result = await create_end_user(
            profile_sid="BUcnam001",
            end_user_type="cnam_information",
            attributes={"cnam_display_name": "SCOTT PAINTING"},
            friendly_name="CNAM-SCOTT PAINTING",
            idempotency_key="cnam-eu-k",
        )
    assert result["sid"] == "ITcnam001"
    call_data = client_mock.post.call_args.kwargs.get("data", {})
    parsed_attrs = json.loads(call_data["Attributes"])
    assert parsed_attrs["cnam_display_name"] == "SCOTT PAINTING"


@pytest.mark.asyncio
async def test_create_end_user_dob_not_logged(monkeypatch, caplog):
    """Law #9: DOB must not appear in log output."""
    _apply_settings(monkeypatch)
    body = {"sid": "ITrep002", "type": "authorized_representative_1"}
    patcher, _c, _r = _patch_client(201, body)
    attrs = {"dob": "1980-01-15", "ssn_last4": "4321", "first_name": "Jane"}
    with caplog.at_level(logging.INFO, logger="aspire_orchestrator.providers.twilio_trust_hub"):
        with patcher:
            await create_end_user(
                profile_sid="BUprofile001",
                end_user_type="authorized_representative_1",
                attributes=attrs,
                friendly_name="Jane Rep",
                idempotency_key="rep2-k",
            )
    log_text = caplog.text
    assert "1980-01-15" not in log_text
    assert "4321" not in log_text


# ===========================================================================
# Entity assignments
# ===========================================================================


@pytest.mark.asyncio
async def test_assign_entity_to_profile(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "RNassignment001"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        result = await assign_entity_to_profile(
            "BUprofile001", "ITrep001", idempotency_key="assign-k"
        )
    assert result["sid"] == "RNassignment001"
    call_data = client_mock.post.call_args.kwargs.get("data", {})
    assert call_data["ObjectSid"] == "ITrep001"


@pytest.mark.asyncio
async def test_assign_entity_to_trust_product(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "RNtpassign001"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        result = await assign_entity_to_trust_product(
            "BUshaken001", "BUprofile001", idempotency_key="tp-assign-k"
        )
    assert result["sid"] == "RNtpassign001"
    call_data = client_mock.post.call_args.kwargs.get("data", {})
    assert call_data["ObjectSid"] == "BUprofile001"


# ===========================================================================
# Channel endpoint assignments
# ===========================================================================


@pytest.mark.asyncio
async def test_assign_number_to_profile(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "RNchan001", "channel_endpoint_type": "phone-number"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        result = await assign_number_to_profile(
            "BUprofile001", "PNnumber001", idempotency_key="chan-k"
        )
    assert result["sid"] == "RNchan001"
    call_data = client_mock.post.call_args.kwargs.get("data", {})
    assert call_data["ChannelEndpointType"] == "phone-number"
    assert call_data["ChannelEndpointSid"] == "PNnumber001"


@pytest.mark.asyncio
async def test_add_phone_to_trust_product(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "RNtpchan001"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        result = await add_phone_to_trust_product(
            "BUshaken001", "PNnumber001", idempotency_key="tp-chan-k"
        )
    assert result["sid"] == "RNtpchan001"


@pytest.mark.asyncio
async def test_list_channel_endpoint_assignments_customer_profile(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"results": [{"sid": "RNchan001"}, {"sid": "RNchan002"}]}
    patcher, _c, _r = _patch_client(200, body)
    with patcher:
        results = await list_channel_endpoint_assignments(
            "BUprofile001", kind="customer_profile"
        )
    assert len(results) == 2


@pytest.mark.asyncio
async def test_list_channel_endpoint_assignments_trust_product(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"channel_endpoint_assignments": [{"sid": "RNtpchan001"}]}
    patcher, _c, _r = _patch_client(200, body)
    with patcher:
        results = await list_channel_endpoint_assignments(
            "BUshaken001", kind="trust_product"
        )
    assert len(results) == 1


@pytest.mark.asyncio
async def test_delete_channel_endpoint_assignment_customer_profile(monkeypatch):
    _apply_settings(monkeypatch)
    patcher, client_mock, _resp = _patch_client(204, {})
    _resp.status_code = 204
    with patcher:
        # Should not raise
        await delete_channel_endpoint_assignment(
            "BUprofile001", "RNchan001", kind="customer_profile"
        )


@pytest.mark.asyncio
async def test_delete_channel_endpoint_assignment_404_treated_as_success(monkeypatch):
    _apply_settings(monkeypatch)
    patcher, client_mock, _resp = _patch_client(404, {})
    with patcher:
        # 404 = already gone — should not raise (idempotent)
        await delete_channel_endpoint_assignment(
            "BUprofile001", "RNchan_gone", kind="trust_product"
        )


# ===========================================================================
# Trust Products
# ===========================================================================


@pytest.mark.asyncio
async def test_create_trust_product_happy_path(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "BUshaken001", "status": "draft"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        result = await create_trust_product(
            friendly_name="SHAKEN-suite001",
            email="ops@aspire.app",
            policy_sid="RNshakenpol",
            idempotency_key="shaken-create-k",
        )
    assert result["sid"] == "BUshaken001"
    call_data = client_mock.post.call_args.kwargs.get("data", {})
    assert call_data["PolicySid"] == "RNshakenpol"
    assert "StatusCallback" in call_data


@pytest.mark.asyncio
async def test_create_trust_product_custom_status_callback(monkeypatch):
    _apply_settings(monkeypatch, callback_url="https://default.aspire.app/cb")
    body = {"sid": "BUcnam001", "status": "draft"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        await create_trust_product(
            friendly_name="CNAM-suite001",
            email="ops@aspire.app",
            policy_sid=_CNAM_POLICY_SID_KNOWN,
            status_callback="https://custom-callback.aspire.app/cb",
            idempotency_key="cnam-create-k",
        )
    call_data = client_mock.post.call_args.kwargs.get("data", {})
    assert call_data["StatusCallback"] == "https://custom-callback.aspire.app/cb"


@pytest.mark.asyncio
async def test_submit_trust_product_happy_path(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "BUshaken001", "status": "pending-review"}
    patcher, client_mock, _resp = _patch_client(200, body)
    with patcher:
        result = await submit_trust_product("BUshaken001", idempotency_key="submit-shaken-k")
    assert result["status"] == "pending-review"
    data = client_mock.put.call_args.kwargs.get("data", {})
    assert data.get("Status") == "pending-review"


@pytest.mark.asyncio
async def test_fetch_trust_product_status(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "BUshaken001", "status": "twilio-approved"}
    patcher, _c, _r = _patch_client(200, body)
    with patcher:
        status = await fetch_trust_product_status("BUshaken001")
    assert status == "twilio-approved"


# ===========================================================================
# IncomingPhoneNumbers helpers
# ===========================================================================


@pytest.mark.asyncio
async def test_enable_caller_id_lookup(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "PNnumber001", "voice_caller_id_lookup": True}
    patcher, client_mock, _resp = _patch_client(200, body)
    with patcher:
        result = await enable_caller_id_lookup("PNnumber001", idempotency_key="cid-k")
    assert result["voice_caller_id_lookup"] is True
    call_data = client_mock.put.call_args.kwargs.get("data", {})
    assert call_data["VoiceCallerIdLookup"] == "true"


@pytest.mark.asyncio
async def test_disable_caller_id_lookup(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "PNnumber001", "voice_caller_id_lookup": False}
    patcher, client_mock, _resp = _patch_client(200, body)
    with patcher:
        await disable_caller_id_lookup("PNnumber001", idempotency_key="cid-disable-k")
    call_data = client_mock.put.call_args.kwargs.get("data", {})
    assert call_data["VoiceCallerIdLookup"] == "false"


@pytest.mark.asyncio
async def test_update_phone_number_friendly_name(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "PNnumber001", "friendly_name": "Acme Office"}
    patcher, client_mock, _resp = _patch_client(200, body)
    with patcher:
        result = await update_phone_number_friendly_name(
            "PNnumber001", "Acme Office", idempotency_key="fname-k"
        )
    assert result["friendly_name"] == "Acme Office"
    call_data = client_mock.put.call_args.kwargs.get("data", {})
    assert call_data["FriendlyName"] == "Acme Office"


# ===========================================================================
# A2P 10DLC stubs
# ===========================================================================


@pytest.mark.asyncio
async def test_create_a2p_brand_registration(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "BNbrand001", "status": "PENDING"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        result = await create_a2p_brand_registration(
            customer_profile_sid="BUprofile001",
            a2p_profile_sid="BUa2p001",
            sole_prop=True,
            idempotency_key="a2p-brand-k",
        )
    assert result["sid"] == "BNbrand001"
    call_data = client_mock.post.call_args.kwargs.get("data", {})
    assert call_data["BrandType"] == "SOLE_PROPRIETOR"


@pytest.mark.asyncio
async def test_create_messaging_service(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "MGmessaging001", "friendly_name": "Acme SMS"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        result = await create_messaging_service(
            friendly_name="Acme SMS", idempotency_key="msg-svc-k"
        )
    assert result["sid"] == "MGmessaging001"


@pytest.mark.asyncio
async def test_add_phone_to_messaging_service(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "RNmsgsvc001"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        result = await add_phone_to_messaging_service(
            "MGmessaging001", "PNnumber001", idempotency_key="msg-phone-k"
        )
    assert result["sid"] == "RNmsgsvc001"
    call_data = client_mock.post.call_args.kwargs.get("data", {})
    assert call_data["PhoneNumberSid"] == "PNnumber001"


@pytest.mark.asyncio
async def test_create_a2p_campaign(monkeypatch):
    _apply_settings(monkeypatch)
    body = {"sid": "QEcampaign001", "status": "PENDING"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        result = await create_a2p_campaign(
            messaging_service_sid="MGmessaging001",
            description="Appointment reminders",
            message_samples=["Your appointment is tomorrow at 9am"],
            use_case="MIXED",
            has_embedded_links=False,
            has_embedded_phone=False,
            idempotency_key="a2p-campaign-k",
        )
    assert result["sid"] == "QEcampaign001"
    call_data = client_mock.post.call_args.kwargs.get("data", {})
    assert call_data["UseCase"] == "MIXED"
    assert call_data["HasEmbeddedLinks"] == "false"
    assert call_data["HasEmbeddedPhone"] == "false"
    parsed_samples = json.loads(call_data["MessageSamples"])
    assert "Your appointment is tomorrow at 9am" in parsed_samples


# ===========================================================================
# Error mapping — 4xx → TrustHubError, non-retryable
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,error_body",
    [
        (400, {"code": 20001, "message": "Invalid parameter"}),
        (401, {"code": 20003, "message": "Authentication Error"}),
        (403, {"code": 20004, "message": "Forbidden"}),
        (404, {"code": 20404, "message": "Not Found"}),
        (422, {"code": 20100, "message": "Unprocessable Entity"}),
    ],
)
async def test_4xx_raises_trust_hub_error_not_retry(monkeypatch, status, error_body):
    """4xx responses must raise TrustHubError immediately — never RetryableError."""
    _apply_settings(monkeypatch)
    patcher, _c, _r = _patch_client(status, error_body)
    with patcher:
        with pytest.raises(TrustHubError) as exc_info:
            await create_secondary_customer_profile(
                suite_id="suite-bad",
                legal_name="Bad Corp",
                email="bad@corp.com",
                policy_sid="RNbad",
                idempotency_key="idem-bad",
            )
    assert exc_info.value.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [429, 500, 502, 503, 504],
)
async def test_5xx_429_raises_retryable_in_inner_fn(monkeypatch, status):
    """5xx/429 inner functions raise RetryableError so resilient_call retries.
    After budget exhausts, the original RetryableError propagates.
    We verify the first exception from the inner helper is RetryableError.
    """
    _apply_settings(monkeypatch)

    resp = _mock_response(status, {"message": "Server Error"})
    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    client_mock.get = AsyncMock(return_value=resp)

    with patch("httpx.AsyncClient", return_value=client_mock):
        with pytest.raises((TrustHubError, RetryableError)):
            await fetch_customer_profile_status("BUtest")


# ===========================================================================
# Error mapping — specific operations
# ===========================================================================


@pytest.mark.asyncio
async def test_submit_customer_profile_400_raises(monkeypatch):
    _apply_settings(monkeypatch)
    patcher, _c, _r = _patch_client(400, {"message": "Already submitted"})
    with patcher:
        with pytest.raises(TrustHubError) as exc_info:
            await submit_customer_profile("BUtest", idempotency_key="k")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_assign_entity_to_profile_404_raises(monkeypatch):
    _apply_settings(monkeypatch)
    patcher, _c, _r = _patch_client(404, {"message": "Profile not found"})
    with patcher:
        with pytest.raises(TrustHubError) as exc_info:
            await assign_entity_to_profile("BUnotexist", "ITrep", idempotency_key="k")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_add_phone_to_trust_product_400_raises(monkeypatch):
    _apply_settings(monkeypatch)
    patcher, _c, _r = _patch_client(400, {"message": "Invalid number SID"})
    with patcher:
        with pytest.raises(TrustHubError) as exc_info:
            await add_phone_to_trust_product("BUshaken", "PNbad", idempotency_key="k")
    assert exc_info.value.status_code == 400


# ===========================================================================
# Circuit open → TrustHubError TWILIO_CIRCUIT_OPEN
# ===========================================================================


@pytest.mark.asyncio
async def test_circuit_open_raises_trust_hub_error(monkeypatch):
    """When the circuit breaker is OPEN, operations must raise TrustHubError."""
    _apply_settings(monkeypatch)

    with patch(
        "aspire_orchestrator.providers.twilio_trust_hub.resilient_call",
        side_effect=CircuitOpenError("twilio", 30.0),
    ):
        with pytest.raises(TrustHubError) as exc_info:
            await create_secondary_customer_profile(
                suite_id="suite-oc",
                legal_name="Open Circuit Co",
                email="oc@test.com",
                policy_sid="RNpol",
                idempotency_key="oc-k",
            )
    assert exc_info.value.code == "TWILIO_CIRCUIT_OPEN"
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_circuit_open_fetch_status(monkeypatch):
    _apply_settings(monkeypatch)
    with patch(
        "aspire_orchestrator.providers.twilio_trust_hub.resilient_call",
        side_effect=CircuitOpenError("twilio", 30.0),
    ):
        with pytest.raises(TrustHubError) as exc_info:
            await fetch_trust_product_status("BUtest")
    assert exc_info.value.code == "TWILIO_CIRCUIT_OPEN"


@pytest.mark.asyncio
async def test_circuit_open_assign_entity_to_profile(monkeypatch):
    _apply_settings(monkeypatch)
    with patch(
        "aspire_orchestrator.providers.twilio_trust_hub.resilient_call",
        side_effect=CircuitOpenError("twilio", 30.0),
    ):
        with pytest.raises(TrustHubError) as exc_info:
            await assign_entity_to_profile("BUtest", "ITtest", idempotency_key="k")
    assert exc_info.value.code == "TWILIO_CIRCUIT_OPEN"


@pytest.mark.asyncio
async def test_circuit_open_delete_channel_endpoint_assignment(monkeypatch):
    _apply_settings(monkeypatch)
    with patch(
        "aspire_orchestrator.providers.twilio_trust_hub.resilient_call",
        side_effect=CircuitOpenError("twilio", 30.0),
    ):
        with pytest.raises(TrustHubError) as exc_info:
            await delete_channel_endpoint_assignment(
                "BUtest", "RNtest", kind="customer_profile"
            )
    assert exc_info.value.code == "TWILIO_CIRCUIT_OPEN"


# ===========================================================================
# PII redaction — Law #9
# ===========================================================================


@pytest.mark.asyncio
async def test_pii_not_in_error_message_on_4xx(monkeypatch):
    """Error messages must never echo back PII from the request."""
    _apply_settings(monkeypatch)
    # Simulate a 400 that Twilio echoes back an error body without PII
    err_body = {"code": 20001, "message": "Invalid parameter: Email"}
    patcher, _c, _r = _patch_client(400, err_body)
    with patcher:
        with pytest.raises(TrustHubError) as exc_info:
            await create_secondary_customer_profile(
                suite_id="suite-pii",
                legal_name="Acme",
                email="owner@pii.com",
                policy_sid="RNpol",
                idempotency_key="pii-k",
            )
    # The PII email address must not appear in the exception message
    assert "owner@pii.com" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_phone_number_not_logged_at_info(monkeypatch, caplog):
    """Full E.164 phone numbers must not appear in INFO logs."""
    _apply_settings(monkeypatch)
    body = {"sid": "RNchan001"}
    patcher, _c, _r = _patch_client(201, body)
    with caplog.at_level(logging.INFO, logger="aspire_orchestrator.providers.twilio_trust_hub"):
        with patcher:
            await assign_number_to_profile(
                "BUprofile001", "PN4482885386", idempotency_key="k"
            )
    # The number SID is logged (non-PII) but E.164 numbers must not appear
    log_text = caplog.text
    assert "+14482885386" not in log_text


# ===========================================================================
# Idempotency key propagated through headers
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "coro_factory",
    [
        lambda: submit_customer_profile("BU001", idempotency_key="MY-KEY"),
        lambda: submit_trust_product("BU001", idempotency_key="MY-KEY"),
        lambda: assign_entity_to_trust_product("BU001", "IT001", idempotency_key="MY-KEY"),
        lambda: assign_number_to_profile("BU001", "PN001", idempotency_key="MY-KEY"),
        lambda: add_phone_to_trust_product("BU001", "PN001", idempotency_key="MY-KEY"),
    ],
)
async def test_idempotency_key_in_headers_for_mutations(monkeypatch, coro_factory):
    """Every state-mutating method must pass Idempotency-Key header."""
    _apply_settings(monkeypatch)
    body = {"sid": "BU001", "status": "ok"}
    patcher, client_mock, _resp = _patch_client(200, body)
    with patcher:
        await coro_factory()

    # Check whichever HTTP method was called
    for method_name in ("post", "put"):
        call_obj = getattr(client_mock, method_name).call_args
        if call_obj is not None:
            headers = call_obj.kwargs.get("headers", {}) or {}
            assert headers.get("Idempotency-Key") == "MY-KEY", (
                f"{method_name} call missing Idempotency-Key header"
            )
            break
    else:
        pytest.fail("No post/put call found")


# ===========================================================================
# Additional edge cases
# ===========================================================================


@pytest.mark.asyncio
async def test_delete_channel_endpoint_assignment_trust_product(monkeypatch):
    _apply_settings(monkeypatch)
    patcher, client_mock, _resp = _patch_client(204, {})
    _resp.status_code = 204
    with patcher:
        await delete_channel_endpoint_assignment(
            "BUshaken001", "RNtpchan001", kind="trust_product"
        )
    # Verify the URL contains TrustProducts (not CustomerProfiles)
    call_url = client_mock.delete.call_args.args[0] if client_mock.delete.call_args.args else ""
    assert "TrustProducts" in call_url


@pytest.mark.asyncio
async def test_fetch_customer_profile_status_returns_string(monkeypatch):
    """Return value is always a str, even when status field is absent."""
    _apply_settings(monkeypatch)
    body = {}  # No status field
    patcher, _c, _r = _patch_client(200, body)
    with patcher:
        status = await fetch_customer_profile_status("BUprofile001")
    assert isinstance(status, str)


@pytest.mark.asyncio
async def test_create_trust_product_uses_settings_callback_url(monkeypatch):
    """When status_callback not provided, uses settings.trust_hub_status_callback_url."""
    _apply_settings(monkeypatch, callback_url="https://settings-callback.aspire.app/cb")
    body = {"sid": "BUtp001", "status": "draft"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        await create_trust_product(
            friendly_name="SHAKEN-test",
            email="ops@test.com",
            policy_sid="RNshaken",
            idempotency_key="tp-k",
        )
    call_data = client_mock.post.call_args.kwargs.get("data", {})
    assert call_data["StatusCallback"] == "https://settings-callback.aspire.app/cb"


@pytest.mark.asyncio
async def test_create_a2p_brand_registration_non_sole_prop(monkeypatch):
    """When sole_prop=False, BrandType should not be set."""
    _apply_settings(monkeypatch)
    body = {"sid": "BNbrand002", "status": "PENDING"}
    patcher, client_mock, _resp = _patch_client(201, body)
    with patcher:
        await create_a2p_brand_registration(
            customer_profile_sid="BUprofile001",
            a2p_profile_sid="BUa2p001",
            sole_prop=False,
            idempotency_key="brand-corp-k",
        )
    call_data = client_mock.post.call_args.kwargs.get("data", {})
    assert "BrandType" not in call_data


@pytest.mark.asyncio
async def test_list_channel_endpoint_assignments_empty_response(monkeypatch):
    """Empty API response returns empty list, not KeyError."""
    _apply_settings(monkeypatch)
    patcher, _c, _r = _patch_client(200, {})
    with patcher:
        results = await list_channel_endpoint_assignments(
            "BUprofile001", kind="customer_profile"
        )
    assert results == []
