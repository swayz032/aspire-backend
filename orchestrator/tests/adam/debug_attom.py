"""Debug ATTOM client directly."""
import asyncio, os
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

from aspire_orchestrator.providers.attom_client import (
    execute_attom_property_detail,
    execute_attom_sales_history,
    execute_attom_valuation_avm,
)

async def test():
    addr = "4863 Price St, Forest Park, GA 30297"
    print(f"Testing ATTOM with: {addr}\n")

    # Property detail
    r = await execute_attom_property_detail(
        payload={"address": addr},
        correlation_id="debug", suite_id="test", office_id="test",
    )
    print(f"PROPERTY DETAIL: outcome={r.outcome.value}")
    if r.error:
        print(f"  Error: {r.error}")
    if r.data:
        props = r.data.get("property", [])
        print(f"  Properties: {len(props)}")
        if props:
            p = props[0]
            a = p.get("address", {})
            b = p.get("building", {})
            s = p.get("summary", {})
            lot = p.get("lot", {})
            asmt = p.get("assessment", {}).get("assessed", {})
            print(f"  Address: {a.get('oneLine', '?')}")
            print(f"  SqFt: {b.get('size', {}).get('livingSize', '?')}")
            print(f"  Year Built: {s.get('yearBuilt', '?')}")
            print(f"  Type: {s.get('propType', '?')}")
            beds = b.get("rooms", {}).get("beds", "?")
            baths = b.get("rooms", {}).get("bathsFull", "?")
            print(f"  Beds/Baths: {beds}/{baths}")
            print(f"  Lot: {lot.get('lotSize1', '?')} acres")
            print(f"  Assessed: ${asmt.get('assdTtlValue', '?')}")

    # Sales history
    print()
    r2 = await execute_attom_sales_history(
        payload={"address": addr},
        correlation_id="debug", suite_id="test", office_id="test",
    )
    print(f"SALES HISTORY: outcome={r2.outcome.value}")
    if r2.error:
        print(f"  Error: {r2.error}")
    if r2.data:
        props = r2.data.get("property", [])
        print(f"  Records: {len(props)}")
        for sale in props[:3]:
            amt = sale.get("amount", {}).get("saleAmt", "?")
            dt = sale.get("date", {}).get("saleTransDate", "?")
            print(f"  Sale: ${amt} on {dt}")

    # Valuation
    print()
    r3 = await execute_attom_valuation_avm(
        payload={"address": addr},
        correlation_id="debug", suite_id="test", office_id="test",
    )
    print(f"VALUATION AVM: outcome={r3.outcome.value}")
    if r3.error:
        print(f"  Error: {r3.error}")
    if r3.data:
        props = r3.data.get("property", [])
        if props:
            avm = props[0].get("avm", {}).get("amount", {})
            print(f"  Value: ${avm.get('value', '?')}")
            print(f"  Range: ${avm.get('low', '?')} - ${avm.get('high', '?')}")

asyncio.run(test())
