"""Final test: Paint naps at Home Depot near Forest Park GA — full playbook."""
import os, sys
os.environ["ASPIRE_SERPAPI_API_KEY"] = os.environ.get("SERPAPI_API_KEY", os.environ.get("ASPIRE_SERPAPI_API_KEY", ""))
os.environ["ASPIRE_GOOGLE_MAPS_API_KEY"] = os.environ.get("GOOGLE_MAPS_API_KEY", os.environ.get("ASPIRE_GOOGLE_MAPS_API_KEY", ""))
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"
sys.path.insert(0, "src")

import asyncio
from aspire_orchestrator.services.adam.router import route_to_playbook
from aspire_orchestrator.services.adam.playbooks import dispatch_playbook
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext

CTX = PlaybookContext(correlation_id="test", suite_id="test", office_id="test", tenant_id="test")


async def main():
    q = "Find me paint roller naps available at Home Depot in Forest Park GA 30297"
    print("=" * 70)
    print(f"  QUERY: {q}")
    print("=" * 70)

    c, p = route_to_playbook(q, "trades")
    print(f"  Segment: {c.segment} | Intent: {c.intent} | Playbook: {p.name if p else 'NONE'}")

    if not p:
        print("  NO PLAYBOOK MATCHED")
        return

    result = await dispatch_playbook(p.name, q, CTX)
    print(f"\n  Artifact: {result.artifact_type}")
    print(f"  Summary: {result.summary}")
    print(f"  Providers: {result.providers_called}")
    print(f"  Records: {len(result.records)}")

    # Store info
    store = result.extra.get("store", {})
    if store:
        print(f"\n  STORE: {store.get('store_name', '?')} (#{store.get('store_id', '?')})")

    # Products
    hd_products = [r for r in result.records if r.get("retailer") == "Home Depot"]
    other_products = [r for r in result.records if r.get("retailer") != "Home Depot"]

    print(f"\n  --- HOME DEPOT PRODUCTS ({len(hd_products)}) ---")
    for i, r in enumerate(hd_products):
        name = r.get("product_name", "?")
        brand = r.get("brand", "")
        price = r.get("price", "?")
        was = r.get("price_was")
        saving = r.get("price_saving")
        pct = r.get("percentage_off")
        stock = r.get("in_store_stock")
        store_name = r.get("store_id", "")
        rating = r.get("rating")
        reviews = r.get("reviews")
        avail = r.get("availability", "")
        delivery = r.get("delivery_info", "")
        badges = r.get("badges", "")
        model = r.get("model", "")

        print(f"\n  {i+1}. {name}")
        if brand:
            print(f"     Brand: {brand} | Model: {model}")
        line = f"     ${price}"
        if was:
            line += f" (was ${was}"
            if saving:
                line += f", save ${saving}"
            if pct:
                line += f", {pct}% off"
            line += ")"
        print(line)
        if stock is not None:
            print(f"     IN STOCK: {stock} units at {store_name}")
        else:
            print(f"     Stock: {avail}")
        if delivery:
            print(f"     Delivery: {delivery}")
        if rating:
            print(f"     Rating: {rating}/5 ({reviews} reviews)")
        if badges:
            print(f"     Badges: {badges}")

    if other_products:
        print(f"\n  --- OTHER RETAILERS ({len(other_products)}) ---")
        for i, r in enumerate(other_products[:3]):
            name = r.get("product_name", "?")
            price = r.get("price", "?")
            retailer = r.get("retailer", "?")
            print(f"  {i+1}. {name} | ${price} | {retailer}")


asyncio.run(main())
