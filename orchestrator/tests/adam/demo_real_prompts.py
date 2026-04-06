"""Adam Research — REAL prompts, REAL APIs, user's address.

Tests Adam exactly as a user would talk to Ava.
"""

from __future__ import annotations

import asyncio
import os
import time
import json

os.environ["ASPIRE_ATTOM_API_KEY"] = os.environ.get("ATTOM_API_KEY", "")
os.environ["ASPIRE_EXA_API_KEY"] = os.environ.get("EXA_SEARCH_API_KEY", "")
os.environ["ASPIRE_SERPAPI_API_KEY"] = os.environ.get("SERPAPI_API_KEY", "")
os.environ["ASPIRE_TRIPADVISOR_API_KEY"] = os.environ.get("TRIPADVISOR_API_KEY", "")
os.environ["ASPIRE_PARALLEL_API_KEY"] = os.environ.get("PARALLEL_API_KEY", "")
os.environ["ASPIRE_BRAVE_API_KEY"] = os.environ.get("ASPIRE_BRAVE_API_KEY", "")
os.environ["ASPIRE_TAVILY_API_KEY"] = os.environ.get("ASPIRE_TAVILY_API_KEY", "")
os.environ["ASPIRE_GOOGLE_MAPS_API_KEY"] = os.environ.get("ASPIRE_GOOGLE_MAPS_API_KEY", "")
os.environ["ASPIRE_HERE_API_KEY"] = os.environ.get("ASPIRE_HERE_API_KEY", "")
os.environ["ASPIRE_FOURSQUARE_API_KEY"] = os.environ.get("ASPIRE_FOURSQUARE_API_KEY", "")
os.environ["ASPIRE_CREDENTIAL_STRICT_MODE"] = "0"

from aspire_orchestrator.services.adam.playbooks import dispatch_playbook
from aspire_orchestrator.services.adam.router import route_to_playbook
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.telemetry import estimate_provider_cost

CTX = PlaybookContext(
    suite_id="demo-plumber-001",
    office_id="office-atl-001",
    correlation_id="real-prompt-demo",
    tenant_id="demo-plumber-001",
)


def show(resp, elapsed):
    print(f"\n  ADAM SAYS:")
    print(f"  {'─' * 70}")
    if resp.summary:
        print(f"  {resp.summary[:200]}")
    print(f"\n  Artifact: {resp.artifact_type} | {len(resp.records)} records | {elapsed:.1f}s")

    if resp.verification_report:
        vr = resp.verification_report
        print(f"  Verified: {vr.status} (score: {vr.confidence_score:.2f}, {vr.source_count} sources, {len(vr.conflicts)} conflicts)")

    if resp.missing_fields:
        print(f"  Missing: {', '.join(resp.missing_fields)}")

    if resp.records:
        print(f"\n  DATA RETURNED:")
        for i, rec in enumerate(resp.records[:8]):
            if not isinstance(rec, dict):
                continue
            name = (rec.get("product_name") or rec.get("name") or
                    rec.get("normalized_address") or rec.get("title", "?"))
            parts = [name[:65]]

            for field in ["price", "extracted_price"]:
                if rec.get(field):
                    parts.append(f"${rec[field]}")
                    break
            if rec.get("rating"):
                parts.append(f"{rec['rating']}★")
            if rec.get("in_store_stock") is not None:
                parts.append(f"stock:{rec['in_store_stock']}")
            if rec.get("living_sqft"):
                parts.append(f"{rec['living_sqft']}sqft")
            if rec.get("year_built"):
                parts.append(f"built:{rec['year_built']}")
            if rec.get("phone"):
                parts.append(rec["phone"])
            if rec.get("percentage_off"):
                parts.append(f"{rec['percentage_off']}% off")
            if rec.get("traveler_rating"):
                parts.append(f"{rec['traveler_rating']}★")

            print(f"    [{i+1}] {' | '.join(parts)}")

    if resp.next_queries:
        print(f"\n  ADAM SUGGESTS NEXT:")
        for nq in resp.next_queries[:2]:
            print(f"    → {nq}")
    print()


async def test(num, title, prompt, segment=None):
    print(f"\n{'═' * 80}")
    print(f"  TEST {num}: {title}")
    print(f"  USER: \"{prompt}\"")
    if segment:
        print(f"  (tenant segment: {segment})")
    print(f"{'═' * 80}")

    cls, pb = route_to_playbook(prompt, tenant_segment=segment)
    print(f"  Route: {cls.segment}/{cls.intent} → {pb.name if pb else 'NONE'}")
    if pb:
        print(f"  Providers: {', '.join(pb.provider_order)}")

    if not pb:
        print(f"  ⚠ No playbook matched — would fall back to legacy search")
        return None

    t0 = time.monotonic()
    resp = await dispatch_playbook(pb.name, prompt, CTX)
    elapsed = time.monotonic() - t0

    show(resp, elapsed)
    return resp


async def main():
    print("=" * 80)
    print(f"{'ADAM RESEARCH — REAL USER PROMPTS':^80}")
    print(f"{'Testing like a real Aspire user talking to Ava':^80}")
    print("=" * 80)

    results = []

    # ──────────────────────────────────────────────────────────────────
    # TRADES scenarios — what a plumber/HVAC tech/roofer would ask
    # ──────────────────────────────────────────────────────────────────

    r = await test(1,
        "PLUMBER needs condenser pricing for a quote",
        "I need pricing on a 3 ton Goodman condenser for a customer quote"
    )
    results.append(("Condenser pricing", r))

    r = await test(2,
        "HVAC tech needs property info for job site",
        "Pull property facts for 4863 Price St, Forest Park, GA 30297"
    )
    results.append(("Property facts (GA)", r))

    r = await test(3,
        "GC looking for roofers in Atlanta",
        "Find me roofing contractors near Atlanta GA with good reviews"
    )
    results.append(("Roofers Atlanta", r))

    r = await test(4,
        "Plumber needs Milwaukee tools pricing",
        "Price check Milwaukee M18 FUEL impact driver and drill combo"
    )
    results.append(("Milwaukee tools", r))

    # ──────────────────────────────────────────────────────────────────
    # ACCOUNTING scenarios — what a bookkeeper/CPA would ask
    # ──────────────────────────────────────────────────────────────────

    r = await test(5,
        "CPA checking quarterly tax dates",
        "Official quarterly estimated tax due dates for LLC"
    )
    results.append(("Tax dates", r))

    r = await test(6,
        "Bookkeeper looking for new clients",
        "Find bookkeeping prospects: 20 construction companies"
    )
    results.append(("Prospects", r))

    # ──────────────────────────────────────────────────────────────────
    # LANDLORD scenarios — what a property investor would ask
    # ──────────────────────────────────────────────────────────────────

    r = await test(7,
        "LANDLORD wants property profile",
        "Give me the full property profile for 4863 Price St, Forest Park, GA 30297",
        segment="landlord"
    )
    results.append(("Landlord profile", r))

    r = await test(8,
        "LANDLORD checking tenant screening rules",
        "Screening rules for tenant application fair housing compliance",
        segment="landlord"
    )
    results.append(("Screening rules", r))

    # ──────────────────────────────────────────────────────────────────
    # TRAVEL scenario
    # ──────────────────────────────────────────────────────────────────

    r = await test(9,
        "Business trip hotel search",
        "Find 5 business-friendly hotels near Georgia World Congress Center in Atlanta"
    )
    results.append(("Hotels Atlanta", r))

    # ──────────────────────────────────────────────────────────────────
    # SUMMARY
    # ──────────────────────────────────────────────────────────────────
    print(f"\n{'═' * 80}")
    print(f"{'RESULTS SUMMARY':^80}")
    print(f"{'═' * 80}")
    print(f"  {'Test':<22} {'Records':<10} {'Confidence':<22} {'Status'}")
    print(f"  {'─'*22} {'─'*10} {'─'*22} {'─'*8}")
    for name, r in results:
        if r and r.artifact_type != "error":
            conf = r.confidence.get("status", "?") if r.confidence else "?"
            recs = len(r.records)
            status = "✓ PASS" if recs > 0 else "⚠ EMPTY"
            print(f"  {name:<22} {recs:<10} {conf:<22} {status}")
        elif r:
            print(f"  {name:<22} {'0':<10} {'error':<22} ✗ FAIL")
        else:
            print(f"  {name:<22} {'-':<10} {'-':<22} ⚠ SKIP")

    passed = sum(1 for _, r in results if r and r.artifact_type != "error" and len(r.records) > 0)
    total = len(results)
    print(f"\n  {passed}/{total} scenarios returned real data")
    print(f"{'═' * 80}")


if __name__ == "__main__":
    asyncio.run(main())
