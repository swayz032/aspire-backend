"""Check ATTOM for auction, foreclosure filing, default notice, lis pendens data."""
import asyncio, os, json
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

import sys
sys.path.insert(0, "src")
from aspire_orchestrator.providers.attom_client import _attom_request

async def main():
    print("=" * 80)
    print("  ATTOM AUCTION / FORECLOSURE DATA DEEP SCAN")
    print("=" * 80)

    # 1. Check allevents for foreclosure fields on ALL properties
    print("\n=== ALL EVENTS — scanning for foreclosure flags ===")
    r = await _attom_request(
        path="/allevents/detail",
        query_params={"postalcode": "30297", "propertytype": "SFR", "pagesize": "50"},
        tool_id="scan", correlation_id="scan", suite_id="scan", office_id="scan",
    )
    if r.outcome.value == "success" and r.data:
        props = r.data.get("property", [])
        foreclosure_count = 0
        for p in props:
            sale = p.get("sale", {})
            # Check ALL fields for foreclosure/auction/default keywords
            foreclosure_val = sale.get("foreclosure", "")
            if foreclosure_val:
                foreclosure_count += 1
                addr = p.get("address", {}).get("oneLine", "?")
                print(f"  FORECLOSURE FLAG: {addr} | foreclosure={foreclosure_val}")
                # Print all sale fields
                for k, v in sale.items():
                    if v and str(v) != "0":
                        print(f"    sale.{k}: {v}")
        print(f"  Properties with foreclosure flag: {foreclosure_count} of {len(props)}")

    # 2. Try to find a known foreclosure property in Georgia
    # Georgia had 1,331 foreclosure starts in Feb 2026
    print("\n=== SALE DETAIL — check all fields for auction/default data ===")
    r2 = await _attom_request(
        path="/sale/detail",
        query_params={"postalcode": "30297", "propertytype": "SFR", "pagesize": "20",
                      "orderby": "saleSearchDate desc"},
        tool_id="scan", correlation_id="scan", suite_id="scan", office_id="scan",
    )
    if r2.outcome.value == "success" and r2.data:
        for p in r2.data.get("property", [])[:20]:
            sale = p.get("sale", {})
            amt = sale.get("amount", {})
            trans_type = amt.get("saletranstype", "")
            foreclosure = sale.get("foreclosure", "")
            addr = p.get("address", {}).get("oneLine", "?")
            if foreclosure or "foreclos" in str(trans_type).lower() or "reo" in str(trans_type).lower():
                print(f"  DISTRESSED: {addr}")
                for k, v in sale.items():
                    if v and str(v) != "0" and k != "vintage":
                        if isinstance(v, dict):
                            for k2, v2 in v.items():
                                if v2 and str(v2) != "0":
                                    print(f"    sale.{k}.{k2}: {v2}")
                        else:
                            print(f"    sale.{k}: {v}")

    # 3. Try expandedprofile for foreclosure/default data
    print("\n=== EXPANDED PROFILE — all foreclosure-related fields ===")
    r3 = await _attom_request(
        path="/property/expandedprofile",
        query_params={"postalcode": "30297", "propertytype": "SFR", "pagesize": "10"},
        tool_id="scan", correlation_id="scan", suite_id="scan", office_id="scan",
    )
    if r3.outcome.value == "success" and r3.data:
        for p in r3.data.get("property", [])[:10]:
            s = p.get("summary", {})
            sale = p.get("sale", {})
            addr = p.get("address", {}).get("oneLine", "?")
            reo = s.get("REOflag", s.get("reoflag", ""))
            qc = s.get("quitClaimFlag", s.get("quitclaimflag", ""))
            foreclosure = sale.get("foreclosure", "")
            if reo == "True" or qc == "True" or foreclosure:
                print(f"  FLAG: {addr} | REO={reo} | QuitClaim={qc} | Foreclosure={foreclosure}")
                for k, v in sale.items():
                    if v and str(v) != "0" and k != "vintage":
                        if isinstance(v, dict):
                            for k2, v2 in v.items():
                                if v2 and str(v2) != "0":
                                    print(f"    sale.{k}.{k2}: {v2}")
                        else:
                            print(f"    sale.{k}: {v}")

    # 4. Try different ZIP codes known for foreclosures (FL, IN)
    print("\n=== HIGH-FORECLOSURE ZIP (Indianapolis 46201) ===")
    r4 = await _attom_request(
        path="/sale/detail",
        query_params={"postalcode": "46201", "propertytype": "SFR", "pagesize": "20",
                      "orderby": "saleSearchDate desc"},
        tool_id="scan", correlation_id="scan", suite_id="scan", office_id="scan",
    )
    if r4.outcome.value == "success" and r4.data:
        fc = 0
        for p in r4.data.get("property", []):
            sale = p.get("sale", {})
            foreclosure = sale.get("foreclosure", "")
            trans = sale.get("amount", {}).get("saletranstype", "")
            if foreclosure or "foreclos" in trans.lower() or "reo" in trans.lower() or "sheriff" in trans.lower():
                fc += 1
                addr = p.get("address", {}).get("oneLine", "?")
                price = sale.get("amount", {}).get("saleamt", "?")
                date = sale.get("amount", {}).get("salerecdate", "?")
                print(f"  {addr} | ${price} | {date} | type={trans} | foreclosure={foreclosure}")
                # Print ALL sale fields
                for k, v in sale.items():
                    if v and str(v) != "0" and k not in ("vintage", "amount", "calculation"):
                        print(f"    sale.{k}: {v}")
        print(f"  Foreclosure/distressed: {fc} of {len(r4.data.get('property', []))}")

    # 5. Try expandedprofile in high-foreclosure area
    print("\n=== EXPANDED PROFILE — Indianapolis 46201 ===")
    r5 = await _attom_request(
        path="/property/expandedprofile",
        query_params={"postalcode": "46201", "propertytype": "SFR", "pagesize": "20"},
        tool_id="scan", correlation_id="scan", suite_id="scan", office_id="scan",
    )
    if r5.outcome.value == "success" and r5.data:
        flags = 0
        for p in r5.data.get("property", []):
            s = p.get("summary", {})
            sale = p.get("sale", {})
            reo = s.get("REOflag", "")
            qc = s.get("quitClaimFlag", "")
            fc = sale.get("foreclosure", "")
            if reo == "True" or qc == "True" or fc:
                flags += 1
                addr = p.get("address", {}).get("oneLine", "?")
                price = sale.get("amount", {}).get("saleAmt", "?")
                print(f"  {addr} | REO={reo} | QC={qc} | FC={fc} | ${price}")
        print(f"  Flagged: {flags} of {len(r5.data.get('property', []))}")

asyncio.run(main())
