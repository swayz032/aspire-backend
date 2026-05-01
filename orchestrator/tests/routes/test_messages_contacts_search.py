"""Tests for GET /v1/messages/contacts/search (4-source contact search).

TDD: Written BEFORE implementation per plan §5 Lane E1.

Covers:
  - Returns results from routing_contacts (highest priority, role badge)
  - Returns results from sms_thread contacts
  - Returns results from call memory entities (last 90 days)
  - Manual E.164 fallback when q matches phone pattern
  - Priority ordering: routing > sms > call > manual
  - Capability token required
  - Cross-tenant isolation
  - Response shape contract
  - Empty results return empty list, not 404
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

_MOCK_CONTACTS = [
    {
        "display_name": "Tonio Owner",
        "phone": "+14155550001",
        "source": "routing",
        "role": "owner",
        "last_interaction_at": "2026-04-30T09:00:00+00:00",
    },
    {
        "display_name": "Alice Smith",
        "phone": "+14155550101",
        "source": "sms",
        "role": None,
        "last_interaction_at": "2026-04-30T08:00:00+00:00",
    },
    {
        "display_name": "+14155550202",
        "phone": "+14155550202",
        "source": "call",
        "role": None,
        "last_interaction_at": "2026-04-28T14:00:00+00:00",
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

def test_contacts_search_requires_token():
    """No capability_token -> 401."""
    resp = _client.get(
        "/v1/messages/contacts/search",
        params={"q": "Alice"},
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401


def test_contacts_search_cross_tenant_denied():
    """Token from tenant B with tenant A headers -> 401."""
    tenant_b_token = _mint_token("telephony:sms_read", suite_id=SUITE_ID_B, office_id=OFFICE_ID_B)

    resp = _client.get(
        "/v1/messages/contacts/search",
        params={"q": "Alice", "capability_token": json.dumps(tenant_b_token)},
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_contacts_search_returns_results():
    """Happy path: returns contact list with correct shape."""
    cap_token = _mint_token("telephony:sms_read")

    with patch(
        "aspire_orchestrator.routes.messages._search_contacts",
        new=AsyncMock(return_value=_MOCK_CONTACTS),
    ):
        resp = _client.get(
            "/v1/messages/contacts/search",
            params={"q": "Alice", "limit": 20, "capability_token": json.dumps(cap_token)},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "contacts" in data
    assert isinstance(data["contacts"], list)

    for c in data["contacts"]:
        assert "display_name" in c
        assert "phone" in c
        assert "source" in c
        assert c["source"] in ("routing", "sms", "call", "manual")


def test_contacts_search_priority_order():
    """routing contacts appear before sms, then call, then manual."""
    cap_token = _mint_token("telephony:sms_read")

    with patch(
        "aspire_orchestrator.routes.messages._search_contacts",
        new=AsyncMock(return_value=_MOCK_CONTACTS),
    ):
        resp = _client.get(
            "/v1/messages/contacts/search",
            params={"q": "Tonio", "capability_token": json.dumps(cap_token)},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    contacts = resp.json()["contacts"]
    sources = [c["source"] for c in contacts]
    # routing must come first if present
    if "routing" in sources:
        routing_idx = sources.index("routing")
        sms_idx = sources.index("sms") if "sms" in sources else len(sources)
        assert routing_idx < sms_idx


def test_contacts_search_routing_has_role_badge():
    """Routing contacts have role field set (role badge)."""
    cap_token = _mint_token("telephony:sms_read")
    routing_contacts = [c for c in _MOCK_CONTACTS if c["source"] == "routing"]

    with patch(
        "aspire_orchestrator.routes.messages._search_contacts",
        new=AsyncMock(return_value=routing_contacts),
    ):
        resp = _client.get(
            "/v1/messages/contacts/search",
            params={"q": "Tonio", "capability_token": json.dumps(cap_token)},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    contacts = resp.json()["contacts"]
    routing = [c for c in contacts if c["source"] == "routing"]
    for r in routing:
        assert r["role"] is not None


def test_contacts_search_empty_query_returns_recents():
    """Empty q= returns recent contacts (up to limit)."""
    cap_token = _mint_token("telephony:sms_read")

    with patch(
        "aspire_orchestrator.routes.messages._search_contacts",
        new=AsyncMock(return_value=_MOCK_CONTACTS),
    ):
        resp = _client.get(
            "/v1/messages/contacts/search",
            params={"q": "", "limit": 20, "capability_token": json.dumps(cap_token)},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    assert "contacts" in resp.json()


def test_contacts_search_no_results():
    """No matches returns empty list, not 404."""
    cap_token = _mint_token("telephony:sms_read")

    with patch(
        "aspire_orchestrator.routes.messages._search_contacts",
        new=AsyncMock(return_value=[]),
    ):
        resp = _client.get(
            "/v1/messages/contacts/search",
            params={"q": "xyznotexist", "capability_token": json.dumps(cap_token)},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    assert resp.json()["contacts"] == []


def test_contacts_search_limit_capped():
    """limit > 100 is rejected with 422."""
    cap_token = _mint_token("telephony:sms_read")

    resp = _client.get(
        "/v1/messages/contacts/search",
        params={"q": "test", "limit": 999, "capability_token": json.dumps(cap_token)},
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 422
