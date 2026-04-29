"""Live test: Can Adam find paint naps at Home Depot in Forest Park GA?"""
import os, sys
os.environ["ASPIRE_SERPAPI_API_KEY"] = os.environ.get("SERPAPI_API_KEY", os.environ.get("ASPIRE_SERPAPI_API_KEY", ""))
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"
sys.path.insert(0, "src")

import asyncio
from aspire_orchestrator.services.adam.router import route_to_playbook
from aspire_orchestrator.services.adam.playbooks import dispatch_playbook
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext

CTX = PlaybookContext(
    correlation_id="test-hd",
    suite_id="test",
    office_id="test",
    tenant_id="test",
)

QUERIES = [
    "Price check paint roller naps at Home Depot Forest Park GA",
    "Find me paint roller naps available at Home Depot in Forest Park GA",
    "How much are 9 inch paint roller covers at Home Depot near 30297",
]


async def main():
    for q in QUERIES:
        print("=" * 70)
        print(f"  QUERY: {q}")
        c, p = route_to_playbook(q)
        print(f"  Segment: {c.segment} | Intent: {c.intent} | Playbook: {p.name if p else 'NONE'}")

        if p:
            result = await dispatch_playbook(p.name, q, CTX)
            print(f"  Artifact: {result.artifact_type}")
            print(f"  Summary: {result.summary[:200]}")
            print(f"  Providers: {result.providers_called}")
            print(f"  Records: {len(result.records)}")
            for i, rec in enumerate(result.records[:5]):
                if isinstance(rec, dict):
                    name = rec.get("name", rec.get("title", "?"))
                    price = rec.get("price", rec.get("extracted_price", ""))
                    source = rec.get("retailer", rec.get("source", ""))
                    print(f"    {i+1}. {name} | {price} | {source}")
                else:
                    d = rec.to_dict() if hasattr(rec, "to_dict") else str(rec)
                    if isinstance(d, dict):
                        print(f"    {i+1}. {d.get('name', '?')} | {d.get('price', '')} | {d.get('retailer', '')}")
                    else:
                        print(f"    {i+1}. {str(d)[:200]}")
        print()


asyncio.run(main())
