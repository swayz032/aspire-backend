"""SerpApi Home Depot auto-resolves store from delivery_zip. Just use it."""
import os, sys, asyncio, json, httpx

API_KEY = os.environ.get("SERPAPI_API_KEY", "")


async def main():
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "home_depot",
                "q": "paint roller nap 9 inch",
                "delivery_zip": "30297",
                "api_key": API_KEY,
            },
        )
        data = resp.json()

        # Print FULL search_information — this has the store details
        print("search_information:")
        print(json.dumps(data.get("search_information", {}), indent=2))

        # Print first product with ALL fields
        products = data.get("products", [])
        print(f"\nProducts: {len(products)}")
        if products:
            print("\nFull first product:")
            print(json.dumps(products[0], indent=2))

            # Show pickup details for first 5
            for p in products[:5]:
                pickup = p.get("pickup", {})
                title = p.get("title", "?")
                price = p.get("price", "?")
                print(f"\n  {title}")
                print(f"  ${price} | pickup: {json.dumps(pickup)}")


asyncio.run(main())
