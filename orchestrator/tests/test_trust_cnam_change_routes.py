"""Tests for Wave 9 — POST /v1/trust-hub/cnam-change route.

Covers:
  - Missing capability token → 401
  - Wrong scope → 403
  - Cooldown not met (last_cnam_change_at < 30d ago) → 409 COOLDOWN_NOT_MET
  - Display name fails sanitization (only special chars) → 422 INVALID_DISPLAY_NAME
  - No trust profile → 409 NO_TRUST_PROFILE
  - Trust state not in allowed set → 409 PROFILE_NOT_READY_FOR_CHANGE
  - Happy path (NULL last_cnam_change_at) → 202 with request_id + sanitized name
  - Happy path (cooldown satisfied: change > 30 days ago) → 202
  - Duplicate in-flight change → 409 CHANGE_ALREADY_IN_PROGRESS
  - Receipt-side guarantee: sanitized name returned ≤ 15 chars

Author: Aspire — Wave 9
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.trust_cnam_change import router

# ---------------------------------------------------------------------------
# Test app setup
# ---------------------------------------------------------------------------

app = FastAPI()
app.include_router(router)

SUITE_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())
OFFICE_ID = str(uuid.uuid4())
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
    return type("Scope", (), {
        "suite_id": uuid.UUID(SUITE_ID),
        "tenant_id": uuid.UUID(TENANT_ID),
        "office_id": uuid.UUID(OFFICE_ID),
    })()


def _patch_scope() -> Any:
    return patch(
        "aspire_orchestrator.routes.trust_cnam_change._resolve_scope",
        return_value=_mock_scope(),
    )


def _patch_cap_token_valid() -> Any:
    return patch(
        "aspire_orchestrator.routes.trust_cnam_change._validate_cap_token",
        return_value=None,
    )


def _patch_cap_token_missing() -> Any:
    from fastapi import HTTPException, status as fa_status
    return patch(
        "aspire_orchestrator.routes.trust_cnam_change._validate_cap_token",
        side_effect=HTTPException(
            status_code=fa_status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_CAPABILITY_TOKEN"},
        ),
    )


def _patch_cap_token_wrong_scope() -> Any:
    from fastapi import HTTPException, status as fa_status
    return patch(
        "aspire_orchestrator.routes.trust_cnam_change._validate_cap_token",
        side_effect=HTTPException(
            status_code=fa_status.HTTP_403_FORBIDDEN,
            detail={"error": "SCOPE_MISMATCH"},
        ),
    )


def _patch_cap_token_id() -> Any:
    return patch(
        "aspire_orchestrator.routes.trust_cnam_change._cap_token_id",
        return_value="test-cap-token-id",
    )


def _patch_enqueue() -> Any:
    return patch(
        "aspire_orchestrator.routes.trust_cnam_change._enqueue_apply_cnam_change",
        new_callable=AsyncMock,
        return_value=True,
    )


def _make_profile_row(
    trust_state: str = "number_attached",
    last_cnam_change_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": TRUST_PROFILE_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "trust_state": trust_state,
        "cnam_end_user_sid": "ITaaaaaaaaaaaaaaaaaaaaaaaaaaaaCNAMEU",
        "cnam_trust_product_sid": "BUaaaaaaaaaaaaaaaaaaaaaaaaaaaaCNAMTP",
        "last_cnam_change_at": last_cnam_change_at,
    }


def _patch_supabase_select_profile(profile_rows: list[dict[str, Any]]) -> Any:
    return patch(
        "aspire_orchestrator.routes.trust_cnam_change.supabase_select",
        new_callable=AsyncMock,
        return_value=profile_rows,
    )


def _patch_supabase_insert(success: bool = True) -> Any:
    if success:
        return patch(
            "aspire_orchestrator.routes.trust_cnam_change.supabase_insert",
            new_callable=AsyncMock,
            return_value={"id": str(uuid.uuid4())},
        )
    from aspire_orchestrator.services.supabase_client import SupabaseClientError
    return patch(
        "aspire_orchestrator.routes.trust_cnam_change.supabase_insert",
        new_callable=AsyncMock,
        side_effect=SupabaseClientError(
            "insert/tenant_cnam_change_requests",
            409,
            "duplicate key value violates unique constraint",
        ),
    )


def _body(name: str = "Scott Painting Pro") -> dict[str, Any]:
    return {
        "new_display_name": name,
        "capability_token": {
            "token_id": "test",
            "scopes": ["trust_hub:cnam_change"],
        },
    }


# ---------------------------------------------------------------------------
# Test 1: Missing capability token → 401
# ---------------------------------------------------------------------------


def test_missing_capability_token_returns_401() -> None:
    with _patch_scope(), _patch_cap_token_missing():
        client = TestClient(app)
        resp = client.post("/v1/trust-hub/cnam-change", json=_body(), headers=_HEADERS)
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "MISSING_CAPABILITY_TOKEN"


# ---------------------------------------------------------------------------
# Test 2: Wrong scope → 403
# ---------------------------------------------------------------------------


def test_wrong_scope_returns_403() -> None:
    with _patch_scope(), _patch_cap_token_wrong_scope():
        client = TestClient(app)
        resp = client.post("/v1/trust-hub/cnam-change", json=_body(), headers=_HEADERS)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "SCOPE_MISMATCH"


# ---------------------------------------------------------------------------
# Test 3: Cooldown not met → 409 COOLDOWN_NOT_MET
# ---------------------------------------------------------------------------


def test_cooldown_not_met_returns_409() -> None:
    recent_change = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        _patch_supabase_select_profile([_make_profile_row(last_cnam_change_at=recent_change)]),
    ):
        client = TestClient(app)
        resp = client.post("/v1/trust-hub/cnam-change", json=_body(), headers=_HEADERS)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "COOLDOWN_NOT_MET"
    assert detail["cooldown_days"] == 30
    assert "next_eligible_at" in detail


# ---------------------------------------------------------------------------
# Test 4: Display name fails sanitization → 422
# ---------------------------------------------------------------------------


def test_invalid_display_name_returns_422() -> None:
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
    ):
        client = TestClient(app)
        # Pass a name that's >= 2 chars (passes Pydantic) but sanitizer rejects.
        resp = client.post(
            "/v1/trust-hub/cnam-change",
            json=_body(name="!@"),  # only special chars
            headers=_HEADERS,
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_DISPLAY_NAME"


# ---------------------------------------------------------------------------
# Test 5: No trust profile → 409
# ---------------------------------------------------------------------------


def test_no_trust_profile_returns_409() -> None:
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        _patch_supabase_select_profile([]),
    ):
        client = TestClient(app)
        resp = client.post("/v1/trust-hub/cnam-change", json=_body(), headers=_HEADERS)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "NO_TRUST_PROFILE"


# ---------------------------------------------------------------------------
# Test 6: Trust state not allowed → 409
# ---------------------------------------------------------------------------


def test_trust_state_not_allowed_returns_409() -> None:
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        _patch_supabase_select_profile([_make_profile_row(trust_state="cnam_submitted")]),
    ):
        client = TestClient(app)
        resp = client.post("/v1/trust-hub/cnam-change", json=_body(), headers=_HEADERS)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "PROFILE_NOT_READY_FOR_CHANGE"


# ---------------------------------------------------------------------------
# Test 7: Happy path — NULL last_cnam_change_at → 202
# ---------------------------------------------------------------------------


def test_happy_path_null_last_change_returns_202() -> None:
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        _patch_supabase_select_profile([_make_profile_row(last_cnam_change_at=None)]),
        _patch_supabase_insert(),
        _patch_enqueue() as enqueue_mock,
    ):
        client = TestClient(app)
        resp = client.post("/v1/trust-hub/cnam-change", json=_body(), headers=_HEADERS)

    assert resp.status_code == 202
    body = resp.json()
    assert "request_id" in body
    assert body["sanitized_display_name"] == "SCOTT PAINTING"
    assert "estimated_completion" in body
    enqueue_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 8: Happy path — change > 30 days ago → 202
# ---------------------------------------------------------------------------


def test_happy_path_old_change_satisfies_cooldown() -> None:
    old_change = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        _patch_supabase_select_profile([_make_profile_row(last_cnam_change_at=old_change)]),
        _patch_supabase_insert(),
        _patch_enqueue(),
    ):
        client = TestClient(app)
        resp = client.post("/v1/trust-hub/cnam-change", json=_body(), headers=_HEADERS)

    assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Test 9: Duplicate in-flight change → 409 CHANGE_ALREADY_IN_PROGRESS
# ---------------------------------------------------------------------------


def test_duplicate_in_flight_returns_409() -> None:
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        _patch_supabase_select_profile([_make_profile_row(last_cnam_change_at=None)]),
        _patch_supabase_insert(success=False),
    ):
        client = TestClient(app)
        resp = client.post("/v1/trust-hub/cnam-change", json=_body(), headers=_HEADERS)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "CHANGE_ALREADY_IN_PROGRESS"


# ---------------------------------------------------------------------------
# Test 10: Sanitized name is <= 15 chars and starts with a letter
# ---------------------------------------------------------------------------


def test_sanitized_name_is_15_chars_max() -> None:
    """Long business names must be truncated to 15 chars, starting with a letter."""
    with (
        _patch_scope(),
        _patch_cap_token_valid(),
        _patch_cap_token_id(),
        _patch_supabase_select_profile([_make_profile_row(last_cnam_change_at=None)]),
        _patch_supabase_insert(),
        _patch_enqueue(),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/trust-hub/cnam-change",
            json=_body(name="Mom's Pet Grooming, Incorporated"),
            headers=_HEADERS,
        )
    assert resp.status_code == 202
    sanitized = resp.json()["sanitized_display_name"]
    assert 1 <= len(sanitized) <= 15
    assert sanitized[0].isalpha()


# ---------------------------------------------------------------------------
# Test 11: Pydantic length validation — 1-char name → 422
# ---------------------------------------------------------------------------


def test_pydantic_length_validation_too_short() -> None:
    """Pydantic rejects names < 2 chars before route logic runs."""
    with _patch_scope(), _patch_cap_token_valid():
        client = TestClient(app)
        resp = client.post(
            "/v1/trust-hub/cnam-change",
            json={
                "new_display_name": "A",
                "capability_token": {"token_id": "t", "scopes": ["trust_hub:cnam_change"]},
            },
            headers=_HEADERS,
        )
    assert resp.status_code == 422
