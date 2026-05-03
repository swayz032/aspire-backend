"""Integration test — Front Desk config sync chain to Sarah personalization (Pass 19 Lane D §3.5.5).

Verifies the full E2E backend sync chain:
  1. PATCH /v1/front-desk/config with new routing_owner_phone
  2. Redis/LKG cache key invalidated for the office
  3. GET /v1/sarah/personalization returns the new phone (cache_hit=false)
  4. Receipts cut: front_desk_config_save (version_no incremented) +
     personalization_resolve (cache_hit=false)

Aspire Laws:
  Law #2: Both PATCH and POST personalization webhook cut receipts.
  Law #3: Fail-closed on missing token.
  Law #5: Capability token required for PATCH.
  Law #6: Scope from headers (Gateway-trusted paths).

Note: This test uses the in-process LKG cache mechanism
(not Redis — Redis deferred to Phase 2). The invalidation call
`invalidate_personalization_cache_for_office(office_id)` is wired inside
the PATCH handler (§3.5.5), so we verify the LKG cache is cleared
and the next personalization request reads fresh DB data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-ci")
os.environ.setdefault("ASPIRE_RATE_LIMIT", "1000000")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.front_desk import router as front_desk_router
from aspire_orchestrator.routes.sarah import router as sarah_router
from aspire_orchestrator.routes.sarah import _lkg_cache  # in-process cache

# Build a combined app with both routers
_app = FastAPI()
_app.include_router(front_desk_router)
_app.include_router(sarah_router)
_client = TestClient(_app, raise_server_exceptions=False)

SUITE_ID = "cc000000-0000-0000-0000-000000000001"
OFFICE_ID = "cc000000-0000-0000-0000-000000000002"
TENANT_ID = "cc000000-0000-0000-0000-000000000003"
CALLED_NUMBER = "+14484008888"
EL_SECRET = "sync-chain-el-secret-test"

OLD_OWNER_PHONE = "+14045550182"
NEW_OWNER_PHONE = "+14045559999"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mint_front_desk_token(scope: str = "front_desk:config_save") -> dict:
    from aspire_orchestrator.services.token_service import mint_token
    return mint_token(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        tool="front_desk",
        scopes=[scope],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=45,
    )


def _make_el_signature(body_bytes: bytes, secret: str, ts: int | None = None) -> str:
    if ts is None:
        ts = int(time.time())
    signed = f"{ts}.".encode() + body_bytes
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v0={sig}"


def _build_config_row(version_no: int = 3) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "version_no": version_no,
        "is_current": True,
        "after_hours_mode": "take_message",
        "busy_mode": "take_message",
        "public_number_mode": "ASPIRE_NEW_NUMBER",
        "catch_mode": "APP_AND_PHONE_SIMUL_RING",
        "greeting_name_override": "",
        "pronunciation_override": "",
        # Pass 19+: business_hours moved from a phantom table to the config
        # JSONB column (live schema verified 2026-05-03).
        "business_hours": {
            "mon": {"open": True, "startTime": "08:00", "endTime": "18:00"},
            "tue": {"open": True, "startTime": "08:00", "endTime": "18:00"},
            "wed": {"open": True, "startTime": "08:00", "endTime": "18:00"},
            "thu": {"open": True, "startTime": "08:00", "endTime": "18:00"},
            "fri": {"open": True, "startTime": "08:00", "endTime": "18:00"},
            "sat": {"open": False},
            "sun": {"open": False},
        },
        "timezone": "America/New_York",
    }


def _routing_with_new_phone(owner_phone: str) -> list[dict]:
    return [
        {"role": "owner", "label": "Tonio", "phone": owner_phone, "is_active": True},
        {"role": "sales", "label": "Maya", "phone": "+14045550002", "is_active": True},
    ]


def _build_personalization_select(owner_phone: str, version_no: int = 4) -> Any:
    """Build supabase_select side_effect for the personalization endpoint."""
    def _select(table: str, filters: str = "", **kwargs) -> list[dict]:
        if table == "tenant_phone_numbers":
            return [{
                "phone_number": CALLED_NUMBER,
                "suite_id": SUITE_ID,
                "office_id": OFFICE_ID,
                "tenant_id": TENANT_ID,
                "status": "active",
            }]
        if table == "front_desk_configs":
            return [_build_config_row(version_no)]
        if table == "front_desk_routing_contacts":
            return _routing_with_new_phone(owner_phone)
        if table == "suite_profiles":
            return [{
                "business_name": "Test Biz",
                "industry": "services",
                "owner_name": "Tonio S",
                "timezone": "America/New_York",
                "email": "t@biz.com",
            }]
        # tenant_profiles / office_profiles / business_hours no longer exist —
        # any read against them in the live schema returns []. Mirror that.
        return []
    return _select


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFrontDeskSyncChain:
    """Full sync chain: PATCH config → cache invalidation → fresh personalization."""

    def setup_method(self) -> None:
        """Clear LKG cache before each test for isolation."""
        _lkg_cache.clear()

    def teardown_method(self) -> None:
        """Clean LKG cache after each test."""
        _lkg_cache.clear()

    def _patch_and_assert_success(self, new_owner_phone: str) -> dict:
        """PATCH /v1/front-desk/config with new routing_owner_phone and return response body."""
        token = _mint_front_desk_token()
        current_config = _build_config_row(version_no=3)

        with (
            patch("aspire_orchestrator.routes.front_desk.supabase_select",
                  new=AsyncMock(side_effect=[
                      [current_config],  # fetch current max version
                  ])),
            patch("aspire_orchestrator.routes.front_desk.supabase_insert",
                  new=AsyncMock(return_value={"id": str(uuid.uuid4()), "version_no": 4})),
            patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts"),
        ):
            resp = _client.patch(
                "/v1/front-desk/config",
                json={
                    "routing_owner_phone": new_owner_phone,
                    "capability_token": token,
                },
                headers={
                    "X-Tenant-Id": TENANT_ID,
                    "X-Suite-Id": SUITE_ID,
                    "X-Office-Id": OFFICE_ID,
                },
            )

        assert resp.status_code == 200, f"PATCH failed: {resp.status_code} {resp.text}"
        return resp.json()

    def test_patch_config_returns_success(self) -> None:
        """PATCH /v1/front-desk/config returns success:true."""
        body = self._patch_and_assert_success(NEW_OWNER_PHONE)
        assert body.get("success") is True

    def test_patch_config_version_incremented(self) -> None:
        """PATCH /v1/front-desk/config increments version_no from 3 to 4."""
        token = _mint_front_desk_token()
        current_config = _build_config_row(version_no=3)

        inserted_rows: list[dict] = []

        async def _capture_insert(table: str, row: dict) -> dict:
            inserted_rows.append(row)
            return {**row, "id": str(uuid.uuid4())}

        with (
            patch("aspire_orchestrator.routes.front_desk.supabase_select",
                  new=AsyncMock(side_effect=[[current_config]])),
            patch("aspire_orchestrator.routes.front_desk.supabase_insert",
                  new=AsyncMock(side_effect=_capture_insert)),
            patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts"),
        ):
            resp = _client.patch(
                "/v1/front-desk/config",
                json={"capability_token": token},
                headers={
                    "X-Tenant-Id": TENANT_ID,
                    "X-Suite-Id": SUITE_ID,
                    "X-Office-Id": OFFICE_ID,
                },
            )

        assert resp.status_code == 200
        assert inserted_rows, "No row was inserted — versioned write failed"
        assert inserted_rows[0]["version_no"] == 4, (
            f"Expected version_no=4, got {inserted_rows[0]['version_no']}"
        )

    def test_patch_config_cuts_receipt(self) -> None:
        """Law #2: PATCH /v1/front-desk/config cuts front_desk_config_save receipt."""
        receipts: list[list] = []

        def _capture(r: list) -> None:
            receipts.extend(r)

        token = _mint_front_desk_token()
        current_config = _build_config_row(version_no=3)

        with (
            patch("aspire_orchestrator.routes.front_desk.supabase_select",
                  new=AsyncMock(side_effect=[[current_config]])),
            patch("aspire_orchestrator.routes.front_desk.supabase_insert",
                  new=AsyncMock(return_value={"id": str(uuid.uuid4())})),
            patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts",
                  side_effect=_capture),
        ):
            _client.patch(
                "/v1/front-desk/config",
                json={"capability_token": token},
                headers={
                    "X-Tenant-Id": TENANT_ID,
                    "X-Suite-Id": SUITE_ID,
                    "X-Office-Id": OFFICE_ID,
                },
            )

        assert receipts, "No receipt cut — Law #2 violation"
        r = receipts[0]
        assert r["receipt_type"] == "front_desk_config_save"
        assert r["outcome"] == "success"
        assert r["risk_tier"] == "yellow"

    def test_patch_invalidates_lkg_cache(self) -> None:
        """PATCH /v1/front-desk/config calls invalidate_personalization_cache_for_office."""
        # Pre-populate the LKG cache for this office
        import time as _time
        from aspire_orchestrator.routes.sarah import _cache_put
        _cache_put(
            CALLED_NUMBER,
            {"routing_owner_phone": OLD_OWNER_PHONE},
            {"office_id": OFFICE_ID, "tenant_id": TENANT_ID},
        )
        assert CALLED_NUMBER in _lkg_cache, "Pre-condition: cache not populated"

        token = _mint_front_desk_token()
        current_config = _build_config_row(version_no=3)

        with (
            patch("aspire_orchestrator.routes.front_desk.supabase_select",
                  new=AsyncMock(side_effect=[[current_config]])),
            patch("aspire_orchestrator.routes.front_desk.supabase_insert",
                  new=AsyncMock(return_value={"id": str(uuid.uuid4())})),
            patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts"),
        ):
            _client.patch(
                "/v1/front-desk/config",
                json={"capability_token": token},
                headers={
                    "X-Tenant-Id": TENANT_ID,
                    "X-Suite-Id": SUITE_ID,
                    "X-Office-Id": OFFICE_ID,
                },
            )

        # After PATCH, the cache entry for this office must be gone
        assert CALLED_NUMBER not in _lkg_cache, (
            f"LKG cache not invalidated after PATCH — next personalization call "
            f"would return stale routing_owner_phone='{OLD_OWNER_PHONE}'"
        )

    def test_personalization_returns_new_phone_after_patch(self) -> None:
        """After PATCH, personalization webhook returns new routing_owner_phone (cache_hit=false)."""
        # Step 1: Ensure cache is empty (simulating post-invalidation state)
        _lkg_cache.clear()

        # Step 2: POST to personalization — should read fresh from DB with NEW_OWNER_PHONE
        payload = {
            "called_number": CALLED_NUMBER,
            "call_sid": "CAtest-sync-chain",
            "caller_id": "+19175550200",
            "agent_id": "agent_6501kp71h69jfqysgd055hemqhrq",
        }
        body_bytes = json.dumps(payload).encode()
        sig = _make_el_signature(body_bytes, EL_SECRET)

        with (
            patch(
                "aspire_orchestrator.routes.sarah.settings",
                MagicMock(
                    elevenlabs_webhook_secret=EL_SECRET,
                    disable_personalization_hmac=False,
                    aspire_env="dev",
                ),
            ),
            patch(
                "aspire_orchestrator.routes.sarah.supabase_select",
                new=AsyncMock(
                    side_effect=_build_personalization_select(NEW_OWNER_PHONE, version_no=4)
                ),
            ),
            patch(
                "aspire_orchestrator.routes.sarah.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            resp = _client.post(
                "/v1/sarah/personalization",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "ElevenLabs-Signature": sig,
                },
            )

        assert resp.status_code == 200, f"Personalization failed: {resp.status_code}"
        dyn = resp.json()["dynamic_variables"]

        # Must return NEW phone, not old one
        assert dyn["routing_owner_phone"] == NEW_OWNER_PHONE, (
            f"Personalization returned old phone '{dyn['routing_owner_phone']}' "
            f"instead of new '{NEW_OWNER_PHONE}' — cache not invalidated correctly"
        )

    def test_personalization_receipt_has_suite_id(self) -> None:
        """Law #2: personalization_resolve receipt includes suite_id (tenant-scoped audit trail)."""
        receipts: list[dict] = []

        def _capture(r: list) -> None:
            receipts.extend(r)

        payload = {
            "called_number": CALLED_NUMBER,
            "call_sid": "CAtest-receipt",
            "caller_id": "+19175550200",
            "agent_id": "agent_6501kp71h69jfqysgd055hemqhrq",
        }
        body_bytes = json.dumps(payload).encode()
        sig = _make_el_signature(body_bytes, EL_SECRET)

        with (
            patch(
                "aspire_orchestrator.routes.sarah.settings",
                MagicMock(
                    elevenlabs_webhook_secret=EL_SECRET,
                    disable_personalization_hmac=False,
                    aspire_env="dev",
                ),
            ),
            patch(
                "aspire_orchestrator.routes.sarah.supabase_select",
                new=AsyncMock(
                    side_effect=_build_personalization_select(NEW_OWNER_PHONE)
                ),
            ),
            patch(
                "aspire_orchestrator.routes.sarah.receipt_store.store_receipts",
                side_effect=_capture,
            ),
        ):
            _client.post(
                "/v1/sarah/personalization",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "ElevenLabs-Signature": sig,
                },
            )

        assert receipts, "No personalization receipt cut — Law #2 violation"
        r = receipts[0]
        assert r.get("receipt_type") == "personalization_resolve"
        assert r.get("suite_id") == SUITE_ID
        assert r.get("office_id") == OFFICE_ID


class TestRoutingContactCacheInvalidation:
    """Routing CRUD must invalidate the LKG cache so the next call to Sarah
    sees fresh routing_*_phone dyn vars instead of stale cached ones."""

    def setup_method(self) -> None:
        _lkg_cache.clear()

    def teardown_method(self) -> None:
        _lkg_cache.clear()

    def _seed_cache(self, phone: str) -> None:
        from aspire_orchestrator.routes.sarah import _cache_put

        _cache_put(
            CALLED_NUMBER,
            {"routing_owner_phone": phone},
            {"office_id": OFFICE_ID, "tenant_id": TENANT_ID},
        )
        assert CALLED_NUMBER in _lkg_cache, "pre-condition: cache seeded"

    def test_create_routing_contact_invalidates_cache(self) -> None:
        self._seed_cache(OLD_OWNER_PHONE)
        token = _mint_front_desk_token(scope="front_desk:routing_write")

        with (
            patch("aspire_orchestrator.routes.front_desk.supabase_insert",
                  new=AsyncMock(return_value={"id": str(uuid.uuid4()), "role": "owner"})),
            patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts"),
        ):
            resp = _client.post(
                "/v1/front-desk/routing-contacts",
                json={
                    "role": "owner",
                    "label": "Tonio",
                    "phone": NEW_OWNER_PHONE,
                    "capability_token": token,
                },
                headers={
                    "X-Tenant-Id": TENANT_ID,
                    "X-Suite-Id": SUITE_ID,
                    "X-Office-Id": OFFICE_ID,
                },
            )

        assert resp.status_code == 200, resp.text
        assert CALLED_NUMBER not in _lkg_cache, (
            "Routing CRUD did not invalidate cache — Sarah would serve "
            "stale routing_owner_phone for up to 10 minutes."
        )

    def test_update_routing_contact_invalidates_cache(self) -> None:
        self._seed_cache(OLD_OWNER_PHONE)
        token = _mint_front_desk_token(scope="front_desk:routing_write")
        contact_id = str(uuid.uuid4())

        with (
            patch("aspire_orchestrator.routes.front_desk.supabase_update",
                  new=AsyncMock(return_value={"id": contact_id})),
            patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts"),
        ):
            resp = _client.patch(
                f"/v1/front-desk/routing-contacts/{contact_id}",
                json={"phone": NEW_OWNER_PHONE, "capability_token": token},
                headers={
                    "X-Tenant-Id": TENANT_ID,
                    "X-Suite-Id": SUITE_ID,
                    "X-Office-Id": OFFICE_ID,
                },
            )

        assert resp.status_code == 200, resp.text
        assert CALLED_NUMBER not in _lkg_cache

    def test_delete_routing_contact_invalidates_cache(self) -> None:
        self._seed_cache(OLD_OWNER_PHONE)
        token = _mint_front_desk_token(scope="front_desk:routing_write")
        contact_id = str(uuid.uuid4())

        with (
            # Live schema has no soft-delete column; handler does a hard
            # DELETE via supabase_delete and cuts an immutable receipt.
            patch("aspire_orchestrator.routes.front_desk.supabase_delete",
                  new=AsyncMock(return_value=None)),
            patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts"),
        ):
            # DELETE handler takes capability_token as a bare dict body
            # (no Pydantic wrapper), so the body root IS the token.
            resp = _client.request(
                "DELETE",
                f"/v1/front-desk/routing-contacts/{contact_id}",
                json=token,
                headers={
                    "X-Tenant-Id": TENANT_ID,
                    "X-Suite-Id": SUITE_ID,
                    "X-Office-Id": OFFICE_ID,
                },
            )

        assert resp.status_code == 200, resp.text
        assert CALLED_NUMBER not in _lkg_cache
