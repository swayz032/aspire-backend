"""Scan ATTOM for investment intelligence capabilities."""
import asyncio, os, json
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

import sys
sys.path.insert(0, "src")
from aspire_orchestrator.providers.attom_client import _get_client, _attom_request

ZIP = "30297"

async def test_endpoint(name, path, params):
    r = await _attom_request(
        path=path, query_params=params,
        tool_id="scan", correlation_id="scan", suite_id="scan", office_id="scan",
    )
    total = 0
    props = []
    if r.outcome.value == "success" and r.data:
        status = r.data.get("status", {})
        total = status.get("total", 0)
        props = r.data.get("property", [])
    print(f"\n{'='*70}")
    print(f"  {name}")
    print(f"  Endpoint: {path}")
    print(f"  Result: {r.outcome.value} | Total: {total}")
    for p in props[:5]:
        addr = p.get("address", {}).get("oneLine", "?")
        sale = p.get("sale", {})
        amt = sale.get("amount", {}).get("saleamt", "")
        dt = sale.get("amount", {}).get("salerecdate", sale.get("saleTransDate", ""))
        beds = p.get("building", {}).get("rooms", {}).get("beds", "?")
        sqft = p.get("building", {}).get("size", {}).get("universalsize", "?")
        yr = p.get("summary", {}).get("yearbuilt", "?")
        absentee = p.get("summary", {}).get("absenteeInd", "")
        ptype = sale.get("amount", {}).get("saletranstype", "")
        line = f"    {addr} | {beds}bd | {sqft}sqft | {yr}"
        if amt:
            line += f" | ${int(amt):,}"
        if dt:
            line += f" | {dt}"
        if absentee:
            line += f" | {absentee}"
        if ptype:
            line += f" | {ptype}"
        print(line)
    return total, props


async def main():
    print("=" * 70)
    print("  ATTOM INVESTMENT INTELLIGENCE SCAN — ZIP 30297")
    print("=" * 70)

    # 1. Absentee owners (potential rentals / motivated sellers)
    await test_endpoint(
        "ABSENTEE OWNERS (not living in property)",
        "/property/snapshot",
        {"postalcode": ZIP, "absenteeInd": "A", "propertytype": "SFR", "pagesize": "5"},
    )

    # 2. Corporate owners (investor-owned)
    await test_endpoint(
        "CORPORATE OWNERS (LLC/Corp owned)",
        "/property/snapshot",
        {"postalcode": ZIP, "corporateIndicator": "Y", "propertytype": "SFR", "pagesize": "5"},
    )

    # 3. Foreclosure sales
    await test_endpoint(
        "FORECLOSURE SALES (distressed)",
        "/sale/snapshot",
        {"postalcode": ZIP, "saletranstype": "Foreclosure", "pagesize": "5"},
    )

    # 4. REO sales (bank-owned)
    await test_endpoint(
        "REO / BANK-OWNED SALES",
        "/sale/snapshot",
        {"postalcode": ZIP, "saletranstype": "REO Sale", "pagesize": "5"},
    )

    # 5. Quit claim deeds (often distressed/family transfers)
    await test_endpoint(
        "QUIT CLAIM DEED TRANSFERS",
        "/sale/snapshot",
        {"postalcode": ZIP, "saledoctype": "QUIT CLAIM", "pagesize": "5"},
    )

    # 6. Recent sales under market (potential flips)
    await test_endpoint(
        "BELOW-MARKET SALES ($50K-$150K in 30297)",
        "/sale/snapshot",
        {"postalcode": ZIP, "minsaleamt": "50000", "maxsaleamt": "150000",
         "propertytype": "SFR", "pagesize": "5", "orderby": "saleSearchDate desc"},
    )

    # 7. Vacant land
    await test_endpoint(
        "VACANT LAND PARCELS",
        "/property/snapshot",
        {"postalcode": ZIP, "propertytype": "VACANT", "pagesize": "5"},
    )

    # 8. Pre-foreclosure
    await test_endpoint(
        "PRE-FORECLOSURE (direct endpoint)",
        "/property/preforeclosure",
        {"postalcode": ZIP, "pagesize": "5"},
    )

    # 9. All events (timeline) for investment properties
    # Let's check one of the absentee properties for full intelligence
    print(f"\n{'='*70}")
    print("  DEEP DIVE: First absentee property — full investment profile")
    print(f"{'='*70}")

    # Get first absentee owner
    r = await _attom_request(
        path="/property/snapshot",
        query_params={"postalcode": ZIP, "absenteeInd": "A", "propertytype": "SFR", "pagesize": "1"},
        tool_id="scan", correlation_id="scan", suite_id="scan", office_id="scan",
    )
    if r.outcome.value == "success" and r.data:
        props = r.data.get("property", [])
        if props:
            addr = props[0].get("address", {})
            a1 = addr.get("line1", "")
            a2 = addr.get("line2", "")
            print(f"  Target: {a1}, {a2}")

            # Pull full profile
            profile = await _attom_request(
                path="/property/detailmortgageowner",
                query_params={"address1": a1, "address2": a2},
                tool_id="scan", correlation_id="scan", suite_id="scan", office_id="scan",
            )
            if profile.outcome.value == "success" and profile.data:
                p = profile.data.get("property", [{}])[0]
                owner = p.get("owner", {})
                mortgage = p.get("mortgage", {})
                summary = p.get("summary", {})
                building = p.get("building", {})

                print(f"  Owner: {owner.get('owner1', {}).get('fullname', '?')}")
                print(f"  Absentee: {summary.get('absenteeInd', '?')}")
                print(f"  Mailing: {owner.get('mailingaddressoneline', '?')}")
                print(f"  Mortgage: ${mortgage.get('amount', '?')} {mortgage.get('loantypecode', '')}")
                print(f"  Size: {building.get('size', {}).get('livingsize', '?')}sqft")
                print(f"  Built: {summary.get('yearbuilt', '?')}")

            # Pull equity
            equity = await _attom_request(
                path="/valuation/homeequity",
                query_params={"address1": a1, "address2": a2},
                tool_id="scan", correlation_id="scan", suite_id="scan", office_id="scan",
            )
            if equity.outcome.value == "success" and equity.data:
                ep = equity.data.get("property", [{}])[0]
                eq = ep.get("homeEquity", {})
                avm = ep.get("avm", {}).get("amount", {})
                print(f"  AVM: ${avm.get('value', '?')}")
                print(f"  LTV: {eq.get('LTV', '?')}%")
                print(f"  Equity: ${eq.get('estimatedAvailableEquity', '?')}")
                print(f"  Loan Balance: ${eq.get('totalEstimatedLoanBalance', '?')}")


asyncio.run(main())
