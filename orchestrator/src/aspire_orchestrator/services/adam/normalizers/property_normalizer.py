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
    summary = building.get("summary", {}) or {}
    rooms = building.get("rooms", {}) or {}
    lot = p.get("lot", {}) or {}
    assessment = p.get("assessment", {}) or {}
    owner = p.get("owner", {}) or {}
    sale = p.get("sale", {}) or {}
    vintage = p.get("vintage", {}) or {}

    return PropertyRecord(
        normalized_address=f"{address.get('oneLine', '')}",
        parcel_apn=str(p.get("identifier", {}).get("apn", "") or ""),
        parcel_fips=str(p.get("identifier", {}).get("fips", "") or ""),
        attom_id=str(p.get("identifier", {}).get("attomId", "") or ""),
        property_type=summary.get("proptype", "") or p.get("summary", {}).get("proptype", ""),
        year_built=_safe_int(summary.get("yearbuilt") or p.get("summary", {}).get("yearbuilt")),
        living_sqft=_safe_int(building.get("size", {}).get("livingsize") or summary.get("livingsize")),
        lot_sqft=_safe_int(lot.get("lotsize2") or lot.get("lotsize1")),
        beds=_safe_int(rooms.get("beds") or summary.get("beds")),
        baths=_safe_float(rooms.get("bathstotal") or summary.get("baths")),
        stories=_safe_int(summary.get("levels")),
        units_count=_safe_int(summary.get("unitsCount")),
        construction_frame=building.get("construction", {}).get("frameType", ""),
        roof_cover=building.get("roof", {}).get("cover", ""),
        quality=summary.get("quality", ""),
        owner_name=f"{owner.get('owner1', {}).get('fullName', '')}".strip(),
        owner_type=owner.get("corporateIndicator", ""),
        mailing_address=owner.get("mailAddress", {}).get("oneLine", "") if owner.get("mailAddress") else "",
        absentee_owner_indicator=owner.get("absenteeOwnerStatus") == "A" if owner.get("absenteeOwnerStatus") else None,
        last_sale_date=str(sale.get("saleTransDate", "") or ""),
        last_sale_amount=_safe_float(sale.get("saleAmountData", {}).get("saleAmt") if sale.get("saleAmountData") else sale.get("amount", {}).get("saleAmt")),
        assessment_context=str(assessment.get("assessed", {}).get("assdTtlValue", "")) if assessment.get("assessed") else "",
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


def normalize_from_attom_valuation(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize ATTOM valuation/homeequity (AVM) response."""
    props = data.get("property", [])
    if not props:
        return {}

    p = props[0] if isinstance(props, list) else props
    avm = p.get("assessment", {}).get("market", {}) or p.get("avm", {}) or {}

    return {
        "estimated_value": _safe_float(avm.get("mktTtlValue") or avm.get("amount", {}).get("value")),
        "estimated_value_high": _safe_float(avm.get("mktTtlValueHigh") or avm.get("amount", {}).get("high")),
        "estimated_value_low": _safe_float(avm.get("mktTtlValueLow") or avm.get("amount", {}).get("low")),
        "valuation_confidence": str(avm.get("confidence", "")),
    }


def normalize_from_attom_rental(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize ATTOM valuation/rental (Rental AVM) response."""
    props = data.get("property", [])
    if not props:
        return {}

    p = props[0] if isinstance(props, list) else props
    rental = p.get("rental", {}) or {}

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
