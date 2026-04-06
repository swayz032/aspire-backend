"""Debug landlord property facts playbook end-to-end."""
import asyncio, os
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_HERE_API_KEY"] = os.environ.get("ASPIRE_HERE_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

from aspire_orchestrator.services.adam.playbooks import dispatch_playbook
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext

CTX = PlaybookContext(
    suite_id="debug", office_id="debug", correlation_id="debug-landlord",
    tenant_id="debug",
)

async def main():
    query = "Give me the full property profile for 4863 Price St, Forest Park, GA 30297"
    resp = await dispatch_playbook("PROPERTY_FACTS", query, CTX)

    print(f"Artifact: {resp.artifact_type}")
    print(f"Records: {len(resp.records)}")
    print(f"Providers: {resp.providers_called}")
    print(f"Confidence: {resp.confidence}")
    print(f"Missing: {resp.missing_fields}")

    if resp.records:
        import json
        for i, rec in enumerate(resp.records[:2]):
            print(f"\nRecord {i+1}:")
            for k, v in rec.items():
                if v and v != "" and v != [] and v != 0 and v != 0.0 and k != "sources":
                    print(f"  {k}: {v}")

asyncio.run(main())
