"""Adam Research Platform — Full Scenario Test Suite.

Runs realistic user scenarios through the complete pipeline:
classify -> route -> dispatch -> provider (mocked) -> normalize -> verify -> artifact

Shows the full ResearchResponse for each scenario including records, confidence,
missing fields, verification report, and cost estimates.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.services.adam.playbooks import dispatch_playbook
from aspire_orchestrator.services.adam.router import route_to_playbook
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.telemetry import estimate_provider_cost


CTX = PlaybookContext(
    suite_id="demo-plumber-001",
    office_id="office-lex-001",
    correlation_id="demo-2026-04-06",
    tenant_id="demo-plumber-001",
)


def _ok(tool_id: str, data: dict) -> ToolExecutionResult:
    return ToolExecutionResult(outcome=Outcome.SUCCESS, tool_id=tool_id, data=data)


def _fail(tool_id: str, error: str) -> ToolExecutionResult:
    return ToolExecutionResult(outcome=Outcome.FAILED, tool_id=tool_id, error=error)


# ============================================================================
# MOCK DATA — realistic provider responses
# ============================================================================

ATTOM_DETAIL = {
    "property": [{
        "identifier": {"Id": 98765, "fips": "21067", "apn": "789-012-345"},
        "address": {
            "oneLine": "456 Oak Ave, Lexington, KY 40509",
            "locality": "Lexington",
            "countrySubd": "KY",
            "postal1": "40509",
        },
        "summary": {"yearBuilt": 2003, "propType": "SFR", "propSubType": "Residential"},
        "building": {
            "size": {"livingSize": 2200, "universalSize": 2600},
            "rooms": {"beds": 4, "bathsFull": 2, "bathsHalf": 1},
        },
        "lot": {"lotSize1": 0.33, "lotSize2": 14375},
        "assessment": {"assessed": {"assdTtlValue": 245000}},
        "vintage": {"lastModified": "2026-03-01"},
    }],
}

ATTOM_SALES = {
    "property": [{
        "saleTransactionType": "Resale",
        "amount": {"saleAmt": 310000},
        "date": {"saleTransDate": "2025-08-20"},
        "buyer": {"fullName": "John Smith"},
        "seller": {"fullName": "Jane Doe"},
    }],
}

ATTOM_VALUATION = {
    "property": [{
        "avm": {"amount": {"value": 325000, "high": 340000, "low": 310000}},
        "address": {"oneLine": "456 Oak Ave, Lexington, KY 40509"},
    }],
}

ATTOM_RENTAL = {
    "property": [{
        "rentalAvm": {"rentAmount": 1850, "rentHigh": 2100, "rentLow": 1600},
        "address": {"oneLine": "456 Oak Ave, Lexington, KY 40509"},
    }],
}

ATTOM_SCHOOLS = {
    "property": [{
        "identifier": {"Id": 98765},
        "address": {"oneLine": "456 Oak Ave, Lexington, KY 40509"},
        "summary": {"yearBuilt": 2003, "propType": "SFR"},
        "building": {
            "size": {"livingSize": 2200},
            "rooms": {"beds": 4, "bathsFull": 2, "bathsHalf": 1},
        },
        "lot": {"lotSize1": 0.33},
        "assessment": {"assessed": {"assdTtlValue": 245000}},
    }],
}

SHOPPING_CONDENSER = {
    "results": [
        {
            "title": "Goodman GSX140361 3 Ton 14 SEER Condenser",
            "price": "$1,849.00",
            "extracted_price": 1849.0,
            "source": "Alpine Home Air",
            "rating": 4.5,
            "reviews": 127,
            "delivery": "Free shipping",
            "thumbnail": "https://cdn.alpinehomeair.com/goodman-3ton.jpg",
            "product_id": "GSX140361",
        },
        {
            "title": "Carrier 24ACC636A003 3 Ton 16 SEER Condenser",
            "price": "$2,399.00",
            "extracted_price": 2399.0,
            "source": "?"
        },
        {
            "title": "Rheem RA1636AJ1NA 3 Ton 16 SEER Condenser",
            "price": "$1,999.00",
            "extracted_price": 1999.0,
            "source": "?"
        },
    ],
    "query": "3 ton condenser",
    "result_count": 3,
}

HOMEDEPOT_CONDENSER = {
    "results": [
        {
            "title": "MRCOOL 3 Ton 14.3 SEER2 Central Air Conditioner Condenser",
            "brand": "MRCOOL",
            "model_number": "MAC14036A",
            "price": 1699.0,
            "price_was": 1899.0,
            "price_saving": 200.0,
            "percentage_off": 11,
            "rating": 4.6,
            "reviews": 89,
            "link": "https://homedepot.com/p/mrcool-3ton",
            "pickup": {"quantity": 3},
            "delivery": {"has_delivery": True},
            "thumbnails": [{"link": "https://images.homedepot.com/mrcool.jpg"}],
        },
        {
            "title": "Goodman 3 Ton 14.3 SEER2 Condenser",
            "brand": "Goodman",
            "model_number": "GSXN403610",
            "price": 1549.0,
            "rating": 4.4,
            "reviews": 203,
            "link": "https://homedepot.com/p/goodman-3ton",
            "pickup": {"quantity": 7},
            "delivery": {"has_delivery": True},
            "thumbnails": [{"link": "https://images.homedepot.com/goodman.jpg"}],
        },
    ],
    "query": "3 ton condenser",
    "result_count": 2,
}

GOOGLE_PLACES_ROOFERS = {
    "results": [
        {
            "name": "Lexington Blue Roofing",
            "formatted_address": "1200 Industry Rd, Lexington, KY 40505",
            "formatted_phone_number": "(859) 555-7890",
            "website": "https://lexblue.com",
            "rating": 4.8,
            "user_ratings_total": 156,
            "types": ["roofing_contractor"],
        },
        {
            "name": "Bluegrass Exteriors",
            "formatted_address": "340 New Circle Rd, Lexington, KY 40505",
            "formatted_phone_number": "(859) 555-4321",
            "website": "https://bluegrassexteriors.com",
            "rating": 4.5,
            "user_ratings_total": 89,
            "types": ["roofing_contractor", "general_contractor"],
        },
        {
            "name": "CentiMark Corporation",
            "formatted_address": "2100 Palumbo Dr, Lexington, KY 40509",
            "formatted_phone_number": "(859) 555-0000",
            "rating": 4.2,
            "user_ratings_total": 34,
            "types": ["roofing_contractor"],
        },
    ],
}

EXA_TAX_COMPLIANCE = {
    "results": [
        {
            "title": "Estimated Tax Due Dates for 2026 | IRS.gov",
            "url": "https://www.irs.gov/businesses/small-businesses-self-employed/estimated-taxes",
            "text": (
                "If you are filing as a sole proprietor, partner, S corporation shareholder, "
                "or self-employed individual, you generally have to make estimated tax payments. "
                "2026 Payment Due Dates: 1st Quarter (Jan 1 - Mar 31) = April 15, 2026. "
                "2nd Quarter (Apr 1 - May 31) = June 16, 2026. "
                "3rd Quarter (Jun 1 - Aug 31) = September 15, 2026. "
                "4th Quarter (Sep 1 - Dec 31) = January 15, 2027."
            ),
            "publishedDate": "2026-01-15",
            "score": 0.97,
        },
        {
            "title": "Form 1040-ES: Estimated Tax for Individuals",
            "url": "https://www.irs.gov/forms-pubs/about-form-1040-es",
            "text": "Use Form 1040-ES to figure and pay your estimated tax.",
            "publishedDate": "2026-01-01",
            "score": 0.89,
        },
    ],
    "costDollars": {"total": 0.004},
}

BRAVE_COMPLIANCE = {
    "results": [
        {
            "title": "LLC Quarterly Tax Requirements - Nolo",
            "url": "https://www.nolo.com/llc-taxes",
            "description": "LLCs must pay quarterly estimated taxes if they expect to owe $1,000 or more.",
        },
    ],
}

EXA_PROSPECTS = {
    "results": [
        {
            "title": "ABC Construction LLC - Lexington KY",
            "url": "https://abcconstruction-lex.com",
            "text": "ABC Construction specializes in commercial and residential construction in central Kentucky. Founded 2015, 25 employees.",
            "publishedDate": "2025-11-01",
            "score": 0.88,
        },
    ],
    "costDollars": {"total": 0.003},
}

GOOGLE_PLACES_PROSPECTS = {
    "results": [
        {
            "name": "Builder's First Choice LLC",
            "formatted_address": "890 Winchester Rd, Lexington, KY 40505",
            "formatted_phone_number": "(859) 555-9876",
            "website": "https://buildersfirstchoice.com",
            "rating": 4.3,
            "user_ratings_total": 67,
            "types": ["general_contractor"],
        },
        {
            "name": "Central KY Framing Inc",
            "formatted_address": "1500 Versailles Rd, Lexington, KY 40504",
            "formatted_phone_number": "(859) 555-1111",
            "rating": 4.0,
            "user_ratings_total": 23,
            "types": ["general_contractor"],
        },
    ],
}

TRIPADVISOR_HOTELS = {
    "data": [
        {
            "location_id": "T001",
            "name": "Hilton Lexington Downtown",
            "address_obj": {
                "street1": "369 W Vine St",
                "city": "Lexington",
                "state": "Kentucky",
                "postalcode": "40507",
                "address_string": "369 W Vine St, Lexington, KY 40507",
            },
            "rating": "4.5",
            "num_reviews": "2341",
            "hotel_class": "4.0",
            "price_level": "$$$",
            "subcategory": [{"name": "hotel"}],
            "category": {"name": "hotel"},
        },
        {
            "location_id": "T002",
            "name": "21c Museum Hotel Lexington",
            "address_obj": {
                "street1": "167 W Main St",
                "city": "Lexington",
                "state": "Kentucky",
                "postalcode": "40507",
                "address_string": "167 W Main St, Lexington, KY 40507",
            },
            "rating": "4.5",
            "num_reviews": "1876",
            "hotel_class": "4.0",
            "price_level": "$$$$",
            "subcategory": [{"name": "hotel"}],
            "category": {"name": "hotel"},
        },
    ],
}

GOOGLE_PLACES_HOTELS = {
    "results": [
        {
            "name": "Hyatt Regency Lexington",
            "formatted_address": "401 W High St, Lexington, KY 40507",
            "formatted_phone_number": "(859) 253-1234",
            "website": "https://hyatt.com/lexington",
            "rating": 4.3,
            "user_ratings_total": 1200,
            "types": ["lodging", "hotel"],
        },
    ],
}

EXA_HOTELS = {
    "results": [
        {
            "title": "Best Hotels in Lexington KY for Business Travelers 2026",
            "url": "https://example.com/best-hotels-lex",
            "text": "Top picks: Hilton Downtown (convention access), 21c Museum Hotel (boutique), Hyatt Regency (airport shuttle)",
            "score": 0.82,
        },
    ],
    "costDollars": {"total": 0.002},
}


# ============================================================================
# SCENARIO RUNNERS
# ============================================================================

def _print_header(num, total, title, query, segment=None):
    print()
    print(f"{'=' * 90}")
    print(f"  SCENARIO {num}/{total}: {title}")
    print(f"  Query: \"{query}\"")
    if segment:
        print(f"  Tenant segment: {segment}")
    print(f"{'=' * 90}")


def _print_classification(cls, playbook):
    print(f"\n  CLASSIFICATION:")
    print(f"    Segment:   {cls.segment}")
    print(f"    Intent:    {cls.intent}")
    print(f"    Entity:    {cls.entity_type}")
    print(f"    Geo:       {cls.geo_scope or 'none'}")
    print(f"    Confidence: {cls.confidence:.0%}")

    if playbook:
        est = sum(estimate_provider_cost(p) for p in playbook.provider_order)
        print(f"\n  PLAYBOOK SELECTED:")
        print(f"    Name:      {playbook.name}")
        print(f"    Artifact:  {playbook.artifact_type}")
        print(f"    Providers: {', '.join(playbook.provider_order)}")
        print(f"    Max calls: {playbook.max_provider_calls}")
        print(f"    Est cost:  ${est:.3f}")
    else:
        print(f"\n  PLAYBOOK: None (would fallback to legacy Adam)")


def _print_response(resp):
    print(f"\n  RESPONSE:")
    print(f"    Artifact:    {resp.artifact_type}")
    print(f"    Records:     {len(resp.records)}")
    print(f"    Providers:   {', '.join(resp.providers_called)}")
    print(f"    Confidence:  {resp.confidence}")
    print(f"    Missing:     {resp.missing_fields or 'none'}")
    print(f"    Cost est:    ${resp.cost_estimate:.4f}")

    if resp.verification_report:
        vr = resp.verification_report
        print(f"\n  VERIFICATION REPORT:")
        print(f"    Status:      {vr.status}")
        print(f"    Score:       {vr.confidence_score:.2f}")
        print(f"    Sources:     {vr.source_count}")
        print(f"    Conflicts:   {len(vr.conflicts)}")
        print(f"    Missing:     {vr.missing_fields or 'none'}")
        if vr.conflicts:
            for c in vr.conflicts[:3]:  # Show max 3
                providers = [v.get("provider", "?") for v in c.values]
                vals = [str(v.get("value", "?")) for v in c.values]
                print(f"      CONFLICT: {c.field_name} -> {', '.join(vals)} (from: {', '.join(providers)})")

    if resp.records:
        print(f"\n  RECORDS (first 3):")
        for i, rec in enumerate(resp.records[:3]):
            # Show key fields depending on artifact type
            if isinstance(rec, dict):
                name = rec.get("product_name") or rec.get("name") or rec.get("normalized_address") or rec.get("title", "?")
                price = rec.get("price") or rec.get("extracted_price")
                rating = rec.get("rating") or rec.get("traveler_rating")
                stock = rec.get("in_store_stock")
                line = f"    [{i+1}] {name}"
                if price:
                    line += f" | ${price}"
                if rating:
                    line += f" | rating: {rating}"
                if stock is not None:
                    line += f" | in-stock: {stock}"
                print(line)

    if resp.next_queries:
        print(f"\n  SUGGESTED NEXT QUERIES:")
        for nq in resp.next_queries[:3]:
            print(f"    -> {nq}")


async def run_scenarios():
    total = 7
    results = []

    # ------------------------------------------------------------------
    # 1. TRADES: HVAC condenser price check (Shopping + Home Depot)
    # ------------------------------------------------------------------
    query = "How much is a 3 ton condenser? I need pricing for a quote"
    _print_header(1, total, "TRADES — Price Check (Condenser)", query)
    cls, pb = route_to_playbook(query)
    _print_classification(cls, pb)

    with patch(
        "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
        new_callable=AsyncMock, return_value=_ok("serpapi_shopping.search", SHOPPING_CONDENSER),
    ), patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        new_callable=AsyncMock, return_value=_ok("serpapi_homedepot.search", HOMEDEPOT_CONDENSER),
    ):
        resp = await dispatch_playbook(pb.name, query, CTX)
    _print_response(resp)
    results.append(("Price Check", pb.name if pb else "NONE", resp.artifact_type, len(resp.records), resp.confidence.get("status", "?")))

    # ------------------------------------------------------------------
    # 2. TRADES: Property facts for a job site
    # ------------------------------------------------------------------
    query = "Pull property details and square footage for 456 Oak Ave Lexington KY"
    _print_header(2, total, "TRADES — Property Facts & Permits", query)
    cls, pb = route_to_playbook(query)
    _print_classification(cls, pb)

    with patch(
        "aspire_orchestrator.providers.attom_client.execute_attom_property_detail",
        new_callable=AsyncMock, return_value=_ok("attom.property_detail", ATTOM_DETAIL),
    ), patch(
        "aspire_orchestrator.providers.attom_client.execute_attom_sales_history",
        new_callable=AsyncMock, return_value=_ok("attom.sales_history", ATTOM_SALES),
    ):
        resp = await dispatch_playbook(pb.name, query, CTX)
    _print_response(resp)
    results.append(("Property Facts", pb.name if pb else "NONE", resp.artifact_type, len(resp.records), resp.confidence.get("status", "?")))

    # ------------------------------------------------------------------
    # 3. TRADES: Subcontractor scout — find roofers
    # ------------------------------------------------------------------
    query = "Find me 5 good roofers near Lexington KY with reviews"
    _print_header(3, total, "TRADES — Subcontractor Scout (Roofers)", query)
    cls, pb = route_to_playbook(query)
    _print_classification(cls, pb)

    with patch(
        "aspire_orchestrator.services.search_router.route_places_search",
        new_callable=AsyncMock, return_value=_ok("google_places.search", GOOGLE_PLACES_ROOFERS),
    ):
        resp = await dispatch_playbook(pb.name, query, CTX)
    _print_response(resp)
    results.append(("Sub Scout", pb.name if pb else "NONE", resp.artifact_type, len(resp.records), resp.confidence.get("status", "?")))

    # ------------------------------------------------------------------
    # 4. ACCOUNTING: Tax compliance lookup
    # ------------------------------------------------------------------
    query = "When are quarterly estimated tax payments due for my LLC this year?"
    _print_header(4, total, "ACCOUNTING — Tax Compliance Lookup", query)
    cls, pb = route_to_playbook(query)
    _print_classification(cls, pb)

    with patch(
        "aspire_orchestrator.providers.exa_client.execute_exa_search",
        new_callable=AsyncMock, return_value=_ok("exa.search", EXA_TAX_COMPLIANCE),
    ), patch(
        "aspire_orchestrator.services.search_router.route_web_search",
        new_callable=AsyncMock, return_value=_ok("brave.search", BRAVE_COMPLIANCE),
    ):
        resp = await dispatch_playbook(pb.name, query, CTX)
    _print_response(resp)
    results.append(("Tax Compliance", pb.name if pb else "NONE", resp.artifact_type, len(resp.records), resp.confidence.get("status", "?")))

    # ------------------------------------------------------------------
    # 5. ACCOUNTING: Prospect research
    # ------------------------------------------------------------------
    query = "Find bookkeeping prospects: construction companies in Lexington KY"
    _print_header(5, total, "ACCOUNTING — Prospect Research", query)
    cls, pb = route_to_playbook(query)
    _print_classification(cls, pb)

    with patch(
        "aspire_orchestrator.services.search_router.route_places_search",
        new_callable=AsyncMock, return_value=_ok("google_places.search", GOOGLE_PLACES_PROSPECTS),
    ), patch(
        "aspire_orchestrator.providers.exa_client.execute_exa_search",
        new_callable=AsyncMock, return_value=_ok("exa.search", EXA_PROSPECTS),
    ):
        resp = await dispatch_playbook(pb.name, query, CTX)
    _print_response(resp)
    results.append(("Prospects", pb.name if pb else "NONE", resp.artifact_type, len(resp.records), resp.confidence.get("status", "?")))

    # ------------------------------------------------------------------
    # 6. LANDLORD: Property facts with full ATTOM context
    # ------------------------------------------------------------------
    query = "Give me the full property profile for 456 Oak Ave — sqft, owner, valuation, rental estimate"
    _print_header(6, total, "LANDLORD — Full Property Profile", query, segment="landlord")
    cls, pb = route_to_playbook(query, tenant_segment="landlord")
    _print_classification(cls, pb)

    with patch(
        "aspire_orchestrator.services.search_router.route_places_search",
        new_callable=AsyncMock, return_value=_ok("here.geocode", {"items": [{"address": {"label": "456 Oak Ave"}}]}),
    ), patch(
        "aspire_orchestrator.providers.attom_client.execute_attom_property_detail",
        new_callable=AsyncMock, return_value=_ok("attom.property_detail", ATTOM_DETAIL),
    ), patch(
        "aspire_orchestrator.providers.attom_client.execute_attom_property_detail_with_schools",
        new_callable=AsyncMock, return_value=_ok("attom.property_detail_with_schools", ATTOM_SCHOOLS),
    ), patch(
        "aspire_orchestrator.providers.attom_client.execute_attom_valuation_avm",
        new_callable=AsyncMock, return_value=_ok("attom.valuation_avm", ATTOM_VALUATION),
    ), patch(
        "aspire_orchestrator.providers.attom_client.execute_attom_rental_avm",
        new_callable=AsyncMock, return_value=_ok("attom.rental_avm", ATTOM_RENTAL),
    ):
        resp = await dispatch_playbook(pb.name, query, CTX)
    _print_response(resp)
    results.append(("Landlord Props", pb.name if pb else "NONE", resp.artifact_type, len(resp.records), resp.confidence.get("status", "?")))

    # ------------------------------------------------------------------
    # 7. TRAVEL: Business trip hotel research
    # ------------------------------------------------------------------
    query = "Find good hotels near Rupp Arena in Lexington for a 3-night business trip"
    _print_header(7, total, "TRAVEL — Hotel Research", query)
    cls, pb = route_to_playbook(query)
    _print_classification(cls, pb)

    with patch(
        "aspire_orchestrator.providers.tripadvisor_client.execute_tripadvisor_search",
        new_callable=AsyncMock, return_value=_ok("tripadvisor.search", TRIPADVISOR_HOTELS),
    ), patch(
        "aspire_orchestrator.services.search_router.route_places_search",
        new_callable=AsyncMock, return_value=_ok("google_places.search", GOOGLE_PLACES_HOTELS),
    ), patch(
        "aspire_orchestrator.providers.exa_client.execute_exa_search",
        new_callable=AsyncMock, return_value=_ok("exa.search", EXA_HOTELS),
    ):
        resp = await dispatch_playbook(pb.name, query, CTX)
    _print_response(resp)
    results.append(("Hotel Research", pb.name if pb else "NONE", resp.artifact_type, len(resp.records), resp.confidence.get("status", "?")))

    # ------------------------------------------------------------------
    # SUMMARY
    # ------------------------------------------------------------------
    print()
    print("=" * 90)
    print(f"{'ADAM RESEARCH PLATFORM — SCENARIO RESULTS':^90}")
    print("=" * 90)
    print(f"  {'Scenario':<18} {'Playbook':<32} {'Artifact':<22} {'Records':<9} {'Confidence'}")
    print(f"  {'-'*18} {'-'*32} {'-'*22} {'-'*9} {'-'*15}")
    for name, pb_name, artifact, recs, conf in results:
        status = "PASS" if artifact != "error" else "FAIL"
        print(f"  {name:<18} {pb_name:<32} {artifact:<22} {recs:<9} {conf} [{status}]")

    passed = sum(1 for r in results if r[2] != "error")
    print(f"\n  RESULT: {passed}/{total} scenarios executed successfully")
    print("=" * 90)


if __name__ == "__main__":
    asyncio.run(run_scenarios())
