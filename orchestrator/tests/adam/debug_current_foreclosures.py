"""Test ATTOM date-range filters to find CURRENT 2025-2026 foreclosure activity.

Key params from ATTOM docs:
  - startsalesearchdate / endsalesearchdate: filter by sale date
  - startcalendardate / endcalendardate: filter by ATTOM record update date
  - orderby=salesearchdate/calendardate: sort by date
"""
import asyncio, os, json
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

import sys
sys.path.insert(0, "src")
from aspire_orchestrator.providers.attom_client import _attom_request


async def test(name, path, params):
    r = await _attom_request(
        path=path, query_params=params,
        tool_id="fc", correlation_id="fc", suite_id="fc", office_id="fc",
    )
    total = 0
    props = []
    if r.outcome.value == "success" and r.data:
        total = r.data.get("status", {}).get("total", 0)
        props = r.data.get("property", [])
    print(f"\n{'='*70}")
    print(f"  {name}")
    print(f"  Path: {path}")
    print(f"  Params: {params}")
    print(f"  Result: {r.outcome.value} | Total: {total} | Returned: {len(props)}")
    if r.error:
        print(f"  Error: {r.error}")
    return total, props


async def main():
    print("=" * 70)
    print("  FINDING CURRENT 2025-2026 FORECLOSURE ACTIVITY")
    print("=" * 70)

    # Test 1: allevents/detail with startcalendardate (recently updated records)
    total, props = await test(
        "ALLEVENTS — recently updated records (2025+) in 30297",
        "/allevents/detail",
        {"postalcode": "30297", "propertytype": "SFR",
         "startcalendardate": "2025/01/01", "pagesize": "20",
         "orderby": "calendardate desc"},
    )
    fc_count = 0
    for p in props[:10]:
        sale = p.get("sale", {})
        fc = sale.get("foreclosure", "")
        addr = p.get("address", {}).get("oneLine", "?")
        amt = sale.get("amount", {})
        trans = amt.get("saletranstype", "")
        date = amt.get("salerecdate", sale.get("salesearchdate", "?"))
        avm = p.get("avm", {}).get("amount", {}).get("value", "?")
        if fc:
            fc_count += 1
            print(f"  FC! {addr} | fc={fc} | {trans} | {date} | AVM=${avm}")
    print(f"  Foreclosure-flagged: {fc_count} of {len(props)} (total in ZIP: {total})")

    # Test 2: allevents/detail with startsalesearchdate (recent sales only)
    total2, props2 = await test(
        "ALLEVENTS — recent sales (2024+) in 30297",
        "/allevents/detail",
        {"postalcode": "30297", "propertytype": "SFR",
         "startsalesearchdate": "2024/01/01", "pagesize": "20",
         "orderby": "salesearchdate desc"},
    )
    for p in props2[:5]:
        sale = p.get("sale", {})
        fc = sale.get("foreclosure", "")
        addr = p.get("address", {}).get("oneLine", "?")
        amt = sale.get("amount", {})
        trans = amt.get("saletranstype", "")
        date = amt.get("salerecdate", "?")
        price = amt.get("saleamt", "?")
        if fc:
            print(f"  FC! {addr} | fc={fc} | {trans} | {date} | ${price}")

    # Test 3: sale/snapshot with recent sales + distressed types
    total3, props3 = await test(
        "SALE/SNAPSHOT — recent REO/foreclosure sales in 30297",
        "/sale/snapshot",
        {"postalcode": "30297", "propertytype": "SFR",
         "startsalesearchdate": "2023/01/01", "pagesize": "20",
         "orderby": "salesearchdate desc"},
    )
    for p in props3[:10]:
        sale = p.get("sale", {})
        fc = sale.get("foreclosure", "")
        addr = p.get("address", {}).get("oneLine", "?")
        amt = sale.get("amount", {})
        trans = amt.get("saletranstype", "")
        date = amt.get("salerecdate", "?")
        price = amt.get("saleamt", "?")
        if fc or "reo" in str(trans).lower() or "foreclos" in str(trans).lower():
            print(f"  DISTRESSED! {addr} | fc={fc} | {trans} | {date} | ${price}")

    # Test 4: Try high-foreclosure area (Indianapolis) with recent data
    total4, props4 = await test(
        "ALLEVENTS — recently updated (2025+) in Indianapolis 46201",
        "/allevents/detail",
        {"postalcode": "46201", "propertytype": "SFR",
         "startcalendardate": "2025/01/01", "pagesize": "30",
         "orderby": "calendardate desc"},
    )
    fc_indy = 0
    for p in props4[:15]:
        sale = p.get("sale", {})
        fc = sale.get("foreclosure", "")
        addr = p.get("address", {}).get("oneLine", "?")
        amt = sale.get("amount", {})
        trans = amt.get("saletranstype", "")
        date = amt.get("salerecdate", sale.get("salesearchdate", "?"))
        if fc:
            fc_indy += 1
            print(f"  FC! {addr} | fc={fc} | {trans} | {date}")
    print(f"  Foreclosure-flagged: {fc_indy} of {len(props4)} (total: {total4})")

    # Test 5: Try Florida (high current foreclosure activity)
    total5, props5 = await test(
        "ALLEVENTS — recently updated (2025+) in Fort Lauderdale 33311",
        "/allevents/detail",
        {"postalcode": "33311", "propertytype": "SFR",
         "startcalendardate": "2025/01/01", "pagesize": "30",
         "orderby": "calendardate desc"},
    )
    fc_fl = 0
    for p in props5[:15]:
        sale = p.get("sale", {})
        fc = sale.get("foreclosure", "")
        addr = p.get("address", {}).get("oneLine", "?")
        amt = sale.get("amount", {})
        trans = amt.get("saletranstype", "")
        date = amt.get("salerecdate", sale.get("salesearchdate", "?"))
        if fc:
            fc_fl += 1
            print(f"  FC! {addr} | fc={fc} | {trans} | {date}")
    print(f"  Foreclosure-flagged: {fc_fl} of {len(props5)} (total: {total5})")

    # Test 6: Deep dive — pull expandedhistory for any 2025+ flagged property
    # to see if the foreclosure records have recent dates
    recent_fc_addrs = []
    for p in (props + props4 + props5):
        sale = p.get("sale", {})
        if sale.get("foreclosure"):
            addr = p.get("address", {})
            if addr.get("line1") and addr.get("line2"):
                recent_fc_addrs.append((addr["line1"], addr["line2"], addr.get("oneLine", "")))

    if recent_fc_addrs:
        print(f"\n{'='*70}")
        print(f"  DEEP DIVE: expandedhistory for {len(recent_fc_addrs[:3])} recently-updated FC properties")
        print(f"{'='*70}")
        for a1, a2, full in recent_fc_addrs[:3]:
            r = await _attom_request(
                path="/saleshistory/expandedhistory",
                query_params={"address1": a1, "address2": a2},
                tool_id="fc", correlation_id="fc", suite_id="fc", office_id="fc",
            )
            if r.outcome.value == "success" and r.data:
                for prop in r.data.get("property", []):
                    fcs = prop.get("foreclosure", [])
                    print(f"\n  {full}")
                    print(f"  Foreclosure records: {len(fcs)}")
                    for fc in fcs:
                        if len(fc) > 1:  # Skip empty records
                            rd = fc.get("recordingDate", "?")
                            dt = fc.get("distressType", "?")
                            auction = fc.get("auctionDateTime", "")
                            lender = fc.get("beneficiaryName", "")
                            loan = fc.get("originalLoanAmount", "")
                            print(f"    {rd} | type={dt} | auction={auction} | lender={lender} | loan=${loan}")

asyncio.run(main())
