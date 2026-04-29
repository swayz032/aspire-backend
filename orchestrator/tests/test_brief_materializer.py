"""Tests for BriefMaterializer."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services.brief_materializer import BriefMaterializer

TENANT_A = uuid.uuid4()
SUITE_A = uuid.uuid4()
OFFICE_A = uuid.uuid4()
THREAD_A = uuid.uuid4()
NOW = datetime.now(tz=timezone.utc)


def _scope() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)


def _office_cache_row(last_built: datetime, freshness_seq: int = 1) -> dict:
    return {
        "tenant_id": str(TENANT_A),
        "suite_id": str(SUITE_A),
        "office_id": str(OFFICE_A),
        "brief_text": "cached brief",
        "brief_json": {"summary": "cached"},
        "last_built_at": last_built.isoformat(),
        "freshness_seq": freshness_seq,
        "due_now_count": 0,
        "overdue_count": 0,
        "pending_approval_count": 0,
        "recent_receipts_count": 0,
    }


def _patch_module(*paths):
    """Helper to build a stack of patches against brief_materializer module."""
    return [
        patch(
            f"aspire_orchestrator.services.brief_materializer.{p}",
            new=AsyncMock(),
        )
        for p in paths
    ]


@pytest.mark.asyncio
class TestOfficeBrief:
    async def test_returns_cached_when_fresh(self) -> None:
        materializer = BriefMaterializer()
        fresh_row = _office_cache_row(NOW - timedelta(seconds=10))
        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(return_value=[fresh_row]),
        ) as mock_select, patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(),
        ) as mock_upsert:
            result = await materializer.build_office_brief(
                OFFICE_A, scope=_scope(), refresh=False
            )
            assert result.brief_text == "cached brief"
            mock_upsert.assert_not_awaited()  # cache hit => no rebuild
            assert mock_select.await_count >= 1

    async def test_rebuilds_when_refresh_true(self) -> None:
        materializer = BriefMaterializer()
        stale_row = _office_cache_row(NOW - timedelta(minutes=5), freshness_seq=3)
        rebuilt_row = _office_cache_row(NOW, freshness_seq=4)

        # supabase_select is called multiple times: cache check, recent memory,
        # candidates, approvals. Return stale_row for the first call, then [].
        select_returns = [
            [stale_row],  # cache row (stale)
            [],           # recent memory_objects
            [],           # open proactive_candidates
            [],           # pending approval_links
            [],           # recent receipts
            [rebuilt_row],  # final cache read after upsert
        ]

        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(side_effect=select_returns),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(return_value=rebuilt_row),
        ) as mock_upsert:
            result = await materializer.build_office_brief(
                OFFICE_A, scope=_scope(), refresh=True
            )
            mock_upsert.assert_awaited()
            assert result.freshness_seq >= stale_row["freshness_seq"]


@pytest.mark.asyncio
class TestThreadBrief:
    async def test_thread_brief_returns_typed_model(self) -> None:
        materializer = BriefMaterializer()
        cached = {
            "thread_id": str(THREAD_A),
            "tenant_id": str(TENANT_A),
            "suite_id": str(SUITE_A),
            "summary": "Customer agreed to revised scope.",
            "last_promise": "Updated finishes by Friday.",
            "pending_blockers": [],
            "latest_receipt_id": None,
            "next_best_action": {"text": "Send updated quote."},
            "last_built_at": (NOW - timedelta(seconds=5)).isoformat(),
            "freshness_seq": 7,
        }
        with patch(
            "aspire_orchestrator.services.brief_materializer.supabase_select",
            new=AsyncMock(return_value=[cached]),
        ), patch(
            "aspire_orchestrator.services.brief_materializer.supabase_upsert",
            new=AsyncMock(),
        ):
            result = await materializer.build_thread_brief(
                THREAD_A, scope=_scope(), refresh=False
            )
            assert result.summary == "Customer agreed to revised scope."
            assert result.last_promise == "Updated finishes by Friday."
