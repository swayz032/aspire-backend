"""Live test: Paint roller naps at Home Depot near Forest Park GA 30297."""
import os, sys
os.environ["ASPIRE_SERPAPI_API_KEY"] = os.environ.get("SERPAPI_API_KEY", os.environ.get("ASPIRE_SERPAPI_API_KEY", ""))
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"
sys.path.insert(0, "src")

import asyncio
from aspire_orchestrator.providers.serpapi_homedepot_client import execute_serpapi_homedepot_search
from aspire_orchestrator.services.adam.router import route_to_playbook
from aspire_orchestrator.services.adam.playbooks import dispatch_playbook
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext

ARGS = {"correlation_id": "test", "suite_id": "test", "office_id": "test"}
CTX = PlaybookContext(correlation_id="test", suite_id="test", office_id="test", tenant_id="test")


async def main():
    # Step 1: Direct Home Depot API call
    print("=" * 70)
    print("  DIRECT HOME DEPOT API — paint roller naps near 30297")
    print("=" * 70)

    result = await execute_serpapi_homedepot_search(
        payload={
            "query": "paint roller naps 9 inch",
            "delivery_zip": "30297",
            "store_id": "0253",  # Forest Park HD store
        },
        **ARGS,
    )

    print(f"Result: {result.outcome.value}")
    if result.error:
        print(f"Error: {result.error}")
    if result.data:
        products = result.data.get("results", [])
        print(f"Products found: {len(products)}")
        for i, p in enumerate(products[:10]):
            title = p.get("title", "?")
            brand = p.get("brand", "")
            price = p.get("price", "?")
            was = p.get("price_was", "")
            saving = p.get("price_saving", "")
            pct = p.get("percentage_off", "")
            rating = p.get("rating", "")
            reviews = p.get("reviews", "")
            stock = p.get("pickup_quantity")
            delivery = p.get("delivery", "")
            model = p.get("model_number", "")

            print(f"\n  {i+1}. {title}")
            if brand:
                print(f"     Brand: {brand} | Model: {model}")
            print(f"     Price: {price}", end="")
            if was:
                print(f" (was {was}, save {saving}, {pct} off)", end="")
            print()
            if stock is not None:
                print(f"     IN-STORE STOCK: {stock} units at Forest Park HD")
            if delivery:
                print(f"     Delivery: {delivery}")
            if rating:
                print(f"     Rating: {rating}/5 ({reviews} reviews)")

    # Step 2: Full playbook route test
    print(f"\n{'='*70}")
    print("  PLAYBOOK ROUTE — 'price check paint roller naps Home Depot 30297'")
    print(f"{'='*70}")

    q = "Price check paint roller naps at Home Depot near 30297"
    c, p = route_to_playbook(q, "trades")
    print(f"Segment: {c.segment} | Intent: {c.intent} | Playbook: {p.name if p else 'NONE'}")

    if p:
        resp = await dispatch_playbook(p.name, q, CTX)
        print(f"Artifact: {resp.artifact_type}")
        print(f"Summary: {resp.summary[:300]}")
        print(f"Providers: {resp.providers_called}")
        print(f"Records: {len(resp.records)}")
        for i, rec in enumerate(resp.records[:5]):
            if isinstance(rec, dict):
                name = rec.get("name", rec.get("title", "?"))
                price = rec.get("price", rec.get("extracted_price", ""))
                brand = rec.get("brand", "")
                stock = rec.get("in_store_stock", rec.get("pickup_quantity", ""))
                print(f"  {i+1}. {name} | {price} | {brand} | stock={stock}")


asyncio.run(main())
