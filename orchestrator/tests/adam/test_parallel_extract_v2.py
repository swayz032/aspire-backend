"""Test Parallel Extract with full_content for structured auction data."""
import asyncio, os, json, httpx

API_KEY = os.environ.get("PARALLEL_API_KEY", "l7WhsawpbFAAhC4vZ9XTMkos5HGrmWUQ96Td_V2v")


async def main():
    print("=" * 70)
    print("  PARALLEL EXTRACT v2 — Full content from Auction.com")
    print("=" * 70)

    async with httpx.AsyncClient(timeout=45.0) as client:
        # Test 1: Auction.com search results page
        resp = await client.post(
            "https://api.parallel.ai/v1beta/extract",
            headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
            json={
                "urls": ["https://www.auction.com/residential/30297_zp"],
                "objective": "Extract all property listings with their full addresses, auction status, opening bid price, number of bedrooms and bathrooms, square footage",
                "excerpts": {"max_chars_per_result": 5000},
                "full_content": False,
            },
        )
        print(f"\nAuction.com search: HTTP {resp.status_code}")
        data = resp.json()
        for r in data.get("results", []):
            print(f"  Title: {r.get('title')}")
            for exc in r.get("excerpts", []):
                print(f"  Content ({len(exc)} chars):")
                print(f"  {exc[:2000]}")

        # Test 2: Individual auction listing page (more detail)
        resp2 = await client.post(
            "https://api.parallel.ai/v1beta/extract",
            headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
            json={
                "urls": [
                    "https://www.auction.com/details/580-shellnut-dr-forest-park-ga-2009350",
                    "https://www.auction.com/details/502-hanes-dr-forest-park-ga-1738932",
                ],
                "objective": "Extract property details: full address, auction date, opening bid, current bid, property type, beds, baths, sqft, lot size, year built, estimated value, foreclosure status, auction end date",
                "excerpts": {"max_chars_per_result": 3000},
                "full_content": False,
            },
        )
        print(f"\nIndividual listings: HTTP {resp2.status_code}")
        data2 = resp2.json()
        for r in data2.get("results", []):
            print(f"\n  URL: {r.get('url')}")
            print(f"  Title: {r.get('title')}")
            for exc in r.get("excerpts", []):
                print(f"  {exc[:1500]}")
        for e in data2.get("errors", []):
            print(f"  ERROR: {e}")


asyncio.run(main())
