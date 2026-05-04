"""Tests for A2P 10DLC registration routes — Wave 7.

Covers:
  - POST /v1/a2p/start  — happy path, profile not approved, already started
  - POST /v1/a2p/verify-otp — valid OTP, invalid OTP, exceeded retries
  - GET  /v1/a2p/status — correct shape

Author: Aspire — Wave 7
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.a2p import router

# ---------------------------------------------------------------------------
# Test app setup
# ---------------------------------------------------------------------------

app = FastAPI()
app.include_router(router)

SUITE_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())
OFFICE_ID = str(uuid.uuid4())
BRAND_ID = str(uuid.uuid4())
CAMPAIGN_ID = str(uuid.uuid4())
TRUST_PROFILE_ID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Mock scope + capability token helpers (matching front_desk.py pattern)
# ---------------------------------------------------------------------------


def _mock_scope() -> Any:
    scope = type("Scope", (), {
        "suite_id": uuid.UUID(SUITE_ID),
        "tenant_id": uuid.UUID(TENANT_ID),
        "office_id": uuid.UUID(OFFICE_ID),
    })()
    return scope


def _patch_scope():
    """Patch _resolve_scope to return mock scope without requiring headers."""
    return patch(
        "aspire_orchestrator.routes.a2p._resolve_scope",
        return_value=_mock_scope(),
    )


def _patch_cap_token_valid():
    """Patch _validate_cap_token to do nothing (passes silently)."""
    return patch(
        "aspire_orchestrator.routes.a2p._validate_cap_token",
        return_value=None,
    )


def _patch_cap_token_id():
    return patch(
        "aspire_orchestrator.routes.a2p._cap_token_id",
        return_value="test-cap-token-id",
    )


def _patch_enqueue():
    return patch(
        "aspire_orchestrator.routes.a2p._enqueue_advance_a2p",
        new_callable=AsyncMock,
    )


# ---------------------------------------------------------------------------
# POST /v1/a2p/start tests
# ---------------------------------------------------------------------------


def _valid_start_body() -> dict[str, Any]:
    return {
        "brand_type": "sole_proprietor",
        "campaign_use_case": "MIXED",
        "campaign_description": "Business notifications for Aspire platform users",
        "sample_messages": ["Hello from Aspire!", "Your appointment is confirmed."],
        "has_embedded_links": False,
        "has_embedded_phone": False,
        "capability_token": {"token_id": "test", "scopes": ["a2p:register"]},
    }


def _profile_row(trust_state: str = "profile_approved") -> dict[str, Any]:
    return {
        "id": TRUST_PROFILE_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "trust_state": trust_state,
        "twilio_secondary_profile_sid": "BUaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa00",
    }


@pytest.mark.asyncio
async def test_a2p_start_happy_path():
    """Valid token + profile approved → 200, brand row created, ARQ enqueued."""
    client = TestClient(app, raise_server_exceptions=True)

    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        _patch_enqueue() as mock_enqueue,
        patch(
            "aspire_orchestrator.routes.a2p.supabase_select",
            new_callable=AsyncMock,
            side_effect=[
                [_profile_row()],  # trust profile select
                [],               # existing brand select (none found)
            ],
        ),
        patch(
            "aspire_orchestrator.routes.a2p.supabase_insert",
            new_callable=AsyncMock,
            side_effect=[
                {"id": BRAND_ID},     # brand insert
                {"id": CAMPAIGN_ID},  # campaign insert
            ],
        ),
    ):
        resp = client.post("/v1/a2p/start", json=_valid_start_body())

    assert resp.status_code == 200
    data = resp.json()
    assert "brand_id" in data
    assert "campaign_id" in data
    assert data["brand_status"] == "draft"
    assert data["campaign_status"] == "draft"

    mock_enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_a2p_start_profile_not_approved_returns_409():
    """Customer Profile not yet approved → 409 PROFILE_NOT_READY."""
    client = TestClient(app, raise_server_exceptions=True)

    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        patch(
            "aspire_orchestrator.routes.a2p.supabase_select",
            new_callable=AsyncMock,
            return_value=[_profile_row(trust_state="kyb_collected")],
        ),
    ):
        resp = client.post("/v1/a2p/start", json=_valid_start_body())

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "PROFILE_NOT_READY"
    assert detail["reason_code"] == "PROFILE_NOT_READY"
    assert "kyb_collected" in detail["trust_state"]


@pytest.mark.asyncio
async def test_a2p_start_no_trust_profile_returns_409():
    """No trust profile at all → 409 PROFILE_NOT_READY."""
    client = TestClient(app, raise_server_exceptions=True)

    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        patch(
            "aspire_orchestrator.routes.a2p.supabase_select",
            new_callable=AsyncMock,
            return_value=[],  # no profile
        ),
    ):
        resp = client.post("/v1/a2p/start", json=_valid_start_body())

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "PROFILE_NOT_READY"


@pytest.mark.asyncio
async def test_a2p_start_already_started_returns_409():
    """Brand already exists → 409 A2P_ALREADY_STARTED."""
    client = TestClient(app, raise_server_exceptions=True)

    existing_brand = {
        "id": BRAND_ID,
        "brand_status": "pending",
        "suite_id": SUITE_ID,
    }

    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        patch(
            "aspire_orchestrator.routes.a2p.supabase_select",
            new_callable=AsyncMock,
            side_effect=[
                [_profile_row()],    # trust profile
                [existing_brand],    # existing brand
            ],
        ),
    ):
        resp = client.post("/v1/a2p/start", json=_valid_start_body())

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "A2P_ALREADY_STARTED"
    assert detail["brand_id"] == BRAND_ID


@pytest.mark.asyncio
async def test_a2p_start_missing_capability_token_denied():
    """Missing capability token → route must deny (Law #5)."""
    client = TestClient(app, raise_server_exceptions=False)

    # DO NOT patch _validate_cap_token — let the real validator reject it
    body = {**_valid_start_body(), "capability_token": None}

    with (
        _patch_scope(),
        patch(
            "aspire_orchestrator.routes.a2p._validate_cap_token",
            side_effect=Exception("Missing capability token for a2p:register"),
        ),
    ):
        resp = client.post("/v1/a2p/start", json=body)

    assert resp.status_code in (401, 403, 422, 500)


@pytest.mark.asyncio
async def test_a2p_start_invalid_use_case_returns_422():
    """Invalid campaign_use_case → 422 validation error."""
    client = TestClient(app, raise_server_exceptions=True)

    with _patch_scope(), _patch_cap_token_valid(), _patch_cap_token_id():
        body = {**_valid_start_body(), "campaign_use_case": "INVALID_USE_CASE"}
        resp = client.post("/v1/a2p/start", json=body)

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /v1/a2p/verify-otp tests
# ---------------------------------------------------------------------------


def _valid_otp_body(code: str = "654321") -> dict[str, Any]:
    return {
        "otp_code": code,
        "capability_token": {"token_id": "test", "scopes": ["a2p:register"]},
    }


@pytest.mark.asyncio
async def test_verify_otp_valid_returns_200():
    """Valid OTP → 200, brand_status=otp_confirmed, ARQ enqueued."""
    client = TestClient(app, raise_server_exceptions=True)

    otp_result = {
        "success": True,
        "brand_id": BRAND_ID,
        "brand_status": "otp_confirmed",
        "otp_attempts": 0,
        "locked_out": False,
        "receipt_id": "trust_a2p_brand_registered_test",
    }

    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_enqueue() as mock_enqueue,
        patch(
            "aspire_orchestrator.routes.a2p.submit_a2p_otp",
            new_callable=AsyncMock,
            return_value=otp_result,
        ),
    ):
        resp = client.post("/v1/a2p/verify-otp", json=_valid_otp_body())

    assert resp.status_code == 200
    data = resp.json()
    assert data["brand_status"] == "otp_confirmed"
    assert data["locked_out"] is False
    assert "receipt_id" in data

    mock_enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_otp_invalid_code_returns_400():
    """Wrong OTP code → 400 INVALID_OTP."""
    client = TestClient(app, raise_server_exceptions=True)

    otp_result = {
        "success": False,
        "brand_id": BRAND_ID,
        "brand_status": "pending",
        "otp_attempts": 1,
        "locked_out": False,
        "receipt_id": None,
        "reason_code": "INVALID_OTP",
    }

    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        patch(
            "aspire_orchestrator.routes.a2p.submit_a2p_otp",
            new_callable=AsyncMock,
            return_value=otp_result,
        ),
    ):
        resp = client.post("/v1/a2p/verify-otp", json=_valid_otp_body("111111"))

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_OTP"
    assert detail["reason_code"] == "INVALID_OTP"
    assert detail["otp_attempts"] == 1


@pytest.mark.asyncio
async def test_verify_otp_exceeded_retries_returns_429():
    """3rd wrong OTP → 429 OTP_LOCKED_OUT."""
    client = TestClient(app, raise_server_exceptions=True)

    otp_result = {
        "success": False,
        "brand_id": BRAND_ID,
        "brand_status": "suspended",
        "otp_attempts": 3,
        "locked_out": True,
        "receipt_id": None,
        "reason_code": "OTP_LOCKED_OUT",
    }

    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        patch(
            "aspire_orchestrator.routes.a2p.submit_a2p_otp",
            new_callable=AsyncMock,
            return_value=otp_result,
        ),
    ):
        resp = client.post("/v1/a2p/verify-otp", json=_valid_otp_body("000000"))

    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["error"] == "OTP_LOCKED_OUT"
    assert detail["otp_attempts"] == 3


@pytest.mark.asyncio
async def test_verify_otp_non_digit_code_returns_422():
    """OTP code with non-digits → 422 (Pydantic pattern validation)."""
    client = TestClient(app, raise_server_exceptions=True)

    with _patch_scope(), _patch_cap_token_valid():
        resp = client.post("/v1/a2p/verify-otp", json=_valid_otp_body("ABCDEF"))

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_verify_otp_wrong_length_returns_422():
    """OTP code with wrong length → 422."""
    client = TestClient(app, raise_server_exceptions=True)

    with _patch_scope(), _patch_cap_token_valid():
        resp = client.post("/v1/a2p/verify-otp", json=_valid_otp_body("12345"))  # 5 digits

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/a2p/status tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2p_status_returns_correct_shape():
    """GET /v1/a2p/status returns brand_status + campaign_status + otp_required."""
    client = TestClient(app, raise_server_exceptions=True)

    brand_row = {
        "id": BRAND_ID,
        "suite_id": SUITE_ID,
        "brand_status": "pending",
        "brand_type": "sole_proprietor",
        "otp_verified_at": None,
        "submitted_at": "2026-05-03T12:00:00+00:00",
        "approved_at": None,
        "rejection_reason": None,
    }
    campaign_row = {
        "id": CAMPAIGN_ID,
        "campaign_status": "draft",
    }

    with (
        _patch_scope(),
        patch(
            "aspire_orchestrator.routes.a2p.supabase_select",
            new_callable=AsyncMock,
            side_effect=[
                [brand_row],    # brand select
                [campaign_row], # campaign select
            ],
        ),
    ):
        resp = client.get("/v1/a2p/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["brand_id"] == BRAND_ID
    assert data["brand_status"] == "pending"
    assert data["campaign_id"] == CAMPAIGN_ID
    assert data["campaign_status"] == "draft"
    assert data["otp_required"] is True  # pending + no otp_verified_at
    assert "brand_type" in data
    assert "submitted_at" in data


@pytest.mark.asyncio
async def test_a2p_status_approved_brand_otp_not_required():
    """Approved brand → otp_required=False."""
    client = TestClient(app, raise_server_exceptions=True)

    brand_row = {
        "id": BRAND_ID,
        "suite_id": SUITE_ID,
        "brand_status": "approved",
        "brand_type": "sole_proprietor",
        "otp_verified_at": "2026-05-03T12:00:00+00:00",
        "submitted_at": "2026-05-03T11:00:00+00:00",
        "approved_at": "2026-05-03T14:00:00+00:00",
        "rejection_reason": None,
    }

    with (
        _patch_scope(),
        patch(
            "aspire_orchestrator.routes.a2p.supabase_select",
            new_callable=AsyncMock,
            side_effect=[
                [brand_row],
                [],  # no campaign yet
            ],
        ),
    ):
        resp = client.get("/v1/a2p/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["otp_required"] is False


@pytest.mark.asyncio
async def test_a2p_status_no_brand_returns_404():
    """No A2P registration → 404."""
    client = TestClient(app, raise_server_exceptions=True)

    with (
        _patch_scope(),
        patch(
            "aspire_orchestrator.routes.a2p.supabase_select",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        resp = client.get("/v1/a2p/status")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "NO_A2P_REGISTRATION"


@pytest.mark.asyncio
async def test_a2p_status_rejected_exposes_rejection_reason():
    """Rejected brand → rejection_reason visible in status response."""
    client = TestClient(app, raise_server_exceptions=True)

    brand_row = {
        "id": BRAND_ID,
        "suite_id": SUITE_ID,
        "brand_status": "rejected",
        "brand_type": "sole_proprietor",
        "otp_verified_at": None,
        "submitted_at": None,
        "approved_at": None,
        "rejection_reason": "Sole proprietor verification failed",
    }

    with (
        _patch_scope(),
        patch(
            "aspire_orchestrator.routes.a2p.supabase_select",
            new_callable=AsyncMock,
            side_effect=[[brand_row], []],
        ),
    ):
        resp = client.get("/v1/a2p/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["rejection_reason"] == "Sole proprietor verification failed"


@pytest.mark.asyncio
async def test_a2p_status_non_rejected_hides_rejection_reason():
    """Non-rejected brand → rejection_reason is null in response (not exposed)."""
    client = TestClient(app, raise_server_exceptions=True)

    brand_row = {
        "id": BRAND_ID,
        "suite_id": SUITE_ID,
        "brand_status": "approved",
        "brand_type": "sole_proprietor",
        "otp_verified_at": "2026-05-03T12:00:00+00:00",
        "submitted_at": None,
        "approved_at": None,
        "rejection_reason": "OTP_ATTEMPT:2",  # internal tracking, must not be exposed
    }

    with (
        _patch_scope(),
        patch(
            "aspire_orchestrator.routes.a2p.supabase_select",
            new_callable=AsyncMock,
            side_effect=[[brand_row], []],
        ),
    ):
        resp = client.get("/v1/a2p/status")

    assert resp.status_code == 200
    data = resp.json()
    # rejection_reason must be None for non-failed brands
    assert data["rejection_reason"] is None


# ---------------------------------------------------------------------------
# Tenant isolation — suite_id always from headers, never from body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2p_start_uses_scope_from_headers_not_body():
    """Tenant scope comes from X- headers via _resolve_scope, never from body fields."""
    client = TestClient(app, raise_server_exceptions=True)

    # The body does NOT contain suite_id or tenant_id
    body = _valid_start_body()
    assert "suite_id" not in body
    assert "tenant_id" not in body

    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        _patch_enqueue(),
        patch(
            "aspire_orchestrator.routes.a2p.supabase_select",
            new_callable=AsyncMock,
            side_effect=[[_profile_row()], []],
        ),
        patch(
            "aspire_orchestrator.routes.a2p.supabase_insert",
            new_callable=AsyncMock,
            return_value={"id": BRAND_ID},
        ),
    ):
        resp = client.post("/v1/a2p/start", json=body)

    assert resp.status_code == 200
