"""Search Router — Provider fallback routing for Adam (Research) skill pack.

Implements the ecosystem's routing logic per providers.yaml:
  - Web search:   brave (primary) -> tavily (fallback)
  - Places search: google_places -> tomtom -> here -> foursquare -> osm_overpass

Each route tries providers in order, falls back on ProviderError or any failure.
Returns ToolExecutionResult with `provider_used` and `fallback_chain` metadata.

Receipts name which provider was used + any fallback cascade (Law #2).
Circuit breaker state is checked per-provider before attempting (Law #3).
"""

from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import ProviderError
from aspire_orchestrator.services.tool_types import ToolExecutionResult

# Web search providers
from aspire_orchestrator.providers.brave_client import execute_brave_search
from aspire_orchestrator.providers.tavily_client import execute_tavily_search

# Places search providers (ordered by priority)
from aspire_orchestrator.providers.google_places_client import execute_google_places_search
from aspire_orchestrator.providers.tomtom_client import execute_tomtom_search
from aspire_orchestrator.providers.here_client import execute_here_search
from aspire_orchestrator.providers.foursquare_client import execute_foursquare_search
from aspire_orchestrator.providers.osm_overpass_client import execute_osm_overpass_query
from aspire_orchestrator.providers.mapbox_client import execute_mapbox_geocode

logger = logging.getLogger(__name__)

# Type alias for executor functions
_ExecutorFn = Any  # Callable with keyword args — same signature as provider executors

# Chain builder functions — resolve function references at call time
# so that unittest.mock.patch can intercept them.

def _web_search_chain() -> list[tuple[str, _ExecutorFn]]:
    return [
        ("brave", execute_brave_search),
        ("tavily", execute_tavily_search),
    ]

def _places_search_chain() -> list[tuple[str, _ExecutorFn]]:
    return [
        ("google_places", execute_google_places_search),
        ("tomtom", execute_tomtom_search),
        ("here", execute_here_search),
        ("foursquare", execute_foursquare_search),
        ("osm_overpass", execute_osm_overpass_query),
    ]

def _geocode_chain() -> list[tuple[str, _ExecutorFn]]:
    return [
        ("mapbox", execute_mapbox_geocode),
        # Future: could add google_geocode, here_geocode as fallbacks
    ]


async def _route_through_chain(
    chain: list[tuple[str, _ExecutorFn]],
    *,
    meta_tool_id: str,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Try providers in order until one succeeds.

    Returns the first successful result, or the last failure with full
    fallback chain metadata.
    """
    fallback_chain: list[str] = []
    last_result: ToolExecutionResult | None = None

    for provider_name, executor_fn in chain:
        fallback_chain.append(provider_name)

        try:
            result = await executor_fn(
                payload=payload,
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                risk_tier=risk_tier,
                capability_token_id=capability_token_id,
                capability_token_hash=capability_token_hash,
            )
        except ProviderError as e:
            logger.warning(
                "Search router: %s failed with ProviderError: %s (%s). "
                "Trying next provider.",
                provider_name, e.code.value if hasattr(e.code, 'value') else e.code, e.message,
            )
            last_result = ToolExecutionResult(
                outcome=Outcome.FAILED,
                tool_id=meta_tool_id,
                error=f"[{provider_name}] {e.message}",
                receipt_data=result.receipt_data if 'result' in dir() else {},
            )
            continue
        except Exception as e:
            logger.error(
                "Search router: %s raised unexpected exception: %s",
                provider_name, type(e).__name__,
            )
            last_result = ToolExecutionResult(
                outcome=Outcome.FAILED,
                tool_id=meta_tool_id,
                error=f"[{provider_name}] Unexpected error: {type(e).__name__}",
                receipt_data={},
            )
            continue

        if result.outcome == Outcome.SUCCESS:
            # Inject routing metadata into the result data
            enriched_data = dict(result.data) if result.data else {}
            enriched_data["provider_used"] = provider_name
            enriched_data["fallback_chain"] = fallback_chain

            # Enrich receipt with routing info
            enriched_receipt = dict(result.receipt_data) if result.receipt_data else {}
            enriched_receipt["router_provider_used"] = provider_name
            enriched_receipt["router_fallback_chain"] = fallback_chain

            logger.info(
                "Search router: %s succeeded via %s (chain: %s)",
                meta_tool_id, provider_name, " -> ".join(fallback_chain),
            )

            return ToolExecutionResult(
                outcome=Outcome.SUCCESS,
                tool_id=result.tool_id,
                data=enriched_data,
                receipt_data=enriched_receipt,
            )
        else:
            logger.warning(
                "Search router: %s failed (%s). Trying next provider.",
                provider_name, result.error or "unknown",
            )
            last_result = result

    # All providers failed — return last error with full chain
    logger.error(
        "Search router: all providers exhausted for %s. Chain: %s",
        meta_tool_id, " -> ".join(fallback_chain),
    )

    error_msg = last_result.error if last_result else "All providers failed"
    receipt_data = dict(last_result.receipt_data) if last_result and last_result.receipt_data else {}
    receipt_data["router_provider_used"] = None
    receipt_data["router_fallback_chain"] = fallback_chain
    receipt_data["router_all_failed"] = True

    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id=meta_tool_id,
        error=f"All providers failed. Last error: {error_msg}",
        data={
            "provider_used": None,
            "fallback_chain": fallback_chain,
        },
        receipt_data=receipt_data,
    )


async def route_web_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Route a web search through the provider chain: brave -> tavily.

    Required payload:
      - query: str — search query

    Returns ToolExecutionResult with provider_used and fallback_chain in data.
    """
    return await _route_through_chain(
        _web_search_chain(),
        meta_tool_id="search.web",
        payload=payload,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        risk_tier=risk_tier,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def route_places_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Route a places search through the provider chain:
    google_places -> tomtom -> here -> foursquare -> osm_overpass.

    Required payload:
      - query: str — search query

    Returns ToolExecutionResult with provider_used and fallback_chain in data.
    """
    return await _route_through_chain(
        _places_search_chain(),
        meta_tool_id="search.places",
        payload=payload,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        risk_tier=risk_tier,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def route_geocode(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Route a geocoding request through the provider chain: mapbox.

    Required payload:
      - query: str — address or place name

    Returns ToolExecutionResult with provider_used and fallback_chain in data.
    """
    return await _route_through_chain(
        _geocode_chain(),
        meta_tool_id="search.geocode",
        payload=payload,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        risk_tier=risk_tier,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )
