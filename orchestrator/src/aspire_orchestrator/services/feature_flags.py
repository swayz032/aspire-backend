"""Feature Flags Service — Supabase-backed with in-memory TTL cache.

Queries a `feature_flags` table in Supabase:
    SELECT enabled FROM feature_flags WHERE flag_name = $1 AND tenant_id = $2

Cache: In-memory dict with 60-second TTL per (flag_name, tenant_id) pair.
Receipt: Every flag evaluation emits a GREEN receipt (Law #2).
Fail-closed: Missing flag or query failure returns False (Law #3).
Tenant isolation: Queries are always scoped by tenant_id (Law #6).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from aspire_orchestrator.services.receipt_store import store_receipts
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache configuration
# ---------------------------------------------------------------------------
_CACHE_TTL_SECONDS: float = 60.0
_cache: dict[tuple[str, str], tuple[bool, float]] = {}  # (flag, tenant) -> (enabled, expires_at)


def _cache_get(flag_name: str, tenant_id: str) -> bool | None:
    """Return cached value if present and not expired, else None."""
    key = (flag_name, tenant_id)
    entry = _cache.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if time.monotonic() > expires_at:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(flag_name: str, tenant_id: str, enabled: bool) -> None:
    """Store a flag value in cache with TTL."""
    key = (flag_name, tenant_id)
    _cache[key] = (enabled, time.monotonic() + _CACHE_TTL_SECONDS)


def clear_cache() -> None:
    """Clear the entire flag cache. Useful in tests."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Receipt helper
# ---------------------------------------------------------------------------
def _emit_receipt(
    flag_name: str,
    tenant_id: str,
    enabled: bool,
    source: str,
    error: str | None = None,
) -> None:
    """Emit a GREEN receipt for a flag evaluation (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_type": "feature_flag_evaluation",
        "action_type": "feature_flag.check",
        "outcome": "success" if error is None else "failed",
        "risk_tier": "green",
        "actor_type": "system",
        "actor_id": "feature_flags_service",
        "suite_id": tenant_id,
        "tool_used": "feature_flags",
        "redacted_inputs": {"flag_name": flag_name, "tenant_id": tenant_id},
        "redacted_outputs": {
            "enabled": enabled,
            "source": source,
        },
    }
    if error is not None:
        receipt["error_message"] = error
    store_receipts([receipt])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def is_feature_enabled(flag_name: str, tenant_id: str) -> bool:
    """Check whether a feature flag is enabled for a tenant.

    Returns:
        True if the flag exists and is enabled for the tenant.
        False if the flag is missing, disabled, or the query fails (Law #3: fail-closed).

    The result is cached for 60 seconds to avoid repeated Supabase queries.
    Every evaluation emits a receipt (Law #2).
    """
    # 1. Check cache
    cached = _cache_get(flag_name, tenant_id)
    if cached is not None:
        _emit_receipt(flag_name, tenant_id, cached, source="cache")
        return cached

    # 2. Query Supabase
    enabled = False
    source = "supabase"
    error: str | None = None

    try:
        rows: list[dict[str, Any]] = await supabase_select(
            "feature_flags",
            {"flag_name": flag_name, "tenant_id": tenant_id},
            limit=1,
        )
        if rows and isinstance(rows[0], dict):
            raw = rows[0].get("enabled")
            enabled = bool(raw)
            source = "supabase"
        else:
            # Flag not found — fail closed (disabled)
            enabled = False
            source = "supabase_not_found"
    except SupabaseClientError as exc:
        logger.warning(
            "Feature flag query failed (fail-closed): flag=%s tenant=%s error=%s",
            flag_name,
            tenant_id,
            exc,
        )
        enabled = False
        source = "supabase_error"
        error = str(exc)
    except Exception as exc:
        logger.error(
            "Unexpected error checking feature flag (fail-closed): flag=%s tenant=%s error=%s",
            flag_name,
            tenant_id,
            exc,
        )
        enabled = False
        source = "unexpected_error"
        error = str(exc)

    # 3. Cache result (even failures — prevents hammering a broken Supabase)
    _cache_set(flag_name, tenant_id, enabled)

    # 4. Emit receipt
    _emit_receipt(flag_name, tenant_id, enabled, source=source, error=error)

    return enabled
