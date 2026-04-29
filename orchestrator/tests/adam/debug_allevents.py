"""Check what allevents/detail returns at ZIP level for investment data."""
import asyncio, os, json
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

import sys
sys.path.insert(0, "src")
from aspire_orchestrator.providers.attom_client import _attom_request

async def main():
    # allevents by ZIP
    r = await _attom_request(
        path="/allevents/detail",
        query_params={"postalcode": "30297", "propertytype": "SFR", "pagesize": "20"},
        tool_id="scan", correlation_id="scan", suite_id="scan", office_id="scan",
    )
    print(f"allevents/detail outcome: {r.outcome.value}")
    if r.data:
        s = r.data.get("status", {})
        print(f"Total: {s.get('total', 0)}")
        props = r.data.get("property", [])
        print(f"Properties returned: {len(props)}")

        for p in props[:10]:
            addr = p.get("address", {}).get("oneLine", "?")
            sale = p.get("sale", {})
            avm = p.get("avm", {})
            assessment = p.get("assessment", {})
            summary = p.get("summary", {})

            amt = sale.get("amount", {})
            avm_amt = avm.get("amount", {})
            avm_change = avm.get("AVMChange", {})

            print(f"\n  {addr}")
            print(f"    Built: {summary.get('yearbuilt', '?')} | {summary.get('absenteeInd', '?')}")
            if amt.get("saleamt"):
                print(f"    Sale: ${int(amt['saleamt']):,} | {amt.get('salerecdate', '?')} | {amt.get('saletranstype', '?')}")
            if avm_amt.get("value"):
                print(f"    AVM: ${int(avm_amt['value']):,} (conf: {avm_amt.get('scr', '?')}/100)")
            if avm_change.get("avmpercentchange"):
                print(f"    AVM Change: {avm_change.get('avmpercentchange')}% (was ${avm_change.get('avmlastmonthvalue', '?'):,})")
            tax = assessment.get("tax", {})
            if tax.get("taxamt"):
                print(f"    Tax: ${tax['taxamt']:,.2f}/yr")

asyncio.run(main())
