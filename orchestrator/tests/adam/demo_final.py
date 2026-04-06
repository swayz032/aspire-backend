"""Adam Final Demo — Show exactly what Adam responds to each user prompt."""
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

CTX = PlaybookContext(
    suite_id="demo-plumber-001", office_id="office-atl-001",
    correlation_id="final-demo", tenant_id="demo-plumber-001",
)

SCENARIOS = [
    # (title, prompt, segment)
    ("PLUMBER asking Ava for condenser pricing",
     "I need pricing on a 3 ton Goodman condenser for a customer quote", None),

    ("HVAC TECH asking about a job site property",
     "Pull property facts for 4863 Price St, Forest Park, GA 30297", None),

    ("GC looking for roofers",
     "Find me roofing contractors near Atlanta GA with good reviews", None),

    ("PLUMBER needs Milwaukee tool pricing",
     "Price check Milwaukee M18 FUEL impact driver and drill combo", None),

    ("CPA asking about tax deadlines",
     "Official quarterly estimated tax due dates for LLC", None),

    ("BOOKKEEPER looking for clients",
     "Find bookkeeping prospects: construction companies near Atlanta", None),

    ("LANDLORD checking a property",
     "Give me the full property profile for 4863 Price St, Forest Park, GA 30297", "landlord"),

    ("LANDLORD asking about screening rules",
     "Screening rules for tenant application fair housing compliance", "landlord"),

    ("BUSINESS OWNER planning a trip",
     "Find 5 business-friendly hotels near Georgia World Congress Center in Atlanta", None),
]


async def main():
    print("=" * 85)
    print(" ADAM RESEARCH PLATFORM — WHAT THE USER SEES")
    print(" Each scenario = a real user talking to Ava, Ava invokes Adam, Adam responds")
    print("=" * 85)

    summary = []

    for i, (title, prompt, segment) in enumerate(SCENARIOS, 1):
        print(f"\n{'=' * 85}")
        print(f" [{i}/9] {title}")
        print(f" USER SAYS: \"{prompt}\"")
        print(f"{'=' * 85}")

        cls, pb = route_to_playbook(prompt, tenant_segment=segment)

        if not pb:
            print(f"\n ADAM RESPONSE: No specialized playbook — falling back to general web search.")
            summary.append((title, 0, "N/A", "SKIP"))
            continue

        t0 = time.monotonic()
        resp = await dispatch_playbook(pb.name, prompt, CTX)
        elapsed = time.monotonic() - t0

        is_error = resp.artifact_type == "error"
        recs = resp.records
        vr = resp.verification_report

        print(f"\n ADAM RESPONSE:")
        print(f" Playbook: {pb.name}")
        print(f" Artifact: {resp.artifact_type}")
        print(f" Time: {elapsed:.1f}s | Providers used: {', '.join(resp.providers_called)}")

        if vr:
            print(f" Verification: {vr.status} (confidence: {vr.confidence_score:.0%}, {vr.source_count} sources)")

        if resp.missing_fields:
            print(f" Missing fields: {', '.join(resp.missing_fields)}")

        if resp.summary:
            print(f"\n Summary: {resp.summary[:250]}")

        if recs:
            print(f"\n DATA ({len(recs)} records):")
            print(f" {'-' * 80}")
            for j, rec in enumerate(recs[:10]):
                if not isinstance(rec, dict):
                    continue

                # Adapt display to artifact type
                if resp.artifact_type == "PriceComparison":
                    name = rec.get("product_name", "?")[:55]
                    price = rec.get("price") or rec.get("extracted_price")
                    retailer = rec.get("retailer", "")
                    rating = rec.get("rating")
                    stock = rec.get("in_store_stock")
                    pct = rec.get("percentage_off")
                    line = f"  {j+1}. {name}"
                    if price: line += f"  —  ${price}"
                    if retailer: line += f"  ({retailer})"
                    if rating: line += f"  [{rating} stars]"
                    if stock is not None: line += f"  [in-stock: {stock}]"
                    if pct: line += f"  [{pct}% OFF]"
                    print(line)

                elif resp.artifact_type in ("VendorShortlist",):
                    name = rec.get("name", "?")[:50]
                    addr = rec.get("normalized_address", "")[:40]
                    phone = rec.get("phone", "")
                    rating = rec.get("rating")
                    line = f"  {j+1}. {name}"
                    if rating: line += f"  [{rating} stars]"
                    if addr: line += f"  — {addr}"
                    if phone: line += f"  {phone}"
                    print(line)

                elif resp.artifact_type in ("PropertyFactPack", "LandlordPropertyPack"):
                    addr = rec.get("normalized_address", "?")
                    sqft = rec.get("living_sqft")
                    year = rec.get("year_built")
                    beds = rec.get("bedrooms")
                    baths = rec.get("bathrooms")
                    assessed = rec.get("assessed_value")
                    line = f"  {j+1}. {addr}"
                    if sqft: line += f"  |  {sqft} sqft"
                    if year: line += f"  |  Built {year}"
                    if beds: line += f"  |  {beds}bd"
                    if baths: line += f"/{baths}ba"
                    if assessed: line += f"  |  Assessed: ${assessed:,}"
                    print(line)

                elif resp.artifact_type == "HotelShortlist":
                    name = rec.get("name", "?")[:50]
                    addr = rec.get("normalized_address", "")[:40]
                    rating = rec.get("traveler_rating") or rec.get("rating")
                    stars = rec.get("star_rating")
                    line = f"  {j+1}. {name}"
                    if stars: line += f"  [{stars}-star]"
                    if rating: line += f"  [{rating} traveler rating]"
                    if addr: line += f"  — {addr}"
                    print(line)

                elif resp.artifact_type in ("ProspectList", "ClientVerificationPack"):
                    name = rec.get("name", "?")[:50]
                    addr = rec.get("normalized_address", "")[:40]
                    rating = rec.get("rating")
                    category = rec.get("category", "")[:25]
                    line = f"  {j+1}. {name}"
                    if rating: line += f"  [{rating} stars]"
                    if category: line += f"  ({category})"
                    if addr: line += f"  — {addr}"
                    print(line)

                elif resp.artifact_type in ("ComplianceBrief", "ScreeningComplianceBrief"):
                    title_str = rec.get("title", "?")[:65]
                    url = rec.get("url", "")
                    domain = rec.get("domain", "")
                    line = f"  {j+1}. {title_str}"
                    if domain: line += f"  [{domain}]"
                    print(line)

                else:
                    name = (rec.get("name") or rec.get("title") or
                            rec.get("product_name") or rec.get("normalized_address") or "?")[:60]
                    print(f"  {j+1}. {name}")

        if resp.next_queries:
            print(f"\n ADAM SUGGESTS NEXT:")
            for nq in resp.next_queries[:3]:
                print(f"   -> {nq}")

        rcount = len(recs)
        conf = vr.status if vr else "N/A"
        status = "PASS" if rcount > 0 and not is_error else ("EMPTY" if not is_error else "FAIL")
        summary.append((title, rcount, conf, status))

    # FINAL SUMMARY
    print(f"\n\n{'=' * 85}")
    print(f" FINAL SCORECARD")
    print(f"{'=' * 85}")
    print(f" {'#':<4} {'Scenario':<45} {'Recs':<7} {'Confidence':<22} {'Result'}")
    print(f" {'─'*4} {'─'*45} {'─'*7} {'─'*22} {'─'*8}")
    for idx, (t, r, c, s) in enumerate(summary, 1):
        emoji = "PASS" if s == "PASS" else ("EMPTY" if s == "EMPTY" else "FAIL" if s == "FAIL" else "SKIP")
        print(f" {idx:<4} {t[:44]:<45} {r:<7} {c:<22} {emoji}")

    passed = sum(1 for _, r, _, s in summary if s == "PASS")
    print(f"\n {passed}/{len(summary)} scenarios returning real production data")
    print(f"{'=' * 85}")


asyncio.run(main())
