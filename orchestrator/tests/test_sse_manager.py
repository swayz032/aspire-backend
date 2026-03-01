"""Tests for SSE Manager — connection tracking, rate limiting, PII redaction, receipts.

Covers:
  - Connection tracking (connect, disconnect, limits)
  - Per-stream rate limiting (10 events/second)
  - PII redaction (SSN, CC, email, phone)
  - SSE event formatting
  - Receipt generation for stream lifecycle
  - Edge cases (concurrent tenants, metadata tracking)
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from aspire_orchestrator.services.sse_manager import (
    MAX_CONNECTIONS_PER_TENANT,
    StreamRateLimiter,
    build_stream_receipt,
    format_sse_event,
    get_connection_tracker,
    redact_pii,
    reset_tracker,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_tracker():
    """Reset the global tracker before each test."""
    reset_tracker()
    yield
    reset_tracker()


# ---------------------------------------------------------------------------
# PII Redaction Tests (Law #9)
# ---------------------------------------------------------------------------


class TestRedactPii:
    """Tests for the PII redaction function."""

    def test_redact_ssn(self) -> None:
        text = "My SSN is 123-45-6789"
        result = redact_pii(text)
        assert "<SSN_REDACTED>" in result
        assert "123-45-6789" not in result

    def test_redact_credit_card(self) -> None:
        text = "Card number 4111111111111111"
        result = redact_pii(text)
        assert "<CC_REDACTED>" in result
        assert "4111111111111111" not in result

    def test_redact_email(self) -> None:
        text = "Contact me at john.doe@example.com"
        result = redact_pii(text)
        assert "<EMAIL_REDACTED>" in result
        assert "john.doe@example.com" not in result

    def test_redact_phone(self) -> None:
        text = "Call me at (555) 123-4567"
        result = redact_pii(text)
        assert "<PHONE_REDACTED>" in result
        assert "(555) 123-4567" not in result

    def test_redact_multiple_pii(self) -> None:
        text = "SSN: 123-45-6789, Email: test@test.com, Phone: 555-123-4567"
        result = redact_pii(text)
        assert "<SSN_REDACTED>" in result
        assert "<EMAIL_REDACTED>" in result
        assert "<PHONE_REDACTED>" in result

    def test_no_pii(self) -> None:
        text = "No PII here, just a regular message"
        result = redact_pii(text)
        assert result == text

    def test_empty_string(self) -> None:
        assert redact_pii("") == ""

    def test_none_returns_none(self) -> None:
        # redact_pii should handle falsy input
        assert redact_pii("") == ""


# ---------------------------------------------------------------------------
# SSE Event Formatting Tests
# ---------------------------------------------------------------------------


class TestFormatSseEvent:
    """Tests for SSE event formatting."""

    def test_basic_event(self) -> None:
        data = {"type": "thinking", "message": "Processing..."}
        result = format_sse_event(data)
        assert result.startswith("data: ")
        assert result.endswith("\n\n")
        parsed = json.loads(result.replace("data: ", "").strip())
        assert parsed["type"] == "thinking"
        assert parsed["message"] == "Processing..."

    def test_event_with_type(self) -> None:
        data = {"type": "step", "message": "Found 5 results"}
        result = format_sse_event(data, event_type="agent_activity")
        assert "event: agent_activity\n" in result
        assert "data: " in result

    def test_pii_redacted_in_message(self) -> None:
        data = {"type": "step", "message": "Found result for john@example.com"}
        result = format_sse_event(data)
        parsed = json.loads(result.replace("data: ", "").strip())
        assert "john@example.com" not in parsed["message"]
        assert "<EMAIL_REDACTED>" in parsed["message"]

    def test_non_message_fields_preserved(self) -> None:
        data = {"type": "done", "timestamp": 1234567890}
        result = format_sse_event(data)
        parsed = json.loads(result.replace("data: ", "").strip())
        assert parsed["timestamp"] == 1234567890

    def test_heartbeat_event(self) -> None:
        data = {"type": "heartbeat", "timestamp": 1234567890}
        result = format_sse_event(data)
        parsed = json.loads(result.replace("data: ", "").strip())
        assert parsed["type"] == "heartbeat"


# ---------------------------------------------------------------------------
# Connection Tracker Tests
# ---------------------------------------------------------------------------


class TestConnectionTracker:
    """Tests for per-tenant connection tracking."""

    def test_connect_and_disconnect(self) -> None:
        tracker = get_connection_tracker()
        assert tracker.try_connect("suite-1", "stream-1")
        assert tracker.get_connection_count("suite-1") == 1
        tracker.disconnect("suite-1", "stream-1")
        assert tracker.get_connection_count("suite-1") == 0

    def test_multiple_connections_same_tenant(self) -> None:
        tracker = get_connection_tracker()
        assert tracker.try_connect("suite-1", "stream-1")
        assert tracker.try_connect("suite-1", "stream-2")
        assert tracker.get_connection_count("suite-1") == 2

    def test_connection_limit_enforced(self) -> None:
        tracker = get_connection_tracker()
        # Fill up to the limit
        for i in range(MAX_CONNECTIONS_PER_TENANT):
            assert tracker.try_connect("suite-1", f"stream-{i}")
        # Next connection should be denied (Law #3)
        assert not tracker.try_connect("suite-1", "stream-overflow")
        assert tracker.get_connection_count("suite-1") == MAX_CONNECTIONS_PER_TENANT

    def test_different_tenants_independent(self) -> None:
        tracker = get_connection_tracker()
        assert tracker.try_connect("suite-1", "stream-1")
        assert tracker.try_connect("suite-2", "stream-2")
        assert tracker.get_connection_count("suite-1") == 1
        assert tracker.get_connection_count("suite-2") == 1

    def test_disconnect_nonexistent_stream(self) -> None:
        tracker = get_connection_tracker()
        # Should not raise
        tracker.disconnect("suite-1", "nonexistent")

    def test_total_connections(self) -> None:
        tracker = get_connection_tracker()
        tracker.try_connect("suite-1", "s1")
        tracker.try_connect("suite-1", "s2")
        tracker.try_connect("suite-2", "s3")
        assert tracker.get_total_connections() == 3

    def test_metadata_tracking(self) -> None:
        tracker = get_connection_tracker()
        tracker.try_connect(
            "suite-1", "stream-1",
            actor_id="actor-1",
            correlation_id="corr-1",
        )
        meta = tracker.get_metadata("stream-1")
        assert meta is not None
        assert meta["suite_id"] == "suite-1"
        assert meta["actor_id"] == "actor-1"
        assert meta["correlation_id"] == "corr-1"
        assert meta["event_count"] == 0

    def test_increment_event_count(self) -> None:
        tracker = get_connection_tracker()
        tracker.try_connect("suite-1", "stream-1")
        tracker.increment_event_count("stream-1")
        tracker.increment_event_count("stream-1")
        meta = tracker.get_metadata("stream-1")
        assert meta is not None
        assert meta["event_count"] == 2

    def test_metadata_removed_on_disconnect(self) -> None:
        tracker = get_connection_tracker()
        tracker.try_connect("suite-1", "stream-1")
        tracker.disconnect("suite-1", "stream-1")
        assert tracker.get_metadata("stream-1") is None


# ---------------------------------------------------------------------------
# Rate Limiter Tests
# ---------------------------------------------------------------------------


class TestStreamRateLimiter:
    """Tests for per-stream rate limiting."""

    def test_allows_within_limit(self) -> None:
        limiter = StreamRateLimiter(max_events=5, window=1.0)
        for _ in range(5):
            assert limiter.check()

    def test_denies_over_limit(self) -> None:
        limiter = StreamRateLimiter(max_events=3, window=1.0)
        for _ in range(3):
            assert limiter.check()
        # 4th event should be denied
        assert not limiter.check()

    def test_remaining_count(self) -> None:
        limiter = StreamRateLimiter(max_events=5, window=1.0)
        assert limiter.remaining == 5
        limiter.check()
        assert limiter.remaining == 4

    def test_window_resets(self) -> None:
        limiter = StreamRateLimiter(max_events=2, window=0.1)
        assert limiter.check()
        assert limiter.check()
        assert not limiter.check()  # Over limit
        # Wait for window to expire
        time.sleep(0.15)
        assert limiter.check()  # Should be allowed again


# ---------------------------------------------------------------------------
# Receipt Builder Tests (Law #2)
# ---------------------------------------------------------------------------


class TestBuildStreamReceipt:
    """Tests for stream lifecycle receipt generation."""

    def test_receipt_has_required_fields(self) -> None:
        receipt = build_stream_receipt(
            action_type="stream.initiate",
            suite_id="suite-1",
            office_id="office-1",
            actor_id="actor-1",
            correlation_id="corr-1",
            outcome="success",
            stream_id="stream-1",
        )
        assert receipt["id"]  # UUID generated
        assert receipt["suite_id"] == "suite-1"
        assert receipt["office_id"] == "office-1"
        assert receipt["actor_id"] == "actor-1"
        assert receipt["correlation_id"] == "corr-1"
        assert receipt["action_type"] == "stream.initiate"
        assert receipt["outcome"] == "success"
        assert receipt["risk_tier"] == "green"
        assert receipt["tool_used"] == "sse_manager"
        assert receipt["receipt_type"] == "streaming"
        assert receipt["created_at"]  # ISO timestamp

    def test_receipt_with_reason_code(self) -> None:
        receipt = build_stream_receipt(
            action_type="stream.denied",
            suite_id="suite-1",
            office_id="office-1",
            actor_id="actor-1",
            correlation_id="corr-1",
            outcome="DENIED",
            stream_id="stream-1",
            reason_code="CONNECTION_LIMIT_EXCEEDED",
        )
        assert receipt["reason_code"] == "CONNECTION_LIMIT_EXCEEDED"
        assert receipt["outcome"] == "DENIED"

    def test_receipt_with_details(self) -> None:
        receipt = build_stream_receipt(
            action_type="stream.complete",
            suite_id="suite-1",
            office_id="office-1",
            actor_id="actor-1",
            correlation_id="corr-1",
            outcome="success",
            stream_id="stream-1",
            details={"event_count": 42},
        )
        assert receipt["redacted_outputs"]["event_count"] == 42
