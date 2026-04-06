"""Tests for Adam telemetry — event emission, cost estimation, error taxonomy."""

from __future__ import annotations

import pytest

from aspire_orchestrator.services.adam.telemetry import (
    AdamErrorCode,
    ATTOM_ENDPOINT_COSTS,
    PROVIDER_COST_ESTIMATES,
    TelemetryEvent,
    calculate_total_cost,
    clear_events,
    emit_cache_hit,
    emit_cache_miss,
    emit_conflict_detected,
    emit_event,
    emit_playbook_selected,
    emit_provider_called,
    emit_provider_failed,
    emit_request_received,
    emit_response_completed,
    estimate_provider_cost,
    get_events,
)


class TestTelemetryEventEmission:
    """Verify all 8 event types emit correctly."""

    def setup_method(self):
        clear_events()

    def test_emit_request_received(self):
        emit_request_received(
            tenant_hash="abc123", segment="trades",
            intent="price_check", playbook="TOOL_MATERIAL_PRICE_CHECK",
        )
        events = get_events()
        assert len(events) == 1
        assert events[0].event_type == "adam_request_received"
        assert events[0].segment == "trades"
        assert events[0].playbook == "TOOL_MATERIAL_PRICE_CHECK"

    def test_emit_playbook_selected(self):
        emit_playbook_selected(
            tenant_hash="abc123", playbook="PROPERTY_FACTS",
            provider_plan=["here", "attom"],
        )
        events = get_events()
        assert len(events) == 1
        assert events[0].event_type == "adam_playbook_selected"
        assert events[0].extra["provider_plan"] == ["here", "attom"]

    def test_emit_provider_called_success(self):
        emit_provider_called(
            tenant_hash="abc123", provider="attom", playbook="PROPERTY_FACTS",
            latency_ms=450.0, cache_hit=False, cost_estimate=0.05,
        )
        events = get_events()
        assert len(events) == 1
        assert events[0].event_type == "adam_provider_called"
        assert events[0].provider == "attom"
        assert events[0].latency_ms == 450.0
        assert events[0].cost_estimate == 0.05
        assert events[0].cache_hit is False

    def test_emit_provider_called_cache_hit_zero_cost(self):
        emit_provider_called(
            tenant_hash="abc123", provider="serpapi_shopping", playbook="PRICE_CHECK",
            latency_ms=5.0, cache_hit=True, cost_estimate=0.01,
        )
        events = get_events()
        assert events[0].cost_estimate == 0.0  # Cache hits are free

    def test_emit_provider_failed(self):
        emit_provider_failed(
            tenant_hash="abc123", provider="attom", playbook="PROPERTY_FACTS",
            error_type="ENTITLEMENT_MISSING", latency_ms=200.0,
        )
        events = get_events()
        assert events[0].event_type == "adam_provider_failed"
        assert events[0].error_type == "ENTITLEMENT_MISSING"
        assert events[0].status == "failed"

    def test_emit_cache_hit(self):
        emit_cache_hit(tenant_hash="abc", provider="brave", playbook="COMPETITOR_SCAN")
        events = get_events()
        assert events[0].event_type == "adam_cache_hit"
        assert events[0].cache_hit is True

    def test_emit_cache_miss(self):
        emit_cache_miss(tenant_hash="abc", provider="brave", playbook="COMPETITOR_SCAN")
        events = get_events()
        assert events[0].event_type == "adam_cache_miss"
        assert events[0].cache_hit is False

    def test_emit_conflict_detected(self):
        emit_conflict_detected(
            tenant_hash="abc", playbook="PROPERTY_FACTS",
            field_name="living_sqft", providers=["attom", "brave"],
            values=[1800, 2200],
        )
        events = get_events()
        assert events[0].event_type == "adam_conflict_detected"
        assert events[0].extra["field"] == "living_sqft"
        assert events[0].conflict_count == 1

    def test_emit_response_completed(self):
        emit_response_completed(
            tenant_hash="abc", segment="landlord", playbook="PROPERTY_FACTS",
            intent="property_fact", verification_status="verified",
            source_count=3, conflict_count=0, missing_fields_count=1,
            total_cost=0.25, total_latency_ms=3200.0,
            artifact_type="LandlordPropertyPack",
        )
        events = get_events()
        assert events[0].event_type == "adam_response_completed"
        assert events[0].verification_status == "verified"
        assert events[0].cost_estimate == 0.25
        assert events[0].extra["artifact_type"] == "LandlordPropertyPack"

    def test_clear_events(self):
        emit_cache_hit(tenant_hash="x", provider="y", playbook="z")
        assert len(get_events()) == 1
        clear_events()
        assert len(get_events()) == 0

    def test_multiple_events_buffered(self):
        emit_request_received(tenant_hash="a", segment="trades", intent="lookup", playbook="P")
        emit_provider_called(tenant_hash="a", provider="brave", playbook="P",
                           latency_ms=100, cache_hit=False, cost_estimate=0.005)
        emit_response_completed(tenant_hash="a", segment="trades", playbook="P",
                              intent="lookup", verification_status="verified",
                              source_count=1, conflict_count=0, missing_fields_count=0,
                              total_cost=0.005, total_latency_ms=100, artifact_type="VendorShortlist")
        assert len(get_events()) == 3


class TestCostEstimation:
    """Verify cost estimation and calculation."""

    def test_estimate_brave_cost(self):
        assert estimate_provider_cost("brave") == 0.005

    def test_estimate_attom_default_cost(self):
        assert estimate_provider_cost("attom") == 0.10

    def test_estimate_attom_endpoint_cost(self):
        assert estimate_provider_cost("attom", "property_detail") == 0.05
        assert estimate_provider_cost("attom", "valuation_avm") == 0.10
        assert estimate_provider_cost("attom", "sales_comparables") == 0.10

    def test_estimate_unknown_provider_cost(self):
        assert estimate_provider_cost("unknown_provider") == 0.01

    def test_all_providers_have_cost_estimate(self):
        expected_providers = [
            "brave", "tavily", "exa", "parallel", "google_places",
            "here", "foursquare", "tomtom", "mapbox", "attom",
            "serpapi_shopping", "serpapi_home_depot", "tripadvisor",
        ]
        for provider in expected_providers:
            cost = estimate_provider_cost(provider)
            assert cost > 0, f"Provider {provider} has no cost estimate"

    def test_calculate_total_cost_simple(self):
        calls = [
            {"provider": "brave", "cache_hit": False},
            {"provider": "google_places", "cache_hit": False},
        ]
        total = calculate_total_cost(calls)
        assert total == round(0.005 + 0.025, 4)

    def test_calculate_total_cost_cache_hits_free(self):
        calls = [
            {"provider": "serpapi_shopping", "cache_hit": True},
            {"provider": "serpapi_home_depot", "cache_hit": False},
        ]
        total = calculate_total_cost(calls)
        assert total == 0.01  # Only the non-cached call counts

    def test_calculate_total_cost_exa_actual(self):
        calls = [
            {"provider": "exa", "cache_hit": False, "actual_cost": 0.007},
        ]
        total = calculate_total_cost(calls)
        assert total == 0.007  # Uses actual cost from Exa, not estimate

    def test_calculate_total_cost_empty(self):
        assert calculate_total_cost([]) == 0.0

    def test_attom_endpoint_costs_complete(self):
        expected_endpoints = [
            "property_detail", "property_snapshot", "property_detail_with_schools",
            "sales_history", "sales_expanded_history", "sales_comparables",
            "sales_trends", "valuation_avm", "rental_avm",
            "school_search", "boundary_lookup",
        ]
        for endpoint in expected_endpoints:
            assert endpoint in ATTOM_ENDPOINT_COSTS, f"Missing ATTOM endpoint cost: {endpoint}"


class TestErrorTaxonomy:
    """Verify all error codes exist."""

    def test_all_error_codes_defined(self):
        expected = [
            "MISSING_REQUIRED_INPUT", "PROVIDER_UNAVAILABLE", "LOW_CONFIDENCE",
            "NO_VERIFIED_RESULTS", "AMBIGUOUS_PRODUCT", "ADDRESS_NOT_NORMALIZED",
            "ENTITLEMENT_MISSING", "RATE_LIMITED", "TIMEOUT", "BUDGET_EXHAUSTED",
        ]
        for code in expected:
            assert hasattr(AdamErrorCode, code), f"Missing error code: {code}"

    def test_error_codes_are_strings(self):
        assert AdamErrorCode.MISSING_REQUIRED_INPUT == "MISSING_REQUIRED_INPUT"
        assert AdamErrorCode.BUDGET_EXHAUSTED == "BUDGET_EXHAUSTED"


class TestTelemetryEventDataclass:
    """Verify TelemetryEvent fields."""

    def test_default_values(self):
        event = TelemetryEvent(event_type="test")
        assert event.event_type == "test"
        assert event.tenant_hash == ""
        assert event.latency_ms == 0.0
        assert event.cost_estimate == 0.0
        assert event.cache_hit is False
        assert event.extra == {}

    def test_timestamp_auto_set(self):
        event = TelemetryEvent(event_type="test")
        assert event.timestamp > 0
