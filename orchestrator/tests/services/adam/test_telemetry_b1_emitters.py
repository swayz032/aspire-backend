"""Phase B-1 — tests for adam/telemetry.py B-1 emitters.

Covers:
  - emit_provider_call_timed populates the spec'd structured-log field names
    (adam.<provider>.duration_ms / cold_start_detected / outcome) on the
    TelemetryEvent.extra dict so downstream log shippers can scrape them
    without a transform layer.
  - cold_start_detected is auto-derived from duration_ms when not provided.
  - A FAILED call is never auto-classified as cold-start even if slow.
  - emit_playbook_outcome populates adam.playbook.outcome + degraded_providers
    + providers_called on extra.
  - degraded_providers is always a list (defensive: None -> []).

These are pure unit tests over the in-memory `_event_buffer` — no I/O,
no Supabase, no Grafana. The emitters route through the existing
`emit_event` helper, so the established log line is preserved.
"""

from __future__ import annotations

import pytest

from aspire_orchestrator.services.adam.telemetry import (
    clear_events,
    emit_playbook_outcome,
    emit_provider_call_timed,
    get_events,
)


@pytest.fixture(autouse=True)
def _isolate_event_buffer():
    """Telemetry's `_event_buffer` is module-level — clear before/after each
    test so cross-test contamination is impossible."""
    clear_events()
    yield
    clear_events()


# ---------------------------------------------------------------------------
# emit_provider_call_timed
# ---------------------------------------------------------------------------


def test_emit_provider_call_timed_writes_structured_fields() -> None:
    emit_provider_call_timed(
        tenant_hash="hash-abc",
        provider="attom",
        playbook="PROPERTY_FACTS_AND_PERMITS",
        duration_ms=1234.5,
        outcome="success",
        cold_start_detected=False,
    )
    events = get_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "adam_provider_call_timed"
    assert ev.provider == "attom"
    assert ev.playbook == "PROPERTY_FACTS_AND_PERMITS"
    assert ev.latency_ms == 1234.5
    assert ev.status == "success"
    # Spec'd structured-log keys — exact names matter for Loki/Promtail.
    assert ev.extra["adam.attom.duration_ms"] == 1234.5
    assert ev.extra["adam.attom.cold_start_detected"] is False
    assert ev.extra["adam.attom.outcome"] == "success"


def test_emit_provider_call_timed_uses_provider_in_field_prefix() -> None:
    """Apify-specific calls produce adam.apify_zillow.* fields."""
    emit_provider_call_timed(
        provider="apify_zillow",
        playbook="PROPERTY_FACTS_AND_PERMITS",
        duration_ms=2400.0,
        outcome="success",
        cold_start_detected=False,
    )
    ev = get_events()[0]
    assert "adam.apify_zillow.duration_ms" in ev.extra
    assert "adam.apify_zillow.cold_start_detected" in ev.extra
    assert "adam.apify_zillow.outcome" in ev.extra
    # No cross-provider leakage
    assert "adam.attom.duration_ms" not in ev.extra


def test_emit_provider_call_timed_auto_detects_cold_start_on_slow_success() -> None:
    """When cold_start_detected is not passed, slow (>5s) successful calls
    are flagged as cold-start automatically."""
    emit_provider_call_timed(
        provider="apify_zillow",
        playbook="PROPERTY_FACTS_AND_PERMITS",
        duration_ms=8000.0,  # 8s — well above the 5s threshold
        outcome="success",
    )
    ev = get_events()[0]
    assert ev.extra["adam.apify_zillow.cold_start_detected"] is True


def test_emit_provider_call_timed_does_not_flag_fast_calls_as_cold_start() -> None:
    emit_provider_call_timed(
        provider="apify_zillow",
        playbook="PROPERTY_FACTS_AND_PERMITS",
        duration_ms=1500.0,
        outcome="success",
    )
    ev = get_events()[0]
    assert ev.extra["adam.apify_zillow.cold_start_detected"] is False


def test_emit_provider_call_timed_does_not_flag_failures_as_cold_start() -> None:
    """A slow FAILED call must not be auto-classified as cold-start — failures
    are network/error conditions, not the cold-start signal we want to track."""
    emit_provider_call_timed(
        provider="apify_zillow",
        playbook="PROPERTY_FACTS_AND_PERMITS",
        duration_ms=15000.0,  # very slow
        outcome="failed",
        error_type="TimeoutError",
    )
    ev = get_events()[0]
    assert ev.extra["adam.apify_zillow.cold_start_detected"] is False
    assert ev.status == "failed"
    assert ev.error_type == "TimeoutError"


def test_emit_provider_call_timed_respects_explicit_cold_start_flag() -> None:
    """An explicit cold_start_detected=True overrides the auto-derive
    even for fast calls — useful when the playbook has out-of-band signal
    (e.g., first call of a session)."""
    emit_provider_call_timed(
        provider="apify_zillow",
        playbook="PROPERTY_FACTS_AND_PERMITS",
        duration_ms=1500.0,
        outcome="success",
        cold_start_detected=True,
    )
    ev = get_events()[0]
    assert ev.extra["adam.apify_zillow.cold_start_detected"] is True


# ---------------------------------------------------------------------------
# emit_playbook_outcome
# ---------------------------------------------------------------------------


def test_emit_playbook_outcome_writes_structured_fields() -> None:
    emit_playbook_outcome(
        tenant_hash="hash-xyz",
        playbook="PROPERTY_FACTS_AND_PERMITS",
        outcome="partial",
        total_latency_ms=7300.0,
        providers_called=["attom", "apify_zillow"],
        degraded_providers=["apify_zillow"],
    )
    events = get_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "adam_playbook_outcome"
    assert ev.playbook == "PROPERTY_FACTS_AND_PERMITS"
    assert ev.latency_ms == 7300.0
    assert ev.status == "partial"
    assert ev.extra["adam.playbook.outcome"] == "partial"
    assert ev.extra["adam.playbook.degraded_providers"] == ["apify_zillow"]
    assert ev.extra["adam.playbook.providers_called"] == ["attom", "apify_zillow"]


def test_emit_playbook_outcome_normalizes_none_degraded_to_empty_list() -> None:
    """Defensive: when no degradation, callers may pass None — emitter must
    coerce to [] so downstream consumers can iterate safely."""
    emit_playbook_outcome(
        playbook="PROPERTY_FACTS_AND_PERMITS",
        outcome="success",
        total_latency_ms=2100.0,
        providers_called=["attom", "apify_zillow"],
        degraded_providers=None,
    )
    ev = get_events()[0]
    assert ev.extra["adam.playbook.degraded_providers"] == []
    assert isinstance(ev.extra["adam.playbook.degraded_providers"], list)


def test_emit_playbook_outcome_copies_providers_list() -> None:
    """The emitter must not retain a reference to the caller's list — a
    subsequent append by the caller should not mutate the recorded event."""
    providers = ["attom"]
    emit_playbook_outcome(
        playbook="PROPERTY_FACTS_AND_PERMITS",
        outcome="success",
        total_latency_ms=1000.0,
        providers_called=providers,
        degraded_providers=[],
    )
    providers.append("apify_zillow")  # caller mutates after emit
    ev = get_events()[0]
    assert ev.extra["adam.playbook.providers_called"] == ["attom"]


def test_emit_playbook_outcome_supports_wrapper_timeout_status() -> None:
    """The reason_code 'PLAYBOOK_WRAPPER_TIMEOUT' (Phase B-1 server.py change)
    maps to outcome='wrapper_timeout' in this emitter when the playbook
    layer (B-2) catches the wrapper timeout."""
    emit_playbook_outcome(
        playbook="PROPERTY_FACTS_AND_PERMITS",
        outcome="wrapper_timeout",
        total_latency_ms=28000.0,
        providers_called=["attom", "apify_zillow"],
        degraded_providers=["attom", "apify_zillow"],
    )
    ev = get_events()[0]
    assert ev.status == "wrapper_timeout"
    assert ev.extra["adam.playbook.outcome"] == "wrapper_timeout"
