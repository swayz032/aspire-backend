"""Tests for Adam segment + intent classifiers (classifiers.py).

Validates: keyword-based fast classification, geo scope detection,
fallback to general_smb when no keywords match.
All tests are synchronous — classify_fast() is deterministic, zero-cost.
"""

from __future__ import annotations

import pytest

from aspire_orchestrator.services.adam.classifiers import (
    ClassificationResult,
    classify_fast,
)


# ---------------------------------------------------------------------------
# Segment classification
# ---------------------------------------------------------------------------


class TestSegmentClassification:
    """classify_fast assigns the correct segment for known keyword sets."""

    def test_trades_keyword_roofing(self):
        result = classify_fast("Find licensed roofers within 15 miles")
        assert result.segment == "trades"

    def test_trades_keyword_hvac(self):
        result = classify_fast("HVAC contractor prices for 3 ton condenser")
        assert result.segment == "trades"

    def test_trades_keyword_plumbing(self):
        result = classify_fast("plumb repair estimate near downtown")
        assert result.segment == "trades"

    def test_trades_keyword_estimate(self):
        result = classify_fast("estimate for flooring job at 456 Oak Ave")
        assert result.segment == "trades"

    def test_landlord_keyword_rent(self):
        result = classify_fast("rental vacancy tenant screening for this single-family home")
        assert result.segment == "landlord"

    def test_landlord_keyword_tenant_screening(self):
        result = classify_fast("Screening rules for tenant application fair housing")
        assert result.segment == "landlord"

    def test_landlord_keyword_property_detail(self):
        result = classify_fast("Give me property facts for 245 Oak St sqft owner parcel")
        assert result.segment == "landlord"

    def test_landlord_keyword_vacancy(self):
        result = classify_fast("rental vacancy turnover maintenance request")
        assert result.segment == "landlord"

    def test_accounting_keyword_bookkeeping(self):
        result = classify_fast("Find bookkeeping prospects 20 construction companies")
        assert result.segment == "accounting_bookkeeping"

    def test_accounting_keyword_quarterly_tax(self):
        result = classify_fast("Official quarterly estimated tax due dates for LLC")
        assert result.segment == "accounting_bookkeeping"

    def test_accounting_keyword_cpa(self):
        result = classify_fast("CPA looking for new clients reconcil accounts")
        assert result.segment == "accounting_bookkeeping"

    def test_travel_keyword_hotel(self):
        result = classify_fast("Find 5 business-friendly hotels near convention center")
        assert result.segment == "travel"

    def test_travel_keyword_business_trip(self):
        result = classify_fast("business trip lodging near airport parking breakfast")
        assert result.segment == "travel"

    def test_unknown_query_fallback_to_general_smb(self):
        """Completely unrecognized query must fall back to general_smb."""
        result = classify_fast("xyzzy frob quux wibble wobble")
        assert result.segment == "general_smb"

    def test_empty_query_fallback_to_general_smb(self):
        """Empty query falls back to general_smb + lookup."""
        result = classify_fast("")
        assert result.segment == "general_smb"
        assert result.intent == "lookup"

    def test_tenant_segment_used_as_default_when_no_keywords(self):
        """When no keywords match, tenant_segment overrides general_smb default."""
        result = classify_fast("xyzzy frob", tenant_segment="landlord")
        assert result.segment == "landlord"

    def test_keyword_match_beats_tenant_segment(self):
        """Strong keyword signal overrides tenant_segment hint."""
        result = classify_fast("Find HVAC contractors for estimate", tenant_segment="landlord")
        assert result.segment == "trades"


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------


class TestIntentClassification:
    """classify_fast assigns the correct intent for known keyword sets."""

    def test_price_check_intent_from_price_keyword(self):
        result = classify_fast("Compare price for a 3 ton condenser")
        assert result.intent == "price_check"

    def test_price_check_intent_from_cost_keyword(self):
        result = classify_fast("how much does a new furnace cost installed")
        assert result.intent == "price_check"

    def test_price_check_intent_from_estimate_cost(self):
        result = classify_fast("estimate cost for drywall repair")
        assert result.intent == "price_check"

    def test_property_fact_intent_sqft(self):
        result = classify_fast("Pull the square footage and permit context for 123 Main St")
        assert result.intent == "property_fact"

    def test_property_fact_intent_year_built(self):
        result = classify_fast("what year built and lot size for this parcel assessed value")
        assert result.intent == "property_fact"

    def test_compare_intent(self):
        result = classify_fast("compare competitor HVAC contractors versus us ranking")
        assert result.intent == "compare"

    def test_compliance_lookup_intent(self):
        result = classify_fast("What are the compliance requirements filing deadline for IRS")
        assert result.intent == "compliance_lookup"

    def test_hotel_research_intent(self):
        result = classify_fast("Find hotel accommodation near convention center for business trip")
        assert result.intent == "hotel_research"

    def test_prospect_research_intent(self):
        result = classify_fast("Find prospect clients leads for new business pipeline")
        assert result.intent == "prospect_research"

    def test_verify_intent(self):
        result = classify_fast("verify the license and insurance of this contractor BBB complaint")
        assert result.intent == "verify"

    def test_lookup_is_default_when_no_intent_keywords(self):
        result = classify_fast("xyzzy frob quux wibble wobble")
        assert result.intent == "lookup"


# ---------------------------------------------------------------------------
# Entity type mapping
# ---------------------------------------------------------------------------


class TestEntityTypeMapping:
    """Entity type is correctly derived from intent."""

    def test_price_check_maps_to_product(self):
        result = classify_fast("price for a 3 ton condenser material cost")
        assert result.entity_type == "product"

    def test_property_fact_maps_to_property(self):
        result = classify_fast("square footage lot size year built parcel assessed")
        assert result.entity_type == "property"

    def test_hotel_research_maps_to_hotel(self):
        result = classify_fast("hotel accommodation lodging near airport business trip")
        assert result.entity_type == "hotel"

    def test_prospect_research_maps_to_business(self):
        result = classify_fast("Find prospect clients leads potential business pipeline")
        assert result.entity_type == "business"

    def test_compliance_lookup_maps_to_web(self):
        result = classify_fast("compliance regulation law rule filing deadline")
        assert result.entity_type == "web"

    def test_lookup_maps_to_web(self):
        result = classify_fast("xyzzy frob quux")
        assert result.entity_type == "web"


# ---------------------------------------------------------------------------
# Geo scope detection
# ---------------------------------------------------------------------------


class TestGeoScopeDetection:
    """_detect_geo_scope correctly identifies geographic scope from query."""

    def test_zip_code_detected(self):
        result = classify_fast("Find roofers within 15 miles of 40509")
        assert result.geo_scope == "zip"

    def test_five_digit_zip_in_query(self):
        result = classify_fast("contractors near 90210")
        assert result.geo_scope == "zip"

    def test_near_me_detected_as_radius(self):
        result = classify_fast("plumbers near me")
        assert result.geo_scope == "radius"

    def test_within_miles_detected_as_radius(self):
        result = classify_fast("subcontractors within 20 miles")
        assert result.geo_scope == "radius"

    def test_nearby_detected_as_radius(self):
        result = classify_fast("HVAC techs nearby")
        assert result.geo_scope == "radius"

    def test_county_detected(self):
        result = classify_fast("contractors in Fayette county")
        assert result.geo_scope == "county"

    def test_state_detected(self):
        result = classify_fast("statewide plumbing regulations")
        assert result.geo_scope == "state"

    def test_national_detected(self):
        result = classify_fast("nationwide compliance requirements")
        assert result.geo_scope == "national"

    def test_no_geo_signal_returns_empty_string(self):
        result = classify_fast("find a local electrician")
        # "local" does not trigger any geo pattern
        assert result.geo_scope == ""


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


class TestConfidenceScoring:
    """Confidence is 0.0 for no matches, > 0 for keyword matches."""

    def test_no_keywords_produces_zero_confidence(self):
        result = classify_fast("xyzzy frob quux wibble wobble")
        assert result.confidence == 0.0

    def test_strong_keyword_match_produces_nonzero_confidence(self):
        result = classify_fast("HVAC contractor estimate price condenser furnace repair install permit")
        assert result.confidence > 0.0

    def test_confidence_capped_at_one(self):
        # Spam many keywords
        query = " ".join([
            "hvac plumb electric roof paint landscap contractor handyman flooring tile",
            "price cost estimate compare compare best top rated",
        ])
        result = classify_fast(query)
        assert result.confidence <= 1.0


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


class TestReturnType:
    """classify_fast always returns a ClassificationResult dataclass."""

    def test_returns_classification_result_instance(self):
        result = classify_fast("anything")
        assert isinstance(result, ClassificationResult)

    def test_playbook_field_empty_on_classify_fast(self):
        """Playbook is resolved by the router, not the classifier."""
        result = classify_fast("HVAC price estimate")
        assert result.playbook == ""
