"""supplier_matcher — thin shim delegating to adam_supplier_router.

Wave 5.1a-1 deleted this module and replaced it with adam_supplier_router.route_supplier_search.
Wave 9f re-adds it as a compatibility shim so that pre-existing tests and any other callers that
still import from this module continue to work without modification.

All calls are delegated to adam_supplier_router.  A DeprecationWarning is emitted so that future
callers are steered to the canonical module.

Law compliance (inherited from adam_supplier_router):
  Law #1: Returns structured data — no autonomous decisions.
  Law #2: Receipts emitted per provider sub-call inside adam_supplier_router.
  Law #3: Missing line_item → error result with receipt.
  Law #6: suite_id/office_id scoped throughout.
  Law #9: Only line_item[:100] + counts in receipts/logs.
"""

from __future__ import annotations

import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aspire_orchestrator.services.receipt_store as _receipt_store_module
from aspire_orchestrator.services.blueprint.adam_supplier_router import (
    SupplyCategory,
    route_supplier_search,
)
from aspire_orchestrator.services.supabase_client import supabase_select, supabase_insert

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public result type expected by existing tests
# ---------------------------------------------------------------------------

@dataclass
class SupplierMatchResult:
    matches: list[dict[str, Any]]
    below_minimum: bool
    missing_input_inserted: bool
    source_apis_called: list[str] = field(default_factory=list)
    credits_used: int = 0


# ---------------------------------------------------------------------------
# Internal helpers expected by existing tests (patched in tests)
# ---------------------------------------------------------------------------

async def _fetch_office_location(
    office_id: str,
    suite_id: str,
) -> tuple[float | None, float | None, str | None]:
    """Return (latitude, longitude, zip_code) for the office.

    Queries office_profiles (or offices) table scoped to suite_id (Law #6).
    Returns (None, None, None) on any error — callers skip geo-routing gracefully.
    """
    try:
        rows = await supabase_select(
            "office_profiles",
            filters=f"suite_id=eq.{suite_id}&id=eq.{office_id}",
            limit=1,
        )
        if rows:
            r = rows[0]
            return (
                float(r["latitude"]) if r.get("latitude") is not None else None,
                float(r["longitude"]) if r.get("longitude") is not None else None,
                str(r["zip_code"]) if r.get("zip_code") else None,
            )
    except Exception as exc:
        logger.warning("supplier_matcher._fetch_office_location: error=%s", type(exc).__name__)
    return None, None, None


async def _insert_missing_input(
    *,
    suite_id: str,
    office_id: str,
    project_id: str,
    line_item: str,
    correlation_id: str,
) -> None:
    """Insert a blueprint_missing_inputs row when <3 suppliers found (Law #2)."""
    try:
        await supabase_insert(
            "blueprint_missing_inputs",
            {
                "id": str(uuid.uuid4()),
                "suite_id": suite_id,
                "office_id": office_id,
                "project_id": project_id,
                "field_type": "supplier",
                "field_context": {"line_item": line_item[:100]},
                "reason": "fewer_than_3_suppliers_found",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:
        logger.warning("supplier_matcher._insert_missing_input: error=%s", type(exc).__name__)


def _emit_search_receipt(
    *,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    line_item: str,
    match_count: int,
    provider_mix: dict[str, int],
) -> None:
    """Emit a blueprint.procure.supplier_search receipt (Law #2, Law #9)."""
    import hashlib, json
    truncated = line_item[:100]
    canonical = json.dumps({"line_item": truncated}, sort_keys=True, separators=(",", ":"))
    receipt = {
        "receipt_version": "1.0",
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": "blueprint.procure.supplier_search",
        "actor": "skillpack:supplier-matcher-shim",
        "suite_id": suite_id,
        "office_id": office_id,
        "correlation_id": correlation_id,
        "status": "ok" if match_count >= 3 else "degraded",
        "inputs_hash": f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
        "metadata": {
            "match_count": match_count,
            "provider_mix": provider_mix,
        },
        "redactions": ["line_item_truncated_100"],
    }
    try:
        _receipt_store_module.store_receipts([receipt])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API — delegating to adam_supplier_router
# ---------------------------------------------------------------------------

_MINIMUM_SUPPLIERS = 3

# Category inference heuristic (mirrors legacy supplier_matcher logic)
_PLUMBING_KEYWORDS = frozenset({"pvc", "cpvc", "pex", "copper", "drain", "pipe", "plumbing", "fitting", "valve", "duct"})
_APPLIANCE_KEYWORDS = frozenset({"appliance", "refrigerator", "dishwasher", "range", "oven", "washer", "dryer", "microwave", "tile", "flooring", "finish", "cabinet", "vanity"})
_SPECIALTY_KEYWORDS = frozenset({"custom", "specialty", "exotic", "rare", "fabricated", "millwork", "stone", "granite", "marble"})


def _infer_category(line_item: str) -> SupplyCategory:
    lower = line_item.lower()
    tokens = frozenset(lower.split())
    if tokens & _SPECIALTY_KEYWORDS:
        return "specialty_hardware"
    if tokens & _PLUMBING_KEYWORDS:
        return "commercial_plumbing"
    if tokens & _APPLIANCE_KEYWORDS:
        return "appliance_finish"
    return "commodity"


async def match_suppliers(
    line_item: str,
    *,
    suite_id: str,
    office_id: str,
    project_id: str,
    geofence_miles: float = 25.0,
    correlation_id: str,
) -> SupplierMatchResult:
    """Match suppliers for a material line item.

    Shim that replicates the original supplier_matcher behaviour: aggregate
    results from BOTH SerpAPI Home Depot AND Google Places, then merge and
    deduplicate.  New callers MUST use adam_supplier_router.route_supplier_search
    directly; this shim exists only for backward compatibility with existing tests.

    Emits DeprecationWarning on every call.
    """
    warnings.warn(
        "supplier_matcher.match_suppliers is deprecated. "
        "Use aspire_orchestrator.services.blueprint.adam_supplier_router.route_supplier_search instead.",
        DeprecationWarning,
        stacklevel=2,
    )

    lat, lng, zip_code = await _fetch_office_location(office_id=office_id, suite_id=suite_id)

    # --- Call HD (SerpAPI) ---
    from aspire_orchestrator.services.blueprint.adam_supplier_router import (
        _call_serpapi_homedepot,
        _call_google_places,
        _normalize_serpapi_hd,
        _normalize_places_to_cfq,
        rank_candidates,
    )

    candidates: list[dict[str, Any]] = []
    source_apis: list[str] = []
    credits = 0

    raw_hd, c1 = await _call_serpapi_homedepot(
        line_item=line_item,
        office_zip=zip_code,
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
    )
    credits += c1
    if c1 > 0:
        source_apis.append("serpapi_homedepot")
    normalized_hd = [n for r in raw_hd if (n := _normalize_serpapi_hd(r)) is not None]
    candidates.extend(normalized_hd)

    # --- Call Google Places (local trade) ---
    raw_places, c2 = await _call_google_places(
        line_item=line_item,
        office_lat=lat,
        office_lng=lng,
        geofence_miles=geofence_miles,
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
    )
    credits += c2
    if c2 > 0:
        source_apis.append("google_places")
    normalized_places = [n for r in raw_places if (n := _normalize_places_to_cfq(r)) is not None]
    candidates.extend(normalized_places)

    ranked = rank_candidates(candidates, brand_familiarity_map={})

    below_minimum = len(ranked) < _MINIMUM_SUPPLIERS
    missing_input_inserted = False

    if below_minimum:
        await _insert_missing_input(
            suite_id=suite_id,
            office_id=office_id,
            project_id=project_id,
            line_item=line_item,
            correlation_id=correlation_id,
        )
        missing_input_inserted = True

    provider_mix: dict[str, int] = {}
    for api in source_apis:
        provider_mix[api] = provider_mix.get(api, 0) + 1

    _emit_search_receipt(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        line_item=line_item,
        match_count=len(ranked),
        provider_mix=provider_mix,
    )

    return SupplierMatchResult(
        matches=ranked,
        below_minimum=below_minimum,
        missing_input_inserted=missing_input_inserted,
        source_apis_called=source_apis,
        credits_used=credits,
    )
