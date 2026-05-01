"""Tests for GET /v1/messages/templates (V1 quick-reply template list).

TDD: Written BEFORE implementation per plan §5 Lane E1.

Covers:
  - Exact 5-template payload per plan §3.9.7
  - Correct token lists per template
  - Capability token required
  - Response shape contract
  - No state change -> no receipt required (GREEN tier read)
"""
from __future__ import annotations

import os
import uuid

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

_SCOPE_HEADERS = {
    "X-Tenant-Id": TENANT_ID,
    "X-Suite-Id": SUITE_ID,
    "X-Office-Id": OFFICE_ID,
}

# Exact expected templates per plan §3.9.7
_EXPECTED_TEMPLATES = [
    {
        "body": "Confirming our appointment for {{date}} at {{time}}. Reply YES to confirm or call us at {{business_phone}}.",
        "tokens": ["date", "time", "business_phone"],
    },
    {
        "body": "Hi {{first_name}} — quick follow-up on the quote we sent {{relative_time}}. Any questions?",
        "tokens": ["first_name", "relative_time"],
    },
    {
        "body": "Thanks for your inquiry. We'll get back to you within {{response_window}}.",
        "tokens": ["response_window"],
    },
    {
        "body": "Reminder: your invoice #{{invoice_number}} for {{amount}} is due {{due_date}}.",
        "tokens": ["invoice_number", "amount", "due_date"],
    },
    {
        "body": "We received your message. Sarah will follow up shortly.",
        "tokens": [],
    },
]


def _mint_token(scope: str) -> dict:
    from aspire_orchestrator.services.token_service import mint_token
    return mint_token(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        tool="messages",
        scopes=[scope],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=45,
    )


# ---------------------------------------------------------------------------
# Capability token
# ---------------------------------------------------------------------------

def test_templates_requires_token():
    """No capability_token -> 401."""
    resp = _client.get("/v1/messages/templates", headers=_SCOPE_HEADERS)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Exact payload contract
# ---------------------------------------------------------------------------

def test_templates_returns_exactly_5():
    """Exactly 5 templates returned (V1 spec)."""
    cap_token = _mint_token("telephony:sms_read")

    resp = _client.get(
        "/v1/messages/templates",
        params={"capability_token": json.dumps(cap_token)},
        headers=_SCOPE_HEADERS,
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "templates" in data
    assert len(data["templates"]) == 5


def test_templates_exact_bodies():
    """Template bodies match plan §3.9.7 exactly."""
    cap_token = _mint_token("telephony:sms_read")

    resp = _client.get(
        "/v1/messages/templates",
        params={"capability_token": json.dumps(cap_token)},
        headers=_SCOPE_HEADERS,
    )

    assert resp.status_code == 200
    templates = resp.json()["templates"]

    for i, expected in enumerate(_EXPECTED_TEMPLATES):
        assert templates[i]["body"] == expected["body"], (
            f"Template {i} body mismatch.\n"
            f"  Expected: {expected['body']}\n"
            f"  Got:      {templates[i]['body']}"
        )


def test_templates_exact_token_lists():
    """Token lists match plan §3.9.7 exactly per template."""
    cap_token = _mint_token("telephony:sms_read")

    resp = _client.get(
        "/v1/messages/templates",
        params={"capability_token": json.dumps(cap_token)},
        headers=_SCOPE_HEADERS,
    )

    assert resp.status_code == 200
    templates = resp.json()["templates"]

    for i, expected in enumerate(_EXPECTED_TEMPLATES):
        assert sorted(templates[i]["tokens"]) == sorted(expected["tokens"]), (
            f"Template {i} token list mismatch.\n"
            f"  Expected: {expected['tokens']}\n"
            f"  Got:      {templates[i]['tokens']}"
        )


def test_templates_shape():
    """Each template has id, body, tokens fields."""
    cap_token = _mint_token("telephony:sms_read")

    resp = _client.get(
        "/v1/messages/templates",
        params={"capability_token": json.dumps(cap_token)},
        headers=_SCOPE_HEADERS,
    )

    assert resp.status_code == 200
    for t in resp.json()["templates"]:
        assert "id" in t
        assert "body" in t
        assert "tokens" in t
        assert isinstance(t["tokens"], list)


def test_templates_last_has_empty_tokens():
    """5th template (plain acknowledgement) has empty token list."""
    cap_token = _mint_token("telephony:sms_read")

    resp = _client.get(
        "/v1/messages/templates",
        params={"capability_token": json.dumps(cap_token)},
        headers=_SCOPE_HEADERS,
    )

    assert resp.status_code == 200
    last_template = resp.json()["templates"][-1]
    assert last_template["tokens"] == []


def test_templates_missing_scope_headers_returns_401():
    """Missing X- headers -> 401."""
    cap_token = _mint_token("telephony:sms_read")

    resp = _client.get(
        "/v1/messages/templates",
        params={"capability_token": json.dumps(cap_token)},
    )
    assert resp.status_code == 401
