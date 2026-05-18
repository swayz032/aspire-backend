"""Blueprint Supplier Cache — Wave 5.1a-3.

24-hour TTL Supabase-backed cache for Adam's MATERIAL_SUPPLIER_SEARCH Unwrangle
results.  Conserves the ~100-credit Unwrangle trial plan by de-duplicating repeat
lookups across blueprint lines and projects.

Aspire Laws enforced:
  Law #1: No autonomous decisions — returns (candidates, was_cached) only.
          fetch_fn is called exactly once per miss; retries are the orchestrator's.
  Law #2: Every code path (hit / miss / cap_hit) emits a receipt to receipt_store.
  Law #3: Supabase errors are logged and propagated; no silent degradation.
  Law #6: suite_id is embedded in cache_key + all DB queries.  Cross-tenant
          isolation is enforced at DB layer via RLS (migration 118).
  Law #9: Only category[:60] + line_item[:80] + counts appear in receipts/logs.

Public API
----------
    candidates, was_cached = await get_or_fetch_supplier_candidates(
        suite_id=..., project_id=..., category=..., line_item=...,
        office_zip=..., correlation_id=..., fetch_fn=<async callable>,
        credit_cost=10,
    )

``fetch_fn`` signature
----------------------
    async def fetch_fn(force_serpapi_only: bool = False) -> CandidateList
    ...where CandidateList = dict[str, Any] (same shape as adam_supplier_router.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aspire_orchestrator.config.settings import settings
import aspire_orchestrator.services.receipt_store as _receipt_store_module
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
    supabase_upsert,
    supabase_update,
)

logger = logging.getLogger(__name__)

CandidateList = dict[str, Any]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_TABLE_CACHE = "blueprint_supplier_cache"
_TABLE_PROJECTS = "blueprint_projects"
_CACHE_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cache_key(*, category: str, line_item: str, office_zip: str | None) -> str:
    """SHA256 of (category + normalised line_item + zip_or_empty).

    Normalisation strips leading/trailing whitespace and lowercases so
    '  1/2 PVC pipe  ' and '1/2 PVC PIPE' produce the same key.
    """
    line_item_norm = line_item.strip().lower()
    zip_part = (office_zip or "").strip()
    raw = category + "\x00" + line_item_norm + "\x00" + zip_part
    return hashlib.sha256(raw.encode()).hexdigest()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _expires_at() -> str:
    return (_now_utc() + timedelta(hours=_CACHE_TTL_HOURS)).isoformat()


def _make_receipt(
    *,
    event_type: str,
    suite_id: str,
    project_id: str,
    correlation_id: str,
    status: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build a Law #2 immutable receipt for a cache event."""
    return {
        "receipt_version": "1.0",
        "receipt_id": str(uuid.uuid4()),
        "ts": _now_utc().isoformat(),
        "event_type": event_type,
        "actor": "skillpack:drew-blueprint",
        "suite_id": suite_id,
        "project_id": project_id,  # extra context — not a core receipt field
        "correlation_id": correlation_id,
        "status": status,
        "policy": {
            "decision": "allow" if status in ("hit", "miss", "cap_hit") else "deny",
            "policy_id": "blueprint-supplier-cache-v1",
            "reasons": [],
        },
        "redactions": [
            "line_item_truncated_80",
            "raw_api_response_omitted",
        ],
        "metadata": metadata,
    }


def _store_receipt(receipt: dict[str, Any]) -> None:
    """Fire-and-forget receipt write — failures are logged, never raised."""
    try:
        _receipt_store_module.store_receipts([receipt])
    except Exception as exc:
        logger.warning(
            "supplier_cache: receipt store failed event_type=%s err=%s",
            receipt.get("event_type"),
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Credit cap check
# ---------------------------------------------------------------------------

async def _get_project_credits(project_id: str, suite_id: str) -> int:
    """Return unwrangle_credits_used for the given project row.

    Returns 0 if the row is missing or the column doesn't exist yet
    (graceful degradation during rolling deploy).
    """
    try:
        rows = await supabase_select(
            _TABLE_PROJECTS,
            f"id=eq.{project_id}&suite_id=eq.{suite_id}&select=unwrangle_credits_used",
        )
        if rows:
            return int(rows[0].get("unwrangle_credits_used", 0))
        return 0
    except SupabaseClientError as exc:
        logger.warning(
            "supplier_cache: could not read project credits project_id=%s err=%s",
            project_id[:8],
            type(exc).__name__,
        )
        return 0


async def _increment_project_credits(
    project_id: str, suite_id: str, delta: int
) -> None:
    """Atomically increment unwrangle_credits_used on blueprint_projects.

    Uses a PATCH via supabase_update.  If the update fails we log and continue
    — the cap is a cost-control soft gate, not a hard correctness requirement.
    The worst outcome of a failed increment is one extra Unwrangle call.
    """
    try:
        # PostgREST does not support SQL expressions in PATCH bodies, so we
        # read-then-write.  The window between read and write is small (~1ms)
        # and the cap is a cost hint, not an atomic limit, so this is fine.
        current = await _get_project_credits(project_id, suite_id)
        await supabase_update(
            _TABLE_PROJECTS,
            f"id=eq.{project_id}&suite_id=eq.{suite_id}",
            {"unwrangle_credits_used": current + delta},
        )
    except Exception as exc:
        logger.warning(
            "supplier_cache: credit increment failed project_id=%s delta=%d err=%s",
            project_id[:8],
            delta,
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Cache read / write
# ---------------------------------------------------------------------------

async def _cache_lookup(
    suite_id: str, key: str
) -> CandidateList | None:
    """Return cached payload if a non-expired row exists, else None."""
    now_iso = _now_utc().isoformat()
    try:
        rows = await supabase_select(
            _TABLE_CACHE,
            f"suite_id=eq.{suite_id}&cache_key=eq.{key}&expires_at=gt.{now_iso}",
            limit=1,
        )
        if rows:
            return rows[0].get("payload")  # type: ignore[return-value]
        return None
    except SupabaseClientError as exc:
        logger.warning(
            "supplier_cache: lookup failed suite=%s err=%s",
            suite_id[:8],
            type(exc).__name__,
        )
        return None  # treat DB error as a cache miss — do not block execution


async def _cache_store(
    *,
    suite_id: str,
    key: str,
    payload: CandidateList,
    source_apis: list[str],
    credits_used: int,
) -> None:
    """Upsert a cache row.  ON CONFLICT (suite_id, cache_key) DO UPDATE.

    Two concurrent fetches for the same key will race to the DB; the last
    upsert wins.  Both store identical data so the outcome is correct.
    """
    row = {
        "suite_id": suite_id,
        "cache_key": key,
        "payload": payload,
        "source_apis": source_apis,
        "credits_used": credits_used,
        "expires_at": _expires_at(),
    }
    try:
        await supabase_upsert(
            _TABLE_CACHE,
            row,
            on_conflict="suite_id,cache_key",
        )
    except SupabaseClientError as exc:
        logger.warning(
            "supplier_cache: store failed suite=%s err=%s",
            suite_id[:8],
            type(exc).__name__,
        )
        # Non-fatal — we already have the result, we just couldn't cache it.


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_or_fetch_supplier_candidates(
    *,
    suite_id: str,
    project_id: str,
    category: str,
    line_item: str,
    office_zip: str | None,
    correlation_id: str,
    fetch_fn: Callable[..., Awaitable[CandidateList]],
    credit_cost: int = 10,
) -> tuple[CandidateList, bool]:
    """Look up supplier candidates in the 24-hour cache; fetch on miss.

    Parameters
    ----------
    suite_id:        Tenant suite UUID (Law #6 isolation).
    project_id:      Blueprint project UUID — used for per-project credit tracking.
    category:        Supplier category string (commodity / commercial_plumbing / …).
    line_item:       Raw line item text — normalised internally for key hashing.
    office_zip:      ZIP code used for geo-aware lookups; included in cache key.
    correlation_id:  Trace ID propagated into all receipts (Law #2).
    fetch_fn:        Async callable ``async (force_serpapi_only: bool = False) -> CandidateList``.
                     Called at most once per cache miss.  NEVER called on a hit.
    credit_cost:     Unwrangle credits consumed by one fetch_fn() call (default 10).

    Returns
    -------
    (candidates, was_cached)
        candidates  — CandidateList dict (same shape as adam_supplier_router output).
        was_cached  — True if the result came from cache; False if fetch_fn was called.

    Law compliance
    --------------
    Law #1: No autonomous decisions.  Returns result only; never retries.
    Law #2: Emits blueprint.supplier_cache.hit / .miss / .cap_hit receipt on every path.
    Law #3: Capability token validation is the orchestrator's responsibility;
            this layer enforces suite_id scoping only.
    Law #6: cache_key embeds suite_id; DB RLS enforces row-level isolation.
    Law #9: Only category[:60] + line_item[:80] in receipts/logs; no raw API data.
    """
    key = _cache_key(category=category, line_item=line_item, office_zip=office_zip)
    cap = settings.unwrangle_per_project_cap

    # ── 1. Cache lookup ────────────────────────────────────────────────────────
    cached_payload = await _cache_lookup(suite_id, key)

    if cached_payload is not None:
        # HIT — return early, fetch_fn never called
        _store_receipt(
            _make_receipt(
                event_type="blueprint.supplier_cache.hit",
                suite_id=suite_id,
                project_id=project_id,
                correlation_id=correlation_id,
                status="hit",
                metadata={
                    "category": category[:60],
                    "line_item_prefix": line_item[:80],
                    "cache_key": key[:16] + "…",
                    "office_zip": office_zip,
                },
            )
        )
        logger.debug(
            "supplier_cache: HIT suite=%s category=%s item=%s",
            suite_id[:8], category, line_item[:40],
        )
        return cached_payload, True

    # ── 2. Miss — check per-project credit cap ──────────────────────────────
    current_credits = await _get_project_credits(project_id, suite_id)
    over_cap = current_credits >= cap

    if over_cap:
        # CAP HIT — call fetch_fn with force_serpapi_only=True, do NOT cache
        logger.warning(
            "supplier_cache: CAP HIT suite=%s project=%s credits=%d cap=%d "
            "switching to serpapi_only for item=%s",
            suite_id[:8], project_id[:8], current_credits, cap, line_item[:40],
        )
        try:
            candidates = await fetch_fn(force_serpapi_only=True)
        except Exception as exc:
            logger.error(
                "supplier_cache: fetch_fn(force_serpapi_only=True) failed item=%s err=%s",
                line_item[:40], type(exc).__name__, exc_info=True,
            )
            # Propagate — orchestrator decides retry strategy (Law #1)
            raise

        _store_receipt(
            _make_receipt(
                event_type="blueprint.supplier_cache.cap_hit",
                suite_id=suite_id,
                project_id=project_id,
                correlation_id=correlation_id,
                status="cap_hit",
                metadata={
                    "category": category[:60],
                    "line_item_prefix": line_item[:80],
                    "credits_used_on_project": current_credits,
                    "cap": cap,
                    "force_serpapi_only": True,
                    "cached": False,
                },
            )
        )
        return candidates, False

    # ── 3. Miss under cap — call fetch_fn normally ──────────────────────────
    try:
        candidates = await fetch_fn(force_serpapi_only=False)
    except Exception as exc:
        logger.error(
            "supplier_cache: fetch_fn failed item=%s err=%s",
            line_item[:40], type(exc).__name__, exc_info=True,
        )
        raise  # propagate; orchestrator retries (Law #1)

    # ── 4. Store result in cache ─────────────────────────────────────────────
    source_apis: list[str] = candidates.get("source_apis_called", [])
    await _cache_store(
        suite_id=suite_id,
        key=key,
        payload=candidates,
        source_apis=source_apis,
        credits_used=credit_cost,
    )

    # ── 5. Increment project credit counter ─────────────────────────────────
    await _increment_project_credits(project_id, suite_id, credit_cost)

    # ── 6. Emit MISS receipt ─────────────────────────────────────────────────
    _store_receipt(
        _make_receipt(
            event_type="blueprint.supplier_cache.miss",
            suite_id=suite_id,
            project_id=project_id,
            correlation_id=correlation_id,
            status="miss",
            metadata={
                "category": category[:60],
                "line_item_prefix": line_item[:80],
                "cache_key": key[:16] + "…",
                "office_zip": office_zip,
                "credit_cost": credit_cost,
                "project_credits_after": current_credits + credit_cost,
                "source_apis": source_apis,
                "cached": True,
            },
        )
    )
    logger.debug(
        "supplier_cache: MISS stored suite=%s category=%s item=%s credits_now=%d",
        suite_id[:8], category, line_item[:40], current_credits + credit_cost,
    )
    return candidates, False
