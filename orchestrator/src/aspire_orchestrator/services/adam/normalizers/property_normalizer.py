"""Normalize ATTOM API responses to PropertyRecord.

Follows ATTOM_FIELD_MAP.md from handoff. Each ATTOM endpoint returns
different response shapes — this normalizer maps them all to a single
canonical PropertyRecord.

Rule: Never invent parcel facts. If a field is missing, leave it empty.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.property_record import (
    PropertyRecord,
    SaleRecord,
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
