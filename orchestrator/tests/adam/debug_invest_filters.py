"""Test correct ATTOM investment filter parameters."""
import asyncio, os
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

import sys
sys.path.insert(0, "src")
from aspire_orchestrator.providers.attom_client import _attom_request

ZIP = "30297"

async def test(name, path, params):
    r = await _attom_request(
        path=path, query_params=params,
        tool_id="inv", correlation_id="inv", suite_id="inv", office_id="inv",
    )
    total = 0
    props = []
    if r.outcome.value == "success" and r.data:
        total = r.data.get("status", {}).get("total", 0)
        props = r.data.get("property", [])
    print(f"\n{name}: {r.outcome.value} | Total: {total}")
    for p in props[:5]:
        addr = p.get("address", {}).get("oneLine", "?")
        summary = p.get("summary", {})
        sale = p.get("sale", {})
        amt = sale.get("amount", {})
        owner = p.get("owner", {})
        o1 = owner.get("owner1", {})
        bldg = p.get("building", {})

        line = f"  {addr}"
        if summary.get("yearbuilt"):
            line += f" | Built:{summary['yearbuilt']}"
        if bldg.get("size", {}).get("universalsize"):
            line += f" | {bldg['size']['universalsize']}sqft"
        if bldg.get("rooms", {}).get("beds"):
            line += f" | {bldg['rooms']['beds']}bd"
        if summary.get("absenteeInd"):
            line += f" | [{summary['absenteeInd']}]"
        if o1.get("fullname") or o1.get("fullName"):
            line += f" | Owner: {o1.get('fullname') or o1.get('fullName')}"
        if owner.get("corporateindicator") == "Y" or owner.get("corporateIndicator") == "Y":
            line += " [CORP]"
        if amt.get("saleamt"):
            line += f" | Last sale: ${int(amt['saleamt']):,}"
        if sale.get("foreclosure"):
            line += f" | FORECLOSURE: {sale['foreclosure']}"
        print(line)
    return total


async def main():
    print("=" * 80)
    print("  ATTOM INVESTMENT FILTERS — CORRECT PARAM NAMES")
    print("=" * 80)

    # 1. Absentee owners (correct: absenteeowner=absentee)
    await test("ABSENTEE OWNERS in 30297",
        "/property/snapshot",
        {"postalcode": ZIP, "propertytype": "SFR", "absenteeowner": "absentee", "pagesize": "5"})

    # 2. Owner-occupied (correct: absenteeowner=occupied)
    await test("OWNER OCCUPIED in 30297",
        "/property/snapshot",
        {"postalcode": ZIP, "propertytype": "SFR", "absenteeowner": "occupied", "pagesize": "3"})

    # 3. Absentee + detailmortgageowner (full profile for each)
    await test("ABSENTEE with MORTGAGE+OWNER detail",
        "/property/detailmortgageowner",
        {"postalcode": ZIP, "propertytype": "SFR", "absenteeowner": "absentee", "pagesize": "5"})

    # 4. AVM for absentee (find undervalued investment properties)
    await test("ABSENTEE AVM VALUES (investment targets)",
        "/attomavm/detail",
        {"postalcode": ZIP, "propertytype": "SFR", "absenteeowner": "absentee", "pagesize": "5"})

    # 5. All Events with foreclosure flag
    await test("ALL EVENTS (check for foreclosure flag)",
        "/allevents/detail",
        {"postalcode": ZIP, "propertytype": "SFR", "pagesize": "5"})

    # 6. Sale detail — check foreclosure transactions
    await test("RECENT SALES with transaction type",
        "/sale/detail",
        {"postalcode": ZIP, "propertytype": "SFR", "pagesize": "5", "orderby": "saleSearchDate desc"})

    # 7. Rental AVM for investment analysis
    await test("RENTAL AVM (investment yield)",
        "/valuation/rentalavm",
        {"postalcode": ZIP, "propertytype": "SFR", "absenteeowner": "absentee", "pagesize": "5"})

    # 8. Assessment for absentee (find tax deals)
    await test("ASSESSMENT for ABSENTEE (tax analysis)",
        "/assessment/detail",
        {"postalcode": ZIP, "propertytype": "SFR", "absenteeowner": "absentee", "pagesize": "5"})

asyncio.run(main())
