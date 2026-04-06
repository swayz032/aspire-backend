"""PropertyRecord — Canonical schema for U.S. property intelligence (ATTOM).

Source: ATTOM (primary), web evidence (fallback)
Dedup: normalized address + APN + ATTOM ID + FIPS+APN
Field map follows ATTOM_FIELD_MAP.md from handoff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution


@dataclass
class SaleRecord:
    """A single transaction in the property's sale history."""

    date: str = ""
    amount: float | None = None
    trans_type: str = ""
    buyer: str = ""
    seller: str = ""


@dataclass
class PropertyRecord:
    """Canonical property intelligence record."""

    # Identity
    normalized_address: str = ""
    address_confidence: float = 0.0
    parcel_apn: str = ""
    parcel_fips: str = ""
    attom_id: str = ""

    # Characteristics
    property_type: str = ""
    year_built: int | None = None
    living_sqft: int | None = None
    lot_sqft: int | None = None
    beds: int | None = None
    baths: float | None = None
    stories: int | None = None
    units_count: int | None = None
    construction_frame: str = ""
    roof_cover: str = ""
    quality: str = ""

    # Ownership
    owner_name: str = ""
    owner_type: str = ""
    mailing_address: str = ""
    absentee_owner_indicator: bool | None = None

    # Transactions
    last_sale_date: str = ""
    last_sale_amount: float | None = None
    sale_history: list[SaleRecord] = field(default_factory=list)
    price_per_sqft: float | None = None

    # Valuation
    estimated_value: float | None = None
    estimated_value_high: float | None = None
    estimated_value_low: float | None = None
    valuation_confidence: str = ""

    # Rental
    estimated_rent: float | None = None
    estimated_rent_high: float | None = None
    estimated_rent_low: float | None = None

    # Permits / Assessment
    permit_signals: list[str] = field(default_factory=list)
    assessment_context: str = ""

    # Geography / Schools
    school_district_name: str = ""
    school_context: str = ""
    geo_hierarchy: str = ""
    neighborhood: str = ""
    subdivision: str = ""
    zcta: str = ""

    # Freshness
    source_last_modified: str = ""

    # Verification
    verification_status: str = "unverified"
    confidence: float = 0.0
    sources: list[SourceAttribution] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if k == "sources":
                d[k] = [{"provider": s.provider, "retrieved_at": s.retrieved_at, "source_id": s.source_id} for s in v]
            elif k == "sale_history":
                d[k] = [{"date": s.date, "amount": s.amount, "trans_type": s.trans_type, "buyer": s.buyer, "seller": s.seller} for s in v]
            elif k == "extra":
                d.update(v)
            else:
                d[k] = v
        return d
