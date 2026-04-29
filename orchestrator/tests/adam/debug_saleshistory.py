"""Test ALL saleshistory endpoints for preforeclosure/auction/NOD/lis pendens data."""
import asyncio, os, json
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

import sys
sys.path.insert(0, "src")
from aspire_orchestrator.providers.attom_client import _attom_request

# Use a known property AND a high-foreclosure ZIP
TARGETS = [
    # Specific property
    {"address1": "4863 PRICE ST", "address2": "Forest Park, GA 30297"},
    # High-foreclosure ZIP search
    {"postalcode": "46201", "propertytype": "SFR", "pagesize": "10"},
    # Another high-foreclosure ZIP
    {"postalcode": "30297", "propertytype": "SFR", "pagesize": "10"},
]

ENDPOINTS = [
    "saleshistory/expandedhistory",
    "saleshistory/detail",
    "saleshistory/basichistory",
    "saleshistory/snapshot",
]


async def test_endpoint(path, params, label):
    r = await _attom_request(
        path=f"/{path}",
        query_params=params,
        tool_id="fc", correlation_id="fc", suite_id="fc", office_id="fc",
    )
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"  Endpoint: {path}")
    print(f"  Params: {params}")
    print(f"  Result: {r.outcome.value}")
    if r.outcome.value != "success" or not r.data:
        if r.error:
            print(f"  Error: {r.error}")
        if r.data:
            print(f"  Error data: {json.dumps(r.data, indent=2)[:500]}")
        return

    props = r.data.get("property", [])
    total = r.data.get("status", {}).get("total", 0)
    print(f"  Total: {total} | Returned: {len(props)}")

    for p in props[:5]:
        addr = p.get("address", {}).get("oneLine", "?")
        print(f"\n  --- {addr} ---")
        # Print ALL top-level keys
        for key in sorted(p.keys()):
            if key in ("address", "identifier", "vintage"):
                continue
            val = p[key]
            if isinstance(val, dict):
                # Recursively print non-empty values
                _print_dict(key, val, depth=2)
            elif isinstance(val, list):
                print(f"    {key}: [{len(val)} items]")
                for i, item in enumerate(val[:3]):
                    if isinstance(item, dict):
                        _print_dict(f"{key}[{i}]", item, depth=3)
                    else:
                        print(f"      {key}[{i}]: {item}")
            elif val and str(val) != "0":
                print(f"    {key}: {val}")


def _print_dict(prefix, d, depth=2):
    indent = "    " * depth
    for k, v in d.items():
        if v is None or str(v) == "0" or k == "vintage":
            continue
        if isinstance(v, dict):
            _print_dict(f"{prefix}.{k}", v, depth)
        elif isinstance(v, list):
            print(f"{indent}{prefix}.{k}: [{len(v)} items]")
            for i, item in enumerate(v[:2]):
                if isinstance(item, dict):
                    _print_dict(f"{prefix}.{k}[{i}]", item, depth + 1)
                else:
                    print(f"{indent}  {item}")
        elif v and str(v).strip():
            print(f"{indent}{prefix}.{k}: {v}")


async def main():
    print("=" * 70)
    print("  ATTOM SALESHISTORY ENDPOINT DEEP SCAN")
    print("  Looking for: NOD, Lis Pendens, Auction dates, Default amounts")
    print("=" * 70)

    # Test each endpoint with a specific property first
    for ep in ENDPOINTS:
        await test_endpoint(ep, TARGETS[0], f"SPECIFIC PROPERTY — {ep}")

    # Test expandedhistory with ZIP search in high-foreclosure area
    await test_endpoint(
        "saleshistory/expandedhistory",
        TARGETS[1],
        "HIGH-FORECLOSURE ZIP (Indianapolis 46201) — expandedhistory"
    )

    # Test expandedhistory with ZIP 30297
    await test_endpoint(
        "saleshistory/expandedhistory",
        TARGETS[2],
        "ZIP 30297 — expandedhistory"
    )

    # Also try allevents with expanded output
    print(f"\n{'='*70}")
    print("  BONUS: allevents/detail with full field dump")
    print(f"{'='*70}")
    r = await _attom_request(
        path="/allevents/detail",
        query_params={"postalcode": "46201", "propertytype": "SFR", "pagesize": "5"},
        tool_id="fc", correlation_id="fc", suite_id="fc", office_id="fc",
    )
    if r.outcome.value == "success" and r.data:
        props = r.data.get("property", [])
        for p in props[:3]:
            addr = p.get("address", {}).get("oneLine", "?")
            print(f"\n  --- {addr} ---")
            for key in sorted(p.keys()):
                if key in ("address", "identifier", "vintage"):
                    continue
                val = p[key]
                if isinstance(val, dict):
                    _print_dict(key, val, depth=2)
                elif val:
                    print(f"    {key}: {val}")


asyncio.run(main())
