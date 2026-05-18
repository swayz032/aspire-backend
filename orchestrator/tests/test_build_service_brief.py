"""Tests for BriefMaterializer.build_service_brief().

Coverage:
  - Empty-state: no memory → empty brief, cache row upserted, receipt emitted
  - With picks + overrides: brief_json contains projected items
  - Pending intents: unresolved intents counted correctly
  - Handoffs: handoff_note entries appear in brief
  - Active threads: count reflects open service threads
  - Refresh=False (cache hit): returns cached row, no re-upsert
  - Refresh=True: bypasses TTL, rebuilds even when cache is fresh
  - Stale cache: TTL expired → rebuilds
  - Receipt emission: receipt with action=memory.service_brief.built present
  - Receipt failure: receipt store error does NOT crash build
  - RLS isolation (suite B sees nothing from suite A)
  - office_id mismatch → MemoryServiceError with TENANT_ISOLATION_VIOLATION
  - DB upsert failure → MemoryServiceError with DB_UPSERT_FAILED + failure receipt
  - freshness_seq monotonically increases across rebuilds
  - brief_text rendered from counts only (Law #9 — no content in rendered text)
  - active_threads DB error → count defaults to 0, build continues
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from aspire_orchestrator.schemas.memory_v1 import (
    ServiceBriefOut,
    ScopedIdentity,
)
from aspire_orchestrator.services.brief_materializer import BriefMaterializer
from aspire_orchestrator.services.memory_service import MemoryServiceError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_A = uuid.uuid4()
SUITE_A = uuid.uuid4()
OFFICE_A = uuid.uuid4()

TENANT_B = uuid.uuid4()
SUITE_B = uuid.uuid4()
OFFICE_B = uuid.uuid4()

NOW = datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope_a() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)


def _scope_b() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_B, suite_id=SUITE_B, office_id=OFFICE_B)


def _cache_row(
    *,
    scope: ScopedIdentity | None = None,
    last_built: datetime | None = None,
    freshness_seq: int = 1,
    picks: int = 0,
    overrides: int = 0,
    pending_intents: int = 0,
    handoffs: int = 0,
    threads: int = 0,
) -> dict[str, Any]:
    """Build a service_brief_cache DB row dict."""
    if scope is None:
        scope = _scope_a()
    if last_built is None:
        last_built = NOW - timedelta(seconds=10)  # fresh
    return {
        "tenant_id": str(scope.tenant_id),
        "suite_id": str(scope.suite_id),
        "office_id": str(scope.office_id),
        "brief_text": (
            f"Service brief: {picks} recent picks, {overrides} overrides, "
            f"{pending_intents} open pending intents, {handoffs} recent handoffs, "
            f"{threads} active threads, 0 open candidates "
            f"(0 due now, 0 overdue), 0 pending approvals."
        ),
        "brief_json": {
            "recent_picks": [],
            "recent_overrides": [],
            "open_pending_intents": [],
            "recent_handoffs": [],
            "open_candidates": [],
            "pending_approvals": [],
            "recent_receipts": [],
            "recent_picks_count": picks,
            "recent_overrides_count": overrides,
            "open_pending_intents_count": pending_intents,
            "recent_handoffs_count": handoffs,
            "active_threads_count": threads,
        },
        "due_now_count": 0,
        "overdue_count": 0,
        "pending_approval_count": 0,
        "recent_receipts_count": 0,
        "last_built_at": last_built.isoformat(),
        "freshness_seq": freshness_seq,
    }


def _memory_row(
    *,
    memory_type: str = "decision_fact",
    detail: dict[str, Any] | None = None,
    scope: ScopedIdentity | None = None,
    visibility_scope: str = "service",
) -> dict[str, Any]:
    """Build a minimal memory_objects row dict."""
    if scope is None:
        scope = _scope_a()
    return {
        "memory_id": str(uuid.uuid4()),
        "tenant_id": str(scope.tenant_id),
        "suite_id": str(scope.suite_id),
        "office_id": str(scope.office_id),
        "memory_type": memory_type,
        "visibility_scope": visibility_scope,
        "detail": detail or {},
        "title": "test memory",
        "summary": "test summary",
        "status": "approved",
        "thread_id": None,
        "last_activity_at": NOW.isoformat(),
    }


def _pick_row(scope: ScopedIdentity | None = None) -> dict[str, Any]:
    return _memory_row(
        memory_type="decision_fact",
        detail={"decision_type": "material_pick", "material": "tile"},
        scope=scope,
    )


def _override_row(scope: ScopedIdentity | None = None) -> dict[str, Any]:
    return _memory_row(
        memory_type="decision_fact",
        detail={"decision_type": "material_override", "material": "grout"},
        scope=scope,
    )


def _pending_intent_row(scope: ScopedIdentity | None = None) -> dict[str, Any]:
    return _memory_row(memory_type="pending_intent", scope=scope)


def _handoff_row(scope: ScopedIdentity | None = None) -> dict[str, Any]:
    return _memory_row(memory_type="handoff_note", scope=scope)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def materializer() -> BriefMaterializer:
    return BriefMaterializer()


# ---------------------------------------------------------------------------
# 1. Empty state — no memory → returns empty brief + upserts + emits receipt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEmptyState:
    async def test_empty_brief_upserts_cache_row(
        self, materializer: BriefMaterializer
    ) -> None:
        """No memory objects → build succeeds, upsert called once."""
        fresh_row = _cache_row(freshness_seq=1)

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(return_value=[]),  # cache miss + all data fetches empty
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=fresh_row),
        ) as mock_upsert, patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
        ):
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        mock_upsert.assert_awaited_once()
        assert isinstance(result, ServiceBriefOut)
        assert result.tenant_id == TENANT_A
        assert result.suite_id == SUITE_A
        assert result.office_id == OFFICE_A
        assert result.freshness_seq == 1

    async def test_empty_brief_seq_starts_at_one(
        self, materializer: BriefMaterializer
    ) -> None:
        """When no prior cache exists, freshness_seq initialises to 1."""
        built_row = _cache_row(freshness_seq=1)

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(return_value=[]),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=built_row),
        ), patch("aspire_orchestrator.services.receipt_store.store_receipts"):
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        assert result.freshness_seq == 1


# ---------------------------------------------------------------------------
# 2. With picks + overrides — brief_json contains projected items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWithServiceData:
    async def test_three_picks_two_overrides(
        self, materializer: BriefMaterializer
    ) -> None:
        """3 picks + 2 overrides → brief reflects those counts."""
        picks = [_pick_row() for _ in range(3)]
        # decision_fact rows for different types: picks + overrides returned together
        overrides = [_override_row() for _ in range(2)]
        all_decision_facts = picks + overrides

        def _select_side_effect(table: str, *args: Any, **kwargs: Any) -> list[dict]:
            if table == "service_brief_cache":
                return []  # cache miss
            if table == "memory_objects":
                return all_decision_facts
            return []

        built_row = _cache_row(freshness_seq=1, picks=3, overrides=2)

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(side_effect=_select_side_effect),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=built_row),
        ) as mock_upsert, patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
        ):
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        mock_upsert.assert_awaited_once()
        upsert_payload = mock_upsert.call_args[0][1]  # second positional arg = row dict
        assert upsert_payload["brief_json"]["recent_picks_count"] == 3
        assert upsert_payload["brief_json"]["recent_overrides_count"] == 2
        assert result.recent_picks_count == 3
        assert result.recent_overrides_count == 2

    async def test_pending_intents_counted(
        self, materializer: BriefMaterializer
    ) -> None:
        """2 unresolved pending intents → open_pending_intents_count = 2."""
        intents = [_pending_intent_row() for _ in range(2)]

        def _select_side_effect(table: str, *args: Any, **kwargs: Any) -> list[dict]:
            if table == "service_brief_cache":
                return []
            if table == "memory_objects":
                return intents
            return []

        built_row = _cache_row(freshness_seq=1, pending_intents=2)

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(side_effect=_select_side_effect),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=built_row),
        ), patch("aspire_orchestrator.services.receipt_store.store_receipts"):
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        assert result.open_pending_intents_count == 2

    async def test_handoffs_counted(self, materializer: BriefMaterializer) -> None:
        """3 handoff_note rows → recent_handoffs_count = 3."""
        handoffs = [_handoff_row() for _ in range(3)]

        def _select_side_effect(table: str, *args: Any, **kwargs: Any) -> list[dict]:
            if table == "service_brief_cache":
                return []
            if table == "memory_objects":
                return handoffs
            return []

        built_row = _cache_row(freshness_seq=1, handoffs=3)

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(side_effect=_select_side_effect),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=built_row),
        ), patch("aspire_orchestrator.services.receipt_store.store_receipts"):
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        assert result.recent_handoffs_count == 3

    async def test_active_threads_count_reflected(
        self, materializer: BriefMaterializer
    ) -> None:
        """4 active threads → active_threads_count = 4."""
        thread_rows = [
            {
                "tenant_id": str(TENANT_A),
                "suite_id": str(SUITE_A),
                "office_id": str(OFFICE_A),
                "thread_id": str(uuid.uuid4()),
                "thread_type": "job_thread",
                "status": "open",
                "last_activity_at": NOW.isoformat(),
            }
            for _ in range(4)
        ]

        def _select_side_effect(table: str, *args: Any, **kwargs: Any) -> list[dict]:
            if table == "service_brief_cache":
                return []
            if table == "threads":
                return thread_rows
            return []  # memory_objects, proactive_candidates, approval_links, receipts

        built_row = _cache_row(freshness_seq=1, threads=4)

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(side_effect=_select_side_effect),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=built_row),
        ), patch("aspire_orchestrator.services.receipt_store.store_receipts"):
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        assert result.active_threads_count == 4


# ---------------------------------------------------------------------------
# 3. Cache TTL behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCacheBehaviour:
    async def test_refresh_false_returns_fresh_cache(
        self, materializer: BriefMaterializer
    ) -> None:
        """Fresh cache (<60s) + refresh=False → return cache, NO upsert."""
        fresh_row = _cache_row(last_built=NOW - timedelta(seconds=10), freshness_seq=5)

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(return_value=[fresh_row]),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(),
        ) as mock_upsert:
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        mock_upsert.assert_not_awaited()
        assert result.freshness_seq == 5

    async def test_refresh_true_bypasses_fresh_cache(
        self, materializer: BriefMaterializer
    ) -> None:
        """Fresh cache + refresh=True → still rebuilds."""
        fresh_row = _cache_row(last_built=NOW - timedelta(seconds=10), freshness_seq=5)
        rebuilt_row = _cache_row(last_built=NOW, freshness_seq=6)

        def _select_side_effect(table: str, *args: Any, **kwargs: Any) -> list[dict]:
            if table == "service_brief_cache":
                return [fresh_row]
            return []

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(side_effect=_select_side_effect),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=rebuilt_row),
        ) as mock_upsert, patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
        ):
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=True
            )

        mock_upsert.assert_awaited_once()
        assert result.freshness_seq == 6

    async def test_stale_cache_triggers_rebuild(
        self, materializer: BriefMaterializer
    ) -> None:
        """Stale cache (>60s) + refresh=False → rebuilds."""
        stale_row = _cache_row(last_built=NOW - timedelta(minutes=5), freshness_seq=3)
        rebuilt_row = _cache_row(last_built=NOW, freshness_seq=4)

        def _select_side_effect(table: str, *args: Any, **kwargs: Any) -> list[dict]:
            if table == "service_brief_cache":
                return [stale_row]
            return []

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(side_effect=_select_side_effect),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=rebuilt_row),
        ) as mock_upsert, patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
        ):
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        mock_upsert.assert_awaited_once()
        assert result.freshness_seq == 4

    async def test_freshness_seq_increments(
        self, materializer: BriefMaterializer
    ) -> None:
        """Two successive rebuilds → freshness_seq increases monotonically."""
        stale_row = _cache_row(last_built=NOW - timedelta(minutes=5), freshness_seq=7)
        rebuilt_row = _cache_row(last_built=NOW, freshness_seq=8)

        def _select_side_effect(table: str, *args: Any, **kwargs: Any) -> list[dict]:
            if table == "service_brief_cache":
                return [stale_row]
            return []

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(side_effect=_select_side_effect),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=rebuilt_row),
        ), patch("aspire_orchestrator.services.receipt_store.store_receipts"):
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=True
            )

        assert result.freshness_seq > stale_row["freshness_seq"]


# ---------------------------------------------------------------------------
# 4. Receipt emission (Law #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestReceiptEmission:
    async def test_receipt_emitted_on_build(
        self, materializer: BriefMaterializer
    ) -> None:
        """Receipt with action=memory.service_brief.built is stored on every build."""
        built_row = _cache_row(freshness_seq=1)
        captured_receipts: list[dict] = []

        def _capture(receipts: list[dict]) -> None:
            captured_receipts.extend(receipts)

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(return_value=[]),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=built_row),
        ), patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
            side_effect=_capture,
        ):
            await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        assert len(captured_receipts) == 1
        r = captured_receipts[0]
        assert r["action_type"] == "memory.service_brief.built"
        assert r["outcome"] == "ok"
        assert r["suite_id"] == str(SUITE_A)
        assert r["tenant_id"] == str(TENANT_A)
        assert r["office_id"] == str(OFFICE_A)

    async def test_receipt_contains_counts_not_content(
        self, materializer: BriefMaterializer
    ) -> None:
        """Receipt details must contain integer counts, not memory content (Law #9)."""
        built_row = _cache_row(freshness_seq=1)
        captured_receipts: list[dict] = []

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(return_value=[]),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=built_row),
        ), patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
            side_effect=lambda r: captured_receipts.extend(r),
        ):
            await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        details = captured_receipts[0]["details"]
        for key in (
            "picks_count",
            "overrides_count",
            "open_pending_intents_count",
            "recent_handoffs_count",
            "active_threads_count",
        ):
            assert key in details
            assert isinstance(details[key], int), f"{key} should be int"

    async def test_receipt_failure_does_not_crash_build(
        self, materializer: BriefMaterializer
    ) -> None:
        """If receipt store raises, build_service_brief still returns successfully."""
        built_row = _cache_row(freshness_seq=1)

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(return_value=[]),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=built_row),
        ), patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
            side_effect=RuntimeError("receipt store unavailable"),
        ):
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        # Build must succeed even if receipt emission fails
        assert isinstance(result, ServiceBriefOut)
        assert result.freshness_seq == 1

    async def test_receipt_emitted_on_failed_upsert(
        self, materializer: BriefMaterializer
    ) -> None:
        """Even when upsert raises, a failed receipt must be emitted (Law #2)."""
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        captured_receipts: list[dict] = []

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(return_value=[]),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(side_effect=SupabaseClientError("DB down", detail="DB down")),
        ), patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
            side_effect=lambda r: captured_receipts.extend(r),
        ):
            with pytest.raises(MemoryServiceError):
                await materializer.build_service_brief(
                    OFFICE_A, scope=_scope_a(), refresh=False
                )

        # Receipt must still have been emitted with outcome=failed
        assert len(captured_receipts) >= 1
        assert captured_receipts[0]["outcome"] == "failed"


# ---------------------------------------------------------------------------
# 5. RLS isolation (Law #6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTenantIsolation:
    async def test_suite_b_does_not_see_suite_a_picks(
        self, materializer: BriefMaterializer
    ) -> None:
        """Suite B's fetch must use suite B's scope — never reads suite A data."""
        suite_a_picks = [_pick_row(scope=_scope_a()) for _ in range(3)]
        suite_b_row = _cache_row(scope=_scope_b(), freshness_seq=1)

        # Capture filter strings used in supabase_select calls
        observed_filters: list[str] = []

        async def _select_spy(table: str, filter_str: str = "", **kwargs: Any) -> list[dict]:
            observed_filters.append(filter_str)
            if table == "service_brief_cache":
                return []
            return []  # suite B has no data

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(side_effect=_select_spy),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=suite_b_row),
        ), patch("aspire_orchestrator.services.receipt_store.store_receipts"):
            result = await materializer.build_service_brief(
                OFFICE_B, scope=_scope_b(), refresh=False
            )

        # Every filter that was issued must scope to SUITE_B, never SUITE_A
        for f in observed_filters:
            assert str(SUITE_A) not in f, (
                f"Filter leaked suite A ID into suite B query: {f!r}"
            )
        assert result.suite_id == SUITE_B

    async def test_office_id_scope_mismatch_raises(
        self, materializer: BriefMaterializer
    ) -> None:
        """office_id that does not match scope.office_id → TENANT_ISOLATION_VIOLATION."""
        mismatched_office = uuid.uuid4()

        with pytest.raises(MemoryServiceError) as exc_info:
            await materializer.build_service_brief(
                mismatched_office, scope=_scope_a(), refresh=False
            )

        assert exc_info.value.code == "TENANT_ISOLATION_VIOLATION"

    async def test_upsert_scoped_to_correct_tenant(
        self, materializer: BriefMaterializer
    ) -> None:
        """The upsert payload must contain the correct tenant_id / suite_id."""
        built_row = _cache_row(scope=_scope_b(), freshness_seq=1)

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(return_value=[]),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=built_row),
        ) as mock_upsert, patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
        ):
            await materializer.build_service_brief(
                OFFICE_B, scope=_scope_b(), refresh=False
            )

        payload = mock_upsert.call_args[0][1]
        assert payload["tenant_id"] == str(TENANT_B)
        assert payload["suite_id"] == str(SUITE_B)
        assert payload["office_id"] == str(OFFICE_B)


# ---------------------------------------------------------------------------
# 6. DB error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestErrorHandling:
    async def test_upsert_failure_raises_memory_service_error(
        self, materializer: BriefMaterializer
    ) -> None:
        """SupabaseClientError on upsert → MemoryServiceError with DB_UPSERT_FAILED."""
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(return_value=[]),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(side_effect=SupabaseClientError("DB down", detail="DB down")),
        ), patch("aspire_orchestrator.services.receipt_store.store_receipts"):
            with pytest.raises(MemoryServiceError) as exc_info:
                await materializer.build_service_brief(
                    OFFICE_A, scope=_scope_a(), refresh=False
                )

        assert exc_info.value.code == "DB_UPSERT_FAILED"

    async def test_active_threads_db_error_defaults_to_zero(
        self, materializer: BriefMaterializer
    ) -> None:
        """If threads table query fails, active_threads_count defaults to 0."""
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        built_row = _cache_row(freshness_seq=1, threads=0)

        def _select_side_effect(table: str, *args: Any, **kwargs: Any) -> list[dict]:
            if table == "service_brief_cache":
                return []
            if table == "threads":
                raise SupabaseClientError("threads unavailable", detail="threads unavailable")
            return []

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(side_effect=_select_side_effect),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=built_row),
        ), patch("aspire_orchestrator.services.receipt_store.store_receipts"):
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        # build must succeed; threads count must degrade gracefully
        assert isinstance(result, ServiceBriefOut)
        assert result.active_threads_count == 0

    async def test_memory_fetch_error_returns_empty_list(
        self, materializer: BriefMaterializer
    ) -> None:
        """If memory_objects query fails, service helpers return [] (graceful degrade)."""
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        built_row = _cache_row(freshness_seq=1)

        def _select_side_effect(table: str, *args: Any, **kwargs: Any) -> list[dict]:
            if table == "service_brief_cache":
                return []
            if table == "memory_objects":
                raise SupabaseClientError("memory unavailable", detail="memory unavailable")
            return []

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(side_effect=_select_side_effect),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=built_row),
        ), patch("aspire_orchestrator.services.receipt_store.store_receipts"):
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        assert isinstance(result, ServiceBriefOut)
        # Counts must gracefully default to 0 on fetch failure
        assert result.recent_picks_count == 0
        assert result.recent_overrides_count == 0


# ---------------------------------------------------------------------------
# 7. Concurrency — duplicate upserts use ON CONFLICT key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestConcurrency:
    async def test_on_conflict_key_in_upsert_call(
        self, materializer: BriefMaterializer
    ) -> None:
        """supabase_upsert must be called with on_conflict=tenant_id,suite_id,office_id."""
        built_row = _cache_row(freshness_seq=1)

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(return_value=[]),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=built_row),
        ) as mock_upsert, patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
        ):
            await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        _, kwargs = mock_upsert.call_args
        assert kwargs.get("on_conflict") == "tenant_id,suite_id,office_id"


# ---------------------------------------------------------------------------
# 8. Brief text (Law #9 — counts only, no content)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBriefText:
    async def test_brief_text_contains_counts_only(
        self, materializer: BriefMaterializer
    ) -> None:
        """brief_text must be a deterministic count-only string (no memory summaries)."""
        built_row = _cache_row(freshness_seq=1, picks=2, overrides=1)
        built_row["brief_text"] = (
            "Service brief: 2 recent picks, 1 overrides, "
            "0 open pending intents, 0 recent handoffs, "
            "0 active threads, 0 open candidates "
            "(0 due now, 0 overdue), 0 pending approvals."
        )

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(return_value=[]),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=built_row),
        ), patch("aspire_orchestrator.services.receipt_store.store_receipts"):
            result = await materializer.build_service_brief(
                OFFICE_A, scope=_scope_a(), refresh=False
            )

        assert result.brief_text is not None
        # Verify the rendered text is count-based
        assert "Service brief:" in result.brief_text
        assert "picks" in result.brief_text
        assert "overrides" in result.brief_text


# ---------------------------------------------------------------------------
# 9. Schema — ServiceBriefOut Pydantic model
# ---------------------------------------------------------------------------


class TestServiceBriefOutSchema:
    def test_defaults_are_zero(self) -> None:
        """All integer counters default to zero."""
        out = ServiceBriefOut(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            last_built_at=NOW,
        )
        assert out.due_now_count == 0
        assert out.overdue_count == 0
        assert out.pending_approval_count == 0
        assert out.recent_receipts_count == 0
        assert out.recent_picks_count == 0
        assert out.recent_overrides_count == 0
        assert out.open_pending_intents_count == 0
        assert out.recent_handoffs_count == 0
        assert out.active_threads_count == 0
        assert out.freshness_seq == 0

    def test_counts_populated(self) -> None:
        """ServiceBriefOut correctly holds service-specific counter fields."""
        out = ServiceBriefOut(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            last_built_at=NOW,
            recent_picks_count=5,
            recent_overrides_count=3,
            open_pending_intents_count=7,
            recent_handoffs_count=2,
            active_threads_count=10,
        )
        assert out.recent_picks_count == 5
        assert out.recent_overrides_count == 3
        assert out.open_pending_intents_count == 7
        assert out.recent_handoffs_count == 2
        assert out.active_threads_count == 10
