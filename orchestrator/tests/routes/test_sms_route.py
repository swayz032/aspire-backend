"""Tests for SMS send route (Pass 16 -- Law #3, #4, #5).

Covers:
- POST /sms/send: no capability token -> 401
- POST /sms/send happy path -> 200 with message_sid + receipt_id
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.sms import router as sms_router

_app = FastAPI()
_app.include_router(sms_router)
_client = TestClient(_app, raise_server_exceptions=False)

SUITE_ID = "00000000-0000-0000-0000-000000000001"
OFFICE_ID = "00000000-0000-0000-0000-000000000011"
TENANT_ID = "00000000-0000-0000-0000-000000000099"
THREAD_MEM_ID = "mem-abc123def456"
IDEM_KEY = "test-sms-idem-key-abcxyz12345"

_SCOPE_HEADERS = {
    "X-Tenant-Id": TENANT_ID,
    "X-Suite-Id": SUITE_ID,
    "X-Office-Id": OFFICE_ID,
}


def _mint_token(scope: str) -> dict:
    from aspire_orchestrator.services.token_service import mint_token
    return mint_token(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        tool="sms",
        scopes=[scope],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=45,
    )


def test_sms_send_yellow_requires_token():
    """No capability_token -> 401 MISSING_CAPABILITY_TOKEN."""
    resp = _client.post(
        "/v1/sms/send",
        json={
            "thread_memory_id": THREAD_MEM_ID,
            "body": "Hello world test message",
            "idempotency_key": IDEM_KEY,
        },
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401
    assert "MISSING_CAPABILITY_TOKEN" in str(resp.json())


def test_sms_send_happy_path():
    """Happy path: valid token -> 200 with message_sid, status, receipt_id."""
    cap_token = _mint_token("telephony:sms_send")
    receipt_id = str(uuid.uuid4())
    msg_sid = "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    with patch("aspire_orchestrator.routes.sms.send_sms",
               new=AsyncMock(return_value={
                   "message_sid": msg_sid,
                   "status": "queued",
                   "receipt_id": receipt_id,
               })):

        resp = _client.post(
            "/v1/sms/send",
            json={
                "thread_memory_id": THREAD_MEM_ID,
                "body": "Hello, this is a test SMS message.",
                "idempotency_key": IDEM_KEY,
                "capability_token": cap_token,
            },
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["message_sid"] == msg_sid
    assert data["status"] == "queued"
    assert data["receipt_id"] == receipt_id
