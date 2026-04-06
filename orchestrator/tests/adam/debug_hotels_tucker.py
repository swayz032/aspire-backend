"""Debug: What do TripAdvisor and Google Places actually return for Tucker GA hotels?"""
import os, sys, json
os.environ["ASPIRE_SERPAPI_API_KEY"] = os.environ.get("SERPAPI_API_KEY", os.environ.get("ASPIRE_SERPAPI_API_KEY", ""))
os.environ["ASPIRE_GOOGLE_MAPS_API_KEY"] = os.environ.get("GOOGLE_MAPS_API_KEY", os.environ.get("ASPIRE_GOOGLE_MAPS_API_KEY", ""))
os.environ["ASPIRE_TRIPADVISOR_API_KEY"] = os.environ.get("TRIPADVISOR_API_KEY", os.environ.get("ASPIRE_TRIPADVISOR_API_KEY", ""))
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"
sys.path.insert(0, "src")

import asyncio
from aspire_orchestrator.providers.tripadvisor_client import execute_tripadvisor_search
from aspire_orchestrator.providers.google_places_client import execute_google_places_search

ARGS = {"correlation_id": "debug", "suite_id": "debug", "office_id": "debug"}


async def main():
    # 1. TripAdvisor raw response
    print("=" * 70)
    print("  TRIPADVISOR — hotels Tucker GA")
    print("=" * 70)
    ta = await execute_tripadvisor_search(
        payload={"query": "hotels in Tucker GA", "category": "hotels", "language": "en"},
        **ARGS,
    )
    print("Result:", ta.outcome.value)
    if ta.error:
        print("Error:", ta.error)
    if ta.data:
        results = ta.data.get("results", [])
        print("Results:", len(results))
        for i, r in enumerate(results[:10]):
            name = r.get("name", "?")
            addr = r.get("address_obj", {})
            city = addr.get("city", "?")
            state = addr.get("state", "?")
            rating = r.get("rating", "?")
            reviews = r.get("num_reviews", "?")
            price = r.get("price_level", r.get("price", "?"))
            hotel_class = r.get("hotel_class", "?")
            print("%d. %s | %s, %s | rating=%s | reviews=%s | price=%s | class=%s" % (
                i+1, name, city, state, rating, reviews, price, hotel_class))
        # Print raw first result
        if results:
            print("\nRaw first result keys:", list(results[0].keys()))

    # 2. Google Places raw response — tighter search
    print("\n" + "=" * 70)
    print("  GOOGLE PLACES — hotels in Tucker GA")
    print("=" * 70)
    gp = await execute_google_places_search(
        payload={"query": "hotels in Tucker GA 30084", "type": "lodging"},
        **ARGS,
    )
    print("Result:", gp.outcome.value)
    if gp.error:
        print("Error:", gp.error)
    if gp.data:
        results = gp.data.get("results", [])
        print("Results:", len(results))
        for i, r in enumerate(results[:15]):
            name = r.get("name", "?")
            addr = r.get("formatted_address", r.get("address", "?"))
            rating = r.get("rating", "?")
            reviews = r.get("userRatingCount", r.get("user_ratings_total", "?"))
            price = r.get("priceLevel", r.get("price_level", "?"))
            phone = r.get("phone", "")
            website = r.get("website", "")
            print("%d. %s" % (i+1, name))
            print("   %s | rating=%s (%s reviews) | price=%s" % (addr, rating, reviews, price))
            if phone:
                print("   phone=%s | web=%s" % (phone, website[:60] if website else ""))


asyncio.run(main())
