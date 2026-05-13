"""GET /v1/materials/search — cache-first materials search route (Pass C + Pass E).

Law compliance:
  Law #2 — receipt cut on every outcome (success, cached, denied, error).
  Law #3 — fail closed: missing headers → 401, PII query → 400, no token → 401.
  Law #4 — GREEN tier: read-only search, no state change.
  Law #5 — capability token validated server-side before execution.
  Law #6 — tenant isolation: suite_id/office_id enforced on every cache key.
  Law #9 — PII redaction: query rejected (not stripped) on PII detection.

Search modes (Pass E):
  mode=tool     (default) — Home Depot SerpApi engine (existing Pass C flow)
  mode=supplier           — Yelp SerpApi engine, returns {suppliers: [...]}

Auto-mode-detect (server-side defensive layer):
  If mode=tool but query matches a commercial/specialty keyword set, the response
  includes suggested_mode='supplier'. Client decides whether to flip.

Receipt envelope spec (redacted_outputs):
  engine, account_id, cached, budget_remaining_a, budget_remaining_b,
  query_normalized, store_id / find_loc, product_count / supplier_count, specialty_count.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, status

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.routes._scope import _resolve_scope
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.adam.cache import (
    cache_get,
    cache_set,
)
from aspire_orchestrator.services.adam.cache_normalize import (
    NormalizeRejection,
    QueryRejectionCode,
    normalize_query,
)
from aspire_orchestrator.services.adam.cache_sanitizer import sanitize_product_list
from aspire_orchestrator.services.adam.filter_derivation import derive_filters
from aspire_orchestrator.services.adam.predictive_addons import get_predictive_addons
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.serpapi_budget import (
    BudgetExhaustedError,
    current_counts,
    select_account,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
)
from aspire_orchestrator.services.token_service import validate_token

# Pass E: Yelp adapter — imported at module level so tests can patch via
# `aspire_orchestrator.routes.materials.execute_serpapi_yelp_search`
from aspire_orchestrator.providers.serpapi_yelp_client import (
    execute_serpapi_yelp_search,
)

# Pass C: HD playbook — same rationale
from aspire_orchestrator.services.adam.playbooks.trades import (
    execute_tool_material_price_check,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/materials", tags=["materials"])

# ---------------------------------------------------------------------------
# Environment-driven TTL config
# ---------------------------------------------------------------------------

_HD_TTL_DAYS = int(os.environ.get("MATERIALS_CACHE_TTL_DAYS", "7"))
_HD_TTL_SECONDS = _HD_TTL_DAYS * 86400
_SHOPPING_TTL_SECONDS = 3 * 86400  # hardcoded 3 days
_YELP_TTL_SECONDS = 4 * 3600       # 4 hours — supplier availability changes fast

# Overall route budget (R1): Express proxy timeout is 12s. We budget 11s here
# so FastAPI can return a proper error before the proxy aborts the connection.
_ROUTE_TIMEOUT_SECONDS = 11.0

# ATTOM hardware POI categories (R4: verified against attom_client.py —
# DEFAULT_POI_CATEGORIES there are SHOPPING/EATING/EDUCATION etc.;
# we pass these 4 as explicit categoryName overrides for the specialty lane).
_HARDWARE_POI_CATEGORIES = [
    "HARDWARE AND BUILDING MATERIAL DEALERS",
    "LUMBER, BUILDING MATERIAL",
    "ELECTRICAL SUPPLIES",
    "PLUMBING HEATING AND AIR-CONDITIONING EQUIPMENT SUPPLIES",
]

# ---------------------------------------------------------------------------
# Supplier auto-detect keyword set (Pass E, server-side defensive layer).
# Primary detection is client-side; this provides a safety net.
# If mode=tool but query matches one of these keywords, we add
# suggested_mode='supplier' to the response — client decides whether to flip.
# ---------------------------------------------------------------------------
_SUPPLIER_KEYWORDS: frozenset[str] = frozenset([
    "precast",
    "manhole",
    "concrete by yard",
    "wholesale",
    "mep",
    "prestress",
    "structural steel",
    "dimensional lumber",
    "commercial grade",
    "lumber yard",
    "rebar",
    "lift station",
    "grease trap",
    "transformer",
    "concrete supplier",
    "bulk concrete",
    "bulk lumber",
    "trade supply",
])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit_receipt(
    *,
    receipt_type: str,
    suite_id: str,
    office_id: str,
    tenant_id: str,
    outcome: str,
    reason_code: str,
    correlation_id: str,
    trace_id: str,
    capability_token_id: str | None,
    redacted_outputs: dict[str, Any] | None = None,
    redacted_inputs: dict[str, Any] | None = None,
) -> str:
    """Emit an immutable receipt and return its ID (Law #2)."""
    receipt_id = str(uuid.uuid4())
    receipt: dict[str, Any] = {
        "id": receipt_id,
        "receipt_type": receipt_type,
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "outcome": outcome,
        "action_type": "materials.search",
        "tool_used": "materials_search",
        "risk_tier": "green",
        "reason_code": reason_code,
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "capability_token_id": capability_token_id or "",
        "created_at": _now_iso(),
    }
    if redacted_outputs:
        receipt["redacted_outputs"] = redacted_outputs
    if redacted_inputs:
        receipt["redacted_inputs"] = redacted_inputs
    receipt_store.store_receipts([receipt])
    return receipt_id


def _cap_token_id(cap_token: dict[str, Any] | None) -> str | None:
    if not cap_token:
        return None
    if cap_token.get("id"):
        return str(cap_token["id"])
    sig = cap_token.get("signature") or cap_token.get("token") or ""
    if sig:
        import hashlib
        return hashlib.sha256(str(sig).encode()).hexdigest()[:16]
    return None


def _detect_suggested_mode(query_lower: str) -> str | None:
    """Return 'supplier' if any supplier keyword is present in the query.

    This is the server-side defensive auto-detect layer. The primary detection
    is client-side; this ensures the API can also signal when a tool-mode query
    looks like it should be supplier-mode.

    Returns 'supplier' or None (no suggestion).
    """
    for kw in _SUPPLIER_KEYWORDS:
        if kw in query_lower:
            return "supplier"
    return None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/search")
async def search_materials(
    q: str = Query(..., description="Material search query (max 500 chars, no PII)"),
    mode: str = Query("tool", description="Search mode: 'tool' (Home Depot) or 'supplier' (Yelp)"),
    address: str | None = Query(None, description="Full project address (street + city + state + ZIP). Tool mode: resolved to nearest HD store via static directory. Supplier mode: used as Yelp find_loc fallback when location/zip_code absent."),
    zip_code: str | None = Query(None, description="ZIP code for local store lookup / Yelp location"),
    location: str | None = Query(None, description="Location string for Yelp supplier search (city, state, or address)"),
    store_id: str | None = Query(None, description="Home Depot store ID override (tool mode only)"),
    include_shopping: bool = Query(False, description="Include Google Shopping results (tool mode only)"),
    idempotency_key: str | None = Query(None, description="Client-generated UUID for dedup"),
    capability_token: str | None = Query(None, description="Capability token (JSON)"),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Cache-first materials search — Home Depot or Yelp engine depending on mode.

    mode=tool (default): Home Depot SerpApi engine (existing Pass C behavior).
    mode=supplier: Yelp SerpApi engine — returns {suppliers: [...]}.

    GREEN tier. All provider calls go through the dual-budget gate.
    Returns 200 with `is_cached_only_mode: true` when both SerpApi accounts
    are exhausted.

    Auto-detect: when mode=tool but the query matches commercial/specialty
    keywords, the response includes suggested_mode='supplier'. Client decides
    whether to flip — the server never auto-flips mode.
    """
    # ── 1. Scope resolution (Law #6) ──────────────────────────────────────────────
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)
    correlation_id = get_correlation_id() or str(uuid.uuid4())
    trace_id = get_trace_id() or correlation_id

    # ── 2. Capability token validation (Law #5) ──────────────────────────────────────────
    cap_token_dict: dict[str, Any] | None = None
    if capability_token:
        import json
        try:
            cap_token_dict = json.loads(capability_token)
        except Exception:
            cap_token_dict = None

    if cap_token_dict is None:
        receipt_id = _emit_receipt(
            receipt_type="materials_search_denied",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="denied", reason_code="MISSING_CAPABILITY_TOKEN",
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=None,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_CAPABILITY_TOKEN", "receipt_id": receipt_id},
        )

    token_result = validate_token(
        cap_token_dict,
        expected_suite_id=suite_id,
        expected_office_id=office_id,
        required_scope="materials:search",
    )
    if not token_result.valid:
        err_code = token_result.error.value if token_result.error else "INVALID_TOKEN"
        receipt_id = _emit_receipt(
            receipt_type="materials_search_denied",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="denied", reason_code=err_code,
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=None,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": err_code, "receipt_id": receipt_id},
        )

    cap_token_id = _cap_token_id(cap_token_dict)

    # ── 3. Query normalisation + PII rejection ────────────────────────────────────────────
    normalised = normalize_query(q)
    if isinstance(normalised, NormalizeRejection):
        receipt_id = _emit_receipt(
            receipt_type="materials_search_rejected",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="denied", reason_code=normalised.code.value,
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=cap_token_id,
            redacted_inputs={"query_length": len(q)},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "INVALID_QUERY",
                "code": normalised.code.value,
                "message": normalised.reason,
                "receipt_id": receipt_id,
            },
        )

    # ── 4. Mode validation + supplier-mode dispatch (Pass E) ───────────────────────
    #
    # Validate mode param (fail-closed: unknown modes are rejected, not silently
    # downgraded to tool mode — that would mask client bugs, Law #3).
    effective_mode = mode.strip().lower() if mode else "tool"
    if effective_mode not in ("tool", "supplier"):
        receipt_id = _emit_receipt(
            receipt_type="materials_search_rejected",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="denied", reason_code="INVALID_MODE",
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=cap_token_id,
            redacted_inputs={"mode": mode, "query_length": len(q)},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "INVALID_MODE",
                "message": "mode must be 'tool' or 'supplier'",
                "receipt_id": receipt_id,
            },
        )

    # Server-side auto-detect: flag if tool-mode query looks like supplier-mode.
    query_lower = normalised.lower()
    suggested_mode = _detect_suggested_mode(query_lower) if effective_mode == "tool" else None

    # Route to supplier mode (Yelp engine)
    if effective_mode == "supplier":
        return await _search_suppliers(
            q=normalised,
            location=location or zip_code or address or "",
            idempotency_key=idempotency_key,
            suite_id=suite_id,
            office_id=office_id,
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            cap_token_id=cap_token_id,
        )

    # ── 5. Idempotency check (tool mode) ─────────────────────────────────────
    if idempotency_key:
        try:
            existing = await supabase_select(
                "materials_search_cache",
                f"idempotency_key=eq.{idempotency_key}&suite_id=eq.{suite_id}",
                limit=1,
            )
            if existing:
                row = existing[0]
                logger.info(
                    "materials_search idempotent replay key=%s suite_id=%s",
                    idempotency_key[:12], suite_id,
                )
                receipt_id = _emit_receipt(
                    receipt_type="materials_search_cached",
                    suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
                    outcome="success",
                    reason_code="IDEMPOTENCY_REPLAY",
                    correlation_id=correlation_id, trace_id=trace_id,
                    capability_token_id=cap_token_id,
                    redacted_outputs={
                        "cached": True,
                        "engine": "home_depot",
                        "query_normalized": normalised,
                        "product_count": row.get("product_count", 0),
                        "specialty_count": row.get("specialty_count", 0),
                    },
                )
                return {
                    "success": True,
                    "products": row.get("products", []),
                    "specialty_suppliers": row.get("specialty_suppliers", []),
                    "filters": row.get("filters", {}),
                    "addon_suggestions": row.get("addon_suggestions", []),
                    "is_cached_only_mode": False,
                    "from_cache": True,
                    "receipt_id": receipt_id,
                    "query_normalized": normalised,
                    "mode": "tool",
                    "suggested_mode": suggested_mode,
                }
        except SupabaseClientError as exc:
            logger.warning("materials_search idempotency check failed: %s", exc)

    # ── 5a. Resolve address → nearest HD store (tool mode only) ─────────────────
    # When the client sends the full project address and no explicit store_id,
    # extract the ZIP from the address string and resolve to the nearest HD
    # store via the O(1) static directory. This prevents SerpApi from defaulting
    # to its built-in geolocation (which has historically returned Bangkok for
    # US addresses when no ZIP/store_id is supplied).
    #
    # Strategy: extract a 5-digit ZIP from the address string using the same
    # regex already used by hd_store_resolver._match_store_by_address. This
    # avoids a geocoding round-trip and keeps the path synchronous + free.
    # If address has no ZIP we still benefit from supplier-mode fallback (above).
    resolved_store_id: str | None = store_id
    resolved_zip: str | None = zip_code
    closest_store_info: dict[str, Any] | None = None

    if address and not store_id:
        import re as _re
        _zip_match = _re.search(r"\b(\d{5})\b", address)
        if _zip_match:
            _extracted_zip = _zip_match.group(1)
            try:
                from aspire_orchestrator.services.adam.hd_store_directory import (
                    lookup_store_by_zip_code,
                )
                _nearest = lookup_store_by_zip_code(_extracted_zip)
                if _nearest:
                    resolved_store_id = str(_nearest.get("store_id", "")) or None
                    resolved_zip = str(_nearest.get("postal_code", "")).zfill(5) or _extracted_zip
                    closest_store_info = {
                        "id": str(_nearest.get("store_id", "")),
                        "name": _nearest.get("name", "Home Depot"),
                        "address": _nearest.get("address", ""),
                        "city": _nearest.get("city", ""),
                        "state": _nearest.get("state", ""),
                        "zip": str(_nearest.get("postal_code", "")).zfill(5),
                    }
                    logger.info(
                        "materials_search address resolved store_id=%s zip=%s",
                        resolved_store_id, resolved_zip,
                    )
                else:
                    # ZIP found in address but not in directory — use extracted ZIP as fallback
                    resolved_zip = _extracted_zip
                    logger.info(
                        "materials_search address ZIP %s not in directory, using as zip_code",
                        _extracted_zip,
                    )
            except Exception as _exc:
                # Fail-soft (Law #3 variant): log and continue with original params.
                # Tool mode will still return results without closest_store.
                logger.warning("materials_search hd_store address lookup failed: %s", _exc)

    # ── 5. In-memory cache hit ────────────────────────────────────────────────────
    # Include resolved_store_id/resolved_zip so two addresses that resolve to
    # different HD stores don't share a cache row (Law #6 — tenant isolation
    # within the same suite extends to store-level result isolation).
    cache_params = {
        "zip": resolved_zip or "",
        "store": resolved_store_id or "",
        "shopping": include_shopping,
    }
    cached_result = cache_get(
        tenant_id=suite_id,  # Law #6: use suite_id as isolation key
        provider="serpapi_home_depot",
        playbook="materials_search",
        query=normalised,
        params=cache_params,
    )
    if cached_result is not None:
        _counts = current_counts()
        receipt_id = _emit_receipt(
            receipt_type="materials_search_cached",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="success", reason_code="CACHE_HIT",
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=cap_token_id,
            redacted_outputs={
                "engine": "home_depot",
                "cached": True,
                "budget_remaining_a": max(0, 240 - _counts.get("A", 0)),
                "budget_remaining_b": max(0, 240 - _counts.get("B", 0)),
                "query_normalized": normalised,
                "resolved_store_id": resolved_store_id,
                "address_provided": bool(address),
                "product_count": len(cached_result.get("products", [])),
                "specialty_count": len(cached_result.get("specialty_suppliers", [])),
            },
        )
        return {
            "success": True,
            **cached_result,
            "closest_store": closest_store_info,
            "is_cached_only_mode": False,
            "from_cache": True,
            "receipt_id": receipt_id,
            "query_normalized": normalised,
            "mode": "tool",
            "suggested_mode": suggested_mode,
        }

    # ── 6. Budget check — fail gracefully with cached-only mode ────────────
    account_id = select_account()
    _counts = current_counts()

    if account_id is None:
        receipt_id = _emit_receipt(
            receipt_type="materials_search_budget_exhausted",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="failed", reason_code="SERPAPI_BUDGET_EXHAUSTED",
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=cap_token_id,
            redacted_outputs={
                "engine": "home_depot",
                "cached": False,
                "budget_remaining_a": 0,
                "budget_remaining_b": 0,
                "query_normalized": normalised,
                "resolved_store_id": resolved_store_id,
                "address_provided": bool(address),
                "product_count": 0,
                "specialty_count": 0,
            },
        )
        return {
            "success": True,
            "products": [],
            "specialty_suppliers": [],
            "filters": {},
            "addon_suggestions": [],
            "closest_store": closest_store_info,
            "is_cached_only_mode": True,
            "from_cache": False,
            "receipt_id": receipt_id,
            "query_normalized": normalised,
            "mode": "tool",
            "suggested_mode": suggested_mode,
        }

    # ── 7. Build PlaybookContext + execute HD search ──────────────────────────────────────
    ctx = PlaybookContext(
        suite_id=suite_id,
        office_id=office_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        capability_token_id=cap_token_id,
    )

    import asyncio

    try:
        research_result = await asyncio.wait_for(
            execute_tool_material_price_check(
                query=normalised,
                ctx=ctx,
                zip_code=resolved_zip or "",
                store_id=resolved_store_id or "",
                voice_path=False,  # R1: text-mode path, 3-attempt loop
            ),
            timeout=_ROUTE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        _counts_post = current_counts()
        receipt_id = _emit_receipt(
            receipt_type="materials_search_timeout",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="failed", reason_code="PROVIDER_TIMEOUT",
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=cap_token_id,
            redacted_outputs={
                "engine": "home_depot",
                "account_id": account_id,
                "cached": False,
                "budget_remaining_a": max(0, 240 - _counts_post.get("A", 0)),
                "budget_remaining_b": max(0, 240 - _counts_post.get("B", 0)),
                "query_normalized": normalised,
                "resolved_store_id": resolved_store_id,
                "address_provided": bool(address),
                "product_count": 0,
                "specialty_count": 0,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"error": "PROVIDER_TIMEOUT", "receipt_id": receipt_id},
        )
    except BudgetExhaustedError:
        _counts_post = current_counts()
        receipt_id = _emit_receipt(
            receipt_type="materials_search_budget_exhausted",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="failed", reason_code="SERPAPI_BOTH_ACCOUNTS_FAILED",
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=cap_token_id,
            redacted_outputs={
                "engine": "home_depot",
                "cached": False,
                "budget_remaining_a": 0,
                "budget_remaining_b": 0,
                "query_normalized": normalised,
                "resolved_store_id": resolved_store_id,
                "address_provided": bool(address),
                "product_count": 0,
                "specialty_count": 0,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "SERPAPI_BOTH_ACCOUNTS_FAILED", "receipt_id": receipt_id},
        )
    except Exception as exc:
        logger.error("materials_search unexpected error: %s", exc, exc_info=True)
        _counts_post = current_counts()
        receipt_id = _emit_receipt(
            receipt_type="materials_search_error",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="failed", reason_code="PROVIDER_INTERNAL_ERROR",
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=cap_token_id,
            redacted_outputs={
                "engine": "home_depot",
                "cached": False,
                "budget_remaining_a": max(0, 240 - _counts_post.get("A", 0)),
                "budget_remaining_b": max(0, 240 - _counts_post.get("B", 0)),
                "query_normalized": normalised,
                "resolved_store_id": resolved_store_id,
                "address_provided": bool(address),
                "product_count": 0,
                "specialty_count": 0,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_INTERNAL_ERROR", "receipt_id": receipt_id},
        )

    # ── 8. Extract products from research_result ───────────────────────────────────────
    products: list[dict[str, Any]] = []
    records = getattr(research_result, "records", None) or []
    for rec in records:
        raw_products = []
        if hasattr(rec, "products"):
            raw_products = rec.products or []
        elif isinstance(rec, dict):
            raw_products = rec.get("products") or rec.get("results") or []
        # Normalise records to dicts
        for p in raw_products:
            products.append(p if isinstance(p, dict) else (p.__dict__ if hasattr(p, "__dict__") else {}))

    # Also check top-level extra dict
    extra = getattr(research_result, "extra", {}) or {}
    if not products and extra.get("results"):
        for item in extra["results"]:
            products.append(item if isinstance(item, dict) else {})

    # ── 9. Specialty fallback (ATTOM POI) when < 3 products ─────────────────────
    specialty_suppliers: list[dict[str, Any]] = []
    if len(products) < 3 and (resolved_zip or zip_code):
        try:
            from aspire_orchestrator.providers.attom_client import (
                execute_attom_poi_search,
            )
            poi_result = await asyncio.wait_for(
                execute_attom_poi_search(
                    payload={
                        "zipCode": resolved_zip or zip_code,
                        "categoryName": "|".join(_HARDWARE_POI_CATEGORIES),
                        "useDefaultCategories": False,
                    },
                    correlation_id=correlation_id,
                    suite_id=suite_id,
                    office_id=office_id,
                ),
                timeout=4.0,
            )
            if poi_result and getattr(poi_result, "outcome", None) and str(poi_result.outcome) == "success":
                data = getattr(poi_result, "data", {}) or {}
                pois = data.get("poi") or data.get("results") or []
                for poi in pois[:8]:
                    specialty_suppliers.append({
                        "id": poi.get("attomId") or poi.get("id") or "",
                        "name": poi.get("businessName") or poi.get("name") or "",
                        "category": poi.get("categoryName") or "",
                        "phone": poi.get("phone") or "",
                        "address": poi.get("address") or "",
                        "distance_miles": poi.get("distance") or 0,
                    })
        except Exception as poi_exc:
            logger.warning("materials_search attom_poi fallback failed: %s", poi_exc)

    # ── 10. Derive filters + add-ons ───────────────────────────────────────────────
    filters = derive_filters(products)
    addon_suggestions = get_predictive_addons(normalised)

    # ── 11. Sanitize before cache write (Law #9) ────────────────────────────────────
    sanitized_products = sanitize_product_list(products)

    # ── 12. Write to in-memory cache (tenant-isolated key, Law #6) ──────────────
    result_payload: dict[str, Any] = {
        "products": sanitized_products,
        "specialty_suppliers": specialty_suppliers,
        "filters": filters,
        "addon_suggestions": addon_suggestions,
    }
    cache_set(
        tenant_id=suite_id,
        provider="serpapi_home_depot",
        playbook="materials_search",
        query=normalised,
        params=cache_params,
        value=result_payload,
        ttl_override=_HD_TTL_SECONDS,
    )

    # ── 13. Persist idempotency record to Supabase ─────────────────────────────────────
    if idempotency_key:
        try:
            await supabase_insert(
                "materials_search_cache",
                {
                    "id": str(uuid.uuid4()),
                    "idempotency_key": idempotency_key,
                    "suite_id": suite_id,
                    "office_id": office_id,
                    "tenant_id": tenant_id,
                    "query_normalized": normalised,
                    "products": sanitized_products,
                    "specialty_suppliers": specialty_suppliers,
                    "filters": filters,
                    "addon_suggestions": addon_suggestions,
                    "product_count": len(sanitized_products),
                    "specialty_count": len(specialty_suppliers),
                    "created_at": _now_iso(),
                },
            )
        except SupabaseClientError as exc:
            logger.warning("materials_search idempotency persist failed: %s", exc)

    # ── 14. Success receipt ─────────────────────────────────────────────────────────
    _counts_final = current_counts()
    receipt_id = _emit_receipt(
        receipt_type="materials_search_success",
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        outcome="success", reason_code="EXECUTED",
        correlation_id=correlation_id, trace_id=trace_id,
        capability_token_id=cap_token_id,
        redacted_outputs={
            "engine": "home_depot",
            "account_id": account_id,
            "cached": False,
            "budget_remaining_a": max(0, 240 - _counts_final.get("A", 0)),
            "budget_remaining_b": max(0, 240 - _counts_final.get("B", 0)),
            "query_normalized": normalised,
            "resolved_store_id": resolved_store_id,
            "address_provided": bool(address),
            "product_count": len(sanitized_products),
            "specialty_count": len(specialty_suppliers),
        },
    )

    logger.info(
        "materials_search success suite_id=%s query=%s products=%d specialty=%d",
        suite_id, normalised[:60], len(sanitized_products), len(specialty_suppliers),
    )

    return {
        "success": True,
        "products": sanitized_products,
        "specialty_suppliers": specialty_suppliers,
        "filters": filters,
        "addon_suggestions": addon_suggestions,
        "closest_store": closest_store_info,
        "is_cached_only_mode": False,
        "from_cache": False,
        "receipt_id": receipt_id,
        "query_normalized": normalised,
        "mode": "tool",
        "suggested_mode": suggested_mode,
    }


# ---------------------------------------------------------------------------
# Supplier mode sub-handler (Pass E — Yelp engine)
# ---------------------------------------------------------------------------


async def _search_suppliers(
    *,
    q: str,
    location: str,
    idempotency_key: str | None,
    suite_id: str,
    office_id: str,
    tenant_id: str,
    correlation_id: str,
    trace_id: str,
    cap_token_id: str | None,
) -> dict[str, Any]:
    """Execute supplier search via SerpApi Yelp engine.

    Cache key: (query_normalized, 'yelp', location_key).
    Cache TTL: 4 hours (supplier hours/availability changes faster than HD pricing).
    Budget: same dual-account pool as tool mode.
    Fail closed (Law #3): exhausted budget → {suppliers: [], mode: 'cached_only'}.
    """
    import asyncio

    location_key = location.strip().lower() if location else ""

    # ── S1. In-memory cache hit ────────────────────────────────────────────────────
    supplier_cache_params = {"location": location_key}
    cached_supplier = cache_get(
        tenant_id=suite_id,
        provider="serpapi_yelp",
        playbook="supplier_search",
        query=q,
        params=supplier_cache_params,
    )
    if cached_supplier is not None:
        _counts = current_counts()
        receipt_id = _emit_receipt(
            receipt_type="materials_supplier_search_cached",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="success", reason_code="CACHE_HIT",
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=cap_token_id,
            redacted_outputs={
                "engine": "yelp",
                "cached": True,
                "budget_remaining_a": max(0, 240 - _counts.get("A", 0)),
                "budget_remaining_b": max(0, 240 - _counts.get("B", 0)),
                "query_normalized": q,
                "find_loc": location_key,
                "supplier_count": len(cached_supplier.get("suppliers", [])),
            },
        )
        return {
            "success": True,
            **cached_supplier,
            "from_cache": True,
            "is_cached_only_mode": False,
            "receipt_id": receipt_id,
            "query_normalized": q,
            "mode": "supplier",
        }

    # ── S2. Budget check — fail gracefully (Law #3) ───────────────────────────────
    account_id = select_account()
    _counts = current_counts()

    if account_id is None:
        receipt_id = _emit_receipt(
            receipt_type="materials_supplier_search_budget_exhausted",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="failed", reason_code="SERPAPI_BUDGET_EXHAUSTED",
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=cap_token_id,
            redacted_outputs={
                "engine": "yelp",
                "cached": False,
                "budget_remaining_a": 0,
                "budget_remaining_b": 0,
                "query_normalized": q,
                "find_loc": location_key,
                "supplier_count": 0,
            },
        )
        return {
            "success": True,
            "suppliers": [],
            "mode": "cached_only",
            "is_cached_only_mode": True,
            "from_cache": False,
            "receipt_id": receipt_id,
            "query_normalized": q,
            "message": "Daily supplier-lookup quota reached",
        }

    # ── S3. Execute Yelp search ───────────────────────────────────────────────────
    try:
        yelp_result = await asyncio.wait_for(
            execute_serpapi_yelp_search(
                payload={
                    "find_desc": q,
                    "find_loc": location or "",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                capability_token_id=cap_token_id,
                timeout=5.0,
            ),
            timeout=_ROUTE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        _counts_post = current_counts()
        receipt_id = _emit_receipt(
            receipt_type="materials_supplier_search_timeout",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="failed", reason_code="PROVIDER_TIMEOUT",
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=cap_token_id,
            redacted_outputs={
                "engine": "yelp",
                "cached": False,
                "budget_remaining_a": max(0, 240 - _counts_post.get("A", 0)),
                "budget_remaining_b": max(0, 240 - _counts_post.get("B", 0)),
                "query_normalized": q,
                "find_loc": location_key,
                "supplier_count": 0,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"error": "PROVIDER_TIMEOUT", "receipt_id": receipt_id},
        )
    except Exception as exc:
        logger.error("materials supplier_search unexpected error: %s", exc, exc_info=True)
        _counts_post = current_counts()
        receipt_id = _emit_receipt(
            receipt_type="materials_supplier_search_error",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="failed", reason_code="PROVIDER_INTERNAL_ERROR",
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=cap_token_id,
            redacted_outputs={
                "engine": "yelp",
                "cached": False,
                "budget_remaining_a": max(0, 240 - _counts_post.get("A", 0)),
                "budget_remaining_b": max(0, 240 - _counts_post.get("B", 0)),
                "query_normalized": q,
                "find_loc": location_key,
                "supplier_count": 0,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_INTERNAL_ERROR", "receipt_id": receipt_id},
        )

    # ── S4. Check for budget exhaustion in adapter result ───────────────────────
    if yelp_result.outcome and str(yelp_result.outcome).lower() in ("failed", "error"):
        error_str = yelp_result.error or ""
        is_budget_exhausted = "budget exhausted" in error_str.lower() or "SERPAPI_BUDGET_EXHAUSTED" in error_str
        _counts_post = current_counts()
        if is_budget_exhausted:
            receipt_id = _emit_receipt(
                receipt_type="materials_supplier_search_budget_exhausted",
                suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
                outcome="failed", reason_code="SERPAPI_BUDGET_EXHAUSTED",
                correlation_id=correlation_id, trace_id=trace_id,
                capability_token_id=cap_token_id,
                redacted_outputs={
                    "engine": "yelp",
                    "cached": False,
                    "budget_remaining_a": 0,
                    "budget_remaining_b": 0,
                    "query_normalized": q,
                    "find_loc": location_key,
                    "supplier_count": 0,
                },
            )
            return {
                "success": True,
                "suppliers": [],
                "mode": "cached_only",
                "is_cached_only_mode": True,
                "from_cache": False,
                "receipt_id": receipt_id,
                "query_normalized": q,
                "message": "Daily supplier-lookup quota reached",
            }
        # Other adapter failure — re-emit receipt with adapter receipt_id context
        receipt_id = _emit_receipt(
            receipt_type="materials_supplier_search_error",
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="failed", reason_code="PROVIDER_INTERNAL_ERROR",
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=cap_token_id,
            redacted_outputs={
                "engine": "yelp",
                "cached": False,
                "budget_remaining_a": max(0, 240 - _counts_post.get("A", 0)),
                "budget_remaining_b": max(0, 240 - _counts_post.get("B", 0)),
                "query_normalized": q,
                "find_loc": location_key,
                "supplier_count": 0,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_INTERNAL_ERROR", "receipt_id": receipt_id},
        )

    # ── S5. Extract suppliers ───────────────────────────────────────────────────
    suppliers: list[dict[str, Any]] = (yelp_result.data or {}).get("suppliers", [])

    # ── S6. Write to in-memory cache ─────────────────────────────────────────────────
    supplier_payload: dict[str, Any] = {"suppliers": suppliers}
    cache_set(
        tenant_id=suite_id,
        provider="serpapi_yelp",
        playbook="supplier_search",
        query=q,
        params=supplier_cache_params,
        value=supplier_payload,
        ttl_override=_YELP_TTL_SECONDS,
    )

    # ── S7. Persist idempotency record ──────────────────────────────────────────────
    if idempotency_key:
        try:
            await supabase_insert(
                "materials_search_cache",
                {
                    "id": str(uuid.uuid4()),
                    "idempotency_key": idempotency_key,
                    "suite_id": suite_id,
                    "office_id": office_id,
                    "tenant_id": tenant_id,
                    "query_normalized": q,
                    "engine": "yelp",
                    "suppliers": suppliers,
                    "product_count": 0,
                    "specialty_count": 0,
                    "supplier_count": len(suppliers),
                    "created_at": _now_iso(),
                },
            )
        except SupabaseClientError as exc:
            logger.warning("materials_supplier_search idempotency persist failed: %s", exc)

    # ── S8. Success receipt ────────────────────────────────────────────────────────
    _counts_final = current_counts()
    receipt_id = _emit_receipt(
        receipt_type="materials_supplier_search_success",
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        outcome="success", reason_code="EXECUTED",
        correlation_id=correlation_id, trace_id=trace_id,
        capability_token_id=cap_token_id,
        redacted_outputs={
            "engine": "yelp",
            "account_id": account_id,
            "cached": False,
            "budget_remaining_a": max(0, 240 - _counts_final.get("A", 0)),
            "budget_remaining_b": max(0, 240 - _counts_final.get("B", 0)),
            "query_normalized": q,
            "find_loc": location_key,
            "supplier_count": len(suppliers),
        },
    )

    logger.info(
        "materials_supplier_search success suite_id=%s query=%s suppliers=%d",
        suite_id, q[:60], len(suppliers),
    )

    return {
        "success": True,
        "suppliers": suppliers,
        "from_cache": False,
        "is_cached_only_mode": False,
        "receipt_id": receipt_id,
        "query_normalized": q,
        "mode": "supplier",
    }
