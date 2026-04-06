"""Debug: What FULL data do TripAdvisor Details and Google Places return for Tucker hotels?"""
import os, sys, json
os.environ["ASPIRE_GOOGLE_MAPS_API_KEY"] = os.environ.get("GOOGLE_MAPS_API_KEY", os.environ.get("ASPIRE_GOOGLE_MAPS_API_KEY", ""))
os.environ["ASPIRE_TRIPADVISOR_API_KEY"] = os.environ.get("TRIPADVISOR_API_KEY", os.environ.get("ASPIRE_TRIPADVISOR_API_KEY", ""))
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"
sys.path.insert(0, "src")

import asyncio
from aspire_orchestrator.providers.tripadvisor_client import (
    execute_tripadvisor_search,
    execute_tripadvisor_location_details,
)
from aspire_orchestrator.providers.google_places_client import execute_google_places_search

ARGS = {"correlation_id": "debug", "suite_id": "debug", "office_id": "debug"}


async def main():
    # 1. TripAdvisor Search → get location_ids
    print("=" * 70)
    print("  TRIPADVISOR SEARCH → DETAILS (full data dump)")
    print("=" * 70)
    ta = await execute_tripadvisor_search(
        payload={"query": "hotels in Tucker GA", "category": "hotels"},
        **ARGS,
    )
    if ta.outcome.value == "success" and ta.data:
        locs = ta.data.get("results", [])
        print("Search found %d locations" % len(locs))

        # Get details for first 3
        for loc in locs[:3]:
            loc_id = loc.get("location_id", "")
            name = loc.get("name", "?")
            print("\n--- %s (ID: %s) ---" % (name, loc_id))

            if not loc_id:
                print("  No location_id!")
                continue

            det = await execute_tripadvisor_location_details(
                location_id=loc_id, **ARGS,
            )
            if det.outcome.value == "success" and det.data:
                d = det.data
                # Print ALL keys and values
                for k in sorted(d.keys()):
                    v = d[k]
                    if isinstance(v, (dict, list)):
                        if isinstance(v, list) and len(v) > 5:
                            print("  %s: [%d items] %s..." % (k, len(v), json.dumps(v[:3], default=str)[:300]))
                        else:
                            print("  %s: %s" % (k, json.dumps(v, default=str)[:300]))
                    elif v is not None and str(v).strip():
                        print("  %s: %s" % (k, v))
            else:
                print("  Details FAILED: %s" % det.error)

    # 2. Google Places — what fields come back with Place Details enrichment?
    print("\n" + "=" * 70)
    print("  GOOGLE PLACES — full field dump")
    print("=" * 70)
    gp = await execute_google_places_search(
        payload={"query": "hotels in Tucker GA 30084", "type": "lodging"},
        **ARGS,
    )
    if gp.outcome.value == "success" and gp.data:
        results = gp.data.get("results", [])
        print("Found %d results" % len(results))
        for r in results[:3]:
            print("\n--- %s ---" % r.get("name", "?"))
            for k in sorted(r.keys()):
                v = r[k]
                if isinstance(v, (dict, list)):
                    print("  %s: %s" % (k, json.dumps(v, default=str)[:300]))
                elif v is not None and str(v).strip():
                    print("  %s: %s" % (k, v))


asyncio.run(main())
