"""Tests for Adam Research Cache (cache.py).

Validates: cache get/set, expiry, tenant isolation (Law #6),
SerpApi budget enforcement, and bulk clear operations.
"""

from __future__ import annotations

import time
import pytest
from unittest.mock import patch

from aspire_orchestrator.services.adam.cache import (
    SERPAPI_MONTHLY_LIMIT,
    cache_clear_all,
    cache_clear_tenant,
    cache_get,
    cache_set,
    serpapi_check_budget,
    serpapi_get_count,
    serpapi_increment,
    _cache,
    _serpapi_counter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear all cache state before and after each test."""
    cache_clear_all()
    yield
    cache_clear_all()


TENANT_A = "tenant-alpha-0001"
TENANT_B = "tenant-beta-0002"


# ---------------------------------------------------------------------------
# Basic get/set
# ---------------------------------------------------------------------------


class TestCacheGetSet:
    """cache_set stores values; cache_get retrieves them."""

    def test_set_and_get_returns_stored_value(self):
        cache_set(
            tenant_id=TENANT_A,
            provider="google_places",
            playbook="SUBCONTRACTOR_SCOUT",
            query="roofers near 40509",
            value={"results": ["Acme Roofing"]},
        )
        result = cache_get(
            tenant_id=TENANT_A,
            provider="google_places",
            playbook="SUBCONTRACTOR_SCOUT",
            query="roofers near 40509",
        )
        assert result == {"results": ["Acme Roofing"]}

    def test_get_on_missing_key_returns_none(self):
        result = cache_get(
            tenant_id=TENANT_A,
            provider="brave",
            playbook="TAX_AND_COMPLIANCE_LOOKUP",
            query="quarterly tax deadlines",
        )
        assert result is None

    def test_set_overwrites_existing_value(self):
        cache_set(
            tenant_id=TENANT_A,
            provider="exa",
            playbook="TAX_AND_COMPLIANCE_LOOKUP",
            query="irs deadline",
            value={"v": 1},
        )
        cache_set(
            tenant_id=TENANT_A,
            provider="exa",
            playbook="TAX_AND_COMPLIANCE_LOOKUP",
            query="irs deadline",
            value={"v": 2},
        )
        result = cache_get(
            tenant_id=TENANT_A,
            provider="exa",
            playbook="TAX_AND_COMPLIANCE_LOOKUP",
            query="irs deadline",
        )
        assert result == {"v": 2}

    def test_params_are_part_of_cache_key(self):
        """Different params → different cache entries."""
        cache_set(
            tenant_id=TENANT_A,
            provider="serpapi_shopping",
            playbook="TOOL_MATERIAL_PRICE_CHECK",
            query="condenser",
            params={"brand": "Carrier"},
            value={"price": 1200},
        )
        result_no_params = cache_get(
            tenant_id=TENANT_A,
            provider="serpapi_shopping",
            playbook="TOOL_MATERIAL_PRICE_CHECK",
            query="condenser",
            params=None,
        )
        assert result_no_params is None

    def test_ttl_override_applied(self):
        """Cache entry with ttl_override=1 expires after 1 second."""
        cache_set(
            tenant_id=TENANT_A,
            provider="brave",
            playbook="SUBCONTRACTOR_SCOUT",
            query="roofer",
            value={"x": 1},
            ttl_override=1,
        )
        assert cache_get(
            tenant_id=TENANT_A, provider="brave",
            playbook="SUBCONTRACTOR_SCOUT", query="roofer"
        ) == {"x": 1}

        # Advance time past expiry
        with patch("aspire_orchestrator.services.adam.cache.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 10
            result = cache_get(
                tenant_id=TENANT_A, provider="brave",
                playbook="SUBCONTRACTOR_SCOUT", query="roofer"
            )
        assert result is None


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


class TestCacheExpiry:
    """Expired entries are not returned (TTL enforcement)."""

    def test_expired_entry_returns_none(self):
        """Entry with TTL=1 second must return None after 2 seconds (mocked time)."""
        cache_set(
            tenant_id=TENANT_A,
            provider="serpapi_shopping",
            playbook="TOOL_MATERIAL_PRICE_CHECK",
            query="condenser price",
            value={"price": 999.99},
            ttl_override=1,
        )
        future_time = time.monotonic() + 10  # 10 seconds past expiry
        with patch("aspire_orchestrator.services.adam.cache.time") as mock_time:
            mock_time.monotonic.return_value = future_time
            result = cache_get(
                tenant_id=TENANT_A,
                provider="serpapi_shopping",
                playbook="TOOL_MATERIAL_PRICE_CHECK",
                query="condenser price",
            )
        assert result is None

    def test_non_expired_entry_returned(self):
        """Entry within TTL must still be returned."""
        cache_set(
            tenant_id=TENANT_A,
            provider="attom",
            playbook="PROPERTY_FACTS_AND_PERMITS",
            query="123 Main St",
            value={"sqft": 1800},
            ttl_override=86400,  # 24 hours
        )
        result = cache_get(
            tenant_id=TENANT_A,
            provider="attom",
            playbook="PROPERTY_FACTS_AND_PERMITS",
            query="123 Main St",
        )
        assert result == {"sqft": 1800}


# ---------------------------------------------------------------------------
# Tenant isolation (Law #6)
# ---------------------------------------------------------------------------


class TestCacheTenantIsolation:
    """Cache entries are isolated per tenant — cross-tenant reads are impossible."""

    def test_tenant_a_cache_not_visible_to_tenant_b(self):
        """Cache set by tenant A must NOT be returned when queried by tenant B."""
        cache_set(
            tenant_id=TENANT_A,
            provider="google_places",
            playbook="SUBCONTRACTOR_SCOUT",
            query="roofers near downtown",
            value={"data": "tenant_a_private"},
        )
        result = cache_get(
            tenant_id=TENANT_B,
            provider="google_places",
            playbook="SUBCONTRACTOR_SCOUT",
            query="roofers near downtown",
        )
        assert result is None, (
            "BLOCKER: Tenant B read Tenant A cache entry — cross-tenant isolation violated (Law #6)"
        )

    def test_tenant_b_cache_not_visible_to_tenant_a(self):
        """Cache set by tenant B must NOT be returned when queried by tenant A."""
        cache_set(
            tenant_id=TENANT_B,
            provider="exa",
            playbook="TAX_AND_COMPLIANCE_LOOKUP",
            query="quarterly tax deadline",
            value={"data": "tenant_b_private"},
        )
        result = cache_get(
            tenant_id=TENANT_A,
            provider="exa",
            playbook="TAX_AND_COMPLIANCE_LOOKUP",
            query="quarterly tax deadline",
        )
        assert result is None, (
            "BLOCKER: Tenant A read Tenant B cache entry — cross-tenant isolation violated (Law #6)"
        )

    def test_each_tenant_has_independent_cache_entries(self):
        """Both tenants can store the same query with different values independently."""
        cache_set(
            tenant_id=TENANT_A, provider="brave",
            playbook="PROSPECT_RESEARCH", query="construction companies",
            value={"data": "a_result"},
        )
        cache_set(
            tenant_id=TENANT_B, provider="brave",
            playbook="PROSPECT_RESEARCH", query="construction companies",
            value={"data": "b_result"},
        )
        a_result = cache_get(
            tenant_id=TENANT_A, provider="brave",
            playbook="PROSPECT_RESEARCH", query="construction companies"
        )
        b_result = cache_get(
            tenant_id=TENANT_B, provider="brave",
            playbook="PROSPECT_RESEARCH", query="construction companies"
        )
        assert a_result == {"data": "a_result"}
        assert b_result == {"data": "b_result"}

    def test_cache_clear_tenant_clears_only_that_tenant(self):
        """cache_clear_tenant removes only the targeted tenant's entries."""
        cache_set(
            tenant_id=TENANT_A, provider="brave",
            playbook="PROSPECT_RESEARCH", query="construction companies",
            value={"data": "a_data"},
        )
        cache_set(
            tenant_id=TENANT_B, provider="brave",
            playbook="PROSPECT_RESEARCH", query="construction companies",
            value={"data": "b_data"},
        )
        count = cache_clear_tenant(TENANT_A)
        assert count >= 1

        # Tenant A's cache is gone
        assert cache_get(
            tenant_id=TENANT_A, provider="brave",
            playbook="PROSPECT_RESEARCH", query="construction companies"
        ) is None

        # Tenant B's cache survives
        assert cache_get(
            tenant_id=TENANT_B, provider="brave",
            playbook="PROSPECT_RESEARCH", query="construction companies"
        ) == {"data": "b_data"}


# ---------------------------------------------------------------------------
# Bulk clear
# ---------------------------------------------------------------------------


class TestCacheClearAll:
    """cache_clear_all removes all entries including the SerpApi counter."""

    def test_cache_clear_all_removes_all_entries(self):
        cache_set(
            tenant_id=TENANT_A, provider="brave",
            playbook="P1", query="q1", value={"v": 1},
        )
        cache_set(
            tenant_id=TENANT_B, provider="exa",
            playbook="P2", query="q2", value={"v": 2},
        )
        serpapi_increment()
        cache_clear_all()
        assert cache_get(
            tenant_id=TENANT_A, provider="brave", playbook="P1", query="q1"
        ) is None
        assert cache_get(
            tenant_id=TENANT_B, provider="exa", playbook="P2", query="q2"
        ) is None
        assert serpapi_get_count() == 0


# ---------------------------------------------------------------------------
# SerpApi budget enforcement
# ---------------------------------------------------------------------------


class TestSerpApiBudget:
    """SerpApi budget: hard stop at 240 calls/month."""

    def test_check_budget_returns_true_under_limit(self):
        """serpapi_check_budget returns True when count < 240."""
        assert serpapi_check_budget() is True

    def test_check_budget_returns_false_at_limit(self):
        """serpapi_check_budget returns False when count == 240."""
        month_key = __import__(
            "aspire_orchestrator.services.adam.cache", fromlist=["_current_month"]
        )._current_month()
        _serpapi_counter[month_key] = SERPAPI_MONTHLY_LIMIT
        assert serpapi_check_budget() is False

    def test_check_budget_returns_false_above_limit(self):
        """serpapi_check_budget returns False when count > 240."""
        month_key = __import__(
            "aspire_orchestrator.services.adam.cache", fromlist=["_current_month"]
        )._current_month()
        _serpapi_counter[month_key] = SERPAPI_MONTHLY_LIMIT + 5
        assert serpapi_check_budget() is False

    def test_serpapi_increment_tracks_count(self):
        """serpapi_increment increases count by 1 per call."""
        assert serpapi_get_count() == 0
        serpapi_increment()
        assert serpapi_get_count() == 1
        serpapi_increment()
        assert serpapi_get_count() == 2

    def test_serpapi_increment_returns_new_count(self):
        """serpapi_increment return value equals the updated count."""
        new_count = serpapi_increment()
        assert new_count == 1
        new_count = serpapi_increment()
        assert new_count == 2

    def test_serpapi_get_count_zero_at_start(self):
        """SerpApi counter starts at 0 after cache_clear_all."""
        assert serpapi_get_count() == 0

    def test_cache_clear_all_resets_serpapi_counter(self):
        """cache_clear_all resets SerpApi counter to 0."""
        serpapi_increment()
        serpapi_increment()
        assert serpapi_get_count() == 2
        cache_clear_all()
        assert serpapi_get_count() == 0
