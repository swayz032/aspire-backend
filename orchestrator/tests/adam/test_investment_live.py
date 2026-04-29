"""Live test of INVESTMENT_OPPORTUNITY_SCAN playbook with real ATTOM API."""
import asyncio, os, json
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

import sys
sys.path.insert(0, "src")

from aspire_orchestrator.services.adam.playbooks.landlord import execute_investment_opportunity_scan
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext

CTX = PlaybookContext(
    correlation_id="test-invest",
    suite_id="test",
    office_id="test",
    tenant_id="test",
)


async def main():
    print("=" * 70)
    print("  INVESTMENT OPPORTUNITY SCAN — LIVE TEST")
    print("=" * 70)

    result = await execute_investment_opportunity_scan(
        query="Find me investment opportunities in 30297 — foreclosures, distressed properties, absentee owners",
        context=CTX,
    )

    print(f"\nArtifact: {result.artifact_type}")
    print(f"Summary: {result.summary}")
    print(f"Confidence: {result.confidence}")
    print(f"Providers: {result.providers_called}")
    print(f"Missing: {result.missing_fields}")
    print(f"Next queries: {result.next_queries}")

    for i, rec in enumerate(result.records):
        print(f"\n{'='*60}")
        print(f"  Record {i}")
        print(f"{'='*60}")
        print(f"  ZIP: {rec.get('zip_code', '?')}")
        print(f"  Total absentee owners: {rec.get('total_absentee_owners', '?')}")
        print(f"  Foreclosure-flagged: {rec.get('foreclosure_flagged_properties', '?')}")
        print(f"  Deep dive count: {rec.get('deep_dive_count', '?')}")

        # Show opportunities
        for j, opp in enumerate(rec.get("opportunities", [])):
            print(f"\n  --- Opportunity {j+1} ---")
            print(f"  Address: {opp.get('address', '?')}")
            print(f"  Year built: {opp.get('year_built', '?')}")
            print(f"  FC flag: {opp.get('foreclosure_flag', '?')}")
            print(f"  Trans type: {opp.get('trans_type', '?')}")
            print(f"  FC stage: {opp.get('foreclosure_stage', '?')}")
            print(f"  FC count: {opp.get('foreclosure_count', '?')}")
            if opp.get("latest_filing_date"):
                print(f"  Latest filing: {opp.get('latest_filing_date')} — {opp.get('latest_filing_type', '?')}")
            if opp.get("auction_date"):
                print(f"  AUCTION: {opp.get('auction_date')} at {opp.get('auction_location', '?')}")
            if opp.get("lender"):
                print(f"  Lender: {opp.get('lender')}")
            if opp.get("original_loan"):
                print(f"  Original loan: ${opp.get('original_loan'):,.0f}")
            if opp.get("opening_bid"):
                print(f"  Opening bid: ${opp.get('opening_bid'):,.0f}")
            if opp.get("estimated_value"):
                print(f"  AVM value: ${opp.get('estimated_value'):,.0f} (confidence: {opp.get('avm_confidence', '?')})")
            if opp.get("current_loan_balance"):
                print(f"  Loan balance: ${opp.get('current_loan_balance'):,.0f}")
            if opp.get("available_equity"):
                print(f"  Available equity: ${opp.get('available_equity'):,.0f}")
            if opp.get("ltv_ratio"):
                print(f"  LTV: {opp.get('ltv_ratio')}%")
            if opp.get("discount_pct"):
                print(f"  Discount from market: {opp.get('discount_pct')}%")

        # Show all flagged
        flagged = rec.get("all_foreclosure_flagged", [])
        print(f"\n  --- All {len(flagged)} Foreclosure-Flagged Properties ---")
        for fp in flagged:
            line = f"  {fp.get('address', '?')} | fc={fp.get('foreclosure_flag', '?')} | {fp.get('trans_type', '?')}"
            if fp.get("avm_value"):
                line += f" | AVM=${fp['avm_value']:,.0f}"
            if fp.get("last_sale"):
                line += f" | sold=${fp['last_sale']:,.0f}"
            print(line)


asyncio.run(main())
