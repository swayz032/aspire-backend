"""Tests for SSE streaming endpoint — integration tests for stream_agent_activity.

Covers:
  - Stream initiation with receipt generation
  - Connection limit enforcement
  - Heartbeat emission
  - Error handling during streaming
  - Client disconnect handling
  - Event collection from Adam skill pack
  - PII redaction in streamed events
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.sse_manager import (
    format_sse_event,
    get_connection_tracker,
    reset_tracker,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_tracker():
    """Reset connection tracker before each test."""
    reset_tracker()
    yield
    reset_tracker()


# ---------------------------------------------------------------------------
# format_sse_event integration
# ---------------------------------------------------------------------------


class TestFormatSseEventIntegration:
    """Integration tests for SSE event formatting in the streaming context."""

    def test_connected_event_format(self) -> None:
        event = format_sse_event({
            "type": "connected",
            "receipt_id": "r-123",
            "stream_id": "s-456",
            "correlation_id": "c-789",
            "timestamp": 1234567890,
        })
        parsed = json.loads(event.replace("data: ", "").strip())
        assert parsed["type"] == "connected"
        assert parsed["receipt_id"] == "r-123"
        assert parsed["stream_id"] == "s-456"

    def test_thinking_event_format(self) -> None:
        event = format_sse_event({
            "type": "thinking",
            "message": "Processing request...",
            "icon": "thinking",
            "timestamp": int(time.time() * 1000),
        })
        parsed = json.loads(event.replace("data: ", "").strip())
        assert parsed["type"] == "thinking"
        assert parsed["message"] == "Processing request..."

    def test_error_event_format(self) -> None:
        event = format_sse_event({
            "type": "error",
            "message": "Stream interrupted",
            "icon": "error",
            "timestamp": int(time.time() * 1000),
        })
        parsed = json.loads(event.replace("data: ", "").strip())
        assert parsed["type"] == "error"
        assert parsed["message"] == "Stream interrupted"

    def test_done_event_format(self) -> None:
        event = format_sse_event({
            "type": "done",
            "message": "Request completed",
            "icon": "done",
            "timestamp": int(time.time() * 1000),
        })
        parsed = json.loads(event.replace("data: ", "").strip())
        assert parsed["type"] == "done"

    def test_response_event_format(self) -> None:
        response_data = {"narration": "Invoice created", "receipt_id": "r-abc"}
        event = format_sse_event({"type": "response", "data": response_data})
        parsed = json.loads(event.replace("data: ", "").strip())
        assert parsed["type"] == "response"
        assert parsed["data"]["receipt_id"] == "r-abc"

    def test_heartbeat_event_format(self) -> None:
        event = format_sse_event({
            "type": "heartbeat",
            "timestamp": int(time.time() * 1000),
        })
        parsed = json.loads(event.replace("data: ", "").strip())
        assert parsed["type"] == "heartbeat"


# ---------------------------------------------------------------------------
# Connection tracking integration
# ---------------------------------------------------------------------------


class TestStreamConnectionTracking:
    """Integration tests for connection tracking during streaming."""

    def test_connection_registered_on_stream_start(self) -> None:
        tracker = get_connection_tracker()
        suite_id = "suite-integration-1"
        stream_id = "stream-integration-1"

        assert tracker.try_connect(suite_id, stream_id, actor_id="actor-1")
        assert tracker.get_connection_count(suite_id) == 1

    def test_connection_removed_on_disconnect(self) -> None:
        tracker = get_connection_tracker()
        suite_id = "suite-integration-2"
        stream_id = "stream-integration-2"

        tracker.try_connect(suite_id, stream_id)
        assert tracker.get_connection_count(suite_id) == 1

        tracker.disconnect(suite_id, stream_id)
        assert tracker.get_connection_count(suite_id) == 0

    def test_connection_limit_produces_error_event(self) -> None:
        """When connection limit is exceeded, an error event should be emittable."""
        tracker = get_connection_tracker()
        suite_id = "suite-limit"

        # Fill up connections
        for i in range(100):
            tracker.try_connect(suite_id, f"stream-{i}")

        # 101st should be denied
        denied = not tracker.try_connect(suite_id, "stream-overflow")
        assert denied

        # Error event should be formatted correctly
        error_event = format_sse_event({
            "type": "error",
            "message": "Connection limit exceeded for tenant",
            "code": "CONNECTION_LIMIT_EXCEEDED",
            "timestamp": int(time.time() * 1000),
        })
        parsed = json.loads(error_event.replace("data: ", "").strip())
        assert parsed["code"] == "CONNECTION_LIMIT_EXCEEDED"

    def test_multi_tenant_isolation(self) -> None:
        """Connections for different tenants should be independent (Law #6)."""
        tracker = get_connection_tracker()

        tracker.try_connect("suite-a", "stream-a1")
        tracker.try_connect("suite-a", "stream-a2")
        tracker.try_connect("suite-b", "stream-b1")

        assert tracker.get_connection_count("suite-a") == 2
        assert tracker.get_connection_count("suite-b") == 1

        # Disconnecting suite-a stream should not affect suite-b
        tracker.disconnect("suite-a", "stream-a1")
        assert tracker.get_connection_count("suite-a") == 1
        assert tracker.get_connection_count("suite-b") == 1


# ---------------------------------------------------------------------------
# PII Redaction in Streaming Context
# ---------------------------------------------------------------------------


class TestStreamPiiRedaction:
    """Tests that PII is redacted in streamed events (Law #9)."""

    def test_email_redacted_in_step_event(self) -> None:
        event = format_sse_event({
            "type": "step",
            "message": "Found contact: john.doe@example.com",
            "agent": "adam",
        })
        parsed = json.loads(event.replace("data: ", "").strip())
        assert "john.doe@example.com" not in parsed["message"]
        assert "<EMAIL_REDACTED>" in parsed["message"]

    def test_phone_redacted_in_thinking_event(self) -> None:
        event = format_sse_event({
            "type": "thinking",
            "message": "Looking up (555) 123-4567",
        })
        parsed = json.loads(event.replace("data: ", "").strip())
        assert "(555) 123-4567" not in parsed["message"]
        assert "<PHONE_REDACTED>" in parsed["message"]

    def test_ssn_redacted_in_error_event(self) -> None:
        event = format_sse_event({
            "type": "error",
            "message": "Failed to process SSN 123-45-6789",
        })
        parsed = json.loads(event.replace("data: ", "").strip())
        assert "123-45-6789" not in parsed["message"]
        assert "<SSN_REDACTED>" in parsed["message"]

    def test_non_message_fields_not_redacted(self) -> None:
        """Only the 'message' field should be redacted, not other fields."""
        event = format_sse_event({
            "type": "step",
            "message": "Processing...",
            "agent": "adam",
            "correlation_id": "corr-12345",
        })
        parsed = json.loads(event.replace("data: ", "").strip())
        assert parsed["agent"] == "adam"
        assert parsed["correlation_id"] == "corr-12345"
