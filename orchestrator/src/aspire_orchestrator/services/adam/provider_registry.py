"""Adam Provider Registry — Registry-driven provider resolution (ADR-004).

Replaces hardcoded fallback chains with metadata-driven routing.
Each provider registers capabilities; the router consumes registry
metadata to build ordered provider plans per playbook.

Fail-closed: unknown providers raise immediately (Law #3).
Feature-flag gated: disabled providers are excluded from resolution.
Entitlement gated: providers without required entitlements are excluded.
"""

from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.services.adam.feature_flags import (
    ADAM_PROVIDER_ATTOM_V1,
    ADAM_PROVIDER_PARALLEL_V1,
    ADAM_PROVIDER_SERPAPI_HOME_DEPOT_V1,
    ADAM_PROVIDER_SERPAPI_SHOPPING_V1,
    ADAM_PROVIDER_TRIPADVISOR_V1,
    is_adam_flag_enabled,
)
from aspire_orchestrator.services.adam.schemas.provider_metadata import (
    CostClass,
    FreshnessClass,
    ProviderMetadata,
    RateLimitClass,
    TrustClass,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider definitions (13 providers)
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, ProviderMetadata] = {}


def _register(p: ProviderMetadata) -> None:
    _PROVIDERS[p.provider_id] = p


# --- Existing providers (always enabled, no feature flag) ---

_register(ProviderMetadata(
    provider_id="google_places",
    entity_types=("business", "hotel"),
    intents=("lookup", "compare", "verify", "hotel_research"),
    segments=("trades", "accounting_bookkeeping", "landlord", "travel", "general_smb"),
    trust_class=TrustClass.B,
    freshness_class=FreshnessClass.LIVE,
    cost_class=CostClass.MEDIUM,
    rate_limit_class=RateLimitClass.STANDARD,
    supports_cache=True,
    fallbacks=("here", "foursquare", "brave"),
    required_secrets=("ASPIRE_GOOGLE_PLACES_API_KEY",),
    description="Google Places API (New) — primary local business/hotel search",
    capabilities={"text_search": True, "nearby_search": True, "place_details": True},
))

_register(ProviderMetadata(
    provider_id="here",
    entity_types=("business", "geo"),
    intents=("lookup", "geocode", "verify"),
    segments=("trades", "accounting_bookkeeping", "landlord", "travel", "general_smb"),
    trust_class=TrustClass.B,
    freshness_class=FreshnessClass.LIVE,
    cost_class=CostClass.LOW_MEDIUM,
    rate_limit_class=RateLimitClass.STANDARD,
    supports_cache=True,
    fallbacks=("google_places", "foursquare"),
    required_secrets=("ASPIRE_HERE_API_KEY",),
    description="HERE Geocoding & Search — address normalization, POI discovery, geocoding",
    capabilities={"geocode": True, "reverse_geocode": True, "poi_search": True},
))

_register(ProviderMetadata(
    provider_id="foursquare",
    entity_types=("business",),
    intents=("lookup", "compare", "verify"),
    segments=("trades", "accounting_bookkeeping", "landlord", "general_smb"),
    trust_class=TrustClass.B,
    freshness_class=FreshnessClass.LIVE,
    cost_class=CostClass.MEDIUM,
    rate_limit_class=RateLimitClass.STANDARD,
    supports_cache=True,
    fallbacks=("google_places", "here"),
    required_secrets=("ASPIRE_FOURSQUARE_API_KEY",),
    description="Foursquare Places — chain-aware discovery, category taxonomy, dedup",
    capabilities={"place_search": True, "chain_detection": True, "category_taxonomy": True},
))

_register(ProviderMetadata(
    provider_id="brave",
    entity_types=("web",),
    intents=("lookup", "compare", "verify", "compliance_lookup", "monitor"),
    segments=("trades", "accounting_bookkeeping", "landlord", "travel", "general_smb"),
    trust_class=TrustClass.C,
    freshness_class=FreshnessClass.WEB_FRESH,
    cost_class=CostClass.LOW_MEDIUM,
    rate_limit_class=RateLimitClass.STANDARD,
    supports_cache=True,
    fallbacks=("exa", "tavily"),
    required_secrets=("ASPIRE_BRAVE_API_KEY",),
    description="Brave Search — default web search rail, broad fresh results",
    capabilities={"web_search": True, "news_search": True, "local_enrichment": True},
))

_register(ProviderMetadata(
    provider_id="tavily",
    entity_types=("web",),
    intents=("lookup", "compare", "verify"),
    segments=("trades", "accounting_bookkeeping", "landlord", "travel", "general_smb"),
    trust_class=TrustClass.C,
    freshness_class=FreshnessClass.WEB_FRESH,
    cost_class=CostClass.MEDIUM,
    rate_limit_class=RateLimitClass.STANDARD,
    supports_cache=True,
    fallbacks=("brave", "exa"),
    required_secrets=("ASPIRE_TAVILY_API_KEY",),
    description="Tavily — agent-optimized search, URL extraction, site crawling",
    capabilities={"web_search": True, "url_extract": True, "site_crawl": True},
))

_register(ProviderMetadata(
    provider_id="mapbox",
    entity_types=("geo",),
    intents=("geocode",),
    segments=("trades", "accounting_bookkeeping", "landlord", "travel", "general_smb"),
    trust_class=TrustClass.B,
    freshness_class=FreshnessClass.LIVE,
    cost_class=CostClass.LOW,
    rate_limit_class=RateLimitClass.STANDARD,
    supports_cache=True,
    required_secrets=("ASPIRE_MAPBOX_API_KEY",),
    description="Mapbox — geocoding only",
    capabilities={"geocode": True, "reverse_geocode": True},
))

_register(ProviderMetadata(
    provider_id="tomtom",
    entity_types=("business",),
    intents=("lookup", "compare"),
    segments=("trades", "accounting_bookkeeping", "landlord", "general_smb"),
    trust_class=TrustClass.B,
    freshness_class=FreshnessClass.LIVE,
    cost_class=CostClass.LOW_MEDIUM,
    rate_limit_class=RateLimitClass.STANDARD,
    supports_cache=True,
    fallbacks=("here", "foursquare"),
    required_secrets=("ASPIRE_TOMTOM_API_KEY",),
    description="TomTom — secondary places search",
    capabilities={"place_search": True},
))

# --- New providers (feature-flagged) ---

_register(ProviderMetadata(
    provider_id="attom",
    entity_types=("property",),
    intents=("property_fact", "compare", "verify"),
    segments=("trades", "landlord", "accounting_bookkeeping"),
    trust_class=TrustClass.A,
    freshness_class=FreshnessClass.STRUCTURED_CURRENT,
    cost_class=CostClass.MEDIUM_HIGH,
    rate_limit_class=RateLimitClass.STANDARD,
    supports_cache=True,
    fallbacks=(),  # No fallback — fail closed for ATTOM-dependent facts
    feature_flag=ADAM_PROVIDER_ATTOM_V1,
    required_secrets=("ASPIRE_ATTOM_API_KEY",),
    entitlement_gate="attom_property_core",
    description="ATTOM — U.S. property intelligence subsystem (ADR-002)",
    capabilities={
        "property_detail": True, "property_snapshot": True,
        "property_detail_with_schools": True, "sales_history": True,
        "sales_expanded_history": True, "sales_comparables": True,
        "sales_trends": True, "valuation_avm": True, "rental_avm": True,
        "school_context": True, "boundary_lookup": True,
    },
))

_register(ProviderMetadata(
    provider_id="exa",
    entity_types=("web",),
    intents=("compare", "verify", "compliance_lookup", "prospect_research"),
    segments=("trades", "accounting_bookkeeping", "landlord", "travel", "general_smb"),
    trust_class=TrustClass.C,  # Web extraction tier, but with grounding
    freshness_class=FreshnessClass.WEB_FRESH,
    cost_class=CostClass.MEDIUM_HIGH,
    rate_limit_class=RateLimitClass.STANDARD,
    supports_cache=True,
    fallbacks=("brave", "tavily"),
    required_secrets=("ASPIRE_EXA_API_KEY",),
    description="Exa — semantic deep research with structured output, grounding, confidence",
    capabilities={
        "search_auto": True, "search_instant": True,
        "search_deep_lite": True, "search_deep": True,
        "search_deep_reasoning": True,
        "output_schema": True, "grounding": True,
        "categories": True, "summaries": True,
        "subpage_crawl": True, "domain_filtering": True,
        "date_filtering": True, "content_moderation": True,
        "cost_breakdown": True, "live_crawl": True,
    },
))

_register(ProviderMetadata(
    provider_id="parallel",
    entity_types=("web",),
    intents=("compare", "verify", "compliance_lookup"),
    segments=("trades", "accounting_bookkeeping", "landlord", "general_smb"),
    trust_class=TrustClass.C,
    freshness_class=FreshnessClass.CONTROLLED,
    cost_class=CostClass.MEDIUM_HIGH,
    rate_limit_class=RateLimitClass.STANDARD,
    supports_cache=True,
    fallbacks=("exa", "brave"),
    feature_flag=ADAM_PROVIDER_PARALLEL_V1,
    required_secrets=("ASPIRE_PARALLEL_API_KEY",),
    description="Parallel — objective-driven web retrieval, FindAll, Task API for deep research",
    capabilities={
        "search_fast": True, "search_one_shot": True,
        "findall": True, "task_api": True,
        "extract": True, "source_policy": True,
        "excerpt_control": True, "fetch_policy": True,
    },
))

_register(ProviderMetadata(
    provider_id="serpapi_shopping",
    entity_types=("product",),
    intents=("price_check", "compare"),
    segments=("trades", "general_smb"),
    trust_class=TrustClass.B,
    freshness_class=FreshnessClass.SEARCH_DERIVED,
    cost_class=CostClass.MEDIUM_HIGH,
    rate_limit_class=RateLimitClass.BUDGET_CAPPED,
    supports_cache=True,
    fallbacks=("serpapi_home_depot", "brave"),
    feature_flag=ADAM_PROVIDER_SERPAPI_SHOPPING_V1,
    required_secrets=("ASPIRE_SERPAPI_API_KEY",),
    description="SerpApi Google Shopping — cross-retailer product pricing with filters",
    capabilities={
        "price_search": True, "price_filtering": True,
        "sort_by_price": True, "on_sale_filter": True,
        "free_shipping_filter": True, "small_business_filter": True,
        "immersive_product": True, "free_cached_searches": True,
    },
))

_register(ProviderMetadata(
    provider_id="serpapi_home_depot",
    entity_types=("product",),
    intents=("price_check", "compare"),
    segments=("trades",),
    trust_class=TrustClass.B,
    freshness_class=FreshnessClass.SEARCH_DERIVED,
    cost_class=CostClass.MEDIUM_HIGH,
    rate_limit_class=RateLimitClass.BUDGET_CAPPED,
    supports_cache=True,
    fallbacks=("serpapi_shopping", "brave"),
    feature_flag=ADAM_PROVIDER_SERPAPI_HOME_DEPOT_V1,
    required_secrets=("ASPIRE_SERPAPI_API_KEY",),
    description="SerpApi Home Depot — trade materials with store-level stock + pricing",
    capabilities={
        "product_search": True, "store_inventory": True,
        "price_sorting": True, "brand_filtering": True,
        "delivery_zip": True, "sale_detection": True,
        "canada_support": True, "free_cached_searches": True,
    },
))

_register(ProviderMetadata(
    provider_id="tripadvisor",
    entity_types=("hotel",),
    intents=("hotel_research", "compare", "verify"),
    segments=("travel",),
    trust_class=TrustClass.B,
    freshness_class=FreshnessClass.DYNAMIC,
    cost_class=CostClass.MEDIUM,
    rate_limit_class=RateLimitClass.STANDARD,
    supports_cache=True,
    fallbacks=("google_places", "here"),
    feature_flag=ADAM_PROVIDER_TRIPADVISOR_V1,
    required_secrets=("ASPIRE_TRIPADVISOR_API_KEY",),
    description="Tripadvisor — hotel sentiment, rankings, reviews, traveler data",
    capabilities={"hotel_search": True, "reviews": True, "photos": True, "sentiment": True},
))


# ---------------------------------------------------------------------------
# Registry API
# ---------------------------------------------------------------------------

def get_provider(provider_id: str) -> ProviderMetadata:
    """Get a provider by ID. Raises KeyError if unknown (fail-fast, Law #3)."""
    if provider_id not in _PROVIDERS:
        raise KeyError(f"Unknown provider: {provider_id}. Registered: {sorted(_PROVIDERS.keys())}")
    return _PROVIDERS[provider_id]


def get_all_providers() -> dict[str, ProviderMetadata]:
    """Return all registered providers (for inspection/testing)."""
    return dict(_PROVIDERS)


async def resolve_providers(
    *,
    segment: str,
    intent: str,
    entity_type: str,
    budget_tier: str = "medium",
    tenant_id: str,
    enabled_entitlements: set[str] | None = None,
) -> list[ProviderMetadata]:
    """Resolve an ordered list of providers for a given research request.

    Filtering:
      1. Entity type must match
      2. Intent must match
      3. Segment must match
      4. Feature flag must be enabled (if set) — checked per-tenant
      5. Entitlement gate must be satisfied (if set)
      6. Budget tier filters high-cost providers on low budgets

    Ordering:
      - Trust class A first, then B, then C
      - Within same trust class: lower cost first
      - Budget tier adjusts: 'low' excludes medium_high and high cost providers

    Returns empty list if no providers match (caller should fall back to
    existing Adam 4-mode behavior).
    """
    candidates: list[ProviderMetadata] = []
    entitlements = enabled_entitlements or set()

    for p in _PROVIDERS.values():
        # 1. Entity type match
        if entity_type not in p.entity_types:
            continue

        # 2. Intent match
        if intent not in p.intents:
            continue

        # 3. Segment match
        if segment not in p.segments:
            continue

        # 4. Feature flag check (skip disabled providers)
        if p.feature_flag is not None:
            flag_enabled = await is_adam_flag_enabled(p.feature_flag, tenant_id)
            if not flag_enabled:
                logger.debug(
                    "Provider %s excluded: feature flag %s disabled for tenant %s",
                    p.provider_id, p.feature_flag, tenant_id,
                )
                continue

        # 5. Entitlement gate check
        if p.entitlement_gate is not None and p.entitlement_gate not in entitlements:
            logger.debug(
                "Provider %s excluded: entitlement %s not available",
                p.provider_id, p.entitlement_gate,
            )
            continue

        # 6. Budget tier filtering
        if budget_tier == "low" and p.cost_class in (CostClass.MEDIUM_HIGH, CostClass.HIGH):
            logger.debug(
                "Provider %s excluded: cost class %s too high for budget tier %s",
                p.provider_id, p.cost_class.value, budget_tier,
            )
            continue

        candidates.append(p)

    # Sort: trust class (A > B > C), then cost (low > high)
    trust_order = {TrustClass.A: 0, TrustClass.B: 1, TrustClass.C: 2}
    cost_order = {
        CostClass.LOW: 0, CostClass.LOW_MEDIUM: 1,
        CostClass.MEDIUM: 2, CostClass.MEDIUM_HIGH: 3,
        CostClass.HIGH: 4,
    }
    candidates.sort(key=lambda p: (trust_order[p.trust_class], cost_order[p.cost_class]))

    logger.info(
        "Provider registry resolved %d providers for segment=%s intent=%s entity=%s budget=%s: %s",
        len(candidates), segment, intent, entity_type, budget_tier,
        [p.provider_id for p in candidates],
    )

    return candidates


def get_fallback_chain(provider_id: str) -> list[ProviderMetadata]:
    """Get the fallback chain for a provider (for degraded path handling)."""
    provider = get_provider(provider_id)
    chain: list[ProviderMetadata] = []
    for fallback_id in provider.fallbacks:
        try:
            chain.append(get_provider(fallback_id))
        except KeyError:
            logger.warning("Fallback provider %s not found in registry", fallback_id)
    return chain
