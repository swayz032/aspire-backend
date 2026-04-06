"""Check if ATTOM supports radius-based comps."""
import json, os, sys
sys.path.insert(0, "src")
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

import asyncio
from aspire_orchestrator.providers.attom_client import _get_client, _attom_request

async def main():
    # Test 1: Comps by lat/lng radius (0.5 mile = tight neighborhood)
    print("=== COMPS WITHIN 0.5 MILES OF 4863 PRICE ST ===")
    r = await _attom_request(
        path="/sale/snapshot",
        query_params={
            "latitude": "33.621713",
            "longitude": "-84.346311",
            "searchRange": "0.5",
            "minsaleamt": "100000",
            "maxsaleamt": "400000",
            "propertytype": "SFR",
            "pagesize": "10",
            "orderby": "saleSearchDate desc",
        },
        tool_id="test",
        correlation_id="test",
        suite_id="test",
        office_id="test",
    )
    print("Outcome:", r.outcome.value)
    if r.data:
        status = r.data.get("status", {})
        total = status.get("total", 0)
        print("Total comps:", total)
        for p in r.data.get("property", [])[:10]:
            addr = p.get("address", {}).get("oneLine", "?")
            sale = p.get("sale", {})
            amt = sale.get("amount", {}).get("saleamt", 0)
            dt = sale.get("amount", {}).get("salerecdate", "?")
            beds = p.get("building", {}).get("rooms", {}).get("beds", "?")
            yr = p.get("summary", {}).get("yearbuilt", "?")
            ppsf = sale.get("calculation", {}).get("pricepersizeunit", "?")
            dist = p.get("location", {}).get("distance", "?")
            print(f"  {addr}")
            print(f"    ${amt:,} | {dt} | {beds}bd | Built {yr} | ${ppsf}/sqft | {dist}mi away")

    # Test 2: Check what homeEquity gives us for payment accuracy
    print("\n=== HOME EQUITY DETAILS ===")
    r2 = await _attom_request(
        path="/valuation/homeequity",
        query_params={"address1": "4863 Price St", "address2": "Forest Park, GA 30297"},
        tool_id="test", correlation_id="test", suite_id="test", office_id="test",
    )
    if r2.data:
        p = r2.data.get("property", [{}])[0]
        eq = p.get("homeEquity", {})
        for k, v in sorted(eq.items()):
            if v and str(v) != "0":
                print(f"  {k}: {v}")

asyncio.run(main())
