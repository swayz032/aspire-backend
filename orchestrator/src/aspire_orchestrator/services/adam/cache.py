"""Adam Research Cache — Redis-backed with tenant isolation + SerpApi budget enforcement.

Key design:
  - Tenant hash in every key (Law #6 — zero cross-tenant cache sharing)
  - Per-provider TTLs (places=1h, hotel=1h, product=30m, ATTOM=24h, compliance=7d)
  - SerpApi: no_cache=false means SerpApi's own cache serves free repeats.
    Our Redis cache is a second layer on top. Budget counter only increments on real API calls.
  - Hard stop at 240 SerpApi calls/month (10 buffer from 250 limit)

Cache key format: adam:{tenant_hash}:{provider}:{playbook}:{query_hash}
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTL configuration (seconds)
# ---------------------------------------------------------------------------

PROVIDER_TTL: dict[str, int] = {
    # Places / business — short, recency matters
    "google_places": 3600,
    "here": 3600,
    "foursquare": 3600,
    "tomtom": 3600,
    # Web search — short
    "brave": 3600,
    "tavily": 3600,
    "exa": 3600,
    "parallel": 3600,
    # Product pricing — very short, prices change fast
    "serpapi_shopping": 1800,
    "serpapi_home_depot": 1800,
    # Hotel — short
    "tripadvisor": 3600,
    # Property — medium, with freshness label
    "attom": 86400,
}

# Compliance/reference docs — long TTL
COMPLIANCE_TTL = 604800  # 7 days

# SerpApi budget
SERPAPI_MONTHLY_LIMIT = 240  # Hard stop (10 buffer from 250)

# ---------------------------------------------------------------------------
# In-memory cache (Redis integration point — swap to redis when available)
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[Any, float]] = {}  # key -> (value, expires_at)
_serpapi_counter: dict[str, int] = {}  # "YYYY-MM" -> count


def _hash_tenant(tenant_id: str) -> str:
    """Hash tenant ID for cache keys (Law #9 — no raw tenant IDs in keys)."""
    return hashlib.sha256(tenant_id.encode()).hexdigest()[:12]


def _hash_query(query: str, params: dict[str, Any] | None = None) -> str:
    """Hash query + params for cache key."""
    payload = query + (json.dumps(params, sort_keys=True) if params else "")
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _cache_key(tenant_id: str, provider: str, playbook: str, query: str, params: dict[str, Any] | None = None) -> str:
    """Build tenant-isolated cache key."""
    return f"adam:{_hash_tenant(tenant_id)}:{provider}:{playbook}:{_hash_query(query, params)}"


def cache_get(
    *,
    tenant_id: str,
    provider: str,
    playbook: str,
    query: str,
    params: dict[str, Any] | None = None,
) -> Any | None:
    """Get cached result. Returns None on miss or expiry."""
    key = _cache_key(tenant_id, provider, playbook, query, params)
    entry = _cache.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if time.monotonic() > expires_at:
        _cache.pop(key, None)
        logger.debug("Cache expired: %s", key)
        return None
    logger.debug("Cache hit: %s", key)
    return value


def cache_set(
    *,
    tenant_id: str,
    provider: str,
    playbook: str,
    query: str,
    params: dict[str, Any] | None = None,
    value: Any,
    ttl_override: int | None = None,
) -> None:
    """Set cached result with provider-specific TTL."""
    key = _cache_key(tenant_id, provider, playbook, query, params)
    ttl = ttl_override or PROVIDER_TTL.get(provider, 3600)
    _cache[key] = (value, time.monotonic() + ttl)
    logger.debug("Cache set: %s (TTL=%ds)", key, ttl)


def cache_clear_tenant(tenant_id: str) -> int:
    """Clear all cache entries for a tenant. Returns count cleared."""
    prefix = f"adam:{_hash_tenant(tenant_id)}:"
    keys_to_remove = [k for k in _cache if k.startswith(prefix)]
    for k in keys_to_remove:
        _cache.pop(k, None)
    return len(keys_to_remove)


def cache_clear_all() -> None:
    """Clear entire cache. Useful in tests."""
    _cache.clear()
    _serpapi_counter.clear()


# ---------------------------------------------------------------------------
# SerpApi budget enforcement
# ---------------------------------------------------------------------------

def _current_month() -> str:
    """Get current month key (YYYY-MM)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m")


def serpapi_get_count() -> int:
    """[DEPRECATED] Get current month's SerpApi call count.

    Delegates to serpapi_budget.current_counts() for account A.
    Use serpapi_budget.current_counts() directly for dual-account awareness.
    """
    import warnings
    from aspire_orchestrator.services.adam.serpapi_budget import current_counts
    warnings.warn(
        "cache.serpapi_get_count() is deprecated — use serpapi_budget.current_counts()",
        DeprecationWarning,
        stacklevel=2,
    )
    return current_counts().get("A", 0)


def serpapi_increment() -> int:
    """[DEPRECATED] Increment SerpApi counter. Returns new count.

    Delegates to serpapi_budget.try_increment() for account A.
    Use serpapi_budget.select_account() + try_increment() for dual-account budget gate.
    """
    import warnings
    from aspire_orchestrator.services.adam.serpapi_budget import try_increment, current_counts
    warnings.warn(
        "cache.serpapi_increment() is deprecated — use serpapi_budget.try_increment()",
        DeprecationWarning,
        stacklevel=2,
    )
    try_increment("A")
    return current_counts().get("A", 0)


def serpapi_check_budget() -> bool:
    """[DEPRECATED] Check if SerpApi budget allows another call.

    Delegates to serpapi_budget.select_account() — returns True if any account has budget.
    Use serpapi_budget.select_account() directly for dual-account awareness.
    """
    import warnings
    from aspire_orchestrator.services.adam.serpapi_budget import select_account
    warnings.warn(
        "cache.serpapi_check_budget() is deprecated — use serpapi_budget.select_account()",
        DeprecationWarning,
        stacklevel=2,
    )
    return select_account() is not None


def serpapi_budget_error_message() -> str:
    """[DEPRECATED] Build user-facing error message for budget exhaustion.

    Use serpapi_budget.BudgetExhaustedError(current_counts()) directly.
    """
    import warnings
    from aspire_orchestrator.services.adam.serpapi_budget import current_counts, DEFAULT_CAP
    warnings.warn(
        "cache.serpapi_budget_error_message() is deprecated — use BudgetExhaustedError",
        DeprecationWarning,
        stacklevel=2,
    )
    counts = current_counts()
    total = sum(counts.values())
    total_cap = DEFAULT_CAP * len(counts)
    return (
        f"SerpApi monthly research budget exhausted ({total}/{total_cap} searches used across all accounts). "
        f"Product pricing searches will resume next month. "
        f"Web search alternatives (Brave, Exa) are still available for general pricing research."
    )
