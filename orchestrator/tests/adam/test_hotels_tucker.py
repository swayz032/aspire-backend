"""Live test: Hotels in Tucker GA — full playbook."""
import os, sys
os.environ["ASPIRE_SERPAPI_API_KEY"] = os.environ.get("SERPAPI_API_KEY", os.environ.get("ASPIRE_SERPAPI_API_KEY", ""))
os.environ["ASPIRE_GOOGLE_MAPS_API_KEY"] = os.environ.get("GOOGLE_MAPS_API_KEY", os.environ.get("ASPIRE_GOOGLE_MAPS_API_KEY", ""))
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"
sys.path.insert(0, "src")

import asyncio
from aspire_orchestrator.services.adam.router import route_to_playbook
from aspire_orchestrator.services.adam.playbooks import dispatch_playbook
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext

CTX = PlaybookContext(correlation_id="test-hotel", suite_id="test", office_id="test", tenant_id="test")


async def main():
    q = "Find me nice affordable hotels in Tucker GA for a business trip this weekend"
    print("=" * 70)
    print("  QUERY: " + q)
    print("=" * 70)

    c, p = route_to_playbook(q)
    print("  Segment: %s | Intent: %s | Playbook: %s" % (c.segment, c.intent, p.name if p else "NONE"))

    if not p:
        print("  NO PLAYBOOK MATCHED — trying with travel segment hint")
        c, p = route_to_playbook(q, "travel")
        print("  Segment: %s | Intent: %s | Playbook: %s" % (c.segment, c.intent, p.name if p else "NONE"))

    if not p:
        print("  STILL NO MATCH")
        return

    result = await dispatch_playbook(p.name, q, CTX)
    print("\n  Artifact: " + result.artifact_type)
    print("  Summary: " + result.summary[:300])
    print("  Providers: " + str(result.providers_called))
    print("  Confidence: " + str(result.confidence))
    print("  Records: %d" % len(result.records))
    print("  Missing: " + str(result.missing_fields))
    print("  Next queries: " + str(result.next_queries))

    for i, rec in enumerate(result.records[:10]):
        if not isinstance(rec, dict):
            rec = rec.to_dict() if hasattr(rec, "to_dict") else {"raw": str(rec)[:200]}
        print("\n  --- Hotel %d ---" % (i + 1))
        name = rec.get("name", "?")
        addr = rec.get("normalized_address", "")
        rating = rec.get("traveler_rating", rec.get("rating", ""))
        reviews = rec.get("review_count", "")
        price = rec.get("price_range", "")
        phone = rec.get("phone", "")
        website = rec.get("website", "")
        stars = rec.get("star_rating", "")
        amenities = rec.get("amenities", [])
        styles = rec.get("styles", [])
        sentiment = rec.get("sentiment_summary", "")
        description = rec.get("description", "")
        subratings = rec.get("subratings", {})
        trip_types = rec.get("trip_types", {})
        ta_url = rec.get("tripadvisor_url", "")
        ta_rating = rec.get("ta_rating", "")
        ta_reviews = rec.get("ta_review_count", "")
        open_now = rec.get("open_now")

        print("  " + str(name))
        if addr:
            print("  Address: " + str(addr))
        if stars:
            print("  Stars: %s-star hotel" % stars)
        if rating:
            line = "  Google: %s/5 (%s reviews)" % (rating, reviews)
            if ta_rating:
                line += " | TripAdvisor: %s/5 (%s reviews)" % (ta_rating, ta_reviews)
            print(line)
        elif ta_rating:
            print("  TripAdvisor: %s/5 (%s reviews)" % (ta_rating, ta_reviews))
        if price:
            print("  Price: " + str(price))
        if styles:
            print("  Style: " + ", ".join(styles))
        if sentiment:
            print("  Ranking: " + str(sentiment))
        if phone:
            print("  Phone: " + str(phone))
        if website:
            print("  Web: " + str(website)[:80])
        if ta_url:
            print("  TripAdvisor: " + str(ta_url)[:80])
        if amenities:
            print("  Amenities: " + ", ".join(amenities[:10]))
        if subratings:
            parts = ["%s: %s" % (k, v) for k, v in subratings.items()]
            print("  Subratings: " + " | ".join(parts))
        if trip_types:
            parts = ["%s: %s" % (k, v) for k, v in trip_types.items()]
            print("  Trip types: " + " | ".join(parts))
        if description:
            print("  Description: " + str(description)[:200])
        if open_now is not None:
            print("  Open now: " + str(open_now))


asyncio.run(main())
