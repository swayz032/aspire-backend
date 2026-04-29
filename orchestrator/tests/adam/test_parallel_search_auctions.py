"""Test Parallel Search API for auction data — since FindAll isn't in our plan."""
import os, sys
os.environ["ASPIRE_PARALLEL_API_KEY"] = os.environ.get("PARALLEL_API_KEY", os.environ.get("ASPIRE_PARALLEL_API_KEY", ""))
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"
sys.path.insert(0, "src")

import asyncio, json
from aspire_orchestrator.providers.parallel_client import execute_parallel_search

ARGS = {"correlation_id": "test", "suite_id": "test", "office_id": "test"}


async def main():
    print("=" * 70)
    print("  PARALLEL SEARCH — Auction data for 30297")
    print("=" * 70)

    result = await execute_parallel_search(
        payload={
            "objective": "Find current foreclosure auction listings in ZIP code 30297 Forest Park Georgia. Include property addresses, auction dates, opening bid amounts, and property details (beds, baths, sqft). Focus on auctions happening in 2026.",
            "source_domains": ["auction.com", "realtytrac.com", "foreclosure.com", "hubzu.com"],
            "after_date": "2026-01-01",
            "max_results": 10,
        },
        **ARGS,
    )

    print(f"Result: {result.outcome.value}")
    if result.error:
        print(f"Error: {result.error}")
    if result.data:
        print(f"\nData keys: {list(result.data.keys())}")
        results = result.data.get("results", [])
        print(f"Results: {len(results)}")
        for i, r in enumerate(results[:8]):
            if isinstance(r, dict):
                print(f"\n  Result {i+1}:")
                print(f"  {json.dumps(r, indent=2, default=str)[:600]}")
            else:
                print(f"\n  Result {i+1}: {str(r)[:400]}")

        # Also check for answer/summary
        answer = result.data.get("answer", result.data.get("summary", ""))
        if answer:
            print(f"\n--- ANSWER ---")
            print(answer[:2000])


asyncio.run(main())
