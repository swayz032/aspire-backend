"""Deep dive on below-market properties for investment intelligence."""
import asyncio, os
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

import sys
sys.path.insert(0, "src")
from aspire_orchestrator.providers.attom_client import _attom_request

TARGETS = [
    ("5432 ASH ST", "Forest Park, GA 30297"),
    ("1024 CHEAVES ST", "Forest Park, GA 30297"),
    ("625 BRIDGE AVE", "Forest Park, GA 30297"),
    ("864 SPRINGVALLEY DR", "Forest Park, GA 30297"),
]

async def profile(a1, a2):
    r = await _attom_request(
        path="/property/expandedprofile",
        query_params={"address1": a1, "address2": a2},
        tool_id="inv", correlation_id="inv", suite_id="inv", office_id="inv",
    )
    if r.outcome.value != "success" or not r.data:
        print(f"  {a1}, {a2} — FAILED")
        return

    p = r.data.get("property", [{}])[0]
    s = p.get("summary", {})
    sale = p.get("sale", {})
    owner = p.get("assessment", {}).get("owner", {})
    mort = p.get("assessment", {}).get("mortgage", {}).get("FirstConcurrent", {})
    bldg = p.get("building", {})
    addr = p.get("address", {}).get("oneLine", "?")

    print(f"\n  {addr}")
    print(f"    Built: {s.get('yearBuilt', '?')} | {bldg.get('size', {}).get('livingSize', '?')}sqft | {bldg.get('rooms', {}).get('beds', '?')}bd/{bldg.get('rooms', {}).get('bathsTotal', '?')}ba")
    print(f"    Absentee: {s.get('absenteeInd', '?')}")
    print(f"    REO: {s.get('REOflag', '?')} | QuitClaim: {s.get('quitClaimFlag', '?')}")
    print(f"    Owner: {owner.get('owner1', {}).get('fullName', '?')} | Corp: {owner.get('corporateIndicator', '?')}")
    print(f"    Mailing: {owner.get('mailingAddressOneLine', '?')}")
    print(f"    Seller: {sale.get('sellerName', '?')}")
    amt = sale.get("amount", {})
    print(f"    Last sale: ${amt.get('saleAmt', '?')} on {sale.get('saleTransDate', '?')} ({amt.get('saleTransType', '?')})")
    print(f"    Mortgage: ${mort.get('amount', 'None')} {mort.get('loanTypeCode', '')}")

    # Get equity
    eq_r = await _attom_request(
        path="/valuation/homeequity",
        query_params={"address1": a1, "address2": a2},
        tool_id="inv", correlation_id="inv", suite_id="inv", office_id="inv",
    )
    if eq_r.outcome.value == "success" and eq_r.data:
        ep = eq_r.data.get("property", [{}])[0]
        eq = ep.get("homeEquity", {})
        avm = ep.get("avm", {}).get("amount", {})
        print(f"    AVM: ${avm.get('value', '?')} | LTV: {eq.get('LTV', '?')}% | Equity: ${eq.get('estimatedAvailableEquity', '?')}")
        balance = eq.get("totalEstimatedLoanBalance", 0)
        value = avm.get("value", 0)
        if balance and value:
            spread = value - balance
            print(f"    SPREAD: AVM ${value:,} - Balance ${balance:,} = ${spread:,} potential")


async def main():
    print("=" * 70)
    print("  INVESTMENT DEEP DIVE — Below-market properties in 30297")
    print("=" * 70)
    for a1, a2 in TARGETS:
        await profile(a1, a2)

asyncio.run(main())
