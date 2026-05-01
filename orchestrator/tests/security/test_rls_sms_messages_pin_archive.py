"""RLS Evil Tests — SMS thread pin/archive cross-tenant isolation (Pass 19 Lane D).

Law #6: Tenant A cannot pin or archive Tenant B's SMS threads.
Cross-tenant PATCH on sms_thread memory_objects (pin/archive state changes)
must return 401/403 — not silently succeed.

Tests:
  - Tenant B token + Tenant A headers → 401 on pin
  - Tenant B token + Tenant A headers → 401 on archive
  - Tenant B token + Tenant A headers → 401 on read (mark-as-read)
  - Missing scope headers → 401 (fail-closed, Law #3)
  - Wrong capability token scope → 401

Aspire Laws:
  Law #3: Fail Closed — scope mismatch = deny, never silent success.
  Law #6: Zero cross-tenant leakage on sms_thread state changes.
  Law #2: Denial receipt cut on rejected requests.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-ci")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.messages import router as messages_router

_app = FastAPI()
_app.include_router(messages_router)
_client = TestClient(_app, raise_server_exceptions=False)

# Tenant A — owns the SMS thread
SUITE_A = "aa000000-0000-0000-0000-000000000001"
OFFICE_A = "aa000000-0000-0000-0000-000000000002"
TENANT_A = "aa000000-0000-0000-0000-000000000003"

# Tenant B — attacker
SUITE_B = "bb000000-0000-0000-0000-000000000001"
OFFICE_B = "bb000000-0000-0000-0000-000000000002"
TENANT_B = "bb000000-0000-0000-0000-000000000003"

THREAD_ID_A = str(uuid.uuid4())

_HEADERS_A = {
    "X-Tenant-Id": TENANT_A,
    "X-Suite-Id": SUITE_A,
    "X-Office-Id": OFFICE_A,
}


def _mint_token(suite_id: str, office_id: str, scope: str = "telephony:sms_manage") -> dict:
    from aspire_orchestrator.services.token_service import mint_token
    return mint_token(
        suite_id=suite_id,
        office_id=office_id,
        tool="messages",
        scopes=[scope],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=45,
    )


def _thread_row_for_tenant_a() -> list[dict]:
    """SMS thread row belonging to Tenant A."""
    return [{
        "memory_id": THREAD_ID_A,
        "tenant_id": TENANT_A,
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
        "memory_type": "sms_thread",
        "is_pinned": False,
        "is_archived": False,
        "read_at": None,
        "detail": {"from": "+15551234567", "body": "Secret message of Tenant A"},
    }]


# ---------------------------------------------------------------------------
# Cross-tenant token with correct headers: B's token against A's resources
# ---------------------------------------------------------------------------


class TestCrossTenantTokenRejected:
    """Law #6: Tenant B's capability token cannot operate on Tenant A's SMS threads."""

    def test_cross_tenant_pin_denied(self) -> None:
        """Tenant B token + Tenant A headers → 401 on pin."""
        tenant_b_token = _mint_token(suite_id=SUITE_B, office_id=OFFICE_B)

        resp = _client.patch(
            f"/v1/messages/threads/{THREAD_ID_A}/pin",
            json={"capability_token": tenant_b_token},
            headers=_HEADERS_A,  # Tenant A's scope headers
        )

        # Token is for Tenant B but headers claim Tenant A → token validation must fail
        assert resp.status_code == 401, (
            f"Cross-tenant pin must return 401, got {resp.status_code}. "
            "Tenant B's token must not work with Tenant A's scope headers."
        )

    def test_cross_tenant_archive_denied(self) -> None:
        """Tenant B token + Tenant A headers → 401 on archive."""
        tenant_b_token = _mint_token(suite_id=SUITE_B, office_id=OFFICE_B)

        resp = _client.patch(
            f"/v1/messages/threads/{THREAD_ID_A}/archive",
            json={"capability_token": tenant_b_token},
            headers=_HEADERS_A,
        )

        assert resp.status_code == 401, (
            f"Cross-tenant archive must return 401, got {resp.status_code}."
        )

    def test_cross_tenant_read_denied(self) -> None:
        """Tenant B token + Tenant A headers → 401 on mark-as-read."""
        tenant_b_token = _mint_token(suite_id=SUITE_B, office_id=OFFICE_B)

        resp = _client.patch(
            f"/v1/messages/threads/{THREAD_ID_A}/read",
            json={"capability_token": tenant_b_token},
            headers=_HEADERS_A,
        )

        assert resp.status_code == 401, (
            f"Cross-tenant read must return 401, got {resp.status_code}."
        )

    def test_b_scope_headers_with_a_token_denied(self) -> None:
        """Tenant A token + Tenant B headers → 401 (token-scope mismatch)."""
        tenant_a_token = _mint_token(suite_id=SUITE_A, office_id=OFFICE_A)

        resp = _client.patch(
            f"/v1/messages/threads/{THREAD_ID_A}/pin",
            json={"capability_token": tenant_a_token},
            headers={  # Tenant B headers — mismatch with Tenant A token
                "X-Tenant-Id": TENANT_B,
                "X-Suite-Id": SUITE_B,
                "X-Office-Id": OFFICE_B,
            },
        )

        assert resp.status_code == 401, (
            f"Token-scope mismatch (A token + B headers) must return 401, got {resp.status_code}."
        )


# ---------------------------------------------------------------------------
# Missing scope headers → 401 (Law #3 fail-closed)
# ---------------------------------------------------------------------------


class TestMissingScopeHeadersFailClosed:
    """Law #3: Missing scope headers → 401, never 200 or 500."""

    def test_no_scope_headers_pin_denied(self) -> None:
        """No X-Tenant-Id/X-Suite-Id/X-Office-Id → 401."""
        token = _mint_token(suite_id=SUITE_A, office_id=OFFICE_A)

        resp = _client.patch(
            f"/v1/messages/threads/{THREAD_ID_A}/pin",
            json={"capability_token": token},
            # No scope headers
        )

        assert resp.status_code == 401, (
            f"Missing scope headers must return 401, got {resp.status_code}."
        )

    def test_no_scope_headers_archive_denied(self) -> None:
        """No scope headers → 401 on archive."""
        token = _mint_token(suite_id=SUITE_A, office_id=OFFICE_A)

        resp = _client.patch(
            f"/v1/messages/threads/{THREAD_ID_A}/archive",
            json={"capability_token": token},
        )

        assert resp.status_code == 401

    def test_partial_scope_headers_denied(self) -> None:
        """Only X-Suite-Id missing → 401 (all 3 headers required)."""
        token = _mint_token(suite_id=SUITE_A, office_id=OFFICE_A)

        resp = _client.patch(
            f"/v1/messages/threads/{THREAD_ID_A}/pin",
            json={"capability_token": token},
            headers={
                "X-Tenant-Id": TENANT_A,
                # Missing X-Suite-Id
                "X-Office-Id": OFFICE_A,
            },
        )

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Wrong capability token scope → 401 (Law #5)
# ---------------------------------------------------------------------------


class TestWrongCapabilityTokenScope:
    """Law #5: Wrong token scope → 401 (sms_read insufficient for state changes)."""

    def test_read_only_scope_cannot_pin(self) -> None:
        """telephony:sms_read scope insufficient for pin (requires sms_manage)."""
        read_only_token = _mint_token(
            suite_id=SUITE_A, office_id=OFFICE_A, scope="telephony:sms_read"
        )

        resp = _client.patch(
            f"/v1/messages/threads/{THREAD_ID_A}/pin",
            json={"capability_token": read_only_token},
            headers=_HEADERS_A,
        )

        assert resp.status_code == 401

    def test_read_only_scope_cannot_archive(self) -> None:
        """telephony:sms_read scope insufficient for archive."""
        read_only_token = _mint_token(
            suite_id=SUITE_A, office_id=OFFICE_A, scope="telephony:sms_read"
        )

        resp = _client.patch(
            f"/v1/messages/threads/{THREAD_ID_A}/archive",
            json={"capability_token": read_only_token},
            headers=_HEADERS_A,
        )

        assert resp.status_code == 401

    def test_no_token_pin_denied(self) -> None:
        """No capability token at all → 401 (Law #5: token required)."""
        resp = _client.patch(
            f"/v1/messages/threads/{THREAD_ID_A}/pin",
            json={},  # No capability_token
            headers=_HEADERS_A,
        )

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Confirm zero data leak for valid tenant
# ---------------------------------------------------------------------------


class TestTenantACanPinOwnThread:
    """Positive control: Tenant A CAN pin their own thread."""

    def test_tenant_a_can_pin_own_thread(self) -> None:
        """Tenant A with correct token and headers can pin their own SMS thread."""
        token = _mint_token(suite_id=SUITE_A, office_id=OFFICE_A)
        thread = _thread_row_for_tenant_a()
        updated_thread = {**thread[0], "is_pinned": True}

        with (
            patch("aspire_orchestrator.routes.messages.supabase_select",
                  new=AsyncMock(return_value=thread)),
            patch("aspire_orchestrator.routes.messages.supabase_update",
                  new=AsyncMock(return_value=updated_thread)),
            patch("aspire_orchestrator.routes.messages._cut_receipt",
                  new=AsyncMock(return_value={"id": str(uuid.uuid4())})),
        ):
            resp = _client.patch(
                f"/v1/messages/threads/{THREAD_ID_A}/pin",
                json={"capability_token": token},
                headers=_HEADERS_A,
            )

        assert resp.status_code == 200, (
            f"Tenant A should be able to pin own thread, got {resp.status_code}: {resp.text}"
        )
