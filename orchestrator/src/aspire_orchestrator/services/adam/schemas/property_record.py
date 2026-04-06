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
class ForeclosureRecord:
    """A single foreclosure filing on the property.

    Source: ATTOM saleshistory/expandedhistory → foreclosure[] array
    distress_type: D = Default (NOD/Lis Pendens), T = Trustee Sale (NTS/NFS)
    """

    recording_date: str = ""
    distress_type: str = ""  # D = Default/NOD, T = Trustee Sale/NTS
    distress_type_label: str = ""  # Human-readable: "Notice of Default", "Trustee Sale"
    borrower_name: str = ""  # trustorFirstName + trustorLastName
    trustee_name: str = ""
    trustee_city: str = ""
    trustee_state: str = ""
    lender_name: str = ""  # beneficiaryName
    original_loan_amount: float | None = None
    original_loan_date: str = ""
    auction_date_time: str = ""  # "2014-08-20 3:30 PM"
    auction_location: str = ""  # "200 E Washington St"
    opening_bid: float | None = None  # recordedOpeningBid
    document_number: str = ""
    trustee_sale_number: str = ""


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
    owner_occupied: str = ""

    # Mortgage
    mortgage_lender: str = ""
    mortgage_amount: float | None = None
    mortgage_date: str = ""
    mortgage_loan_type: str = ""
    mortgage_term_months: int | None = None
    mortgage_due_date: str = ""
    deed_type: str = ""

    # Transactions
    last_sale_date: str = ""
    last_sale_amount: float | None = None
    sale_history: list[SaleRecord] = field(default_factory=list)
    price_per_sqft: float | None = None

    # Valuation (AVM)
    estimated_value: float | None = None
    estimated_value_high: float | None = None
    estimated_value_low: float | None = None
    valuation_confidence: str = ""
    avm_confidence_score: int | None = None  # 0-100 from ATTOM
    avm_fsd: float | None = None  # Forecast Standard Deviation %
    avm_price_per_sqft: float | None = None
    avm_date: str = ""

    # Equity (from homeequity endpoint)
    ltv_ratio: int | None = None  # Loan-to-value %
    available_equity: float | None = None
    lendable_equity: float | None = None
    current_loan_balance: float | None = None
    estimated_monthly_payment: float | None = None
    equity_last_updated: str = ""

    # Tax Assessment
    tax_assessed_total: float | None = None
    tax_assessed_land: float | None = None
    tax_assessed_improvement: float | None = None
    tax_market_value: float | None = None
    tax_market_land: float | None = None
    tax_market_improvement: float | None = None
    annual_tax_amount: float | None = None
    tax_year: int | None = None
    tax_per_sqft: float | None = None

    # Last Sale Detail
    last_sale_price_per_sqft: float | None = None
    last_sale_price_per_bed: float | None = None
    last_sale_type: str = ""  # Resale, New Construction, etc.
    last_sale_cash_or_mortgage: str = ""
    last_sale_arms_length: bool | None = None
    last_sale_doc_number: str = ""
    appreciation_pct: float | None = None  # Since last sale

    # Rental
    estimated_rent: float | None = None
    estimated_rent_high: float | None = None
    estimated_rent_low: float | None = None

    # Permits
    permit_signals: list[Any] = field(default_factory=list)  # list of permit dicts or strings
    assessment_context: str = ""

    # Nearby Comps (recent sales in same ZIP)
    nearby_comps: list[dict[str, Any]] = field(default_factory=list)

    # Schools
    nearby_schools: list[dict[str, Any]] = field(default_factory=list)
    school_district_name: str = ""
    school_context: str = ""

    # Foreclosure / Distress
    foreclosure_records: list[ForeclosureRecord] = field(default_factory=list)
    in_foreclosure: bool | None = None  # True if active foreclosure pipeline
    foreclosure_stage: str = ""  # "default", "auction", "reo", "none"
    prior_foreclosure: bool | None = None  # Property had past foreclosure

    # Previous Owner / Seller
    previous_owner_name: str = ""

    # Zoning & Census
    zoning_type: str = ""
    zoning_code: str = ""
    census_tract: str = ""
    census_block_group: str = ""

    # Additional Building Detail
    attic_sqft: int | None = None
    major_improvements_year: int | None = None
    homeowner_exemption: bool | None = None
    reo_flag: bool | None = None
    quit_claim_flag: bool | None = None

    # Geography
    geo_hierarchy: str = ""
    neighborhood: str = ""
    subdivision: str = ""
    county: str = ""
    zcta: str = ""
    latitude: float | None = None
    longitude: float | None = None

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
            elif k == "foreclosure_records":
                d[k] = [
                    {fk: fv for fk, fv in fr.__dict__.items() if fv}
                    for fr in v
                ]
            elif k == "sale_history":
                d[k] = [{"date": s.date, "amount": s.amount, "trans_type": s.trans_type, "buyer": s.buyer, "seller": s.seller} for s in v]
            elif k == "extra":
                d.update(v)
            else:
                d[k] = v
        return d
