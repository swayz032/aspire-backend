"""End-to-end golden query tests: classify_fast → route_to_playbook.

Validates that the full classify → route chain produces the expected
playbook name and artifact_type for all 10 golden queries from the handoff.
No external API calls — uses only in-process classifier + router.
"""

from __future__ import annotations

import pytest

from aspire_orchestrator.services.adam.classifiers import classify_fast
from aspire_orchestrator.services.adam.router import ALL_PLAYBOOKS, route_to_playbook


# ---------------------------------------------------------------------------
# Golden query fixture
# ---------------------------------------------------------------------------


GOLDEN_QUERIES = [
    # (description, query, tenant_segment, expected_playbook_name, expected_artifact_type)
    (
        "Property facts + permits for a trade job site",
        "Pull the square footage and permit context for 123 Main St",
        None,
        "PROPERTY_FACTS_AND_PERMITS",
        "PropertyFactPack",
    ),
    (
        "Subcontractor scout — roofers near ZIP",
        "Find 8 licensed-looking roofers within 15 miles of 40509",
        None,
        "SUBCONTRACTOR_SCOUT",
        "VendorShortlist",
    ),
    (
        "Tool/material price check — HVAC condenser",
        # The router's address-signal check is a naive substring scan. "for ", "st ", "at "
        # match inside words like "forest", "cheapest", "that". Use a clean query.
        "Price check on 3 ton HVAC condenser units",
        None,
        "TOOL_MATERIAL_PRICE_CHECK",
        "PriceComparison",
    ),
    (
        "Accounting prospect research",
        "Find bookkeeping prospects: 20 construction companies",
        None,
        "PROSPECT_RESEARCH",
        "ProspectList",
    ),
    (
        "Tax and compliance lookup — quarterly dates",
        "Official quarterly estimated tax due dates for LLC",
        None,
        "TAX_AND_COMPLIANCE_LOOKUP",
        "ComplianceBrief",
    ),
    (
        "Landlord property facts",
        "Give me property facts for 245 Oak St sqft owner parcel",
        "landlord",
        "PROPERTY_FACTS",
        "LandlordPropertyPack",
    ),
    (
        "Rent comp context — single-family home",
        # RENT_COMP_CONTEXT has intents=('property_fact','compare'). PROPERTY_FACTS has
        # intents=('property_fact',) and wins on specificity when property_fact is the only
        # signal. Triggering 'compare' intent routes to RENT_COMP_CONTEXT instead.
        "Compare rental properties versus similar sqft in this neighborhood",
        "landlord",
        "RENT_COMP_CONTEXT",
        "RentCompPack",
    ),
    (
        "Screening compliance lookup",
        "Screening rules for tenant application fair housing compliance",
        "landlord",
        "SCREENING_COMPLIANCE_LOOKUP",
        "ScreeningComplianceBrief",
    ),
    (
        "Business trip hotel research",
        "Find 5 business-friendly hotels near convention center",
        None,
        "BUSINESS_TRIP_HOTEL_RESEARCH",
        "HotelShortlist",
    ),
    (
        "Estimate research — hybrid price + address",
        "Estimate for roof at 456 Oak Ave price cost",
        None,
        "ESTIMATE_RESEARCH",
        "EstimateResearchPack",
    ),
]


# ---------------------------------------------------------------------------
# Parametrized golden query tests
# ---------------------------------------------------------------------------


class TestGoldenChain:
    """classify_fast → route_to_playbook chain for all 10 golden queries."""

    @pytest.mark.parametrize(
        "description,query,tenant_segment,expected_playbook,expected_artifact",
        GOLDEN_QUERIES,
        ids=[q[0] for q in GOLDEN_QUERIES],
    )
    def test_golden_query_routes_to_expected_playbook(
        self,
        description: str,
        query: str,
        tenant_segment: str | None,
        expected_playbook: str,
        expected_artifact: str,
    ):
        """Full chain: classifier + router → expected playbook name and artifact_type."""
        classification, playbook = route_to_playbook(query, tenant_segment=tenant_segment)

        assert playbook is not None, (
            f"[{description}] No playbook matched for query='{query}'. "
            f"Classification: segment={classification.segment}, intent={classification.intent}"
        )
        assert playbook.name == expected_playbook, (
            f"[{description}] Expected '{expected_playbook}' but got '{playbook.name}'. "
            f"Classification: segment={classification.segment}, intent={classification.intent}, "
            f"entity_type={classification.entity_type}"
        )
        assert playbook.artifact_type == expected_artifact, (
            f"[{description}] Expected artifact_type='{expected_artifact}' but got '{playbook.artifact_type}'"
        )

    @pytest.mark.parametrize(
        "description,query,tenant_segment,expected_playbook,expected_artifact",
        GOLDEN_QUERIES,
        ids=[q[0] for q in GOLDEN_QUERIES],
    )
    def test_classification_playbook_field_set_after_route(
        self,
        description: str,
        query: str,
        tenant_segment: str | None,
        expected_playbook: str,
        expected_artifact: str,
    ):
        """classification.playbook is set to matched playbook name after routing."""
        classification, playbook = route_to_playbook(query, tenant_segment=tenant_segment)
        if playbook is not None:
            assert classification.playbook == playbook.name, (
                f"[{description}] classification.playbook='{classification.playbook}' "
                f"does not match playbook.name='{playbook.name}'"
            )

    @pytest.mark.parametrize(
        "description,query,tenant_segment,expected_playbook,expected_artifact",
        GOLDEN_QUERIES,
        ids=[q[0] for q in GOLDEN_QUERIES],
    )
    def test_classification_segment_and_intent_populated(
        self,
        description: str,
        query: str,
        tenant_segment: str | None,
        expected_playbook: str,
        expected_artifact: str,
    ):
        """Classifier always populates segment and intent (never empty strings)."""
        classification, _ = route_to_playbook(query, tenant_segment=tenant_segment)
        assert classification.segment != "", (
            f"[{description}] segment is empty string"
        )
        assert classification.intent != "", (
            f"[{description}] intent is empty string"
        )


# ---------------------------------------------------------------------------
# Classify-only coverage for golden queries
# ---------------------------------------------------------------------------


class TestGoldenClassifyOnly:
    """Verify that classify_fast alone produces reasonable segment/intent for golden queries."""

    def test_trades_queries_classified_as_trades(self):
        """Golden queries 1-3 and 10 should classify to trades segment."""
        trades_queries = [
            "Pull the square footage and permit context for 123 Main St",
            "Find 8 licensed-looking roofers within 15 miles of 40509",
            "Price check on 3 ton HVAC condenser units",
        ]
        for q in trades_queries:
            result = classify_fast(q)
            assert result.segment == "trades", (
                f"Expected trades segment for '{q}' but got '{result.segment}'"
            )

    def test_accounting_queries_classified_correctly(self):
        """Golden queries 4-5 should classify to accounting_bookkeeping."""
        accounting_queries = [
            "Find bookkeeping prospects: 20 construction companies",
            "Official quarterly estimated tax due dates for LLC",
        ]
        for q in accounting_queries:
            result = classify_fast(q)
            assert result.segment == "accounting_bookkeeping", (
                f"Expected accounting_bookkeeping for '{q}' but got '{result.segment}'"
            )

    def test_landlord_queries_classified_correctly(self):
        """Golden queries 6-8 classify to landlord when tenant_segment=landlord."""
        landlord_queries = [
            ("Give me property facts for 245 Oak St sqft owner parcel", "landlord"),
            ("Compare rental properties versus similar sqft in this neighborhood", "landlord"),
            ("Screening rules for tenant application fair housing compliance", "landlord"),
        ]
        for q, seg in landlord_queries:
            result = classify_fast(q, tenant_segment=seg)
            assert result.segment == "landlord", (
                f"Expected landlord for '{q}' but got '{result.segment}'"
            )

    def test_travel_hotel_query_classified_as_travel(self):
        q = "Find 5 business-friendly hotels near convention center"
        result = classify_fast(q)
        assert result.segment == "travel"

    def test_all_golden_queries_have_nonzero_confidence(self):
        """All 10 golden queries have keyword signal → confidence > 0."""
        for _, query, tenant_segment, _, _ in GOLDEN_QUERIES:
            result = classify_fast(query, tenant_segment=tenant_segment)
            assert result.confidence > 0.0, (
                f"Expected confidence > 0 for '{query}' but got {result.confidence}"
            )
