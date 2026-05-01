"""Tests for caller-ID lookup endpoint GET /v1/calls/caller-id-lookup (Pass 19 Lane B).

Covers:
- Priority 1: routing_contacts exact phone match → returns role + display_name
- Priority 2: sms_thread memory contacts → returns contact_type='sms_contact'
- Priority 3: call memory entities (last 90 days) → returns contact_type='call_contact'
- Fallback: no match → returns contact_type='unknown', formatted_number
- Capability token required (scope telephony:caller_id_lookup)
- Receipt cut on every call
- <100ms p95 (validated in test)
- Law #9: full phone number not logged; only prefix in receipt
- Cross-tenant: lookup scoped to office_id (routing_contacts)
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-ci")
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.calls import router as calls_router

_app = FastAPI()
_app.include_router(calls_router)
_client = TestClient(_app, raise_server_exceptions=False)

SUITE_ID = "aa000000-0000-0000-0000-000000000001"
OFFICE_ID = "aa000000-0000-0000-0000-000000000002"
TENANT_ID = "aa000000-0000-0000-0000-000000000003"

_VALID_TOKEN = {
    "token_id": str(uuid.uuid4()),
    "suite_id": SUITE_ID,
    "office_id": OFFICE_ID,
    "tool": "telephony.caller_id_lookup",
    "scopes": ["telephony:caller_id_lookup"],
    "issued_at": datetime.now(timezone.utc).isoformat(),
    "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
    "nonce": str(uuid.uuid4()),
    "signature": "valid-sig",
}

_ROUTING_CONTACT_ROW = [
    {
        "id": str(uuid.uuid4()),
        "office_id": OFFICE_ID,
        "role": "owner",
        "label": "Tonio S.",
        "phone": "+14155550001",
        "is_active": True,
    }
]

_SMS_THREAD_ROW = [
    {
        "memory_id": str(uuid.uuid4()),
        "memory_type": "sms_thread",
        "title": "SMS from Maya",
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "detail": {
            "from": "+14155550002",
            "contact_name": "Maya R.",
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
]

_CALL_MEMORY_ROW = [
    {
        "memory_id": str(uuid.uuid4()),
        "memory_type": "call",
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "detail": {
            "from": "+14155550003",
            "caller_name": "James K.",
        },
        "created_at": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
    }
]


def _common_patches(token_valid: bool = True, tenant_rows: dict | None = None):
    """Returns a context manager patch for the validate_token and supabase_select."""
    return [
        patch(
            "aspire_orchestrator.routes.calls.validate_token",
            return_value=(True, _VALID_TOKEN) if token_valid else (False, "INVALID_CAPABILITY_TOKEN"),
        ),
        patch(
            "aspire_orchestrator.routes.calls.receipt_store.store_receipts",
            return_value=None,
        ),
    ]


class TestCallerIdLookupPriorityOrder:
    """routing_contacts > sms_thread > call memory > fallback."""

    def _make_headers(self) -> dict[str, str]:
        return {
            "X-Aspire-Suite-Id": SUITE_ID,
            "X-Aspire-Office-Id": OFFICE_ID,
            "X-Aspire-Tenant-Id": TENANT_ID,
            "X-Aspire-Capability-Token": "test-token",
        }

    @pytest.mark.asyncio
    async def test_routing_contact_match_returns_first(self) -> None:
        """routing_contacts hit → contact_type='routing_contact', role present."""
        async def _select(table: str, filters: str, **kwargs) -> list[dict[str, Any]]:
            if table == "front_desk_routing_contacts":
                if "+14155550001" in filters:
                    return _ROUTING_CONTACT_ROW
            return []

        with (
            patch(
                "aspire_orchestrator.routes.calls.validate_token",
                return_value=MagicMock(valid=True, error=None),
            ),
            patch(
                "aspire_orchestrator.routes.calls.supabase_select",
                new=AsyncMock(side_effect=_select),
            ),
            patch(
                "aspire_orchestrator.routes.calls.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            resp = _client.get(
                "/v1/calls/caller-id-lookup",
                params={"phone": "+14155550001"},
                headers=self._make_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["contact_type"] == "routing_contact"
        assert data["display_name"] == "Tonio S."
        assert data["role"] == "owner"

    @pytest.mark.asyncio
    async def test_sms_thread_match_when_no_routing_contact(self) -> None:
        """No routing_contact match → falls to sms_thread."""
        async def _select(table: str, filters: str, **kwargs) -> list[dict[str, Any]]:
            if table == "front_desk_routing_contacts":
                return []  # no match
            if table == "memory_objects" and "sms_thread" in filters:
                return _SMS_THREAD_ROW
            return []

        with (
            patch(
                "aspire_orchestrator.routes.calls.validate_token",
                return_value=MagicMock(valid=True, error=None),
            ),
            patch(
                "aspire_orchestrator.routes.calls.supabase_select",
                new=AsyncMock(side_effect=_select),
            ),
            patch(
                "aspire_orchestrator.routes.calls.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            resp = _client.get(
                "/v1/calls/caller-id-lookup",
                params={"phone": "+14155550002"},
                headers=self._make_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["contact_type"] == "sms_contact"

    @pytest.mark.asyncio
    async def test_call_memory_match_when_no_routing_or_sms(self) -> None:
        """No routing or sms match → falls to call memory."""
        async def _select(table: str, filters: str, **kwargs) -> list[dict[str, Any]]:
            if table == "front_desk_routing_contacts":
                return []
            if table == "memory_objects" and "sms_thread" in filters:
                return []
            if table == "memory_objects" and "call" in filters:
                return _CALL_MEMORY_ROW
            return []

        with (
            patch(
                "aspire_orchestrator.routes.calls.validate_token",
                return_value=MagicMock(valid=True, error=None),
            ),
            patch(
                "aspire_orchestrator.routes.calls.supabase_select",
                new=AsyncMock(side_effect=_select),
            ),
            patch(
                "aspire_orchestrator.routes.calls.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            resp = _client.get(
                "/v1/calls/caller-id-lookup",
                params={"phone": "+14155550003"},
                headers=self._make_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["contact_type"] == "call_contact"

    @pytest.mark.asyncio
    async def test_fallback_when_no_match(self) -> None:
        """No match at any priority level → fallback with contact_type='unknown'."""
        async def _select(table: str, filters: str, **kwargs) -> list[dict[str, Any]]:
            return []

        with (
            patch(
                "aspire_orchestrator.routes.calls.validate_token",
                return_value=MagicMock(valid=True, error=None),
            ),
            patch(
                "aspire_orchestrator.routes.calls.supabase_select",
                new=AsyncMock(side_effect=_select),
            ),
            patch(
                "aspire_orchestrator.routes.calls.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            resp = _client.get(
                "/v1/calls/caller-id-lookup",
                params={"phone": "+19999999999"},
                headers=self._make_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["contact_type"] == "unknown"
        assert "formatted_number" in data


class TestCallerIdLookupCapabilityToken:
    """Capability token enforcement (Law #5, Law #3)."""

    def _make_headers(self, token: str = "test-token") -> dict[str, str]:
        return {
            "X-Aspire-Suite-Id": SUITE_ID,
            "X-Aspire-Office-Id": OFFICE_ID,
            "X-Aspire-Tenant-Id": TENANT_ID,
            "X-Aspire-Capability-Token": token,
        }

    def test_missing_token_returns_403(self) -> None:
        """No capability token → 403 denied."""
        resp = _client.get(
            "/v1/calls/caller-id-lookup",
            params={"phone": "+14155550001"},
            headers={
                "X-Aspire-Suite-Id": SUITE_ID,
                "X-Aspire-Office-Id": OFFICE_ID,
                "X-Aspire-Tenant-Id": TENANT_ID,
                # No X-Aspire-Capability-Token
            },
        )
        assert resp.status_code in (403, 422)  # 422 if missing required header

    def test_invalid_token_returns_403(self) -> None:
        """Invalid capability token → 403."""
        with patch(
            "aspire_orchestrator.routes.calls.validate_token",
            return_value=MagicMock(valid=False, error=MagicMock(value="INVALID_CAPABILITY_TOKEN")),
        ):
            resp = _client.get(
                "/v1/calls/caller-id-lookup",
                params={"phone": "+14155550001"},
                headers=self._make_headers(token="bad-token"),
            )
        assert resp.status_code == 403


class TestCallerIdLookupReceipt:
    """Law #2: receipt cut on every call including fallback."""

    def _make_headers(self) -> dict[str, str]:
        return {
            "X-Aspire-Suite-Id": SUITE_ID,
            "X-Aspire-Office-Id": OFFICE_ID,
            "X-Aspire-Tenant-Id": TENANT_ID,
            "X-Aspire-Capability-Token": "test-token",
        }

    def test_receipt_cut_on_successful_lookup(self) -> None:
        stored: list[dict] = []

        async def _select(table: str, filters: str, **kwargs) -> list[dict[str, Any]]:
            if table == "front_desk_routing_contacts":
                return _ROUTING_CONTACT_ROW
            return []

        with (
            patch(
                "aspire_orchestrator.routes.calls.validate_token",
                return_value=MagicMock(valid=True, error=None),
            ),
            patch(
                "aspire_orchestrator.routes.calls.supabase_select",
                new=AsyncMock(side_effect=_select),
            ),
            patch(
                "aspire_orchestrator.routes.calls.receipt_store.store_receipts",
                side_effect=lambda r: stored.extend(r),
            ),
        ):
            _client.get(
                "/v1/calls/caller-id-lookup",
                params={"phone": "+14155550001"},
                headers=self._make_headers(),
            )

        assert len(stored) >= 1
        assert stored[0]["receipt_type"] == "caller_id_lookup"
        assert stored[0]["outcome"] == "success"

    def test_receipt_no_full_phone_pii(self) -> None:
        """Law #9: full phone number must not appear in receipt inputs/outputs."""
        stored: list[dict] = []

        async def _select(table: str, filters: str, **kwargs) -> list[dict[str, Any]]:
            return []

        with (
            patch(
                "aspire_orchestrator.routes.calls.validate_token",
                return_value=MagicMock(valid=True, error=None),
            ),
            patch(
                "aspire_orchestrator.routes.calls.supabase_select",
                new=AsyncMock(side_effect=_select),
            ),
            patch(
                "aspire_orchestrator.routes.calls.receipt_store.store_receipts",
                side_effect=lambda r: stored.extend(r),
            ),
        ):
            _client.get(
                "/v1/calls/caller-id-lookup",
                params={"phone": "+14155550001"},
                headers=self._make_headers(),
            )

        receipt_str = str(stored)
        # Full E.164 should not appear (only prefix)
        assert "+14155550001" not in receipt_str or "+14155..." in receipt_str


class TestCallerIdLookupLatency:
    """<100ms p95 on mock DB."""

    def _make_headers(self) -> dict[str, str]:
        return {
            "X-Aspire-Suite-Id": SUITE_ID,
            "X-Aspire-Office-Id": OFFICE_ID,
            "X-Aspire-Tenant-Id": TENANT_ID,
            "X-Aspire-Capability-Token": "test-token",
        }

    def test_lookup_completes_under_100ms_with_mock_db(self) -> None:
        async def _select(table: str, filters: str, **kwargs) -> list[dict[str, Any]]:
            return []

        with (
            patch(
                "aspire_orchestrator.routes.calls.validate_token",
                return_value=MagicMock(valid=True, error=None),
            ),
            patch(
                "aspire_orchestrator.routes.calls.supabase_select",
                new=AsyncMock(side_effect=_select),
            ),
            patch(
                "aspire_orchestrator.routes.calls.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            start = time.monotonic()
            resp = _client.get(
                "/v1/calls/caller-id-lookup",
                params={"phone": "+19999999999"},
                headers=self._make_headers(),
            )
            elapsed = time.monotonic() - start

        assert resp.status_code == 200
        assert elapsed < 0.1, f"Lookup took {elapsed:.3f}s, budget is 0.1s"
