"""Adam Research Telemetry — Observability + cost tracking + receipt enrichment.

Event families (from handoff telemetry spec):
  adam_request_received, adam_playbook_selected, adam_provider_called,
  adam_provider_failed, adam_cache_hit, adam_cache_miss,
  adam_conflict_detected, adam_response_completed

Cost tracking:
  - Exa: actual cost from costDollars in response
  - SerpApi: $0 for cache hits, ~$0.01 for real calls
  - ATTOM: per-endpoint cost estimation
  - All others: estimated per-call costs

Receipt enrichment: segment, intent, playbook, providers_called,
  actual_cost, confidence_status, missing_fields_count, artifact_type
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost estimation per provider (USD per call)
# ---------------------------------------------------------------------------

PROVIDER_COST_ESTIMATES: dict[str, float] = {
    "brave": 0.005,
    "tavily": 0.01,
    "exa": 0.02,          # Overridden by actual costDollars when available
    "parallel": 0.02,
    "google_places": 0.025,
    "here": 0.005,
    "foursquare": 0.01,
    "tomtom": 0.005,
    "mapbox": 0.005,
    "attom": 0.10,         # Varies by endpoint
    "serpapi_shopping": 0.01,
    "serpapi_home_depot": 0.01,
    "tripadvisor": 0.02,
}

# Higher-cost ATTOM endpoints
ATTOM_ENDPOINT_COSTS: dict[str, float] = {
    "property_detail": 0.05,
    "property_snapshot": 0.03,
    "property_detail_with_schools": 0.08,
    "sales_history": 0.05,
    "sales_expanded_history": 0.08,
    "sales_comparables": 0.10,
    "sales_trends": 0.05,
    "valuation_avm": 0.10,
    "rental_avm": 0.10,
    "school_search": 0.05,
    "boundary_lookup": 0.05,
}


# ---------------------------------------------------------------------------
# Telemetry event types
# ---------------------------------------------------------------------------

@dataclass
class TelemetryEvent:
    """A single telemetry event for Adam research."""

    event_type: str
    timestamp: float = field(default_factory=time.time)
    tenant_hash: str = ""
    segment: str = ""
    playbook: str = ""
    intent: str = ""
    provider: str = ""
    cost_class: str = ""
    latency_ms: float = 0.0
    status: str = ""
    verification_status: str = ""
    source_count: int = 0
    conflict_count: int = 0
    cost_estimate: float = 0.0
    cache_hit: bool = False
    error_type: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Event buffer (in production, flush to Supabase/observability)
# ---------------------------------------------------------------------------

_event_buffer: list[TelemetryEvent] = []


def emit_event(event: TelemetryEvent) -> None:
    """Emit a telemetry event. In production, flushes to Supabase/Grafana."""
    _event_buffer.append(event)
    logger.info(
        "ADAM_TELEMETRY [%s] provider=%s playbook=%s cost=$%.4f latency=%.0fms status=%s",
        event.event_type, event.provider, event.playbook,
        event.cost_estimate, event.latency_ms, event.status,
    )


def get_events() -> list[TelemetryEvent]:
    """Get all buffered events (for testing/debugging)."""
    return list(_event_buffer)


def clear_events() -> None:
    """Clear event buffer (for testing)."""
    _event_buffer.clear()


# ---------------------------------------------------------------------------
# Convenience emitters
# ---------------------------------------------------------------------------

def emit_request_received(
    *, tenant_hash: str, segment: str, intent: str, playbook: str,
) -> None:
    emit_event(TelemetryEvent(
        event_type="adam_request_received",
        tenant_hash=tenant_hash,
        segment=segment,
        intent=intent,
        playbook=playbook,
    ))


def emit_playbook_selected(
    *, tenant_hash: str, playbook: str, provider_plan: list[str],
) -> None:
    emit_event(TelemetryEvent(
        event_type="adam_playbook_selected",
        tenant_hash=tenant_hash,
        playbook=playbook,
        extra={"provider_plan": provider_plan},
    ))


def emit_provider_called(
    *, tenant_hash: str, provider: str, playbook: str,
    latency_ms: float, cache_hit: bool, cost_estimate: float,
) -> None:
    emit_event(TelemetryEvent(
        event_type="adam_provider_called",
        tenant_hash=tenant_hash,
        provider=provider,
        playbook=playbook,
        latency_ms=latency_ms,
        cache_hit=cache_hit,
        cost_estimate=0.0 if cache_hit else cost_estimate,
        status="success",
    ))


def emit_provider_failed(
    *, tenant_hash: str, provider: str, playbook: str,
    error_type: str, latency_ms: float,
) -> None:
    emit_event(TelemetryEvent(
        event_type="adam_provider_failed",
        tenant_hash=tenant_hash,
        provider=provider,
        playbook=playbook,
        error_type=error_type,
        latency_ms=latency_ms,
        status="failed",
    ))


def emit_cache_hit(*, tenant_hash: str, provider: str, playbook: str) -> None:
    emit_event(TelemetryEvent(
        event_type="adam_cache_hit",
        tenant_hash=tenant_hash,
        provider=provider,
        playbook=playbook,
        cache_hit=True,
    ))


def emit_cache_miss(*, tenant_hash: str, provider: str, playbook: str) -> None:
    emit_event(TelemetryEvent(
        event_type="adam_cache_miss",
        tenant_hash=tenant_hash,
        provider=provider,
        playbook=playbook,
        cache_hit=False,
    ))


def emit_conflict_detected(
    *, tenant_hash: str, playbook: str, field_name: str,
    providers: list[str], values: list[Any],
) -> None:
    emit_event(TelemetryEvent(
        event_type="adam_conflict_detected",
        tenant_hash=tenant_hash,
        playbook=playbook,
        conflict_count=1,
        extra={"field": field_name, "providers": providers, "values": [str(v) for v in values]},
    ))


def emit_response_completed(
    *, tenant_hash: str, segment: str, playbook: str, intent: str,
    verification_status: str, source_count: int, conflict_count: int,
    missing_fields_count: int, total_cost: float, total_latency_ms: float,
    artifact_type: str,
) -> None:
    emit_event(TelemetryEvent(
        event_type="adam_response_completed",
        tenant_hash=tenant_hash,
        segment=segment,
        playbook=playbook,
        intent=intent,
        verification_status=verification_status,
        source_count=source_count,
        conflict_count=conflict_count,
        cost_estimate=total_cost,
        latency_ms=total_latency_ms,
        status="completed",
        extra={"missing_fields_count": missing_fields_count, "artifact_type": artifact_type},
    ))


# ---------------------------------------------------------------------------
# Phase B-1 emitters — structured timing + outcome (consumed by Phase B-2).
# ---------------------------------------------------------------------------
#
# These two emitters are the contract the playbook refactor (B-2) will use to
# surface per-provider timing and overall playbook outcome to Grafana / Sentry.
# They reuse the existing TelemetryEvent dataclass — `extra` carries the new
# structured fields. The log line emitted by emit_event already prints
# provider/playbook/cost/latency/status, so a downstream log-based metric
# pipeline (Loki/Promtail or Sentry breadcrumbs) can scrape these directly.

# Threshold (seconds) above which a single provider call is classified as a
# probable cold-start. Tunable: Apify warm p95 is ~2-4s; >5s strongly suggests
# the actor container was cold.
_COLD_START_THRESHOLD_SECONDS = 5.0


def emit_provider_call_timed(
    *,
    tenant_hash: str = "",
    provider: str,
    playbook: str,
    duration_ms: float,
    outcome: str,
    cold_start_detected: bool | None = None,
    error_type: str = "",
) -> None:
    """Phase B-1 — emit a provider call with explicit duration + cold-start flag.

    Field names match the Phase B-1 spec exactly:
      - adam.<provider>.duration_ms
      - adam.<provider>.cold_start_detected
      - adam.<provider>.outcome

    Args:
      provider: provider id (e.g., "attom", "apify_zillow"). The field-name
        prefix `adam.<provider>.*` is derived from this value.
      playbook: playbook name (e.g., "PROPERTY_FACTS_AND_PERMITS").
      duration_ms: wall-clock time for the single call, in milliseconds.
      outcome: one of "success", "unavailable", "failed", "circuit_open",
        "timeout". The playbook (Phase B-2) maps provider results to this.
      cold_start_detected: bool. If None, auto-derived from
        `duration_ms > _COLD_START_THRESHOLD_SECONDS * 1000` AND outcome=="success"
        (a failed call is never classified as cold-start).
      error_type: optional exception class name when outcome != "success".

    Law #9: no PII in any field — `tenant_hash` is a one-way hash, never the
    raw suite/office ID.
    """
    if cold_start_detected is None:
        cold_start_detected = (
            outcome == "success" and duration_ms > (_COLD_START_THRESHOLD_SECONDS * 1000)
        )
    emit_event(TelemetryEvent(
        event_type="adam_provider_call_timed",
        tenant_hash=tenant_hash,
        provider=provider,
        playbook=playbook,
        latency_ms=duration_ms,
        status=outcome,
        error_type=error_type,
        extra={
            # Mirror the spec'd structured-log field names so they appear
            # verbatim in the log line under `extra` and any log-shipper can
            # turn them into Prometheus/Loki metrics with no transform layer.
            f"adam.{provider}.duration_ms": duration_ms,
            f"adam.{provider}.cold_start_detected": cold_start_detected,
            f"adam.{provider}.outcome": outcome,
        },
    ))


def emit_playbook_outcome(
    *,
    tenant_hash: str = "",
    playbook: str,
    outcome: str,
    total_latency_ms: float,
    providers_called: list[str],
    degraded_providers: list[str] | None = None,
) -> None:
    """Phase B-1 — emit terminal playbook outcome with degraded-provider list.

    Field names match the Phase B-1 spec:
      - adam.playbook.outcome
      - adam.playbook.degraded_providers

    Args:
      playbook: playbook name.
      outcome: "success" | "partial" | "failed" | "wrapper_timeout".
      total_latency_ms: end-to-end wall-clock for the playbook.
      providers_called: every provider the playbook tried (success OR fail).
      degraded_providers: providers that did NOT contribute (timeout, breaker
        open, malformed response). Empty/None when outcome="success".
    """
    degraded = list(degraded_providers or [])
    emit_event(TelemetryEvent(
        event_type="adam_playbook_outcome",
        tenant_hash=tenant_hash,
        playbook=playbook,
        latency_ms=total_latency_ms,
        status=outcome,
        extra={
            "adam.playbook.outcome": outcome,
            "adam.playbook.degraded_providers": degraded,
            "adam.playbook.providers_called": list(providers_called),
        },
    ))


# ---------------------------------------------------------------------------
# Cost calculation helpers
# ---------------------------------------------------------------------------

def estimate_provider_cost(provider: str, endpoint: str = "") -> float:
    """Estimate cost for a single provider call."""
    if provider == "attom" and endpoint:
        return ATTOM_ENDPOINT_COSTS.get(endpoint, 0.10)
    return PROVIDER_COST_ESTIMATES.get(provider, 0.01)


def calculate_total_cost(
    provider_calls: list[dict[str, Any]],
) -> float:
    """Calculate total cost from a list of provider call records.

    Each record: {provider, endpoint, cache_hit, actual_cost}
    Exa provides actual cost via costDollars — use that when available.
    SerpApi cache hits are free.
    """
    total = 0.0
    for call in provider_calls:
        if call.get("cache_hit"):
            continue  # Free
        actual = call.get("actual_cost")
        if actual is not None:
            total += actual  # Exa provides actual cost
        else:
            total += estimate_provider_cost(
                call.get("provider", ""),
                call.get("endpoint", ""),
            )
    return round(total, 4)


# ---------------------------------------------------------------------------
# Error taxonomy (user-visible)
# ---------------------------------------------------------------------------

class AdamErrorCode:
    """Adam-specific error codes for user-facing responses."""

    MISSING_REQUIRED_INPUT = "MISSING_REQUIRED_INPUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    NO_VERIFIED_RESULTS = "NO_VERIFIED_RESULTS"
    AMBIGUOUS_PRODUCT = "AMBIGUOUS_PRODUCT"
    ADDRESS_NOT_NORMALIZED = "ADDRESS_NOT_NORMALIZED"
    ENTITLEMENT_MISSING = "ENTITLEMENT_MISSING"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
