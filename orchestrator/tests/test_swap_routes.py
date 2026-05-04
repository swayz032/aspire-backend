"""Tests for Wave 11 number-swap route.

Covers:
  - Missing capability token → 401
  - Wrong scope → 403
  - Tenant without trust profile → 409
  - Tenant with trust profile in wrong state (not number_attached) → 409
  - No active phone number → 409
  - Swap already in progress → 409
  - Both search + e164 provided → 422
  - Neither search nor e164 → 422
  - Self-swap (same number) → 422
  - Happy path (search): returns swap_job_id + estimated_completion, enqueues job
  - Happy path (e164 direct): same result, no search call
  - Number search returns empty → 422 NO_NUMBERS_AVAILABLE

Author: Aspire — Wave 11
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.twilio_swap import router

# ---------------------------------------------------------------------------
# Test app setup
# ---------------------------------------------------------------------------

app = FastAPI()
app.include_router(router)

SUITE_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())
OFFICE_ID = str(uuid.uuid4())
SWAP_JOB_ID = str(uuid.uuid4())
PHONE_NUMBER_ID = str(uuid.uuid4())
TRUST_PROFILE_ID = str(uuid.uuid4())

_HEADERS = {
    "X-Tenant-Id": TENANT_ID,
    "X-Suite-Id": SUITE_ID,
    "X-Office-Id": OFFICE_ID,
}


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _mock_scope() -> Any:
    scope = type("Scope", (), {
        "suite_id": uuid.UUID(SUITE_ID),
        "tenant_id": uuid.UUID(TENANT_ID),
        "office_id": uuid.UUID(OFFICE_ID),
    })()
    return scope


def _patch_scope():
    return patch(
        "aspire_orchestrator.routes.twilio_swap._resolve_scope",
        return_value=_mock_scope(),
    )


def _patch_cap_token_valid():
    return patch(
        "aspire_orchestrator.routes.twilio_swap._validate_cap_token",
        return_value=None,
    )


def _patch_cap_token_missing():
    from fastapi import HTTPException, status
    return patch(
        "aspire_orchestrator.routes.twilio_swap._validate_cap_token",
        side_effect=HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_CAPABILITY_TOKEN"},
        ),
    )


def _patch_cap_token_wrong_scope():
    from fastapi import HTTPException, status
    return patch(
        "aspire_orchestrator.routes.twilio_swap._validate_cap_token",
        side_effect=HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "SCOPE_MISMATCH"},
        ),
    )


def _patch_cap_token_id():
    return patch(
        "aspire_orchestrator.routes.twilio_swap._cap_token_id",
        return_value="test-cap-token-id",
    )


def _patch_enqueue():
    return patch(
        "aspire_orchestrator.routes.twilio_swap._enqueue_advance_swap",
        new_callable=AsyncMock,
    )


def _make_trust_profile_row(trust_state: str = "number_attached") -> dict[str, Any]:
    return {
        "id": TRUST_PROFILE_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "trust_state": trust_state,
        "customer_profile_sid": "BUaaaaaaaaaaaaaaaaaaaaaaaaaaaaaCPSID",
    }


def _make_active_phone_row() -> dict[str, Any]:
    return {
        "id": PHONE_NUMBER_ID,
        "suite_id": SUITE_ID,
        "phone_number": "+14482885386",
        "twilio_sid": "PNaaaaaaaaaaaaaaaaaaaaaaaaaaaaOLD1",
        "status": "active",
    }


def _make_available_number() -> Any:
    m = MagicMock()
    m.phone_number = "+14155550199"
    return m


def _base_body_search() -> dict[str, Any]:
    return {
        "new_number_search": {"area_code": "415", "number_type": "LOCAL"},
        "release_old_number": True,
        "capability_token": {"token_id": "test", "scopes": ["telephony:swap_number"]},
    }


def _base_body_e164() -> dict[str, Any]:
    return {
        "new_number_e164": "+14155550199",
        "release_old_number": True,
        "capability_token": {"token_id": "test", "scopes": ["telephony:swap_number"]},
    }


def _patch_supabase_select_happy(in_flight: list[Any] | None = None) -> Any:
    return patch(
        "aspire_orchestrator.routes.twilio_swap.supabase_select",
        new_callable=AsyncMock,
        side_effect=[
            [_make_trust_profile_row()],     # load trust profile
            [_make_active_phone_row()],      # load active phone number
            in_flight if in_flight is not None else [],  # in-flight swap check
        ],
    )


def _patch_supabase_insert() -> Any:
    return patch(
        "aspire_orchestrator.routes.twilio_swap.supabase_insert",
        new_callable=AsyncMock,
        return_value={"id": SWAP_JOB_ID},
    )


# ---------------------------------------------------------------------------
# Test 1: Missing capability token → 401
# ---------------------------------------------------------------------------


def test_missing_capability_token_returns_401() -> None:
    with (
        _patch_scope(),
        _patch_cap_token_missing(),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=_base_body_e164(),
            headers=_HEADERS,
        )

    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "MISSING_CAPABILITY_TOKEN"


# ---------------------------------------------------------------------------
# Test 2: Wrong scope → 403
# ---------------------------------------------------------------------------


def test_wrong_scope_returns_403() -> None:
    with (
        _patch_scope(),
        _patch_cap_token_wrong_scope(),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=_base_body_e164(),
            headers=_HEADERS,
        )

    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "SCOPE_MISMATCH"


# ---------------------------------------------------------------------------
# Test 3: Tenant has no trust profile → 409 NO_TRUST_PROFILE
# ---------------------------------------------------------------------------


def test_no_trust_profile_returns_409() -> None:
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        patch(
            "aspire_orchestrator.routes.twilio_swap.supabase_select",
            new_callable=AsyncMock,
            return_value=[],  # no trust profile
        ),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=_base_body_e164(),
            headers=_HEADERS,
        )

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "NO_TRUST_PROFILE"


# ---------------------------------------------------------------------------
# Test 4: Trust profile exists but in wrong state → 409 PROFILE_NOT_READY_FOR_SWAP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trust_state", [
    "kyb_collected",
    "profile_submitted",
    "profile_approved",
    "shaken_submitted",
    "cnam_submitted",
])
def test_profile_not_in_number_attached_state_returns_409(trust_state: str) -> None:
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        patch(
            "aspire_orchestrator.routes.twilio_swap.supabase_select",
            new_callable=AsyncMock,
            return_value=[_make_trust_profile_row(trust_state=trust_state)],
        ),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=_base_body_e164(),
            headers=_HEADERS,
        )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "PROFILE_NOT_READY_FOR_SWAP"
    assert detail["trust_state"] == trust_state


# ---------------------------------------------------------------------------
# Test 5: No active phone number → 409 NO_ACTIVE_NUMBER
# ---------------------------------------------------------------------------


def test_no_active_phone_number_returns_409() -> None:
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        patch(
            "aspire_orchestrator.routes.twilio_swap.supabase_select",
            new_callable=AsyncMock,
            side_effect=[
                [_make_trust_profile_row()],  # trust profile
                [],                           # no active phone
            ],
        ),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=_base_body_e164(),
            headers=_HEADERS,
        )

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "NO_ACTIVE_NUMBER"


# ---------------------------------------------------------------------------
# Test 6: Swap already in progress → 409 SWAP_ALREADY_IN_PROGRESS
# ---------------------------------------------------------------------------


def test_swap_already_in_progress_returns_409() -> None:
    existing_swap = {"id": str(uuid.uuid4()), "status": "pending"}

    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        patch(
            "aspire_orchestrator.routes.twilio_swap.supabase_select",
            new_callable=AsyncMock,
            side_effect=[
                [_make_trust_profile_row()],
                [_make_active_phone_row()],
                [existing_swap],  # in-flight swap exists
            ],
        ),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=_base_body_e164(),
            headers=_HEADERS,
        )

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "SWAP_ALREADY_IN_PROGRESS"


# ---------------------------------------------------------------------------
# Test 7: Both search + e164 → 422 AMBIGUOUS_NUMBER_TARGET
# ---------------------------------------------------------------------------


def test_both_search_and_e164_returns_422() -> None:
    body = {
        "new_number_search": {"area_code": "415", "number_type": "LOCAL"},
        "new_number_e164": "+14155550199",
        "capability_token": {"token_id": "test"},
    }

    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        patch(
            "aspire_orchestrator.routes.twilio_swap.supabase_select",
            new_callable=AsyncMock,
            side_effect=[
                [_make_trust_profile_row()],
                [_make_active_phone_row()],
                [],
            ],
        ),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=body,
            headers=_HEADERS,
        )

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "AMBIGUOUS_NUMBER_TARGET"


# ---------------------------------------------------------------------------
# Test 8: Neither search nor e164 → 422 MISSING_NUMBER_TARGET
# ---------------------------------------------------------------------------


def test_neither_search_nor_e164_returns_422() -> None:
    body = {
        "capability_token": {"token_id": "test"},
    }

    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        patch(
            "aspire_orchestrator.routes.twilio_swap.supabase_select",
            new_callable=AsyncMock,
            side_effect=[
                [_make_trust_profile_row()],
                [_make_active_phone_row()],
                [],
            ],
        ),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=body,
            headers=_HEADERS,
        )

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "MISSING_NUMBER_TARGET"


# ---------------------------------------------------------------------------
# Test 9: Self-swap (same number) → 422 SAME_NUMBER
# ---------------------------------------------------------------------------


def test_self_swap_same_number_returns_422() -> None:
    body = {
        "new_number_e164": "+14482885386",  # same as current active
        "capability_token": {"token_id": "test"},
    }

    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        patch(
            "aspire_orchestrator.routes.twilio_swap.supabase_select",
            new_callable=AsyncMock,
            side_effect=[
                [_make_trust_profile_row()],
                [_make_active_phone_row()],
                [],
            ],
        ),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=body,
            headers=_HEADERS,
        )

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "SAME_NUMBER"


# ---------------------------------------------------------------------------
# Test 10: Happy path — e164 direct
# ---------------------------------------------------------------------------


def test_happy_path_e164_returns_202() -> None:
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        _patch_supabase_select_happy(),
        _patch_supabase_insert(),
        _patch_enqueue() as mock_enqueue,
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=_base_body_e164(),
            headers=_HEADERS,
        )

    assert resp.status_code == 202
    data = resp.json()
    assert "swap_job_id" in data
    assert data["old_number_e164"] == "+14482885386"
    assert data["new_number_e164"] == "+14155550199"
    assert "estimated_completion" in data
    mock_enqueue.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 11: Happy path — search mode
# ---------------------------------------------------------------------------


def test_happy_path_search_returns_202() -> None:
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        _patch_supabase_select_happy(),
        _patch_supabase_insert(),
        _patch_enqueue() as mock_enqueue,
        patch(
            "aspire_orchestrator.routes.twilio_swap.search_available_numbers",
            new_callable=AsyncMock,
            return_value=[_make_available_number()],
        ),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=_base_body_search(),
            headers=_HEADERS,
        )

    assert resp.status_code == 202
    data = resp.json()
    assert "swap_job_id" in data
    assert data["new_number_e164"] == "+14155550199"
    mock_enqueue.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 12: Number search returns empty → 422 NO_NUMBERS_AVAILABLE
# ---------------------------------------------------------------------------


def test_search_returns_empty_gives_422() -> None:
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        _patch_supabase_select_happy(),
        patch(
            "aspire_orchestrator.routes.twilio_swap.search_available_numbers",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=_base_body_search(),
            headers=_HEADERS,
        )

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "NO_NUMBERS_AVAILABLE"


# ---------------------------------------------------------------------------
# Test 13: Response shape — estimated_completion is parseable ISO8601
# ---------------------------------------------------------------------------


def test_estimated_completion_is_valid_iso8601() -> None:
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        _patch_supabase_select_happy(),
        _patch_supabase_insert(),
        _patch_enqueue(),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=_base_body_e164(),
            headers=_HEADERS,
        )

    data = resp.json()
    completion_str = data["estimated_completion"]
    # Must parse as ISO 8601
    parsed = datetime.fromisoformat(completion_str.replace("Z", "+00:00"))
    assert parsed > datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Test 14: DB unavailable on trust profile lookup → 503
# ---------------------------------------------------------------------------


def test_db_unavailable_on_profile_lookup_returns_503() -> None:
    from aspire_orchestrator.services.supabase_client import SupabaseClientError

    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        patch(
            "aspire_orchestrator.routes.twilio_swap.supabase_select",
            new_callable=AsyncMock,
            side_effect=SupabaseClientError("select/tenant_trust_profiles", detail="timeout"),
        ),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/twilio/swap-number",
            json=_base_body_e164(),
            headers=_HEADERS,
        )

    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "DB_UNAVAILABLE"


# ---------------------------------------------------------------------------
# GET /v1/twilio/swap-number/{swap_job_id}
# ---------------------------------------------------------------------------


_SWAP_HEADERS_AUTH: dict[str, str] = {
    **_HEADERS,
    "Authorization": "Bearer test-jwt",
}


def _swap_row(status_value: str = "in_progress", **overrides: Any) -> dict[str, Any]:
    base = {
        "id": SWAP_JOB_ID,
        "tenant_id": TENANT_ID,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "old_phone_number_id": PHONE_NUMBER_ID,
        "new_number_e164": "+14155550199",
        "release_old_number": True,
        "status": status_value,
        "reason_code": None,
        "progress": {
            "step_1_initiated_receipt": str(uuid.uuid4()),
            "step_2_new_phone_id": str(uuid.uuid4()),
            "step_3_cp_attached": True,
        },
        "created_at": "2026-05-04T12:00:00Z",
        "updated_at": "2026-05-04T12:01:00Z",
        "completed_at": None,
    }
    base.update(overrides)
    return base


def test_get_swap_status_without_bearer_returns_401() -> None:
    """W7-H1 mirror: read endpoint must reject anonymous access."""
    client = TestClient(app)
    resp = client.get(f"/v1/twilio/swap-number/{SWAP_JOB_ID}", headers=_HEADERS)
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason_code"] == "MISSING_BEARER_TOKEN"


def test_get_swap_status_returns_progress_on_in_flight_swap() -> None:
    swap = _swap_row(status_value="in_progress")
    phone_row = {"id": PHONE_NUMBER_ID, "phone_number": "+14482885386"}

    async def _select_side(table: str, filters: str, **_: Any) -> list[dict[str, Any]]:
        if table == "tenant_phone_swaps":
            return [swap]
        if table == "tenant_phone_numbers":
            return [phone_row]
        return []

    with _patch_scope(), patch(
        "aspire_orchestrator.routes.twilio_swap.supabase_select",
        new_callable=AsyncMock,
        side_effect=_select_side,
    ):
        client = TestClient(app)
        resp = client.get(
            f"/v1/twilio/swap-number/{SWAP_JOB_ID}", headers=_SWAP_HEADERS_AUTH
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["swap_job_id"] == SWAP_JOB_ID
    assert body["status"] == "in_progress"
    assert body["new_number_e164"] == "+14155550199"
    assert body["old_number_e164"] == "+14482885386"
    assert "step_1_initiated_receipt" in body["completed_steps"]
    # current_step is one past the highest completed step number
    assert body["current_step"] == "step_4"
    assert body["completed_at"] is None


def test_get_swap_status_returns_404_when_not_owned_by_suite() -> None:
    """Cross-tenant read: even with a valid swap_job_id, a different
    suite_id in headers must result in 404 (not 403 — we don't confirm
    existence of cross-tenant rows)."""
    async def _select_side(table: str, filters: str, **_: Any) -> list[dict[str, Any]]:
        # Filter is suite_id-scoped — wrong suite returns no rows.
        return []

    with _patch_scope(), patch(
        "aspire_orchestrator.routes.twilio_swap.supabase_select",
        new_callable=AsyncMock,
        side_effect=_select_side,
    ):
        client = TestClient(app)
        resp = client.get(
            f"/v1/twilio/swap-number/{SWAP_JOB_ID}", headers=_SWAP_HEADERS_AUTH
        )

    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "SWAP_NOT_FOUND"


def test_get_swap_status_terminal_succeeded_has_no_current_step() -> None:
    swap = _swap_row(
        status_value="succeeded",
        completed_at="2026-05-04T12:05:00Z",
        progress={
            "step_1_initiated_receipt": "r1",
            "step_2_new_phone_id": "p1",
            "step_3_cp_attached": True,
            "step_11_complete": True,
        },
    )
    phone_row = {"id": PHONE_NUMBER_ID, "phone_number": "+14482885386"}

    async def _select_side(table: str, filters: str, **_: Any) -> list[dict[str, Any]]:
        if table == "tenant_phone_swaps":
            return [swap]
        if table == "tenant_phone_numbers":
            return [phone_row]
        return []

    with _patch_scope(), patch(
        "aspire_orchestrator.routes.twilio_swap.supabase_select",
        new_callable=AsyncMock,
        side_effect=_select_side,
    ):
        client = TestClient(app)
        resp = client.get(
            f"/v1/twilio/swap-number/{SWAP_JOB_ID}", headers=_SWAP_HEADERS_AUTH
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "succeeded"
    # On terminal status, current_step is null even though there are completed steps
    assert body["current_step"] is None
    assert body["completed_at"] == "2026-05-04T12:05:00Z"
