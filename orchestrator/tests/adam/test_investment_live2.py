"""Live test of updated INVESTMENT_OPPORTUNITY_SCAN with current data + Exa auctions."""
import asyncio, os
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_EXA_API_KEY"] = os.environ.get("EXA_API_KEY", "")
os.environ["ASPIRE_PARALLEL_EXTRACT_API_KEY"] = os.environ.get("PARALLEL_EXTRACT_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

import sys
sys.path.insert(0, "src")

from aspire_orchestrator.services.adam.playbooks.landlord import execute_investment_opportunity_scan
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext

CTX = PlaybookContext(
    correlation_id="test-invest2",
    suite_id="test",
    office_id="test",
    tenant_id="test",
)


async def main():
    print("=" * 70)
    print("  INVESTMENT OPPORTUNITY SCAN v2 — LIVE TEST")
    print("  Now with: recent activity filter + Exa live auction listings")
    print("=" * 70)

    result = await execute_investment_opportunity_scan(
        query="Find me current investment opportunities in 30297 — foreclosures, auctions, distressed properties",
        context=CTX,
    )

    print(f"\nSummary: {result.summary}")
    print(f"Providers: {result.providers_called}")
    print(f"Confidence: {result.confidence}")

    rec = result.records[0] if result.records else {}
    print(f"\nZIP: {rec.get('zip_code')}")
    print(f"Absentee owners: {rec.get('total_absentee_owners')}")
    print(f"Foreclosure-flagged: {rec.get('foreclosure_flagged_properties')}")
    print(f"Deep dives: {rec.get('deep_dive_count')}")

    # Live auction listings from Exa
    listings = rec.get("live_auction_listings", [])
    print(f"\n--- LIVE AUCTION LISTINGS ({len(listings)} found) ---")
    for i, l in enumerate(listings):
        title = l.get("title", "?")
        url = l.get("url", "?")
        snippet = l.get("snippet", "")[:150]
        print(f"  {i+1}. {title}")
        print(f"     {url}")
        if snippet:
            print(f"     {snippet}")

    # Deep dive opportunities
    opps = rec.get("opportunities", [])
    print(f"\n--- TOP INVESTMENT OPPORTUNITIES ({len(opps)}) ---")
    for i, opp in enumerate(opps):
        print(f"\n  #{i+1}: {opp.get('address', '?')}")
        print(f"  Year built: {opp.get('year_built', '?')}")
        if opp.get("estimated_value"):
            print(f"  AVM: ${opp['estimated_value']:,.0f}")
        if opp.get("foreclosure_count"):
            print(f"  Foreclosure filings: {opp['foreclosure_count']}")
        if opp.get("latest_filing_date"):
            print(f"  Latest filing: {opp['latest_filing_date']} — {opp.get('latest_filing_type', '?')}")
        if opp.get("auction_date"):
            print(f"  AUCTION: {opp['auction_date']} at {opp.get('auction_location', '?')}")
        if opp.get("lender"):
            print(f"  Lender: {opp['lender']}")
        if opp.get("original_loan"):
            print(f"  Original loan: ${opp['original_loan']:,.0f}")
        if opp.get("available_equity"):
            print(f"  Equity: ${opp['available_equity']:,.0f} (LTV: {opp.get('ltv_ratio', '?')}%)")
        if opp.get("discount_pct"):
            print(f"  Discount: {opp['discount_pct']}%")

    # All flagged
    flagged = rec.get("all_foreclosure_flagged", [])
    recent = [f for f in flagged if f.get("recent_activity")]
    print(f"\n--- ALL FLAGGED: {len(flagged)} total, {len(recent)} with recent activity ---")
    for fp in flagged[:8]:
        line = fp.get("address", "?")
        if fp.get("avm_value"):
            line += f" | AVM=${fp['avm_value']:,.0f}"
        if fp.get("last_sale"):
            line += f" | sold=${fp['last_sale']:,.0f}"
        line += f" | fc={fp.get('foreclosure_flag', '?')}"
        if fp.get("recent_activity"):
            line += " [RECENT]"
        print(f"  {line}")


asyncio.run(main())
