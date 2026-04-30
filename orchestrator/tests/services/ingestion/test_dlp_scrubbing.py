"""DLP scrubbing integration tests (Pass 18, THREAT-016).

Verifies:
  1. SSN in body → masked in memory_objects.detail (not stored raw).
  2. Email in free-text field → Presidio entity replacement.
  3. Email in identity field (viewer_email) → SHA-256 hash, raw not stored.
  4. DLP failure is fail-open — ingestion proceeds with original detail.
  5. Nested dict fields are recursively scrubbed.
  6. List items with text fields are scrubbed.
  7. scrub_text async wrapper returns correct result.

These are unit tests against the DLP helpers — they do NOT require a running
Supabase or Presidio server (Presidio is lazy-loaded and regex-fallback handles
the entity detection without the full ML model in CI).
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.dlp import scrub_text
from aspire_orchestrator.services.ingestion.base import BaseIngestionAdapter


# ---------------------------------------------------------------------------
# Minimal concrete adapter for testing _scrub_detail_pii
# ---------------------------------------------------------------------------

class _ConcreteAdapter(BaseIngestionAdapter):
    """Minimal concrete subclass — all abstract methods raise NotImplementedError."""

    provider_name = "test_provider"
    memory_type = "sms_thread"

    async def verify_signature(self, *, body: bytes, headers: Any) -> bool:  # type: ignore[override]
        raise NotImplementedError

    async def resolve_scope(self, payload: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def build_envelope(self, payload: Any, *, scope: Any, thread: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError


@pytest.fixture()
def adapter() -> _ConcreteAdapter:
    return _ConcreteAdapter()


# ---------------------------------------------------------------------------
# TASK 1a — scrub_text async wrapper
# ---------------------------------------------------------------------------

def test_scrub_text_returns_string() -> None:
    """scrub_text must be awaitable and return a string."""
    result = asyncio.get_event_loop().run_until_complete(scrub_text("hello world"))
    assert isinstance(result, str)


def test_scrub_text_passes_through_safe_text() -> None:
    """scrub_text must not mangle non-PII text."""
    safe = "Invoice total: $150. Reference: INV-001."
    result = asyncio.get_event_loop().run_until_complete(scrub_text(safe))
    assert "150" in result or "INV" in result  # structure preserved


def test_scrub_text_redacts_ssn() -> None:
    """SSN in free text must be replaced — raw digits must not appear."""
    text = "Customer SSN is 123-45-6789, please process."
    result = asyncio.get_event_loop().run_until_complete(scrub_text(text))
    # Raw SSN must not survive — either Presidio or regex fallback catches it
    assert "123-45-6789" not in result
    assert "REDACTED" in result or "<" in result


def test_scrub_text_redacts_credit_card() -> None:
    """Credit card number must be replaced."""
    text = "Card used: 4111 1111 1111 1111 for this transaction."
    result = asyncio.get_event_loop().run_until_complete(scrub_text(text))
    assert "4111 1111 1111 1111" not in result


def test_scrub_text_empty_string_passthrough() -> None:
    """Empty string must be returned as-is without errors."""
    result = asyncio.get_event_loop().run_until_complete(scrub_text(""))
    assert result == ""


# ---------------------------------------------------------------------------
# TASK 1b — _scrub_detail_pii on body field (SMS inbound scenario)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scrub_detail_pii_ssn_in_body(adapter: _ConcreteAdapter) -> None:
    """SSN embedded in SMS body must not appear in scrubbed detail."""
    detail = {
        "from": "+15551234567",
        "to": "+12125550198",
        "body": "My SSN is 123-45-6789 please update my records.",
        "message_sid": "SM123",
    }
    scrubbed = await adapter._scrub_detail_pii(detail)

    assert "123-45-6789" not in scrubbed["body"], (
        "Raw SSN must not be stored in memory_objects.detail"
    )
    # Non-PII fields must be preserved
    assert scrubbed["from"] == detail["from"]
    assert scrubbed["to"] == detail["to"]
    assert scrubbed["message_sid"] == "SM123"


@pytest.mark.asyncio
async def test_scrub_detail_pii_email_in_body(adapter: _ConcreteAdapter) -> None:
    """Email address in body must be scrubbed by Presidio / regex fallback."""
    detail = {
        "body": "Please contact john.doe@example.com for details.",
    }
    scrubbed = await adapter._scrub_detail_pii(detail)
    # Email must be replaced — either EMAIL_REDACTED or REDACTED placeholder
    assert "john.doe@example.com" not in scrubbed["body"]


@pytest.mark.asyncio
async def test_scrub_detail_pii_viewer_email_hashed(adapter: _ConcreteAdapter) -> None:
    """viewer_email (identity field) must be hashed, not blanked or passed through."""
    detail = {
        "viewer_email": "alice@example.com",
        "amount": "500.00",
    }
    scrubbed = await adapter._scrub_detail_pii(detail)

    # Raw email must not appear
    assert "alice@example.com" not in scrubbed["viewer_email"]
    # Must start with a 3-char prefix for correlation traceability
    assert scrubbed["viewer_email"].startswith("ali")
    # Must contain the hash marker
    assert "sha256:" in scrubbed["viewer_email"]
    # Non-PII field preserved
    assert scrubbed["amount"] == "500.00"


@pytest.mark.asyncio
async def test_scrub_detail_pii_nested_dict(adapter: _ConcreteAdapter) -> None:
    """Nested dict with transcript_text must be recursively scrubbed."""
    detail = {
        "metadata": {
            "duration_secs": 120,
            "body": "caller SSN is 987-65-4321",
        },
    }
    scrubbed = await adapter._scrub_detail_pii(detail)
    assert "987-65-4321" not in scrubbed["metadata"]["body"]
    assert scrubbed["metadata"]["duration_secs"] == 120


@pytest.mark.asyncio
async def test_scrub_detail_pii_list_of_turns(adapter: _ConcreteAdapter) -> None:
    """List of transcript turn dicts must each be recursively scrubbed."""
    detail = {
        "transcript": [
            {"role": "user", "message": "My card is 4111 1111 1111 1111"},
            {"role": "agent", "message": "Thank you, I'll process that now."},
        ],
    }
    scrubbed = await adapter._scrub_detail_pii(detail)
    turns = scrubbed["transcript"]
    assert "4111 1111 1111 1111" not in turns[0]["message"]
    # Agent turn without PII should be unchanged or minimally modified
    assert "Thank you" in turns[1]["message"]


@pytest.mark.asyncio
async def test_scrub_detail_pii_does_not_mutate_input(adapter: _ConcreteAdapter) -> None:
    """Input dict must not be mutated — _scrub_detail_pii returns a new dict."""
    original = {"body": "SSN: 123-45-6789", "id": "abc123"}
    original_copy = copy.deepcopy(original)
    await adapter._scrub_detail_pii(original)
    assert original == original_copy


@pytest.mark.asyncio
async def test_scrub_detail_pii_fail_open_on_dlp_error(adapter: _ConcreteAdapter) -> None:
    """If scrub_text raises, _scrub_detail_pii logs warning and returns original field value.

    This verifies the fail-open policy — DLP failure must NOT abort ingestion.
    """
    detail = {"body": "Some text with SSN 123-45-6789"}

    with patch(
        "aspire_orchestrator.services.ingestion.base.scrub_text",
        side_effect=RuntimeError("presidio exploded"),
    ):
        scrubbed = await adapter._scrub_detail_pii(detail)

    # On error: original value preserved (fail-open)
    assert scrubbed["body"] == detail["body"]


# ---------------------------------------------------------------------------
# TASK 1c — ingest() wires scrubbing before write
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_scrubs_before_write() -> None:
    """End-to-end: ingest() must call _scrub_detail_pii before MemoryService.write.

    We mock at the boundary just after build_envelope to verify scrubbing
    actually happens — if DLP is bypassed the raw SSN would reach the mock write.
    """
    from unittest.mock import AsyncMock, patch, MagicMock
    from aspire_orchestrator.schemas.memory_v1 import (
        MemoryObjectIn, ScopedIdentity, Provenance
    )
    import uuid

    scope = ScopedIdentity(
        tenant_id=uuid.uuid4(),
        suite_id=uuid.uuid4(),
        office_id=uuid.uuid4(),
    )

    raw_ssn = "111-22-3333"
    envelope = MemoryObjectIn(
        scope=scope,
        memory_type="sms_thread",
        title="Test",
        summary=f"Summary with SSN {raw_ssn}",
        detail={"body": f"Message body with SSN {raw_ssn}"},
        idempotency_key="test-idem-key-001",
        provenance=Provenance(
            trace_id=uuid.uuid4(),
            correlation_id=uuid.uuid4(),
            runtime_family="provider_webhook",
        ),
    )

    captured_envelopes: list[MemoryObjectIn] = []

    async def mock_write(env: MemoryObjectIn, *, scope: Any, embed: bool) -> Any:
        captured_envelopes.append(env)
        mock_out = MagicMock()
        mock_out.memory_id = uuid.uuid4()
        return mock_out

    adapter = _ConcreteAdapter()
    adapter._memory_service = MagicMock()
    adapter._memory_service.write = mock_write

    with (
        patch.object(adapter, "verify_signature", return_value=True),
        patch.object(adapter, "resolve_scope", return_value=scope),
        patch.object(adapter, "build_envelope", return_value=envelope),
        patch.object(adapter, "thread_envelope", return_value=None),
    ):
        result = await adapter.ingest(
            body=b"{}",
            headers={},
            payload={},
        )

    assert len(captured_envelopes) == 1
    written = captured_envelopes[0]
    # Raw SSN must not appear in either detail or summary reaching the write call
    assert raw_ssn not in (written.detail.get("body") or ""), (
        "Raw SSN reached MemoryService.write — DLP scrubbing is not wired correctly"
    )
    assert raw_ssn not in (written.summary or ""), (
        "Raw SSN in summary reached MemoryService.write"
    )
