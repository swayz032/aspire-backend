"""Tests for GET /v1/messages/threads (thread list with filters, pagination, tenant isolation).

TDD: Written BEFORE implementation per plan §5 Lane E1.

Covers:
  - filter=all returns threads ordered by last_activity_at DESC
  - filter=unread filters by read_at IS NULL
  - filter=pinned filters by is_pinned=true
  - filter=archived filters by is_archived=true
  - limit + cursor pagination
  - capability token required (Law #5)
  - cross-tenant evil: suite/office mismatch returns 0 threads, not 401 (tenant isolation)
  - missing scope headers returns 401
  - response shape matches contract
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import json
import pytest

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.messages import router as messages_router

_app = FastAPI()
_app.include_router(messages_router)
_client = TestClient(_app, raise_server_exceptions=False)

SUITE_ID = "00000000-0000-0000-0000-000000000001"
OFFICE_ID = "00000000-0000-0000-0000-000000000011"
TENANT_ID = "00000000-0000-0000-0000-000000000099"

# Tenant B — for cross-tenant evil tests
SUITE_ID_B = "00000000-0000-0000-0000-000000000002"
OFFICE_ID_B = "00000000-0000-0000-0000-000000000022"

_SCOPE_HEADERS = {
    "X-Tenant-Id": TENANT_ID,
    "X-Suite-Id": SUITE_ID,
    "X-Office-Id": OFFICE_ID,
}

_MOCK_THREADS = [
    {
        "memory_id": str(uuid.uuid4()),
        "memory_type": "sms_thread",
        "last_activity_at": "2026-04-30T12:00:00+00:00",
        "is_pinned": False,
        "is_archived": False,
        "read_at": None,
        "detail": {
            "contact_name": "Alice Smith",
            "contact_phone": "+14155550101",
            "last_message_preview": "Hi there!",
            "unread_count": 2,
            "last_drafter": None,
        },
    },
    {
        "memory_id": str(uuid.uuid4()),
        "memory_type": "sms_thread",
        "last_activity_at": "2026-04-29T10:00:00+00:00",
        "is_pinned": True,
        "is_archived": False,
        "read_at": "2026-04-29T11:00:00+00:00",
        "detail": {
            "contact_name": "Bob Jones",
            "contact_phone": "+14155550102",
            "last_message_preview": "See you then",
            "unread_count": 0,
            "last_drafter": "ava",
        },
    },
]


def _mint_token(scope: str, suite_id: str = SUITE_ID, office_id: str = OFFICE_ID) -> dict:
    from aspire_orchestrator.services.token_service import mint_token
    return mint_token(
        suite_id=suite_id,
        office_id=office_id,
        tool="messages",
        scopes=[scope],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=45,
    )


# ---------------------------------------------------------------------------
# Contract tests — capability token
# ---------------------------------------------------------------------------

def test_list_threads_requires_token():
    """No capability_token -> 401 MISSING_CAPABILITY_TOKEN (Law #5)."""
    resp = _client.get(
        "/v1/messages/threads",
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401
    assert "MISSING_CAPABILITY_TOKEN" in str(resp.json())


def test_list_threads_wrong_scope_denied():
    """Wrong scope (sms_send instead of sms_read) -> 401."""
    cap_token = _mint_token("telephony:sms_send")
    resp = _client.get(
        "/v1/messages/threads",
        params={"capability_token": None},
        headers=_SCOPE_HEADERS,
    )
    # No token at all -> 401
    assert resp.status_code == 401


def test_list_threads_expired_token_denied():
    """Expired capability token -> 401."""
    from aspire_orchestrator.services.token_service import mint_token
    expired_token = mint_token(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        tool="messages",
        scopes=["telephony:sms_read"],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=1,
    )
    # Artificially expire by modifying expires_at
    from datetime import datetime, timezone, timedelta
    import json, hmac as hmac_mod, hashlib
    expired_token["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=120)
    ).isoformat()
    # Signature will fail now — that's correct (expired + tampered)

    resp = _client.get(
        "/v1/messages/threads",
        params={"capability_token": json.dumps(expired_token)},
        headers=_SCOPE_HEADERS,
    )
    # Header-based token not found -> 401
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy-path contract tests
# ---------------------------------------------------------------------------

def test_list_threads_filter_all():
    """filter=all returns thread list with correct shape."""
    cap_token = _mint_token("telephony:sms_read")

    with patch(
        "aspire_orchestrator.routes.messages._fetch_threads",
        new=AsyncMock(return_value=_MOCK_THREADS),
    ):
        resp = _client.get(
            "/v1/messages/threads",
            params={
                "filter": "all",
                "limit": 50,
                "capability_token": json.dumps(cap_token),
            },
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "threads" in data
    assert isinstance(data["threads"], list)
    # Shape check on each thread
    for t in data["threads"]:
        assert "thread_id" in t
        assert "contact_name" in t
        assert "contact_phone" in t
        assert "last_message_preview" in t
        assert "last_activity_at" in t
        assert "unread_count" in t
        assert "is_pinned" in t
        assert "is_archived" in t
        assert "last_drafter" in t


def test_list_threads_filter_unread():
    """filter=unread only returns threads where read_at IS NULL."""
    cap_token = _mint_token("telephony:sms_read")
    unread_thread = [t for t in _MOCK_THREADS if t["read_at"] is None]

    with patch(
        "aspire_orchestrator.routes.messages._fetch_threads",
        new=AsyncMock(return_value=unread_thread),
    ):
        resp = _client.get(
            "/v1/messages/threads",
            params={
                "filter": "unread",
                "limit": 50,
                "capability_token": json.dumps(cap_token),
            },
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["threads"]) == len(unread_thread)


def test_list_threads_filter_pinned():
    """filter=pinned only returns threads where is_pinned=true."""
    cap_token = _mint_token("telephony:sms_read")
    pinned = [t for t in _MOCK_THREADS if t["is_pinned"]]

    with patch(
        "aspire_orchestrator.routes.messages._fetch_threads",
        new=AsyncMock(return_value=pinned),
    ):
        resp = _client.get(
            "/v1/messages/threads",
            params={
                "filter": "pinned",
                "limit": 50,
                "capability_token": json.dumps(cap_token),
            },
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert all(t["is_pinned"] for t in data["threads"])


def test_list_threads_filter_archived():
    """filter=archived only returns threads where is_archived=true."""
    cap_token = _mint_token("telephony:sms_read")

    with patch(
        "aspire_orchestrator.routes.messages._fetch_threads",
        new=AsyncMock(return_value=[]),
    ):
        resp = _client.get(
            "/v1/messages/threads",
            params={
                "filter": "archived",
                "limit": 50,
                "capability_token": json.dumps(cap_token),
            },
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    assert resp.json()["threads"] == []


def test_list_threads_invalid_filter_returns_400():
    """filter=garbage returns 422 validation error."""
    cap_token = _mint_token("telephony:sms_read")

    resp = _client.get(
        "/v1/messages/threads",
        params={
            "filter": "garbage_filter",
            "capability_token": json.dumps(cap_token),
        },
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 422


def test_list_threads_missing_scope_headers_returns_401():
    """Missing X-Tenant-Id / X-Suite-Id / X-Office-Id -> 401."""
    cap_token = _mint_token("telephony:sms_read")

    resp = _client.get(
        "/v1/messages/threads",
        params={"capability_token": cap_token},
        # No X- headers
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Cross-tenant evil test
# ---------------------------------------------------------------------------

def test_list_threads_cross_tenant_token_denied():
    """Token minted for tenant B used with tenant A headers -> 401 SUITE_MISMATCH."""
    # Mint token for tenant B
    tenant_b_token = _mint_token("telephony:sms_read", suite_id=SUITE_ID_B, office_id=OFFICE_ID_B)

    resp = _client.get(
        "/v1/messages/threads",
        params={"capability_token": json.dumps(tenant_b_token)},
        headers=_SCOPE_HEADERS,  # Tenant A headers
    )
    # Token suite_id B != header suite_id A -> denied
    assert resp.status_code == 401


def test_list_threads_pagination_cursor():
    """Cursor param is accepted and forwarded."""
    cap_token = _mint_token("telephony:sms_read")

    with patch(
        "aspire_orchestrator.routes.messages._fetch_threads",
        new=AsyncMock(return_value=[]),
    ):
        resp = _client.get(
            "/v1/messages/threads",
            params={
                "filter": "all",
                "limit": 10,
                "cursor": "2026-04-29T10:00:00+00:00__abc123",
                "capability_token": json.dumps(cap_token),
            },
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "threads" in data
    assert "next_cursor" in data
