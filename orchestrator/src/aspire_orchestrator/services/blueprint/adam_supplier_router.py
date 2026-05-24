"""Adam supplier router -- MATERIAL_SUPPLIER_SEARCH category-based provider dispatch.

Routes material line_item to the correct provider chain based on supply category:
    commodity            -> SerpAPI Shopping (parallel) + SerpAPI HD -> Unwrangle HD fallback
    commercial_plumbing  -> Unwrangle Ferguson + SerpAPI Shopping (parallel)
    appliance_finish     -> Unwrangle Lowes + SerpAPI Shopping (parallel)
    local_trade          -> Google Places only (call-for-quote)
    specialty_hardware   -> Defer to manual

Wave 5.4a: serpapi_shopping added as parallel call for every non-local category.
  Union of candidates is deduped and re-ranked by price+rating score.

Law compliance:
  Law #1: Returns ranked list only.
  Law #2: Receipts per provider sub-call + parent for full search.
  Law #3: Missing provider keys -> fail-closed.
  Law #6: suite_id/office_id scoped throughout.
  Law #9: Only line_item[:100] + counts in receipts/logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from aspire_orchestrator.services.blueprint.candidate_ranker import rank_candidates
from aspire_orchestrator.services.blueprint.lowes_category_map import resolve_lowes_url
import aspire_orchestrator.services.receipt_store as _receipt_store_module

logger = logging.getLogger(__name__)

SupplyCategory = Literal["commodity", "commercial_plumbing", "appliance_finish", "local_trade", "specialty_hardware"]
CandidateList = dict[str, Any]

# Stub detection for Unwrangle clients (Wave 5.1a-2 wires these)
try:
    from aspire_orchestrator.providers.unwrangle_homedepot_client import UnwrangleHomeDepotClient  # type: ignore[import]
    _UNWRANGLE_HD_AVAILABLE = True
except ImportError:
    _UNWRANGLE_HD_AVAILABLE = False

try:
    from aspire_orchestrator.providers.unwrangle_ferguson_client import UnwrangleFergusonClient  # type: ignore[import]
    _UNWRANGLE_FERGUSON_AVAILABLE = True
except ImportError:
    _UNWRANGLE_FERGUSON_AVAILABLE = False

try:
    from aspire_orchestrator.providers.unwrangle_lowes_client import UnwrangleLowesClient  # type: ignore[import]
    _UNWRANGLE_LOWES_AVAILABLE = True
except ImportError:
    _UNWRANGLE_LOWES_AVAILABLE = False


def _make_receipt(*, event_type: str, suite_id: str, office_id: str, correlation_id: str, status: str, inputs: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    receipt: dict[str, Any] = {
        "receipt_version": "1.0",
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "actor": "skillpack:adam-research",
        "suite_id": suite_id,
        "office_id": office_id,
        "correlation_id": correlation_id,
        "status": status,
        "inputs_hash": f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
        "policy": {"decision": "allow" if status in ("ok", "degraded", "defer_to_manual") else "deny", "policy_id": "adam-supplier-search-v1", "reasons": []},
        "redactions": ["line_item_truncated_100", "raw_api_response_omitted"],
    }
    if metadata:
        receipt["metadata"] = metadata
    return receipt


def _store_receipt(receipt: dict[str, Any]) -> None:
    try:
        _receipt_store_module.store_receipts([receipt])
    except Exception:
        pass


def _freshness_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _call_serpapi_homedepot(*, line_item: str, office_zip: str | None, suite_id: str, office_id: str, correlation_id: str) -> tuple[list[dict[str, Any]], int]:
    from aspire_orchestrator.providers.serpapi_homedepot_client import execute_serpapi_homedepot_search
    from aspire_orchestrator.models import Outcome
    payload: dict[str, Any] = {"query": line_item[:200], "hd_sort": "top_sellers"}
    if office_zip:
        payload["delivery_zip"] = office_zip
    try:
        result = await execute_serpapi_homedepot_search(payload=payload, correlation_id=correlation_id, suite_id=suite_id, office_id=office_id, risk_tier="green")
        if result.outcome == Outcome.SUCCESS:
            raw: list[Any] = result.data.get("products") or result.data.get("shopping_results") or []
            return (list(raw) if isinstance(raw, list) else []), 1
        return [], 0
    except Exception as exc:
        logger.warning("adam_supplier_router: serpapi_hd failed item=%s err=%s", line_item[:40], type(exc).__name__)
        return [], 0


async def _call_serpapi_shopping(*, line_item: str, office_zip: str | None, suite_id: str, office_id: str, correlation_id: str) -> tuple[list[dict[str, Any]], int]:
    """Parallel Google Shopping call for all non-local categories (Wave 5.4a)."""
    from aspire_orchestrator.providers.serpapi_shopping_client import execute_serpapi_shopping_search
    from aspire_orchestrator.models import Outcome
    payload: dict[str, Any] = {"query": line_item[:200], "gl": "us", "hl": "en"}
    try:
        result = await execute_serpapi_shopping_search(payload=payload, correlation_id=correlation_id, suite_id=suite_id, office_id=office_id, risk_tier="green")
        if result.outcome == Outcome.SUCCESS:
            raw: list[Any] = result.data.get("results") or []
            return (list(raw) if isinstance(raw, list) else []), 1
        return [], 0
    except Exception as exc:
        logger.warning("adam_supplier_router: serpapi_shopping failed item=%s err=%s", line_item[:40], type(exc).__name__)
        return [], 0


async def _call_unwrangle_homedepot_stub(*, line_item: str, office_zip: str | None) -> tuple[list[dict[str, Any]], int]:
    """Stub. TODO: Wave 5.1a-2 wires UnwrangleHomeDepotClient.search_products(line_item, office_zip)."""
    if not _UNWRANGLE_HD_AVAILABLE:
        return [], 0
    try:
        client = UnwrangleHomeDepotClient()  # type: ignore[name-defined]
        results = await client.search_products(line_item, office_zip)
        return results if isinstance(results, list) else [], 1
    except Exception as exc:
        logger.warning("adam_supplier_router: unwrangle_hd failed item=%s err=%s", line_item[:40], type(exc).__name__)
        return [], 0


async def _call_unwrangle_ferguson_stub(*, line_item: str) -> tuple[list[dict[str, Any]], int]:
    """Stub. TODO: Wave 5.1a-2 wires UnwrangleFergusonClient.search_products(line_item)."""
    if not _UNWRANGLE_FERGUSON_AVAILABLE:
        return [], 0
    try:
        client = UnwrangleFergusonClient()  # type: ignore[name-defined]
        results = await client.search_products(line_item)
        return results if isinstance(results, list) else [], 1
    except Exception as exc:
        logger.warning("adam_supplier_router: unwrangle_ferg failed item=%s err=%s", line_item[:40], type(exc).__name__)
        return [], 0


async def _call_unwrangle_lowes_stub(*, line_item: str, office_zip: str | None) -> tuple[list[dict[str, Any]], int]:
    """Stub. TODO: Wave 5.1a-2 wires UnwrangleLowesClient.find_nearest_store + browse_category."""
    if not _UNWRANGLE_LOWES_AVAILABLE:
        return [], 0
    category_url = resolve_lowes_url(line_item)
    if not category_url:
        return [], 0
    try:
        client = UnwrangleLowesClient()  # type: ignore[name-defined]
        store_no: str | None = None
        credits = 0
        if office_zip:
            store_no = await client.find_nearest_store(office_zip)
            credits += 1
        results = await client.browse_category(category_url, store_no)
        credits += 1
        return results if isinstance(results, list) else [], credits
    except Exception as exc:
        logger.warning("adam_supplier_router: unwrangle_lowes failed item=%s err=%s", line_item[:40], type(exc).__name__)
        return [], 0


async def _call_unwrangle_lowes_search(
    *, line_item: str, suite_id: str, office_id: str, correlation_id: str,
    store_no: str | None = None, office_zip: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Parallel keyword search via Unwrangle lowes_search platform.

    NOTE: lowes_search currently returns 504 from Unwrangle upstream (ticket confirmed).
    Wired here for parallel dispatch alongside the category-browse path. On 504 it
    fast-fails with empty results and zero credits. No retry. Wave 5.2 re-enable target.
    """
    if not _UNWRANGLE_LOWES_AVAILABLE:
        return [], 0
    try:
        from aspire_orchestrator.providers.unwrangle_lowes_client import search_products
        from aspire_orchestrator.models import Outcome
        result = await search_products(
            line_item,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            store_no=store_no,
            zipcode=office_zip,
            risk_tier="green",
        )
        if result.outcome == Outcome.SUCCESS:
            raw: list[Any] = (result.data or {}).get("results", [])
            return list(raw) if isinstance(raw, list) else [], 1
        # 504 upstream skip or other failure: zero credits, empty results, no retry
        return [], 0
    except Exception as exc:
        logger.warning(
            "adam_supplier_router: unwrangle_lowes_search failed item=%s err=%s",
            line_item[:40], type(exc).__name__,
        )
        return [], 0


async def _call_google_places(*, line_item: str, office_lat: float | None, office_lng: float | None, geofence_miles: float, suite_id: str, office_id: str, correlation_id: str) -> tuple[list[dict[str, Any]], int]:
    from aspire_orchestrator.providers.google_places_client import execute_google_places_search
    from aspire_orchestrator.models import Outcome
    if office_lat is None or office_lng is None:
        return [], 0
    payload: dict[str, Any] = {"query": f"building supply trade contractor {line_item[:60]}", "location": f"{office_lat},{office_lng}", "radius": min(50_000, int(geofence_miles * 1609.34))}
    try:
        result = await execute_google_places_search(payload=payload, correlation_id=correlation_id, suite_id=suite_id, office_id=office_id, risk_tier="green")
        if result.outcome == Outcome.SUCCESS:
            return list(result.data.get("results", [])), 1
        return [], 0
    except Exception as exc:
        logger.warning("adam_supplier_router: google_places failed item=%s err=%s", line_item[:40], type(exc).__name__)
        return [], 0


def _normalize_serpapi_hd(raw: dict[str, Any]) -> dict[str, Any] | None:
    title = str(raw.get("title") or raw.get("name") or "")
    if not title:
        return None
    price_val: float | None = None
    price_str = raw.get("price") or raw.get("extracted_price")
    if price_str:
        try:
            price_val = float(str(price_str).replace("$", "").replace(",", "").strip())
        except ValueError:
            pass
    return {
        "supplier": {"name": "The Home Depot", "id": "homedepot-national", "distance_mi": None, "phone": "1-800-466-3337"},
        "product": {"name": title, "brand": raw.get("brand"), "model_no": raw.get("model_number") or raw.get("mpn"), "upc": raw.get("upc"), "in_stock": bool(raw.get("in_stock") or raw.get("availability") == "In Stock"), "qty_available": None},
        "price": {"value": price_val, "currency": "USD", "source": "retail"},
        "tariff_flag_detected": raw.get("tariff_flag"),
        "freshness_as_of": _freshness_now(),
        "_source_api": "serpapi_homedepot",
    }


def _normalize_serpapi_shopping(raw: dict[str, Any]) -> dict[str, Any] | None:
    title = str(raw.get("title") or "")
    if not title:
        return None
    price_val: float | None = None
    price_raw = raw.get("extracted_price") or raw.get("price")
    if price_raw is not None:
        try:
            price_val = float(str(price_raw).replace("$", "").replace(",", "").strip())
        except ValueError:
            pass
    source_name = str(raw.get("source") or "Google Shopping")
    rating_raw = raw.get("rating")
    rating: float | None = None
    try:
        rating = float(rating_raw) if rating_raw is not None else None
    except (TypeError, ValueError):
        pass
    return {
        "supplier": {"name": source_name, "id": f"gshop-{source_name.lower().replace(' ', '-')}", "distance_mi": None, "phone": None},
        "product": {"name": title, "brand": None, "model_no": raw.get("product_id"), "upc": None, "in_stock": True, "qty_available": None, "rating": rating, "reviews": raw.get("reviews")},
        "price": {"value": price_val, "currency": "USD", "source": "google_shopping"},
        "tariff_flag_detected": None,
        "freshness_as_of": _freshness_now(),
        "_source_api": "serpapi_shopping",
        "_dedup_key": f"gshop:{title[:60].lower()}:{price_val}",
    }


def _dedup_union(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union two candidate lists, dedup by (name_prefix, price) to avoid near-duplicates."""
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for c in primary + secondary:
        key = c.get("_dedup_key")
        if key is None:
            prod = c.get("product") or {}
            price = (c.get("price") or {}).get("value")
            key = f"{(prod.get('name') or '')[:60].lower()}:{price}"
        if key not in seen:
            seen.add(key)
            merged.append(c)
    return merged


def _normalize_places_to_cfq(raw: dict[str, Any]) -> dict[str, Any] | None:
    name = str(raw.get("name") or "")
    if not name:
        return None
    phone = str(raw.get("phone") or raw.get("formatted_phone_number") or "")
    return {
        "supplier": {"name": name, "id": raw.get("place_id") or str(uuid.uuid4()), "distance_mi": None, "phone": phone or None},
        "product": {"name": "Trade supply (call for quote)", "brand": None, "model_no": None, "upc": None, "in_stock": bool((raw.get("opening_hours") or {}).get("open_now")), "qty_available": None},
        "price": {"value": None, "currency": "USD", "source": "call_for_quote"},
        "tariff_flag_detected": None,
        "freshness_as_of": _freshness_now(),
        "_source_api": "google_places",
    }


def _pass_through(raw: dict[str, Any], source_api: str) -> dict[str, Any]:
    r = dict(raw)
    r.setdefault("freshness_as_of", _freshness_now())
    r["_source_api"] = source_api
    return r


async def _route_commodity(*, line_item: str, office_zip: str | None, brand_familiarity_map: dict[str, float], suite_id: str, office_id: str, correlation_id: str) -> CandidateList:
    source_apis: list[str] = []
    credits = 0
    # Wave 5.4a: run serpapi_shopping in parallel with serpapi_homedepot
    (raw_hd, c1), (raw_shopping, c_shop) = await asyncio.gather(
        _call_serpapi_homedepot(line_item=line_item, office_zip=office_zip, suite_id=suite_id, office_id=office_id, correlation_id=correlation_id),
        _call_serpapi_shopping(line_item=line_item, office_zip=office_zip, suite_id=suite_id, office_id=office_id, correlation_id=correlation_id),
    )
    credits += c1 + c_shop
    if c1 > 0:
        source_apis.append("serpapi_homedepot")
    if c_shop > 0:
        source_apis.append("serpapi_shopping")
    _store_receipt(_make_receipt(event_type="provider.serpapi_homedepot", suite_id=suite_id, office_id=office_id, correlation_id=correlation_id, status="ok" if raw_hd else "degraded", inputs={"line_item": line_item[:100], "category": "commodity"}, metadata={"result_count": len(raw_hd), "credits_used": c1}))
    _store_receipt(_make_receipt(event_type="provider.serpapi_shopping.blueprint", suite_id=suite_id, office_id=office_id, correlation_id=correlation_id, status="ok" if raw_shopping else "degraded", inputs={"line_item": line_item[:100], "category": "commodity"}, metadata={"result_count": len(raw_shopping), "credits_used": c_shop}))
    if not raw_hd:
        raw_uw, c2 = await _call_unwrangle_homedepot_stub(line_item=line_item, office_zip=office_zip)
        credits += c2
        if c2 > 0:
            source_apis.append("unwrangle_homedepot")
        _store_receipt(_make_receipt(event_type="provider.unwrangle_homedepot", suite_id=suite_id, office_id=office_id, correlation_id=correlation_id, status="ok" if raw_uw else "degraded", inputs={"line_item": line_item[:100], "category": "commodity"}, metadata={"result_count": len(raw_uw), "credits_used": c2}))
        primary_normalized = [_pass_through(r, "unwrangle_homedepot") for r in raw_uw]
    else:
        primary_normalized = [n for r in raw_hd if (n := _normalize_serpapi_hd(r)) is not None]
    shopping_normalized = [n for r in raw_shopping if (n := _normalize_serpapi_shopping(r)) is not None]
    merged = _dedup_union(primary_normalized, shopping_normalized)
    candidates = rank_candidates(merged, brand_familiarity_map=brand_familiarity_map)
    return {"status": "ok" if candidates else "degraded", "candidates": candidates, "source_apis_called": source_apis, "credits_used": credits, "degradation_reason": "No results from Home Depot or Google Shopping" if not candidates else None}


async def _route_commercial_plumbing(*, line_item: str, office_zip: str | None, brand_familiarity_map: dict[str, float], suite_id: str, office_id: str, correlation_id: str) -> CandidateList:
    source_apis: list[str] = []
    credits = 0
    # Wave 5.4a: serpapi_shopping runs in parallel with Ferguson
    (raw_ferg, c1), (raw_shopping, c_shop) = await asyncio.gather(
        _call_unwrangle_ferguson_stub(line_item=line_item),
        _call_serpapi_shopping(line_item=line_item, office_zip=office_zip, suite_id=suite_id, office_id=office_id, correlation_id=correlation_id),
    )
    credits += c1 + c_shop
    if c1 > 0:
        source_apis.append("unwrangle_ferguson")
    if c_shop > 0:
        source_apis.append("serpapi_shopping")
    _store_receipt(_make_receipt(event_type="provider.unwrangle_ferguson", suite_id=suite_id, office_id=office_id, correlation_id=correlation_id, status="ok" if raw_ferg else "degraded", inputs={"line_item": line_item[:100], "category": "commercial_plumbing"}, metadata={"result_count": len(raw_ferg), "credits_used": c1}))
    _store_receipt(_make_receipt(event_type="provider.serpapi_shopping.blueprint", suite_id=suite_id, office_id=office_id, correlation_id=correlation_id, status="ok" if raw_shopping else "degraded", inputs={"line_item": line_item[:100], "category": "commercial_plumbing"}, metadata={"result_count": len(raw_shopping), "credits_used": c_shop}))
    ferg_normalized = [_pass_through(r, "unwrangle_ferguson") for r in raw_ferg]
    shopping_normalized = [n for r in raw_shopping if (n := _normalize_serpapi_shopping(r)) is not None]
    merged = _dedup_union(ferg_normalized, shopping_normalized)
    candidates = rank_candidates(merged, brand_familiarity_map=brand_familiarity_map)
    return {"status": "ok" if candidates else "degraded", "candidates": candidates, "source_apis_called": source_apis, "credits_used": credits, "degradation_reason": "Ferguson and Google Shopping returned no results" if not candidates else None}


async def _route_appliance_finish(*, line_item: str, office_zip: str | None, brand_familiarity_map: dict[str, float], suite_id: str, office_id: str, correlation_id: str) -> CandidateList:
    source_apis: list[str] = []
    credits = 0
    # Wave 5.4a+c: category-browse, keyword search (known-504 but wired), and shopping run in parallel.
    # lowes_search will return empty results until Unwrangle fixes upstream (Wave 5.2 target).
    (raw_lowes, c1), (raw_lowes_kw, c_kw), (raw_shopping, c_shop) = await asyncio.gather(
        _call_unwrangle_lowes_stub(line_item=line_item, office_zip=office_zip),
        _call_unwrangle_lowes_search(line_item=line_item, suite_id=suite_id, office_id=office_id, correlation_id=correlation_id, office_zip=office_zip),
        _call_serpapi_shopping(line_item=line_item, office_zip=office_zip, suite_id=suite_id, office_id=office_id, correlation_id=correlation_id),
    )
    credits += c1 + c_kw + c_shop
    if c1 > 0:
        source_apis.append("unwrangle_lowes")
    if c_kw > 0:
        source_apis.append("unwrangle_lowes_search")
    if c_shop > 0:
        source_apis.append("serpapi_shopping")
    _store_receipt(_make_receipt(event_type="provider.unwrangle_lowes", suite_id=suite_id, office_id=office_id, correlation_id=correlation_id, status="ok" if raw_lowes else "degraded", inputs={"line_item": line_item[:100], "category": "appliance_finish"}, metadata={"result_count": len(raw_lowes), "credits_used": c1}))
    _store_receipt(_make_receipt(event_type="provider.unwrangle_lowes_search", suite_id=suite_id, office_id=office_id, correlation_id=correlation_id, status="ok" if raw_lowes_kw else "504_skip", inputs={"line_item": line_item[:100], "category": "appliance_finish"}, metadata={"result_count": len(raw_lowes_kw), "credits_used": c_kw}))
    _store_receipt(_make_receipt(event_type="provider.serpapi_shopping.blueprint", suite_id=suite_id, office_id=office_id, correlation_id=correlation_id, status="ok" if raw_shopping else "degraded", inputs={"line_item": line_item[:100], "category": "appliance_finish"}, metadata={"result_count": len(raw_shopping), "credits_used": c_shop}))
    lowes_normalized = [_pass_through(r, "unwrangle_lowes") for r in raw_lowes]
    lowes_kw_normalized = [_pass_through(r, "unwrangle_lowes_search") for r in raw_lowes_kw]
    shopping_normalized = [n for r in raw_shopping if (n := _normalize_serpapi_shopping(r)) is not None]
    merged = _dedup_union(_dedup_union(lowes_normalized, lowes_kw_normalized), shopping_normalized)
    candidates = rank_candidates(merged, brand_familiarity_map=brand_familiarity_map)
    return {"status": "ok" if candidates else "degraded", "candidates": candidates, "source_apis_called": source_apis, "credits_used": credits, "degradation_reason": "Lowes and Google Shopping returned no results" if not candidates else None}


async def _route_local_trade(*, line_item: str, office_lat: float | None, office_lng: float | None, geofence_miles: float, brand_familiarity_map: dict[str, float], suite_id: str, office_id: str, correlation_id: str) -> CandidateList:
    source_apis: list[str] = []
    credits = 0
    raw_places, c1 = await _call_google_places(line_item=line_item, office_lat=office_lat, office_lng=office_lng, geofence_miles=geofence_miles, suite_id=suite_id, office_id=office_id, correlation_id=correlation_id)
    credits += c1
    if c1 > 0:
        source_apis.append("google_places")
    _store_receipt(_make_receipt(event_type="provider.google_places", suite_id=suite_id, office_id=office_id, correlation_id=correlation_id, status="ok" if raw_places else "degraded", inputs={"line_item": line_item[:100], "category": "local_trade"}, metadata={"result_count": len(raw_places), "credits_used": c1}))
    normalized = [n for r in raw_places if (n := _normalize_places_to_cfq(r)) is not None]
    candidates = rank_candidates(normalized, brand_familiarity_map=brand_familiarity_map)
    return {"status": "ok" if candidates else "degraded", "candidates": candidates, "source_apis_called": source_apis, "credits_used": credits, "degradation_reason": "No local trade suppliers found within geofence" if not candidates else None}


def _route_specialty_hardware() -> CandidateList:
    return {"status": "defer_to_manual", "candidates": [], "source_apis_called": [], "credits_used": 0, "degradation_reason": "specialty_hardware requires manual supplier selection in Materials tab"}


async def route_supplier_search(
    *,
    line_item: str,
    category: SupplyCategory,
    brand_familiarity_map: dict[str, float],
    geofence_miles: float = 25.0,
    office_zip: str | None,
    office_lat: float | None,
    office_lng: float | None,
    suite_id: str,
    office_id: str,
    correlation_id: str,
) -> CandidateList:
    """Dispatch material line_item to correct provider(s) and return ranked candidates.

    Law #2: Per-provider receipts inside _route_* functions. Parent receipt by caller.
    Law #3: Missing line_item -> error result with receipt.
    Law #6: suite_id/office_id scoped throughout.
    Law #9: line_item[:100] + counts only in receipts/logs.
    """
    if not line_item or not line_item.strip():
        _store_receipt(_make_receipt(event_type="adam.material_supplier_search", suite_id=suite_id, office_id=office_id, correlation_id=correlation_id, status="error", inputs={"line_item": "", "category": category}, metadata={"error": "MISSING_LINE_ITEM"}))
        return {"status": "error", "candidates": [], "source_apis_called": [], "credits_used": 0, "degradation_reason": "line_item is required"}

    try:
        if category == "commodity":
            result = await _route_commodity(line_item=line_item, office_zip=office_zip, brand_familiarity_map=brand_familiarity_map, suite_id=suite_id, office_id=office_id, correlation_id=correlation_id)
        elif category == "commercial_plumbing":
            result = await _route_commercial_plumbing(line_item=line_item, office_zip=office_zip, brand_familiarity_map=brand_familiarity_map, suite_id=suite_id, office_id=office_id, correlation_id=correlation_id)
        elif category == "appliance_finish":
            result = await _route_appliance_finish(line_item=line_item, office_zip=office_zip, brand_familiarity_map=brand_familiarity_map, suite_id=suite_id, office_id=office_id, correlation_id=correlation_id)
        elif category == "local_trade":
            result = await _route_local_trade(line_item=line_item, office_lat=office_lat, office_lng=office_lng, geofence_miles=geofence_miles, brand_familiarity_map=brand_familiarity_map, suite_id=suite_id, office_id=office_id, correlation_id=correlation_id)
        elif category == "specialty_hardware":
            result = _route_specialty_hardware()
            _store_receipt(_make_receipt(event_type="adam.material_supplier_search", suite_id=suite_id, office_id=office_id, correlation_id=correlation_id, status="defer_to_manual", inputs={"line_item": line_item[:100], "category": category}, metadata={"candidates_returned": 0, "credits_used": 0}))
            return result
        else:
            result = {"status": "error", "candidates": [], "source_apis_called": [], "credits_used": 0, "degradation_reason": f"Unknown category: {category!r}"}

        logger.info("adam_supplier_router: category=%s status=%s candidates=%d credits=%d suite=%s", category, result.get("status"), len(result.get("candidates", [])), result.get("credits_used", 0), suite_id[:8])
        return result

    except Exception as exc:
        logger.error("adam_supplier_router: unhandled error category=%s item=%s err=%s", category, line_item[:40], type(exc).__name__, exc_info=True)
        _store_receipt(_make_receipt(event_type="adam.material_supplier_search", suite_id=suite_id, office_id=office_id, correlation_id=correlation_id, status="error", inputs={"line_item": line_item[:100], "category": category}, metadata={"error": type(exc).__name__}))
        return {"status": "error", "candidates": [], "source_apis_called": [], "credits_used": 0, "degradation_reason": f"Internal error: {type(exc).__name__}"}