"""Deep dive into saleshistory/expandedhistory foreclosure data.
This endpoint has the NOD, lis pendens, auction dates, opening bids.
"""
import asyncio, os, json
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

import sys
sys.path.insert(0, "src")
from aspire_orchestrator.providers.attom_client import _attom_request


async def pull_expanded_history(a1, a2):
    """Pull saleshistory/expandedhistory and dump ALL foreclosure fields."""
    r = await _attom_request(
        path="/saleshistory/expandedhistory",
        query_params={"address1": a1, "address2": a2},
        tool_id="fc", correlation_id="fc", suite_id="fc", office_id="fc",
    )
    print(f"\n{'='*70}")
    print(f"  {a1}, {a2}")
    print(f"  Result: {r.outcome.value}")
    if r.outcome.value != "success" or not r.data:
        print(f"  Error: {r.error}")
        return

    props = r.data.get("property", [])
    for p in props:
        addr = p.get("address", {}).get("oneLine", "?")
        fc_list = p.get("foreclosure", [])
        sh_list = p.get("saleHistory", p.get("salehistory", []))

        print(f"\n  Address: {addr}")
        print(f"  Foreclosure records: {len(fc_list)}")

        # Dump EVERY field in each foreclosure record
        for i, fc in enumerate(fc_list):
            print(f"\n  --- Foreclosure [{i}] ---")
            print(f"  {json.dumps(fc, indent=4, default=str)}")

        print(f"\n  Sale history records: {len(sh_list)}")
        for i, sh in enumerate(sh_list[:3]):
            trans_type = sh.get("amount", {}).get("saleTransType", sh.get("amount", {}).get("saletranstype", "?"))
            buyer = sh.get("buyerName", "?")
            seller = sh.get("sellerName", "?")
            amt = sh.get("amount", {}).get("saleAmt", sh.get("amount", {}).get("saleamt", "?"))
            date = sh.get("saleTransDate", sh.get("saletransdate", "?"))
            print(f"  Sale[{i}]: {date} | ${amt} | {trans_type} | buyer={buyer} | seller={seller}")


async def find_foreclosure_properties():
    """Use sale/snapshot to find distressed properties, then pull expandedhistory."""
    print(f"\n{'='*70}")
    print(f"  STEP 1: Find foreclosure properties via allevents/detail")
    print(f"{'='*70}")

    # Search for properties with foreclosure flags in high-FC areas
    for zip_code in ["46201", "30297", "33311", "48205"]:  # Indy, Forest Park, Ft Lauderdale, Detroit
        r = await _attom_request(
            path="/allevents/detail",
            query_params={"postalcode": zip_code, "propertytype": "SFR", "pagesize": "50"},
            tool_id="fc", correlation_id="fc", suite_id="fc", office_id="fc",
        )
        if r.outcome.value != "success" or not r.data:
            print(f"  {zip_code}: {r.outcome.value} - {r.error}")
            continue

        props = r.data.get("property", [])
        fc_props = []
        for p in props:
            sale = p.get("sale", {})
            fc = sale.get("foreclosure", "")
            trans = sale.get("amount", {}).get("saletranstype", "")
            if fc or "foreclos" in str(trans).lower() or "reo" in str(trans).lower() or "sheriff" in str(trans).lower():
                addr = p.get("address", {})
                fc_props.append({
                    "line1": addr.get("line1", ""),
                    "line2": addr.get("line2", ""),
                    "oneLine": addr.get("oneLine", ""),
                    "foreclosure": fc,
                    "transType": trans,
                })

        print(f"  {zip_code}: {len(fc_props)} distressed of {len(props)}")
        for fp in fc_props[:3]:
            print(f"    {fp['oneLine']} | fc={fp['foreclosure']} | type={fp['transType']}")

        # Pull expandedhistory for first distressed property
        if fc_props:
            fp = fc_props[0]
            await pull_expanded_history(fp["line1"], fp["line2"])

    # Also try our test property
    print(f"\n{'='*70}")
    print(f"  STEP 2: Our test property — 4863 Price St")
    print(f"{'='*70}")
    await pull_expanded_history("4863 PRICE ST", "Forest Park, GA 30297")

    # Try some known high-foreclosure addresses
    print(f"\n{'='*70}")
    print(f"  STEP 3: Try specific addresses in high-FC areas")
    print(f"{'='*70}")

    # Find absentee properties in Detroit (high foreclosure area)
    r2 = await _attom_request(
        path="/property/snapshot",
        query_params={"postalcode": "48205", "propertytype": "SFR",
                      "absenteeowner": "absentee", "pagesize": "10"},
        tool_id="fc", correlation_id="fc", suite_id="fc", office_id="fc",
    )
    if r2.outcome.value == "success" and r2.data:
        for p in r2.data.get("property", [])[:5]:
            addr = p.get("address", {})
            a1 = addr.get("line1", "")
            a2 = addr.get("line2", "")
            if a1 and a2:
                await pull_expanded_history(a1, a2)


asyncio.run(find_foreclosure_properties())
