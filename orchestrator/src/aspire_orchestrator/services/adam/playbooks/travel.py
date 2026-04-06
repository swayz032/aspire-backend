"""TRAVEL Playbooks — hotel research for business trips.

Playbook: Business Trip Hotel Research
Strategy: Google Places (primary, full data) + TripAdvisor (enrichment via Details API)
Guardrail: research and recommendation only, NO booking in v1.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.schemas.research_response import ResearchResponse
from aspire_orchestrator.services.adam.verifier import verify_records

logger = logging.getLogger(__name__)


async def execute_business_trip_hotel_research(
    query: str,
    ctx: PlaybookContext,
    destination: str = "",
    meeting_address: str = "",
    budget_max: float | None = None,
    preferences: list[str] | None = None,
) -> ResearchResponse:
    """BUSINESS_TRIP_HOTEL_RESEARCH — Find hotel options with full details.

    Strategy:
      1. Google Places text search (type=lodging) — primary source for address,
         rating, reviews, phone, website, price level
      2. TripAdvisor Search → Details API — hotel class, ranking, amenities,
         price level, reviews, web URL
      3. Dedup by name, merge best data from both sources
      4. Exa for sentiment/review enrichment on top picks

    Guardrail: research and recommendation only, NO booking in v1.
    """
    from aspire_orchestrator.providers.google_places_client import execute_google_places_search
    from aspire_orchestrator.providers.tripadvisor_client import (
        execute_tripadvisor_search,
        execute_tripadvisor_location_details,
    )
    from aspire_orchestrator.providers.exa_client import execute_exa_search
    from aspire_orchestrator.services.adam.normalizers.hotel_normalizer import (
        normalize_from_tripadvisor,
        normalize_from_google_places_hotel,
    )

    logger.info("Executing BUSINESS_TRIP_HOTEL_RESEARCH for: %s", query[:80])

    # Extract location from query if destination not provided
    import re
    location = destination
    if not location:
        # Try to extract "in <City> <State>" pattern
        loc_match = re.search(r'\bin\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:,?\s*[A-Z]{2})?)', query)
        if loc_match:
            location = loc_match.group(1)
        else:
            location = query
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    seen_names: set[str] = set()

    # Step 1: Google Places — TWO searches for maximum coverage
    # Search A: tight "hotels in {location}" for local results
    # Search B: broader "hotels near {location}" for nearby area
    gp_a, gp_b = await asyncio.gather(
        execute_google_places_search(
            payload={"query": f"hotels in {location}", "type": "lodging"},
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
        ),
        execute_google_places_search(
            payload={"query": f"affordable hotels near {location}"},
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
        ),
    )
    providers_called.append("google_places")

    # Merge both GP result sets
    for gp_result in (gp_a, gp_b):
        if gp_result.outcome.value == "success" and gp_result.data:
            for place in gp_result.data.get("results", [])[:10]:
                hotel = normalize_from_google_places_hotel(place)
                if hotel.name:
                    name_key = hotel.name.lower().strip()
                    if name_key not in seen_names:
                        seen_names.add(name_key)
                        records.append(hotel.to_dict())
                        sources.extend(hotel.sources)

    # Step 2: TripAdvisor Search → Details for each hotel (parallel)
    ta_search = await execute_tripadvisor_search(
        payload={"query": f"hotels in {location}", "category": "hotels", "language": "en"},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("tripadvisor")

    if ta_search.outcome.value == "success" and ta_search.data:
        ta_locations = ta_search.data.get("results", [])

        # Get details for each TA location (parallel — up to 10)
        detail_tasks = []
        for loc in ta_locations[:10]:
            loc_id = loc.get("location_id", "")
            if loc_id:
                detail_tasks.append(execute_tripadvisor_location_details(
                    location_id=loc_id,
                    correlation_id=ctx.correlation_id,
                    suite_id=ctx.suite_id,
                    office_id=ctx.office_id,
                ))

        if detail_tasks:
            detail_results = await asyncio.gather(*detail_tasks, return_exceptions=True)

            for dr in detail_results:
                if isinstance(dr, Exception):
                    continue
                if dr.outcome.value != "success" or not dr.data:
                    continue

                d = dr.data
                name = d.get("name", "")
                name_key = name.lower().strip()

                # Geographic filter — skip hotels not in target area
                addr_obj = d.get("address_obj", {}) or {}
                ta_state = (addr_obj.get("state") or "").strip()
                ta_city = (addr_obj.get("city") or "").strip()
                ta_country = (addr_obj.get("country") or "").strip()

                # Extract target city/state from location (e.g., "Tucker" + "GA" from "Tucker GA")
                loc_parts = location.split()
                target_state = loc_parts[-1] if len(loc_parts) >= 2 and len(loc_parts[-1]) == 2 else ""
                target_city_name = " ".join(loc_parts[:-1]) if target_state else location

                # Skip if wrong country (not US)
                if ta_country and ta_country.lower() not in ("united states", "us", "usa", ""):
                    logger.debug("Skipping TA hotel %s — wrong country: %s", name, ta_country)
                    continue
                # Skip if wrong state
                if target_state and ta_state:
                    state_ok = _state_matches(target_state, ta_state)
                    if not state_ok:
                        logger.debug("Skipping TA hotel %s — wrong state: %s vs %s", name, ta_state, target_state)
                        continue
                # Skip if city doesn't match target (allows nearby cities in same metro)
                if target_city_name and ta_city:
                    city_match = (
                        target_city_name.lower() == ta_city.lower()
                        or target_city_name.lower() in ta_city.lower()
                        or ta_city.lower() in target_city_name.lower()
                    )
                    if not city_match:
                        logger.debug("Skipping TA hotel %s — wrong city: %s vs %s", name, ta_city, target_city_name)
                        continue

                # Build hotel dict from TA details
                address = ", ".join(filter(None, [
                    addr_obj.get("street1", ""),
                    addr_obj.get("city", ""),
                    addr_obj.get("state", ""),
                    addr_obj.get("postalcode", ""),
                ]))
                ranking = d.get("ranking_data", {}) or {}

                # Parse subratings
                subratings: dict[str, float] = {}
                for _sr_key, sr_val in (d.get("subratings") or {}).items():
                    if isinstance(sr_val, dict) and sr_val.get("localized_name"):
                        sr_v = _safe_float(sr_val.get("value"))
                        if sr_v is not None:
                            subratings[sr_val["localized_name"]] = sr_v

                # Parse rating breakdown
                rating_breakdown: dict[str, int] = {}
                for stars, count in (d.get("review_rating_count") or {}).items():
                    ct = _safe_int(count)
                    if ct is not None:
                        rating_breakdown[stars] = ct

                # Parse trip types
                trip_types: dict[str, int] = {}
                for tt in d.get("trip_types", []):
                    if isinstance(tt, dict) and tt.get("localized_name"):
                        tv = _safe_int(tt.get("value"))
                        if tv is not None:
                            trip_types[tt["localized_name"]] = tv

                # Parse amenities
                amenities_list = d.get("amenities", [])
                if amenities_list and isinstance(amenities_list[0], dict):
                    amenities_list = [a.get("name", "") for a in amenities_list if isinstance(a, dict)]

                ta_hotel = {
                    "name": name,
                    "normalized_address": address or d.get("address_string", ""),
                    "city": addr_obj.get("city", ""),
                    "state": addr_obj.get("state", ""),
                    "postal_code": addr_obj.get("postalcode", ""),
                    "star_rating": _safe_float(d.get("hotel_class")),
                    "traveler_rating": _safe_float(d.get("rating")),
                    "review_count": _safe_int(d.get("num_reviews")),
                    "rating_breakdown": rating_breakdown,
                    "subratings": subratings,
                    "price_range": d.get("price_level", ""),
                    "styles": d.get("styles", []),
                    "phone": d.get("phone", ""),
                    "website": d.get("website", ""),
                    "tripadvisor_url": d.get("web_url", ""),
                    "amenities": amenities_list,
                    "description": d.get("description", ""),
                    "sentiment_summary": ranking.get("ranking_string", ""),
                    "ta_ranking": ranking.get("ranking", ""),
                    "trip_types": trip_types,
                    "latitude": _safe_float(d.get("latitude")),
                    "longitude": _safe_float(d.get("longitude")),
                    "photo_count": _safe_int(d.get("photo_count")),
                    "sources": [{"provider": "tripadvisor"}],
                }

                # Match TA hotel to existing GP record — try exact, substring, fuzzy
                matched_rec = None
                for rec in records:
                    rec_key = rec.get("name", "").lower().strip()
                    # Exact match
                    if rec_key == name_key:
                        matched_rec = rec
                        break
                    # Substring match (one name contains the other)
                    if name_key in rec_key or rec_key in name_key:
                        matched_rec = rec
                        break
                if not matched_rec:
                    # Fuzzy: 2+ significant word overlap
                    stop_words = {"hotel", "inn", "suites", "by", "the", "&", "and", "a", "an",
                                  "extended", "stay", "express", "-", "ga", "atlanta"}
                    name_words = set(name_key.split()) - stop_words
                    for rec in records:
                        rec_words = set(rec.get("name", "").lower().split()) - stop_words
                        if len(name_words & rec_words) >= 2:
                            matched_rec = rec
                            break

                if matched_rec:
                    rec = matched_rec
                    # Merge all TA fields GP doesn't have
                    merge_fields = [
                        "star_rating", "price_range", "amenities", "styles",
                        "sentiment_summary", "ta_ranking", "description",
                        "subratings", "rating_breakdown", "trip_types",
                        "tripadvisor_url", "photo_count",
                    ]
                    for field in merge_fields:
                        if ta_hotel.get(field) and not rec.get(field):
                            rec[field] = ta_hotel[field]
                    if ta_hotel.get("review_count") and not rec.get("ta_review_count"):
                        rec["ta_review_count"] = ta_hotel["review_count"]
                        rec["ta_rating"] = ta_hotel.get("traveler_rating")
                    rec["sources"].append({"provider": "tripadvisor"})
                else:
                    # New hotel not in Google Places — add it
                    seen_names.add(name_key)
                    records.append(ta_hotel)
                    sources.append(SourceAttribution(provider="tripadvisor"))

    # Step 3: Exa for review sentiment on top 3 hotels
    top_names = [r.get("name", "") for r in records[:3] if r.get("name")]
    if top_names:
        exa_query = f"{' vs '.join(top_names)} hotel reviews {location}"
        exa_result = await execute_exa_search(
            payload={"query": exa_query, "num_results": 3, "moderation": True},
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
        )
        providers_called.append("exa")
        if exa_result.outcome.value == "success" and exa_result.data:
            for r in exa_result.data.get("results", [])[:3]:
                snippet = r.get("text", r.get("highlight", ""))
                if snippet and records:
                    # Attach review snippets to first matching hotel
                    for rec in records:
                        if rec.get("name", "").lower() in str(snippet).lower():
                            rec["web_review_snippet"] = str(snippet)[:300]
                            break
            sources.append(SourceAttribution(provider="exa"))

    # Step 4: Safety scoring — protect our users from sketchy hotels
    # Extract city name for location relevance boost
    city_name = location.split(",")[0].split()[-1] if location else ""  # "Tucker" from "Tucker GA"
    for rec in records:
        safety = _compute_safety_score(rec, target_city=city_name)
        rec["safety_score"] = safety["score"]
        rec["safety_verdict"] = safety["verdict"]
        rec["safety_flags"] = safety["flags"]

    # Sort: recommended hotels first, then by rating
    def _sort_key(r: dict) -> tuple:
        verdict_order = {"Recommended for business travel": 0, "Acceptable": 1, "Use caution": 2, "Not recommended": 3}
        return (verdict_order.get(r.get("safety_verdict", ""), 9), -(r.get("traveler_rating") or 0))
    records.sort(key=_sort_key)

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["name", "normalized_address", "traveler_rating"],
    )

    return ResearchResponse(
        artifact_type="HotelShortlist",
        summary=(
            f"Found {len(records)} hotels in {location}. "
            f"Providers: {'+'.join(providers_called)}. "
            f"Verification: {report.status}."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=list(report.missing_fields),
        next_queries=[
            f"Compare prices on booking sites for {location}",
            "Check amenities and parking details",
            f"Find restaurants near hotels in {location}",
        ],
        verification_report=report,
        segment="travel",
        intent="hotel_research",
        playbook="BUSINESS_TRIP_HOTEL_RESEARCH",
        providers_called=providers_called,
    )


_STATE_ABBREV = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}


def _state_matches(target_abbrev: str, ta_state: str) -> bool:
    """Check if a state abbreviation matches a TA state name (could be full name or abbreviation)."""
    t = target_abbrev.upper()
    s = ta_state.strip().upper()
    if t == s:
        return True
    full_name = _STATE_ABBREV.get(t, "")
    if full_name and full_name == s.lower():
        return True
    # Reverse: ta_state might be abbreviation and target might be full
    return False


def _compute_safety_score(hotel: dict[str, Any], target_city: str = "") -> dict[str, Any]:
    """Compute a safety/quality score for business travelers.

    Factors:
      - Overall rating (Google or TA)
      - Review count (more reviews = more reliable signal)
      - Cleanliness subrating (from TA)
      - Location subrating (from TA)
      - 1-star review percentage (high = red flag)
      - Hotel style ("Budget" = caution, "Business"/"Luxury" = positive)
      - Known sketchy chain detection
      - Location relevance (in target city = bonus)

    Returns: {"score": 1-10, "verdict": str, "flags": list[str]}
    """
    flags: list[str] = []
    score = 5.0  # Start neutral

    # Factor 1: Overall rating
    rating = hotel.get("traveler_rating") or hotel.get("ta_rating") or 0
    if rating >= 4.3:
        score += 2.0
    elif rating >= 3.8:
        score += 1.0
    elif rating >= 3.3:
        score += 0.0
    elif rating >= 2.5:
        score -= 1.5
        flags.append("Low rating (%.1f/5)" % rating)
    elif rating > 0:
        score -= 3.0
        flags.append("Very low rating (%.1f/5)" % rating)

    # Factor 2: Review volume (more reviews = more trustworthy signal)
    reviews = hotel.get("review_count") or 0
    if reviews >= 1000:
        score += 0.5  # Well-known property
    elif reviews < 50 and rating < 3.5:
        score -= 0.5
        flags.append("Few reviews (%d) — limited data" % reviews)

    # Factor 3: Cleanliness subrating (from TA)
    subratings = hotel.get("subratings", {})
    cleanliness = subratings.get("Cleanliness", subratings.get("cleanliness"))
    if cleanliness is not None:
        if isinstance(cleanliness, (int, float)):
            if cleanliness < 3.0:
                score -= 2.0
                flags.append("Low cleanliness rating (%.1f/5)" % cleanliness)
            elif cleanliness >= 4.0:
                score += 0.5

    # Factor 4: Location subrating
    loc_rating = subratings.get("Location", subratings.get("location"))
    if loc_rating is not None:
        if isinstance(loc_rating, (int, float)):
            if loc_rating < 3.0:
                score -= 1.0
                flags.append("Poor location rating (%.1f/5)" % loc_rating)

    # Factor 5: 1-star review percentage
    breakdown = hotel.get("rating_breakdown", {})
    if breakdown:
        total_reviews = sum(int(v) for v in breakdown.values() if str(v).isdigit())
        one_star = int(breakdown.get("1", 0))
        if total_reviews > 10:
            one_star_pct = one_star / total_reviews * 100
            if one_star_pct > 30:
                score -= 2.0
                flags.append("%.0f%% of reviews are 1-star" % one_star_pct)
            elif one_star_pct > 20:
                score -= 1.0
                flags.append("%.0f%% 1-star reviews" % one_star_pct)

    # Factor 6: Hotel style
    styles = hotel.get("styles", [])
    style_lower = [s.lower() for s in styles]
    if "business" in style_lower or "luxury" in style_lower:
        score += 1.0
    if "budget" in style_lower:
        score -= 0.5
        if rating < 3.5:
            flags.append("Budget hotel with low rating")

    # Factor 7: Known sketchy chain detection
    name_lower = (hotel.get("name") or "").lower()
    cautious_chains = ["motel 6", "studio 6", "knights inn", "rodeway inn",
                       "econo lodge", "americas best", "red roof"]
    premium_chains = ["hilton", "marriott", "hyatt", "doubletree", "holiday inn",
                      "hampton inn", "courtyard", "fairfield", "springhill",
                      "residence inn", "homewood", "embassy suites"]
    for chain in cautious_chains:
        if chain in name_lower:
            score -= 1.0
            flags.append("Budget chain — verify conditions before booking")
            break
    for chain in premium_chains:
        if chain in name_lower:
            score += 1.0
            break

    # Factor 8: Location relevance — hotels in the target city get a small boost
    if target_city:
        addr = (hotel.get("normalized_address") or "").lower()
        city = (hotel.get("city") or "").lower()
        if target_city.lower() in addr or target_city.lower() in city:
            score += 0.5

    # Clamp score
    score = max(1.0, min(10.0, score))

    # Verdict
    if score >= 7.5:
        verdict = "Recommended for business travel"
    elif score >= 5.5:
        verdict = "Acceptable"
    elif score >= 3.5:
        verdict = "Use caution"
    else:
        verdict = "Not recommended"

    return {"score": round(score, 1), "verdict": verdict, "flags": flags}


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None
