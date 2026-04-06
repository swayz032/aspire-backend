"""Debug the HERE geocode step."""
import asyncio, os
os.environ["ASPIRE_HERE_API_KEY"] = os.environ.get("ASPIRE_HERE_API_KEY", "")
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

from aspire_orchestrator.providers.here_client import execute_here_search
from aspire_orchestrator.providers.attom_client import execute_attom_detail_mortgage_owner

async def main():
    query = "Give me the full property profile for 4863 Price St, Forest Park, GA 30297"

    # Step 1: What does HERE return?
    result = await execute_here_search(
        payload={"query": query, "limit": 1},
        correlation_id="debug", suite_id="test", office_id="test",
    )
    print(f"HERE outcome: {result.outcome.value}")
    items = (result.data or {}).get("items", [])
    if items:
        addr = items[0].get("address", {})
        print(f"HERE label: {addr.get('label', '?')}")
        print(f"HERE house: {addr.get('houseNumber', '?')}")
        print(f"HERE street: {addr.get('street', '?')}")
        print(f"HERE city: {addr.get('city', '?')}")
        print(f"HERE state: {addr.get('stateCode', '?')}")
        print(f"HERE zip: {addr.get('postalCode', '?')}")

        # Build clean address
        house = addr.get("houseNumber", "")
        street = addr.get("street", "")
        city = addr.get("city", "")
        state = addr.get("stateCode", "")
        zip_code = addr.get("postalCode", "").split("-")[0]
        clean = f"{house} {street}, {city}, {state} {zip_code}".strip()
        print(f"\nClean address: {clean}")

        # Step 2: Does ATTOM find it?
        r = await execute_attom_detail_mortgage_owner(
            payload={"address": clean},
            correlation_id="debug", suite_id="test", office_id="test",
        )
        print(f"\nATTOM outcome: {r.outcome.value}")
        if r.data:
            props = r.data.get("property", [])
            if props:
                p = props[0]
                print(f"attomId: {p.get('identifier', {}).get('attomId')}")
                print(f"address: {p.get('address', {}).get('oneLine')}")
                sqft = p.get("building", {}).get("size", {}).get("livingsize")
                year = p.get("summary", {}).get("yearbuilt")
                owner = p.get("owner", {}).get("owner1", {}).get("fullname")
                mort = p.get("mortgage", {})
                print(f"sqft: {sqft}")
                print(f"year: {year}")
                print(f"beds: {p.get('building', {}).get('rooms', {}).get('beds')}")
                print(f"baths: {p.get('building', {}).get('rooms', {}).get('bathsfull')}")
                print(f"owner: {owner}")
                print(f"mortgage: ${mort.get('amount')} {mort.get('loantypecode')} from {mort.get('lender', {}).get('lastname')}")

asyncio.run(main())
