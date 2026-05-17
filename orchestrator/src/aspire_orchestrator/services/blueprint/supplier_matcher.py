"""Supplier matcher — hybrid Home Depot + Google Places supplier discovery for PROCURE stage.

Matches each blueprint_materials line item to up to 5 nearby suppliers within a geofence,
merging results from:
  1. SerpApi Home Depot search (product availability + pricing)
  2. Google Places text search (lumber yards, electrical wholesalers, other building suppliers)

Law compliance:
  Law #1: Returns ranked matches; Drew.procure() decides which supplier to persist. No
           autonomous write decisions here.
  Law #2: Emits a blueprint.procure.supplier_search receipt for every call.
  Law #3: If SerpApi/Google Places keys missing → fail-closed. Missing input row instead.
  Law #6: office location is fetched scoped by suite_id/office_id.
  Law #9: Never log raw supplier addresses, phone numbers, or full supplier blocks.
           Log only line_item (max 100 chars), match_count, provider_mix.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SupplierMatch:
    """A single supplier candidate returned by match_suppliers."""

    name: str
    address: str
    distance_miles: float
    has_in_stock: bool
    provider: str            # "home_depot" | "google_places"
    contact_phone: str
    product_url: str | None


@dataclass
class SupplierSearchResult:
    """Full result of a supplier search call."""

    matches: list[SupplierMatch] = field(default_factory=list)
    below_minimum: bool = False   # True when <3 suppliers found within geofence
    missing_input_inserted: bool = False
    provider_mix: dict[str, int] = field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------------------
# Haversine distance helper
# ---------------------------------------------------------------------------

def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in miles between two lat/lng points."""
    r_miles = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return r_miles * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _miles_to_meters(miles: float) -> int:
    """Convert miles to meters (capped at Google Places API max of 50000m)."""
    return min(50_000, int(miles * 1609.34))


# ---------------------------------------------------------------------------
# Office location fetch
# ---------------------------------------------------------------------------

async def _fetch_office_location(
    *,
    suite_id: str,
    office_id: str,
) -> tuple[float | None, float | None, str | None]:
    """Return (lat, lng, zip_code) for the office. Returns (None, None, None) on failure.

    Queries office_profiles scoped by suite_id + office_id (Law #6).
    Law #9: Only structured fields (lat, lng, zip) are used — no address string logging.
    """
    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_select,
    )
    try:
        rows = await supabase_select(
            "office_profiles",
            filters=f"suite_id=eq.{suite_id}&id=eq.{office_id}",
            limit=1,
        )
        if not rows:
            logger.warning(
                "supplier_matcher: no office_profile found suite=%s office=%s",
                suite_id[:8],
                office_id[:8],
            )
            return None, None, None

        row = rows[0]
        lat = row.get("latitude") or row.get("lat")
        lng = row.get("longitude") or row.get("lng") or row.get("lon")
        zip_code = str(row.get("zip_code") or row.get("postal_code") or row.get("zip") or "")
        if lat is None or lng is None:
            logger.warning(
                "supplier_matcher: office_profile missing lat/lng suite=%s office=%s",
                suite_id[:8],
                office_id[:8],
            )
        return (float(lat) if lat is not None else None,
                float(lng) if lng is not None else None,
                zip_code or None)
    except SupabaseClientError as exc:
        logger.warning(
            "supplier_matcher: office_profiles fetch failed suite=%s error=%s",
            suite_id[:8],
            type(exc).__name__,
        )
        return None, None, None


# ---------------------------------------------------------------------------
# Home Depot search
# ---------------------------------------------------------------------------

async def _search_home_depot(
    *,
    line_item: str,
    delivery_zip: str | None,
    correlation_id: str,
    suite_id: str,
    office_id: str,
) -> list[dict[str, Any]]:
    """Search SerpApi Home Depot for line_item products. Returns list of raw result dicts.

    Returns empty list on error (fail-soft; Google Places is the backup for supplier count).
    Law #3: Missing SERPAPI_KEY → fail-closed → returns [].
    """
    from aspire_orchestrator.providers.serpapi_homedepot_client import (
        execute_serpapi_homedepot_search,
    )
    from aspire_orchestrator.models import Outcome

    payload: dict[str, Any] = {
        "query": line_item[:200],
        "hd_sort": "top_sellers",
    }
    if delivery_zip:
        payload["delivery_zip"] = delivery_zip

    try:
        result = await execute_serpapi_homedepot_search(
            payload=payload,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            risk_tier="green",
        )
        if result.outcome == Outcome.SUCCESS:
            products = result.data.get("products") or result.data.get("shopping_results") or []
            return list(products) if isinstance(products, list) else []
        return []
    except Exception as exc:
        logger.warning(
            "supplier_matcher: home_depot search failed line_item=%s error=%s",
            line_item[:40],
            type(exc).__name__,
        )
        return []


# ---------------------------------------------------------------------------
# Google Places search
# ---------------------------------------------------------------------------

async def _search_google_places(
    *,
    query: str,
    office_lat: float,
    office_lng: float,
    radius_miles: float,
    correlation_id: str,
    suite_id: str,
    office_id: str,
) -> list[dict[str, Any]]:
    """Search Google Places for building suppliers near the office.

    Returns list of enriched place dicts. Empty on failure.
    Law #3: Missing GOOGLE_MAPS_API_KEY → fail-closed → returns [].
    """
    from aspire_orchestrator.providers.google_places_client import (
        execute_google_places_search,
    )
    from aspire_orchestrator.models import Outcome

    payload: dict[str, Any] = {
        "query": query,
        "location": f"{office_lat},{office_lng}",
        "radius": _miles_to_meters(radius_miles),
        "type": "hardware_store",
    }
    try:
        result = await execute_google_places_search(
            payload=payload,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            risk_tier="green",
        )
        if result.outcome == Outcome.SUCCESS:
            return list(result.data.get("results", []))
        return []
    except Exception as exc:
        logger.warning(
            "supplier_matcher: google_places search failed error=%s",
            type(exc).__name__,
        )
        return []


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def _normalize_hd_result(
    raw: dict[str, Any],
    office_lat: float | None,
    office_lng: float | None,
) -> SupplierMatch | None:
    """Convert a SerpApi Home Depot product result to SupplierMatch.

    Home Depot products don't have an address per se — we use "The Home Depot"
    as the name and indicate distance via nearest store lookup when lat/lng available.
    """
    title = str(raw.get("title") or raw.get("name") or "")
    if not title:
        return None

    price = raw.get("price") or raw.get("extracted_price")
    link = raw.get("link") or raw.get("product_url") or ""
    has_stock = bool(raw.get("in_stock") or raw.get("availability") == "In Stock")

    return SupplierMatch(
        name="The Home Depot",
        address="",   # HD product results don't embed store addresses
        distance_miles=0.0,
        has_in_stock=has_stock,
        provider="home_depot",
        contact_phone="1-800-466-3337",
        product_url=link if link else None,
    )


def _normalize_places_result(
    raw: dict[str, Any],
    office_lat: float | None,
    office_lng: float | None,
    geofence_miles: float,
) -> SupplierMatch | None:
    """Convert a Google Places result to SupplierMatch.

    Filters out results outside the geofence when lat/lng available.
    """
    name = str(raw.get("name") or "")
    address = str(raw.get("formatted_address") or raw.get("address") or "")
    if not name:
        return None

    place_lat = (raw.get("location") or {}).get("lat")
    place_lng = (raw.get("location") or {}).get("lng")

    distance = 0.0
    if office_lat is not None and office_lng is not None and place_lat and place_lng:
        distance = _haversine_miles(office_lat, office_lng, float(place_lat), float(place_lng))
        if distance > geofence_miles:
            return None   # Outside geofence — drop

    phone = str(raw.get("phone") or raw.get("formatted_phone_number") or "")
    website = str(raw.get("website") or raw.get("url") or "")
    opening_hours = raw.get("opening_hours") or {}
    has_stock = bool(opening_hours.get("open_now"))  # Best proxy for "available now"

    return SupplierMatch(
        name=name,
        address=address,
        distance_miles=round(distance, 2),
        has_in_stock=has_stock,
        provider="google_places",
        contact_phone=phone,
        product_url=website if website else None,
    )


# ---------------------------------------------------------------------------
# Missing inputs
# ---------------------------------------------------------------------------

async def _insert_missing_input(
    *,
    suite_id: str,
    project_id: str,
    line_item: str,
    match_count: int,
    geofence_miles: float,
    correlation_id: str,
) -> None:
    """Insert a blueprint_missing_inputs row requesting contractor add a preferred supplier.

    Law #9: Only the truncated line_item (100 chars) is stored — no PII or full address blocks.
    """
    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_insert,
    )
    short_item = line_item[:100]
    try:
        await supabase_insert(
            "blueprint_missing_inputs",
            {
                "id": str(uuid.uuid4()),
                "suite_id": suite_id,
                "project_id": project_id,
                "description": (
                    f"Only {match_count} supplier(s) found within {geofence_miles:.0f} miles "
                    f"for material: {short_item}. "
                    f"Need ≥3 suppliers for reliable procurement comparison."
                ),
                "suggested_resolution": (
                    "Add a preferred local supplier (name, address, phone) for this material "
                    "in the Materials tab, or expand the search radius."
                ),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except SupabaseClientError as exc:
        logger.warning(
            "supplier_matcher: failed to insert missing_input for material=%s error=%s",
            short_item[:40],
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Receipt emission
# ---------------------------------------------------------------------------

def _emit_search_receipt(
    *,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    line_item: str,
    match_count: int,
    provider_mix: dict[str, int],
) -> None:
    """Emit blueprint.procure.supplier_search receipt (Law #2).

    Law #9: Only sanitized counts and short line_item (100 chars) in receipt.
    """
    from aspire_orchestrator.services.receipt_store import store_receipts
    import hashlib, json
    inputs = {"line_item": line_item[:100], "suite_id": suite_id, "office_id": office_id}
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    inputs_hash = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    receipt: dict[str, Any] = {
        "receipt_version": "1.0",
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": "blueprint.procure.supplier_search",
        "actor": "skillpack:drew-blueprint",
        "correlation_id": correlation_id,
        "status": "ok",
        "inputs_hash": inputs_hash,
        "policy": {
            "decision": "allow",
            "policy_id": "drew-blueprint-v1",
            "reasons": [],
        },
        "redactions": ["line_item_truncated_100", "addresses_omitted"],
        "metadata": {
            "suite_id": suite_id,
            "office_id": office_id,
            "match_count": match_count,
            "provider_mix": provider_mix,
        },
    }
    try:
        store_receipts([receipt])
    except Exception:
        pass  # Receipt store errors are never fatal


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

async def match_suppliers(
    line_item: str,
    *,
    suite_id: str,
    office_id: str,
    project_id: str,
    geofence_miles: float = 25.0,
    correlation_id: str,
) -> SupplierSearchResult:
    """Return up to 5 suppliers within geofence for a material line item.

    Hybrid strategy:
      1. Google Places text search for "building supply near <office_location>"
         filtered to suppliers within geofence_miles.
      2. SerpApi Home Depot search for the line_item keywords.

    Ranking: distance ascending, then has_in_stock descending (available first).
    Deduplication: by (name.lower(), first 50 chars of address).

    If <3 suppliers are found after merging, a blueprint_missing_inputs row is
    inserted asking the contractor to add a preferred local supplier.

    Receipt: emits blueprint.procure.supplier_search regardless of outcome.

    Law #2: Receipt emitted on every path.
    Law #3: Missing provider keys → fail-closed (empty lists from each provider,
            missing_input inserted, receipt emitted with match_count=0).
    Law #6: office location fetched with suite_id + office_id filters.
    Law #9: Only line_item[:100] + counts in logs and receipts. No full addresses.

    Args:
        line_item: The material description (from blueprint_materials.line_item).
        suite_id: Tenant UUID — used for DB scoping and receipt.
        office_id: Office UUID — used to look up office lat/lng.
        project_id: Blueprint project UUID — used when inserting missing_inputs.
        geofence_miles: Max distance radius in miles (default 25.0).
        correlation_id: Trace correlation UUID.

    Returns:
        SupplierSearchResult with matches list, below_minimum flag, provider_mix.
    """
    result = SupplierSearchResult()

    # Step 1: Get office lat/lng for geofence center
    office_lat, office_lng, office_zip = await _fetch_office_location(
        suite_id=suite_id,
        office_id=office_id,
    )

    # Step 2: Parallel provider searches
    import asyncio

    hd_task = asyncio.create_task(
        _search_home_depot(
            line_item=line_item,
            delivery_zip=office_zip,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
    )
    places_query = f"building supply {line_item[:60]}"
    if office_lat is not None and office_lng is not None:
        places_task = asyncio.create_task(
            _search_google_places(
                query=places_query,
                office_lat=office_lat,
                office_lng=office_lng,
                radius_miles=geofence_miles,
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )
        )
    else:
        places_task = asyncio.create_task(asyncio.coroutine(lambda: [])())

    hd_raw, places_raw = await asyncio.gather(hd_task, places_task, return_exceptions=True)

    # Handle exceptions from gather (Law #3 fail-closed: treat as empty lists)
    if isinstance(hd_raw, Exception):
        logger.warning("supplier_matcher: hd_task exception: %s", type(hd_raw).__name__)
        hd_raw = []
    if isinstance(places_raw, Exception):
        logger.warning("supplier_matcher: places_task exception: %s", type(places_raw).__name__)
        places_raw = []

    # Step 3: Normalize, geofence-filter, dedupe
    seen: set[tuple[str, str]] = set()
    candidates: list[SupplierMatch] = []

    # Google Places first (richer location data for dedup key)
    for raw in (places_raw or []):
        match = _normalize_places_result(
            raw,
            office_lat=office_lat,
            office_lng=office_lng,
            geofence_miles=geofence_miles,
        )
        if match is None:
            continue
        dedup_key = (match.name.lower().strip(), match.address[:50].lower())
        if dedup_key not in seen:
            seen.add(dedup_key)
            candidates.append(match)

    # Home Depot (add if not already present)
    hd_added = 0
    for raw in (hd_raw or []):
        match = _normalize_hd_result(raw, office_lat=office_lat, office_lng=office_lng)
        if match is None:
            continue
        dedup_key = ("the home depot", "")
        if dedup_key not in seen:
            seen.add(dedup_key)
            candidates.append(match)
            hd_added = 1
            break   # One HD result is sufficient (it's a national chain)

    # Step 4: Rank — distance ascending, has_in_stock descending (True sorts before False)
    candidates.sort(key=lambda m: (m.distance_miles, not m.has_in_stock))

    # Step 5: Cap at 5
    top5 = candidates[:5]

    # Step 6: Provider mix tally
    mix: dict[str, int] = {}
    for m in top5:
        mix[m.provider] = mix.get(m.provider, 0) + 1
    result.provider_mix = mix

    # Step 7: Below-minimum check → insert missing_inputs
    if len(top5) < 3:
        result.below_minimum = True
        await _insert_missing_input(
            suite_id=suite_id,
            project_id=project_id,
            line_item=line_item,
            match_count=len(top5),
            geofence_miles=geofence_miles,
            correlation_id=correlation_id,
        )
        result.missing_input_inserted = True

    result.matches = top5

    # Step 8: Emit receipt (Law #2 — always, even on empty result)
    _emit_search_receipt(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        line_item=line_item,
        match_count=len(top5),
        provider_mix=mix,
    )

    logger.info(
        "supplier_matcher: line_item=%s match_count=%d providers=%s suite=%s",
        line_item[:40],
        len(top5),
        mix,
        suite_id[:8],
    )

    return result
