"""Tests for ProactiveCandidateEngine.

Mocks supabase_* helpers and store_receipts. No live Supabase required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.schemas.memory_v1 import (
    CandidateQuery,
    ProactiveCandidateIn,
    ScopedIdentity,
)
from aspire_orchestrator.services.memory_service import MemoryServiceError
from aspire_orchestrator.services.proactive_candidate_engine import (
    ProactiveCandidateEngine,
)

TENANT_A = uuid.uuid4()
SUITE_A = uuid.uuid4()
OFFICE_A = uuid.uuid4()
ENTITY_ID = uuid.uuid4()
NOW = datetime.now(tz=timezone.utc)


def _scope() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)


def _candidate_in(**overrides) -> ProactiveCandidateIn:
    base = dict(
        tenant_id=TENANT_A,
        suite_id=SUITE_A,
        office_id=OFFICE_A,
        owner_agent="ava",
        entity_type="quote",
        entity_id=ENTITY_ID,
        recommended_action="create_draft",
        action_class="draft",
        why_now="Customer hasn't responded in 5 days.",
        confidence=0.85,
        risk_tier="yellow",
        needs_approval=True,
        receipt_required=True,
    )
    base.update(overrides)
    return ProactiveCandidateIn(**base)


def _existing_row(**overrides) -> dict:
    base = {
        "candidate_id": str(uuid.uuid4()),
        "schema_version": "v1",
        "tenant_id": str(TENANT_A),
        "suite_id": str(SUITE_A),
        "office_id": str(OFFICE_A),
        "owner_agent": "ava",
        "source_event_ids": [],
        "source_memory_ids": [],
        "entity_type": "quote",
        "entity_id": str(ENTITY_ID),
        "thread_id": None,
        "recommended_action": "create_draft",
        "action_class": "draft",
        "why_now": "old reason",
        "confidence": 0.7,
        "risk_tier": "yellow",
        "needs_approval": True,
        "receipt_required": True,
        "due_at": None,
        "cooldown_until": None,
        "status": "open",
        "created_at": NOW.isoformat(),
        "last_activity_at": NOW.isoformat(),
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
class TestCreateCandidate:
    async def test_returns_existing_active_row_no_duplicate_insert(self) -> None:
        engine = ProactiveCandidateEngine()
        existing = _existing_row()
        with patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
            new=AsyncMock(return_value=[existing]),
        ) as mock_select, patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_insert",
            new=AsyncMock(),
        ) as mock_insert, patch(
            "aspire_orchestrator.services.proactive_candidate_engine.store_receipts",
            new=MagicMock(return_value=None),
        ):
            result = await engine.create_candidate(_candidate_in(), scope=_scope())
            assert str(result.candidate_id) == existing["candidate_id"]
            mock_select.assert_awaited()
            mock_insert.assert_not_awaited()  # dedup => no second insert

    async def test_honors_cooldown_returns_existing(self) -> None:
        engine = ProactiveCandidateEngine()
        cooldown_row = _existing_row(
            status="dismissed",
            cooldown_until=(NOW + timedelta(hours=4)).isoformat(),
        )
        with patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
            new=AsyncMock(return_value=[cooldown_row]),
        ), patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_insert",
            new=AsyncMock(),
        ) as mock_insert, patch(
            "aspire_orchestrator.services.proactive_candidate_engine.store_receipts",
            new=MagicMock(return_value=None),
        ):
            result = await engine.create_candidate(_candidate_in(), scope=_scope())
            assert str(result.candidate_id) == cooldown_row["candidate_id"]
            mock_insert.assert_not_awaited()


@pytest.mark.asyncio
class TestTransition:
    async def test_open_to_approved_succeeds(self) -> None:
        engine = ProactiveCandidateEngine()
        row = _existing_row(status="open")
        approved_row = {**row, "status": "approved"}
        with patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
            new=AsyncMock(return_value=[row]),
        ), patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_update",
            new=AsyncMock(return_value=approved_row),
        ), patch(
            "aspire_orchestrator.services.proactive_candidate_engine.store_receipts",
            new=MagicMock(return_value=None),
        ) as mock_receipt:
            result = await engine.transition(
                uuid.UUID(row["candidate_id"]),
                "approved",
                scope=_scope(),
            )
            assert result.status == "approved"
            mock_receipt.assert_called()

    async def test_invalid_transition_raises(self) -> None:
        engine = ProactiveCandidateEngine()
        row = _existing_row(status="executed")
        with patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
            new=AsyncMock(return_value=[row]),
        ), patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_update",
            new=AsyncMock(),
        ), patch(
            "aspire_orchestrator.services.proactive_candidate_engine.store_receipts",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(MemoryServiceError, match="INVALID_STATE_TRANSITION"):
                await engine.transition(
                    uuid.UUID(row["candidate_id"]),
                    "open",
                    scope=_scope(),
                )


@pytest.mark.asyncio
class TestQuery:
    async def test_query_filters_by_owner_and_status(self) -> None:
        engine = ProactiveCandidateEngine()
        rows = [_existing_row(), _existing_row()]
        with patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
            new=AsyncMock(return_value=rows),
        ) as mock_select:
            results = await engine.query(
                CandidateQuery(
                    tenant_id=TENANT_A,
                    suite_id=SUITE_A,
                    office_id=OFFICE_A,
                    owner_agent=["ava"],
                    status=["open", "snoozed"],
                    limit=10,
                ),
                scope=_scope(),
            )
            assert len(results) == 2
            mock_select.assert_awaited_once()
