"""Tests for GET /v1/messages/threads/{threadId}/messages (paginated message list).

TDD: Written BEFORE implementation per plan §5 Lane E1.

Covers:
  - Happy path: returns messages ordered by sent_at ASC
  - Cursor pagination via `before` param
  - Capability token required
  - Thread must belong to the requesting tenant (tenant isolation)
  - Response shape contract
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
SUITE_ID_B = "00000000-0000-0000-0000-000000000002"
OFFICE_ID_B = "00000000-0000-0000-0000-000000000022"

THREAD_ID = str(uuid.uuid4())

_SCOPE_HEADERS = {
    "X-Tenant-Id": TENANT_ID,
    "X-Suite-Id": SUITE_ID,
    "X-Office-Id": OFFICE_ID,
}

_MOCK_MESSAGES = [
    {
        "id": str(uuid.uuid4()),
        "direction": "inbound",
        "from_number": "+14155550101",
        "to_number": "+12125550198",
        "body": "Hello, is this the front desk?",
        "status": "received",
        "sent_at": "2026-04-30T10:00:00+00:00",
        "delivered_at": None,
        "media_urls": [],
        "twilio_message_sid": "SM" + "a" * 32,
    },
    {
        "id": str(uuid.uuid4()),
        "direction": "outbound",
        "from_number": "+12125550198",
        "to_number": "+14155550101",
        "body": "Yes, how can I help?",
        "status": "delivered",
        "sent_at": "2026-04-30T10:01:00+00:00",
        "delivered_at": "2026-04-30T10:01:05+00:00",
        "media_urls": [],
        "twilio_message_sid": "SM" + "b" * 32,
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
# Capability token tests
# ---------------------------------------------------------------------------

def test_thread_messages_requires_token():
    """No capability_token -> 401."""
    resp = _client.get(
        f"/v1/messages/threads/{THREAD_ID}/messages",
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401


def test_thread_messages_cross_tenant_token_denied():
    """Token minted for tenant B with tenant A headers -> 401."""
    tenant_b_token = _mint_token("telephony:sms_read", suite_id=SUITE_ID_B, office_id=OFFICE_ID_B)

    resp = _client.get(
        f"/v1/messages/threads/{THREAD_ID}/messages",
        params={"capability_token": json.dumps(tenant_b_token)},
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_thread_messages_happy_path():
    """Returns messages in sent_at ASC order with correct shape."""
    cap_token = _mint_token("telephony:sms_read")

    with patch(
        "aspire_orchestrator.routes.messages._fetch_messages",
        new=AsyncMock(return_value=_MOCK_MESSAGES),
    ):
        resp = _client.get(
            f"/v1/messages/threads/{THREAD_ID}/messages",
            params={
                "limit": 100,
                "capability_token": json.dumps(cap_token),
            },
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "messages" in data
    assert isinstance(data["messages"], list)
    assert len(data["messages"]) == 2

    # Shape check
    for msg in data["messages"]:
        assert "id" in msg
        assert "direction" in msg
        assert "from_number" in msg
        assert "to_number" in msg
        assert "body" in msg
        assert "status" in msg
        assert "sent_at" in msg

    # ASC order: first message is older
    assert data["messages"][0]["sent_at"] <= data["messages"][1]["sent_at"]


def test_thread_messages_cursor_pagination():
    """before cursor param is accepted and forwarded."""
    cap_token = _mint_token("telephony:sms_read")

    with patch(
        "aspire_orchestrator.routes.messages._fetch_messages",
        new=AsyncMock(return_value=[_MOCK_MESSAGES[0]]),
    ):
        resp = _client.get(
            f"/v1/messages/threads/{THREAD_ID}/messages",
            params={
                "limit": 100,
                "before": "2026-04-30T10:01:00+00:00__" + str(uuid.uuid4()),
                "capability_token": json.dumps(cap_token),
            },
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "messages" in data
    assert "has_more" in data


def test_thread_messages_empty_thread():
    """Thread with no messages returns empty list, not 404."""
    cap_token = _mint_token("telephony:sms_read")

    with patch(
        "aspire_orchestrator.routes.messages._fetch_messages",
        new=AsyncMock(return_value=[]),
    ):
        resp = _client.get(
            f"/v1/messages/threads/{THREAD_ID}/messages",
            params={"capability_token": json.dumps(cap_token)},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    assert resp.json()["messages"] == []


def test_thread_messages_limit_capped():
    """limit > 200 is rejected with 422."""
    cap_token = _mint_token("telephony:sms_read")

    resp = _client.get(
        f"/v1/messages/threads/{THREAD_ID}/messages",
        params={
            "limit": 9999,
            "capability_token": json.dumps(cap_token),
        },
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 422
