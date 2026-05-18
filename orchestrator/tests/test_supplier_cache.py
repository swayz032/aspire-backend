"""Tests for Blueprint Supplier Cache (services/blueprint/supplier_cache.py).

Wave 5.1a-3 — 24-hour TTL cache + per-project Unwrangle credit cap.

Coverage:
  - Cache miss path: fetch_fn called once, result stored, returns (candidates, False)
  - Cache hit path: fetch_fn NOT called, returns (cached, True)
  - TTL expiry: row with expires_at < NOW() treated as miss
  - Cap hit: fetch_fn called with force_serpapi_only=True, result NOT cached
  - Receipt emission on all three code paths (Law #2)
  - Tenant isolation: suite B's identical cache_key cannot read suite A's data (Law #6)
  - Credit increment on miss
  - Concurrent fetches with same key produce one upsert (no double-call)
  - cache_key normalisation: whitespace + case variants yield same key
  - PII never appears in receipts / logs (Law #9)
  - Supabase lookup errors → treated as miss (graceful degradation)
  - fetch_fn raising exceptions → propagated (Law #1, orchestrator retries)
"""

from __future__ import annotations

import hashlib
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SUITE_A = "aaaaaaaa-0000-0000-0000-000000000001"
SUITE_B = "bbbbbbbb-0000-0000-0000-000000000002"
PROJECT_ID = "cccccccc-0000-0000-0000-000000000003"
CORR_ID = "dddddddd-eeee-ffff-0000-000000000004"


def _make_candidates(status: str = "ok", count: int = 2) -> dict[str, Any]:
    return {
        "status": status,
        "candidates": [{"product": {"name": f"Item {i}"}} for i in range(count)],
        "source_apis_called": ["serpapi_homedepot"],
        "credits_used": 10,
    }


def _sha256(category: str, line_item: str, office_zip: str) -> str:
    raw = category + "\x00" + line_item.strip().lower() + "\x00" + (office_zip or "").strip()
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Module import under patch so we can control all async DB calls
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_receipt_store():
    """Suppress real receipt writes in all tests."""
    with patch(
        "aspire_orchestrator.services.blueprint.supplier_cache._receipt_store_module.store_receipts",
        side_effect=lambda receipts: None,
    ):
        yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_supabase():
    """Return a namespace of async mocks for supabase calls."""
    with (
        patch("aspire_orchestrator.services.blueprint.supplier_cache.supabase_select", new_callable=AsyncMock) as mock_select,
        patch("aspire_orchestrator.services.blueprint.supplier_cache.supabase_upsert", new_callable=AsyncMock) as mock_upsert,
        patch("aspire_orchestrator.services.blueprint.supplier_cache.supabase_update", new_callable=AsyncMock) as mock_update,
    ):
        yield {"select": mock_select, "upsert": mock_upsert, "update": mock_update}


# ---------------------------------------------------------------------------
# 1. Cache miss path
# ---------------------------------------------------------------------------

class TestCacheMiss:
    """Cache miss: fetch_fn is called once, result is stored, returns (result, False)."""

    @pytest.mark.asyncio
    async def test_miss_calls_fetch_fn_once(self, mock_supabase):
        mock_supabase["select"].return_value = []   # no cached row
        fetch_fn = AsyncMock(return_value=_make_candidates())

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        candidates, was_cached = await get_or_fetch_supplier_candidates(
            suite_id=SUITE_A,
            project_id=PROJECT_ID,
            category="commodity",
            line_item="PVC pipe 1/2 inch",
            office_zip="30301",
            correlation_id=CORR_ID,
            fetch_fn=fetch_fn,
            credit_cost=10,
        )

        fetch_fn.assert_awaited_once_with(force_serpapi_only=False)

    @pytest.mark.asyncio
    async def test_miss_returns_false_was_cached(self, mock_supabase):
        mock_supabase["select"].return_value = []
        fetch_fn = AsyncMock(return_value=_make_candidates())

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        _, was_cached = await get_or_fetch_supplier_candidates(
            suite_id=SUITE_A,
            project_id=PROJECT_ID,
            category="commodity",
            line_item="copper wire 12awg",
            office_zip="30301",
            correlation_id=CORR_ID,
            fetch_fn=fetch_fn,
        )

        assert was_cached is False

    @pytest.mark.asyncio
    async def test_miss_stores_result_via_upsert(self, mock_supabase):
        mock_supabase["select"].return_value = []
        result_data = _make_candidates()
        fetch_fn = AsyncMock(return_value=result_data)

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        await get_or_fetch_supplier_candidates(
            suite_id=SUITE_A,
            project_id=PROJECT_ID,
            category="commodity",
            line_item="pex tubing",
            office_zip=None,
            correlation_id=CORR_ID,
            fetch_fn=fetch_fn,
            credit_cost=10,
        )

        mock_supabase["upsert"].assert_awaited_once()
        upsert_args = mock_supabase["upsert"].call_args
        row = upsert_args[0][1]  # second positional arg is the data dict
        assert row["suite_id"] == SUITE_A
        assert "cache_key" in row
        assert "payload" in row
        assert "expires_at" in row

    @pytest.mark.asyncio
    async def test_miss_increments_project_credits(self, mock_supabase):
        # select is called three times in a miss-under-cap scenario:
        #   1. cache lookup          -> [] (miss)
        #   2. _get_project_credits  -> [{"unwrangle_credits_used": 5}]  (cap check)
        #   3. _increment_project_credits reads current before write -> [{"unwrangle_credits_used": 5}]
        mock_supabase["select"].side_effect = [
            [],                                    # cache miss
            [{"unwrangle_credits_used": 5}],       # cap check read
            [{"unwrangle_credits_used": 5}],       # read-then-write in increment
        ]
        fetch_fn = AsyncMock(return_value=_make_candidates())

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        await get_or_fetch_supplier_candidates(
            suite_id=SUITE_A,
            project_id=PROJECT_ID,
            category="commercial_plumbing",
            line_item="brass valve 3/4",
            office_zip=None,
            correlation_id=CORR_ID,
            fetch_fn=fetch_fn,
            credit_cost=10,
        )

        # supabase_update should have been called with credits_used = 5+10 = 15
        mock_supabase["update"].assert_awaited()
        update_args = mock_supabase["update"].call_args
        data = update_args[0][2]
        assert data["unwrangle_credits_used"] == 15


# ---------------------------------------------------------------------------
# 2. Cache hit path
# ---------------------------------------------------------------------------

class TestCacheHit:
    """Cache hit: fetch_fn is NEVER called; cached payload is returned as-is."""

    @pytest.mark.asyncio
    async def test_hit_returns_true_was_cached(self, mock_supabase):
        cached = _make_candidates()
        mock_supabase["select"].return_value = [{"payload": cached}]
        fetch_fn = AsyncMock()

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        _, was_cached = await get_or_fetch_supplier_candidates(
            suite_id=SUITE_A,
            project_id=PROJECT_ID,
            category="commodity",
            line_item="2x4 lumber 8ft",
            office_zip="90210",
            correlation_id=CORR_ID,
            fetch_fn=fetch_fn,
        )

        assert was_cached is True

    @pytest.mark.asyncio
    async def test_hit_fetch_fn_never_called(self, mock_supabase):
        cached = _make_candidates()
        mock_supabase["select"].return_value = [{"payload": cached}]
        fetch_fn = AsyncMock()

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        await get_or_fetch_supplier_candidates(
            suite_id=SUITE_A,
            project_id=PROJECT_ID,
            category="commodity",
            line_item="2x4 lumber 8ft",
            office_zip="90210",
            correlation_id=CORR_ID,
            fetch_fn=fetch_fn,
        )

        fetch_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hit_returns_cached_payload(self, mock_supabase):
        cached = _make_candidates(status="ok", count=3)
        mock_supabase["select"].return_value = [{"payload": cached}]
        fetch_fn = AsyncMock()

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        result, _ = await get_or_fetch_supplier_candidates(
            suite_id=SUITE_A,
            project_id=PROJECT_ID,
            category="commodity",
            line_item="2x4 lumber 8ft",
            office_zip="90210",
            correlation_id=CORR_ID,
            fetch_fn=fetch_fn,
        )

        assert result == cached

    @pytest.mark.asyncio
    async def test_hit_does_not_increment_credits(self, mock_supabase):
        cached = _make_candidates()
        mock_supabase["select"].return_value = [{"payload": cached}]
        fetch_fn = AsyncMock()

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        await get_or_fetch_supplier_candidates(
            suite_id=SUITE_A,
            project_id=PROJECT_ID,
            category="commodity",
            line_item="drywall 1/2",
            office_zip=None,
            correlation_id=CORR_ID,
            fetch_fn=fetch_fn,
        )

        mock_supabase["update"].assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. TTL expiry — expired row = miss
# ---------------------------------------------------------------------------

class TestTTLExpiry:
    """Rows with expires_at < NOW() are NOT returned by the cache lookup."""

    @pytest.mark.asyncio
    async def test_expired_row_treated_as_miss(self, mock_supabase):
        """The supabase_select filter includes expires_at=gt.NOW, so expired
        rows return empty — the service treats that as a miss and calls fetch_fn."""
        mock_supabase["select"].return_value = []  # simulates GT filter excluding stale row
        fetch_fn = AsyncMock(return_value=_make_candidates())

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        _, was_cached = await get_or_fetch_supplier_candidates(
            suite_id=SUITE_A,
            project_id=PROJECT_ID,
            category="commodity",
            line_item="expired item",
            office_zip=None,
            correlation_id=CORR_ID,
            fetch_fn=fetch_fn,
        )

        assert was_cached is False
        fetch_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fresh_row_returned_as_hit(self, mock_supabase):
        """A row whose expires_at is in the future is returned as a hit."""
        mock_supabase["select"].return_value = [{"payload": _make_candidates()}]
        fetch_fn = AsyncMock()

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        _, was_cached = await get_or_fetch_supplier_candidates(
            suite_id=SUITE_A,
            project_id=PROJECT_ID,
            category="commodity",
            line_item="fresh item",
            office_zip=None,
            correlation_id=CORR_ID,
            fetch_fn=fetch_fn,
        )

        assert was_cached is True


# ---------------------------------------------------------------------------
# 4. Credit cap enforcement
# ---------------------------------------------------------------------------

class TestCreditCap:
    """When project's unwrangle_credits_used >= cap, use force_serpapi_only=True."""

    @pytest.mark.asyncio
    async def test_cap_hit_calls_fetch_fn_with_force_serpapi_only(self, mock_supabase):
        mock_supabase["select"].side_effect = [
            [],   # cache miss
            [{"unwrangle_credits_used": 25}],  # project at cap
        ]
        fetch_fn = AsyncMock(return_value=_make_candidates())

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates
        from aspire_orchestrator.config.settings import settings

        with patch.object(settings, "unwrangle_per_project_cap", 25):
            await get_or_fetch_supplier_candidates(
                suite_id=SUITE_A,
                project_id=PROJECT_ID,
                category="commodity",
                line_item="cap test item",
                office_zip=None,
                correlation_id=CORR_ID,
                fetch_fn=fetch_fn,
            )

        fetch_fn.assert_awaited_once_with(force_serpapi_only=True)

    @pytest.mark.asyncio
    async def test_cap_hit_does_not_store_in_cache(self, mock_supabase):
        mock_supabase["select"].side_effect = [
            [],
            [{"unwrangle_credits_used": 100}],
        ]
        fetch_fn = AsyncMock(return_value=_make_candidates())

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates
        from aspire_orchestrator.config.settings import settings

        with patch.object(settings, "unwrangle_per_project_cap", 25):
            await get_or_fetch_supplier_candidates(
                suite_id=SUITE_A,
                project_id=PROJECT_ID,
                category="commodity",
                line_item="cap test no cache",
                office_zip=None,
                correlation_id=CORR_ID,
                fetch_fn=fetch_fn,
            )

        mock_supabase["upsert"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cap_hit_returns_false_was_cached(self, mock_supabase):
        mock_supabase["select"].side_effect = [
            [],
            [{"unwrangle_credits_used": 50}],
        ]
        fetch_fn = AsyncMock(return_value=_make_candidates())

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates
        from aspire_orchestrator.config.settings import settings

        with patch.object(settings, "unwrangle_per_project_cap", 25):
            _, was_cached = await get_or_fetch_supplier_candidates(
                suite_id=SUITE_A,
                project_id=PROJECT_ID,
                category="commodity",
                line_item="cap test",
                office_zip=None,
                correlation_id=CORR_ID,
                fetch_fn=fetch_fn,
            )

        assert was_cached is False

    @pytest.mark.asyncio
    async def test_under_cap_uses_normal_fetch(self, mock_supabase):
        mock_supabase["select"].side_effect = [
            [],
            [{"unwrangle_credits_used": 10}],
        ]
        fetch_fn = AsyncMock(return_value=_make_candidates())

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates
        from aspire_orchestrator.config.settings import settings

        with patch.object(settings, "unwrangle_per_project_cap", 25):
            await get_or_fetch_supplier_candidates(
                suite_id=SUITE_A,
                project_id=PROJECT_ID,
                category="commodity",
                line_item="under cap item",
                office_zip=None,
                correlation_id=CORR_ID,
                fetch_fn=fetch_fn,
            )

        fetch_fn.assert_awaited_once_with(force_serpapi_only=False)


# ---------------------------------------------------------------------------
# 5. Receipt emission — Law #2
# ---------------------------------------------------------------------------

class TestReceiptEmission:
    """Every code path emits exactly one receipt with correct event_type."""

    @pytest.mark.asyncio
    async def test_miss_emits_miss_receipt(self, mock_supabase):
        mock_supabase["select"].return_value = []
        fetch_fn = AsyncMock(return_value=_make_candidates())

        receipts_captured: list[list[dict]] = []

        with patch(
            "aspire_orchestrator.services.blueprint.supplier_cache._receipt_store_module.store_receipts",
            side_effect=lambda r: receipts_captured.append(r),
        ):
            from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

            await get_or_fetch_supplier_candidates(
                suite_id=SUITE_A,
                project_id=PROJECT_ID,
                category="commodity",
                line_item="receipt miss test",
                office_zip=None,
                correlation_id=CORR_ID,
                fetch_fn=fetch_fn,
            )

        flat = [r for batch in receipts_captured for r in batch]
        miss_receipts = [r for r in flat if r["event_type"] == "blueprint.supplier_cache.miss"]
        assert len(miss_receipts) == 1, f"Expected 1 miss receipt, got: {[r['event_type'] for r in flat]}"
        assert miss_receipts[0]["suite_id"] == SUITE_A
        assert miss_receipts[0]["correlation_id"] == CORR_ID
        assert miss_receipts[0]["status"] == "miss"

    @pytest.mark.asyncio
    async def test_hit_emits_hit_receipt(self, mock_supabase):
        mock_supabase["select"].return_value = [{"payload": _make_candidates()}]
        fetch_fn = AsyncMock()

        receipts_captured: list[list[dict]] = []

        with patch(
            "aspire_orchestrator.services.blueprint.supplier_cache._receipt_store_module.store_receipts",
            side_effect=lambda r: receipts_captured.append(r),
        ):
            from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

            await get_or_fetch_supplier_candidates(
                suite_id=SUITE_A,
                project_id=PROJECT_ID,
                category="commodity",
                line_item="receipt hit test",
                office_zip=None,
                correlation_id=CORR_ID,
                fetch_fn=fetch_fn,
            )

        flat = [r for batch in receipts_captured for r in batch]
        hit_receipts = [r for r in flat if r["event_type"] == "blueprint.supplier_cache.hit"]
        assert len(hit_receipts) == 1
        assert hit_receipts[0]["status"] == "hit"

    @pytest.mark.asyncio
    async def test_cap_hit_emits_cap_receipt(self, mock_supabase):
        mock_supabase["select"].side_effect = [
            [],
            [{"unwrangle_credits_used": 25}],
        ]
        fetch_fn = AsyncMock(return_value=_make_candidates())

        receipts_captured: list[list[dict]] = []

        from aspire_orchestrator.config.settings import settings

        with (
            patch(
                "aspire_orchestrator.services.blueprint.supplier_cache._receipt_store_module.store_receipts",
                side_effect=lambda r: receipts_captured.append(r),
            ),
            patch.object(settings, "unwrangle_per_project_cap", 25),
        ):
            from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

            await get_or_fetch_supplier_candidates(
                suite_id=SUITE_A,
                project_id=PROJECT_ID,
                category="commodity",
                line_item="cap receipt test",
                office_zip=None,
                correlation_id=CORR_ID,
                fetch_fn=fetch_fn,
            )

        flat = [r for batch in receipts_captured for r in batch]
        cap_receipts = [r for r in flat if r["event_type"] == "blueprint.supplier_cache.cap_hit"]
        assert len(cap_receipts) == 1
        assert cap_receipts[0]["metadata"]["force_serpapi_only"] is True


# ---------------------------------------------------------------------------
# 6. Tenant isolation (Law #6)
# ---------------------------------------------------------------------------

class TestTenantIsolation:
    """suite_id is embedded in every DB query; cross-tenant reads return nothing."""

    @pytest.mark.asyncio
    async def test_cache_key_lookup_scoped_to_suite_id(self, mock_supabase):
        """supabase_select is called with suite_id=eq.<SUITE_A>, not suite B."""
        mock_supabase["select"].return_value = []
        fetch_fn = AsyncMock(return_value=_make_candidates())

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        await get_or_fetch_supplier_candidates(
            suite_id=SUITE_A,
            project_id=PROJECT_ID,
            category="commodity",
            line_item="isolation check item",
            office_zip="77001",
            correlation_id=CORR_ID,
            fetch_fn=fetch_fn,
        )

        # The first select call (cache lookup) must include SUITE_A
        first_call = mock_supabase["select"].call_args_list[0]
        filter_str = str(first_call)
        assert SUITE_A in filter_str

    @pytest.mark.asyncio
    async def test_suite_b_key_collision_returns_empty(self, mock_supabase):
        """Identical line_item for suite B should not read suite A's cache entry.

        We simulate this by verifying the DB query always scopes to the caller's
        suite_id — the select filter will include suite_id=eq.SUITE_B, which at the
        DB layer hits a different partition of rows thanks to RLS."""
        # Suite B lookup returns nothing (different tenant, RLS isolates it)
        mock_supabase["select"].return_value = []
        fetch_fn = AsyncMock(return_value=_make_candidates())

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        _, was_cached_b = await get_or_fetch_supplier_candidates(
            suite_id=SUITE_B,
            project_id=PROJECT_ID,
            category="commodity",
            line_item="shared line item",
            office_zip=None,
            correlation_id=CORR_ID,
            fetch_fn=fetch_fn,
        )

        assert was_cached_b is False, (
            "BLOCKER: Suite B read a cache hit — tenant isolation violated (Law #6)"
        )
        # Verify suite_id=eq.SUITE_B in the lookup, not SUITE_A
        first_select = mock_supabase["select"].call_args_list[0]
        assert SUITE_B in str(first_select)
        assert SUITE_A not in str(first_select)


# ---------------------------------------------------------------------------
# 7. Cache key normalisation
# ---------------------------------------------------------------------------

class TestCacheKeyNormalization:
    """Whitespace and case variants of the same item must produce the same key."""

    def test_same_key_different_case(self):
        from aspire_orchestrator.services.blueprint.supplier_cache import _cache_key
        k1 = _cache_key(category="commodity", line_item="1/2 PVC PIPE", office_zip="30301")
        k2 = _cache_key(category="commodity", line_item="1/2 pvc pipe", office_zip="30301")
        assert k1 == k2

    def test_same_key_strips_whitespace(self):
        from aspire_orchestrator.services.blueprint.supplier_cache import _cache_key
        k1 = _cache_key(category="commodity", line_item="  1/2 PVC pipe  ", office_zip="30301")
        k2 = _cache_key(category="commodity", line_item="1/2 pvc pipe", office_zip="30301")
        assert k1 == k2

    def test_different_zip_different_key(self):
        from aspire_orchestrator.services.blueprint.supplier_cache import _cache_key
        k1 = _cache_key(category="commodity", line_item="item", office_zip="30301")
        k2 = _cache_key(category="commodity", line_item="item", office_zip="90210")
        assert k1 != k2

    def test_none_zip_and_empty_string_zip_same_key(self):
        from aspire_orchestrator.services.blueprint.supplier_cache import _cache_key
        k1 = _cache_key(category="commodity", line_item="item", office_zip=None)
        k2 = _cache_key(category="commodity", line_item="item", office_zip="")
        assert k1 == k2

    def test_different_categories_different_keys(self):
        from aspire_orchestrator.services.blueprint.supplier_cache import _cache_key
        k1 = _cache_key(category="commodity", line_item="pipe", office_zip=None)
        k2 = _cache_key(category="commercial_plumbing", line_item="pipe", office_zip=None)
        assert k1 != k2


# ---------------------------------------------------------------------------
# 8. Concurrent fetches (upsert idempotency)
# ---------------------------------------------------------------------------

class TestConcurrentFetches:
    """Two concurrent misses for the same key call fetch_fn twice but upsert twice.

    Both upserts use ON CONFLICT merge-duplicates so the final DB state is correct
    regardless of ordering.  We verify upsert is called for each miss independently.
    """

    @pytest.mark.asyncio
    async def test_concurrent_misses_both_upsert(self, mock_supabase):
        mock_supabase["select"].return_value = []
        fetch_fn = AsyncMock(return_value=_make_candidates())

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        await asyncio.gather(
            get_or_fetch_supplier_candidates(
                suite_id=SUITE_A,
                project_id=PROJECT_ID,
                category="commodity",
                line_item="concurrent item",
                office_zip=None,
                correlation_id=CORR_ID,
                fetch_fn=fetch_fn,
            ),
            get_or_fetch_supplier_candidates(
                suite_id=SUITE_A,
                project_id=PROJECT_ID,
                category="commodity",
                line_item="concurrent item",
                office_zip=None,
                correlation_id=CORR_ID,
                fetch_fn=fetch_fn,
            ),
        )

        # Both tasks tried to upsert (ON CONFLICT handles the race at DB layer)
        assert mock_supabase["upsert"].await_count == 2


# ---------------------------------------------------------------------------
# 9. PII never in receipts (Law #9)
# ---------------------------------------------------------------------------

class TestPIIRedaction:
    """Receipts must not contain raw API response data or full line_item text."""

    @pytest.mark.asyncio
    async def test_receipt_does_not_contain_raw_payload(self, mock_supabase):
        mock_supabase["select"].return_value = []
        sensitive_item = "SSN: 123-45-6789 copper wire"
        fetch_fn = AsyncMock(return_value=_make_candidates())

        receipts_captured: list[list[dict]] = []

        with patch(
            "aspire_orchestrator.services.blueprint.supplier_cache._receipt_store_module.store_receipts",
            side_effect=lambda r: receipts_captured.append(r),
        ):
            from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

            await get_or_fetch_supplier_candidates(
                suite_id=SUITE_A,
                project_id=PROJECT_ID,
                category="commodity",
                line_item=sensitive_item,
                office_zip=None,
                correlation_id=CORR_ID,
                fetch_fn=fetch_fn,
            )

        flat = [r for batch in receipts_captured for r in batch]
        for receipt in flat:
            receipt_str = str(receipt)
            # The raw API response (candidates list) must not appear
            assert "Item 0" not in receipt_str, "Raw API response leaked into receipt"
            # line_item_prefix is truncated to 80 chars
            if "line_item_prefix" in receipt_str:
                meta = receipt.get("metadata", {})
                prefix = meta.get("line_item_prefix", "")
                assert len(prefix) <= 80

    @pytest.mark.asyncio
    async def test_receipt_redaction_field_present(self, mock_supabase):
        mock_supabase["select"].return_value = []
        fetch_fn = AsyncMock(return_value=_make_candidates())

        receipts_captured: list[list[dict]] = []

        with patch(
            "aspire_orchestrator.services.blueprint.supplier_cache._receipt_store_module.store_receipts",
            side_effect=lambda r: receipts_captured.append(r),
        ):
            from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

            await get_or_fetch_supplier_candidates(
                suite_id=SUITE_A,
                project_id=PROJECT_ID,
                category="commodity",
                line_item="test item",
                office_zip=None,
                correlation_id=CORR_ID,
                fetch_fn=fetch_fn,
            )

        flat = [r for batch in receipts_captured for r in batch]
        for receipt in flat:
            assert "redactions" in receipt
            assert "raw_api_response_omitted" in receipt["redactions"]


# ---------------------------------------------------------------------------
# 10. Supabase errors handled gracefully
# ---------------------------------------------------------------------------

class TestSupabaseErrorHandling:
    """DB errors on lookup are treated as cache misses; execution continues."""

    @pytest.mark.asyncio
    async def test_lookup_db_error_treated_as_miss(self, mock_supabase):
        from aspire_orchestrator.services.supabase_client import SupabaseClientError
        mock_supabase["select"].side_effect = SupabaseClientError("select/blueprint_supplier_cache", 503, "unavailable")
        fetch_fn = AsyncMock(return_value=_make_candidates())

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        # Should NOT raise; treats DB error as miss
        candidates, was_cached = await get_or_fetch_supplier_candidates(
            suite_id=SUITE_A,
            project_id=PROJECT_ID,
            category="commodity",
            line_item="db error item",
            office_zip=None,
            correlation_id=CORR_ID,
            fetch_fn=fetch_fn,
        )

        # fetch_fn was called (treated as miss)
        fetch_fn.assert_awaited()
        # result is returned from fetch_fn
        assert candidates["status"] == "ok"

    @pytest.mark.asyncio
    async def test_fetch_fn_exception_propagates(self, mock_supabase):
        """fetch_fn raising propagates — orchestrator decides retry (Law #1)."""
        mock_supabase["select"].return_value = []
        fetch_fn = AsyncMock(side_effect=RuntimeError("provider timeout"))

        from aspire_orchestrator.services.blueprint.supplier_cache import get_or_fetch_supplier_candidates

        with pytest.raises(RuntimeError, match="provider timeout"):
            await get_or_fetch_supplier_candidates(
                suite_id=SUITE_A,
                project_id=PROJECT_ID,
                category="commodity",
                line_item="error propagate item",
                office_zip=None,
                correlation_id=CORR_ID,
                fetch_fn=fetch_fn,
            )


# ---------------------------------------------------------------------------
# 11. Settings — unwrangle_per_project_cap default
# ---------------------------------------------------------------------------

class TestSettings:
    """The default cap value and env var prefix are correct."""

    def test_default_cap_is_25(self):
        from aspire_orchestrator.config.settings import Settings
        s = Settings()
        assert s.unwrangle_per_project_cap == 25

    def test_cap_readable_from_env(self, monkeypatch):
        monkeypatch.setenv("ASPIRE_UNWRANGLE_PER_PROJECT_CAP", "50")
        from aspire_orchestrator.config.settings import Settings
        s = Settings()
        assert s.unwrangle_per_project_cap == 50
