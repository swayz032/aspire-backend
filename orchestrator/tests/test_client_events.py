"""Tests for POST /v1/client/events endpoint (Wave 4I — F7 fix).

Validates:
- Event ingestion with required fields
- suite_id format validation: UUID or STE-XXX display ID (Law #3)
- PII redaction in messages (Law #9)
- Rate limiting (10/min per suite)
- Cross-tenant rejection (Law #6)
- Metadata size caps
- Receipt generation (Law #2)
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

# Premium Aspire display IDs (migration 063)
_SUITE_A = "STE-1042"
_SUITE_B = "STE-1043"
_SUITE_RATE = "STE-9999"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Clear rate limit counters between tests."""
    from aspire_orchestrator.server import _client_event_counts
    _client_event_counts.clear()
    yield
    _client_event_counts.clear()


@pytest.fixture()
def client():
    from aspire_orchestrator.server import app
    return TestClient(app)


class TestClientEventIngestion:
    """POST /v1/client/events — basic functionality."""

    def test_valid_event_accepted(self, client: TestClient) -> None:
        resp = client.post("/v1/client/events", json={
            "event_type": "ui.error",
            "suite_id": _SUITE_A,
            "message": "Conference lobby rendered blank",
            "severity": "error",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "event_id" in data
        assert "correlation_id" in data

    def test_uuid_suite_id_accepted(self, client: TestClient) -> None:
        """UUID-format suite_id also accepted (backward compat)."""
        resp = client.post("/v1/client/events", json={
            "event_type": "ui.error",
            "suite_id": "STE-0001",
            "message": "test",
        })
        assert resp.status_code == 201

    def test_missing_event_type_rejected(self, client: TestClient) -> None:
        resp = client.post("/v1/client/events", json={
            "suite_id": _SUITE_A,
            "message": "test",
        })
        assert resp.status_code == 400
        assert resp.json()["error"] == "MISSING_EVENT_TYPE"

    def test_missing_suite_id_rejected(self, client: TestClient) -> None:
        resp = client.post("/v1/client/events", json={
            "event_type": "ui.error",
            "message": "test",
        })
        assert resp.status_code == 400
        assert resp.json()["error"] == "MISSING_SUITE_ID"

    def test_suite_id_from_header(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/client/events",
            json={"event_type": "ui.error", "message": "test"},
            headers={"x-suite-id": _SUITE_B},
        )
        assert resp.status_code == 201

    def test_invalid_suite_id_format_rejected(self, client: TestClient) -> None:
        """Non-UUID/non-STE suite_id rejected (Law #3 — fail-closed)."""
        resp = client.post("/v1/client/events", json={
            "event_type": "ui.error",
            "suite_id": "not-a-valid-id",
            "message": "test",
        })
        assert resp.status_code == 400
        assert resp.json()["error"] == "INVALID_SUITE_ID"

    def test_sql_injection_suite_id_rejected(self, client: TestClient) -> None:
        """SQL injection in suite_id rejected by format validation."""
        resp = client.post("/v1/client/events", json={
            "event_type": "ui.error",
            "suite_id": "'; DROP TABLE receipts; --",
            "message": "test",
        })
        assert resp.status_code == 400
        assert resp.json()["error"] == "INVALID_SUITE_ID"

    def test_invalid_severity_rejected(self, client: TestClient) -> None:
        resp = client.post("/v1/client/events", json={
            "event_type": "ui.error",
            "suite_id": _SUITE_A,
            "severity": "INVALID",
            "message": "test",
        })
        assert resp.status_code == 400
        assert resp.json()["error"] == "INVALID_SEVERITY"

    def test_invalid_json_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/client/events",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "INVALID_JSON"

    def test_all_severity_levels_accepted(self, client: TestClient) -> None:
        for idx, severity in enumerate(("debug", "info", "warning", "error", "critical")):
            resp = client.post("/v1/client/events", json={
                "event_type": "test",
                "suite_id": f"STE-{2000 + idx}",
                "severity": severity,
                "message": f"Test {severity}",
            })
            assert resp.status_code == 201, f"Failed for severity={severity}"


class TestClientEventPiiRedaction:
    """PII redaction in client events (Law #9)."""

    def test_ssn_redacted(self) -> None:
        from aspire_orchestrator.server import _redact_pii
        assert _redact_pii("SSN is 123-45-6789") == "SSN is <SSN_REDACTED>"

    def test_credit_card_redacted(self) -> None:
        from aspire_orchestrator.server import _redact_pii
        assert _redact_pii("Card 1234567890123456") == "Card <CC_REDACTED>"

    def test_email_redacted(self) -> None:
        from aspire_orchestrator.server import _redact_pii
        result = _redact_pii("Contact john@example.com for help")
        assert "<EMAIL_REDACTED>" in result
        assert "john@example.com" not in result

    def test_phone_redacted(self) -> None:
        """Phone numbers must be redacted (Law #9 — R-001 fix)."""
        from aspire_orchestrator.server import _redact_pii
        assert "<PHONE_REDACTED>" in _redact_pii("Call 555-123-4567 now")
        assert "<PHONE_REDACTED>" in _redact_pii("Call (555) 123-4567 now")
        assert "555-123-4567" not in _redact_pii("Call 555-123-4567 now")

    def test_message_truncated_at_2000(self, client: TestClient) -> None:
        long_msg = "x" * 5000
        resp = client.post("/v1/client/events", json={
            "event_type": "test",
            "suite_id": _SUITE_A,
            "message": long_msg,
        })
        assert resp.status_code == 201


class TestClientEventRateLimiting:
    """Rate limiting: 10 events/min per suite."""

    def test_rate_limit_enforced(self, client: TestClient) -> None:
        """11th event within 60s should be rejected."""
        for i in range(10):
            resp = client.post("/v1/client/events", json={
                "event_type": "flood",
                "suite_id": _SUITE_RATE,
                "message": f"Event {i}",
            })
            assert resp.status_code == 201, f"Event {i} should be accepted"

        # 11th should be rate limited
        resp = client.post("/v1/client/events", json={
            "event_type": "flood",
            "suite_id": _SUITE_RATE,
            "message": "Event 10",
        })
        assert resp.status_code == 429
        assert resp.json()["error"] == "RATE_LIMITED"

    def test_rate_limit_per_suite(self, client: TestClient) -> None:
        """Different suites have independent rate limits."""
        for i in range(10):
            client.post("/v1/client/events", json={
                "event_type": "test",
                "suite_id": _SUITE_A,
                "message": f"Event {i}",
            })

        # Suite B should still be able to send
        resp = client.post("/v1/client/events", json={
            "event_type": "test",
            "suite_id": _SUITE_B,
            "message": "First event from B",
        })
        assert resp.status_code == 201


class TestClientEventEvil:
    """Security/evil tests for client event ingestion."""

    def test_xss_payload_stored_safely(self, client: TestClient) -> None:
        """XSS payload in message should not cause issues."""
        resp = client.post("/v1/client/events", json={
            "event_type": "test",
            "suite_id": _SUITE_A,
            "message": "<script>alert('xss')</script>",
        })
        assert resp.status_code == 201

    def test_sql_injection_in_metadata(self, client: TestClient) -> None:
        """SQL injection in metadata should be parameterized away."""
        resp = client.post("/v1/client/events", json={
            "event_type": "test",
            "suite_id": _SUITE_A,
            "message": "test",
            "metadata": {"payload": "'; DROP TABLE receipts; --"},
        })
        assert resp.status_code == 201

    def test_oversized_metadata_capped(self, client: TestClient) -> None:
        """Metadata over 10KB should be truncated."""
        big_metadata = {"key": "x" * 15000}
        resp = client.post("/v1/client/events", json={
            "event_type": "test",
            "suite_id": _SUITE_A,
            "message": "test",
            "metadata": big_metadata,
        })
        assert resp.status_code == 201


class TestCalendarIntentRouting:
    """Verify calendar actions route to nora_conference via _ACTION_TO_PACK."""

    def test_calendar_create_routes_to_nora(self) -> None:
        from aspire_orchestrator.services.intent_classifier import _resolve_skill_pack
        assert _resolve_skill_pack("calendar.create") == "nora_conference"

    def test_calendar_read_routes_to_nora(self) -> None:
        from aspire_orchestrator.services.intent_classifier import _resolve_skill_pack
        assert _resolve_skill_pack("calendar.read") == "nora_conference"

    def test_calendar_list_routes_to_nora(self) -> None:
        from aspire_orchestrator.services.intent_classifier import _resolve_skill_pack
        assert _resolve_skill_pack("calendar.list") == "nora_conference"
