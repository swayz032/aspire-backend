"""Direct test of Parallel Extract API (v1beta) for auction data."""
import asyncio, os, json, httpx

API_KEY = os.environ.get("PARALLEL_API_KEY", "l7WhsawpbFAAhC4vZ9XTMkos5HGrmWUQ96Td_V2v")

URLS = [
    "https://www.auction.com/residential/30297_zp",
    "https://www.realtytrac.com/forest-park-ga-30297/foreclosure/auction/",
]


async def main():
    print("=" * 70)
    print("  PARALLEL EXTRACT (v1beta) — Direct API Test")
    print("=" * 70)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.parallel.ai/v1beta/extract",
            headers={
                "x-api-key": API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "urls": URLS,
                "objective": "Extract foreclosure auction property listings: address, auction date, opening bid, beds, baths, sqft, property status, lender",
                "excerpts": True,
                "full_content": False,
            },
        )

        print(f"HTTP {resp.status_code}")
        data = resp.json()

        if resp.status_code == 200:
            results = data.get("results", [])
            errors = data.get("errors", [])
            print(f"Results: {len(results)}")
            print(f"Errors: {len(errors)}")

            for r in results:
                print(f"\n  URL: {r.get('url')}")
                print(f"  Title: {r.get('title', '?')}")
                excerpts = r.get("excerpts", [])
                print(f"  Excerpts: {len(excerpts)}")
                for j, exc in enumerate(excerpts[:3]):
                    print(f"    [{j}] {exc[:500]}")

            for e in errors:
                print(f"  ERROR: {e.get('url')} — {e.get('error_type')} {e.get('content', '')[:200]}")
        else:
            print(f"Error: {json.dumps(data, indent=2)[:500]}")


asyncio.run(main())
