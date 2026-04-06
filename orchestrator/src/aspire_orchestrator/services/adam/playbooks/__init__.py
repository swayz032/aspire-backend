"""Segment-specific research playbooks for Adam.

Segments: trades, accounting_bookkeeping, landlord, travel
Each playbook defines provider plan + execution logic + output contract.
"""

from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.schemas.research_response import ResearchResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Playbook name → execute function dispatch table
# ---------------------------------------------------------------------------

_PLAYBOOK_DISPATCH: dict[str, Any] = {}


def _ensure_dispatch_loaded() -> None:
    """Lazy-load the dispatch table on first call to avoid circular imports."""
    if _PLAYBOOK_DISPATCH:
        return

    from aspire_orchestrator.services.adam.playbooks.trades import (
        execute_property_facts_and_permits,
        execute_estimate_research,
        execute_tool_material_price_check,
        execute_competitor_pricing_scan,
        execute_subcontractor_scout,
        execute_territory_opportunity_scan,
    )
    from aspire_orchestrator.services.adam.playbooks.accounting import (
        execute_prospect_research,
        execute_client_verification,
        execute_tax_and_compliance,
        execute_local_niche_scan,
        execute_industry_benchmark,
        execute_ar_collections_intel,
    )
    from aspire_orchestrator.services.adam.playbooks.landlord import (
        execute_property_facts,
        execute_rent_comp_context,
        execute_permit_and_renovation,
        execute_neighborhood_demand,
        execute_screening_compliance,
        execute_turnover_vendor_scout,
    )
    from aspire_orchestrator.services.adam.playbooks.travel import (
        execute_business_trip_hotel_research,
    )

    _PLAYBOOK_DISPATCH.update({
        # Trades (6)
        "PROPERTY_FACTS_AND_PERMITS": execute_property_facts_and_permits,
        "ESTIMATE_RESEARCH": execute_estimate_research,
        "TOOL_MATERIAL_PRICE_CHECK": execute_tool_material_price_check,
        "COMPETITOR_PRICING_SCAN": execute_competitor_pricing_scan,
        "SUBCONTRACTOR_SCOUT": execute_subcontractor_scout,
        "TERRITORY_OPPORTUNITY_SCAN": execute_territory_opportunity_scan,
        # Accounting (6)
        "PROSPECT_RESEARCH": execute_prospect_research,
        "CLIENT_VERIFICATION": execute_client_verification,
        "TAX_AND_COMPLIANCE_LOOKUP": execute_tax_and_compliance,
        "LOCAL_NICHE_SCAN": execute_local_niche_scan,
        "INDUSTRY_BENCHMARK_PACK": execute_industry_benchmark,
        "AR_COLLECTIONS_INTEL": execute_ar_collections_intel,
        # Landlord (6)
        "PROPERTY_FACTS": execute_property_facts,
        "RENT_COMP_CONTEXT": execute_rent_comp_context,
        "PERMIT_AND_RENOVATION_CONTEXT": execute_permit_and_renovation,
        "NEIGHBORHOOD_DEMAND_SCAN": execute_neighborhood_demand,
        "SCREENING_COMPLIANCE_LOOKUP": execute_screening_compliance,
        "TURNOVER_VENDOR_SCOUT": execute_turnover_vendor_scout,
        # Travel (1)
        "BUSINESS_TRIP_HOTEL_RESEARCH": execute_business_trip_hotel_research,
    })


async def dispatch_playbook(
    playbook_name: str,
    query: str,
    ctx: PlaybookContext,
    **kwargs: Any,
) -> ResearchResponse:
    """Dispatch to the correct playbook execute function by name.

    Returns a ResearchResponse on success, or a minimal error response on failure.
    Fails closed (Law #3): unknown playbook names return an error response.
    """
    _ensure_dispatch_loaded()

    execute_fn = _PLAYBOOK_DISPATCH.get(playbook_name)
    if execute_fn is None:
        logger.error("No dispatch entry for playbook: %s", playbook_name)
        return ResearchResponse(
            artifact_type="error",
            summary=f"Unknown playbook: {playbook_name}",
            playbook=playbook_name,
            confidence={"status": "unverified", "score": 0.0},
        )

    try:
        # Playbook functions use either `ctx` or `context` as the parameter name.
        # Inspect to determine which, then call with the correct kwarg.
        import inspect
        sig = inspect.signature(execute_fn)
        if "context" in sig.parameters:
            return await execute_fn(query=query, context=ctx, **kwargs)
        return await execute_fn(query=query, ctx=ctx, **kwargs)
    except Exception as exc:
        logger.error(
            "Playbook %s execution failed: %s", playbook_name, exc, exc_info=True,
        )
        return ResearchResponse(
            artifact_type="error",
            summary=f"Playbook execution failed: {exc}",
            playbook=playbook_name,
            confidence={"status": "unverified", "score": 0.0},
        )
