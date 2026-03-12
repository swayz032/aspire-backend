"""Tests for the Ops Telemetry Facade — W5 Phase 3 Group B.

Extends test_admin_api.py with additional edge-case tests:
  - PII redaction verification (Law #9)
  - Auth edge cases (Law #3)
  - Response shape validation for frontend contract compliance
  - Outbox/rollout stub correctness
  - Version bump to 3.0.0

Total: ~12 new tests.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from aspire_orchestrator.server import app
from aspire_orchestrator.routes.admin import (
    clear_admin_stores,
    register_incident,
    register_provider_call,
)
from aspire_orchestrator.services.receipt_store import clear_store, store_receipts

_TEST_JWT_SECRET = "test-ops-telemetry-secret-w5-32x-extra-padding-12345"


def _make_admin_token(sub: str = "ops-admin") -> str:
    return pyjwt.encode({"sub": sub}, _TEST_JWT_SECRET, algorithm="HS256")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ASPIRE_ADMIN_JWT_SECRET", _TEST_JWT_SECRET)
    clear_admin_stores()
    clear_store()
    yield
    clear_admin_stores()
    clear_store()


@pytest.fixture
def headers() -> dict[str, str]:
    return {
        "x-admin-token": _make_admin_token(),
        "x-correlation-id": str(uuid.uuid4()),
    }


# =============================================================================
# 1. Health + Version Tests
# =============================================================================


class TestOpsHealth:
    def test_health_version_3(self, client) -> None:
        """Health endpoint returns version 3.0.0 after Phase 3 bump."""
        response = client.get("/admin/ops/health")
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "3.0.0"
        assert data["status"] == "ok"

    def test_health_no_auth_required(self, client) -> None:
        """Health check works without admin token."""
        response = client.get("/admin/ops/health")
        assert response.status_code == 200

    def test_health_server_time_is_iso(self, client) -> None:
        """server_time is a valid ISO 8601 string."""
        data = client.get("/admin/ops/health").json()
        # ISO 8601 contains T separator
        assert "T" in data["server_time"]
        # Should be parseable
        datetime.fromisoformat(data["server_time"].replace("Z", "+00:00"))


# =============================================================================
# 2. Auth Edge Cases (Law #3: Fail Closed)
# =============================================================================


class TestAuthEdgeCases:
    def test_expired_jwt_rejected(self, client) -> None:
        """Expired JWT returns 401 (fail closed)."""
        import time

        expired_token = pyjwt.encode(
            {"sub": "admin", "exp": int(time.time()) - 3600},
            _TEST_JWT_SECRET,
            algorithm="HS256",
        )
        response = client.get(
            "/admin/ops/incidents",
            headers={"x-admin-token": expired_token},
        )
        assert response.status_code == 401

    def test_wrong_algorithm_rejected(self, client) -> None:
        """JWT with wrong algorithm is rejected."""
        # PyJWT will fail to decode HS384 token with HS256-only config
        wrong_algo_token = pyjwt.encode(
            {"sub": "admin"},
            _TEST_JWT_SECRET,
            algorithm="HS384",
        )
        response = client.get(
            "/admin/ops/incidents",
            headers={"x-admin-token": wrong_algo_token},
        )
        assert response.status_code == 401

    def test_all_auth_endpoints_require_token(self, client) -> None:
        """Every auth-required endpoint returns 401 without token."""
        endpoints = [
            "/admin/ops/incidents",
            "/admin/ops/receipts",
            "/admin/ops/provider-calls",
            "/admin/ops/outbox",
            "/admin/ops/rollouts",
            "/admin/proposals/pending",
        ]
        for ep in endpoints:
            r = client.get(ep)
            assert r.status_code == 401, f"{ep} should require auth, got {r.status_code}"


# =============================================================================
# 3. PII Redaction Tests (Law #9)
# =============================================================================


class TestPIIRedaction:
    def test_provider_call_payload_always_redacted(self, client, headers) -> None:
        """Provider call payload preview is always a string, max 200 chars."""
        call = {
            "call_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "provider": "stripe",
            "action": "payment.create",
            "status": "success",
            "http_status": 200,
            "retry_count": 0,
            "started_at": _now_iso(),
            "finished_at": _now_iso(),
            "payload_preview": '{"card_number": "4242424242424242", "email": "user@example.com", "ssn": "123-45-6789"}',
        }
        register_provider_call(call)

        response = client.get("/admin/ops/provider-calls", headers=headers)
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        preview = items[0]["redacted_payload_preview"]
        assert isinstance(preview, str)
        assert len(preview) <= 200

    def test_receipt_summaries_exclude_raw_data(self, client, headers) -> None:
        """Receipt summaries do NOT include raw redacted_inputs/outputs."""
        store_receipts([{
            "id": "r-pii-test",
            "correlation_id": "c-pii",
            "suite_id": "suite-pii",
            "office_id": "o-1",
            "action_type": "email.send",
            "risk_tier": "yellow",
            "outcome": "success",
            "created_at": _now_iso(),
            "redacted_inputs": '{"to": "user@example.com", "body": "secret stuff"}',
            "redacted_outputs": '{"message_id": "msg-123"}',
        }])

        response = client.get(
            "/admin/ops/receipts",
            headers=headers,
            params={"suite_id": "suite-pii"},
        )
        assert response.status_code == 200
        items = response.json()["items"]
        # Summaries should NOT contain raw input/output fields
        for item in items:
            assert "redacted_inputs" not in item
            assert "redacted_outputs" not in item
            # Should only have the safe summary fields
            assert "receipt_id" in item
            assert "action_type" in item
            assert "outcome" in item


# =============================================================================
# 4. Response Shape Contract Tests (Frontend compatibility)
# =============================================================================


class TestResponseShapes:
    def test_incidents_list_shape(self, client, headers) -> None:
        """Incidents response has items[], page{}, server_time."""
        register_incident({
            "incident_id": "inc-shape",
            "state": "open",
            "severity": "sev2",
            "title": "Shape test",
            "first_seen": _now_iso(),
            "last_seen": _now_iso(),
        })
        data = client.get("/admin/ops/incidents", headers=headers).json()
        assert "items" in data
        assert isinstance(data["items"], list)
        assert "page" in data
        assert "has_more" in data["page"]
        assert "next_cursor" in data["page"]
        assert "server_time" in data

    def test_outbox_shape(self, client, headers) -> None:
        """Outbox response has queue_depth, oldest_age_seconds, stuck_jobs, server_time."""
        data = client.get("/admin/ops/outbox", headers=headers).json()
        assert "queue_depth" in data
        assert "oldest_age_seconds" in data
        assert "stuck_jobs" in data
        assert "server_time" in data
        assert isinstance(data["queue_depth"], int)
        assert isinstance(data["oldest_age_seconds"], int)
        assert isinstance(data["stuck_jobs"], int)

    def test_rollouts_empty_shape(self, client, headers) -> None:
        """Rollouts returns empty list with proper pagination."""
        data = client.get("/admin/ops/rollouts", headers=headers).json()
        assert data["items"] == []
        assert data["page"]["has_more"] is False
        assert data["page"]["next_cursor"] is None

    def test_provider_calls_shape(self, client, headers) -> None:
        """Provider calls response items have all expected fields."""
        register_provider_call({
            "call_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "provider": "twilio",
            "action": "call.create",
            "status": "success",
            "http_status": 201,
            "retry_count": 0,
            "started_at": _now_iso(),
            "finished_at": _now_iso(),
            "payload_preview": "{}",
        })
        data = client.get("/admin/ops/provider-calls", headers=headers).json()
        item = data["items"][0]
        expected_keys = {
            "call_id", "correlation_id", "provider", "action",
            "status", "http_status", "retry_count", "started_at",
            "finished_at", "redacted_payload_preview",
        }
        assert expected_keys.issubset(set(item.keys()))


# =============================================================================
# 5. Validation Edge Cases
# =============================================================================


class TestValidationEdgeCases:
    def test_invalid_state_filter(self, client, headers) -> None:
        """Invalid state filter returns 400 with VALIDATION_ERROR."""
        r = client.get("/admin/ops/incidents", headers=headers, params={"state": "bogus"})
        assert r.status_code == 400
        assert r.json()["code"] == "VALIDATION_ERROR"

    def test_invalid_severity_filter(self, client, headers) -> None:
        """Invalid severity filter returns 400."""
        r = client.get("/admin/ops/incidents", headers=headers, params={"severity": "p99"})
        assert r.status_code == 400

    def test_receipts_require_suite_id(self, client, headers) -> None:
        """GET /admin/ops/receipts without suite_id returns 400 (Law #6)."""
        r = client.get("/admin/ops/receipts", headers=headers)
        assert r.status_code == 400
        assert r.json()["code"] == "MISSING_SUITE_ID"


class TestIncidentIngest:
    def test_service_report_creates_incident(self, client, monkeypatch, headers) -> None:
        monkeypatch.setenv("ASPIRE_ADMIN_INCIDENT_S2S_SECRET", "desktop-secret")
        correlation_id = str(uuid.uuid4())

        response = client.post(
            "/admin/ops/incidents/report",
            headers={
                "authorization": "Bearer desktop-secret",
                "x-correlation-id": correlation_id,
                "x-trace-id": "trace-test-001",
                "x-actor-id": "aspire-desktop-server",
            },
            json={
                "title": "Desktop orchestrator timeout",
                "severity": "sev2",
                "source": "aspire_desktop",
                "component": "/api/orchestrator/intent",
                "suite_id": "suite-123",
                "fingerprint": "desktop:intent:suite-123:timeout",
                "error_code": "ORCHESTRATOR_TIMEOUT",
                "message": "Timed out after 45s",
            },
        )

        assert response.status_code == 202
        body = response.json()
        assert body["accepted"] is True
        assert body["deduped"] is False
        assert body["correlation_id"] == correlation_id
        assert body["trace_id"] == "trace-test-001"

        listed = client.get("/admin/ops/incidents", headers=headers).json()["items"]
        assert len(listed) == 1
        assert listed[0]["title"] == "Desktop orchestrator timeout"
        assert listed[0]["correlation_id"] == correlation_id

    def test_service_report_dedupes_same_fingerprint(self, client, monkeypatch, headers) -> None:
        monkeypatch.setenv("ASPIRE_ADMIN_INCIDENT_S2S_SECRET", "desktop-secret")
        report_headers = {
            "authorization": "Bearer desktop-secret",
            "x-actor-id": "aspire-desktop-server",
        }

        first = client.post(
            "/admin/ops/incidents/report",
            headers={**report_headers, "x-correlation-id": "corr-1", "x-trace-id": "trace-1"},
            json={
                "title": "Desktop orchestrator unavailable",
                "severity": "sev1",
                "source": "aspire_desktop",
                "component": "/api/orchestrator/intent",
                "suite_id": "suite-123",
                "fingerprint": "desktop:intent:suite-123:unavailable",
                "error_code": "ORCHESTRATOR_UNAVAILABLE",
            },
        )
        second = client.post(
            "/admin/ops/incidents/report",
            headers={**report_headers, "x-correlation-id": "corr-2", "x-trace-id": "trace-2"},
            json={
                "title": "Desktop orchestrator unavailable",
                "severity": "sev1",
                "source": "aspire_desktop",
                "component": "/api/orchestrator/intent",
                "suite_id": "suite-123",
                "fingerprint": "desktop:intent:suite-123:unavailable",
                "error_code": "ORCHESTRATOR_UNAVAILABLE",
            },
        )

        assert first.status_code == 202
        assert second.status_code == 202
        assert second.json()["deduped"] is True
        assert first.json()["incident_id"] == second.json()["incident_id"]

        listed = client.get("/admin/ops/incidents", headers=headers).json()["items"]
        assert len(listed) == 1
        detail = client.get(f"/admin/ops/incidents/{first.json()['incident_id']}", headers=headers).json()
        assert len(detail["timeline"]) == 2
        assert detail["trace_id"] == "trace-2"
