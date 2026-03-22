"""SSE Connection Manager — Enterprise-grade Server-Sent Events for Canvas Mode.

Manages SSE connections with:
  - Per-tenant connection limits (max 100 concurrent connections per suite_id)
  - Heartbeat generation (every 15s to prevent proxy timeouts)
  - Per-stream rate limiting (max 10 events/second)
  - PII redaction on all outbound event messages (Law #9)
  - Receipt generation for stream lifecycle events (Law #2)
  - Correlation ID propagation

Law compliance:
  - Law #1: SSE is a transport layer. Orchestrator decides; SSE delivers.
  - Law #2: Stream initiation and termination produce receipts.
  - Law #3: Connection limit exceeded -> deny with receipt.
  - Law #6: Connection tracking is per-tenant (suite_id).
  - Law #7: SSE manager is a transport tool, not a decision maker.
  - Law #9: All event messages are PII-redacted before emission.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CONNECTIONS_PER_TENANT = 100
HEARTBEAT_INTERVAL_SECONDS = 15.0
MAX_EVENTS_PER_SECOND = 10
RATE_LIMIT_WINDOW_SECONDS = 1.0

# ---------------------------------------------------------------------------
# PII Redaction (Law #9 — inline for SSE, mirrors server.py _redact_pii)
# ---------------------------------------------------------------------------

_SSN_RE = re.compile(r'\b\d{3}-\d{2}-\d{4}\b')
_CC_RE = re.compile(r'\b\d{13,19}\b')
_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
_PHONE_RE = re.compile(
    r'(?:\+?1[-.\s]?)?(?:\(\d{3}\)\s?|\b\d{3}[-.\s])\d{3}[-.\s]?\d{4}\b'
)


def redact_pii(text: str) -> str:
    """Redact PII from text per Law #9 redaction rules.

    Handles: SSN, credit card numbers, email addresses, phone numbers.
    """
    if not text:
        return text
    text = _SSN_RE.sub('<SSN_REDACTED>', text)
    text = _CC_RE.sub('<CC_REDACTED>', text)
    text = _EMAIL_RE.sub('<EMAIL_REDACTED>', text)
    text = _PHONE_RE.sub('<PHONE_REDACTED>', text)
    return text


# ---------------------------------------------------------------------------
# SSE Event Formatting
# ---------------------------------------------------------------------------


def format_sse_event(data: dict[str, Any], event_type: str | None = None) -> str:
    """Format a dict as an SSE event string.

    SSE format:
      event: <type>\\n     (optional)
      data: <json>\\n
      \\n

    The message field is PII-redacted before serialization.
    """
    # Redact PII from message field (Law #9)
    if "message" in data and isinstance(data["message"], str):
        data = {**data, "message": redact_pii(data["message"])}

    lines: list[str] = []
    if event_type:
        lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(data)}")
    lines.append("")  # trailing newline to complete the event
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Connection Tracker
# ---------------------------------------------------------------------------


class _ConnectionTracker:
    """In-memory tracker for active SSE connections per tenant.

    Thread-safety note: FastAPI runs on a single event loop, so dict operations
    are safe. For multi-worker deployments, replace with Redis.
    """

    __slots__ = ("_connections", "_stream_metadata")

    def __init__(self) -> None:
        # suite_id -> set of stream_ids
        self._connections: dict[str, set[str]] = defaultdict(set)
        # stream_id -> metadata (for diagnostics / receipt generation)
        self._stream_metadata: dict[str, dict[str, Any]] = {}

    def try_connect(
        self,
        suite_id: str,
        stream_id: str,
        *,
        actor_id: str = "",
        correlation_id: str = "",
    ) -> bool:
        """Attempt to register a new SSE connection.

        Returns True if allowed, False if connection limit exceeded (Law #3).
        """
        if len(self._connections[suite_id]) >= MAX_CONNECTIONS_PER_TENANT:
            logger.warning(
                "SSE connection limit exceeded for suite %s (%d/%d)",
                suite_id[:8],
                len(self._connections[suite_id]),
                MAX_CONNECTIONS_PER_TENANT,
            )
            return False

        self._connections[suite_id].add(stream_id)
        self._stream_metadata[stream_id] = {
            "suite_id": suite_id,
            "actor_id": actor_id,
            "correlation_id": correlation_id,
            "connected_at": time.monotonic(),
            "event_count": 0,
        }
        return True

    def disconnect(self, suite_id: str, stream_id: str) -> None:
        """Remove a connection from tracking."""
        self._connections[suite_id].discard(stream_id)
        if not self._connections[suite_id]:
            del self._connections[suite_id]
        self._stream_metadata.pop(stream_id, None)

    def get_connection_count(self, suite_id: str) -> int:
        """Get active connection count for a tenant."""
        return len(self._connections.get(suite_id, set()))

    def get_total_connections(self) -> int:
        """Get total active connections across all tenants."""
        return sum(len(s) for s in self._connections.values())

    def increment_event_count(self, stream_id: str) -> None:
        """Increment event counter for a stream (diagnostics)."""
        meta = self._stream_metadata.get(stream_id)
        if meta:
            meta["event_count"] = meta.get("event_count", 0) + 1

    def get_metadata(self, stream_id: str) -> dict[str, Any] | None:
        """Get metadata for a stream."""
        return self._stream_metadata.get(stream_id)


# Singleton instance
_tracker = _ConnectionTracker()


def get_connection_tracker() -> _ConnectionTracker:
    """Get the singleton connection tracker."""
    return _tracker


# ---------------------------------------------------------------------------
# Rate Limiter (per-stream, 10 events/second)
# ---------------------------------------------------------------------------


class StreamRateLimiter:
    """Token bucket rate limiter for individual SSE streams.

    Allows bursts up to MAX_EVENTS_PER_SECOND within a 1-second window.
    """

    __slots__ = ("_max_events", "_window", "_timestamps")

    def __init__(
        self,
        max_events: int = MAX_EVENTS_PER_SECOND,
        window: float = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self._max_events = max_events
        self._window = window
        self._timestamps: list[float] = []

    def check(self) -> bool:
        """Check if an event can be emitted. Returns True if allowed."""
        now = time.monotonic()
        # Prune timestamps outside the window
        self._timestamps = [
            ts for ts in self._timestamps if now - ts < self._window
        ]
        if len(self._timestamps) >= self._max_events:
            return False
        self._timestamps.append(now)
        return True

    @property
    def remaining(self) -> int:
        """Number of events that can still be emitted in the current window."""
        now = time.monotonic()
        active = sum(1 for ts in self._timestamps if now - ts < self._window)
        return max(0, self._max_events - active)


# ---------------------------------------------------------------------------
# Receipt Helpers (Law #2)
# ---------------------------------------------------------------------------


def build_stream_receipt(
    *,
    action_type: str,
    suite_id: str,
    office_id: str,
    actor_id: str,
    correlation_id: str,
    outcome: str,
    stream_id: str,
    reason_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for SSE stream lifecycle events.

    Used for: stream.initiate, stream.complete, stream.error, stream.denied
    """
    now = datetime.now(timezone.utc).isoformat()
    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": actor_id,
        "action_type": action_type,
        "risk_tier": "green",
        "tool_used": "sse_manager",
        "outcome": outcome,
        "reason_code": reason_code,
        "created_at": now,
        "receipt_type": "streaming",
        "redacted_inputs": None,
        "redacted_outputs": details or {"stream_id": stream_id},
    }
    receipt["receipt_hash"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, default=str).encode()
    ).hexdigest()
    return receipt


# ---------------------------------------------------------------------------
# Reset (for testing)
# ---------------------------------------------------------------------------


def reset_tracker() -> None:
    """Reset the global connection tracker. Used in tests only."""
    global _tracker
    _tracker = _ConnectionTracker()
