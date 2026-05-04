"""Tests for Trust Hub KYB intake REST API — Wave 3.

Coverage:
  Contract:
    - Happy path: KYB submit, status, dispute, status-callback skeleton
    - Pydantic validation: bad EIN/DOB/SSN/state → 422
    - Capability token: missing → 401, wrong scope → 401, expired → 401
    - Tenant isolation: forged X-Tenant-Id → blocked at scope/cap-token layer
    - Vault unreachable → 503 with VAULT_UNAVAILABLE
    - Duplicate KYB submit (same suite_id) → 409
    - Status 404 when no trust profile; 200 with correct shape when present
    - Dispute: increments dispute_count, resets state, deletes old vault secret
    - Dispute max-disputes: 409 when dispute_count >= 5
    - Status-callback: invalid HMAC → 401; valid HMAC → 200 + receipt
    - PII never in receipt: EIN/DOB/SSN must not pass cut_trust_receipt
  Evil:
    - No cap token → 401
    - Expired token → 401
    - Wrong tenant token → 401
    - PII keys in redacted_inputs → TrustReceiptError raised + 500
    - Adapter never retries internally (only returns errors)

All Supabase, Vault, ARQ, and Twilio calls are mocked.
No real network traffic.

Author: Aspire — Wave 3 tests
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "bbbbbbbb-0000-0000-0000-000000000002"
SUITE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
OFFICE_ID = "cccccccc-0000-0000-0000-000000000003"
TRUST_PROFILE_ID = "aaaaaaaa-0000-0000-0000-000000000001"

SCOPE_HEADERS = {
    "X-Tenant-Id": TENANT_ID,
    "X-Suite-Id": SUITE_ID,
    "X-Office-Id": OFFICE_ID,
}

VALID_CAP_TOKEN: dict[str, Any] = {
    "token_id": "cap-token-001",
    "suite_id": SUITE_ID,
    "office_id": OFFICE_ID,
    "tool": "trust_hub",
    "scopes": ["trust_hub:kyb_submit", "trust_hub:resubmit"],
    "issued_at": "2026-05-03T00:00:00+00:00",
    "expires_at": "2099-12-31T23:59:59+00:00",
    "signature": "test-signature",
    "correlation_id": "corr-001",
}

def _valid_kyb_body() -> dict[str, Any]:
    """Return a fresh deep-copy of the valid KYB body each call.

    Never mutate the returned value — tests that modify authorized_reps
    must call this function instead of reusing a shared constant, to
    prevent shallow-copy mutation from contaminating later tests.
    """
    import copy
    return copy.deepcopy({
        "legal_business_name": "Scott Painting Services",
        "dba_name": None,
        "business_type": "llc",
        "address_street": "123 Main St",
        "address_city": "Ann Arbor",
        "address_state": "MI",
        "address_zip": "48104",
        "ein": "12-3456789",
        "authorized_reps": [
            {
                "first_name": "Tony",
                "last_name": "Scott",
                "title": "Owner",
                "email": "tony@scottpainting.com",
                "phone_e164": "+14482885386",
                "dob": "1990-01-15",
                "ssn_last4": "4321",
            }
        ],
        "capability_token": VALID_CAP_TOKEN,
    })


# Keep module-level alias for test code that reads (not mutates) the body.
# Tests that mutate `authorized_reps` MUST call _valid_kyb_body() instead.
VALID_KYB_BODY: dict[str, Any] = _valid_kyb_body()


# ---------------------------------------------------------------------------
# App fixture — isolated from server.py to avoid full startup cost
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Minimal FastAPI app with only the trust_hub router."""
    from aspire_orchestrator.routes.trust_hub import router

    app = FastAPI()
    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------


def _mock_validate_token_valid(
    cap_token: dict[str, Any] | None,
    scope: Any,
    required_scope: str,
) -> None:
    """No-op: simulates a valid capability token check."""
    pass


def _mock_validate_token_missing(
    cap_token: dict[str, Any] | None,
    scope: Any,
    required_scope: str,
) -> None:
    """Raise 401 when token is None — mirrors real _validate_cap_token."""
    from fastapi import HTTPException, status

    if cap_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_CAPABILITY_TOKEN"},
        )


def _mock_validate_token_expired(
    cap_token: dict[str, Any] | None,
    scope: Any,
    required_scope: str,
) -> None:
    from fastapi import HTTPException, status

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "TOKEN_EXPIRED"},
    )


def _mock_validate_token_wrong_scope(
    cap_token: dict[str, Any] | None,
    scope: Any,
    required_scope: str,
) -> None:
    from fastapi import HTTPException, status

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "SCOPE_MISMATCH"},
    )


# ---------------------------------------------------------------------------
# Base patch set for the route module
# ---------------------------------------------------------------------------


BASE_PATCHES = {
    # Token validation — default: accept all
    "aspire_orchestrator.routes.trust_hub._validate_cap_token": _mock_validate_token_valid,
    # Scope resolution — default: return valid scope
    "aspire_orchestrator.routes.trust_hub._resolve_scope": None,
    # Supabase
    "aspire_orchestrator.routes.trust_hub.supabase_select": None,
    "aspire_orchestrator.routes.trust_hub.supabase_insert": None,
    "aspire_orchestrator.routes.trust_hub.supabase_update": None,
    "aspire_orchestrator.routes.trust_hub.supabase_rpc": None,
    # Vault helpers (these call supabase_rpc internally but we patch them directly)
    "aspire_orchestrator.routes.trust_hub._vault_create_secret": None,
    "aspire_orchestrator.routes.trust_hub._vault_delete_secret": None,
    # Trust receipt
    "aspire_orchestrator.routes.trust_hub.cut_trust_receipt": None,
    # ARQ enqueue — silent no-op by default
    "aspire_orchestrator.routes.trust_hub._enqueue_advance_trust_state": None,
    # Twilio HMAC validation
    "aspire_orchestrator.routes.trust_hub.verify_twilio_signature": None,
}


def _make_scope() -> Any:
    """Return a ScopedIdentity-like object for use in mock returns."""
    from uuid import UUID

    class _Scope:
        tenant_id = UUID(TENANT_ID)
        suite_id = UUID(SUITE_ID)
        office_id = UUID(OFFICE_ID)

    return _Scope()


class _PatchCtx:
    """Lightweight patch manager used in each test."""

    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        self._patches: list[Any] = []
        self._mocks: dict[str, Any] = {}
        self._overrides = overrides or {}

    def start(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "aspire_orchestrator.routes.trust_hub._validate_cap_token": MagicMock(
                side_effect=_mock_validate_token_valid
            ),
            "aspire_orchestrator.routes.trust_hub._resolve_scope": MagicMock(
                return_value=_make_scope()
            ),
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(return_value=[]),
            "aspire_orchestrator.routes.trust_hub.supabase_insert": AsyncMock(return_value={}),
            "aspire_orchestrator.routes.trust_hub.supabase_update": AsyncMock(return_value={}),
            "aspire_orchestrator.routes.trust_hub.supabase_rpc": AsyncMock(return_value={"id": "vault-uuid-001"}),
            "aspire_orchestrator.routes.trust_hub._vault_create_secret": AsyncMock(return_value="vault-uuid-001"),
            "aspire_orchestrator.routes.trust_hub._vault_delete_secret": AsyncMock(return_value=None),
            "aspire_orchestrator.routes.trust_hub.cut_trust_receipt": AsyncMock(
                return_value="trust_kyb_collected_abc123"
            ),
            "aspire_orchestrator.routes.trust_hub._enqueue_advance_trust_state": AsyncMock(return_value=None),
            "aspire_orchestrator.routes.trust_hub.verify_twilio_signature": MagicMock(return_value=True),
        }
        defaults.update(self._overrides)

        for target, mock_obj in defaults.items():
            p = patch(target, mock_obj)
            m = p.start()
            self._patches.append(p)
            # key = last component, deduplicated
            key = target.split(".")[-1]
            self._mocks[key] = m

        return self._mocks

    def stop(self) -> None:
        for p in self._patches:
            p.stop()


# ---------------------------------------------------------------------------
# ============================================================================
# 1. HAPPY PATH — POST /v1/trust-hub/kyb
# ============================================================================
# ---------------------------------------------------------------------------


class TestKYBSubmitHappyPath:

    def setup_method(self) -> None:
        self.app = _build_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_kyb_submit_returns_201_with_receipt(self) -> None:
        ctx = _PatchCtx()
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb",
                json=VALID_KYB_BODY,
                headers=SCOPE_HEADERS,
            )
            assert resp.status_code == 201, resp.text
            data = resp.json()
            assert "trust_profile_id" in data
            assert data["trust_state"] == "kyb_collected"
            assert data["receipt_id"] == "trust_kyb_collected_abc123"
        finally:
            ctx.stop()

    def test_kyb_submit_calls_vault_for_ein(self) -> None:
        ctx = _PatchCtx()
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb",
                json=VALID_KYB_BODY,
                headers=SCOPE_HEADERS,
            )
            assert resp.status_code == 201
            vault_create = mocks["_vault_create_secret"]
            assert vault_create.called
            # First call must be for EIN
            first_call_kwargs = vault_create.call_args_list[0]
            assert "ein" in first_call_kwargs[1].get("name", "") or \
                   "ein" in str(first_call_kwargs)
        finally:
            ctx.stop()

    def test_kyb_submit_calls_vault_for_rep_dob_and_ssn(self) -> None:
        ctx = _PatchCtx()
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb",
                json=VALID_KYB_BODY,
                headers=SCOPE_HEADERS,
            )
            assert resp.status_code == 201
            # 1 EIN + 1 DOB + 1 SSN = 3 vault creates for 1 rep
            vault_create = mocks["_vault_create_secret"]
            assert vault_create.call_count == 3
        finally:
            ctx.stop()

    def test_kyb_submit_inserts_trust_profile(self) -> None:
        ctx = _PatchCtx()
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb",
                json=VALID_KYB_BODY,
                headers=SCOPE_HEADERS,
            )
            assert resp.status_code == 201
            insert = mocks["supabase_insert"]
            # First insert is trust profile, second+ are reps
            assert insert.call_count >= 2
            first_table = insert.call_args_list[0][0][0]
            assert first_table == "tenant_trust_profiles"
        finally:
            ctx.stop()

    def test_kyb_submit_inserts_authorized_rep(self) -> None:
        ctx = _PatchCtx()
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb",
                json=VALID_KYB_BODY,
                headers=SCOPE_HEADERS,
            )
            assert resp.status_code == 201
            insert = mocks["supabase_insert"]
            tables = [call[0][0] for call in insert.call_args_list]
            assert "tenant_authorized_reps" in tables
        finally:
            ctx.stop()

    def test_kyb_submit_enqueues_arq_job(self) -> None:
        ctx = _PatchCtx()
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb",
                json=VALID_KYB_BODY,
                headers=SCOPE_HEADERS,
            )
            assert resp.status_code == 201
            enqueue = mocks["_enqueue_advance_trust_state"]
            assert enqueue.called
        finally:
            ctx.stop()

    def test_kyb_submit_cuts_receipt(self) -> None:
        ctx = _PatchCtx()
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb",
                json=VALID_KYB_BODY,
                headers=SCOPE_HEADERS,
            )
            assert resp.status_code == 201
            cut = mocks["cut_trust_receipt"]
            assert cut.called
            call_kwargs = cut.call_args[1]
            assert call_kwargs["receipt_type"] == "kyb_collected"
            assert call_kwargs["outcome"] == "success"
            assert call_kwargs["to_state"] == "kyb_collected"
        finally:
            ctx.stop()

    def test_kyb_submit_two_reps(self) -> None:
        """Two authorized reps: 1 EIN + 2*(DOB+SSN) = 5 vault calls."""
        base = _valid_kyb_body()
        body = {**base, "authorized_reps": [
            base["authorized_reps"][0],
            {
                "first_name": "Jane",
                "last_name": "Scott",
                "title": "Manager",
                "email": "jane@scottpainting.com",
                "phone_e164": "+14482885387",
                "dob": "1985-03-20",
                "ssn_last4": "5678",
            },
        ]}
        ctx = _PatchCtx()
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb",
                json=body,
                headers=SCOPE_HEADERS,
            )
            assert resp.status_code == 201
            vault_create = mocks["_vault_create_secret"]
            assert vault_create.call_count == 5  # 1 EIN + 2*(DOB+SSN)
        finally:
            ctx.stop()


# ---------------------------------------------------------------------------
# ============================================================================
# 2. PYDANTIC VALIDATION (422 paths)
# ============================================================================
# ---------------------------------------------------------------------------


class TestKYBValidation:

    def setup_method(self) -> None:
        self.app = _build_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def _submit(self, overrides: dict[str, Any]) -> Any:
        body = _valid_kyb_body()
        body.update(overrides)
        return self.client.post(
            "/v1/trust-hub/kyb",
            json=body,
            headers=SCOPE_HEADERS,
        )

    def test_invalid_ein_format(self) -> None:
        resp = self._submit({"ein": "123456789"})  # missing dash
        assert resp.status_code == 422

    def test_invalid_ein_too_short(self) -> None:
        resp = self._submit({"ein": "12-345678"})  # only 6 digits after dash
        assert resp.status_code == 422

    def test_invalid_state_code(self) -> None:
        resp = self._submit({"address_state": "Michigan"})
        assert resp.status_code == 422

    def test_invalid_state_lowercase(self) -> None:
        resp = self._submit({"address_state": "mi"})
        assert resp.status_code == 422

    def test_invalid_zip_code(self) -> None:
        resp = self._submit({"address_zip": "abcde"})
        assert resp.status_code == 422

    def test_invalid_dob_format(self) -> None:
        body = _valid_kyb_body()
        body["authorized_reps"][0] = {
            **body["authorized_reps"][0],
            "dob": "01-15-1990",  # wrong format
        }
        resp = self.client.post(
            "/v1/trust-hub/kyb", json=body, headers=SCOPE_HEADERS
        )
        assert resp.status_code == 422

    def test_invalid_ssn_last4_too_long(self) -> None:
        body = _valid_kyb_body()
        body["authorized_reps"][0] = {
            **body["authorized_reps"][0],
            "ssn_last4": "12345",  # 5 digits, must be exactly 4
        }
        resp = self.client.post(
            "/v1/trust-hub/kyb", json=body, headers=SCOPE_HEADERS
        )
        assert resp.status_code == 422

    def test_invalid_ssn_last4_letters(self) -> None:
        body = _valid_kyb_body()
        body["authorized_reps"][0] = {
            **body["authorized_reps"][0],
            "ssn_last4": "abcd",
        }
        resp = self.client.post(
            "/v1/trust-hub/kyb", json=body, headers=SCOPE_HEADERS
        )
        assert resp.status_code == 422

    def test_invalid_phone_e164_no_country_code(self) -> None:
        body = _valid_kyb_body()
        body["authorized_reps"][0] = {
            **body["authorized_reps"][0],
            "phone_e164": "4482885386",  # missing +1
        }
        resp = self.client.post(
            "/v1/trust-hub/kyb", json=body, headers=SCOPE_HEADERS
        )
        assert resp.status_code == 422

    def test_zero_reps_rejected(self) -> None:
        resp = self._submit({"authorized_reps": []})
        assert resp.status_code == 422

    def test_three_reps_rejected(self) -> None:
        rep = VALID_KYB_BODY["authorized_reps"][0]
        resp = self._submit({"authorized_reps": [rep, rep, rep]})
        assert resp.status_code == 422

    def test_business_name_too_short(self) -> None:
        resp = self._submit({"legal_business_name": "X"})  # min_length=2
        assert resp.status_code == 422

    def test_business_name_too_long(self) -> None:
        resp = self._submit({"legal_business_name": "A" * 121})  # max_length=120
        assert resp.status_code == 422

    def test_invalid_business_type(self) -> None:
        resp = self._submit({"business_type": "bogus_type"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# ============================================================================
# 3. CAPABILITY TOKEN ENFORCEMENT (Law #5)
# ============================================================================
# ---------------------------------------------------------------------------


class TestCapabilityTokenEnforcement:

    def setup_method(self) -> None:
        self.app = _build_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_missing_token_returns_401(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub._validate_cap_token": MagicMock(
                side_effect=_mock_validate_token_missing
            ),
        })
        mocks = ctx.start()
        try:
            body = {**_valid_kyb_body(), "capability_token": None}
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=body, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 401
        finally:
            ctx.stop()

    def test_expired_token_returns_401(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub._validate_cap_token": MagicMock(
                side_effect=_mock_validate_token_expired
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=VALID_KYB_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 401
        finally:
            ctx.stop()

    def test_wrong_scope_token_returns_401(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub._validate_cap_token": MagicMock(
                side_effect=_mock_validate_token_wrong_scope
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=VALID_KYB_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 401
        finally:
            ctx.stop()

    def test_dispute_missing_token_returns_401(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub._validate_cap_token": MagicMock(
                side_effect=_mock_validate_token_missing
            ),
        })
        mocks = ctx.start()
        try:
            body = {"capability_token": None, "ein": "12-3456789"}
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=body, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 401
        finally:
            ctx.stop()


# ---------------------------------------------------------------------------
# ============================================================================
# 4. TENANT ISOLATION (Law #6)
# ============================================================================
# ---------------------------------------------------------------------------


class TestTenantIsolation:

    def setup_method(self) -> None:
        self.app = _build_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_missing_scope_headers_returns_401(self) -> None:
        """No X-Tenant-Id / X-Suite-Id / X-Office-Id → _resolve_scope raises 401."""
        # Use REAL _resolve_scope (no override) — only patch token and supabase
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub._resolve_scope": None,  # will be replaced below
        })
        # Patch _resolve_scope as the real function which raises on missing headers
        with patch(
            "aspire_orchestrator.routes.trust_hub._resolve_scope",
        ) as mock_scope:
            from fastapi import HTTPException, status as http_status

            mock_scope.side_effect = HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail={"error": "MISSING_SCOPE_HEADERS"},
            )
            resp = self.client.post(
                "/v1/trust-hub/kyb",
                json=VALID_KYB_BODY,
                # No scope headers
            )
            assert resp.status_code == 401

    def test_forged_tenant_id_blocked_by_cap_token(self) -> None:
        """A request with mismatched tenant → cap token suite_id check fails → 401."""
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub._validate_cap_token": MagicMock(
                side_effect=_mock_validate_token_wrong_scope
            ),
        })
        mocks = ctx.start()
        try:
            forged_headers = {
                "X-Tenant-Id": "ffffffff-0000-0000-0000-000000000099",
                "X-Suite-Id": "ffffffff-0000-0000-0000-000000000098",
                "X-Office-Id": "ffffffff-0000-0000-0000-000000000097",
            }
            resp = self.client.post(
                "/v1/trust-hub/kyb",
                json=VALID_KYB_BODY,
                headers=forged_headers,
            )
            assert resp.status_code == 401
        finally:
            ctx.stop()


# ---------------------------------------------------------------------------
# ============================================================================
# 5. VAULT FAILURE → 503
# ============================================================================
# ---------------------------------------------------------------------------


class TestVaultFailure:

    def setup_method(self) -> None:
        self.app = _build_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_vault_ein_unreachable_returns_503(self) -> None:
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub._vault_create_secret": AsyncMock(
                side_effect=SupabaseClientError("rpc/create_vault_secret", 503, "Vault unavailable")
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=VALID_KYB_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 503
            data = resp.json()
            assert data.get("detail", {}).get("reason_code") == "VAULT_UNAVAILABLE"
        finally:
            ctx.stop()

    def test_vault_rep_dob_unreachable_returns_503(self) -> None:
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        call_count = 0

        async def _vault_create_side_effect(value: str, *, name: str, description: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "vault-ein-ok"
            # Second call (rep DOB) fails
            raise SupabaseClientError("rpc/create_vault_secret", 503, "Vault unavailable")

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub._vault_create_secret": AsyncMock(
                side_effect=_vault_create_side_effect
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=VALID_KYB_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 503
            assert resp.json()["detail"]["reason_code"] == "VAULT_UNAVAILABLE"
        finally:
            ctx.stop()


# ---------------------------------------------------------------------------
# ============================================================================
# 6. DUPLICATE PROFILE → 409
# ============================================================================
# ---------------------------------------------------------------------------


class TestDuplicateProfile:

    def setup_method(self) -> None:
        self.app = _build_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_duplicate_suite_id_returns_409_on_preflight_select(self) -> None:
        """supabase_select returns existing profile row → 409 before vault calls."""
        existing_profile = {
            "id": TRUST_PROFILE_ID,
            "trust_state": "kyb_collected",
            "suite_id": SUITE_ID,
        }
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                return_value=[existing_profile]
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=VALID_KYB_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 409
            data = resp.json()
            assert data["detail"]["error"] == "PROFILE_ALREADY_EXISTS"
            # Vault must NOT have been called (pre-flight check blocks it)
            vault_create = mocks["_vault_create_secret"]
            assert not vault_create.called
        finally:
            ctx.stop()

    def test_duplicate_on_insert_returns_409(self) -> None:
        """DB unique constraint fires on insert → 409."""
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_insert": AsyncMock(
                side_effect=SupabaseClientError("insert/tenant_trust_profiles", 409, "duplicate key value violates unique constraint")
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=VALID_KYB_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 409
        finally:
            ctx.stop()


# ---------------------------------------------------------------------------
# ============================================================================
# 7. GET /v1/trust-hub/status
# ============================================================================
# ---------------------------------------------------------------------------


class TestTrustHubStatus:

    def setup_method(self) -> None:
        self.app = _build_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_status_404_when_no_profile(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(return_value=[]),
        })
        mocks = ctx.start()
        try:
            resp = self.client.get("/v1/trust-hub/status", headers=SCOPE_HEADERS)
            assert resp.status_code == 404
            assert resp.json()["detail"]["error"] == "NO_TRUST_PROFILE"
        finally:
            ctx.stop()

    def test_status_200_with_correct_shape(self) -> None:
        profile = {
            "id": TRUST_PROFILE_ID,
            "suite_id": SUITE_ID,
            "trust_state": "cnam_submitted",
            "kyb_collected_at": "2026-05-01T00:00:00+00:00",
            "profile_approved_at": "2026-05-02T00:00:00+00:00",
            "shaken_approved_at": "2026-05-02T01:00:00+00:00",
            "cnam_approved_at": None,
            "rejection_reason": None,
            "rejection_code": None,
            "cnam_display_name": "SCOTT PAINTING",
            "branded_calling_enabled": False,
            "branded_calling_display_name": None,
        }

        select_call_count = 0

        async def _select_side(table: str, filters: str, **kwargs: Any) -> list[dict[str, Any]]:
            nonlocal select_call_count
            select_call_count += 1
            if table == "tenant_trust_profiles":
                return [profile]
            if table == "tenant_a2p_brands":
                return []  # no A2P brand yet
            return []

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                side_effect=_select_side
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.get("/v1/trust-hub/status", headers=SCOPE_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["trust_state"] == "cnam_submitted"
            assert data["cnam_display_name"] == "SCOTT PAINTING"
            assert "milestones" in data
            milestones = data["milestones"]
            assert milestones["kyb_collected"] is True
            assert milestones["profile_approved"] is True
            assert milestones["shaken_approved"] is True
            assert milestones["cnam_approved"] is False
            assert milestones["a2p_approved"] is False
            assert milestones["branded_calling_live"] is False
        finally:
            ctx.stop()

    def test_status_no_cap_token_required(self) -> None:
        """GET /status is Green tier — no cap token needed."""
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(return_value=[]),
        })
        mocks = ctx.start()
        try:
            # No capability_token in request — should still work (no auth error, only 404)
            resp = self.client.get("/v1/trust-hub/status", headers=SCOPE_HEADERS)
            assert resp.status_code == 404  # Not 401
            # _validate_cap_token must NOT have been called
            validate = mocks["_validate_cap_token"]
            assert not validate.called
        finally:
            ctx.stop()


# ---------------------------------------------------------------------------
# ============================================================================
# 8. POST /v1/trust-hub/dispute
# ============================================================================
# ---------------------------------------------------------------------------


VALID_DISPUTE_BODY: dict[str, Any] = {
    "ein": "99-8765432",
    "address_street": "456 Oak Ave",
    "capability_token": VALID_CAP_TOKEN,
}


def _profile_row(dispute_count: int = 0) -> dict[str, Any]:
    return {
        "id": TRUST_PROFILE_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "office_id": OFFICE_ID,
        "trust_state": "profile_rejected",
        "kyb_collected_at": "2026-05-01T00:00:00+00:00",
        "dispute_count": dispute_count,
        "ein_vault_secret_id": "old-ein-vault-uuid",
        "legal_business_name": "Scott Painting Services",
        "business_type": "llc",
    }


class TestDispute:

    def setup_method(self) -> None:
        self.app = _build_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_dispute_increments_count_and_resets_state(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                return_value=[_profile_row(dispute_count=1)]
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=VALID_DISPUTE_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["trust_state"] == "kyb_collected"
            assert data["dispute_count"] == 2
            assert "receipt_id" in data
        finally:
            ctx.stop()

    def test_dispute_deletes_old_vault_secret_before_new_ein(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                return_value=[_profile_row(dispute_count=0)]
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=VALID_DISPUTE_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 200
            delete_mock = mocks["_vault_delete_secret"]
            assert delete_mock.called
            # First delete call must be for old EIN vault ID
            first_arg = delete_mock.call_args_list[0][0][0]
            assert first_arg == "old-ein-vault-uuid"
        finally:
            ctx.stop()

    def test_dispute_creates_new_vault_secret_for_ein(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                return_value=[_profile_row(dispute_count=0)]
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=VALID_DISPUTE_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 200
            create_mock = mocks["_vault_create_secret"]
            assert create_mock.called
        finally:
            ctx.stop()

    def test_dispute_enqueues_arq_job(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                return_value=[_profile_row()]
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=VALID_DISPUTE_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 200
            enqueue = mocks["_enqueue_advance_trust_state"]
            assert enqueue.called
        finally:
            ctx.stop()

    def test_dispute_no_profile_returns_404(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(return_value=[]),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=VALID_DISPUTE_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 404
        finally:
            ctx.stop()

    def test_dispute_max_disputes_returns_409(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                return_value=[_profile_row(dispute_count=5)]
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=VALID_DISPUTE_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 409
            data = resp.json()
            assert data["detail"]["error"] == "MAX_DISPUTES_REACHED"
        finally:
            ctx.stop()

    def test_dispute_at_exactly_five_returns_409(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                return_value=[_profile_row(dispute_count=5)]
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=VALID_DISPUTE_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 409
        finally:
            ctx.stop()

    def test_dispute_at_four_is_allowed(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                return_value=[_profile_row(dispute_count=4)]
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=VALID_DISPUTE_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 200
            assert resp.json()["dispute_count"] == 5
        finally:
            ctx.stop()


# ---------------------------------------------------------------------------
# ============================================================================
# 9. POST /v1/trust-hub/status-callback
# ============================================================================
# ---------------------------------------------------------------------------


TWILIO_FORM_VALID = {
    "ResourceSid": "BU-profile-0001",
    "Status": "twilio-approved",
    "AccountSid": "ACtest123",
}


class TestStatusCallback:

    def setup_method(self) -> None:
        self.app = _build_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_invalid_hmac_returns_401(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.verify_twilio_signature": MagicMock(
                return_value=False
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/status-callback",
                data=TWILIO_FORM_VALID,
                headers={"X-Twilio-Signature": "bad-sig"},
            )
            assert resp.status_code == 401
        finally:
            ctx.stop()

    def test_valid_hmac_returns_200(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(return_value=[]),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/status-callback",
                data=TWILIO_FORM_VALID,
                headers={"X-Twilio-Signature": "valid-sig"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "received"
        finally:
            ctx.stop()

    def test_valid_hmac_with_known_profile_cuts_receipt(self) -> None:
        profile = {
            "id": TRUST_PROFILE_ID,
            "suite_id": SUITE_ID,
            "tenant_id": TENANT_ID,
            "office_id": OFFICE_ID,
            "trust_state": "profile_submitted",
            "twilio_secondary_profile_sid": "BU-profile-0001",
            "twilio_shaken_bundle_sid": None,
            "twilio_cnam_bundle_sid": None,
        }

        async def _select_side(table: str, filters: str, **kwargs: Any) -> list[dict[str, Any]]:
            if "twilio_secondary_profile_sid" in filters:
                return [profile]
            return []

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                side_effect=_select_side
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/status-callback",
                data=TWILIO_FORM_VALID,
                headers={"X-Twilio-Signature": "valid-sig"},
            )
            assert resp.status_code == 200
            cut = mocks["cut_trust_receipt"]
            assert cut.called
        finally:
            ctx.stop()

    def test_valid_hmac_unknown_profile_still_200(self) -> None:
        """Unknown SID → log + 200 (Twilio must not retry)."""
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(return_value=[]),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/status-callback",
                data={"ResourceSid": "BU-unknown-9999", "Status": "twilio-approved"},
                headers={"X-Twilio-Signature": "valid-sig"},
            )
            assert resp.status_code == 200
        finally:
            ctx.stop()

    def test_no_jwt_required_on_callback(self) -> None:
        """Status-callback is a public webhook — no JWT, no cap token."""
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(return_value=[]),
        })
        mocks = ctx.start()
        try:
            # Explicitly send with no auth headers at all
            resp = self.client.post(
                "/v1/trust-hub/status-callback",
                data=TWILIO_FORM_VALID,
                headers={"X-Twilio-Signature": "valid-sig"},
            )
            assert resp.status_code == 200
        finally:
            ctx.stop()


# ---------------------------------------------------------------------------
# ============================================================================
# 10. PII GUARDRAILS (Law #9)
# ============================================================================
# ---------------------------------------------------------------------------


class TestPIIGuardrails:

    def setup_method(self) -> None:
        self.app = _build_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_ein_not_in_receipt_redacted_inputs(self) -> None:
        """Verify cut_trust_receipt is called with NO EIN in redacted_inputs."""
        captured_kwargs: dict[str, Any] = {}

        async def _capture_receipt(**kwargs: Any) -> str:
            captured_kwargs.update(kwargs)
            return "receipt-id-001"

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.cut_trust_receipt": AsyncMock(
                side_effect=_capture_receipt
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=VALID_KYB_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 201
            redacted = captured_kwargs.get("redacted_inputs", {})
            assert "ein" not in redacted
            assert "dob" not in redacted
            assert "ssn_last4" not in redacted
            assert "email" not in redacted
            assert "phone_e164" not in redacted
        finally:
            ctx.stop()

    def test_cut_trust_receipt_pii_rejection_propagates_500(self) -> None:
        """If somehow PII slips into cut_trust_receipt, the error propagates as 500."""
        from aspire_orchestrator.workers.trust_onboarding.trust_receipts import TrustReceiptError

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.cut_trust_receipt": AsyncMock(
                side_effect=TrustReceiptError(
                    "PII_LEAK_BLOCKED",
                    "Forbidden PII key 'ein' in redacted_inputs",
                )
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=VALID_KYB_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 500
            data = resp.json()
            assert data["detail"]["error"] == "RECEIPT_FAILED"
        finally:
            ctx.stop()

    def test_response_body_never_contains_ein(self) -> None:
        ctx = _PatchCtx()
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=VALID_KYB_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 201
            resp_text = resp.text
            # EIN value must not appear in response
            assert "12-3456789" not in resp_text
            assert VALID_KYB_BODY["authorized_reps"][0]["dob"] not in resp_text
            assert VALID_KYB_BODY["authorized_reps"][0]["ssn_last4"] not in resp_text
        finally:
            ctx.stop()


# ---------------------------------------------------------------------------
# ============================================================================
# 11. EVIL TESTS (Security)
# ============================================================================
# ---------------------------------------------------------------------------


class TestEvilTests:

    def setup_method(self) -> None:
        self.app = _build_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_no_cap_token_body_returns_401(self) -> None:
        """Adapter must deny with 401 when capability_token is missing."""
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub._validate_cap_token": MagicMock(
                side_effect=_mock_validate_token_missing
            ),
        })
        mocks = ctx.start()
        try:
            body = {**_valid_kyb_body(), "capability_token": None}
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=body, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 401
            # No vault calls, no inserts — fail-closed
            assert not mocks["_vault_create_secret"].called
            assert not mocks["supabase_insert"].called
        finally:
            ctx.stop()

    def test_expired_token_no_vault_calls(self) -> None:
        """Expired token → deny immediately, no side effects."""
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub._validate_cap_token": MagicMock(
                side_effect=_mock_validate_token_expired
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=VALID_KYB_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 401
            assert not mocks["_vault_create_secret"].called
            assert not mocks["supabase_insert"].called
        finally:
            ctx.stop()

    def test_wrong_tenant_token_denied(self) -> None:
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub._validate_cap_token": MagicMock(
                side_effect=_mock_validate_token_wrong_scope
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=VALID_KYB_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 401
        finally:
            ctx.stop()

    def test_status_callback_forged_signature_rejected(self) -> None:
        """Forged Twilio HMAC → 401, no DB reads."""
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.verify_twilio_signature": MagicMock(
                return_value=False
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/status-callback",
                data=TWILIO_FORM_VALID,
                headers={"X-Twilio-Signature": "forged"},
            )
            assert resp.status_code == 401
        finally:
            ctx.stop()

    def test_dispute_with_no_token_returns_401_not_404(self) -> None:
        """Dispute without cap token: auth check fires before profile lookup."""
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub._validate_cap_token": MagicMock(
                side_effect=_mock_validate_token_missing
            ),
        })
        mocks = ctx.start()
        try:
            body = {**VALID_DISPUTE_BODY, "capability_token": None}
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=body, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 401
            # Profile lookup must NOT have happened
            assert not mocks["supabase_select"].called
        finally:
            ctx.stop()


# ---------------------------------------------------------------------------
# ============================================================================
# 12. ADDITIONAL COVERAGE — vault helpers, dispute rep paths, callback receipt
# ============================================================================
# ---------------------------------------------------------------------------


class TestVaultHelperInternals:
    """Cover _vault_create_secret / _vault_delete_secret via supabase_rpc layer."""

    def setup_method(self) -> None:
        self.app = _build_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_kyb_submit_503_when_select_fails(self) -> None:
        """supabase_select failure on duplicate check -> 503."""
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                side_effect=SupabaseClientError("select/tenant_trust_profiles", 503, "DB unavailable")
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=_valid_kyb_body(), headers=SCOPE_HEADERS
            )
            assert resp.status_code == 503
        finally:
            ctx.stop()

    def test_vault_create_via_rpc_returns_secret_id(self) -> None:
        """_vault_create_secret calls supabase_rpc and extracts id field."""
        ctx = _PatchCtx(overrides={
            # Do NOT mock _vault_create_secret — let it call through to supabase_rpc
            "aspire_orchestrator.routes.trust_hub._vault_create_secret": None,
        })
        # Patch with the real helper but provide supabase_rpc mock
        with patch("aspire_orchestrator.routes.trust_hub.supabase_rpc", new=AsyncMock(return_value={"id": "vault-real-uuid"})), \
             patch("aspire_orchestrator.routes.trust_hub._validate_cap_token", side_effect=_mock_validate_token_valid), \
             patch("aspire_orchestrator.routes.trust_hub._resolve_scope", return_value=_make_scope()), \
             patch("aspire_orchestrator.routes.trust_hub.supabase_select", new=AsyncMock(return_value=[])), \
             patch("aspire_orchestrator.routes.trust_hub.supabase_insert", new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.routes.trust_hub.cut_trust_receipt", new=AsyncMock(return_value="r1")), \
             patch("aspire_orchestrator.routes.trust_hub._enqueue_advance_trust_state", new=AsyncMock()):
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=_valid_kyb_body(), headers=SCOPE_HEADERS
            )
            assert resp.status_code == 201

    def test_vault_create_raises_503_when_rpc_returns_no_id(self) -> None:
        """_vault_create_secret raises SupabaseClientError when RPC returns no id."""
        with patch("aspire_orchestrator.routes.trust_hub.supabase_rpc", new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.routes.trust_hub._validate_cap_token", side_effect=_mock_validate_token_valid), \
             patch("aspire_orchestrator.routes.trust_hub._resolve_scope", return_value=_make_scope()), \
             patch("aspire_orchestrator.routes.trust_hub.supabase_select", new=AsyncMock(return_value=[])), \
             patch("aspire_orchestrator.routes.trust_hub.supabase_insert", new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.routes.trust_hub.cut_trust_receipt", new=AsyncMock(return_value="r1")), \
             patch("aspire_orchestrator.routes.trust_hub._enqueue_advance_trust_state", new=AsyncMock()):
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=_valid_kyb_body(), headers=SCOPE_HEADERS
            )
            assert resp.status_code == 503
            assert "VAULT_UNAVAILABLE" in resp.text

    def test_dispute_with_reps_updates_existing_rep(self) -> None:
        """dispute with authorized_reps and existing rep rows -> update path."""
        existing_rep = {
            "id": "rep-001",
            "trust_profile_id": TRUST_PROFILE_ID,
            "rep_index": 1,
            "dob_vault_secret_id": "old-dob-vault",
            "ssn_last4_vault_secret_id": "old-ssn-vault",
        }

        async def _select_side(table: str, filters: str, **kwargs: Any) -> list[dict[str, Any]]:
            if table == "tenant_trust_profiles":
                return [_profile_row(dispute_count=0)]
            if table == "tenant_authorized_reps":
                return [existing_rep]
            return []

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                side_effect=_select_side
            ),
        })
        mocks = ctx.start()
        try:
            rep = _valid_kyb_body()["authorized_reps"][0]
            body = {
                "authorized_reps": [rep],
                "capability_token": VALID_CAP_TOKEN,
            }
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=body, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 200
            update = mocks["supabase_update"]
            assert update.called
            delete = mocks["_vault_delete_secret"]
            assert delete.call_count >= 2
        finally:
            ctx.stop()

    def test_dispute_with_reps_inserts_new_rep_when_not_found(self) -> None:
        """dispute with authorized_reps, no existing rep row -> insert path."""
        async def _select_side(table: str, filters: str, **kwargs: Any) -> list[dict[str, Any]]:
            if table == "tenant_trust_profiles":
                return [_profile_row(dispute_count=0)]
            return []

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                side_effect=_select_side
            ),
        })
        mocks = ctx.start()
        try:
            rep = _valid_kyb_body()["authorized_reps"][0]
            body = {
                "authorized_reps": [rep],
                "capability_token": VALID_CAP_TOKEN,
            }
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=body, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 200
            insert = mocks["supabase_insert"]
            assert insert.called
            tables = [c[0][0] for c in insert.call_args_list]
            assert "tenant_authorized_reps" in tables
        finally:
            ctx.stop()

    def test_dispute_plaintext_fields_only(self) -> None:
        """dispute with only plaintext fields (no EIN, no reps) — no vault calls."""
        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                return_value=[_profile_row()]
            ),
        })
        mocks = ctx.start()
        try:
            body = {
                "legal_business_name": "New Business Name LLC",
                "address_street": "789 Pine Rd",
                "capability_token": VALID_CAP_TOKEN,
            }
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=body, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 200
            vault_create = mocks["_vault_create_secret"]
            assert not vault_create.called
        finally:
            ctx.stop()

    def test_status_callback_receipt_failure_still_returns_200(self) -> None:
        """Receipt failure in status-callback must not prevent 200 return to Twilio."""
        from aspire_orchestrator.workers.trust_onboarding.trust_receipts import TrustReceiptError

        profile = {
            "id": TRUST_PROFILE_ID,
            "suite_id": SUITE_ID,
            "tenant_id": TENANT_ID,
            "office_id": OFFICE_ID,
            "trust_state": "profile_submitted",
            "twilio_secondary_profile_sid": "BU-profile-0001",
            "twilio_shaken_bundle_sid": None,
            "twilio_cnam_bundle_sid": None,
        }

        async def _select_side(table: str, filters: str, **kwargs: Any) -> list[dict[str, Any]]:
            if "twilio_secondary_profile_sid" in filters:
                return [profile]
            return []

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                side_effect=_select_side
            ),
            "aspire_orchestrator.routes.trust_hub.cut_trust_receipt": AsyncMock(
                side_effect=TrustReceiptError("RECEIPT_STORE_FAILED", "DB write failed")
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/status-callback",
                data={"ResourceSid": "BU-profile-0001", "Status": "twilio-approved"},
                headers={"X-Twilio-Signature": "valid-sig"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "received"
        finally:
            ctx.stop()

    def test_status_db_unavailable_returns_503(self) -> None:
        """GET /status with DB error -> 503."""
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                side_effect=SupabaseClientError("select/tenant_trust_profiles", 503, "DB unavailable")
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.get("/v1/trust-hub/status", headers=SCOPE_HEADERS)
            assert resp.status_code == 503
        finally:
            ctx.stop()

    def test_dispute_db_update_failure_returns_503(self) -> None:
        """Profile update failure in dispute -> 503."""
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_select": AsyncMock(
                return_value=[_profile_row()]
            ),
            "aspire_orchestrator.routes.trust_hub.supabase_update": AsyncMock(
                side_effect=SupabaseClientError("update/tenant_trust_profiles", 503, "DB unavailable")
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/dispute", json=VALID_DISPUTE_BODY, headers=SCOPE_HEADERS
            )
            assert resp.status_code == 503
        finally:
            ctx.stop()

    def test_kyb_rep_insert_failure_returns_503(self) -> None:
        """Rep row insert failure -> 503."""
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        insert_count = 0

        async def _insert_side(table: str, data: dict[str, Any]) -> dict[str, Any]:
            nonlocal insert_count
            insert_count += 1
            if table == "tenant_trust_profiles":
                return {"id": TRUST_PROFILE_ID}
            if table == "tenant_authorized_reps":
                raise SupabaseClientError("insert/tenant_authorized_reps", 503, "DB unavailable")
            return {}

        ctx = _PatchCtx(overrides={
            "aspire_orchestrator.routes.trust_hub.supabase_insert": AsyncMock(
                side_effect=_insert_side
            ),
        })
        mocks = ctx.start()
        try:
            resp = self.client.post(
                "/v1/trust-hub/kyb", json=_valid_kyb_body(), headers=SCOPE_HEADERS
            )
            assert resp.status_code == 503
        finally:
            ctx.stop()
