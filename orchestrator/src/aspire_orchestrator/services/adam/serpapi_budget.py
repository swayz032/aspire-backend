"""SerpApi dual-account budget manager — Supabase-persistent monthly counter.

Design:
  - Two SerpApi accounts (A and B), each capped at 240 calls/month.
  - select_account() picks the first account that still has budget.
  - try_increment() atomically increments via Supabase RPC (fail-safe on DB error).
  - get_api_key() maps account_id → env var (never stored in settings/logs).
  - mark_account_exhausted() hard-caps the account in DB immediately.
  - current_counts() returns live budget state for receipt redacted_outputs.

Env vars (Railway Ava-Brain):
  account A → SERPAPI_API_KEY
  account B → ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY

Law #9: API keys are never logged. account_id ('A'/'B') is safe to log.
Law #2: Budget state is surfaced in adapter receipts via current_counts().
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACCOUNTS: tuple[str, ...] = ("A", "B")
DEFAULT_CAP: int = 240  # 10-unit buffer below SerpApi free-tier 250 limit

# ---------------------------------------------------------------------------
# Supabase client (lazy init — mirrors provider_call_logger pattern)
# ---------------------------------------------------------------------------

_supabase_client: Any = None
_supabase_init_done: bool = False
_supabase_init_lock = threading.Lock()


def _init_supabase() -> Any | None:
    """Lazy-init Supabase client for budget persistence."""
    global _supabase_client, _supabase_init_done

    if _supabase_init_done:
        return _supabase_client

    with _supabase_init_lock:
        if _supabase_init_done:
            return _supabase_client

        url = os.environ.get("ASPIRE_SUPABASE_URL", "")
        key = os.environ.get("ASPIRE_SUPABASE_SERVICE_ROLE_KEY", "")

        if url and key:
            try:
                from supabase import create_client
                _supabase_client = create_client(url, key)
                logger.info("SerpApiBudget: Supabase client initialized")
            except Exception as exc:
                logger.warning("SerpApiBudget: Supabase init failed: %s", exc)
                _supabase_client = None
        else:
            logger.warning(
                "SerpApiBudget: ASPIRE_SUPABASE_URL or ASPIRE_SUPABASE_SERVICE_ROLE_KEY "
                "not set — falling back to in-memory counter"
            )
            _supabase_client = None

        _supabase_init_done = True
        return _supabase_client


# ---------------------------------------------------------------------------
# In-memory fallback (used when Supabase unavailable — resets on restart)
# ---------------------------------------------------------------------------

_in_memory_counts: dict[str, dict[str, int]] = {}  # month -> {account_id -> count}
_in_memory_lock = threading.Lock()


def _current_month() -> str:
    """Return current UTC month key as 'YYYY-MM'."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Budget read
# ---------------------------------------------------------------------------

def _get_count_for_account(account_id: str) -> int:
    """Return current month's call count for account_id.

    Tries Supabase first; falls back to in-memory on any error.
    """
    month = _current_month()
    client = _init_supabase()

    if client is not None:
        try:
            result = (
                client.table("serpapi_budget")
                .select("count")
                .eq("month", month)
                .eq("account_id", account_id)
                .execute()
            )
            rows = result.data if result else []
            if isinstance(rows, list) and rows:
                return int(rows[0].get("count", 0))
            return 0
        except Exception as exc:
            logger.warning(
                "SerpApiBudget: DB read failed for account %s — using in-memory: %s",
                account_id, exc,
            )

    # In-memory fallback
    with _in_memory_lock:
        return _in_memory_counts.get(month, {}).get(account_id, 0)


def current_counts() -> dict[str, int]:
    """Return {account_id: count} for current month for all accounts.

    Used by adapters to populate receipt redacted_outputs.
    """
    return {acc: _get_count_for_account(acc) for acc in ACCOUNTS}


# ---------------------------------------------------------------------------
# Account selection
# ---------------------------------------------------------------------------

def select_account() -> str | None:
    """Return the first account (A then B) that still has budget, or None.

    None means both accounts are exhausted — caller must raise BudgetExhaustedError.
    """
    for account_id in ACCOUNTS:
        count = _get_count_for_account(account_id)
        if count < DEFAULT_CAP:
            return account_id
    return None


# ---------------------------------------------------------------------------
# Atomic increment via Supabase RPC
# ---------------------------------------------------------------------------

def try_increment(account_id: str) -> bool:
    """Atomically increment the budget counter for account_id.

    Returns True if the increment succeeded (slot was available).
    Returns False if the account was already at cap (RPC returns NULL).

    The RPC `increment_serpapi_budget` uses UPDATE ... WHERE count < p_cap,
    so a NULL return means the row was at cap — no increment happened.
    """
    month = _current_month()
    client = _init_supabase()

    if client is not None:
        try:
            result = client.rpc(
                "increment_serpapi_budget",
                {"p_month": month, "p_account_id": account_id, "p_cap": DEFAULT_CAP},
            ).execute()

            new_count = result.data
            # Defensive: supabase-py may wrap scalar in list across versions (R1)
            if isinstance(new_count, list):
                new_count = new_count[0] if new_count else None

            if new_count is None:
                # RPC returned NULL → UPDATE matched no rows → account at cap
                logger.warning(
                    "SerpApiBudget: account %s at cap (%d) — increment denied",
                    account_id, DEFAULT_CAP,
                )
                return False

            new_count = int(new_count)
            if new_count >= DEFAULT_CAP:
                logger.warning(
                    "SerpApiBudget: account %s reached cap (%d/%d)",
                    account_id, new_count, DEFAULT_CAP,
                )
            elif new_count >= 200:
                logger.warning(
                    "SerpApiBudget: account %s WARNING (%d/%d calls this month)",
                    account_id, new_count, DEFAULT_CAP,
                )
            else:
                logger.debug(
                    "SerpApiBudget: account %s incremented to %d/%d",
                    account_id, new_count, DEFAULT_CAP,
                )
            return True

        except Exception as exc:
            logger.warning(
                "SerpApiBudget: DB increment failed for account %s — using in-memory: %s",
                account_id, exc,
            )

    # In-memory fallback
    with _in_memory_lock:
        month_counts = _in_memory_counts.setdefault(month, {})
        current = month_counts.get(account_id, 0)
        if current >= DEFAULT_CAP:
            return False
        month_counts[account_id] = current + 1
        return True


# ---------------------------------------------------------------------------
# Key resolution (Law #9 — keys never logged)
# ---------------------------------------------------------------------------

def get_api_key(account_id: str) -> str:
    """Resolve account_id to its API key from environment.

    Maps:
      'A' → SERPAPI_API_KEY          (Account A — primary, Railway env var)
      'B' → ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY  (Account B — Railway env var)

    Raises KeyError if the env var is missing or empty (fail-closed, Law #3).
    """
    if account_id == "A":
        key = os.environ.get("SERPAPI_API_KEY", "").strip()
        if not key:
            # Also try ASPIRE_SERPAPI_API_KEY (settings prefix form)
            key = os.environ.get("ASPIRE_SERPAPI_API_KEY", "").strip()
        if not key:
            raise KeyError(
                "SERPAPI_API_KEY not configured — SerpApi account A unavailable"
            )
        return key

    if account_id == "B":
        key = os.environ.get("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "").strip()
        if not key:
            raise KeyError(
                "ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY not configured — "
                "SerpApi account B unavailable"
            )
        return key

    raise KeyError(f"Unknown SerpApi account_id: {account_id!r}")


# ---------------------------------------------------------------------------
# Hard exhaustion marker
# ---------------------------------------------------------------------------

def mark_account_exhausted(account_id: str, reason: str) -> None:
    """Force account_id count to cap in DB (e.g. after receiving HTTP 429).

    This prevents further calls from select_account() picking this account
    until the monthly cron reset.
    """
    month = _current_month()
    client = _init_supabase()

    if client is not None:
        try:
            client.table("serpapi_budget").upsert(
                {
                    "month": month,
                    "account_id": account_id,
                    "count": DEFAULT_CAP,
                    "cap": DEFAULT_CAP,
                },
                on_conflict="month,account_id",
            ).execute()
            logger.warning(
                "SerpApiBudget: account %s forcibly exhausted (reason=%s)",
                account_id, reason,
            )
        except Exception as exc:
            logger.warning(
                "SerpApiBudget: failed to mark account %s exhausted: %s",
                account_id, exc,
            )

    # Law #2: emit immutable receipt for every account exhaustion event.
    try:
        from aspire_orchestrator.services.receipt_store import store_receipts
        store_receipts([{
            "id": str(uuid.uuid4()),
            "action_type": "external_api.budget.exhausted",
            "tool_used": "serpapi_budget",
            "outcome": "failed",
            "reason_code": "SERPAPI_ACCOUNT_EXHAUSTED",
            "actor_type": "SYSTEM",
            "actor_id": "serpapi-budget-module",
            "risk_tier": "green",
            "receipt_type": "orchestrator",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "redacted_outputs": {
                "account_id": account_id,
                "reason": reason,
                "month": month,
            },
            "receipt_hash": "",
        }])
    except Exception:
        pass  # Receipt failure must never block the budget mutation

    # Mirror in in-memory fallback regardless
    with _in_memory_lock:
        _in_memory_counts.setdefault(month, {})[account_id] = DEFAULT_CAP


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class BudgetExhaustedError(Exception):
    """Raised when both SerpApi accounts have exhausted their monthly budget."""

    def __init__(self, counts: dict[str, int] | None = None) -> None:
        counts = counts or {}
        msg = (
            f"SerpApi monthly budget exhausted on all accounts "
            f"(A={counts.get('A', DEFAULT_CAP)}/{DEFAULT_CAP}, "
            f"B={counts.get('B', DEFAULT_CAP)}/{DEFAULT_CAP}). "
            f"Product pricing searches will resume next month."
        )
        super().__init__(msg)
        self.counts = counts


# ---------------------------------------------------------------------------
# Test helpers (reset in-memory state between tests)
# ---------------------------------------------------------------------------

def _reset_for_tests() -> None:
    """Reset in-memory state. Call in test teardown only."""
    global _supabase_init_done, _supabase_client
    with _in_memory_lock:
        _in_memory_counts.clear()
    with _supabase_init_lock:
        _supabase_init_done = False
        _supabase_client = None
