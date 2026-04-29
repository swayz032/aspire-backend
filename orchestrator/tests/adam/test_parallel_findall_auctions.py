"""Test Parallel FindAll to extract structured auction data from Auction.com and RealtyTrac."""
import os, sys

# Must set env BEFORE any imports that read settings
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", os.environ.get("ASPIRE_ATTOM_API_KEY", ""))
os.environ["ASPIRE_EXA_API_KEY"] = os.environ.get("EXA_API_KEY", os.environ.get("ASPIRE_EXA_API_KEY", ""))
os.environ["ASPIRE_PARALLEL_API_KEY"] = os.environ.get("PARALLEL_API_KEY", os.environ.get("ASPIRE_PARALLEL_API_KEY", "l7WhsawpbFAAhC4vZ9XTMkos5HGrmWUQ96Td_V2v"))
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

sys.path.insert(0, "src")

# Now safe to import
import asyncio, json

from aspire_orchestrator.providers.parallel_client import execute_parallel_findall
from aspire_orchestrator.providers.exa_client import execute_exa_search

ARGS = {
    "correlation_id": "test-fc",
    "suite_id": "test",
    "office_id": "test",
}


async def main():
    print("=" * 70)
    print("  PARALLEL FINDALL vs EXA — Auction Data Extraction")
    print("=" * 70)

    # Step 1: Exa finds auction listing URLs
    print("\n--- Step 1: Exa Search for auction URLs ---")
    exa_result = await execute_exa_search(
        payload={"query": "foreclosure auction listings 30297 Forest Park Georgia 2026 property auction date"},
        **ARGS,
    )
    urls_found = []
    if exa_result.outcome.value == "success" and exa_result.data:
        for r in exa_result.data.get("results", [])[:8]:
            url = r.get("url", "")
            title = r.get("title", "")
            print(f"  {title}")
            print(f"    {url}")
            urls_found.append(url)
    print(f"  Total URLs found: {len(urls_found)}")

    # Step 2: Parallel FindAll — extract structured auction data from those URLs
    if urls_found:
        print(f"\n--- Step 2: Parallel FindAll — extracting from {len(urls_found[:3])} URLs ---")
        findall_result = await execute_parallel_findall(
            payload={
                "objective": "Extract all foreclosure auction property listings including: property address, auction date, opening bid amount, property type (beds/baths/sqft), current status, and lender/bank name. Return structured data for each property.",
                "urls": urls_found[:3],
                "max_results": 20,
            },
            **ARGS,
        )
        print(f"  Result: {findall_result.outcome.value}")
        if findall_result.error:
            print(f"  Error: {findall_result.error}")
        if findall_result.data:
            results = findall_result.data.get("results", [])
            print(f"  Extracted results: {len(results)}")
            for i, item in enumerate(results[:10]):
                print(f"\n  Result {i+1}:")
                print(f"  {json.dumps(item, indent=2, default=str)[:500]}")

    # Step 3: Also try Parallel FindAll directly on Auction.com search page
    print(f"\n--- Step 3: Parallel FindAll — direct Auction.com crawl ---")
    direct_result = await execute_parallel_findall(
        payload={
            "objective": "Extract all foreclosure auction property listings from this page. For each property get: full address, auction date, opening bid price, number of beds and baths, square footage, property status, and any lender information.",
            "urls": [
                "https://www.auction.com/residential/GA--Forest_Park/foreclosure/?zip=30297",
                "https://www.realtytrac.com/forest-park-ga-30297/foreclosure/auction/",
            ],
            "max_results": 20,
        },
        **ARGS,
    )
    print(f"  Result: {direct_result.outcome.value}")
    if direct_result.error:
        print(f"  Error: {direct_result.error}")
    if direct_result.data:
        results = direct_result.data.get("results", [])
        print(f"  Extracted results: {len(results)}")
        for i, item in enumerate(results[:10]):
            print(f"\n  Result {i+1}:")
            if isinstance(item, dict):
                print(f"  {json.dumps(item, indent=2, default=str)[:600]}")
            else:
                print(f"  {str(item)[:600]}")


asyncio.run(main())
