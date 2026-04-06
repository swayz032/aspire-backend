"""Retest the 2 previously-failed scenarios after classifier fixes."""
import asyncio, os, time

os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_EXA_API_KEY"] = os.environ.get("EXA_SEARCH_API_KEY", "")
os.environ["ASPIRE_SERPAPI_API_KEY"] = os.environ.get("SERPAPI_API_KEY", "")
os.environ["ASPIRE_TRIPADVISOR_API_KEY"] = os.environ.get("TRIPADVISOR_API_KEY", "")
os.environ["ASPIRE_PARALLEL_API_KEY"] = os.environ.get("PARALLEL_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

from aspire_orchestrator.services.adam.playbooks import dispatch_playbook
from aspire_orchestrator.services.adam.router import route_to_playbook
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext

CTX = PlaybookContext(suite_id="test", office_id="test", correlation_id="retest", tenant_id="test")


async def main():
    # TEST 4: Milwaukee tools (was: general_smb/NONE)
    q1 = "Price check Milwaukee M18 FUEL impact driver and drill combo"
    cls1, pb1 = route_to_playbook(q1)
    print(f"TEST 4: {cls1.segment}/{cls1.intent} -> {pb1.name if pb1 else 'NONE'}")
    if pb1:
        t0 = time.monotonic()
        r1 = await dispatch_playbook(pb1.name, q1, CTX)
        elapsed = time.monotonic() - t0
        print(f"  Artifact: {r1.artifact_type} | {len(r1.records)} records | {elapsed:.1f}s")
        for rec in r1.records[:5]:
            name = rec.get("product_name", "?")[:60]
            price = rec.get("price") or rec.get("extracted_price")
            stock = rec.get("in_store_stock")
            line = f"    - {name}"
            if price:
                line += f" | ${price}"
            if stock is not None:
                line += f" | in-stock: {stock}"
            print(line)
    else:
        print("  STILL FAILING: no playbook matched")

    print()

    # TEST 9: Hotels (was: accounting_bookkeeping/CLIENT_VERIFICATION)
    q2 = "Find 5 business-friendly hotels near Georgia World Congress Center in Atlanta"
    cls2, pb2 = route_to_playbook(q2)
    print(f"TEST 9: {cls2.segment}/{cls2.intent} -> {pb2.name if pb2 else 'NONE'}")
    if pb2:
        t0 = time.monotonic()
        r2 = await dispatch_playbook(pb2.name, q2, CTX)
        elapsed = time.monotonic() - t0
        print(f"  Artifact: {r2.artifact_type} | {len(r2.records)} records | {elapsed:.1f}s")
        for rec in r2.records[:5]:
            name = rec.get("name", "?")[:60]
            rating = rec.get("traveler_rating") or rec.get("rating")
            line = f"    - {name}"
            if rating:
                line += f" | {rating} stars"
            print(line)
    else:
        print("  STILL FAILING: no playbook matched")


asyncio.run(main())
