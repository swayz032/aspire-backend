"""Adam Research Playbooks — Landlord Segment.

Six playbook execute functions for the landlord segment:
  1. property_facts          — full property fact pack from ATTOM
  2. rent_comp_context       — rental AVM + sales comparables context
  3. permit_and_renovation   — permit signals + web evidence
  4. neighborhood_demand     — sales trends + competitor density
  5. screening_compliance    — tenant screening law compliance research
  6. turnover_vendor_scout   — make-ready vendor discovery

Provider routing (per ecosystem providers.yaml + ATTOM ADR-002):
  Geocode: here (primary address normalization before ATTOM)
  Property: attom (authoritative — trust class A)
  Places:   google_places → foursquare → here
  Web:      exa (primary) → brave (fallback)

Guardrails:
  - execute_rent_comp_context: never manufacture a price target if rental AVM
    returns no data. Return missing_fields=["estimated_rent"] + next_queries.
  - execute_screening_compliance: never produce tenant scoring, never emit
    accept/reject recommendations. Compliance-information only.
  - ATTOM requires address normalization via HERE geocoding first (ADR-002).
    If HERE geocoding fails, proceed with raw address string (best-effort).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome


def _safe_result(result: Any) -> Any | None:
    """Guard for asyncio.gather(return_exceptions=True) results.

    If the result is an exception (provider crash, timeout, etc.), log it
    and return None so the caller can skip merging.  Otherwise return the
    ToolExecutionResult as-is.
    """
    if isinstance(result, BaseException):
        logger.warning("gather sub-task raised %s: %s", type(result).__name__, result)
        return None
    return result
from aspire_orchestrator.providers.attom_client import (
    _attom_request,
    execute_attom_detail_mortgage_owner,
    execute_attom_property_detail,
    execute_attom_property_detail_with_schools,
    execute_attom_rental_avm,
    execute_attom_sales_comparables,
    execute_attom_sales_history,
    execute_attom_sales_trends,
    execute_attom_valuation_avm,
)

# Additional ATTOM endpoints for full property intelligence
# These are imported inside execute_property_facts to avoid circular imports
# execute_attom_assessment_detail, execute_attom_sale_detail, execute_attom_equity
from aspire_orchestrator.providers.brave_client import execute_brave_search
from aspire_orchestrator.providers.exa_client import execute_exa_search
from aspire_orchestrator.providers.foursquare_client import execute_foursquare_search
from aspire_orchestrator.providers.google_places_client import execute_google_places_search
from aspire_orchestrator.providers.here_client import execute_here_search
from aspire_orchestrator.services.adam.normalizers.business_normalizer import (
    normalize_from_foursquare,
    normalize_from_google_places,
    normalize_from_here,
)
from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
    normalize_from_attom_assessment,
    normalize_from_attom_avm,
    normalize_from_attom_detail,
    normalize_from_attom_equity,
    normalize_from_attom_expanded_profile,
    normalize_from_attom_rental,
    normalize_from_attom_sale_detail,
    normalize_from_attom_sales_history,
    normalize_from_attom_foreclosure,
    normalize_from_attom_schools,
    normalize_from_attom_valuation,
)
from aspire_orchestrator.services.adam.normalizers.web_normalizer import (
    normalize_from_brave,
    normalize_from_exa,
)
from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.schemas.research_response import ResearchResponse
from aspire_orchestrator.services.adam.schemas.verification_report import VerificationReport
from aspire_orchestrator.services.adam.verifier import verify_records

logger = logging.getLogger(__name__)

_NOW = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _provider_args(ctx: PlaybookContext) -> dict[str, Any]:
    """Shared keyword args for every provider execute call."""
    return {
        "correlation_id": ctx.correlation_id,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "capability_token_id": ctx.capability_token_id,
        "capability_token_hash": ctx.capability_token_hash,
    }


# ---------------------------------------------------------------------------
# Card-per-section fanout (W0)
# ---------------------------------------------------------------------------
# The desktop carousel renders one PropertyCard per record in the response.
# `_cardSection` (consumed in PropertyCard.tsx:599) selects which renderer
# from SECTION_RENDERERS (PropertyCard.tsx:344) to use. By emitting one
# record per section we light up every category of data ATTOM returned —
# overview, ownership, mortgage, valuation, sale_history, foreclosure,
# permits, schools, rental — instead of cramming everything into a single
# overview card.
#
# Each section has a predicate that decides whether the card is worth
# emitting at all. Empty sections are skipped so the user never swipes onto
# a blank "Mortgage" or "Schools" card. The predicate signature is
# `(record) -> bool`; predicates must be total functions and never raise.
#
# Order is locked to read like a property report:
#   overview → ownership → valuation → mortgage → sale_history →
#   foreclosure → permits → schools → rental
_SECTION_PLAN: list[tuple[str, str, Any]] = [
    ("overview", "Property Overview",
        lambda r: True),
    ("ownership", "Ownership",
        lambda r: bool(r.get("owner_name") or r.get("previous_owner_name") or r.get("mailing_address"))),
    ("valuation", "Valuations & Tax",
        lambda r: bool(r.get("tax_market_value") or r.get("estimated_value") or r.get("annual_tax_amount"))),
    ("avm_history", "AVM History",
        lambda r: bool(r.get("avm_history"))),
    ("mortgage", "Mortgage",
        lambda r: bool(r.get("mortgage_amount") or r.get("mortgage_lender") or r.get("current_loan_balance"))),
    ("sale_history", "Sale History",
        lambda r: bool(r.get("last_sale_amount") or r.get("last_sale_date") or r.get("sale_history"))),
    ("transaction_history", "Transaction History",
        lambda r: bool(r.get("transaction_history"))),
    ("comps", "Sales Comparables",
        lambda r: bool(r.get("comps"))),
    ("foreclosure", "Foreclosure",
        lambda r: bool(
            r.get("prior_foreclosure")
            or r.get("foreclosure_records")
            or r.get("foreclosure_filing")
            or (r.get("foreclosure_stage") and r.get("foreclosure_stage") != "none")
            or r.get("reo_flag")
        )),
    ("permits", "Permits",
        lambda r: bool(r.get("permit_signals") or r.get("major_improvements_year"))),
    ("schools", "Schools",
        lambda r: bool(r.get("nearby_schools") or r.get("school_district_name"))),
    ("community", "Community & Demographics",
        lambda r: bool(r.get("community"))),
    ("poi", "Points of Interest",
        lambda r: bool(r.get("poi"))),
    ("salestrend", "Market Sales Trends",
        lambda r: bool(r.get("salestrend"))),
    ("rental", "Rental",
        lambda r: r.get("estimated_rent") is not None),
]


def fan_out_property_sections(prop_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand a single normalized property record into one record per UI
    section.

    Returns a list of dict copies, each tagged with `_cardSection`,
    `_sectionLabel`, and `_section_order`. Sections whose predicate returns
    False are skipped. If every predicate fails (extremely sparse property),
    a single overview-tagged record is returned as a safety net so the
    carousel always shows at least one card.
    """
    section_records: list[dict[str, Any]] = []
    for order, (section, label, predicate) in enumerate(_SECTION_PLAN):
        try:
            if not predicate(prop_dict):
                continue
        except Exception:  # noqa: BLE001 — predicate guards must never crash playbook
            continue
        section_record = dict(prop_dict)
        section_record["_cardSection"] = section
        section_record["_sectionLabel"] = label
        section_record["_section_order"] = order
        section_records.append(section_record)

    if not section_records:
        fallback = dict(prop_dict)
        fallback["_cardSection"] = "overview"
        fallback["_sectionLabel"] = "Property Overview"
        fallback["_section_order"] = 0
        section_records.append(fallback)

    return section_records


def _extract_location_label(query: str) -> str:
    """Pull a human location string out of an investment-scan query for
    the Exa web search prompt. Examples:
      "auctions in Forest Park GA" → "Forest Park GA"
      "Atlanta ga foreclosures"   → "Atlanta GA"
      "houses for sale 30297"     → "30297"
    """
    import re as _re_loc
    raw = (query or "").strip()
    if not raw:
        return ""

    # Prefer literal ZIP if present.
    zip_m = _re_loc.search(r"\b(\d{5})\b", raw)
    if zip_m:
        return zip_m.group(1)

    # Capture "<City> <ST>" or "<City>, <ST>" anywhere in the query.
    # Allow lowercased state codes ("ga", "fl") because users dictate.
    city_state = _re_loc.search(
        r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)\s*,?\s+([A-Za-z]{2})\b",
        raw,
    )
    if city_state:
        city = city_state.group(1).strip()
        state = city_state.group(2).strip().upper()
        return f"{city} {state}"

    # Last resort: return the whole query trimmed to ~60 chars so the Exa
    # search has SOMETHING to anchor on.
    return raw[:60]


def _split_city_state(query: str) -> tuple[str, str]:
    """Pull (city, state) from a query. Returns ("", "") if not found.
    Handles "Forest Park GA", "Atlanta, GA", "auctions in Forest Park ga".
    """
    import re as _re_cs
    m = _re_cs.search(
        r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)\s*,?\s+([A-Za-z]{2})\b",
        (query or "").strip(),
    )
    if not m:
        return "", ""
    return m.group(1).strip(), m.group(2).strip().upper()


async def _resolve_city_to_geoid(
    city: str, state: str, ctx: PlaybookContext,
) -> str:
    """Resolve a (city, state) pair to ATTOM's geoIdV4 for the matching
    incorporated place. ATTOM /v4/location/lookup returns up to 10
    name-matching geographies; we filter by state from geographyName
    (which contains "<City>, <County>, <ST>"). Returns "" on no match.
    """
    if not city or not state:
        return ""
    from aspire_orchestrator.providers.attom_client import (
        execute_attom_location_lookup,
    )
    args = _provider_args(ctx)
    result = await execute_attom_location_lookup(
        payload={"name": city, "geography_type": "PL"},
        **args,
    )
    if not result or result.outcome != Outcome.SUCCESS or not result.data:
        return ""
    geographies = result.data.get("geographies", []) or []
    target_state = state.upper()
    for geo in geographies:
        geo_name = str(geo.get("geographyName") or "")
        if f", {target_state}" in geo_name.upper() or geo_name.upper().endswith(f", {target_state}"):
            geo_id = str(geo.get("geoIdV4") or "").strip()
            if geo_id:
                logger.info(
                    "Resolved city %r/%s to geoIdV4=%s (matched %r)",
                    city, target_state, geo_id, geo_name,
                )
                return geo_id
    logger.info(
        "No geoIdV4 match for %r/%s in %d candidates",
        city, target_state, len(geographies),
    )
    return ""


def _source(provider: str) -> SourceAttribution:
    return SourceAttribution(provider=provider, retrieved_at=_NOW())


def _confidence_dict(report: VerificationReport) -> dict[str, Any]:
    return {
        "status": report.status,
        "score": report.confidence_score,
        "source_count": report.source_count,
        "conflict_count": report.conflict_count,
    }


def _extract_address(query: str) -> str:
    """Extract a US address from a natural language query.

    Looks for patterns like "123 Main St, City, ST 12345" inside the query.
    Falls back to the full query if no address pattern found (ATTOM will parse it).
    """
    import re
    # Pattern: number + street, then city/state/zip.
    # Accept both state abbreviations (GA) and full names (Georgia).
    match = re.search(
        r'(\d+\s+[\w\s]+(?:St|Ave|Rd|Blvd|Dr|Ln|Ct|Way|Pl|Cir|Pkwy|Hwy|Ter|Loop|Trail)\.?'
        r'(?:\s*,\s*[\w\s]+,?\s*(?:[A-Z]{2}|[A-Za-z]{4,})\s*,?\s*\d{5}(?:-\d{4})?))',
        query, re.IGNORECASE,
    )
    if match:
        addr = match.group(1).strip()
        logger.debug("Extracted address from query: '%s' -> '%s'", query[:60], addr)
        return addr

    # Fallback: if query contains a comma and a ZIP, strip common leading intent
    # phrases (including stacked prefixes like "pull property facts for ...").
    prefixes = [
        "pull property facts for", "pull property details for", "pull property profile for",
        "find property facts for", "find property details for", "find property profile for",
        "give me", "pull", "get", "show me", "find", "look up",
        "property facts for", "property profile for", "property details for",
        "the full property profile for", "the property profile for",
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

    # Explicit wrapper fallback used by invoke-sync ("... Additional details: ...")
    lower_query = query.lower()
    if marker in lower_query:
        idx = lower_query.rfind(marker)
        tail = query[idx + len(marker):].strip(" .,:;-")
        if tail:
            return tail

    # Final fallback: try to recover an inline address from noisy wrappers
    # like "property lookup. Additional details: 4863 Price Street, ...".
    loose = re.search(
        r'(\d+\s+[\w\s]+(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Ct|Court|Way|Pl|Place|Cir|Circle|Pkwy|Parkway|Hwy|Highway|Ter|Terrace|Loop|Trail)\b[^,\n]*'
        r'(?:,\s*[\w\s]+){0,2}\s*,?\s*(?:[A-Z]{2}|[A-Za-z]{4,})\s*,?\s*\d{5}(?:-\d{4})?)',
        query,
        re.IGNORECASE,
    )
    if loose:
        return loose.group(1).strip()

    return query


def _extract_house_number(address: str) -> str:
    """Return house number from an address string, else empty string.

    Prefers leading number but falls back to first standalone number so
    wrapped prompts ("... details: 4863 Price St ...") still pin correctly.
    """
    if not isinstance(address, str):
        return ""
    s = address.strip()
    m = re.match(r"\s*(\d+)\b", s)
    if m:
        return m.group(1)
    m2 = re.search(r"\b(\d{1,6})\b", s)
    return m2.group(1) if m2 else ""


def _normalize_addr_key(address: str) -> str:
    """Lightweight normalization for fuzzy address matching."""
    if not isinstance(address, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", address.lower())


def _choose_best_attom_property_index(requested_address: str, properties: list[dict[str, Any]]) -> int:
    """Pick the ATTOM property entry that best matches the requested address."""
    if not properties:
        return 0

    req = requested_address or ""
    req_key = _normalize_addr_key(req)
    req_house = _extract_house_number(req)
    req_parts = [p.strip().lower() for p in req.split(",") if p.strip()]
    req_street = req_parts[0] if req_parts else ""
    req_city = req_parts[1] if len(req_parts) > 1 else ""

    best_idx = 0
    best_score = -1
    for idx, prop in enumerate(properties):
        addr = ((prop or {}).get("address") or {}).get("oneLine", "") or ""
        addr_key = _normalize_addr_key(addr)
        addr_house = _extract_house_number(addr)
        addr_parts = [p.strip().lower() for p in addr.split(",") if p.strip()]
        addr_street = addr_parts[0] if addr_parts else ""
        addr_city = addr_parts[1] if len(addr_parts) > 1 else ""

        score = 0
        if req_key and addr_key and req_key == addr_key:
            score += 100
        # House number is a hard discriminator for subject-property lookups.
        if req_house:
            if addr_house and req_house == addr_house:
                score += 40
            elif addr_house and req_house != addr_house:
                score -= 80
            else:
                score -= 40
        if req_street and addr_street and req_street in addr_street:
            score += 10
        if req_city and addr_city and req_city == addr_city:
            score += 5

        if score > best_score:
            best_score = score
            best_idx = idx

    return best_idx


def _pin_attom_payload_to_subject(payload: dict[str, Any] | None, requested_address: str) -> dict[str, Any] | None:
    """Return payload with property list pinned to best subject match."""
    if not isinstance(payload, dict):
        return payload
    props = payload.get("property")
    if not isinstance(props, list) or len(props) <= 1:
        return payload
    idx = _choose_best_attom_property_index(requested_address, props)
    chosen = props[idx]
    cloned = dict(payload)
    cloned["property"] = [chosen]
    return cloned


async def _geocode_address(query: str, context: PlaybookContext) -> str:
    """Best-effort address normalization using HERE search.

    Falls back to extracted/raw address when HERE fails or returns no candidates.
    """
    raw_address = _extract_address(query)
    raw_house = _extract_house_number(raw_address)
    args = _provider_args(context)
    try:
        result = await execute_here_search(
            payload={"query": raw_address, "limit": 1},
            **args,
        )
        if result.outcome == Outcome.SUCCESS and result.data:
            first = (result.data.get("results") or [{}])[0]
            normalized = first.get("address")
            if isinstance(normalized, str) and normalized.strip():
                normalized = normalized.strip()
                normalized_house = _extract_house_number(normalized)
                # Guardrail: if user gave a specific house number, do not degrade
                # to a street-level address that loses the number.
                if raw_house and normalized_house and raw_house == normalized_house:
                    return normalized
                if raw_house and not normalized_house:
                    logger.warning(
                        "landlord._geocode_address dropped house number (%s -> %s); using raw address",
                        raw_address, normalized,
                    )
                    return raw_address
                return normalized
    except Exception as exc:
        logger.warning("landlord._geocode_address failed: %s", exc)
    return raw_address


# ---------------------------------------------------------------------------
# 1. Property Facts
# ---------------------------------------------------------------------------

async def execute_property_facts(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Build a full property fact pack from ATTOM authoritative data.

    Deterministic primary path (NO fallback chain — Aspire no-fallback principle):
      1. parse_us_address(query) -> address1, address2 (raises ParseError if
         street_number/street_name/city/state are missing)
      2. ATTOM /expandedprofile + supporting endpoints called in parallel with
         the parsed address1+address2 (no HERE geocoding round-trip required;
         usaddress is a CRF tagger trained on the USPS address corpus)
      3. ParseError surfaces as a Yellow-tier `needs_more_input` artifact
         asking the user for a complete address — NOT a degraded fallback

    required_fields: normalized_address, living_sqft, year_built, owner_name
    Returns: PropertyFactPack artifact (legacy alias: LandlordPropertyPack).
    """
    logger.info(
        "landlord.property_facts start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # Step 1: Deterministic address parse (usaddress CRF tagger).
    # ATTOM /expandedprofile expects address1 + address2 in normalized USPS
    # form. parse_us_address produces them directly — no HERE geocoding needed.
    from aspire_orchestrator.services.adam.address_parser import (
        ParseError, parse_us_address,
    )
    try:
        parsed = parse_us_address(query)
    except ParseError as exc:
        logger.info(
            "landlord.property_facts: address parse failed (asking user) — %s",
            exc,
            extra={"correlation_id": context.correlation_id},
        )
        return ResearchResponse(
            artifact_type="needs_more_input",
            summary=(
                "I need a city and state to look that up — "
                "what's the full address?"
            ),
            records=[],
            sources=[],
            freshness={"mode": "live", "provider": "address_parser"},
            confidence={"status": "needs_input", "score": 0.0},
            missing_fields=["city", "state"],
            next_queries=[
                "Provide street, city, state (and ZIP if you have it)",
            ],
            segment="landlord",
            intent="property_facts",
            playbook="landlord.property_facts",
            providers_called=[],
            extra={"parse_error": str(exc), "raw_query": query},
        )

    normalized_address = f"{parsed.address1}, {parsed.address2}"
    providers_called.append("address_parser")

    # Pass structured components directly to ATTOM. _validate_address now
    # honors `address1`/`address2` payload keys when present (deterministic
    # primary path — no internal regex split, no HERE round-trip).
    attom_payload = {
        "address1": parsed.address1,
        "address2": parsed.address2,
        # Keep `address` for callers/normalizers that still expect a single
        # combined string in source attribution.
        "address": normalized_address,
    }

    # Step 2: 12 parallel ATTOM calls + 1 Apify Zillow photos call —
    # single asyncio.gather covers everything that doesn't depend on
    # detail-response IDs. Each call has its own retry + per-call timeout
    # so the wallclock is bounded by the slowest endpoint.
    from aspire_orchestrator.providers.attom_client import (
        execute_attom_allevents_snapshot,
        execute_attom_assessment_detail,
        execute_attom_avm_history,
        execute_attom_building_permits,
        execute_attom_expanded_profile,
        execute_attom_home_equity,
        execute_attom_poi_search,
        execute_attom_property_detail_with_schools,
        execute_attom_sale_detail,
        execute_attom_sales_expanded_history,
    )
    from aspire_orchestrator.providers.apify_zillow_client import (
        execute_apify_zillow_photos,
    )

    _raw_results = await asyncio.gather(
        execute_attom_detail_mortgage_owner(payload=attom_payload, **args),
        execute_attom_valuation_avm(payload=attom_payload, **args),
        execute_attom_home_equity(payload=attom_payload, **args),
        execute_attom_assessment_detail(payload=attom_payload, **args),
        execute_attom_sale_detail(payload=attom_payload, **args),
        execute_attom_expanded_profile(payload=attom_payload, **args),
        execute_attom_allevents_snapshot(payload=attom_payload, **args),
        execute_attom_poi_search(payload=attom_payload, **args),
        execute_attom_avm_history(payload=attom_payload, **args),
        execute_attom_building_permits(payload=attom_payload, **args),
        execute_attom_property_detail_with_schools(payload=attom_payload, **args),
        execute_attom_sales_expanded_history(payload=attom_payload, **args),
        execute_apify_zillow_photos(payload={"address": normalized_address}, **args),
        return_exceptions=True,
    )
    # Guard: convert exceptions to None (graceful degradation, not crash)
    detail_result = _safe_result(_raw_results[0])      # building + owner + mortgage
    avm_result = _safe_result(_raw_results[1])          # AVM value + confidence + FSD
    equity_result = _safe_result(_raw_results[2])       # LTV, equity, loan balance
    assessment_result = _safe_result(_raw_results[3])   # tax assessment + market value
    sale_result = _safe_result(_raw_results[4])         # last sale detail + price/sqft
    expanded_result = _safe_result(_raw_results[5])     # zoning, seller, census, REO flags
    allevents_result = _safe_result(_raw_results[6])    # full transaction history (W1b)
    poi_result = _safe_result(_raw_results[7])          # nearby POIs (W2)
    avm_history_result = _safe_result(_raw_results[8])  # AVM trajectory (W2)
    permit_result = _safe_result(_raw_results[9])       # building permits (was sequential)
    schools_result = _safe_result(_raw_results[10])     # nearby schools (was sequential)
    fc_result = _safe_result(_raw_results[11])          # foreclosure / expanded sale history (was sequential)
    apify_result = _safe_result(_raw_results[12])       # Apify Zillow photos (Estimate Studio Visuals)
    providers_called.append("attom")
    providers_called.append("apify_zillow")

    # --- Normalize base property record (building + owner + mortgage) ---
    prop_dict: dict[str, Any] = {}
    if detail_result and detail_result.outcome == Outcome.SUCCESS and detail_result.data:
        pinned_detail_data = _pin_attom_payload_to_subject(detail_result.data, normalized_address)
        prop = normalize_from_attom_detail(pinned_detail_data or detail_result.data)
        prop_dict = prop.to_dict()
        sources.append(_source("attom"))
    else:
        logger.warning(
            "landlord.property_facts: attom detail failed: %s",
            detail_result.error if detail_result else "provider raised exception",
            extra={"correlation_id": context.correlation_id},
        )

    # --- Merge AVM valuation (value + confidence score + FSD) ---
    if avm_result and avm_result.outcome == Outcome.SUCCESS and avm_result.data:
        pinned_avm_data = _pin_attom_payload_to_subject(avm_result.data, normalized_address)
        avm = normalize_from_attom_avm(pinned_avm_data or avm_result.data)
        if avm and prop_dict:
            prop_dict.update(avm)

    # --- Merge equity (LTV, available equity, loan balance, est. payment) ---
    if equity_result and equity_result.outcome == Outcome.SUCCESS and equity_result.data:
        pinned_equity_data = _pin_attom_payload_to_subject(equity_result.data, normalized_address)
        equity = normalize_from_attom_equity(pinned_equity_data or equity_result.data)
        if equity and prop_dict:
            prop_dict.update(equity)

    # --- Merge tax assessment (assessed value, market value, annual tax) ---
    if assessment_result and assessment_result.outcome == Outcome.SUCCESS and assessment_result.data:
        pinned_assessment_data = _pin_attom_payload_to_subject(assessment_result.data, normalized_address)
        tax = normalize_from_attom_assessment(pinned_assessment_data or assessment_result.data)
        if tax and prop_dict:
            prop_dict.update(tax)

    # --- Merge sale detail (last sale price, price/sqft, appreciation) ---
    if sale_result and sale_result.outcome == Outcome.SUCCESS and sale_result.data:
        pinned_sale_data = _pin_attom_payload_to_subject(sale_result.data, normalized_address)
        sale = normalize_from_attom_sale_detail(pinned_sale_data or sale_result.data)
        if sale and prop_dict:
            prop_dict.update(sale)

    # --- Merge expanded profile (zoning, seller, census, REO flags) ---
    if expanded_result and expanded_result.outcome == Outcome.SUCCESS and expanded_result.data:
        pinned_expanded_data = _pin_attom_payload_to_subject(expanded_result.data, normalized_address)
        expanded = normalize_from_attom_expanded_profile(pinned_expanded_data or expanded_result.data)
        if expanded and prop_dict:
            prop_dict.update(expanded)

    # --- Merge full transaction history (W1b — ATTOM /allevents/snapshot) ---
    # Provides the complete recorded timeline (sale, mortgage origination,
    # transfer, assessment, foreclosure filing, etc.) for the property. The
    # SaleHistorySection on the desktop card surfaces this as a chronological
    # list. Failure here is non-fatal — transcript-level data still flows.
    if allevents_result and allevents_result.outcome == Outcome.SUCCESS and allevents_result.data:
        try:
            from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
                normalize_from_attom_allevents,
            )
            pinned_events_data = _pin_attom_payload_to_subject(allevents_result.data, normalized_address)
            transaction_history = normalize_from_attom_allevents(pinned_events_data or allevents_result.data)
            if transaction_history and prop_dict:
                prop_dict["transaction_history"] = transaction_history
        except Exception as exc:  # noqa: BLE001 — never fail the pull on a normalizer bug
            logger.warning("landlord.property_facts: allevents normalize failed: %s", exc)

    # --- Merge POI search (W2 — ATTOM /v4/neighborhood/poi) ---
    # Up to 25 nearby points of interest, deduplicated, sorted by distance.
    if poi_result and poi_result.outcome == Outcome.SUCCESS and poi_result.data:
        try:
            from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
                normalize_from_attom_poi,
            )
            poi_list = normalize_from_attom_poi(poi_result.data)
            if poi_list and prop_dict:
                prop_dict["poi"] = poi_list
        except Exception as exc:  # noqa: BLE001
            logger.warning("landlord.property_facts: poi normalize failed: %s", exc)

    # --- Merge AVM trajectory (W2 — ATTOM /avmhistory/detail) ---
    if avm_history_result and avm_history_result.outcome == Outcome.SUCCESS and avm_history_result.data:
        try:
            from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
                normalize_from_attom_avm_history,
            )
            pinned_avmhx = _pin_attom_payload_to_subject(avm_history_result.data, normalized_address)
            avm_history = normalize_from_attom_avm_history(pinned_avmhx or avm_history_result.data)
            if avm_history and prop_dict:
                prop_dict["avm_history"] = avm_history
        except Exception as exc:  # noqa: BLE001
            logger.warning("landlord.property_facts: avm_history normalize failed: %s", exc)

    # --- Merge building permits from first-wave result --------------------
    # (was a sequential await; now part of the parallel gather above.)
    if permit_result and permit_result.outcome == Outcome.SUCCESS and permit_result.data:
        try:
            pinned_permit_data = _pin_attom_payload_to_subject(permit_result.data, normalized_address)
            permits_raw = []
            for p in (pinned_permit_data or permit_result.data).get("property", []):
                for pm in p.get("buildingPermits", []):
                    permits_raw.append({
                        "date": pm.get("effectiveDate", ""),
                        "number": pm.get("permitNumber", ""),
                        "status": pm.get("status", ""),
                        "description": pm.get("description", ""),
                        "type": pm.get("type", ""),
                        "job_value": pm.get("jobValue"),
                        "business": pm.get("businessName", ""),
                    })
            if permits_raw and prop_dict:
                prop_dict["permit_signals"] = permits_raw
        except Exception:
            pass  # Permits are enrichment, not critical

    # --- Merge schools from first-wave result -----------------------------
    # (was a sequential await; now part of the parallel gather above.)
    if schools_result and schools_result.outcome == Outcome.SUCCESS and schools_result.data:
        try:
            pinned_schools_data = _pin_attom_payload_to_subject(schools_result.data, normalized_address)
            schools = normalize_from_attom_schools(pinned_schools_data or schools_result.data)
            if schools and prop_dict:
                prop_dict["nearby_schools"] = schools
        except Exception:
            pass  # Schools are nice-to-have, not critical

    # --- W2 second wave: community / salestrend / sales_comparables ----------
    # These ATTOM calls require IDs that only appear AFTER the first-wave
    # detail response: geoIdV4 (community + salestrend) and attomId (comps).
    # We fan them out in parallel here, gated on the IDs being present.
    # Each is non-fatal — failure of one doesn't block the others or the pull.
    geo_id_v4_dict = prop_dict.get("geo_id_v4") or {}
    # Some ATTOM /property/detail responses come back with an empty geoIdV4
    # block — usually for newly-recorded parcels or rural addresses. Without
    # a geoIdV4 we lose community + salestrend cards entirely. Fallback path:
    # if we have lat/lng, resolve the hierarchy via /v4/area/hierarchy/lookup
    # and synthesize the missing keys ourselves. Keeps the Demographics + POI
    # + SalesTrend cards alive on properties with sparse detail responses.
    if not geo_id_v4_dict and prop_dict.get("latitude") and prop_dict.get("longitude"):
        try:
            from aspire_orchestrator.providers.attom_client import (
                execute_attom_area_hierarchy_lookup,
            )
            hl_result = await execute_attom_area_hierarchy_lookup(
                payload={
                    "latitude": prop_dict["latitude"],
                    "longitude": prop_dict["longitude"],
                    "geoType": "All",
                },
                **args,
            )
            if hl_result and hl_result.outcome == Outcome.SUCCESS and hl_result.data:
                # ATTOM Area API V4 hierarchy lookup response shape:
                #   {"geographies": [{"geoIdV4": "...",
                #     "geographyTypeAbbreviation": "ZI", ...}, ...]}
                # We're V4-only — no legacy V2 fallback.
                synthetic = {}
                geographies = hl_result.data.get("geographies", [])
                if isinstance(geographies, dict):
                    geographies = [geographies]
                for g in geographies if isinstance(geographies, list) else []:
                    if not isinstance(g, dict):
                        continue
                    geo_type = str(g.get("geographyTypeAbbreviation") or "").strip()
                    geo_id = str(g.get("geoIdV4") or "").strip()
                    if geo_type and geo_id:
                        synthetic[geo_type] = geo_id

                if synthetic:
                    prop_dict["geo_id_v4"] = synthetic
                    geo_id_v4_dict = synthetic
                    logger.info(
                        "landlord.property_facts: synthesized geoIdV4 via hierarchy lookup (%d keys)",
                        len(synthetic),
                    )
        except Exception as exc:  # noqa: BLE001 — fallback is non-fatal enrichment
            logger.warning("landlord.property_facts: hierarchy lookup failed: %s", exc)

    # ATTOM V4 geoIdV4 keys per docs: ND (broad neighborhood — still
    # populated in V4 responses despite the migration intro splitting it
    # conceptually), N1 (Macro Neighborhood), N2 (Neighborhood), N3
    # (Sub-Neighborhood), N4 (Residential Subdivision), CS (County
    # Subdivision), PL (Place), ZI (Zip Code Tabulation Area). Community
    # API accepts any of these; finer granularity = more relevant data.
    # Fallback chain prefers most-specific to most-general.
    community_geo = (
        geo_id_v4_dict.get("ND")
        or geo_id_v4_dict.get("N2")
        or geo_id_v4_dict.get("N1")
        or geo_id_v4_dict.get("N3")
        or geo_id_v4_dict.get("N4")
        or geo_id_v4_dict.get("CS")
        or geo_id_v4_dict.get("PL")
        or geo_id_v4_dict.get("ZI")
        or ""
    )
    # Salestrend is published at zip level by ATTOM (see /v4/transaction/salestrend
    # docs). Prefer ZI; ND/N1/N2 are accepted but yield smaller samples.
    salestrend_geo = (
        geo_id_v4_dict.get("ZI")
        or geo_id_v4_dict.get("ND")
        or geo_id_v4_dict.get("N2")
        or geo_id_v4_dict.get("N1")
        or ""
    )
    subject_attomid = prop_dict.get("attom_id") or ""

    # Comps fallback geoIdV4 — use N4 (Residential Subdivision) or N3
    # (Sub-Neighborhood) for tightest match. /sale/snapshot accepts geoIdV4
    # so we get all recorded sales in the same boundary as the subject.
    n4_id = (
        geo_id_v4_dict.get("N4")
        or geo_id_v4_dict.get("N3")
        or geo_id_v4_dict.get("N2")
        or ""
    )

    # School district geoIdV4 — type DB (School District Boundary) per
    # ATTOM Area API V4 docs. Used to call /v4/school/district once for
    # the property's district name + rating.
    district_geo = geo_id_v4_dict.get("DB") or ""

    # Per-school geoIdV4s — extracted by the schools normalizer above.
    # Each item has type SB (School Attendance Area). We cap at 6 to
    # bound the per-pull receipt count + parallel fan-out width.
    nearby_schools_for_enrichment = (prop_dict.get("nearby_schools") or [])[:6]
    school_geo_ids = [
        str(sch.get("geo_id_v4") or "").strip()
        for sch in nearby_schools_for_enrichment
        if isinstance(sch, dict) and sch.get("geo_id_v4")
    ]

    second_wave_calls: list[Any] = []
    second_wave_keys: list[str] = []
    if community_geo:
        from aspire_orchestrator.providers.attom_client import (
            execute_attom_community_profile,
        )
        second_wave_calls.append(execute_attom_community_profile(
            payload={"geoIdV4": community_geo}, **args,
        ))
        second_wave_keys.append("community")
    if salestrend_geo:
        from aspire_orchestrator.providers.attom_client import (
            execute_attom_sales_trends,
        )
        second_wave_calls.append(execute_attom_sales_trends(
            payload={"geoIdV4": salestrend_geo, "interval": "monthly"}, **args,
        ))
        second_wave_keys.append("salestrend")
    if subject_attomid:
        from aspire_orchestrator.providers.attom_client import (
            execute_attom_sales_comparables,
        )
        second_wave_calls.append(execute_attom_sales_comparables(
            payload={"attomId": subject_attomid}, **args,
        ))
        second_wave_keys.append("sales_comparables")
    # School enrichment — district + per-school profile. Also runs in
    # parallel with the rest of the second wave so it adds zero latency.
    # The school normalizer pulls name/grade/distance from
    # /property/detailwithschools (already in first wave); /v4/school/profile
    # adds rating + test score, /v4/school/district adds district name +
    # district rating. Skipped silently when no geoIdV4s present.
    if district_geo:
        from aspire_orchestrator.providers.attom_client import (
            execute_attom_school_district,
        )
        second_wave_calls.append(execute_attom_school_district(
            payload={"geoIdV4": district_geo}, **args,
        ))
        second_wave_keys.append("school_district")
    if school_geo_ids:
        from aspire_orchestrator.providers.attom_client import (
            execute_attom_school_profile,
        )
        for sch_geo in school_geo_ids:
            second_wave_calls.append(execute_attom_school_profile(
                payload={"geoIdV4": sch_geo}, **args,
            ))
            second_wave_keys.append(f"school_profile:{sch_geo}")

    # /sale/snapshot fallback — runs in parallel with /salescomparables.
    # When salescomparables fails (no entitlement, no attomid, etc.) the
    # /sale/snapshot results take over so the comps card still renders.
    # ATTOM /sale/snapshot accepts geoIdV4 (preferred — neighborhood-tight)
    # OR address+radius (broader, distance-sorted). For radius searches
    # the docs specify orderby=distance asc as the right sort.
    if n4_id:
        second_wave_calls.append(_attom_request(
            path="/sale/snapshot",
            query_params={
                "geoIdV4": n4_id,
                "minsaleamt": "100000",
                "maxsaleamt": "1500000",
                "propertytype": "SFR",
                "pageSize": "20",
                "orderby": "saleSearchDate desc",
            },
            tool_id="attom.neighborhood_comps",
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
        ))
        second_wave_keys.append("neighborhood_comps")
    elif normalized_address:
        # No geoIdV4 N4 — fall back to address + 0.5 mi radius (max distance
        # for "nearby" neighborhood-equivalent comps). orderby=distance asc
        # so closest comparables surface first.
        addr_parts = normalized_address.split(",", 1)
        addr1 = addr_parts[0].strip() if addr_parts else normalized_address
        addr2 = addr_parts[1].strip() if len(addr_parts) > 1 else ""
        if addr1:
            second_wave_calls.append(_attom_request(
                path="/sale/snapshot",
                query_params={
                    "address1": addr1,
                    "address2": addr2,
                    "radius": "0.5",
                    "minsaleamt": "100000",
                    "maxsaleamt": "1500000",
                    "propertytype": "SFR",
                    "pageSize": "20",
                    "orderby": "distance asc",
                },
                tool_id="attom.radius_comps",
                correlation_id=context.correlation_id,
                suite_id=context.suite_id,
                office_id=context.office_id,
            ))
            second_wave_keys.append("neighborhood_comps")

    if second_wave_calls:
        sw_results = await asyncio.gather(*second_wave_calls, return_exceptions=True)
        sw_by_key = dict(zip(second_wave_keys, sw_results, strict=True))

        community_result = _safe_result(sw_by_key.get("community"))
        if community_result and community_result.outcome == Outcome.SUCCESS and community_result.data:
            try:
                from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
                    normalize_from_attom_community,
                )
                community = normalize_from_attom_community(community_result.data)
                if community and prop_dict:
                    prop_dict["community"] = community
            except Exception as exc:  # noqa: BLE001
                logger.warning("landlord.property_facts: community normalize failed: %s", exc)

        salestrend_result = _safe_result(sw_by_key.get("salestrend"))
        if salestrend_result and salestrend_result.outcome == Outcome.SUCCESS and salestrend_result.data:
            try:
                from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
                    normalize_from_attom_salestrend,
                )
                salestrend = normalize_from_attom_salestrend(salestrend_result.data)
                if salestrend and prop_dict:
                    prop_dict["salestrend"] = salestrend
            except Exception as exc:  # noqa: BLE001
                logger.warning("landlord.property_facts: salestrend normalize failed: %s", exc)

        comps_result = _safe_result(sw_by_key.get("sales_comparables"))
        if comps_result and comps_result.outcome == Outcome.SUCCESS and comps_result.data:
            try:
                from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
                    normalize_from_attom_sales_comparables,
                )
                comps = normalize_from_attom_sales_comparables(comps_result.data)
                if comps and prop_dict:
                    prop_dict["comps"] = comps
            except Exception as exc:  # noqa: BLE001
                logger.warning("landlord.property_facts: sales_comparables normalize failed: %s", exc)

        # /sale/snapshot fallback — only populate comps from this if the
        # primary /salescomparables path didn't return any. The two endpoints
        # surface different things: salescomparables = AVM-matched true
        # comparables; sale/snapshot = any recent sale in the geography.
        # Falling back to sale/snapshot is better than zero comps.
        nb_comps_result = _safe_result(sw_by_key.get("neighborhood_comps"))
        if (
            nb_comps_result
            and nb_comps_result.outcome == Outcome.SUCCESS
            and nb_comps_result.data
            and prop_dict is not None
            and not prop_dict.get("comps")  # only fill if salescomparables came up empty
        ):
            try:
                # Use the same coercion helpers the normalizer uses for the
                # primary comps path so types stay consistent across the two
                # data sources.
                from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
                    _safe_float,
                    _safe_int,
                )

                fallback_comps = []
                subject_attom_id = prop_dict.get("attom_id", "")
                for cp in nb_comps_result.data.get("property", [])[:15]:
                    if not isinstance(cp, dict):
                        continue
                    cp_id = str(cp.get("identifier", {}).get("attomId", "")) if isinstance(cp.get("identifier"), dict) else ""
                    if cp_id and cp_id == subject_attom_id:
                        continue  # skip subject itself
                    cp_addr = cp.get("address", {}).get("oneLine", "") if isinstance(cp.get("address"), dict) else ""
                    cp_sale = cp.get("sale", {}) if isinstance(cp.get("sale"), dict) else {}
                    cp_amt = cp_sale.get("amount", {}) if isinstance(cp_sale.get("amount"), dict) else {}
                    cp_bldg = cp.get("building", {}) if isinstance(cp.get("building"), dict) else {}
                    cp_summary = cp.get("summary", {}) if isinstance(cp.get("summary"), dict) else {}
                    cp_loc = cp.get("location", {}) if isinstance(cp.get("location"), dict) else {}
                    fallback_comps.append({
                        "attom_id": cp_id,
                        "address": cp_addr,
                        "distance_miles": _safe_float(cp_loc.get("distance")),
                        "beds": _safe_int((cp_bldg.get("rooms") or {}).get("beds")),
                        "baths": _safe_float((cp_bldg.get("rooms") or {}).get("bathstotal")),
                        "living_sqft": _safe_int(
                            (cp_bldg.get("size") or {}).get("livingsize")
                            or (cp_bldg.get("size") or {}).get("universalsize")
                        ),
                        "year_built": _safe_int(cp_summary.get("yearbuilt")),
                        "last_sale_date": str(
                            cp_amt.get("salerecdate")
                            or cp_sale.get("saleTransDate", "")
                            or ""
                        ),
                        "last_sale_amount": _safe_float(cp_amt.get("saleamt")),
                        "estimated_value": None,
                    })
                if fallback_comps:
                    prop_dict["comps"] = fallback_comps[:12]
                    logger.info(
                        "landlord.property_facts: comps populated from /sale/snapshot fallback (%d records)",
                        len(fallback_comps),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("landlord.property_facts: neighborhood_comps normalize failed: %s", exc)

        # --- Merge school district + per-school profiles --------------------
        # district adds district_name + district_rating to prop_dict.
        # Each school_profile result merges rating + test_score back into
        # the matching school record in prop_dict["nearby_schools"].
        district_result = _safe_result(sw_by_key.get("school_district"))
        if district_result and district_result.outcome == Outcome.SUCCESS and district_result.data:
            try:
                from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
                    normalize_from_attom_school_district,
                )
                district = normalize_from_attom_school_district(district_result.data)
                if district and prop_dict:
                    if district.get("district_name"):
                        prop_dict["school_district_name"] = district["district_name"]
                    if district.get("district_rating") is not None:
                        prop_dict["school_district_rating"] = district["district_rating"]
                    if district.get("district_grade_range"):
                        prop_dict["school_district_grade_range"] = district["district_grade_range"]
                    if district.get("district_enrollment") is not None:
                        prop_dict["school_district_enrollment"] = district["district_enrollment"]
            except Exception as exc:  # noqa: BLE001
                logger.warning("landlord.property_facts: school_district normalize failed: %s", exc)

        # Per-school profiles — merge by geoIdV4 match into nearby_schools[].
        if prop_dict and isinstance(prop_dict.get("nearby_schools"), list):
            try:
                from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
                    normalize_from_attom_school_profile,
                )
                # Build lookup: geo_id_v4 → enriched fields
                profile_by_geo = {}
                for key, sw_result in sw_by_key.items():
                    if not key.startswith("school_profile:"):
                        continue
                    sch_geo = key.split(":", 1)[1]
                    sp_result = _safe_result(sw_result)
                    if (
                        sp_result
                        and sp_result.outcome == Outcome.SUCCESS
                        and sp_result.data
                    ):
                        profile = normalize_from_attom_school_profile(sp_result.data)
                        if profile:
                            profile_by_geo[sch_geo] = profile

                # Merge into matching school records.
                if profile_by_geo:
                    for sch in prop_dict["nearby_schools"]:
                        if not isinstance(sch, dict):
                            continue
                        geo = sch.get("geo_id_v4")
                        if geo and geo in profile_by_geo:
                            for k, v in profile_by_geo[geo].items():
                                if v not in (None, ""):
                                    sch[k] = v
                    logger.info(
                        "landlord.property_facts: enriched %d schools with ratings",
                        len(profile_by_geo),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("landlord.property_facts: school_profile merge failed: %s", exc)

    # --- Merge foreclosure filings + expanded sale history ----------------
    # (was a sequential await; now part of the parallel gather above.)
    if fc_result and fc_result.outcome == Outcome.SUCCESS and fc_result.data:
        try:
            pinned_fc_data = _pin_attom_payload_to_subject(fc_result.data, normalized_address)
            from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
                normalize_from_attom_foreclosure,
            )
            fc_data = normalize_from_attom_foreclosure(pinned_fc_data or fc_result.data)
            if fc_data and prop_dict:
                prop_dict["foreclosure_records"] = fc_data.get("foreclosure_records", [])
                prop_dict["prior_foreclosure"] = fc_data.get("prior_foreclosure", False)
                prop_dict["foreclosure_stage"] = fc_data.get("foreclosure_stage", "none")
                # Merge expanded sale history if we don't have it yet
                if not prop_dict.get("sale_history") and fc_data.get("sale_history_expanded"):
                    prop_dict["sale_history"] = fc_data["sale_history_expanded"]
        except Exception as exc:
            logger.warning("landlord.property_facts: foreclosure normalize failed: %s", exc)

    # --- Merge active preforeclosure filing (W1c) ----------------------------
    # Only call /property/v3/preforeclosuredetails when there's a flag worth
    # investigating: prior_foreclosure, an active stage, or REO. Skipping when
    # the property is clean keeps the per-pull receipt count low for the 95%
    # of properties that have no distress signal.
    needs_preforeclosure = bool(
        prop_dict.get("prior_foreclosure")
        or (prop_dict.get("foreclosure_stage") and prop_dict.get("foreclosure_stage") != "none")
        or prop_dict.get("reo_flag")
        or prop_dict.get("in_foreclosure")
    )
    if needs_preforeclosure:
        try:
            from aspire_orchestrator.providers.attom_client import (
                execute_attom_preforeclosure_details,
            )
            from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
                normalize_from_attom_preforeclosure,
            )
            pf_result = await execute_attom_preforeclosure_details(payload=attom_payload, **args)
            if pf_result and pf_result.outcome == Outcome.SUCCESS and pf_result.data:
                pinned_pf_data = _pin_attom_payload_to_subject(pf_result.data, normalized_address)
                filing = normalize_from_attom_preforeclosure(pinned_pf_data or pf_result.data)
                if filing and prop_dict:
                    prop_dict["foreclosure_filing"] = filing
        except Exception as exc:  # noqa: BLE001 — non-fatal enrichment
            logger.warning("landlord.property_facts: preforeclosure_details failed: %s", exc)

    if prop_dict:
        # Set property_value = tax market value (county official) as the default.
        # AVM is an algorithm estimate — tax assessment is authoritative.
        tax_mv = prop_dict.get("tax_market_value")
        avm_v = prop_dict.get("estimated_value")
        prop_dict["property_value"] = tax_mv or avm_v
        prop_dict["property_value_source"] = "county_tax_assessment" if tax_mv else "avm_estimate"

        # Deterministic primary path: /expandedprofile delivers unit-level data
        # directly when the parsed address1 + unitnumber resolve to a unit. The
        # legacy `assert_unit_data_complete → artifact_type=error` fallback was
        # removed — if expandedprofile cannot find unit-level records the empty
        # PropertyFactPack flows through verify_records and surfaces the
        # missing fields naturally, no degraded artifact_type=error path.

        # --- Merge Apify Zillow photos (Estimate Studio Visuals tab) ---
        # ATTOM has no photos; Zillow scraped via Apify provides
        # interior/exterior/roof lanes for the contractor's visual brief.
        # Failure is non-fatal — facts still flow, photo lanes go empty.
        try:
            if (
                apify_result
                and apify_result.outcome == Outcome.SUCCESS
                and apify_result.data
                and isinstance(apify_result.data, dict)
            ):
                photos_raw = apify_result.data.get("photos") or []
                if isinstance(photos_raw, list) and photos_raw:
                    prop_dict["photos"] = [
                        {
                            "url": (p.get("url") or "").strip(),
                            "caption": p.get("caption"),
                            "lane": (p.get("lane") or "uncategorized").strip().lower(),
                        }
                        for p in photos_raw
                        if isinstance(p, dict) and p.get("url")
                    ]
                    listing_url = apify_result.data.get("listing_url")
                    if listing_url:
                        prop_dict["zillow_listing_url"] = listing_url
                    logger.info(
                        "landlord.property_facts: Apify Zillow merged %d photos",
                        len(prop_dict["photos"]),
                        extra={"correlation_id": context.correlation_id},
                    )
                else:
                    logger.info(
                        "landlord.property_facts: Apify Zillow returned 0 photos",
                        extra={"correlation_id": context.correlation_id},
                    )
            else:
                logger.warning(
                    "landlord.property_facts: Apify Zillow unavailable — facts flow without photos",
                    extra={"correlation_id": context.correlation_id},
                )
        except Exception as merge_err:  # noqa: BLE001 — merge must never break facts
            logger.warning(
                "landlord.property_facts: Apify merge failed (non-fatal): %s",
                str(merge_err)[:160],
                extra={"correlation_id": context.correlation_id},
            )

        # Append the canonical record once for verification. The card-per-
        # section fanout happens AFTER verify_records to avoid inflating the
        # conflict-detector with duplicated rows that differ only in their
        # `_cardSection` metadata.
        records.append(prop_dict)

    # Verify on the single canonical record(s) BEFORE the section fanout below.
    # Running the verifier post-fanout would treat the per-section copies as
    # multiple records with conflicting `_cardSection` values, dropping the
    # confidence score for purely cosmetic reasons.
    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["normalized_address", "living_sqft", "year_built", "owner_name"],
    )

    # ── Card-per-section dispatch (W0) ────────────────────────────────────────
    # Now expand the canonical record into one card per UI section.
    if prop_dict:
        section_records = fan_out_property_sections(prop_dict)
        # Replace the single canonical record with the per-section fanout.
        # records[] has only the prop_dict at this point (lines above).
        records.clear()
        records.extend(section_records)

    next_queries: list[str] = []
    if "normalized_address" in report.missing_fields:
        next_queries.append(f"Verify address with county assessor: {query}")
    if not records:
        next_queries.append(f"Manual ATTOM lookup: {query}")
    else:
        next_queries.extend([
            "Pull nearby sales comparables",
            "Check rental demand in this ZIP",
        ])

    return ResearchResponse(
        artifact_type="LandlordPropertyPack",
        summary=(
            f"Property facts for '{normalized_address}': "
            f"Verification: {report.status} (score={report.confidence_score}). "
            f"Missing: {report.missing_fields or 'none'}."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "attom"},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=next_queries,
        verification_report=report,
        segment="landlord",
        intent="property_facts",
        playbook="landlord.property_facts",
        providers_called=list(dict.fromkeys(providers_called)),
    )


# ---------------------------------------------------------------------------
# 2. Rent Comp Context
# ---------------------------------------------------------------------------

async def execute_rent_comp_context(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Rental AVM + sales comparables for rent pricing context.

    Provider plan:
      here:   geocode address
      attom:  rental_avm + sales_comparables (parallel)
      google_places: neighborhood context

    GUARDRAIL: If rental_avm returns no data, do NOT manufacture a price target.
    Return missing_fields=["estimated_rent"] with corrective next_queries.

    required_fields: normalized_address, estimated_rent
    Returns: RentCompPack artifact.
    """
    logger.info(
        "landlord.rent_comp_context start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # Step 1: Normalize address
    normalized_address = await _geocode_address(query, context)
    providers_called.append("here")

    attom_payload = {"address": normalized_address}

    # Step 2: Parallel ATTOM calls
    _raw_rent = await asyncio.gather(
        execute_attom_rental_avm(payload=attom_payload, **args),
        execute_attom_sales_comparables(
            payload={**attom_payload, "searchtype": "rental", "miles": "1"},
            **args,
        ),
        return_exceptions=True,
    )
    rental_result = _safe_result(_raw_rent[0])
    comps_result = _safe_result(_raw_rent[1])
    providers_called.extend(["attom", "attom"])

    rental_record: dict[str, Any] = {"normalized_address": normalized_address}
    has_rental_data = False

    if rental_result and rental_result.outcome == Outcome.SUCCESS and rental_result.data:
        rental = normalize_from_attom_rental(rental_result.data)
        if rental.get("estimated_rent") is not None:
            has_rental_data = True
        rental_record.update(rental)
        sources.append(_source("attom"))
    else:
        logger.warning(
            "landlord.rent_comp_context: attom rental_avm failed: %s",
            rental_result.error if rental_result else "provider raised exception",
            extra={"correlation_id": context.correlation_id},
        )

    if comps_result and comps_result.outcome == Outcome.SUCCESS and comps_result.data:
        comps_props = comps_result.data.get("property", [])
        rental_record["sales_comparables"] = comps_props[:10]
    else:
        logger.warning(
            "landlord.rent_comp_context: attom sales_comparables failed: %s",
            comps_result.error if comps_result else "provider raised exception",
            extra={"correlation_id": context.correlation_id},
        )

    records.append(rental_record)

    # Step 3: Google Places — neighborhood context
    gp_result = await execute_google_places_search(
        payload={"query": f"neighborhood rental market {normalized_address}"},
        **args,
    )
    providers_called.append("google_places")
    if gp_result.outcome == Outcome.SUCCESS:
        for raw in (gp_result.data or {}).get("results", [])[:3]:
            biz = normalize_from_google_places(raw)
            records.append(biz.to_dict())
        sources.append(_source("google_places"))

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["normalized_address", "estimated_rent"],
    )

    # Guardrail: surface missing rental data clearly
    guardrail_note = ""
    if not has_rental_data:
        guardrail_note = (
            " GUARDRAIL: No rental valuation available — do not use as rent target."
        )

    next_queries: list[str] = []
    if not has_rental_data:
        next_queries.extend([
            f"Zillow rental estimate: {normalized_address}",
            f"Rentometer comparable rents: {normalized_address}",
        ])
    else:
        next_queries.extend([
            "Get full property detail",
            "Check neighborhood demand trends",
        ])

    return ResearchResponse(
        artifact_type="RentCompPack",
        summary=(
            f"Rent comp context for '{normalized_address}': "
            f"Rental data available: {has_rental_data}. "
            f"Verification: {report.status}.{guardrail_note}"
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "attom+google_places"},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=next_queries,
        verification_report=report,
        segment="landlord",
        intent="rent_comp_context",
        playbook="landlord.rent_comp_context",
        providers_called=list(dict.fromkeys(providers_called)),
    )


# ---------------------------------------------------------------------------
# 3. Permit and Renovation
# ---------------------------------------------------------------------------

async def execute_permit_and_renovation(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Research permit history and renovation signals for a property.

    Provider plan:
      here:   geocode address
      attom:  property_detail (for permit signals in building.construction fields)
      exa:    web permit evidence (county portals, news)
      brave:  fallback web evidence

    Returns: PermitContextPack artifact.
    """
    logger.info(
        "landlord.permit_and_renovation start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    exa_grounding: list[dict[str, Any]] = []

    # Step 1: Normalize address
    normalized_address = await _geocode_address(query, context)
    providers_called.append("here")

    # Step 2: ATTOM property detail for permit/construction signals
    detail_result = await execute_attom_property_detail(
        payload={"address": normalized_address},
        **args,
    )
    providers_called.append("attom")
    if detail_result.outcome == Outcome.SUCCESS and detail_result.data:
        prop = normalize_from_attom_detail(detail_result.data)
        prop_dict = prop.to_dict()
        # Surface permit-relevant construction fields prominently
        prop_dict["permit_context"] = {
            "year_built": prop_dict.get("year_built"),
            "construction_frame": prop_dict.get("construction_frame"),
            "roof_cover": prop_dict.get("roof_cover"),
            "quality": prop_dict.get("quality"),
            "permit_signals": prop_dict.get("permit_signals", []),
        }
        records.append(prop_dict)
        sources.append(_source("attom"))
    else:
        logger.warning(
            "landlord.permit_and_renovation: attom detail failed: %s",
            detail_result.error,
            extra={"correlation_id": context.correlation_id},
        )

    # Step 3: Exa for web permit evidence
    exa_result = await execute_exa_search(
        payload={
            "query": f"{normalized_address} building permit renovation construction",
            "num_results": 8,
            "moderation": True,
            "contents": {"text": True, "highlights": True},
        },
        **args,
    )
    providers_called.append("exa")
    if exa_result.outcome == Outcome.SUCCESS:
        for item in (exa_result.data or {}).get("results", []):
            ev = normalize_from_exa(item, grounding=item.get("grounding"))
            records.append(ev.to_dict())
            if item.get("grounding"):
                exa_grounding.append(item["grounding"])
        sources.append(_source("exa"))
    else:
        # Brave fallback
        brave_result = await execute_brave_search(
            payload={
                "query": f"building permits renovation {normalized_address}",
                "count": 5,
            },
            **args,
        )
        providers_called.append("brave")
        if brave_result.outcome == Outcome.SUCCESS:
            for item in (brave_result.data or {}).get("results", [])[:5]:
                ev = normalize_from_brave(item)
                records.append(ev.to_dict())
            sources.append(_source("brave"))

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["normalized_address"],
        exa_grounding=exa_grounding or None,
    )

    return ResearchResponse(
        artifact_type="PermitContextPack",
        summary=(
            f"Permit & renovation context for '{normalized_address}': "
            f"{len(records)} signals. Verification: {report.status}."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "attom+exa"},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=[
            f"{normalized_address} county building department permit search",
            "Find renovation contractors",
        ],
        verification_report=report,
        segment="landlord",
        intent="permit_and_renovation",
        playbook="landlord.permit_and_renovation",
        providers_called=list(dict.fromkeys(providers_called)),
    )


# ---------------------------------------------------------------------------
# 4. Neighborhood Demand
# ---------------------------------------------------------------------------

async def execute_neighborhood_demand(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Multi-factor neighborhood demand analysis.

    Provider plan:
      attom:    sales_trends for geographic trends (geoid from provider_plan or ZIP regex)
      google_places: rental/housing competitor density
      exa:      rental demand intelligence (news category)
      parallel: multi-factor demand analysis if ADAM_PROVIDER_PARALLEL_V1 flag enabled

    Returns: NeighborhoodDemandBrief artifact.
    """
    logger.info(
        "landlord.neighborhood_demand start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    from aspire_orchestrator.services.adam.feature_flags import (
        ADAM_PROVIDER_PARALLEL_V1,
        is_adam_flag_enabled,
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    exa_grounding: list[dict[str, Any]] = []

    # Extract ZIP from provider_plan (preferred) or query (fallback heuristic)
    zip_match = re.search(r"\b(\d{5})\b", query)
    geoid: str = (provider_plan or {}).get("geoid", zip_match.group(1) if zip_match else "")

    # Step 1: ATTOM sales trends (requires valid geoid)
    if geoid:
        trends_result = await execute_attom_sales_trends(
            payload={"geoid": geoid, "geo_type": "ZI", "interval": "monthly"},
            **args,
        )
        providers_called.append("attom")
        if trends_result.outcome == Outcome.SUCCESS and trends_result.data:
            records.append({
                "source_type": "sales_trends",
                "geoid": geoid,
                "data": trends_result.data.get("trendStat", trends_result.data),
                "provider": "attom",
            })
            sources.append(_source("attom"))
        else:
            logger.warning(
                "landlord.neighborhood_demand: attom sales_trends failed: %s",
                trends_result.error,
                extra={"correlation_id": context.correlation_id},
            )
    else:
        logger.info(
            "landlord.neighborhood_demand: no ZIP in query — skipping attom sales_trends",
            extra={"correlation_id": context.correlation_id},
        )

    # Step 2: Google Places — rental/housing competitor density
    gp_result = await execute_google_places_search(
        payload={"query": f"rental properties property management {query}"},
        **args,
    )
    providers_called.append("google_places")
    if gp_result.outcome == Outcome.SUCCESS:
        raw_results = (gp_result.data or {}).get("results", [])
        for raw in raw_results[:8]:
            biz = normalize_from_google_places(raw)
            records.append(biz.to_dict())
        sources.append(_source("google_places"))
        records.append({
            "source_type": "competitor_density",
            "rental_listings_nearby": len(raw_results),
            "provider": "google_places",
        })

    # Step 3: Exa — rental demand intelligence
    exa_result = await execute_exa_search(
        payload={
            "query": f"rental demand market analysis {query}",
            "type": "deep-lite",
            "category": "news",
            "num_results": 5,
            "moderation": True,
        },
        **args,
    )
    providers_called.append("exa")
    if exa_result.outcome == Outcome.SUCCESS:
        for item in (exa_result.data or {}).get("results", [])[:5]:
            ev = normalize_from_exa(item, grounding=item.get("grounding"))
            records.append(ev.to_dict())
            if item.get("grounding"):
                exa_grounding.append(item["grounding"])
        sources.append(_source("exa"))

    # Step 4: Parallel multi-factor demand analysis (feature-flagged)
    parallel_enabled = await is_adam_flag_enabled(
        ADAM_PROVIDER_PARALLEL_V1, context.tenant_id
    )
    if parallel_enabled:
        try:
            from aspire_orchestrator.providers.parallel_client import execute_parallel_task

            parallel_result = await execute_parallel_task(
                payload={
                    "task": (
                        f"Analyze neighborhood rental demand for: {query}. "
                        "Provide: demand score (1-10), key demand drivers, "
                        "vacancy trend, renter demographic summary."
                    ),
                    "context": {
                        "zip": geoid,
                        "query": query,
                        "records_collected": len(records),
                    },
                },
                **args,
            )
            providers_called.append("parallel")
            if parallel_result.outcome == Outcome.SUCCESS:
                records.append({
                    "source_type": "parallel_demand_analysis",
                    "analysis": parallel_result.data,
                    "provider": "parallel",
                })
                sources.append(_source("parallel"))
        except Exception as exc:
            logger.warning(
                "landlord.neighborhood_demand: parallel task failed: %s",
                exc,
                extra={"correlation_id": context.correlation_id},
            )

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["data"] if geoid else [],
        exa_grounding=exa_grounding or None,
    )

    next_queries: list[str] = []
    if not geoid:
        next_queries.append(f"Provide ZIP code for precise sales trend data: {query}")
    next_queries.extend([
        "Drill into specific ZIP",
        "Compare adjacent neighborhoods",
    ])

    return ResearchResponse(
        artifact_type="NeighborhoodDemandBrief",
        summary=(
            f"Neighborhood demand for '{query}' (ZIP={geoid or 'unknown'}): "
            f"{len(records)} data points. Verification: {report.status}."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "+".join(dict.fromkeys(providers_called))},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=next_queries,
        verification_report=report,
        segment="landlord",
        intent="neighborhood_demand",
        playbook="landlord.neighborhood_demand",
        providers_called=list(dict.fromkeys(providers_called)),
    )


# ---------------------------------------------------------------------------
# 5. Screening Compliance
# ---------------------------------------------------------------------------

async def execute_screening_compliance(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Research tenant screening laws and compliance requirements.

    Provider plan:
      exa: official jurisdiction sources (hud.gov, state AG sites, fair housing orgs)
      brave: fallback for broader compliance coverage

    GUARDRAIL: This playbook NEVER produces tenant scoring, accept/reject
    recommendations, or any judgment about an individual applicant.
    It returns compliance-information only — landlord education.

    Returns: ScreeningComplianceBrief artifact.
    """
    logger.info(
        "landlord.screening_compliance start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    COMPLIANCE_DOMAINS = [
        "hud.gov",
        "justice.gov",
        "ftc.gov",
        "consumerfinance.gov",
        "eeoc.gov",
        "ncsl.org",
        "nolo.com",
        "avail.co",
        "apartments.com",
        "landlordology.com",
    ]

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    exa_grounding: list[dict[str, Any]] = []

    # --- Exa: official compliance sources with domain filtering ---
    exa_result = await execute_exa_search(
        payload={
            "query": f"tenant screening fair housing compliance {query}",
            "type": "auto",
            "category": "news",
            "num_results": 8,
            "include_domains": COMPLIANCE_DOMAINS,
            "moderation": True,
            "contents": {
                "text": {"maxCharacters": 2000},
                "highlights": {"maxCharacters": 1000},
            },
        },
        **args,
    )
    providers_called.append("exa")
    if exa_result.outcome == Outcome.SUCCESS:
        for item in (exa_result.data or {}).get("results", [])[:8]:
            ev = normalize_from_exa(item, grounding=item.get("grounding"))
            records.append(ev.to_dict())
            if item.get("grounding"):
                exa_grounding.append(item["grounding"])
        sources.append(_source("exa"))
    else:
        logger.warning(
            "landlord.screening_compliance: exa failed, falling back to brave: %s",
            exa_result.error,
            extra={"correlation_id": context.correlation_id},
        )

    # --- Brave fallback — always run if Exa returned fewer than 3 results ---
    if len(records) < 3:
        brave_result = await execute_brave_search(
            payload={
                "query": f"tenant screening rules fair housing {query}",
                "count": 5,
            },
            **args,
        )
        providers_called.append("brave")
        if brave_result.outcome == Outcome.SUCCESS:
            for item in (brave_result.data or {}).get("results", [])[:5]:
                ev = normalize_from_brave(item)
                records.append(ev.to_dict())
            sources.append(_source("brave"))

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["url", "title"],
        exa_grounding=exa_grounding or None,
    )

    # Guardrail note is explicit and always present
    guardrail = (
        "GUARDRAIL: No tenant scoring or accept/reject recommendations produced. "
        "Consult licensed legal counsel for jurisdiction-specific advice."
    )

    return ResearchResponse(
        artifact_type="ScreeningComplianceBrief",
        summary=(
            f"Tenant screening compliance for '{query}': {len(records)} compliance "
            f"sources. Verification: {report.status}. {guardrail}"
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "exa+brave"},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=[
            guardrail,
            "Check state-specific screening rules",
            f"Review fair housing requirements: {query}",
        ],
        verification_report=report,
        segment="landlord",
        intent="screening_compliance",
        playbook="landlord.screening_compliance",
        providers_called=list(dict.fromkeys(providers_called)),
    )


# ---------------------------------------------------------------------------
# 6. Turnover Vendor Scout
# ---------------------------------------------------------------------------

async def execute_turnover_vendor_scout(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Discover make-ready vendors for property turnover.

    Provider plan:
      google_places: primary — cleaners, painters, flooring, handyman near property
      foursquare:    corroboration + contact enrichment
      here:          fallback when vendor list is sparse (<5 records)

    Returns: VendorShortlist artifact.
    """
    logger.info(
        "landlord.turnover_vendor_scout start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    VENDOR_CATEGORIES = [
        "cleaning service",
        "house painter",
        "flooring contractor",
        "handyman",
        "property maintenance",
    ]

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # Step 1: Google Places — vendor search per category (parallel)
    async def _search_gp_category(category: str) -> list[dict[str, Any]]:
        result = await execute_google_places_search(
            payload={"query": f"{category} near {query}"},
            **args,
        )
        if result.outcome == Outcome.SUCCESS:
            return (result.data or {}).get("results", [])
        return []

    gp_results_per_cat = await asyncio.gather(
        *[_search_gp_category(cat) for cat in VENDOR_CATEGORIES],
        return_exceptions=True,
    )
    providers_called.append("google_places")

    seen_names: set[str] = set()
    for cat_results in gp_results_per_cat:
        if isinstance(cat_results, BaseException):
            logger.warning("vendor scout category search raised: %s", cat_results)
            continue
        for raw in cat_results[:3]:
            biz = normalize_from_google_places(raw)
            if biz.name and biz.name not in seen_names:
                seen_names.add(biz.name)
                records.append(biz.to_dict())

    if records:
        sources.append(_source("google_places"))

    # Step 2: Foursquare — corroboration + contact enrichment
    fsq_result = await execute_foursquare_search(
        payload={"query": f"home services contractors {query}"},
        **args,
    )
    providers_called.append("foursquare")
    if fsq_result.outcome == Outcome.SUCCESS:
        for raw in (fsq_result.data or {}).get("results", [])[:10]:
            biz = normalize_from_foursquare(raw)
            if biz.name and biz.name not in seen_names:
                seen_names.add(biz.name)
                records.append(biz.to_dict())
        sources.append(_source("foursquare"))
    else:
        logger.warning(
            "landlord.turnover_vendor_scout: foursquare failed: %s",
            fsq_result.error,
            extra={"correlation_id": context.correlation_id},
        )

    # Step 3: HERE fallback — only if vendor list is thin
    if len(records) < 5:
        here_result = await execute_here_search(
            payload={"query": f"home improvement contractors {query}", "limit": 10},
            **args,
        )
        providers_called.append("here")
        if here_result.outcome == Outcome.SUCCESS:
            for raw in (here_result.data or {}).get("items", []):
                biz = normalize_from_here(raw)
                if biz.name and biz.name not in seen_names:
                    seen_names.add(biz.name)
                    records.append(biz.to_dict())
            sources.append(_source("here"))

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["name", "phone"],
    )

    next_queries: list[str] = []
    if len(records) < 3:
        next_queries.append(f"Angi / HomeAdvisor vendors near {query}")
    next_queries.extend([
        "Verify licensing and insurance",
        "Get quotes from top vendors",
    ])

    return ResearchResponse(
        artifact_type="VendorShortlist",
        summary=(
            f"Turnover vendors near '{query}': {len(records)} vendors found. "
            f"Verification: {report.status}."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "+".join(dict.fromkeys(providers_called))},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=next_queries,
        verification_report=report,
        segment="landlord",
        intent="turnover_vendor_scout",
        playbook="landlord.turnover_vendor_scout",
        providers_called=list(dict.fromkeys(providers_called)),
    )


# ---------------------------------------------------------------------------
# 7. Investment Opportunity Scan
# ---------------------------------------------------------------------------

async def execute_investment_opportunity_scan(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Scan a ZIP for investment opportunities: foreclosures, absentee owners,
    below-market sales, equity spreads, and distressed properties.

    Provider plan:
      attom:  property/snapshot (absentee) + allevents/detail (FC flags) +
              salestrend/snapshot (market context)
              Then per-property deep dives: saleshistory/expandedhistory
              (foreclosure filings + auction dates), homeequity (equity spread),
              attomavm/detail (current value)

    Returns: InvestmentOpportunityPack artifact with ranked opportunities.
    """
    logger.info(
        "landlord.investment_opportunity_scan start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = ["attom"]

    # Location handling: ATTOM v1 property endpoints REQUIRE a 5-digit
    # postalcode (verified live 2026-05-04: city+state params return 400
    # INVALID PARAMETERS on /property/snapshot, /allevents/detail, etc.).
    # When the user gives a city-only query ("auctions in Forest Park GA",
    # "Atlanta GA"), we cannot run the ATTOM scan — but we CAN still run
    # the Exa web search with the city+state location signal so the user
    # gets Auction.com + county-site listings instead of an empty card.
    # May 4 user report: "Adam said couldn't find any" was the playbook
    # bailing out before Exa could fire.
    import re as _re_zip
    zip_match = _re_zip.search(r"\b(\d{5})\b", query)
    zip_code = zip_match.group(1) if zip_match else ""
    location_label = _extract_location_label(query)

    # If we have neither a zip nor any usable location signal, ask the user.
    if not zip_code and not location_label:
        return ResearchResponse(
            artifact_type="InvestmentOpportunityPack",
            summary=(
                "I need a location for the auction scan — "
                "give me a city + state or a 5-digit ZIP."
            ),
            records=[],
            sources=[],
            freshness={"mode": "live"},
            confidence={"status": "needs_input", "score": 0},
            missing_fields=["zip_code", "city_state"],
            next_queries=[
                "Try: 'auctions in 30297'",
                "Try: 'auctions in Forest Park GA'",
            ],
            segment="landlord",
            intent="investment_opportunity_scan",
            playbook="landlord.investment_opportunity_scan",
            providers_called=[],
            extra={"raw_query": query[:120]},
        )

    # If no ZIP, try to resolve city → geoIdV4 so ATTOM can scan by place.
    # Verified live 2026-05-04: ATTOM /v4/location/lookup + /sale/snapshot
    # with geoIdV4 work on the user's account for city-level investment
    # scans. May 4 user request: "houses for auction in Forest Park GA"
    # and "Atlanta GA" should hit ATTOM directly, not just Exa.
    geo_id_v4 = ""
    if not zip_code:
        city, state = _split_city_state(query)
        if city and state:
            try:
                geo_id_v4 = await _resolve_city_to_geoid(city, state, context)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "investment_scan: location lookup failed for %s/%s: %s",
                    city, state, exc,
                )
                geo_id_v4 = ""

    has_attom_scope = bool(zip_code) or bool(geo_id_v4)
    logger.info(
        "investment_scan: zip=%r geoIdV4=%r location_label=%r attom_scope=%s",
        zip_code, geo_id_v4, location_label, has_attom_scope,
    )

    from aspire_orchestrator.providers.attom_client import (
        execute_attom_sales_expanded_history,
        execute_attom_home_equity,
        _attom_request,
    )

    # Step 1: Parallel scans — absentee owners + ALL FC-flagged + recent activity + live auctions
    # Use startsalesearchdate=last 18 months to catch current foreclosure pipeline
    from datetime import datetime, timedelta
    cutoff_date = (datetime.now() - timedelta(days=540)).strftime("%Y/%m/%d")

    # Build the parallel call list. ATTOM scope only when we have a
    # postalcode; Exa always runs (with whatever location signal we have).
    # Exa search query uses zip when available, else "<City> <ST>" — Exa
    # finds Auction.com listings, county sheriff sale schedules, and
    # AuctionZip results just fine on either.
    exa_location = zip_code or location_label
    exa_payload = {
        "query": (
            f"foreclosure auction listings {exa_location} 2026 "
            f"upcoming auction date property sale "
            f"site:auction.com OR site:auctionzip.com OR site:hubzu.com"
        ),
    }

    if zip_code:
        # ZIP scope: postalcode-based ATTOM scan (richest signal — absentee
        # owners + FC events + recent FC filings + trends) plus Exa.
        _raw_inv = await asyncio.gather(
            _attom_request(
                path="/property/snapshot",
                query_params={
                    "postalcode": zip_code, "propertytype": "SFR",
                    "absenteeowner": "absentee", "pagesize": "50",
                },
                tool_id="attom.investment_absentee",
                correlation_id=context.correlation_id,
                suite_id=context.suite_id,
                office_id=context.office_id,
            ),
            _attom_request(
                path="/allevents/detail",
                query_params={
                    "postalcode": zip_code, "propertytype": "SFR", "pagesize": "50",
                },
                tool_id="attom.investment_events",
                correlation_id=context.correlation_id,
                suite_id=context.suite_id,
                office_id=context.office_id,
            ),
            # Recent activity scan — catches properties with new FC filings
            _attom_request(
                path="/allevents/detail",
                query_params={
                    "postalcode": zip_code, "propertytype": "SFR",
                    "startsalesearchdate": cutoff_date,
                    "pagesize": "50",
                    "orderby": "salesearchdate desc",
                },
                tool_id="attom.investment_recent",
                correlation_id=context.correlation_id,
                suite_id=context.suite_id,
                office_id=context.office_id,
            ),
            execute_attom_sales_trends(
                payload={"postalcode": zip_code},
                **args,
            ),
            # Exa: Live auction listings from Auction.com + county sites
            execute_exa_search(payload=exa_payload, **args),
            return_exceptions=True,
        )
        absentee_result = _safe_result(_raw_inv[0])
        fc_events_result = _safe_result(_raw_inv[1])
        recent_events_result = _safe_result(_raw_inv[2])
        trends_result = _safe_result(_raw_inv[3])
        exa_result = _safe_result(_raw_inv[4])
    elif geo_id_v4:
        # City scope (verified live 2026-05-04): /sale/snapshot accepts
        # geoIdV4 and returns recent property sales for the whole place
        # (e.g. Atlanta GA = 1,715 sales in last 4 months). FC-flagged
        # subset is surfaced via the salestranstype filter on the result.
        # Absentee/FC events endpoints don't accept geoIdV4 cleanly on this
        # tier so we skip them on city scope; per-property auction data
        # is fetched in the deep-dive section via /preforeclosuredetails.
        from aspire_orchestrator.providers.attom_client import (
            execute_attom_sale_snapshot_geoid,
        )
        _raw_city = await asyncio.gather(
            execute_attom_sale_snapshot_geoid(
                payload={
                    "geoIdV4": geo_id_v4,
                    "start_date": cutoff_date,
                    "pagesize": 50,
                    "orderby": "salesearchdate desc",
                },
                **args,
            ),
            execute_exa_search(payload=exa_payload, **args),
            return_exceptions=True,
        )
        sale_result = _safe_result(_raw_city[0])
        exa_result = _safe_result(_raw_city[1])
        # Repackage sale results into the same shape the FC parser expects
        # (sale_result.data.property[]) — code below already handles it
        # because /sale/snapshot returns the same property[] structure as
        # /allevents/detail.
        absentee_result = None
        fc_events_result = sale_result
        recent_events_result = sale_result
        trends_result = None
        logger.info(
            "investment_scan: city scope geoIdV4=%s sale_count=%s",
            geo_id_v4,
            sale_result.data.get("status", {}).get("total", 0)
            if sale_result and sale_result.data else 0,
        )
    else:
        # Truly no location signal — Exa only.
        logger.info(
            "investment_scan: no zip and no resolvable city — Exa-only for %r",
            location_label,
        )
        exa_only = await execute_exa_search(payload=exa_payload, **args)
        absentee_result = None
        fc_events_result = None
        recent_events_result = None
        trends_result = None
        exa_result = _safe_result(exa_only)
        providers_called.remove("attom") if "attom" in providers_called else None
    providers_called.append("exa")

    # Parse absentee owners
    absentee_addrs: list[dict[str, str]] = []
    absentee_count = 0
    if absentee_result and absentee_result.outcome == Outcome.SUCCESS and absentee_result.data:
        absentee_count = absentee_result.data.get("status", {}).get("total", 0)
        for p in absentee_result.data.get("property", []):
            addr = p.get("address", {})
            absentee_addrs.append({
                "line1": addr.get("line1", ""),
                "line2": addr.get("line2", ""),
                "oneLine": addr.get("oneLine", ""),
            })

    # Parse foreclosure-flagged properties from allevents
    fc_flagged: list[dict[str, Any]] = []
    if fc_events_result and fc_events_result.outcome == Outcome.SUCCESS and fc_events_result.data:
        for p in fc_events_result.data.get("property", []):
            sale = p.get("sale", {})
            fc = sale.get("foreclosure", "")
            trans = sale.get("amount", {}).get("saletranstype", "")
            addr = p.get("address", {})
            avm = p.get("avm", {}).get("amount", {})
            sale_amt = sale.get("amount", {}).get("saleamt")
            summary = p.get("summary", {})

            is_distressed = bool(fc) or "foreclos" in str(trans).lower() or \
                "reo" in str(trans).lower() or "sheriff" in str(trans).lower()

            if is_distressed:
                fc_flagged.append({
                    "line1": addr.get("line1", ""),
                    "line2": addr.get("line2", ""),
                    "oneLine": addr.get("oneLine", ""),
                    "foreclosure_flag": fc,
                    "trans_type": trans,
                    "last_sale_amt": sale_amt,
                    "avm_value": avm.get("value"),
                    "avm_confidence": avm.get("scr"),
                    "year_built": summary.get("yearbuilt"),
                    "absentee": summary.get("absenteeInd", ""),
                })

    # Merge recently-active FC-flagged properties (may overlap with above)
    seen_addrs = {fp.get("oneLine", "") for fp in fc_flagged}
    if recent_events_result and recent_events_result.outcome == Outcome.SUCCESS and recent_events_result.data:
        for p in recent_events_result.data.get("property", []):
            sale = p.get("sale", {})
            fc = sale.get("foreclosure", "")
            trans = sale.get("amount", {}).get("saletranstype", "")
            addr = p.get("address", {})
            one_line = addr.get("oneLine", "")
            if one_line in seen_addrs:
                continue
            is_distressed = bool(fc) or "foreclos" in str(trans).lower() or \
                "reo" in str(trans).lower() or "sheriff" in str(trans).lower()
            if is_distressed:
                avm = p.get("avm", {}).get("amount", {})
                summary = p.get("summary", {})
                fc_flagged.append({
                    "line1": addr.get("line1", ""),
                    "line2": addr.get("line2", ""),
                    "oneLine": one_line,
                    "foreclosure_flag": fc,
                    "trans_type": trans,
                    "last_sale_amt": sale.get("amount", {}).get("saleamt"),
                    "avm_value": avm.get("value"),
                    "avm_confidence": avm.get("scr"),
                    "year_built": summary.get("yearbuilt"),
                    "absentee": summary.get("absenteeInd", ""),
                    "recent_activity": True,
                })
                seen_addrs.add(one_line)

    # Parse Exa live auction listings (Auction.com, county sites)
    live_auction_listings: list[dict[str, Any]] = []
    if exa_result and exa_result.outcome == Outcome.SUCCESS and exa_result.data:
        for result_item in (exa_result.data.get("results", []))[:10]:
            url = result_item.get("url", "")
            title = result_item.get("title", "")
            snippet = result_item.get("text", result_item.get("highlight", ""))
            # Only keep auction-related results
            lower_title = title.lower()
            if any(kw in lower_title for kw in ["auction", "foreclosure", "sale"]):
                live_auction_listings.append({
                    "title": title,
                    "url": url,
                    "snippet": str(snippet)[:300] if snippet else "",
                    "source": "exa",
                })
        if live_auction_listings:
            sources.append(_source("exa"))

    # Step 1b: Parallel Extract — pull structured auction details from Exa URLs
    # Exa finds the listing pages, Parallel Extract reads the structured content
    auction_urls = [l["url"] for l in live_auction_listings if "auction.com" in l["url"]]
    if auction_urls:
        try:
            from aspire_orchestrator.providers.parallel_client import execute_parallel_extract
            extract_result = await execute_parallel_extract(
                payload={
                    "urls": auction_urls[:3],
                    "objective": "Extract property listings: address, auction date, auction time, auction location, beds, baths, sqft, lot size, year built, property type, opening bid, foreclosure status",
                    "max_chars_per_result": 3000,
                },
                **args,
            )
            if extract_result.outcome == Outcome.SUCCESS and extract_result.data:
                providers_called.append("parallel_extract")
                for ext_r in extract_result.data.get("results", []):
                    excerpts = ext_r.get("excerpts", [])
                    if excerpts:
                        # Find existing listing and enrich with extracted content
                        ext_url = ext_r.get("url", "")
                        for listing in live_auction_listings:
                            if listing["url"] == ext_url:
                                listing["extracted_content"] = excerpts[0][:2000]
                                listing["source"] = "exa+parallel_extract"
                                break
                        else:
                            # New listing from extract
                            live_auction_listings.append({
                                "title": ext_r.get("title", ""),
                                "url": ext_url,
                                "extracted_content": excerpts[0][:2000],
                                "source": "parallel_extract",
                            })
                sources.append(_source("parallel"))
        except Exception as exc:
            logger.warning("landlord.investment: parallel extract failed: %s", exc)

    # Parse market trends (ATTOM scope only — city-only path has no trends)
    trends_summary = ""
    if zip_code and trends_result and trends_result.outcome == Outcome.SUCCESS and trends_result.data:
        trends_props = trends_result.data.get("salesTrends", trends_result.data.get("property", []))
        if isinstance(trends_props, list) and trends_props:
            trends_summary = f"ZIP {zip_code} market trends available"

    # Step 2: Deep dive on top 5 foreclosure-flagged properties
    deep_opportunities: list[dict[str, Any]] = []
    for prop_info in fc_flagged[:5]:
        a1 = prop_info.get("line1", "")
        a2 = prop_info.get("line2", "")
        if not a1 or not a2:
            continue

        addr_payload = {"address": f"{a1}, {a2}"}

        # Parallel: expanded history (foreclosure filings) + equity + AVM +
        # Transaction V3 preforeclosure details (auction date, opening bid,
        # courthouse, lender — the actual auction data the user asked about).
        from aspire_orchestrator.providers.attom_client import (
            execute_attom_preforeclosure_details,
        )
        _raw_enrich = await asyncio.gather(
            execute_attom_sales_expanded_history(payload=addr_payload, **args),
            execute_attom_home_equity(payload=addr_payload, **args),
            execute_attom_valuation_avm(payload=addr_payload, **args),
            execute_attom_preforeclosure_details(
                payload={"combined_address": f"{a1}, {a2}"},
                **args,
            ),
            return_exceptions=True,
        )
        fc_result = _safe_result(_raw_enrich[0])
        equity_result = _safe_result(_raw_enrich[1])
        avm_result = _safe_result(_raw_enrich[2])
        prefc_result = _safe_result(_raw_enrich[3])

        opp: dict[str, Any] = {
            "address": prop_info.get("oneLine", f"{a1}, {a2}"),
            "year_built": prop_info.get("year_built"),
            "foreclosure_flag": prop_info.get("foreclosure_flag", ""),
            "trans_type": prop_info.get("trans_type", ""),
            "absentee": prop_info.get("absentee", ""),
        }

        # Merge foreclosure filings
        if fc_result and fc_result.outcome == Outcome.SUCCESS and fc_result.data:
            fc_data = normalize_from_attom_foreclosure(fc_result.data)
            fc_recs = fc_data.get("foreclosure_records", [])
            opp["foreclosure_stage"] = fc_data.get("foreclosure_stage", "none")
            opp["foreclosure_count"] = len(fc_recs)
            if fc_recs:
                latest = fc_recs[0]
                opp["latest_filing_date"] = latest.recording_date
                opp["latest_filing_type"] = latest.distress_type_label
                opp["auction_date"] = latest.auction_date_time
                opp["auction_location"] = latest.auction_location
                opp["lender"] = latest.lender_name
                opp["original_loan"] = latest.original_loan_amount
                opp["opening_bid"] = latest.opening_bid
            # Expanded sale history
            sh = fc_data.get("sale_history_expanded", [])
            if sh:
                opp["sale_history"] = sh[:5]

        # Merge equity
        if equity_result and equity_result.outcome == Outcome.SUCCESS and equity_result.data:
            eq = normalize_from_attom_equity(equity_result.data)
            opp["ltv_ratio"] = eq.get("ltv_ratio")
            opp["available_equity"] = eq.get("available_equity")
            opp["current_loan_balance"] = eq.get("current_loan_balance")

        # Merge AVM
        if avm_result and avm_result.outcome == Outcome.SUCCESS and avm_result.data:
            avm_data = normalize_from_attom_avm(avm_result.data)
            opp["estimated_value"] = avm_data.get("estimated_value")
            opp["avm_confidence"] = avm_data.get("avm_confidence_score")

        # Merge ATTOM Transaction V3 preforeclosure details — actual
        # auction data the user expects on auction-search results.
        # Returns Auction[] (auctionDate, auctionTime, courthouse,
        # auctionAddress) and Default[] (lender, judgmentAmount,
        # defaultAmount, recordedAuctionOpeningBid, trustee, servicer).
        if prefc_result and prefc_result.outcome == Outcome.SUCCESS and prefc_result.data:
            prefc_payload = prefc_result.data.get("PreforeclosureDetails", {})
            auctions = prefc_payload.get("Auction") or []
            defaults = prefc_payload.get("Default") or []
            if auctions:
                a0 = auctions[0]
                opp["auction_date_v3"] = a0.get("auctionDate")
                opp["auction_time_v3"] = a0.get("auctionTime")
                opp["auction_courthouse"] = a0.get("courthouse")
                # Build a one-line auction address from the parts ATTOM ships.
                addr_parts = [
                    a0.get("auctionHouseNumber"),
                    a0.get("auctionDirection"),
                    a0.get("auctionStreetName"),
                    a0.get("auctionSuffix"),
                    a0.get("auctionPostDirection"),
                    a0.get("auctionUnit"),
                ]
                addr_str = " ".join(p for p in addr_parts if p)
                if addr_str:
                    city = a0.get("auctionCity")
                    if city:
                        addr_str = f"{addr_str}, {city}"
                    opp["auction_address_v3"] = addr_str
            if defaults:
                d0 = defaults[0]
                opp["foreclosure_id"] = d0.get("foreclosureID")
                opp["lender_name_v3"] = d0.get("lenderNameFullStandardized")
                opp["lender_phone_v3"] = d0.get("lenderPhone")
                opp["judgment_amount"] = d0.get("judgmentAmount")
                opp["judgment_date"] = d0.get("judgmentDate")
                opp["default_amount"] = d0.get("defaultAmount")
                opp["opening_bid_v3"] = d0.get("recordedAuctionOpeningBid")
                opp["trustee_name"] = d0.get("trusteeName")
                opp["trustee_phone"] = d0.get("trusteePhone")
                opp["original_loan_amount"] = d0.get("originalLoanAmount")
                opp["loan_balance"] = d0.get("loanBalance")
                opp["foreclosure_record_type"] = d0.get("recordType")

        # Calculate investment metrics
        avm_val = opp.get("estimated_value")
        last_sale = prop_info.get("last_sale_amt")
        if avm_val and last_sale:
            opp["discount_pct"] = round((1 - last_sale / avm_val) * 100, 1)

        deep_opportunities.append(opp)

    # Build summary record
    summary_record = {
        "zip_code": zip_code,
        "location_label": location_label,
        "scope": "zip" if has_attom_scope else "city",
        "total_absentee_owners": absentee_count,
        "foreclosure_flagged_properties": len(fc_flagged),
        "deep_dive_count": len(deep_opportunities),
        "market_trends": trends_summary,
        "opportunities": deep_opportunities,
        "live_auction_listings": live_auction_listings,
        "all_foreclosure_flagged": [
            {
                "address": fp.get("oneLine", ""),
                "foreclosure_flag": fp.get("foreclosure_flag", ""),
                "trans_type": fp.get("trans_type", ""),
                "last_sale": fp.get("last_sale_amt"),
                "avm_value": fp.get("avm_value"),
                "year_built": fp.get("year_built"),
                "absentee": fp.get("absentee", ""),
                "recent_activity": fp.get("recent_activity", False),
            }
            for fp in fc_flagged
        ],
    }
    records.append(summary_record)
    if has_attom_scope:
        sources.append(_source("attom"))

    # required_fields tuned to the scope: zip-mode requires zip_code,
    # city-mode requires at least one auction listing (otherwise the user
    # truly got nothing useful and should know).
    required = ["zip_code"] if has_attom_scope else ["live_auction_listings"]
    report = verify_records(
        records=records,
        sources=sources,
        required_fields=required,
    )

    if has_attom_scope:
        summary_text = (
            f"Investment scan for ZIP {zip_code}: "
            f"{absentee_count} absentee owners, "
            f"{len(fc_flagged)} foreclosure-flagged properties, "
            f"{len(deep_opportunities)} with deep dive data"
            f"{f', {len(live_auction_listings)} live auction listings from web' if live_auction_listings else ''}. "
            f"Verification: {report.status}."
        )
    else:
        # City-only path: lead with the auction count from web search.
        if live_auction_listings:
            summary_text = (
                f"Found {len(live_auction_listings)} auction listings near "
                f"{location_label} via Auction.com and county sites. "
                f"For absentee-owner and foreclosure-pipeline data, give me a "
                f"specific ZIP in {location_label}."
            )
        else:
            summary_text = (
                f"No live auction listings turned up for {location_label}. "
                f"Try a specific ZIP in that area for the full ATTOM scan."
            )

    return ResearchResponse(
        artifact_type="InvestmentOpportunityPack",
        summary=summary_text,
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "attom"},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=[
            f"Deep dive on specific property address in {zip_code}",
            f"Rental yield analysis for {zip_code}",
            f"School district comparison for {zip_code}",
            f"Permit activity trends in {zip_code}",
        ],
        verification_report=report,
        segment="landlord",
        intent="investment_opportunity_scan",
        playbook="landlord.investment_opportunity_scan",
        providers_called=providers_called,
    )
