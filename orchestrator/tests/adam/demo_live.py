"""Adam Research Platform — LIVE API Test.

Runs real queries against real provider APIs. No mocks.
Requires API keys set as environment variables.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

# Set API keys BEFORE importing anything else — settings reads env at import time
os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", os.environ.get("ASPIRE_ATTOM_API_KEY", ""))
os.environ["ASPIRE_EXA_API_KEY"] = os.environ.get("EXA_SEARCH_API_KEY", os.environ.get("ASPIRE_EXA_API_KEY", ""))
os.environ["ASPIRE_SERPAPI_API_KEY"] = os.environ.get("SERPAPI_API_KEY", os.environ.get("ASPIRE_SERPAPI_API_KEY", ""))
os.environ["ASPIRE_TRIPADVISOR_API_KEY"] = os.environ.get("TRIPADVISOR_API_KEY", os.environ.get("ASPIRE_TRIPADVISOR_API_KEY", ""))
os.environ["ASPIRE_PARALLEL_API_KEY"] = os.environ.get("PARALLEL_API_KEY", os.environ.get("ASPIRE_PARALLEL_API_KEY", ""))
os.environ["ASPIRE_BRAVE_API_KEY"] = os.environ.get("ASPIRE_BRAVE_API_KEY", "")
os.environ["ASPIRE_TAVILY_API_KEY"] = os.environ.get("ASPIRE_TAVILY_API_KEY", "")
os.environ["ASPIRE_GOOGLE_MAPS_API_KEY"] = os.environ.get("ASPIRE_GOOGLE_MAPS_API_KEY", "")
os.environ["ASPIRE_HERE_API_KEY"] = os.environ.get("ASPIRE_HERE_API_KEY", "")
os.environ["ASPIRE_FOURSQUARE_API_KEY"] = os.environ.get("ASPIRE_FOURSQUARE_API_KEY", "")
# Disable strict credential mode for local test
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

from aspire_orchestrator.services.adam.playbooks import dispatch_playbook
from aspire_orchestrator.services.adam.router import route_to_playbook
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.schemas.research_response import ResearchResponse
from aspire_orchestrator.services.adam.telemetry import estimate_provider_cost
from aspire_orchestrator.config.settings import settings


CTX = PlaybookContext(
    suite_id="live-test-001",
    office_id="office-live-001",
    correlation_id="live-demo-2026-04-06",
    tenant_id="live-test-001",
)


def _check_keys():
    """Print which API keys are available."""
    keys = {
        "ATTOM": settings.attom_api_key,
        "Exa": settings.exa_api_key,
        "SerpApi": settings.serpapi_api_key,
        "Tripadvisor": settings.tripadvisor_api_key,
        "Parallel": settings.parallel_api_key,
        "Brave": settings.brave_api_key,
        "Tavily": settings.tavily_api_key,
        "Google Maps": settings.google_maps_api_key,
        "HERE": settings.here_api_key,
        "Foursquare": settings.foursquare_api_key,
    }
    print("\n  API KEY STATUS:")
    for name, key in keys.items():
        status = f"OK ({key[:8]}...)" if key else "MISSING"
        print(f"    {name:<14} {status}")
    missing = [n for n, k in keys.items() if not k]
    if missing:
        print(f"\n  WARNING: Missing keys: {', '.join(missing)}")
    return keys


def _print_response(resp: ResearchResponse, elapsed: float):
    is_error = resp.artifact_type == "error"
    print(f"\n  RESPONSE {'(ERROR)' if is_error else ''}:")
    print(f"    Artifact:    {resp.artifact_type}")
    print(f"    Records:     {len(resp.records)}")
    print(f"    Providers:   {', '.join(resp.providers_called)}")
    print(f"    Confidence:  {resp.confidence}")
    print(f"    Missing:     {resp.missing_fields or 'none'}")
    print(f"    Latency:     {elapsed:.1f}s")

    if resp.verification_report:
        vr = resp.verification_report
        print(f"\n  VERIFICATION:")
        print(f"    Status:      {vr.status}")
        print(f"    Score:       {vr.confidence_score:.2f}")
        print(f"    Sources:     {vr.source_count}")
        print(f"    Conflicts:   {len(vr.conflicts)}")
        if vr.conflicts:
            for c in vr.conflicts[:3]:
                providers = [v.get("provider", "?") for v in c.values]
                vals = [str(v.get("value", "?"))[:40] for v in c.values]
                print(f"      {c.field_name}: {' vs '.join(vals[:2])}")

    if resp.records:
        print(f"\n  RECORDS:")
        for i, rec in enumerate(resp.records[:5]):
            if isinstance(rec, dict):
                name = (rec.get("product_name") or rec.get("name") or
                        rec.get("normalized_address") or rec.get("title", "?"))[:60]
                price = rec.get("price") or rec.get("extracted_price")
                rating = rec.get("rating") or rec.get("traveler_rating")
                stock = rec.get("in_store_stock")
                sqft = rec.get("living_sqft")
                year = rec.get("year_built")
                line = f"    [{i+1}] {name}"
                if price:
                    line += f" | ${price}"
                if rating:
                    line += f" | {rating}*"
                if stock is not None:
                    line += f" | stock:{stock}"
                if sqft:
                    line += f" | {sqft}sqft"
                if year:
                    line += f" | built:{year}"
                print(line)

    if resp.summary and not is_error:
        print(f"\n  SUMMARY: {resp.summary[:120]}")

    if resp.next_queries:
        print(f"\n  NEXT QUERIES:")
        for nq in resp.next_queries[:2]:
            print(f"    -> {nq}")


async def run_scenario(num: int, total: int, title: str, query: str, segment: str | None = None):
    """Run a single scenario and print results."""
    print(f"\n{'=' * 90}")
    print(f"  SCENARIO {num}/{total}: {title}")
    print(f'  Query: "{query}"')
    if segment:
        print(f"  Segment: {segment}")
    print(f"{'=' * 90}")

    cls, pb = route_to_playbook(query, tenant_segment=segment)

    est = sum(estimate_provider_cost(p) for p in pb.provider_order) if pb else 0
    print(f"\n  CLASSIFICATION: {cls.segment}/{cls.intent}/{cls.entity_type} (conf: {cls.confidence:.0%})")
    if pb:
        print(f"  PLAYBOOK: {pb.name} -> {pb.artifact_type}")
        print(f"  PROVIDERS: {', '.join(pb.provider_order)} (est: ${est:.3f})")
    else:
        print(f"  PLAYBOOK: None — would use legacy fallback")
        return None

    t0 = time.monotonic()
    try:
        resp = await dispatch_playbook(pb.name, query, CTX)
        elapsed = time.monotonic() - t0
        _print_response(resp, elapsed)
        return resp
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"\n  ERROR after {elapsed:.1f}s: {e}")
        return None


async def main():
    total = 7

    print("=" * 90)
    print(f"{'ADAM RESEARCH PLATFORM — LIVE API TEST':^90}")
    print(f"{'Real providers, real data, no mocks':^90}")
    print("=" * 90)

    keys = _check_keys()

    results = []

    # 1. TRADES: Price check (SerpApi Shopping + Home Depot)
    resp = await run_scenario(1, total,
        "TRADES — Price Check (3 ton condenser)",
        "Price check on 3 ton HVAC condenser units",
    )
    results.append(("Price Check", resp))

    # 2. TRADES: Property facts (ATTOM)
    resp = await run_scenario(2, total,
        "TRADES — Property Facts (123 Main St)",
        "Pull the square footage and permit context for 123 Main St, Lexington KY 40509",
    )
    results.append(("Property Facts", resp))

    # 3. TRADES: Subcontractor scout (Google Places)
    resp = await run_scenario(3, total,
        "TRADES — Subcontractor Scout (roofers)",
        "Find 8 licensed-looking roofers within 15 miles of 40509",
    )
    results.append(("Sub Scout", resp))

    # 4. ACCOUNTING: Tax compliance (Exa)
    resp = await run_scenario(4, total,
        "ACCOUNTING — Tax Compliance (quarterly dates)",
        "Official quarterly estimated tax due dates for LLC",
    )
    results.append(("Tax Compliance", resp))

    # 5. LANDLORD: Property profile (HERE + ATTOM)
    resp = await run_scenario(5, total,
        "LANDLORD — Property Facts (full profile)",
        "Give me property facts for 245 Oak St sqft owner parcel",
        segment="landlord",
    )
    results.append(("Landlord Props", resp))

    # 6. ACCOUNTING: Prospect research (Google Places + Exa)
    resp = await run_scenario(6, total,
        "ACCOUNTING — Prospect Research",
        "Find bookkeeping prospects: 20 construction companies",
    )
    results.append(("Prospects", resp))

    # 7. TRAVEL: Hotel research (Tripadvisor + GP + Exa)
    resp = await run_scenario(7, total,
        "TRAVEL — Hotel Research (convention center)",
        "Find 5 business-friendly hotels near convention center",
    )
    results.append(("Hotels", resp))

    # SUMMARY
    print(f"\n{'=' * 90}")
    print(f"{'LIVE TEST SUMMARY':^90}")
    print(f"{'=' * 90}")
    print(f"  {'Scenario':<18} {'Artifact':<25} {'Records':<9} {'Confidence':<20} {'Status'}")
    print(f"  {'-'*18} {'-'*25} {'-'*9} {'-'*20} {'-'*8}")
    for name, resp in results:
        if resp and resp.artifact_type != "error":
            conf = resp.confidence.get("status", "?")
            print(f"  {name:<18} {resp.artifact_type:<25} {len(resp.records):<9} {conf:<20} PASS")
        elif resp:
            print(f"  {name:<18} {resp.artifact_type:<25} {0:<9} {'error':<20} FAIL")
        else:
            print(f"  {name:<18} {'N/A':<25} {0:<9} {'N/A':<20} SKIP")

    print(f"{'=' * 90}")


if __name__ == "__main__":
    asyncio.run(main())
