"""Find the correct Home Depot store ID for Forest Park GA and get store details."""
import asyncio, os, json, httpx

API_KEY = os.environ.get("SERPAPI_API_KEY", "")

async def main():
    print("=" * 70)
    print("  HOME DEPOT STORE LOOKUP — Forest Park GA 30297")
    print("=" * 70)

    # SerpApi has a home_depot_store engine
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Try the product search with zip to see what store info comes back
        resp = await client.get(
            "https://serpapi.com/search",
            params={
                "engine": "home_depot",
                "q": "paint roller",
                "delivery_zip": "30297",
                "api_key": API_KEY,
                "no_cache": "false",
            },
        )
        data = resp.json()

        # Check for store info in response
        store_info = data.get("search_information", {})
        print(f"Search info: {json.dumps(store_info, indent=2)}")

        # Check serpapi_pagination or other metadata
        for key in ["search_metadata", "search_parameters", "search_information"]:
            if key in data:
                print(f"\n{key}: {json.dumps(data[key], indent=2)[:500]}")

        # Check first product for store/pickup info
        products = data.get("products", [])
        if products:
            p = products[0]
            print(f"\nFirst product pickup info:")
            print(f"  pickup: {json.dumps(p.get('pickup', {}), indent=2)}")
            print(f"  delivery: {json.dumps(p.get('delivery', {}), indent=2)}")
            print(f"  fulfillment: {json.dumps(p.get('fulfillment', {}), indent=2)}")
            # Print ALL keys in first product
            print(f"\n  All keys: {list(p.keys())}")

        # Now try with specific store_id for Forest Park
        # Home Depot store locator: Forest Park GA store #0253
        for store_id in ["0253", "253"]:
            print(f"\n--- Testing store_id={store_id} ---")
            resp2 = await client.get(
                "https://serpapi.com/search",
                params={
                    "engine": "home_depot",
                    "q": "paint roller nap 9 inch",
                    "store_id": store_id,
                    "delivery_zip": "30297",
                    "api_key": API_KEY,
                    "no_cache": "false",
                },
            )
            data2 = resp2.json()
            prods = data2.get("products", [])
            print(f"  Products: {len(prods)}")
            if prods:
                p = prods[0]
                pickup = p.get("pickup", {})
                print(f"  {p.get('title', '?')}")
                print(f"  Price: {p.get('price')}")
                print(f"  Pickup: {json.dumps(pickup, indent=4)}")

        # Also try the Google Maps / Home Depot store locator via SerpApi
        print(f"\n--- SerpApi Google Maps: Home Depot Forest Park GA ---")
        resp3 = await client.get(
            "https://serpapi.com/search",
            params={
                "engine": "google_maps",
                "q": "Home Depot Forest Park GA 30297",
                "api_key": API_KEY,
                "no_cache": "false",
            },
        )
        data3 = resp3.json()
        places = data3.get("local_results", [])
        print(f"  Places found: {len(places)}")
        for pl in places[:3]:
            print(f"\n  {pl.get('title', '?')}")
            print(f"  Address: {pl.get('address', '?')}")
            print(f"  Phone: {pl.get('phone', '?')}")
            print(f"  Rating: {pl.get('rating', '?')} ({pl.get('reviews', '?')} reviews)")
            print(f"  Hours: {pl.get('hours', '?')}")
            print(f"  GPS: {pl.get('gps_coordinates', {})}")
            print(f"  Place ID: {pl.get('place_id', '?')}")
            # Check for store number in description or data
            print(f"  Data ID: {pl.get('data_id', '?')}")
            desc = pl.get("description", "")
            if desc:
                print(f"  Description: {desc[:200]}")


asyncio.run(main())
