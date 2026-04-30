"""TRADES Playbooks — 6 research playbooks for trades ICP.

Segments: plumbers, HVAC, electricians, roofers, painters, GCs, landscapers
Playbooks: Property Facts & Permits, Estimate Research, Tool/Material Price Check,
           Competitor Pricing Scan, Subcontractor Scout, Territory Opportunity Scan
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.schemas.research_response import ResearchResponse
from aspire_orchestrator.services.adam.verifier import verify_records

logger = logging.getLogger(__name__)


def _extract_address_from_query(query: str) -> str:
    """Extract address from a natural language query for ATTOM."""
    import re
    # Strict pattern (state abbreviation or full state name)
    match = re.search(
        r'(\d+\s+[\w\s]+(?:St|Ave|Rd|Blvd|Dr|Ln|Ct|Way|Pl|Cir|Pkwy|Hwy|Ter)\.?'
        r'(?:\s*,\s*[\w\s]+,?\s*(?:[A-Z]{2}|[A-Za-z]{4,})\s*,?\s*\d{5}(?:-\d{4})?))',
        query, re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    prefixes = [
        "pull property facts for", "pull property details for", "pull property profile for",
        "find property facts for", "find property details for", "find property profile for",
        "property facts for", "property details for", "property profile for",
        "pull the square footage and permit context for",
        "pull", "get", "show me", "find", "look up",
    ]
    remaining = query.strip()
    while remaining:
        q_lower = remaining.lower().strip()
        consumed = False
        for prefix in sorted(prefixes, key=len, reverse=True):
            if q_lower.startswith(prefix):
                remaining = remaining[len(prefix):].strip(" .,:;-")
                consumed = True
                break
        if not consumed:
            break
    marker = "additional details:"
    rem_lower = remaining.lower()
    if marker in rem_lower:
        idx = rem_lower.rfind(marker)
        tail = remaining[idx + len(marker):].strip(" .,:;-")
        if tail:
            return tail
    if remaining and remaining != query:
        return remaining
    lower_query = query.lower()
    if marker in lower_query:
        idx = lower_query.rfind(marker)
        tail = query[idx + len(marker):].strip(" .,:;-")
        if tail:
            return tail
    # Loose fallback for wrapped inputs like:
    # "property lookup. Additional details: 4863 Price Street, Forest Park, Georgia, 30297"
    loose = re.search(
        r'(\d+\s+[\w\s]+(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Ct|Court|Way|Pl|Place|Cir|Circle|Pkwy|Parkway|Hwy|Highway|Ter|Terrace)\b[^,\n]*'
        r'(?:,\s*[\w\s]+){0,2}\s*,?\s*(?:[A-Z]{2}|[A-Za-z]{4,})\s*,?\s*\d{5}(?:-\d{4})?)',
        query,
        re.IGNORECASE,
    )
    if loose:
        return loose.group(1).strip()
    return query


async def execute_property_facts_and_permits(
    query: str, ctx: PlaybookContext, address: str = "",
) -> ResearchResponse:
    """PROPERTY_FACTS_AND_PERMITS — Resolve property context for quoting."""
    from aspire_orchestrator.providers.attom_client import (
        execute_attom_detail_mortgage_owner,
        execute_attom_sales_history,
    )
    from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
        normalize_from_attom_detail,
        normalize_from_attom_sales_history,
    )

    logger.info("Executing PROPERTY_FACTS_AND_PERMITS for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # Extract address from query
    attom_address = address or _extract_address_from_query(query)

    # 1. ATTOM property detail (with mortgage+owner for full context)
    detail_result = await execute_attom_detail_mortgage_owner(
        payload={"address": attom_address},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
        capability_token_id=ctx.capability_token_id,
        capability_token_hash=ctx.capability_token_hash,
    )
    providers_called.append("attom")

    if detail_result.outcome.value == "success" and detail_result.data:
        prop = normalize_from_attom_detail(detail_result.data)
        records.append(prop.to_dict())
        sources.extend(prop.sources)

    # 2. ATTOM sales history
    history_result = await execute_attom_sales_history(
        payload={"address": attom_address},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
        capability_token_id=ctx.capability_token_id,
        capability_token_hash=ctx.capability_token_hash,
    )

    if history_result.outcome.value == "success" and history_result.data:
        sales = normalize_from_attom_sales_history(history_result.data)
        if sales and records:
            records[0]["sale_history"] = [
                {"date": s.date, "amount": s.amount, "trans_type": s.trans_type,
                 "buyer": s.buyer, "seller": s.seller}
                for s in sales
            ]

    # Verify
    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["normalized_address", "living_sqft", "year_built"],
    )

    return ResearchResponse(
        artifact_type="PropertyFactPack",
        summary=f"Property facts for {address or query}",
        records=records,
        sources=sources,
        freshness={"provider": "attom"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Add rental valuation", "Pull nearby sales comparables"],
        verification_report=report,
        segment="trades",
        intent="property_fact",
        playbook="PROPERTY_FACTS_AND_PERMITS",
        providers_called=providers_called,
    )


async def execute_estimate_research(
    query: str, ctx: PlaybookContext, address: str = "",
) -> ResearchResponse:
    """ESTIMATE_RESEARCH — Support quoting with property facts + material pricing."""
    from aspire_orchestrator.providers.attom_client import execute_attom_property_detail
    from aspire_orchestrator.providers.serpapi_homedepot_client import execute_serpapi_homedepot_search
    from aspire_orchestrator.services.adam.normalizers.property_normalizer import normalize_from_attom_detail
    from aspire_orchestrator.services.adam.normalizers.product_normalizer import normalize_from_serpapi_homedepot

    logger.info("Executing ESTIMATE_RESEARCH for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. ATTOM for property context
    if address:
        detail_result = await execute_attom_property_detail(
            payload={"address": address},
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
        )
        providers_called.append("attom")
        if detail_result.outcome.value == "success" and detail_result.data:
            prop = normalize_from_attom_detail(detail_result.data)
            records.append(prop.to_dict())
            sources.extend(prop.sources)

    # 2. SerpApi Home Depot for material pricing
    hd_result = await execute_serpapi_homedepot_search(
        payload={"query": query, "hd_sort": "price_low_to_high"},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("serpapi_home_depot")

    if hd_result.outcome.value == "success" and hd_result.data:
        for item in hd_result.data.get("results", [])[:8]:
            product = normalize_from_serpapi_homedepot(item)
            records.append(product.to_dict())
            sources.extend(product.sources)

    report = verify_records(records=records, sources=sources, required_fields=["normalized_address", "living_sqft"])

    return ResearchResponse(
        artifact_type="EstimateResearchPack",
        summary=f"Estimate research for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Compare with Google Shopping prices", "Find subcontractors for this job"],
        verification_report=report,
        segment="trades",
        intent="price_check",
        playbook="ESTIMATE_RESEARCH",
        providers_called=providers_called,
    )


def _fuzzy_pick_store_from_query(
    query: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Pick a store from a candidate list when the query mentions a street/area.

    Examples that resolve silently:
      "sheetrock at the Capital Circle one" -> store with "Capital Cir" in address
      "the Tenleytown Home Depot" -> store with "Tenleytown" in address

    Uses a simple substring match on normalized address tokens. If exactly one
    candidate's address shares a meaningful token (3+ chars, alpha) with the
    query, that's the pick. Multiple matches or none -> None (caller falls back).
    """
    import re as _re

    def _normalize_tokens(text: str) -> set[str]:
        return {t for t in _re.findall(r"[A-Za-z]{3,}", text.lower())}

    # Drop common stopwords that would create false positives.
    _STOPWORDS = {
        "home", "depot", "store", "the", "and", "near", "for", "from",
        "drive", "road", "street", "avenue", "boulevard", "lane", "way",
        "place", "court", "circle", "highway", "parkway", "terrace",
        "north", "south", "east", "west",
    }

    query_tokens = _normalize_tokens(query) - _STOPWORDS
    if not query_tokens:
        return None

    matches: list[dict[str, Any]] = []
    for store in candidates:
        addr_tokens = _normalize_tokens(store.get("address", "")) - _STOPWORDS
        if query_tokens & addr_tokens:
            matches.append(store)

    if len(matches) == 1:
        return dict(matches[0])
    return None


def _build_store_disambiguation_response(
    query: str,
    candidates: list[dict[str, Any]],
    providers_called: list[str],
) -> ResearchResponse:
    """Return a StoreDisambiguation artifact so Ava + desktop can prompt for choice."""
    candidate_records: list[dict[str, Any]] = []
    for store in candidates:
        candidate_records.append({
            "card_kind": "store_candidate",
            "store_id": store.get("store_id", ""),
            "name": store.get("name", ""),
            "address": store.get("address", ""),
            "city": store.get("city", ""),
            "state": store.get("state", ""),
            "postal_code": store.get("postal_code", ""),
        })
    return ResearchResponse(
        artifact_type="StoreDisambiguation",
        summary=(
            f"Multiple Home Depot stores in {candidates[0].get('city', '')}. "
            "Which one would you like?"
        ),
        records=[],
        sources=[],
        freshness={"mode": "live"},
        confidence={"status": "verified", "score": 1.0},
        missing_fields=[],
        next_queries=[],
        segment="trades",
        intent="price_check",
        playbook="TOOL_MATERIAL_PRICE_CHECK",
        providers_called=providers_called,
        extra={"candidates": candidate_records, "query": query},
    )


async def execute_tool_material_price_check(
    query: str,
    ctx: PlaybookContext,
    zip_code: str = "",
    store_id: str = "",
    on_sale: bool = False,
    voice_path: bool | None = None,
    city: str = "",
    state: str = "",
    user_address: str = "",
) -> ResearchResponse:
    """TOOL_MATERIAL_PRICE_CHECK - Find current pricing, stock, and store info for tools/materials.

    Strict policy for product cards:
      1. Run search with resolved location/store context.
      2. Retry with tightened query up to 3 attempts (text path) or single attempt (voice).
      3. Fail closed (no partial cards) if required product/store-summary fields are incomplete.

    Voice path budget: 5s end-to-end. When voice_path is True we run one attempt with
    a 4s SerpApi timeout and skip the Google Shopping cross-check entirely. When None,
    voice is auto-detected as "no zip + no store_id + no city" (Ava's typical voice query).

    Round 4 — user_address PRIMARY path (Task #43):
      - When `user_address` is provided (e.g. trades worker on a job site), we
        Geocode + Places searchNearby to pin the closest Home Depot to THAT
        address — not the office. Sets `delivery_zip` from the resolved store's
        postal_code and skips the city -> zip / multi-store disambiguation
        flow entirely. On any failure (timeout, no HD within 50km, API error)
        we fall through to the existing Wave A.5 path.

    Multi-store disambiguation (Wave A.5 / Task #32):
      - When `city` is set and the directory has multiple HD stores in (city, state):
        1. Try fuzzy-match the query against each store's address (e.g. "the one on
           Capital Circle" -> Capital Cir NE). If hit, silent auto-pick.
        2. Fall back to haversine via ctx.office_lat/office_lng. Pick closest within 50km.
        3. Otherwise return artifact_type="StoreDisambiguation" with candidate list.
      - When `store_id` is set explicitly: skip city -> zip; use that store directly.
    """
    import re as _re
    from aspire_orchestrator.providers.serpapi_shopping_client import execute_serpapi_shopping_search
    from aspire_orchestrator.providers.serpapi_homedepot_client import execute_serpapi_homedepot_search
    from aspire_orchestrator.services.adam.normalizers.product_normalizer import (
        normalize_from_serpapi_shopping,
        normalize_from_serpapi_homedepot,
    )
    from aspire_orchestrator.services.adam.hd_store_directory import (
        lookup_store_by_id,
        lookup_zip_by_city,
        find_stores_in_city,
        find_nearest_store,
    )
    from aspire_orchestrator.services.adam.hd_store_resolver import resolve_store_async
    from aspire_orchestrator.services.adam.places_nearest_finder import (
        find_nearest_home_depot_by_address,
        NearestStore,
    )

    logger.info(
        "Executing TOOL_MATERIAL_PRICE_CHECK for: %s (city=%r state=%r store_id=%r zip=%r user_address=%r)",
        query[:80], city, state, store_id, zip_code, (user_address or "")[:60],
    )

    # Round 4 — PRIMARY path: nearest HD by user_address. When this resolves
    # successfully we pin delivery_zip from the resolved store's postal_code
    # and skip the city -> zip lookup entirely. On any failure we fall through
    # to Wave A.5 (city -> zip + multi-store disambiguation).
    #
    # The resolved NearestStore carries Google's formattedAddress + photo + a
    # haversine distance. Those override the static-directory fields in the
    # final store_summary because the user is at a job site — the Google
    # address is what they recognize, and distance_miles is hero data.
    nearest_store: NearestStore | None = None
    if user_address and user_address.strip():
        nearest_store = await find_nearest_home_depot_by_address(
            user_address.strip(),
            # Outer caller guard. Helper enforces an internal asyncio.wait_for
            # at the same value — keeping the boundary single-owned simplifies
            # cancellation semantics. Voice path budget is 5s end-to-end;
            # 3s here leaves 2s for SerpApi.
            timeout=3.0,
        )
        if nearest_store is not None:
            # Pin zip BEFORE the city/store_id branches below run.
            zip_code = nearest_store.postal_code or zip_code
            # Note: place_id is Google's, not a Home Depot store_id — we do
            # NOT set store_id from place_id (SerpApi rejects unknown ids).
            # The static directory still gets a chance to resolve store_id
            # from pickup.store_id in the SerpApi response below.

    if not zip_code:
        zip_match = _re.search(r"\b(\d{5})\b", query)
        if zip_match:
            zip_code = zip_match.group(1)

    location_hint = ""
    if city:
        location_hint = f"{city}, {state}".strip(", ") if state else city
    else:
        city_match = _re.search(r"\bin\s+([A-Za-z][A-Za-z\s]+(?:,\s*[A-Za-z]{2})?)\b", query)
        if city_match:
            location_hint = city_match.group(1).strip(" .,")

    # Wave A.5: explicit store_id beats city/zip — use the directory record directly.
    if store_id:
        directory_record = lookup_store_by_id(store_id)
        if directory_record:
            zip_code = zip_code or directory_record.get("postal_code", "")
    elif city:
        # Wave A.5: multi-store disambiguation in a city.
        candidates = find_stores_in_city(city, state or None)
        if len(candidates) > 1:
            # (a) fuzzy address-hint auto-pick (e.g. "the one on Capital Circle").
            picked = _fuzzy_pick_store_from_query(query, candidates)
            # (b) haversine via office_lat/office_lng if available.
            if picked is None:
                office_lat = getattr(ctx, "office_lat", None)
                office_lng = getattr(ctx, "office_lng", None)
                if office_lat is not None and office_lng is not None:
                    nearest = find_nearest_store(
                        float(office_lat), float(office_lng),
                        city=city, state=state or None, max_km=50.0,
                    )
                    if nearest:
                        picked = nearest
            # (c) no hint and no office address -> return disambiguation artifact.
            if picked is None:
                return _build_store_disambiguation_response(
                    query=query, candidates=candidates, providers_called=[],
                )
            zip_code = zip_code or picked.get("postal_code", "")
            store_id = picked.get("store_id", "")
        elif len(candidates) == 1 and not zip_code:
            zip_code = candidates[0].get("postal_code", "") or zip_code
            store_id = store_id or candidates[0].get("store_id", "")
        elif not zip_code:
            # City→zip lookup (Wave A.2). Single primary path. No fallback chain.
            looked_up = lookup_zip_by_city(city, state or None)
            if looked_up:
                zip_code = looked_up

    def _product_missing_fields(r: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for field in ("product_name", "price", "url", "image_url", "retailer"):
            v = r.get(field)
            if v is None or (isinstance(v, str) and not v.strip()):
                missing.append(field)
        return missing

    def _store_missing_fields(store: dict[str, Any]) -> list[str]:
        # Irreducible contract: store_name. Without a name the card has no
        # identity worth showing. Everything else (address, phone, website) is
        # supplementary metadata — present when populated, omitted gracefully
        # when not.
        #
        # Phone + website were dropped first because Google Places
        # /details/json enrichment was unreliable. Address followed because
        # the same resolver populates it AND the resolver was returning
        # empty fields for no-zip city queries (common Anam path: "find
        # paint sprayers in Tallahassee"). The card UI handles missing
        # fields cleanly — users get the products they asked for and the
        # store's identifiable name (correct via Pass 1.1 — pickup.store_name
        # / search_information.store_name).
        #
        # Follow-up tracked separately: fix the Google Places resolver path
        # for no-zip queries so address/phone/website reliably populate.
        missing: list[str] = []
        for field in ("store_name",):
            v = store.get(field)
            if v is None or (isinstance(v, str) and not str(v).strip()):
                missing.append(field)
        return missing

    providers_called: list[str] = []
    last_missing_fields: list[str] = []
    final_records: list[dict[str, Any]] = []
    final_sources: list[SourceAttribution] = []
    final_store_summary: dict[str, Any] = {}

    # Track Round-4 provider call for cost attribution. The helper itself does
    # not emit a receipt — the playbook wrapper at server.py records the call
    # via providers_called, and Outcome rolls up to SUCCESS/FAILED on the
    # whole playbook. Logging both branches preserves Law #2 evidence.
    if user_address and user_address.strip():
        if nearest_store is not None:
            providers_called.append("google_places_nearest")
            logger.info(
                "Round-4 nearest HD resolved: %s (zip=%s, %.1fmi from user)",
                nearest_store.name, nearest_store.postal_code,
                nearest_store.distance_miles,
            )
        else:
            providers_called.append("google_places_nearest_failed")
            logger.info(
                "Round-4 nearest HD lookup returned None for user_address=%r — "
                "falling through to Wave A.5 (city -> zip)",
                user_address[:60],
            )

    # Voice path = no zip, no store_id, no city hint. When the request has no
    # location signal, Ava's voice flow is the most common caller and the 5s
    # response budget cannot afford 3 retry attempts × 8s = 24s.
    if voice_path is None:
        voice_path = not zip_code and not store_id and not location_hint

    if voice_path:
        query_attempts = [query]
        hd_timeout = 4.0
        skip_google_shopping = True
    else:
        query_attempts = [
            query,
            f"{query} Home Depot",
            f"{query} Home Depot {location_hint}".strip(),
        ]
        hd_timeout = 8.0
        skip_google_shopping = False

    for attempt_idx, attempt_query in enumerate(query_attempts, start=1):
        records: list[dict[str, Any]] = []
        sources: list[SourceAttribution] = []
        hd_store_info: dict[str, Any] = {}
        resolved_store_id = store_id

        shopping_payload: dict[str, Any] = {"query": attempt_query, "sort_by": 1}
        if zip_code:
            shopping_payload["location"] = zip_code
        elif location_hint:
            shopping_payload["location"] = location_hint
        if on_sale:
            shopping_payload["on_sale"] = True

        async def _resolve_and_search_hd() -> Any:
            nonlocal resolved_store_id, hd_store_info
            if not resolved_store_id and (zip_code or location_hint):
                # Voice path budget is 4s end-to-end. Cap the resolver at 1.5s so
                # a slow Google Places call cannot consume the entire window —
                # the static directory + SerpApi response carry the card even
                # when phone/website/image_url enrichment times out.
                resolver_coro = resolve_store_async(
                    zip_code=zip_code,
                    location_hint=location_hint,
                    correlation_id=ctx.correlation_id,
                    suite_id=ctx.suite_id,
                    office_id=ctx.office_id,
                )
                store_match: dict[str, Any] | None
                if voice_path:
                    try:
                        store_match = await asyncio.wait_for(resolver_coro, timeout=1.5)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Voice path: resolve_store_async timed out at 1.5s — "
                            "continuing without enrichment"
                        )
                        store_match = None
                else:
                    store_match = await resolver_coro
                if store_match:
                    resolved_store_id = str(store_match.get("store_id", "")).strip()
                    hd_store_info = dict(store_match)

            hd_payload: dict[str, Any] = {"query": attempt_query, "hd_sort": "best_match"}
            if resolved_store_id:
                hd_payload["store_id"] = resolved_store_id
            if zip_code:
                hd_payload["delivery_zip"] = zip_code
            return await execute_serpapi_homedepot_search(
                payload=hd_payload,
                correlation_id=ctx.correlation_id,
                suite_id=ctx.suite_id,
                office_id=ctx.office_id,
                timeout=hd_timeout,
            )

        if skip_google_shopping:
            hd_result = await _resolve_and_search_hd()
            shopping_result = None
        else:
            hd_result, shopping_result = await asyncio.gather(
                _resolve_and_search_hd(),
                execute_serpapi_shopping_search(
                    payload=shopping_payload,
                    correlation_id=ctx.correlation_id,
                    suite_id=ctx.suite_id,
                    office_id=ctx.office_id,
                ),
                return_exceptions=True,
            )

        if "serpapi_home_depot" not in providers_called:
            providers_called.append("serpapi_home_depot")
        if not skip_google_shopping and "serpapi_shopping" not in providers_called:
            providers_called.append("serpapi_shopping")

        if not isinstance(hd_result, Exception) and hd_result.outcome.value == "success" and hd_result.data:
            serpapi_store = hd_result.data.get("store", {})
            if serpapi_store.get("store_name"):
                hd_store_info["store_name"] = serpapi_store["store_name"]
            if not hd_store_info.get("store_id") and serpapi_store.get("store_id"):
                hd_store_info["store_id"] = serpapi_store["store_id"]

            # Primary store-identity path: read pickup.store_id from the first
            # product and resolve name + address from the static directory.
            # This is deterministic and doesn't depend on Google Places. Phone
            # and website remain optional enrichment (Task #20).
            raw_results = hd_result.data.get("results", [])
            if raw_results:
                pickup = raw_results[0].get("pickup") or {}
                pickup_store_id = (
                    str(pickup.get("store_id", "")).strip()
                    or str(serpapi_store.get("store_id", "")).strip()
                )
                if pickup_store_id:
                    directory_record = lookup_store_by_id(pickup_store_id)
                    if directory_record:
                        # Static directory wins for name + address fields.
                        hd_store_info["store_id"] = directory_record["store_id"]
                        hd_store_info["store_name"] = directory_record["name"]
                        hd_store_info["address"] = directory_record["address"]
                        hd_store_info["city"] = directory_record["city"]
                        hd_store_info["state"] = directory_record["state"]
                        hd_store_info["postal_code"] = directory_record["postal_code"]
                        resolved_store_id = directory_record["store_id"]
                    else:
                        logger.warning(
                            "HD store_id %s not in static directory — "
                            "falling back to SerpApi store name",
                            pickup_store_id,
                        )

            for item in raw_results[:8]:
                product = normalize_from_serpapi_homedepot(item)
                records.append(product.to_dict())
                sources.extend(product.sources)

        if (
            shopping_result is not None
            and not isinstance(shopping_result, Exception)
            and shopping_result.outcome.value == "success"
            and shopping_result.data
        ):
            for item in shopping_result.data.get("results", [])[:6]:
                product = normalize_from_serpapi_shopping(item)
                records.append(product.to_dict())
                sources.extend(product.sources)

        hd_products = [r for r in records if r.get("retailer") == "Home Depot"]
        complete_products = [r for r in hd_products if not _product_missing_fields(r)]
        # Sub-item 1.1: surface SerpApi search_information.store_name as
        # store_summary.name so the store-summary card has the correct local
        # store label even when the resolver disagrees with SerpApi's pin.
        store_summary = {
            "card_kind": "store_summary",
            "store_id": hd_store_info.get("store_id", ""),
            "store_name": hd_store_info.get("store_name", ""),
            "name": hd_store_info.get("store_name", ""),
            "address": hd_store_info.get("address", ""),
            "city": hd_store_info.get("city", ""),
            "state": hd_store_info.get("state", ""),
            "postal_code": hd_store_info.get("postal_code", ""),
            "phone": hd_store_info.get("phone", ""),
            "website": hd_store_info.get("website", ""),
            "image_url": hd_store_info.get("image_url", ""),
            "open_now": hd_store_info.get("open_now"),
            "rating": hd_store_info.get("rating"),
            "retailer": "Home Depot",
        }

        # Round 4: when nearest_store is set, override the Google-derived
        # fields (formatted address + photo + distance). The static directory
        # still contributes city/state/postal_code/store_id since those are
        # what SerpApi keys against. distance_miles is hero data for the card.
        if nearest_store is not None:
            store_summary["address"] = nearest_store.address or store_summary["address"]
            if nearest_store.photo_url:
                store_summary["image_url"] = nearest_store.photo_url
            store_summary["distance_miles"] = round(nearest_store.distance_miles, 1)
            # If the static directory missed (unknown pickup.store_id), still
            # show the user something — Google's name + Google's place_id.
            if not store_summary.get("store_name"):
                store_summary["store_name"] = nearest_store.name
                store_summary["name"] = nearest_store.name
            if not store_summary.get("store_id"):
                store_summary["store_id"] = nearest_store.place_id
            if not store_summary.get("postal_code"):
                store_summary["postal_code"] = nearest_store.postal_code

        store_missing = _store_missing_fields(store_summary)
        last_missing_fields = sorted({
            *[m for r in hd_products for m in _product_missing_fields(r)],
            *[f"store_summary.{f}" for f in store_missing],
        })

        logger.info(
            "TOOL_MATERIAL_PRICE_CHECK attempt=%s hd_products=%s complete_hd_products=%s store_missing=%s",
            attempt_idx, len(hd_products), len(complete_products), store_missing,
        )

        if complete_products and not store_missing:
            # Card pack is Home Depot-specific by design: include only the
            # Home Depot store summary + Home Depot products.
            final_records = [store_summary, *complete_products]
            final_sources = sources
            final_store_summary = store_summary
            break

    if not final_records:
        return ResearchResponse(
            artifact_type="error",
            summary="I could not retrieve complete Home Depot product and store details right now. Please try again in a moment.",
            records=[],
            sources=[],
            freshness={"mode": "live"},
            confidence={"status": "unverified", "score": 0.0},
            missing_fields=last_missing_fields,
            next_queries=["Try again in a moment", "Use a different city or ZIP"],
            segment="trades",
            intent="price_check",
            playbook="TOOL_MATERIAL_PRICE_CHECK",
            providers_called=providers_called,
            extra={"hard_fail": True, "missing_fields": last_missing_fields},
        )

    report = verify_records(records=final_records, sources=final_sources, required_fields=["product_name", "price", "retailer"])

    hd_count = sum(1 for r in final_records if r.get("retailer") == "Home Depot" and r.get("card_kind") != "store_summary")
    in_stock = sum(1 for r in final_records if r.get("retailer") == "Home Depot" and r.get("in_store_stock") and r["in_store_stock"] > 0)
    summary_parts = [f"Price check for {query[:60]}"]
    if final_store_summary.get("store_name"):
        summary_parts.append(
            f"Home Depot store: {final_store_summary['store_name']} (#{final_store_summary.get('store_id', '')})"
        )
    summary_parts.append(f"{hd_count} Home Depot products, {in_stock} in stock")

    return ResearchResponse(
        artifact_type="PriceComparison",
        summary=". ".join(summary_parts) + ".",
        records=final_records,
        sources=final_sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=[
            f"Compare prices at Lowe's near {zip_code}" if zip_code else "Compare at other retailers",
            "Check for current sales and promotions",
        ],
        verification_report=report,
        segment="trades",
        intent="price_check",
        playbook="TOOL_MATERIAL_PRICE_CHECK",
        providers_called=providers_called,
        extra={"store_summary": final_store_summary, "cards_version": "v1"},
    )

async def execute_competitor_pricing_scan(
    query: str, ctx: PlaybookContext, location: str = "",
) -> ResearchResponse:
    """COMPETITOR_PRICING_SCAN — Map local competitors and pricing signals."""
    from aspire_orchestrator.providers.google_places_client import execute_google_places_search
    from aspire_orchestrator.providers.exa_client import execute_exa_search
    from aspire_orchestrator.services.adam.normalizers.business_normalizer import normalize_from_google_places
    from aspire_orchestrator.services.adam.normalizers.web_normalizer import normalize_from_exa

    logger.info("Executing COMPETITOR_PRICING_SCAN for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. Google Places for local competitors
    gp_result = await execute_google_places_search(
        payload={"query": query, "location": location},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("google_places")

    if gp_result.outcome.value == "success" and gp_result.data:
        for place in gp_result.data.get("results", [])[:10]:
            biz = normalize_from_google_places(place)
            records.append(biz.to_dict())
            sources.extend(biz.sources)

    # 2. Exa deep-lite for competitor intelligence with structured output
    exa_result = await execute_exa_search(
        payload={
            "query": f"competitor pricing analysis {query}",
            "type": "deep-lite",
            "category": "company",
            "num_results": 5,
            "moderation": True,
        },
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("exa")

    exa_grounding: list[dict[str, Any]] = []
    if exa_result.outcome.value == "success" and exa_result.data:
        for r in exa_result.data.get("results", [])[:5]:
            we = normalize_from_exa(r)
            records.append(we.to_dict())
            sources.append(SourceAttribution(provider="exa"))
        exa_grounding = exa_result.data.get("grounding", [])

    report = verify_records(
        records=records, sources=sources,
        required_fields=["name", "normalized_address"],
        exa_grounding=exa_grounding,
    )

    return ResearchResponse(
        artifact_type="CompetitorBrief",
        summary=f"Competitor scan for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Deep dive on top competitor", "Compare pricing models"],
        verification_report=report,
        segment="trades",
        intent="compare",
        playbook="COMPETITOR_PRICING_SCAN",
        providers_called=providers_called,
    )


async def execute_subcontractor_scout(
    query: str, ctx: PlaybookContext, location: str = "",
) -> ResearchResponse:
    """SUBCONTRACTOR_SCOUT — Find nearby subcontractors by trade."""
    from aspire_orchestrator.providers.google_places_client import execute_google_places_search
    from aspire_orchestrator.providers.foursquare_client import execute_foursquare_search
    from aspire_orchestrator.services.adam.normalizers.business_normalizer import (
        normalize_from_google_places,
        normalize_from_foursquare,
    )

    logger.info("Executing SUBCONTRACTOR_SCOUT for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. Google Places
    gp_result = await execute_google_places_search(
        payload={"query": query, "location": location},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("google_places")

    if gp_result.outcome.value == "success" and gp_result.data:
        for place in gp_result.data.get("results", [])[:10]:
            biz = normalize_from_google_places(place)
            records.append(biz.to_dict())
            sources.extend(biz.sources)

    # 2. Foursquare for additional coverage
    fs_result = await execute_foursquare_search(
        payload={"query": query, "near": location},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("foursquare")

    if fs_result.outcome.value == "success" and fs_result.data:
        for place in fs_result.data.get("results", [])[:5]:
            biz = normalize_from_foursquare(place)
            records.append(biz.to_dict())
            sources.extend(biz.sources)

    report = verify_records(
        records=records, sources=sources,
        required_fields=["name", "normalized_address", "phone"],
    )

    return ResearchResponse(
        artifact_type="VendorShortlist",
        summary=f"Subcontractor search for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Verify licensing", "Check reviews in detail"],
        verification_report=report,
        segment="trades",
        intent="lookup",
        playbook="SUBCONTRACTOR_SCOUT",
        providers_called=providers_called,
    )


async def execute_territory_opportunity_scan(
    query: str, ctx: PlaybookContext, geo_scope: str = "",
) -> ResearchResponse:
    """TERRITORY_OPPORTUNITY_SCAN — Identify promising ZIPs by density + activity."""
    from aspire_orchestrator.providers.attom_client import execute_attom_sales_trends
    from aspire_orchestrator.providers.google_places_client import execute_google_places_search
    from aspire_orchestrator.providers.exa_client import execute_exa_search
    from aspire_orchestrator.services.adam.normalizers.business_normalizer import normalize_from_google_places
    from aspire_orchestrator.services.adam.normalizers.web_normalizer import normalize_from_exa

    logger.info("Executing TERRITORY_OPPORTUNITY_SCAN for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. ATTOM sales trends for market activity
    if geo_scope:
        trends_result = await execute_attom_sales_trends(
            payload={"geoid": geo_scope, "geo_type": "ZI"},
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
        )
        providers_called.append("attom")
        if trends_result.outcome.value == "success" and trends_result.data:
            records.append({"type": "market_trends", "data": trends_result.data})
            sources.append(SourceAttribution(provider="attom"))

    # 2. Google Places for competitor density
    gp_result = await execute_google_places_search(
        payload={"query": query, "location": geo_scope},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("google_places")

    if gp_result.outcome.value == "success" and gp_result.data:
        for place in gp_result.data.get("results", [])[:10]:
            biz = normalize_from_google_places(place)
            records.append(biz.to_dict())
            sources.extend(biz.sources)

    # 3. Exa for market intelligence
    exa_result = await execute_exa_search(
        payload={
            "query": f"market opportunity {query} {geo_scope}",
            "type": "deep-lite",
            "num_results": 5,
        },
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("exa")

    if exa_result.outcome.value == "success" and exa_result.data:
        for r in exa_result.data.get("results", [])[:5]:
            we = normalize_from_exa(r)
            records.append(we.to_dict())
            sources.append(SourceAttribution(provider="exa"))

    report = verify_records(records=records, sources=sources)

    return ResearchResponse(
        artifact_type="TerritoryAnalysis",
        summary=f"Territory scan for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Drill into top ZIP code", "Compare adjacent territories"],
        verification_report=report,
        segment="trades",
        intent="territory_scan",
        playbook="TERRITORY_OPPORTUNITY_SCAN",
        providers_called=providers_called,
    )

