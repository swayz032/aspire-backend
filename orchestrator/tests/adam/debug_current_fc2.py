"""Test 2: startsalesearchdate on allevents to find recent FC activity."""
import asyncio, os, json
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

import sys
sys.path.insert(0, "src")
from aspire_orchestrator.providers.attom_client import _attom_request


async def main():
    print("=" * 70)
    print("  CURRENT FORECLOSURE HUNT — startsalesearchdate approach")
    print("=" * 70)

    # Strategy: Use startsalesearchdate on allevents to get recent activity,
    # then filter for foreclosure flags
    for zip_code, label in [("30297", "Forest Park GA"), ("46201", "Indianapolis"), ("33311", "Ft Lauderdale"), ("48205", "Detroit"), ("89011", "Henderson NV")]:
        r = await _attom_request(
            path="/allevents/detail",
            query_params={
                "postalcode": zip_code, "propertytype": "SFR",
                "startsalesearchdate": "2024/01/01",
                "pagesize": "50",
                "orderby": "salesearchdate desc",
            },
            tool_id="fc", correlation_id="fc", suite_id="fc", office_id="fc",
        )
        total = 0
        fc_props = []
        if r.outcome.value == "success" and r.data:
            total = r.data.get("status", {}).get("total", 0)
            for p in r.data.get("property", []):
                sale = p.get("sale", {})
                fc = sale.get("foreclosure", "")
                if fc:
                    addr = p.get("address", {})
                    amt = sale.get("amount", {})
                    avm = p.get("avm", {}).get("amount", {})
                    fc_props.append({
                        "address": addr.get("oneLine", "?"),
                        "line1": addr.get("line1", ""),
                        "line2": addr.get("line2", ""),
                        "fc_flag": fc,
                        "trans_type": amt.get("saletranstype", ""),
                        "sale_date": amt.get("salerecdate", ""),
                        "sale_amt": amt.get("saleamt"),
                        "avm_value": avm.get("value"),
                    })

        print(f"\n  {label} ({zip_code}): {len(fc_props)} FC-flagged in recent sales (total recent: {total})")
        for fp in fc_props[:5]:
            line = f"    {fp['address']} | fc={fp['fc_flag']} | {fp['trans_type']} | {fp['sale_date']}"
            if fp['sale_amt']:
                line += f" | ${int(fp['sale_amt']):,}"
            if fp['avm_value']:
                line += f" | AVM=${int(fp['avm_value']):,}"
            print(line)

        # Deep dive on first FC property to see filing dates
        if fc_props:
            fp = fc_props[0]
            if fp["line1"] and fp["line2"]:
                r2 = await _attom_request(
                    path="/saleshistory/expandedhistory",
                    query_params={"address1": fp["line1"], "address2": fp["line2"]},
                    tool_id="fc", correlation_id="fc", suite_id="fc", office_id="fc",
                )
                if r2.outcome.value == "success" and r2.data:
                    for prop in r2.data.get("property", []):
                        fcs = prop.get("foreclosure", [])
                        shs = prop.get("saleHistory", [])
                        print(f"    --- Deep dive: {fp['address']} ---")
                        print(f"    Foreclosure filings: {len(fcs)}")
                        for fc_rec in fcs:
                            if len(fc_rec) > 1:
                                print(f"      recordingDate={fc_rec.get('recordingDate','?')} | "
                                      f"type={fc_rec.get('distressType','?')} | "
                                      f"auction={fc_rec.get('auctionDateTime','')} | "
                                      f"lender={fc_rec.get('beneficiaryName','')} | "
                                      f"loan=${fc_rec.get('originalLoanAmount','?')}")
                        print(f"    Sale history: {len(shs)} records")
                        for sh in shs[:3]:
                            trans = sh.get("amount", {}).get("saleTransType", "?")
                            date = sh.get("saleTransDate", "?")
                            buyer = sh.get("buyerName", "?")
                            seller = sh.get("sellerName", "?")
                            amt = sh.get("amount", {}).get("saleAmt")
                            line = f"      {date} | {trans}"
                            if amt:
                                line += f" | ${int(amt):,}"
                            line += f" | buyer={buyer} | seller={seller}"
                            print(line)

    # Also try sale/snapshot with specific distressed transaction types
    print(f"\n{'='*70}")
    print("  SALE/SNAPSHOT — REO + Sheriff sales in 2024-2026")
    print(f"{'='*70}")
    for zip_code, label in [("30297", "Forest Park"), ("46201", "Indianapolis"), ("33311", "Ft Lauderdale")]:
        r3 = await _attom_request(
            path="/sale/snapshot",
            query_params={
                "postalcode": zip_code, "propertytype": "SFR",
                "startsalesearchdate": "2024/01/01",
                "pagesize": "50",
                "orderby": "salesearchdate desc",
            },
            tool_id="fc", correlation_id="fc", suite_id="fc", office_id="fc",
        )
        distressed = []
        if r3.outcome.value == "success" and r3.data:
            for p in r3.data.get("property", []):
                sale = p.get("sale", {})
                fc = sale.get("foreclosure", "")
                amt = sale.get("amount", {})
                trans = amt.get("saletranstype", "")
                is_distressed = (fc or
                    "reo" in trans.lower() or
                    "foreclos" in trans.lower() or
                    "sheriff" in trans.lower() or
                    "bank" in trans.lower())
                if is_distressed:
                    addr = p.get("address", {}).get("oneLine", "?")
                    distressed.append({
                        "address": addr,
                        "fc": fc,
                        "trans": trans,
                        "date": amt.get("salerecdate", "?"),
                        "price": amt.get("saleamt"),
                    })
        print(f"\n  {label} ({zip_code}): {len(distressed)} distressed in recent sales")
        for d in distressed[:5]:
            line = f"    {d['address']} | fc={d['fc']} | {d['trans']} | {d['date']}"
            if d['price']:
                line += f" | ${int(d['price']):,}"
            print(line)


asyncio.run(main())
