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

    Time-budgeted execution — completes within 30s for voice responsiveness.
    All data is preserved; optional enrichment stages degrade gracefully
    under time pressure (they still run, just with tighter per-call timeouts).

    Strategy:
      Phase 1 (0-8s):   Google Places + TripAdvisor search — ALL in parallel
      Phase 2 (8-18s):  TripAdvisor detail enrichment (top 8 locations)
      Phase 3 (18-30s): Name enrichment + Exa sentiment + photos — ALL in parallel
      Safety scoring + sort always runs (pure CPU, ~0s)

    Guardrail: research and recommendation only, NO booking in v1.
    """
    import time as _time
    from aspire_orchestrator.providers.google_places_client import execute_google_places_search
    from aspire_orchestrator.providers.tripadvisor_client import (
        execute_tripadvisor_search,
        execute_tripadvisor_location_details,
        execute_tripadvisor_location_photos,
    )
    from aspire_orchestrator.providers.exa_client import execute_exa_search
    from aspire_orchestrator.services.adam.normalizers.hotel_normalizer import (
        normalize_from_tripadvisor,
        normalize_from_google_places_hotel,
    )

    _t0 = _time.monotonic()
    _TIME_BUDGET_SECS = 30.0  # Total budget — leaves 15s margin in 45s playbook timeout

    def _elapsed() -> float:
        return _time.monotonic() - _t0

    def _remaining() -> float:
        return max(0.0, _TIME_BUDGET_SECS - _elapsed())

    logger.info("Executing BUSINESS_TRIP_HOTEL_RESEARCH for: %s", query[:80])

    # Extract location from query if destination not provided
    import re
    location = destination
    if not location:
        loc_match = re.search(r'\bin\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:,?\s*[A-Z]{2})?)', query)
        if loc_match:
            location = loc_match.group(1)
        else:
            location = query
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    seen_names: set[str] = set()

    # ── Phase 1: Google Places + TripAdvisor searches — ALL in parallel ──
    # Running GP and TA simultaneously saves 3-5s vs sequential.
    # TA text search uses location string directly (no GP center point needed).
    gp_a_task = execute_google_places_search(
        payload={"query": f"hotels in {location}", "type": "lodging"},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    gp_b_task = execute_google_places_search(
        payload={"query": f"affordable hotels near {location}"},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    ta_text_task = execute_tripadvisor_search(
        payload={"query": f"hotels in {location}", "category": "hotels", "language": "en"},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )

    gp_a, gp_b, ta_text = await asyncio.gather(
        gp_a_task, gp_b_task, ta_text_task, return_exceptions=True,
    )
    providers_called.append("google_places")

    # Process GP results + compute center lat/lng
    all_lats: list[float] = []
    all_lngs: list[float] = []
    for gp_result in (gp_a, gp_b):
        if isinstance(gp_result, Exception):
            continue
        if gp_result.outcome.value == "success" and gp_result.data:
            for place in gp_result.data.get("results", [])[:10]:
                hotel = normalize_from_google_places_hotel(place)
                if hotel.name:
                    name_key = hotel.name.lower().strip()
                    if name_key not in seen_names:
                        seen_names.add(name_key)
                        records.append(hotel.to_dict())
                        sources.extend(hotel.sources)
                        if hotel.latitude and hotel.longitude:
                            all_lats.append(hotel.latitude)
                            all_lngs.append(hotel.longitude)

    center_lat = sum(all_lats) / len(all_lats) if all_lats else None
    center_lng = sum(all_lngs) / len(all_lngs) if all_lngs else None
    lat_long_str = f"{center_lat},{center_lng}" if center_lat and center_lng else ""

    # Now run TA geo search (needs center point) — fast single call
    ta_location_ids: dict[str, str] = {}
    if lat_long_str:
        ta_geo_payload = {
            "query": f"hotels {location}", "category": "hotels", "language": "en",
            "latLong": lat_long_str, "radius": 10, "radiusUnit": "mi",
        }
        try:
            ta_geo = await asyncio.wait_for(
                execute_tripadvisor_search(
                    payload=ta_geo_payload,
                    correlation_id=ctx.correlation_id,
                    suite_id=ctx.suite_id,
                    office_id=ctx.office_id,
                ),
                timeout=min(8.0, _remaining()),
            )
            if ta_geo.outcome.value == "success" and ta_geo.data:
                for loc in ta_geo.data.get("results", []):
                    lid = loc.get("location_id", "")
                    if lid and lid not in ta_location_ids:
                        ta_location_ids[lid] = loc.get("name", "")
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("TA geo search timed out or failed: %s", e)

    providers_called.append("tripadvisor")

    # Process TA text search results
    if not isinstance(ta_text, Exception) and ta_text.outcome.value == "success" and ta_text.data:
        for loc in ta_text.data.get("results", []):
            lid = loc.get("location_id", "")
            if lid and lid not in ta_location_ids:
                ta_location_ids[lid] = loc.get("name", "")

    logger.info("Phase 1 complete: %d GP records, %d TA locations in %.1fs",
                len(records), len(ta_location_ids), _elapsed())

    # ── Phase 2: TripAdvisor detail enrichment (parallel, top 8) ──
    if ta_location_ids and _remaining() > 5.0:
        detail_tasks = [
            execute_tripadvisor_location_details(
                location_id=lid,
                correlation_id=ctx.correlation_id,
                suite_id=ctx.suite_id,
                office_id=ctx.office_id,
            )
            for lid in list(ta_location_ids.keys())[:8]
        ]
        try:
            detail_results = await asyncio.wait_for(
                asyncio.gather(*detail_tasks, return_exceptions=True),
                timeout=min(12.0, _remaining()),
            )
            for dr in detail_results:
                if isinstance(dr, Exception):
                    continue
                if dr.outcome.value != "success" or not dr.data:
                    continue
                _merge_ta_detail_into_records(
                    dr.data, records, seen_names, sources, center_lat, center_lng, location,
                )
        except asyncio.TimeoutError:
            logger.warning("TA detail enrichment hit time budget at %.1fs", _elapsed())

    logger.info("Phase 2 complete: %d records in %.1fs", len(records), _elapsed())

    # ── Phase 3: Name enrichment + Exa + Photos — ALL in parallel ──
    # These are optional enrichment. Run them concurrently within remaining budget.
    phase3_tasks: list[asyncio.Task] = []

    # 3a. Name enrichment for unenriched GP hotels
    async def _enrich_unenriched() -> None:
        unenriched = [
            r for r in records
            if not any(s.get("provider") == "tripadvisor" for s in r.get("sources", []))
        ]
        if not unenriched:
            return
        enrich_tasks = []
        enrich_names = []
        for rec in unenriched[:5]:
            hotel_name = rec.get("name", "")
            if hotel_name:
                enrich_tasks.append(execute_tripadvisor_search(
                    payload={"query": hotel_name, "category": "hotels", "language": "en"},
                    correlation_id=ctx.correlation_id,
                    suite_id=ctx.suite_id,
                    office_id=ctx.office_id,
                ))
                enrich_names.append(hotel_name)

        if not enrich_tasks:
            return
        enrich_results = await asyncio.gather(*enrich_tasks, return_exceptions=True)

        enrich_detail_tasks = []
        for idx, er in enumerate(enrich_results):
            if isinstance(er, Exception) or er.outcome.value != "success" or not er.data:
                continue
            gp_name = enrich_names[idx].lower()
            for loc in er.data.get("results", [])[:3]:
                lid = loc.get("location_id", "")
                ta_name = (loc.get("name") or "").lower()
                if not lid or lid in ta_location_ids:
                    continue
                if _names_match(gp_name, ta_name):
                    ta_location_ids[lid] = loc.get("name", "")
                    enrich_detail_tasks.append(execute_tripadvisor_location_details(
                        location_id=lid,
                        correlation_id=ctx.correlation_id,
                        suite_id=ctx.suite_id,
                        office_id=ctx.office_id,
                    ))
                    break

        if enrich_detail_tasks:
            enrich_details = await asyncio.gather(*enrich_detail_tasks, return_exceptions=True)
            for dr in enrich_details:
                if isinstance(dr, Exception) or dr.outcome.value != "success" or not dr.data:
                    continue
                _merge_ta_detail_into_records(
                    dr.data, records, seen_names, sources, center_lat, center_lng, location,
                )

    # 3b. Exa sentiment on top 3 hotels
    async def _exa_sentiment() -> None:
        top_names = [r.get("name", "") for r in records[:3] if r.get("name")]
        if not top_names:
            return
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
                    for rec in records:
                        if rec.get("name", "").lower() in str(snippet).lower():
                            rec["web_review_snippet"] = str(snippet)[:300]
                            break
            sources.append(SourceAttribution(provider="exa"))

    # 3c. Photos for TA-enriched hotels (top 5)
    async def _fetch_photos() -> None:
        photo_tasks_inner = []
        photo_record_map: list[dict] = []
        count = 0
        for rec in records:
            if count >= 5:
                break
            ta_srcs = [s for s in rec.get("sources", []) if s.get("provider") == "tripadvisor"]
            if ta_srcs and rec.get("tripadvisor_url"):
                for lid, lname in ta_location_ids.items():
                    if _names_match(rec.get("name", "").lower(), lname.lower()):
                        photo_tasks_inner.append(execute_tripadvisor_location_photos(
                            location_id=lid,
                            correlation_id=ctx.correlation_id,
                            suite_id=ctx.suite_id,
                            office_id=ctx.office_id,
                        ))
                        photo_record_map.append(rec)
                        count += 1
                        break

        if photo_tasks_inner:
            photo_results = await asyncio.gather(*photo_tasks_inner, return_exceptions=True)
            for pr, rec in zip(photo_results, photo_record_map):
                if isinstance(pr, Exception):
                    continue
                if pr.outcome.value == "success" and pr.data:
                    photos = pr.data.get("photos", [])
                    if photos:
                        rec["photos"] = photos
                        rec["photo_count"] = len(photos)

    # Run all Phase 3 tasks concurrently within remaining time budget
    if _remaining() > 3.0:
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _enrich_unenriched(),
                    _exa_sentiment(),
                    _fetch_photos(),
                    return_exceptions=True,
                ),
                timeout=min(15.0, _remaining()),
            )
        except asyncio.TimeoutError:
            logger.warning("Phase 3 enrichment hit time budget at %.1fs", _elapsed())
    else:
        logger.info("Skipping Phase 3 enrichment — only %.1fs remaining", _remaining())

    logger.info("All phases complete: %d records in %.1fs", len(records), _elapsed())

    # ── Safety scoring + sort (pure CPU, instant) ──
    city_name = location.split(",")[0].split()[-1] if location else ""
    for rec in records:
        safety = _compute_safety_score(rec, target_city=city_name)
        rec["safety_score"] = safety["score"]
        rec["safety_verdict"] = safety["verdict"]
        rec["safety_flags"] = safety["flags"]

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


def _merge_ta_detail_into_records(
    d: dict[str, Any],
    records: list[dict[str, Any]],
    seen_names: set[str],
    sources: list[SourceAttribution],
    center_lat: float | None,
    center_lng: float | None,
    location: str,
) -> None:
    """Process a TA Location Details response and merge into existing records.

    Geographic filter: uses lat/lng proximity (30 mile radius from GP center)
    + state matching as fallback. Works for any city universally.
    """
    name = d.get("name", "")
    name_key = name.lower().strip()

    addr_obj = d.get("address_obj", {}) or {}
    ta_lat = _safe_float(d.get("latitude"))
    ta_lng = _safe_float(d.get("longitude"))
    ta_state = (addr_obj.get("state") or "").strip()
    ta_country = (addr_obj.get("country") or "").strip()

    # Geographic filter — proximity-based (universal, works for any area)
    # Skip if wrong country
    if ta_country and ta_country.lower() not in ("united states", "us", "usa", ""):
        logger.debug("Skipping TA hotel %s — wrong country: %s", name, ta_country)
        return

    # Primary filter: lat/lng proximity (30 mile radius from GP center)
    if center_lat and center_lng and ta_lat and ta_lng:
        dist_miles = _haversine_miles(center_lat, center_lng, ta_lat, ta_lng)
        if dist_miles > 30:
            logger.debug("Skipping TA hotel %s — %.1f miles away", name, dist_miles)
            return
    else:
        # Fallback: state matching when no coordinates available
        loc_parts = location.split()
        target_state = loc_parts[-1] if len(loc_parts) >= 2 and len(loc_parts[-1]) == 2 else ""
        if target_state and ta_state:
            if not _state_matches(target_state, ta_state):
                logger.debug("Skipping TA hotel %s — wrong state: %s", name, ta_state)
                return

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
        "latitude": ta_lat,
        "longitude": ta_lng,
        "photo_count": _safe_int(d.get("photo_count")),
        "sources": [{"provider": "tripadvisor"}],
    }

    # Match TA hotel to existing GP record — try exact, substring, fuzzy
    matched_rec = None
    for rec in records:
        rec_key = rec.get("name", "").lower().strip()
        if rec_key == name_key:
            matched_rec = rec
            break
        if name_key in rec_key or rec_key in name_key:
            matched_rec = rec
            break
    if not matched_rec:
        # Fuzzy match using strict name comparison (prevents cross-hotel contamination)
        for rec in records:
            if _names_match(name_key, rec.get("name", "")):
                matched_rec = rec
                break

    if matched_rec:
        # Merge all TA fields GP doesn't have
        merge_fields = [
            "star_rating", "price_range", "amenities", "styles",
            "sentiment_summary", "ta_ranking", "description",
            "subratings", "rating_breakdown", "trip_types",
            "tripadvisor_url", "photo_count", "city", "state", "postal_code",
        ]
        for field in merge_fields:
            if ta_hotel.get(field) and not matched_rec.get(field):
                matched_rec[field] = ta_hotel[field]
        if ta_hotel.get("review_count") and not matched_rec.get("ta_review_count"):
            matched_rec["ta_review_count"] = ta_hotel["review_count"]
            matched_rec["ta_rating"] = ta_hotel.get("traveler_rating")
        if not matched_rec.get("phone") and ta_hotel.get("phone"):
            matched_rec["phone"] = ta_hotel["phone"]
        if not matched_rec.get("website") and ta_hotel.get("website"):
            matched_rec["website"] = ta_hotel["website"]
        matched_rec["sources"].append({"provider": "tripadvisor"})
    else:
        # New hotel not in Google Places — add it
        seen_names.add(name_key)
        records.append(ta_hotel)
        sources.append(SourceAttribution(provider="tripadvisor"))


def _names_match(name_a: str, name_b: str) -> bool:
    """Check if two hotel names refer to the same property.

    Strict matching — prevents cross-hotel contamination.
    City/neighborhood/state names are NOT identity words for a hotel.
    """
    a = name_a.lower().strip()
    b = name_b.lower().strip()
    # Exact
    if a == b:
        return True
    # Substring (one name fully contains the other)
    if a in b or b in a:
        return True
    # Significant word overlap — exclude filler AND geographic words
    # Geographic words (cities, neighborhoods, states) are NOT hotel identity
    stop = {"hotel", "inn", "suites", "by", "the", "&", "and", "a", "an",
            "extended", "stay", "express", "-", "at", "of", "in", "near",
            # State abbreviations and common city/neighborhood words
            "ga", "fl", "tx", "ca", "ny", "nc", "sc", "va", "oh", "pa",
            "atlanta", "norcross", "tucker", "chamblee", "stone", "mountain",
            "northlake", "downtown", "midtown", "airport", "north", "south",
            "east", "west", "northeast", "northwest", "southeast", "southwest",
            "central", "metro", "area", "city", "park", "heights", "hills",
            "village", "plaza", "center", "centre", "square"}
    words_a = set(a.replace(",", " ").replace("-", " ").split()) - stop
    words_b = set(b.replace(",", " ").replace("-", " ").split()) - stop
    overlap = words_a & words_b
    # Need 2+ identity words AND >60% of the smaller set
    min_words = min(len(words_a), len(words_b))
    if min_words > 0 and len(overlap) >= 2 and len(overlap) / min_words > 0.6:
        return True
    return False


def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate distance in miles between two lat/lng points."""
    import math
    R = 3959  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


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
