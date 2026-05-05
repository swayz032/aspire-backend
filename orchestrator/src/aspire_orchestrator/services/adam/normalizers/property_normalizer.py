"""Normalize ATTOM API responses to PropertyRecord.

Follows ATTOM_FIELD_MAP.md from handoff. Each ATTOM endpoint returns
different response shapes — this normalizer maps them all to a single
canonical PropertyRecord.

Rule: Never invent parcel facts. If a field is missing, leave it empty.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.property_record import (
    ForeclosureRecord,
    PropertyRecord,
    SaleRecord,
)

logger = logging.getLogger(__name__)


# Property types that ATTOM tracks at unit-level. If the normalized record
# for one of these types comes back with an absurdly small living area, the
# upstream query was almost certainly resolved at building/master-parcel
# level instead of unit level — a data-contract failure that must surface
# as a typed error rather than silently shipping bad data.
_UNIT_PROPERTY_TYPES: frozenset[str] = frozenset({
    "SFR", "CONDO", "TOWNHOUSE",
    "SINGLE FAMILY RESIDENCE", "SINGLE FAMILY", "TOWN HOUSE",
})


class AttomUnitDataMissingError(Exception):
    """Raised when ATTOM returns building-level placeholders for a unit address.

    The provider responded successfully but the unit-level fields are absent
    or stubbed (`living_sqft < 100`, `tax_market_value < ~5`). This is a
    contract failure — the orchestrator should surface it as a structured
    error to the user instead of rendering meaningless data.
    """

    def __init__(self, normalized_address: str, living_sqft: int | None,
                 property_type: str, tax_market_value: float | None = None) -> None:
        self.normalized_address = normalized_address
        self.living_sqft = living_sqft
        self.property_type = property_type
        self.tax_market_value = tax_market_value
        super().__init__(
            "ATTOM returned building-level placeholders for unit address "
            f"'{normalized_address}' (type={property_type}, "
            f"living_sqft={living_sqft}, tax_market_value={tax_market_value}). "
            "Expected unit-level data from /property/expandedprofile."
        )


def assert_unit_data_complete(record: dict[str, Any]) -> None:
    """Contract guard: raise AttomUnitDataMissingError if record looks building-level.

    Triggered when:
      - property_type is in {SFR, CONDO, TOWNHOUSE} (or canonical equivalents)
      - AND living_sqft is suspiciously small (< 100) OR missing entirely while
        tax_market_value is nominal (< 5)

    Why both checks: some commercial parcels legitimately report tiny living
    areas; pairing the type + size signals isolates condo/SFR contract
    failures specifically.
    """
    living_sqft = record.get("living_sqft")
    tax_market = record.get("tax_market_value")
    prop_type = (record.get("property_type") or "").upper().strip()

    if not prop_type:
        return  # Type unknown — cannot make a contract claim.

    if prop_type not in _UNIT_PROPERTY_TYPES:
        return  # Commercial / land / multi-family handled elsewhere.

    bad_size = isinstance(living_sqft, int) and living_sqft < 100
    bad_tax = isinstance(tax_market, (int, float)) and tax_market < 5

    if bad_size or bad_tax:
        raise AttomUnitDataMissingError(
            normalized_address=record.get("normalized_address", ""),
            living_sqft=living_sqft if isinstance(living_sqft, int) else None,
            property_type=prop_type,
            tax_market_value=float(tax_market) if isinstance(tax_market, (int, float)) else None,
        )


def normalize_from_attom_detail(data: dict[str, Any]) -> PropertyRecord:
    """Normalize ATTOM property/detail or property/snapshot response."""
    props = data.get("property", [])
    if not props:
        return PropertyRecord(
            verification_status="unverified",
            sources=[SourceAttribution(provider="attom", retrieved_at=datetime.now(timezone.utc).isoformat())],
        )

    p = props[0] if isinstance(props, list) else props
    address = p.get("address", {}) or {}
    building = p.get("building", {}) or {}
    bldg_summary = building.get("summary", {}) or {}
    prop_summary = p.get("summary", {}) or {}  # ATTOM puts yearbuilt, proptype here
    rooms = building.get("rooms", {}) or {}
    bldg_size = building.get("size", {}) or {}
    lot = p.get("lot", {}) or {}
    assessment = p.get("assessment", {}) or {}
    owner = p.get("owner", {}) or {}
    sale = p.get("sale", {}) or {}
    vintage = p.get("vintage", {}) or {}
    mortgage = p.get("mortgage", {}) or {}
    area = p.get("area", {}) or {}
    location = p.get("location", {}) or {}

    # Geography — lat/lng + county. ATTOM returns lat/lng as strings inside
    # the location block. _safe_float coerces and falls back to None on parse
    # error (e.g. empty string or "0"). County lives in area.countrysecsubd
    # (county name) for most responses; some use area.munname for the
    # municipality and area.countrysecsubd for county. Try the canonical key
    # first, then fall back.
    latitude = _safe_float(location.get("latitude"))
    longitude = _safe_float(location.get("longitude"))
    # ATTOM uses 0/0 as a sentinel for "no coords" — treat as null.
    if latitude == 0 and longitude == 0:
        latitude = longitude = None
    county = (
        area.get("countrysecsubd", "")
        or area.get("countrySecSubd", "")
        or area.get("countyrsa", "")
        or ""
    )

    # geoIdV4 dict — keys like ND (neighborhood), SB (school boundary),
    # CS (county subdivision), ZI (zip), N1/N2/N3 (neighborhood hierarchy).
    # Used by community_profile + salestrend calls in the W2 second-wave
    # gather. Falls back to legacy `geoid` string parsing when geoIdV4 is
    # missing (some ATTOM responses still return the comma-joined legacy
    # string only).
    geo_id_v4 = location.get("geoIdV4") if isinstance(location.get("geoIdV4"), dict) else {}

    # Owner name — ATTOM uses lowercase keys (fullname, not fullName)
    owner1 = owner.get("owner1", {}) or {}
    owner_name = (
        owner1.get("fullname", "") or owner1.get("fullName", "") or
        owner1.get("firstnameandmi", "") or ""
    ).strip()

    return PropertyRecord(
        normalized_address=address.get("oneLine", ""),
        parcel_apn=str(p.get("identifier", {}).get("apn", "") or ""),
        parcel_fips=str(p.get("identifier", {}).get("fips", "") or ""),
        attom_id=str(p.get("identifier", {}).get("attomId", "") or ""),
        # ATTOM: proptype is in p.summary (not building.summary)
        property_type=prop_summary.get("proptype", "") or prop_summary.get("propertyType", ""),
        year_built=_safe_int(prop_summary.get("yearbuilt") or bldg_summary.get("yearbuilt")),
        living_sqft=_safe_int(bldg_size.get("livingsize") or bldg_size.get("livingSize")),
        lot_sqft=_safe_int(lot.get("lotsize2") or lot.get("lotsize1")),
        beds=_safe_int(rooms.get("beds")),
        baths=_safe_float(rooms.get("bathstotal") or rooms.get("bathsfull")),
        stories=_safe_int(bldg_summary.get("levels")),
        construction_frame=building.get("construction", {}).get("frameType", ""),
        roof_cover=building.get("roof", {}).get("cover", ""),
        quality=bldg_summary.get("quality", ""),
        # Owner
        owner_name=owner_name,
        owner_type=owner.get("corporateindicator", "") or owner.get("corporateIndicator", ""),
        mailing_address=owner.get("mailingaddressoneline", "") or (
            owner.get("mailAddress", {}).get("oneLine", "") if isinstance(owner.get("mailAddress"), dict) else ""
        ),
        absentee_owner_indicator=(
            owner.get("absenteeownerstatus", "") == "A" or
            prop_summary.get("absenteeInd", "") == "ABSENTEE OWNER"
        ) if owner.get("absenteeownerstatus") or prop_summary.get("absenteeInd") else None,
        owner_occupied=prop_summary.get("absenteeInd", ""),
        # Mortgage
        mortgage_lender=mortgage.get("lender", {}).get("lastname", "") if isinstance(mortgage.get("lender"), dict) else "",
        mortgage_amount=_safe_float(mortgage.get("amount")),
        mortgage_date=str(mortgage.get("date", "") or ""),
        mortgage_loan_type=str(mortgage.get("loantypecode", "") or ""),
        mortgage_term_months=_safe_int(mortgage.get("term")),
        mortgage_due_date=str(mortgage.get("duedate", "") or ""),
        deed_type=str(mortgage.get("deedtype", "") or ""),
        # Sale
        last_sale_date=str(sale.get("saleTransDate", "") or ""),
        last_sale_amount=_safe_float(
            sale.get("saleAmountData", {}).get("saleAmt")
            if sale.get("saleAmountData")
            else sale.get("amount", {}).get("saleAmt")
        ),
        assessment_context=str(assessment.get("assessed", {}).get("assdTtlValue", "")) if assessment.get("assessed") else "",
        # Geography
        subdivision=lot.get("subdname", "") or area.get("subdname", ""),
        neighborhood=area.get("munname", ""),
        county=county,
        latitude=latitude,
        longitude=longitude,
        geo_id_v4=dict(geo_id_v4),
        source_last_modified=str(vintage.get("lastModified", "") or ""),
        verification_status="verified",
        confidence=0.92,
        sources=[SourceAttribution(provider="attom", retrieved_at=datetime.now(timezone.utc).isoformat())],
    )


def normalize_from_attom_sales_history(data: dict[str, Any]) -> list[SaleRecord]:
    """Normalize ATTOM saleshistory/detail response to list of SaleRecord."""
    props = data.get("property", [])
    if not props:
        return []

    records: list[SaleRecord] = []
    for p in (props if isinstance(props, list) else [props]):
        for sale in p.get("saleHistory", []):
            records.append(SaleRecord(
                date=str(sale.get("amount", {}).get("saleRecDate", "") or sale.get("saleTransDate", "")),
                amount=_safe_float(sale.get("amount", {}).get("saleAmt")),
                trans_type=str(sale.get("amount", {}).get("saleTransType", "")),
                buyer=str(sale.get("buyer1FullName", "") or ""),
                seller=str(sale.get("seller1FullName", "") or ""),
            ))
    return records


def normalize_from_attom_avm(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize ATTOM attomavm/detail response — value + confidence + FSD."""
    props = data.get("property", [])
    if not props:
        return {}

    p = props[0] if isinstance(props, list) else props
    avm = p.get("avm", {}) or {}
    amount = avm.get("amount", {}) or {}
    calcs = avm.get("calculations", {}) or {}

    return {
        "estimated_value": _safe_float(amount.get("value")),
        "estimated_value_high": _safe_float(amount.get("high")),
        "estimated_value_low": _safe_float(amount.get("low")),
        "avm_confidence_score": _safe_int(amount.get("scr")),
        "avm_fsd": _safe_float(amount.get("fsd")),
        "avm_price_per_sqft": _safe_float(calcs.get("perSizeUnit")),
        "avm_date": str(avm.get("eventDate", "")),
    }


def normalize_from_attom_equity(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize ATTOM valuation/homeequity response — equity, LTV, loan balance."""
    props = data.get("property", [])
    if not props:
        return {}

    p = props[0] if isinstance(props, list) else props
    eq = p.get("homeEquity", {}) or {}

    current_balance = _safe_float(eq.get("totalEstimatedLoanBalance") or eq.get("firstAmortizedLoanAmount"))
    avm_val = _safe_float(p.get("avm", {}).get("amount", {}).get("value"))

    # Estimate monthly payment (P&I) based on current balance and remaining term
    # ATTOM gives us amortized balance — we estimate payment from that
    monthly_payment = None
    if current_balance and current_balance > 0:
        # Estimate rate based on LTV (ATTOM gives us this)
        ltv = _safe_int(eq.get("LTV"))
        # Use ATTOM's own amortized balance for back-calculation
        # Reasonable rate estimates: FHA ~3.5% (2019), conventional ~4-7%
        # We use 4.5% as a middle estimate since we don't have actual rate
        r = 0.045 / 12
        # Estimate remaining term from balance ratio
        n = 300  # ~25 years remaining for typical refi/purchase
        try:
            monthly_payment = round(current_balance * (r * (1 + r) ** n) / ((1 + r) ** n - 1), 2)
        except (ZeroDivisionError, OverflowError):
            pass

    return {
        "ltv_ratio": _safe_int(eq.get("LTV")),
        "available_equity": _safe_float(eq.get("estimatedAvailableEquity")),
        "lendable_equity": _safe_float(eq.get("estimatedLendableEquity")),
        "current_loan_balance": current_balance,
        "estimated_monthly_payment": monthly_payment,
        "equity_last_updated": str(eq.get("recordLastUpdated", "")),
    }


def normalize_from_attom_assessment(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize ATTOM assessment/detail response — tax + market value."""
    props = data.get("property", [])
    if not props:
        return {}

    p = props[0] if isinstance(props, list) else props
    asmt = p.get("assessment", {}) or {}
    assessed = asmt.get("assessed", {}) or {}
    market = asmt.get("market", {}) or {}
    tax = asmt.get("tax", {}) or {}

    return {
        "tax_assessed_total": _safe_float(assessed.get("assdttlvalue")),
        "tax_assessed_land": _safe_float(assessed.get("assdlandvalue")),
        "tax_assessed_improvement": _safe_float(assessed.get("assdimprvalue")),
        "tax_market_value": _safe_float(market.get("mktttlvalue")),
        "tax_market_land": _safe_float(market.get("mktlandvalue")),
        "tax_market_improvement": _safe_float(market.get("mktimprvalue")),
        "annual_tax_amount": _safe_float(tax.get("taxamt")),
        "tax_year": _safe_int(tax.get("taxyear")),
        "tax_per_sqft": _safe_float(tax.get("taxpersizeunit")),
    }


def normalize_from_attom_sale_detail(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize ATTOM sale/detail response — full last sale info."""
    props = data.get("property", [])
    if not props:
        return {}

    p = props[0] if isinstance(props, list) else props
    sale = p.get("sale", {}) or {}
    amount = sale.get("amount", {}) or {}
    calc = sale.get("calculation", {}) or {}

    sale_price = _safe_float(amount.get("saleamt"))
    avm_val = _safe_float(p.get("avm", {}).get("amount", {}).get("value"))

    # Calculate appreciation since last sale
    appreciation = None
    if sale_price and sale_price > 0 and avm_val and avm_val > 0:
        appreciation = round(((avm_val - sale_price) / sale_price) * 100, 1)

    return {
        "last_sale_date": str(sale.get("saleTransDate", "") or amount.get("salerecdate", "")),
        "last_sale_amount": sale_price,
        "last_sale_price_per_sqft": _safe_float(calc.get("pricepersizeunit")),
        "last_sale_price_per_bed": _safe_float(calc.get("priceperbed")),
        "last_sale_type": str(amount.get("saletranstype", "")),
        "last_sale_cash_or_mortgage": "Mortgage" if sale.get("cashormortgagepurchase") == "M" else (
            "Cash" if sale.get("cashormortgagepurchase") == "C" else ""
        ),
        "last_sale_arms_length": sale.get("interfamily") == "N" if sale.get("interfamily") else None,
        "last_sale_doc_number": str(amount.get("saledocnum", "")),
        "appreciation_pct": appreciation,
    }


def normalize_from_attom_community(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize ATTOM /v4/neighborhood/community response.

    ATTOM returns 600+ attributes nested under `community`. We surface the
    top-line numbers a user would actually look at on a property card —
    population, income, crime, weather, employment. The full payload is
    discarded to keep the desktop record under a reasonable size; the card
    can show the curated subset and a "more" affordance can re-fetch on
    demand if needed.
    """
    community = data.get("community") or {}
    if not isinstance(community, dict) or not community:
        return {}

    demographics = community.get("demographics") or {}
    crime = community.get("crime") or {}
    employment = community.get("employment") or {}
    weather = community.get("weather") or {}
    education = community.get("education") or {}

    # ATTOM Community attributes are sometimes flat (e.g. demographics is a
    # dict of scalar values) and sometimes nested. Use .get() everywhere
    # and skip blanks.
    return {
        "population": _safe_int(demographics.get("population")),
        "population_density_sq_mi": _safe_float(demographics.get("population_density_sq_mi")),
        "median_household_income": _safe_float(
            demographics.get("median_household_income")
            or demographics.get("medianHouseholdIncome")
        ),
        "median_age": _safe_float(demographics.get("median_age") or demographics.get("medianAge")),
        "owner_occupied_pct": _safe_float(
            demographics.get("owner_occupied_pct")
            or demographics.get("ownerOccupiedPct")
        ),
        "renter_occupied_pct": _safe_float(
            demographics.get("renter_occupied_pct")
            or demographics.get("renterOccupiedPct")
        ),
        "vacancy_pct": _safe_float(demographics.get("vacancy_pct") or demographics.get("vacancyPct")),
        "median_home_value": _safe_float(
            demographics.get("median_home_value")
            or demographics.get("medianHomeValue")
        ),
        # Crime — ATTOM exposes a normalized 0-100 index ("100=highest risk"
        # in their docs); the per-category counts vary by package.
        "crime_index": _safe_float(crime.get("crime_index") or crime.get("crimeIndex")),
        "violent_crime_index": _safe_float(
            crime.get("violent_crime_index") or crime.get("violentCrimeIndex")
        ),
        "property_crime_index": _safe_float(
            crime.get("property_crime_index") or crime.get("propertyCrimeIndex")
        ),
        # Employment / unemployment
        "unemployment_pct": _safe_float(
            employment.get("unemployment_pct")
            or employment.get("unemploymentPct")
        ),
        # Weather averages — useful for property-condition context
        "weather_avg_temp_f": _safe_float(
            weather.get("avg_annual_temp_f")
            or weather.get("avgAnnualTempF")
        ),
        "weather_avg_rainfall_in": _safe_float(
            weather.get("avg_annual_rainfall_in")
            or weather.get("avgAnnualRainfallIn")
        ),
        # Education quality proxy
        "education_high_school_pct": _safe_float(
            education.get("high_school_grad_pct")
            or education.get("highSchoolGradPct")
        ),
        "education_bachelors_pct": _safe_float(
            education.get("bachelors_or_higher_pct")
            or education.get("bachelorsOrHigherPct")
        ),
    }


def normalize_from_attom_poi(data: dict[str, Any], max_items: int = 25) -> list[dict[str, Any]]:
    """Normalize ATTOM /v4/neighborhood/poi response.

    Returns up to `max_items` POIs sorted by distance ascending, deduplicated
    by `ob_id` so a Home Depot listed under multiple business categories
    only appears once.
    """
    pois_raw = data.get("poi", []) or data.get("POI", []) or []
    if isinstance(pois_raw, dict):
        # Some packages wrap under poi.item
        pois_raw = pois_raw.get("item", []) or []
    if not isinstance(pois_raw, list):
        return []

    seen_ids: set[str] = set()
    items: list[dict[str, Any]] = []
    for poi in pois_raw:
        if not isinstance(poi, dict):
            continue
        # Skip secondary listings (PRIMARY=OTHER per ATTOM docs)
        primary = (poi.get("primary") or poi.get("PRIMARY") or "").upper()
        if primary == "OTHER":
            continue
        ob_id = str(poi.get("ob_id", "") or poi.get("OB_ID", "") or "")
        if ob_id and ob_id in seen_ids:
            continue
        if ob_id:
            seen_ids.add(ob_id)

        items.append({
            "ob_id": ob_id,
            "name": str(poi.get("name") or poi.get("NAME") or ""),
            "category": str(
                poi.get("business_category")
                or poi.get("BUSINESS_CATEGORY")
                or ""
            ),
            "lob": str(poi.get("lob") or poi.get("LOB") or ""),
            "industry": str(poi.get("industry") or poi.get("INDUSTRY") or ""),
            "distance_miles": _safe_float(poi.get("distance") or poi.get("DISTANCE")),
            "address": str(
                poi.get("address_full")
                or poi.get("ADDRESS_FULL")
                or poi.get("address")
                or ""
            ),
            "city": str(poi.get("city") or poi.get("CITY") or ""),
            "state": str(poi.get("state") or poi.get("STATE") or ""),
            "phone": str(poi.get("phone") or poi.get("PHONE") or ""),
            "latitude": _safe_float(poi.get("geo_latitude") or poi.get("GEO_LATITUDE")),
            "longitude": _safe_float(poi.get("geo_longitude") or poi.get("GEO_LONGITUDE")),
            "franchise": str(poi.get("franchise") or poi.get("FRANCHISE") or ""),
        })

    items.sort(key=lambda x: x["distance_miles"] if x["distance_miles"] is not None else 999.0)
    return items[:max_items]


def normalize_from_attom_salestrend(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize ATTOM /v4/transaction/salestrend response.

    Returns the most recent quarter snapshot + a 24-month series suitable
    for the salestrend card's mini chart. ATTOM publishes monthly,
    quarterly, and yearly intervals; we keep all three for the card to
    pick.
    """
    salestrends = data.get("salestrends") or data.get("SalesTrend") or []
    if isinstance(salestrends, dict):
        salestrends = [salestrends]
    if not isinstance(salestrends, list) or not salestrends:
        return {}

    # ATTOM groups responses by interval. Pick the most informative.
    monthly: list[dict[str, Any]] = []
    quarterly: list[dict[str, Any]] = []
    yearly: list[dict[str, Any]] = []

    for trend_block in salestrends:
        if not isinstance(trend_block, dict):
            continue
        interval = (trend_block.get("interval") or "").lower()
        items = trend_block.get("salesTrend") or trend_block.get("salestrend") or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            entry = {
                "period": str(item.get("date") or item.get("period") or ""),
                "median_sale_price": _safe_float(
                    item.get("avgSalesPrice")
                    or item.get("medianSalesPrice")
                    or item.get("avgsalesprice")
                ),
                "sale_count": _safe_int(item.get("homesSold") or item.get("salesCount")),
            }
            if interval == "monthly":
                monthly.append(entry)
            elif interval == "quarterly":
                quarterly.append(entry)
            elif interval == "yearly":
                yearly.append(entry)

    monthly.sort(key=lambda e: e["period"], reverse=True)
    quarterly.sort(key=lambda e: e["period"], reverse=True)
    yearly.sort(key=lambda e: e["period"], reverse=True)

    latest = (monthly[:1] or quarterly[:1] or yearly[:1] or [{}])[0]
    return {
        "latest_period": latest.get("period", ""),
        "latest_median_sale_price": latest.get("median_sale_price"),
        "latest_sale_count": latest.get("sale_count"),
        "monthly": monthly[:24],
        "quarterly": quarterly[:8],
        "yearly": yearly[:5],
    }


def normalize_from_attom_avm_history(data: dict[str, Any], max_items: int = 24) -> list[dict[str, Any]]:
    """Normalize ATTOM /avmhistory/detail response — per-period AVM values.

    Returns up to `max_items` snapshots sorted newest-first for the AVM
    history card's trajectory chart.
    """
    props = data.get("property", [])
    if not props:
        return []

    p = props[0] if isinstance(props, list) else props
    history_raw = p.get("avmhistory", []) or p.get("avmHistory", []) or []
    if not isinstance(history_raw, list):
        return []

    points: list[dict[str, Any]] = []
    for snap in history_raw:
        if not isinstance(snap, dict):
            continue
        amount = snap.get("amount") if isinstance(snap.get("amount"), dict) else snap
        points.append({
            "date": str(snap.get("eventDate") or snap.get("eventdate") or ""),
            "value": _safe_float(amount.get("value") if isinstance(amount, dict) else None),
            "value_high": _safe_float(amount.get("high") if isinstance(amount, dict) else None),
            "value_low": _safe_float(amount.get("low") if isinstance(amount, dict) else None),
            "confidence_score": _safe_int(amount.get("scr") if isinstance(amount, dict) else None),
        })

    points = [pt for pt in points if pt["date"] and pt["value"] is not None]
    points.sort(key=lambda pt: pt["date"], reverse=True)
    return points[:max_items]


def normalize_from_attom_sales_comparables(
    data: dict[str, Any],
    max_items: int = 12,
) -> list[dict[str, Any]]:
    """Normalize ATTOM /property/v2/salescomparables response.

    Returns up to `max_items` recent comparable sales sorted by distance
    ascending. Each comp has the fields the comps card needs to render a
    meaningful list: address, distance, beds/baths/sqft, last sale, AVM.
    """
    # Sales comparables responses come back as a different envelope shape
    # than detail/snapshot — the comps live under `RESPONSE_GROUP.RESPONSE.RESPONSE_DATA.PROPERTY_INFORMATION_RESPONSE_ext`
    # in the ATTOM v2 packaging. We try the modern key first and fall back
    # to the legacy nested path.
    comps_raw: list[dict[str, Any]] = []

    # Modern shape: data.comparables[] or data.property[]
    if isinstance(data.get("comparables"), list):
        comps_raw = data["comparables"]
    elif isinstance(data.get("property"), list):
        comps_raw = data["property"]
    else:
        # Legacy nested ATTOM v2 envelope
        rg = data.get("RESPONSE_GROUP") or {}
        resp = rg.get("RESPONSE") if isinstance(rg, dict) else None
        if isinstance(resp, dict):
            rd = resp.get("RESPONSE_DATA") or {}
            if isinstance(rd, dict):
                pir = (
                    rd.get("PROPERTY_INFORMATION_RESPONSE_ext")
                    or rd.get("PROPERTY_INFORMATION_RESPONSE")
                    or {}
                )
                if isinstance(pir, dict):
                    sr = pir.get("SUBJECT_PROPERTY_ext") or {}
                    comps = sr.get("PROPERTY") or pir.get("PROPERTY") or []
                    if isinstance(comps, list):
                        comps_raw = comps

    if not comps_raw:
        return []

    items: list[dict[str, Any]] = []
    for c in comps_raw:
        if not isinstance(c, dict):
            continue
        # ATTOM comps uppercase keys at the legacy node, lowercase at the
        # v4 node — handle both.
        addr = c.get("address") or c.get("ADDRESS") or {}
        addr_one = (
            (addr.get("oneLine") if isinstance(addr, dict) else None)
            or c.get("AddressLine1")
            or c.get("address_full")
            or ""
        )
        building = c.get("building") or c.get("BUILDING") or {}
        rooms = building.get("rooms") if isinstance(building, dict) else {} or {}
        size = building.get("size") if isinstance(building, dict) else {} or {}
        sale = c.get("sale") or c.get("SALE") or {}
        sale_amount = (
            sale.get("amount", {}).get("saleAmt") if isinstance(sale.get("amount"), dict)
            else sale.get("saleAmount")
            or sale.get("salesPriceAmount")
        )
        avm = c.get("avm") or {}
        avm_amount = avm.get("amount", {}) if isinstance(avm.get("amount"), dict) else {}

        items.append({
            "attom_id": str(
                (c.get("identifier") or {}).get("attomId", "")
                or c.get("attomId")
                or c.get("PropertyAddressKey")
                or ""
            ),
            "address": str(addr_one or ""),
            "distance_miles": _safe_float(
                c.get("distance")
                or c.get("Distance")
                or c.get("DistanceMiles")
            ),
            "beds": _safe_int(rooms.get("beds") if isinstance(rooms, dict) else None),
            "baths": _safe_float(rooms.get("bathstotal") if isinstance(rooms, dict) else None),
            "living_sqft": _safe_int(size.get("livingsize") if isinstance(size, dict) else None),
            "year_built": _safe_int(
                (building.get("summary") or {}).get("yearbuilt") if isinstance(building, dict) else None
            ),
            "last_sale_date": str(
                (sale.get("salesSearchDate") if isinstance(sale, dict) else "")
                or (sale.get("saleTransDate") if isinstance(sale, dict) else "")
                or ""
            ),
            "last_sale_amount": _safe_float(sale_amount),
            "estimated_value": _safe_float(avm_amount.get("value") if isinstance(avm_amount, dict) else None),
        })

    # Drop entries with no usable info
    items = [it for it in items if it["address"] or it["last_sale_amount"]]
    items.sort(key=lambda x: x["distance_miles"] if x["distance_miles"] is not None else 999.0)
    return items[:max_items]


def normalize_from_attom_allevents(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize ATTOM /allevents/snapshot response to a flat transaction
    history list.

    Each entry covers one event in the property's recorded timeline (sale,
    mortgage origination, assessment, transfer, foreclosure filing, etc.).
    The shape is intentionally flat so the desktop card can render the
    timeline as MiniRow entries without further reshaping.
    """
    props = data.get("property", [])
    if not props:
        return []

    p = props[0] if isinstance(props, list) else props
    events_raw = (
        p.get("events", [])
        or p.get("allEvents", [])
        or p.get("event", [])
        or []
    )
    if not isinstance(events_raw, list):
        events_raw = [events_raw]

    history: list[dict[str, Any]] = []
    for ev in events_raw:
        if not isinstance(ev, dict):
            continue
        # ATTOM uses several casing conventions across endpoints — try each.
        ev_type = (
            ev.get("eventType")
            or ev.get("eventtype")
            or ev.get("type")
            or ev.get("category")
            or ""
        )
        ev_date = (
            ev.get("eventDate")
            or ev.get("eventdate")
            or ev.get("date")
            or ev.get("recordingDate")
            or ev.get("recordingdate")
            or ""
        )
        amount = _safe_float(
            ev.get("amount")
            or ev.get("eventAmount")
            or ev.get("eventamount")
            or ev.get("salesAmount")
            or ev.get("loanAmount")
        )
        lender_obj = ev.get("lender") if isinstance(ev.get("lender"), dict) else {}
        lender = (
            lender_obj.get("lastname", "")
            or lender_obj.get("lastName", "")
            or ev.get("lenderName", "")
            or ev.get("lendername", "")
            or ""
        )
        doc_number = str(
            ev.get("documentNumber", "")
            or ev.get("documentnumber", "")
            or ev.get("docNum", "")
            or ev.get("docnum", "")
            or ""
        )
        # ATTOM transferor = seller / outgoing party; transferee = buyer.
        transferor = (
            ev.get("transferor", "")
            or ev.get("seller", "")
            or ev.get("sellerName", "")
            or ""
        )
        transferee = (
            ev.get("transferee", "")
            or ev.get("buyer", "")
            or ev.get("buyerName", "")
            or ""
        )

        # Skip wholly empty entries — happens when ATTOM returns a placeholder.
        if not (ev_type or ev_date or amount or lender):
            continue

        history.append({
            "type": str(ev_type),
            "date": str(ev_date),
            "amount": amount,
            "lender": str(lender),
            "doc_number": doc_number,
            "transferor": str(transferor),
            "transferee": str(transferee),
        })

    # Newest first — ATTOM sometimes returns oldest-first depending on package.
    history.sort(key=lambda h: h.get("date") or "", reverse=True)
    return history


def normalize_from_attom_preforeclosure(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize ATTOM /property/v3/preforeclosuredetails response into a
    flat foreclosure_filing dict for the foreclosure card.

    Captures the most recent active filing only (NOD, lis pendens, or
    auction). Older filings are tracked separately in foreclosure_records.
    """
    props = data.get("property", [])
    if not props:
        return {}

    p = props[0] if isinstance(props, list) else props
    fc_block = (
        p.get("foreclosure", {})
        or p.get("preforeclosure", {})
        or p.get("preForeclosure", {})
        or {}
    )
    if not isinstance(fc_block, dict):
        return {}

    # Some ATTOM responses nest the active filing under `currentFiling` or
    # return the filing fields directly on the foreclosure block.
    filing = (
        fc_block.get("currentFiling", {})
        or fc_block.get("currentfiling", {})
        or fc_block
    )
    if not isinstance(filing, dict):
        return {}

    filing_date = (
        filing.get("recordingDate", "")
        or filing.get("recordingdate", "")
        or filing.get("filingDate", "")
        or filing.get("filingdate", "")
        or ""
    )
    default_amount = _safe_float(
        filing.get("defaultAmount")
        or filing.get("defaultamount")
        or filing.get("originalLoanAmount")
        or filing.get("originalloanamount")
    )
    lender_name = (
        filing.get("lenderName", "")
        or filing.get("lendername", "")
        or filing.get("trustee", "")
        or ""
    )
    auction_date = (
        filing.get("auctionDate", "")
        or filing.get("auctiondate", "")
        or filing.get("auctionDateTime", "")
        or ""
    )
    case_number = str(
        filing.get("caseNumber", "")
        or filing.get("casenumber", "")
        or filing.get("docNumber", "")
        or filing.get("docnumber", "")
        or ""
    )
    auction_location = (
        filing.get("auctionLocation", "")
        or filing.get("auctionlocation", "")
        or ""
    )
    distress_type = (
        filing.get("distressType", "")
        or filing.get("distresstype", "")
        or filing.get("filingType", "")
        or ""
    )

    if not (filing_date or default_amount or lender_name or auction_date):
        return {}

    return {
        "filing_date": str(filing_date),
        "default_amount": default_amount,
        "lender_name": str(lender_name),
        "auction_date": str(auction_date),
        "auction_location": str(auction_location),
        "case_number": case_number,
        "distress_type": str(distress_type),
    }


def normalize_from_attom_schools(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize ATTOM detailwithschools response — nearby schools."""
    props = data.get("property", [])
    if not props:
        return []

    p = props[0] if isinstance(props, list) else props
    schools_raw = p.get("school", [])
    schools = []
    for s in schools_raw:
        schools.append({
            "name": s.get("InstitutionName", s.get("institutionname", "")),
            "grade_range": s.get("gradeRange", s.get("Filetag", "")),
            "distance_miles": _safe_float(s.get("distance")),
        })
    return schools


def normalize_from_attom_expanded_profile(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize ATTOM expandedprofile — zoning, census, seller, REO, attic."""
    props = data.get("property", [])
    if not props:
        return {}

    p = props[0] if isinstance(props, list) else props
    lot = p.get("lot", {}) or {}
    area = p.get("area", {}) or {}
    sale = p.get("sale", {}) or {}
    summary = p.get("summary", {}) or {}
    building = p.get("building", {}) or {}
    construction = building.get("construction", {}) or {}
    assessment = p.get("assessment", {}) or {}
    tax = assessment.get("tax", {}) or {}
    exemption = tax.get("exemptiontype", {}) or {}

    return {
        "previous_owner_name": str(sale.get("sellerName", "")).strip().rstrip(","),
        "zoning_type": lot.get("zoningType", ""),
        "zoning_code": lot.get("siteZoningIdent", ""),
        "census_tract": area.get("censusTractIdent", "") or area.get("censustract", ""),
        "census_block_group": area.get("censusBlockGroup", "") or area.get("censusblockgroup", ""),
        "attic_sqft": _safe_int(building.get("size", {}).get("atticSize")),
        "major_improvements_year": _safe_int(
            construction.get("propertyStructureMajorImprovementsYear")
            or construction.get("propertystructuremajorimprovementsyear")
        ),
        "homeowner_exemption": exemption.get("Homeowner") == "Y" or exemption.get("homeowner") == "Y",
        "reo_flag": summary.get("REOflag") == "True" or summary.get("reoflag") == "True",
        "quit_claim_flag": summary.get("quitClaimFlag") == "True" or summary.get("quitclaimflag") == "True",
        # Legal description — ATTOM stores it on the property summary as
        # `legal1` (and sometimes legal2 for overflow). We concatenate when
        # both are present so the full legal text is preserved.
        "legal_description": " ".join(
            part.strip() for part in [
                str(summary.get("legal1", "") or ""),
                str(summary.get("legal2", "") or ""),
            ] if part and part.strip()
        ),
    }


def normalize_from_attom_valuation(data: dict[str, Any]) -> dict[str, Any]:
    """Legacy wrapper — delegates to normalize_from_attom_avm."""
    return normalize_from_attom_avm(data)


def normalize_from_attom_rental(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize ATTOM valuation/rental (Rental AVM) response."""
    props = data.get("property", [])
    if not props:
        return {}

    p = props[0] if isinstance(props, list) else props
    rental = p.get("rental", {}) or p.get("rentalAvm", {}) or {}

    return {
        "estimated_rent": _safe_float(rental.get("rentAmount") or rental.get("amount", {}).get("value")),
        "estimated_rent_high": _safe_float(rental.get("rentHigh") or rental.get("amount", {}).get("high")),
        "estimated_rent_low": _safe_float(rental.get("rentLow") or rental.get("amount", {}).get("low")),
    }


def normalize_from_attom_foreclosure(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize ATTOM saleshistory/expandedhistory foreclosure data.

    Returns dict with foreclosure_records list + stage classification.
    distressType: D = Default (NOD/Lis Pendens), T = Trustee Sale (NTS/NFS)
    """
    props = data.get("property", [])
    if not props:
        return {}

    p = props[0] if isinstance(props, list) else props
    fc_list = p.get("foreclosure", [])

    if not fc_list:
        return {"prior_foreclosure": False, "foreclosure_stage": "none"}

    _distress_labels = {
        "D": "Notice of Default / Lis Pendens",
        "T": "Trustee Sale / Notice of Foreclosure Sale",
    }

    records: list[ForeclosureRecord] = []
    for fc in fc_list:
        # Skip empty records (just sequence number)
        if len(fc) <= 1:
            continue
        trustor_first = fc.get("trustorFirstName", "")
        trustor_last = fc.get("trustorLastName", "")
        borrower = f"{trustor_first} {trustor_last}".strip()

        dtype = fc.get("distressType", "")
        records.append(ForeclosureRecord(
            recording_date=fc.get("recordingDate", ""),
            distress_type=dtype,
            distress_type_label=_distress_labels.get(dtype, dtype),
            borrower_name=borrower,
            trustee_name=fc.get("trusteeFirstName", ""),
            trustee_city=fc.get("trusteeCity", ""),
            trustee_state=fc.get("trusteeState", ""),
            lender_name=fc.get("beneficiaryName", ""),
            original_loan_amount=_safe_float(fc.get("originalLoanAmount")),
            original_loan_date=fc.get("originalLoanDate", ""),
            auction_date_time=fc.get("auctionDateTime", ""),
            auction_location=fc.get("auctionLocation", ""),
            opening_bid=_safe_float(fc.get("recordedOpeningBid")),
            document_number=fc.get("documentNumber", ""),
            trustee_sale_number=fc.get("trusteeSaleNumber", ""),
        ))

    # Classify foreclosure stage from most recent record
    stage = "none"
    if records:
        latest = records[0]  # Sorted by sequence (most recent first)
        if latest.distress_type == "T":
            stage = "auction"
        elif latest.distress_type == "D":
            stage = "default"
        else:
            stage = "filing"

    # Also pull expanded sale history from the same endpoint
    sh_list = p.get("saleHistory", p.get("salehistory", []))
    sale_history: list[dict[str, Any]] = []
    for sh in sh_list:
        amt = sh.get("amount", {})
        sale_history.append({
            "date": sh.get("saleTransDate", sh.get("saletransdate", "")),
            "amount": _safe_float(amt.get("saleAmt", amt.get("saleamt"))),
            "trans_type": amt.get("saleTransType", amt.get("saletranstype", "")),
            "buyer": sh.get("buyerName", ""),
            "seller": sh.get("sellerName", ""),
        })

    return {
        "foreclosure_records": records,
        "prior_foreclosure": True,
        "foreclosure_stage": stage,
        "sale_history_expanded": sale_history,
    }


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
