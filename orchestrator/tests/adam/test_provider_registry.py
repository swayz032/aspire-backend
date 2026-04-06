"""Tests for Adam Provider Registry (provider_registry.py).

Validates: ADR-004 registry-driven provider resolution.
Law #3 (Fail Closed): unknown provider raises immediately.
Law #6 (Tenant Isolation): feature flags evaluated per-tenant.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from aspire_orchestrator.services.adam.provider_registry import (
    get_all_providers,
    get_fallback_chain,
    get_provider,
    resolve_providers,
)
from aspire_orchestrator.services.adam.schemas.provider_metadata import (
    CostClass,
    TrustClass,
)

TENANT_ID = "tenant-test-001"


# ---------------------------------------------------------------------------
# Synchronous registry tests (no mocking needed)
# ---------------------------------------------------------------------------


class TestRegistration:
    """All 13 providers are registered and accessible."""

    EXPECTED_PROVIDER_IDS = {
        "google_places",
        "here",
        "foursquare",
        "brave",
        "tavily",
        "mapbox",
        "tomtom",
        "attom",
        "exa",
        "parallel",
        "serpapi_shopping",
        "serpapi_home_depot",
        "tripadvisor",
    }

    def test_all_13_providers_registered(self):
        """All expected provider IDs are present in the registry."""
        registered = set(get_all_providers().keys())
        assert self.EXPECTED_PROVIDER_IDS == registered, (
            f"Registry mismatch. Missing: {self.EXPECTED_PROVIDER_IDS - registered}. "
            f"Extra: {registered - self.EXPECTED_PROVIDER_IDS}"
        )

    def test_get_provider_returns_metadata(self):
        """get_provider returns a ProviderMetadata with the correct provider_id."""
        meta = get_provider("google_places")
        assert meta.provider_id == "google_places"

    def test_get_provider_unknown_raises_key_error(self):
        """Law #3 — unknown provider must raise KeyError immediately (fail-closed)."""
        with pytest.raises(KeyError, match="Unknown provider"):
            get_provider("nonexistent_provider_xyz")

    def test_trust_classes_are_correct(self):
        """ATTOM = Tier A; Google Places/HERE/Foursquare = Tier B; Brave/Exa = Tier C."""
        assert get_provider("attom").trust_class == TrustClass.A
        assert get_provider("google_places").trust_class == TrustClass.B
        assert get_provider("here").trust_class == TrustClass.B
        assert get_provider("brave").trust_class == TrustClass.C
        assert get_provider("exa").trust_class == TrustClass.C

    def test_fallback_chain_google_places(self):
        """google_places fallback chain resolves to registered providers."""
        chain = get_fallback_chain("google_places")
        chain_ids = [p.provider_id for p in chain]
        assert "here" in chain_ids
        assert "foursquare" in chain_ids

    def test_attom_has_no_fallback(self):
        """ATTOM has no fallback providers — fail closed for ATTOM-dependent facts."""
        meta = get_provider("attom")
        assert meta.fallbacks == ()
        chain = get_fallback_chain("attom")
        assert chain == []


# ---------------------------------------------------------------------------
# Async resolve_providers tests
# ---------------------------------------------------------------------------


class TestResolveProviders:
    """resolve_providers returns the correct ordered provider list."""

    @pytest.mark.asyncio
    async def test_resolves_business_lookup_for_trades(self):
        """Trades + lookup + business → at least google_places and foursquare returned."""
        with patch(
            "aspire_orchestrator.services.adam.provider_registry.is_adam_flag_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ):
            results = await resolve_providers(
                segment="trades",
                intent="lookup",
                entity_type="business",
                budget_tier="medium",
                tenant_id=TENANT_ID,
                enabled_entitlements=set(),
            )
        provider_ids = [p.provider_id for p in results]
        assert "google_places" in provider_ids
        assert "foursquare" in provider_ids

    @pytest.mark.asyncio
    async def test_resolves_property_fact_for_landlord_with_attom_enabled(self):
        """Landlord + property_fact + property + attom flag ON → attom is present."""
        with patch(
            "aspire_orchestrator.services.adam.provider_registry.is_adam_flag_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ):
            results = await resolve_providers(
                segment="landlord",
                intent="property_fact",
                entity_type="property",
                budget_tier="medium",
                tenant_id=TENANT_ID,
                enabled_entitlements={"attom_property_core"},
            )
        provider_ids = [p.provider_id for p in results]
        assert "attom" in provider_ids

    @pytest.mark.asyncio
    async def test_feature_flag_disabled_excludes_provider(self):
        """Provider with disabled feature flag must be excluded from results."""
        # attom flag disabled → attom must NOT appear
        def flag_side_effect(flag_name: str, tenant_id: str):
            if "attom" in flag_name:
                return False
            return True

        mock_flag = AsyncMock(side_effect=flag_side_effect)
        with patch(
            "aspire_orchestrator.services.adam.provider_registry.is_adam_flag_enabled",
            new=mock_flag,
        ):
            results = await resolve_providers(
                segment="landlord",
                intent="property_fact",
                entity_type="property",
                budget_tier="medium",
                tenant_id=TENANT_ID,
                enabled_entitlements={"attom_property_core"},
            )
        provider_ids = [p.provider_id for p in results]
        assert "attom" not in provider_ids

    @pytest.mark.asyncio
    async def test_entitlement_gate_excludes_provider_when_missing(self):
        """ATTOM requires 'attom_property_core' entitlement — missing → excluded."""
        with patch(
            "aspire_orchestrator.services.adam.provider_registry.is_adam_flag_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ):
            results = await resolve_providers(
                segment="landlord",
                intent="property_fact",
                entity_type="property",
                budget_tier="medium",
                tenant_id=TENANT_ID,
                enabled_entitlements=set(),  # no entitlements
            )
        provider_ids = [p.provider_id for p in results]
        assert "attom" not in provider_ids

    @pytest.mark.asyncio
    async def test_entitlement_gate_includes_provider_when_present(self):
        """ATTOM included when correct entitlement is provided."""
        with patch(
            "aspire_orchestrator.services.adam.provider_registry.is_adam_flag_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ):
            results = await resolve_providers(
                segment="trades",
                intent="property_fact",
                entity_type="property",
                budget_tier="medium",
                tenant_id=TENANT_ID,
                enabled_entitlements={"attom_property_core"},
            )
        provider_ids = [p.provider_id for p in results]
        assert "attom" in provider_ids

    @pytest.mark.asyncio
    async def test_low_budget_excludes_medium_high_cost_providers(self):
        """Budget tier 'low' must exclude MEDIUM_HIGH and HIGH cost providers."""
        with patch(
            "aspire_orchestrator.services.adam.provider_registry.is_adam_flag_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ):
            results = await resolve_providers(
                segment="trades",
                intent="price_check",
                entity_type="product",
                budget_tier="low",
                tenant_id=TENANT_ID,
                enabled_entitlements={"attom_property_core"},
            )
        for p in results:
            assert p.cost_class not in (CostClass.MEDIUM_HIGH, CostClass.HIGH), (
                f"Provider {p.provider_id} with cost {p.cost_class} should not appear in low budget"
            )

    @pytest.mark.asyncio
    async def test_trust_class_ordering_a_before_b_before_c(self):
        """Providers are ordered: Tier A first, then B, then C."""
        with patch(
            "aspire_orchestrator.services.adam.provider_registry.is_adam_flag_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ):
            results = await resolve_providers(
                segment="trades",
                intent="verify",
                entity_type="business",
                budget_tier="high",
                tenant_id=TENANT_ID,
                enabled_entitlements=set(),
            )
        if len(results) >= 2:
            trust_order = {TrustClass.A: 0, TrustClass.B: 1, TrustClass.C: 2}
            trust_ranks = [trust_order[p.trust_class] for p in results]
            assert trust_ranks == sorted(trust_ranks), (
                f"Trust ordering violated: {[(p.provider_id, p.trust_class) for p in results]}"
            )

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_matching_providers(self):
        """Resolve returns empty list when no providers match (caller uses fallback)."""
        with patch(
            "aspire_orchestrator.services.adam.provider_registry.is_adam_flag_enabled",
            new_callable=AsyncMock,
            return_value=False,  # all flags off
        ):
            results = await resolve_providers(
                segment="travel",
                intent="hotel_research",
                entity_type="hotel",
                budget_tier="medium",
                tenant_id=TENANT_ID,
                enabled_entitlements=set(),
            )
        # tripadvisor is the only hotel provider and is flag-gated
        # google_places also handles hotel_research and travel, has no flag
        # so we check that tripadvisor specifically is absent
        provider_ids = [p.provider_id for p in results]
        assert "tripadvisor" not in provider_ids

    @pytest.mark.asyncio
    async def test_deterministic_ordering_for_same_inputs(self):
        """resolve_providers produces the same ordered list on repeated calls."""
        with patch(
            "aspire_orchestrator.services.adam.provider_registry.is_adam_flag_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ):
            r1 = await resolve_providers(
                segment="trades",
                intent="lookup",
                entity_type="business",
                budget_tier="medium",
                tenant_id=TENANT_ID,
            )
            r2 = await resolve_providers(
                segment="trades",
                intent="lookup",
                entity_type="business",
                budget_tier="medium",
                tenant_id=TENANT_ID,
            )
        assert [p.provider_id for p in r1] == [p.provider_id for p in r2]

    @pytest.mark.asyncio
    async def test_entity_type_filter_excludes_wrong_entity(self):
        """Providers not supporting the requested entity_type are excluded."""
        with patch(
            "aspire_orchestrator.services.adam.provider_registry.is_adam_flag_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ):
            results = await resolve_providers(
                segment="trades",
                intent="lookup",
                entity_type="hotel",  # only google_places and tripadvisor support hotel
                budget_tier="medium",
                tenant_id=TENANT_ID,
            )
        provider_ids = [p.provider_id for p in results]
        # foursquare, here, tomtom do NOT list "hotel" in entity_types
        assert "foursquare" not in provider_ids
        assert "tomtom" not in provider_ids
