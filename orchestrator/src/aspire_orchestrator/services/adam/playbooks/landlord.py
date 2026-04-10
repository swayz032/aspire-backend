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

    Provider plan:
      here:   geocode address -> normalized address string
      attom:  property_detail_with_schools + sales_history + valuation_avm + rental_avm
              (parallel calls after geocoding)

    required_fields: normalized_address, living_sqft, year_built, owner_name
    Returns: LandlordPropertyPack artifact.
    """
    logger.info(
        "landlord.property_facts start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # Step 1: Normalize address with HERE (fallback to extracted address).
    normalized_address = await _geocode_address(query, context)
    providers_called.append("here")

    attom_payload = {"address": normalized_address}

    # Step 2: 6 parallel ATTOM calls — FULL property intelligence in one shot
    from aspire_orchestrator.providers.attom_client import (
        execute_attom_assessment_detail,
        execute_attom_expanded_profile,
        execute_attom_home_equity,
        execute_attom_sale_detail,
    )

    _raw_results = await asyncio.gather(
        execute_attom_detail_mortgage_owner(payload=attom_payload, **args),
        execute_attom_valuation_avm(payload=attom_payload, **args),
        execute_attom_home_equity(payload=attom_payload, **args),
        execute_attom_assessment_detail(payload=attom_payload, **args),
        execute_attom_sale_detail(payload=attom_payload, **args),
        execute_attom_expanded_profile(payload=attom_payload, **args),
        return_exceptions=True,
    )
    # Guard: convert exceptions to None (graceful degradation, not crash)
    detail_result = _safe_result(_raw_results[0])      # building + owner + mortgage
    avm_result = _safe_result(_raw_results[1])          # AVM value + confidence + FSD
    equity_result = _safe_result(_raw_results[2])       # LTV, equity, loan balance
    assessment_result = _safe_result(_raw_results[3])   # tax assessment + market value
    sale_result = _safe_result(_raw_results[4])         # last sale detail + price/sqft
    expanded_result = _safe_result(_raw_results[5])     # zoning, seller, census, REO flags
    providers_called.append("attom")

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

    # --- Merge permits + comps (additional ATTOM calls) ---
    from aspire_orchestrator.providers.attom_client import (
        execute_attom_building_permits,
        execute_attom_sale_snapshot_zip,
    )

    # Permits
    try:
        permit_result = await execute_attom_building_permits(payload=attom_payload, **args)
        if permit_result.outcome == Outcome.SUCCESS and permit_result.data:
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

    # Comps — neighborhood-level using geoIdV4 N4 (same neighborhood boundary)
    # Falls back to address+radius if geoIdV4 not available
    if prop_dict:
        try:
            # Extract geoIdV4 N4 (neighborhood) from detail response
            n4_id = ""
            if detail_result and detail_result.outcome == Outcome.SUCCESS and detail_result.data:
                det_props = detail_result.data.get("property", [])
                if det_props:
                    geo_v4 = det_props[0].get("location", {}).get("geoIdV4", {})
                    n4_id = geo_v4.get("N4", "") if isinstance(geo_v4, dict) else ""

            comp_result = None
            if n4_id:
                # Neighborhood comps (tight — same ATTOM neighborhood boundary)
                comp_result = await _attom_request(
                    path="/sale/snapshot",
                    query_params={
                        "geoIdV4": n4_id,
                        "minsaleamt": "100000",
                        "maxsaleamt": "500000",
                        "propertytype": "SFR",
                        "pagesize": "10",
                        "orderby": "saleSearchDate desc",
                    },
                    tool_id="attom.neighborhood_comps",
                    correlation_id=context.correlation_id,
                    suite_id=context.suite_id,
                    office_id=context.office_id,
                )
            if not comp_result or comp_result.outcome != Outcome.SUCCESS:
                # Fallback: address + 0.5 mile radius
                comp_result = await _attom_request(
                    path="/sale/snapshot",
                    query_params={
                        "address1": normalized_address.split(",")[0].strip(),
                        "address2": ",".join(normalized_address.split(",")[1:]).strip(),
                        "radius": "0.5",
                        "minsaleamt": "100000",
                        "maxsaleamt": "500000",
                        "propertytype": "SFR",
                        "pagesize": "10",
                        "orderby": "saleSearchDate desc",
                    },
                    tool_id="attom.radius_comps",
                    correlation_id=context.correlation_id,
                    suite_id=context.suite_id,
                    office_id=context.office_id,
                )

            if comp_result and comp_result.outcome == Outcome.SUCCESS and comp_result.data:
                comps = []
                subject_attom_id = prop_dict.get("attom_id", "")
                for cp in comp_result.data.get("property", [])[:15]:
                    # Skip the subject property itself
                    cp_id = str(cp.get("identifier", {}).get("attomId", ""))
                    if cp_id == subject_attom_id:
                        continue
                    cp_addr = cp.get("address", {}).get("oneLine", "")
                    cp_sale = cp.get("sale", {})
                    cp_amt = cp_sale.get("amount", {})
                    cp_bldg = cp.get("building", {})
                    cp_calc = cp_sale.get("calculation", {})
                    comps.append({
                        "address": cp_addr,
                        "sale_price": cp_amt.get("saleamt"),
                        "sale_date": cp_amt.get("salerecdate", cp_sale.get("saleTransDate", "")),
                        "sqft": cp_bldg.get("size", {}).get("universalsize") or cp_bldg.get("size", {}).get("livingsize"),
                        "beds": cp_bldg.get("rooms", {}).get("beds"),
                        "year_built": cp.get("summary", {}).get("yearbuilt"),
                        "price_per_sqft": cp_calc.get("pricepersizeunit"),
                        "distance_miles": cp.get("location", {}).get("distance"),
                    })
                if comps:
                    prop_dict["nearby_comps"] = comps[:10]
        except Exception as exc:
            logger.warning("landlord.property_facts: comps failed: %s", exc)

    # --- Merge schools from detailwithschools (optional extra call) ---
    # Schools come from the detailwithschools endpoint — try it if we have data
    try:
        schools_result = await execute_attom_property_detail_with_schools(
            payload=attom_payload, **args,
        )
        if schools_result.outcome == Outcome.SUCCESS and schools_result.data:
            pinned_schools_data = _pin_attom_payload_to_subject(schools_result.data, normalized_address)
            schools = normalize_from_attom_schools(pinned_schools_data or schools_result.data)
            if schools and prop_dict:
                prop_dict["nearby_schools"] = schools
    except Exception:
        pass  # Schools are nice-to-have, not critical

    # --- Merge foreclosure filings (NOD, lis pendens, auction dates, trustee info) ---
    try:
        from aspire_orchestrator.providers.attom_client import execute_attom_sales_expanded_history
        fc_result = await execute_attom_sales_expanded_history(payload=attom_payload, **args)
        if fc_result.outcome == Outcome.SUCCESS and fc_result.data:
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
        logger.warning("landlord.property_facts: foreclosure data failed: %s", exc)

    if prop_dict:
        # Set property_value = tax market value (county official) as the default.
        # AVM is an algorithm estimate — tax assessment is authoritative.
        tax_mv = prop_dict.get("tax_market_value")
        avm_v = prop_dict.get("estimated_value")
        prop_dict["property_value"] = tax_mv or avm_v
        prop_dict["property_value_source"] = "county_tax_assessment" if tax_mv else "avm_estimate"
        records.append(prop_dict)

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["normalized_address", "living_sqft", "year_built", "owner_name"],
    )

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

    # Extract ZIP from query (look for 5-digit ZIP code)
    import re
    zip_match = re.search(r'\b(\d{5})\b', query)
    if not zip_match:
        return ResearchResponse(
            artifact_type="InvestmentOpportunityPack",
            summary="No ZIP code found in query. Please include a 5-digit ZIP code.",
            records=[],
            sources=[],
            freshness={"mode": "live", "provider": "attom"},
            confidence={"status": "unverified", "score": 0},
            missing_fields=["zip_code"],
            next_queries=["Try: 'investment opportunities in 30297'"],
            segment="landlord",
            intent="investment_opportunity_scan",
            playbook="landlord.investment_opportunity_scan",
            providers_called=[],
        )
    zip_code = zip_match.group(1)

    from aspire_orchestrator.providers.attom_client import (
        execute_attom_sales_expanded_history,
        execute_attom_home_equity,
        _attom_request,
    )

    # Step 1: Parallel scans — absentee owners + ALL FC-flagged + recent activity + live auctions
    # Use startsalesearchdate=last 18 months to catch current foreclosure pipeline
    from datetime import datetime, timedelta
    cutoff_date = (datetime.now() - timedelta(days=540)).strftime("%Y/%m/%d")

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
        execute_exa_search(
            payload={"query": f"foreclosure auction listings {zip_code} 2026 upcoming auction date property sale"},
            **args,
        ),
        return_exceptions=True,
    )
    absentee_result = _safe_result(_raw_inv[0])
    fc_events_result = _safe_result(_raw_inv[1])
    recent_events_result = _safe_result(_raw_inv[2])
    trends_result = _safe_result(_raw_inv[3])
    exa_result = _safe_result(_raw_inv[4])
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

    # Parse market trends
    trends_summary = ""
    if trends_result and trends_result.outcome == Outcome.SUCCESS and trends_result.data:
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

        # Parallel: expanded history (foreclosure filings) + equity + AVM
        _raw_enrich = await asyncio.gather(
            execute_attom_sales_expanded_history(payload=addr_payload, **args),
            execute_attom_home_equity(payload=addr_payload, **args),
            execute_attom_valuation_avm(payload=addr_payload, **args),
            return_exceptions=True,
        )
        fc_result = _safe_result(_raw_enrich[0])
        equity_result = _safe_result(_raw_enrich[1])
        avm_result = _safe_result(_raw_enrich[2])

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

        # Calculate investment metrics
        avm_val = opp.get("estimated_value")
        last_sale = prop_info.get("last_sale_amt")
        if avm_val and last_sale:
            opp["discount_pct"] = round((1 - last_sale / avm_val) * 100, 1)

        deep_opportunities.append(opp)

    # Build summary record
    summary_record = {
        "zip_code": zip_code,
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
    sources.append(_source("attom"))

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["zip_code"],
    )

    return ResearchResponse(
        artifact_type="InvestmentOpportunityPack",
        summary=(
            f"Investment scan for ZIP {zip_code}: "
            f"{absentee_count} absentee owners, "
            f"{len(fc_flagged)} foreclosure-flagged properties, "
            f"{len(deep_opportunities)} with deep dive data"
            f"{f', {len(live_auction_listings)} live auction listings from web' if live_auction_listings else ''}. "
            f"Verification: {report.status}."
        ),
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
