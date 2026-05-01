"""Tests for GET /v1/messages/suggestions (proactive_candidate engine wrapper).

TDD: Written BEFORE implementation per plan §5 Lane E1.

Covers:
  - Returns candidates with action='sms_reply_needed' OR action='sms_followup'
  - Empty list when no matching candidates
  - Capability token required
  - Cross-tenant isolation (only own tenant candidates)
  - Response shape contract
  - limit param respected
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

_SCOPE_HEADERS = {
    "X-Tenant-Id": TENANT_ID,
    "X-Suite-Id": SUITE_ID,
    "X-Office-Id": OFFICE_ID,
}

_MOCK_SUGGESTIONS = [
    {
        "thread_id": str(uuid.uuid4()),
        "contact_name": "Alice Smith",
        "contact_phone": "+14155550101",
        "suggested_body": "Hi Alice — just following up on your inquiry. Any questions?",
        "reason": "sms_followup",
        "candidate_id": str(uuid.uuid4()),
    },
    {
        "thread_id": str(uuid.uuid4()),
        "contact_name": "Bob Jones",
        "contact_phone": "+14155550102",
        "suggested_body": "Hi Bob, we received your message. We'll be in touch shortly.",
        "reason": "sms_reply_needed",
        "candidate_id": str(uuid.uuid4()),
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
# Capability token
# ---------------------------------------------------------------------------

def test_suggestions_requires_token():
    """No capability_token -> 401."""
    resp = _client.get("/v1/messages/suggestions", headers=_SCOPE_HEADERS)
    assert resp.status_code == 401


def test_suggestions_cross_tenant_denied():
    """Token from tenant B with tenant A headers -> 401."""
    tenant_b_token = _mint_token("telephony:sms_read", suite_id=SUITE_ID_B, office_id=OFFICE_ID_B)

    resp = _client.get(
        "/v1/messages/suggestions",
        params={"capability_token": json.dumps(tenant_b_token)},
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_suggestions_happy_path():
    """Returns suggestions list with correct shape."""
    cap_token = _mint_token("telephony:sms_read")

    with patch(
        "aspire_orchestrator.routes.messages._fetch_suggestions",
        new=AsyncMock(return_value=_MOCK_SUGGESTIONS),
    ):
        resp = _client.get(
            "/v1/messages/suggestions",
            params={"limit": 5, "capability_token": json.dumps(cap_token)},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "suggestions" in data
    assert isinstance(data["suggestions"], list)

    for s in data["suggestions"]:
        assert "contact_name" in s
        assert "contact_phone" in s
        assert "suggested_body" in s
        assert "reason" in s
        assert "candidate_id" in s
        # reason must be sms_reply_needed or sms_followup
        assert s["reason"] in ("sms_reply_needed", "sms_followup")


def test_suggestions_empty_when_no_candidates():
    """No matching candidates -> empty list, not 404."""
    cap_token = _mint_token("telephony:sms_read")

    with patch(
        "aspire_orchestrator.routes.messages._fetch_suggestions",
        new=AsyncMock(return_value=[]),
    ):
        resp = _client.get(
            "/v1/messages/suggestions",
            params={"limit": 5, "capability_token": json.dumps(cap_token)},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    assert resp.json()["suggestions"] == []


def test_suggestions_thread_id_optional():
    """thread_id is optional in suggestion (candidate may not have a thread yet)."""
    cap_token = _mint_token("telephony:sms_read")
    no_thread_suggestion = [
        {
            "thread_id": None,
            "contact_name": "New Lead",
            "contact_phone": "+14155550303",
            "suggested_body": "Hi, this is Sarah from Aspire. We received your request.",
            "reason": "sms_reply_needed",
            "candidate_id": str(uuid.uuid4()),
        }
    ]

    with patch(
        "aspire_orchestrator.routes.messages._fetch_suggestions",
        new=AsyncMock(return_value=no_thread_suggestion),
    ):
        resp = _client.get(
            "/v1/messages/suggestions",
            params={"limit": 5, "capability_token": json.dumps(cap_token)},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    suggestions = resp.json()["suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["thread_id"] is None


def test_suggestions_limit_respected():
    """limit param controls max returned suggestions."""
    cap_token = _mint_token("telephony:sms_read")

    with patch(
        "aspire_orchestrator.routes.messages._fetch_suggestions",
        new=AsyncMock(return_value=_MOCK_SUGGESTIONS[:1]),
    ):
        resp = _client.get(
            "/v1/messages/suggestions",
            params={"limit": 1, "capability_token": json.dumps(cap_token)},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    assert len(resp.json()["suggestions"]) <= 1


def test_suggestions_missing_scope_headers_returns_401():
    """Missing X- headers -> 401."""
    cap_token = _mint_token("telephony:sms_read")

    resp = _client.get(
        "/v1/messages/suggestions",
        params={"capability_token": json.dumps(cap_token)},
    )
    assert resp.status_code == 401
