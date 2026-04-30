"""Tests for telephony routes (Pass 16 + Pass 18 -- Law #3, #5, #6).

Covers:
- GET available-numbers: no cap token needed (Green tier)
- POST purchase-number: no token -> 401
- POST purchase-number idempotency hit -> 200 existing record, no duplicate purchase
- POST release-number: no token -> 401
- POST release-number cross-tenant -> 404 (THREAT-015)
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.telephony import router as telephony_router

_app = FastAPI()
_app.include_router(telephony_router)
_client = TestClient(_app, raise_server_exceptions=False)

SUITE_ID = "00000000-0000-0000-0000-000000000001"
OFFICE_ID = "00000000-0000-0000-0000-000000000011"
TENANT_ID = "00000000-0000-0000-0000-000000000099"
PHONE_NUMBER = "+12125550100"
IDEM_KEY = "test-idem-key-xyzabc12345"

_SCOPE_HEADERS = {
    "X-Tenant-Id": TENANT_ID,
    "X-Suite-Id": SUITE_ID,
    "X-Office-Id": OFFICE_ID,
}


def _mint_token(scope: str) -> dict:
    from aspire_orchestrator.services.token_service import mint_token
    return mint_token(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        tool="telephony",
        scopes=[scope],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=45,
    )


# ---------------------------------------------------------------------------
# available-numbers -- Green tier (no cap token)
# ---------------------------------------------------------------------------


def test_available_numbers_green_tier_no_token_required():
    """Green tier: no capability token required for search."""
    from aspire_orchestrator.services.twilio_provisioning import AvailableNumber, PhoneCapabilities

    available = [
        AvailableNumber(
            phone_number=PHONE_NUMBER,
            region="NY",
            monthly_cost_cents=100,
            capabilities=PhoneCapabilities(voice=True, sms=True, mms=False),
        )
    ]

    with patch("aspire_orchestrator.routes.telephony.search_available_numbers",
               new=AsyncMock(return_value=available)), \
         patch("aspire_orchestrator.services.twilio_provisioning.settings",
               twilio_account_sid="ACtest", twilio_auth_token="tok"):

        resp = _client.post(
            "/v1/twilio/available-numbers",
            json={"area_code": "212"},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["count"] == 1
    assert data["numbers"][0]["phone_number"] == PHONE_NUMBER


# ---------------------------------------------------------------------------
# purchase-number -- Yellow tier (cap token required)
# ---------------------------------------------------------------------------


def test_purchase_number_yellow_requires_token():
    """No capability_token -> 401 MISSING_CAPABILITY_TOKEN."""
    resp = _client.post(
        "/v1/twilio/purchase-number",
        json={
            "phone_number": PHONE_NUMBER,
            "idempotency_key": IDEM_KEY,
        },
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401
    assert "MISSING_CAPABILITY_TOKEN" in str(resp.json())


def test_purchase_number_idempotency_hit_returns_existing():
    """Same idempotency_key -> 200 with existing record (not a duplicate purchase)."""
    cap_token = _mint_token("telephony:purchase")

    from aspire_orchestrator.services.twilio_provisioning import PurchasedNumber
    existing = PurchasedNumber(
        phone_number=PHONE_NUMBER,
        twilio_sid="PNxxx",
        elevenlabs_phone_number_id="pn_abc",
        attached_to_agent_id="agent_xxx",
        tenant_id=TENANT_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        receipt_id=str(uuid.uuid4()),
        purchased_at="2026-04-29T10:00:00+00:00",
    )

    with patch("aspire_orchestrator.routes.telephony.purchase_number",
               new=AsyncMock(return_value=existing)):

        resp = _client.post(
            "/v1/twilio/purchase-number",
            json={
                "phone_number": PHONE_NUMBER,
                "idempotency_key": IDEM_KEY,
                "capability_token": cap_token,
            },
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["phone_number"] == PHONE_NUMBER
    assert data["twilio_sid"] == "PNxxx"


# ---------------------------------------------------------------------------
# release-number -- Yellow tier (cap token required)
# ---------------------------------------------------------------------------


def test_release_number_yellow_requires_token():
    """No capability_token -> 401."""
    phone_id = str(uuid.uuid4())
    resp = _client.post(
        f"/v1/twilio/release-number/{phone_id}",
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401


def test_release_number_cross_tenant_404():
    """token for tenant A, phone_number_id of tenant B -> 404 PHONE_NUMBER_NOT_FOUND (THREAT-015)."""
    from aspire_orchestrator.services.twilio_provisioning import TwilioProvisioningError

    cap_token = _mint_token("telephony:release")
    phone_id_b = str(uuid.uuid4())

    with patch("aspire_orchestrator.routes.telephony.release_number",
               new=AsyncMock(side_effect=TwilioProvisioningError(
                   "PHONE_NUMBER_NOT_FOUND",
                   "not found for this tenant",
                   404,
               ))):

        resp = _client.post(
            f"/v1/twilio/release-number/{phone_id_b}",
            json=cap_token,
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 404
    assert "PHONE_NUMBER_NOT_FOUND" in str(resp.json())
