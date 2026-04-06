"""Provider metadata schema for Adam's registry-driven routing.

Each provider in the mesh is described by a ProviderMetadata record that
captures its capabilities, trust profile, cost class, and gating rules.
The registry uses these to resolve ordered provider lists per playbook.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TrustClass(str, Enum):
    """Source trust tier for confidence calculation (ADR-003)."""

    A = "A"  # Authoritative: ATTOM, official govt sources
    B = "B"  # Strong commercial: Google Places, HERE, Foursquare, SerpApi, Tripadvisor
    C = "C"  # Web extraction: Brave, Exa, Tavily, Parallel


class FreshnessClass(str, Enum):
    """How current the provider's data is."""

    LIVE = "live"                          # Real-time API (Google Places, HERE)
    STRUCTURED_CURRENT = "structured_current"  # Structured but periodic (ATTOM)
    DYNAMIC = "dynamic"                    # Dynamic content API (Tripadvisor)
    WEB_FRESH = "web_fresh"                # Web-derived, recent (Exa, Tavily)
    CONTROLLED = "controlled"              # Controlled freshness (Parallel)
    SEARCH_DERIVED = "search_derived"      # Search-result derived (SerpApi)


class CostClass(str, Enum):
    """Estimated cost per API call (ADR-004)."""

    LOW = "low"
    LOW_MEDIUM = "low_medium"
    MEDIUM = "medium"
    MEDIUM_HIGH = "medium_high"
    HIGH = "high"


class RateLimitClass(str, Enum):
    """Rate limit profile."""

    STANDARD = "standard"    # Normal rate limits
    THROTTLED = "throttled"  # Aggressive rate limits, needs careful budgeting
    BUDGET_CAPPED = "budget_capped"  # Hard monthly cap (SerpApi)


@dataclass(frozen=True)
class ProviderMetadata:
    """Capability metadata for a single provider in Adam's mesh.

    Used by the registry to resolve which providers serve a given
    (segment, intent, entity_type, budget_tier) combination.
    """

    provider_id: str
    entity_types: tuple[str, ...]       # business, property, product, hotel, web
    intents: tuple[str, ...]            # lookup, compare, verify, price_check, property_fact, etc.
    segments: tuple[str, ...]           # trades, accounting_bookkeeping, landlord, travel, general_smb
    trust_class: TrustClass
    freshness_class: FreshnessClass
    cost_class: CostClass
    rate_limit_class: RateLimitClass
    supports_cache: bool
    fallbacks: tuple[str, ...] = ()     # provider_ids to try if this one fails
    feature_flag: str | None = None     # None = always enabled (existing providers)
    required_secrets: tuple[str, ...] = ()
    entitlement_gate: str | None = None  # e.g. "attom_property_core"
    description: str = ""

    # Provider-specific capabilities (used by playbooks)
    capabilities: dict[str, bool] = field(default_factory=dict)
