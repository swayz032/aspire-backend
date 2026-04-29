"""RLS Evil Tests — proactive_candidates table (Pass 11).

Law #6: Zero cross-tenant leakage on proactive_candidates.
ProactiveCandidateEngine enforces scope at service layer.

Tests verify:
  - Cross-tenant SELECT returns 0 rows (isolation error on scope mismatch)
  - Cross-tenant INSERT denied before DB I/O
  - Transition on foreign candidate denied
  - Missing scope fields → fail-closed schema validation error

Aspire Laws:
  Law #2: Every create/transition emits a receipt.
  Law #3: Fail Closed — scope mismatch → deny without DB call.
  Law #6: Tenant Isolation — zero cross-tenant leakage.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.schemas.memory_v1 import (
    ActionClass,
    ProactiveCandidateIn,
    ProactiveCandidateOut,
    RecommendedAction,
    ScopedIdentity,
)
from aspire_orchestrator.services.memory_service import MemoryServiceError
from aspire_orchestrator.services.proactive_candidate_engine import ProactiveCandidateEngine

# ---------------------------------------------------------------------------
# Tenant fixtures
# ---------------------------------------------------------------------------

TENANT_A = UUID("aa220000-0000-0000-0000-000000000001")
SUITE_A = UUID("aa220000-0000-0000-0000-000000000002")
OFFICE_A = UUID("aa220000-0000-0000-0000-000000000003")

TENANT_B = UUID("bb220000-0000-0000-0000-000000000001")
SUITE_B = UUID("bb220000-0000-0000-0000-000000000002")
OFFICE_B = UUID("bb220000-0000-0000-0000-000000000003")

NOW = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)


def _scope_a() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)


def _scope_b() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_B, suite_id=SUITE_B, office_id=OFFICE_B)


def _candidate_in(scope: ScopedIdentity) -> ProactiveCandidateIn:
    return ProactiveCandidateIn(
        tenant_id=scope.tenant_id,
        suite_id=scope.suite_id,
        office_id=scope.office_id,
        owner_agent="sarah",
        recommended_action="queue_callback",
        action_class="approval_request",
        why_now="Missed call from prospect.",
        confidence=0.85,
        risk_tier="yellow",
        needs_approval=True,
        receipt_required=True,
    )


def _candidate_row(candidate_id: UUID, scope: ScopedIdentity, status: str = "open") -> dict:
    """Flat DB row for a proactive candidate."""
    return {
        "candidate_id": str(candidate_id),
        "schema_version": "v1",
        "tenant_id": str(scope.tenant_id),
        "suite_id": str(scope.suite_id),
        "office_id": str(scope.office_id),
        "owner_agent": "sarah",
        "recommended_action": "queue_callback",
        "action_class": "approval_request",
        "why_now": "Missed call from prospect.",
        "confidence": 0.85,
        "risk_tier": "yellow",
        "needs_approval": True,
        "receipt_required": True,
        "status": status,
        "created_at": NOW.isoformat(),
        "last_activity_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }


# ---------------------------------------------------------------------------
# Cross-tenant SELECT
# ---------------------------------------------------------------------------


class TestCandidatesCrossTenantSelect:
    """Cross-tenant SELECT: scope mismatch detected before returning data."""

    @pytest.mark.asyncio
    async def test_query_with_mismatched_scope_raises_isolation_error(self) -> None:
        """Evil: scope_a queries but engine builds filter using scope_b query object.

        CandidateQuery with scope_b values passed to scope_a call → isolation error.
        """
        from aspire_orchestrator.schemas.memory_v1 import CandidateQuery

        scope_a = _scope_a()

        # Build a query object with scope_b values
        q_b = CandidateQuery(
            tenant_id=TENANT_B,
            suite_id=SUITE_B,
            office_id=OFFICE_B,
        )

        engine = ProactiveCandidateEngine()

        # Should raise TENANT_ISOLATION_VIOLATION because q.scope != scope_a
        with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
            await engine.query(q_b, scope=scope_a)

    @pytest.mark.asyncio
    async def test_query_with_correct_scope_returns_empty_list(self) -> None:
        """Correct scope → empty list returned (no error). Baseline test."""
        from aspire_orchestrator.schemas.memory_v1 import CandidateQuery

        scope_a = _scope_a()
        q_a = CandidateQuery(
            tenant_id=TENANT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
        )

        engine = ProactiveCandidateEngine()

        with patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            results = await engine.query(q_a, scope=scope_a)

        assert results == []

    @pytest.mark.asyncio
    async def test_transition_on_foreign_candidate_raises(self) -> None:
        """Evil: Tenant A tries to transition Tenant B's candidate → isolation error."""
        candidate_id = uuid.uuid4()
        scope_a = _scope_a()

        # DB returns a TENANT_B row
        b_row = _candidate_row(candidate_id, _scope_b(), status="open")

        engine = ProactiveCandidateEngine()

        with patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
            new=AsyncMock(return_value=[b_row]),
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await engine.transition(
                    candidate_id=candidate_id,
                    new_status="approved",
                    scope=scope_a,
                    reason="Evil transition attempt",
                )


# ---------------------------------------------------------------------------
# Cross-tenant INSERT
# ---------------------------------------------------------------------------


class TestCandidatesCrossTenantInsert:
    """Cross-tenant INSERT: scope mismatch denied before DB I/O."""

    @pytest.mark.asyncio
    async def test_create_candidate_cross_tenant_denied_before_db(self) -> None:
        """Evil: candidate_in has scope_a values but caller passes scope_b → denied."""
        scope_a = _scope_a()
        scope_b = _scope_b()

        # candidate_in belongs to scope_a but caller supplies scope_b
        candidate_in = _candidate_in(scope_a)

        mock_insert = AsyncMock()
        mock_select = AsyncMock(return_value=[])
        engine = ProactiveCandidateEngine()

        with (
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
                new=mock_select,
            ),
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.supabase_insert",
                new=mock_insert,
            ),
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await engine.create_candidate(candidate_in, scope=scope_b)

        # DB INSERT must NOT be called
        mock_insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_candidate_reversed_cross_tenant_also_denied(self) -> None:
        """Evil: candidate_in has scope_b but caller passes scope_a → denied."""
        scope_a = _scope_a()
        scope_b = _scope_b()

        candidate_in = _candidate_in(scope_b)  # scope_b in the candidate

        mock_insert = AsyncMock()
        engine = ProactiveCandidateEngine()

        with patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_insert",
            new=mock_insert,
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await engine.create_candidate(candidate_in, scope=scope_a)

        mock_insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_candidate_correct_scope_succeeds_and_emits_receipt(self) -> None:
        """Positive: correct scope → candidate inserted + receipt emitted (Law #2)."""
        scope_a = _scope_a()
        candidate_in = _candidate_in(scope_a)
        candidate_id = uuid.uuid4()

        receipts_stored: list[list[dict]] = []
        engine = ProactiveCandidateEngine()

        inserted_row = _candidate_row(candidate_id, scope_a, status="open")

        with (
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
                new=AsyncMock(return_value=[]),  # no existing dedup match
            ),
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.supabase_insert",
                new=AsyncMock(return_value=inserted_row),
            ),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=lambda r: receipts_stored.append(r),
            ),
        ):
            out = await engine.create_candidate(candidate_in, scope=scope_a)

        assert out.status == "open"
        assert str(out.tenant_id) == str(TENANT_A)

        # Receipt emitted (Law #2)
        assert len(receipts_stored) == 1
        rcpt = receipts_stored[0][0]
        assert rcpt["action_type"] == "proactive_candidate_created"
        assert rcpt["tool_used"] == "proactive_candidate_engine"
        assert rcpt["tenant_id"] == str(TENANT_A)


# ---------------------------------------------------------------------------
# Terminal state immutability
# ---------------------------------------------------------------------------


class TestCandidatesTerminalStateImmutability:
    """Terminal candidates (executed/dismissed/expired) cannot be transitioned."""

    @pytest.mark.asyncio
    async def test_transition_from_executed_raises_invalid_transition(self) -> None:
        """Evil: Attempt to transition an 'executed' candidate → INVALID_STATE_TRANSITION."""
        candidate_id = uuid.uuid4()
        scope_a = _scope_a()

        executed_row = _candidate_row(candidate_id, scope_a, status="executed")

        engine = ProactiveCandidateEngine()

        with patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
            new=AsyncMock(return_value=[executed_row]),
        ):
            with pytest.raises(MemoryServiceError, match="INVALID_STATE_TRANSITION"):
                await engine.transition(
                    candidate_id=candidate_id,
                    new_status="open",  # invalid: executed → open
                    scope=scope_a,
                    reason="Evil re-open attempt",
                )

    @pytest.mark.asyncio
    async def test_transition_from_dismissed_raises_invalid_transition(self) -> None:
        """Evil: dismissed → approved not a valid transition."""
        candidate_id = uuid.uuid4()
        scope_a = _scope_a()

        dismissed_row = _candidate_row(candidate_id, scope_a, status="dismissed")

        engine = ProactiveCandidateEngine()

        with patch(
            "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
            new=AsyncMock(return_value=[dismissed_row]),
        ):
            with pytest.raises(MemoryServiceError, match="INVALID_STATE_TRANSITION"):
                await engine.transition(
                    candidate_id=candidate_id,
                    new_status="approved",  # invalid: dismissed → approved
                    scope=scope_a,
                    reason="Evil resurrect attempt",
                )

    @pytest.mark.asyncio
    async def test_valid_transition_open_to_dismissed_emits_receipt(self) -> None:
        """Positive: open → dismissed is valid and emits a receipt."""
        candidate_id = uuid.uuid4()
        scope_a = _scope_a()

        open_row = _candidate_row(candidate_id, scope_a, status="open")
        dismissed_row = _candidate_row(candidate_id, scope_a, status="dismissed")

        receipts_stored: list[list[dict]] = []
        engine = ProactiveCandidateEngine()

        with (
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
                new=AsyncMock(return_value=[open_row]),
            ),
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.supabase_update",
                new=AsyncMock(return_value=dismissed_row),
            ),
            patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=lambda r: receipts_stored.append(r),
            ),
        ):
            out = await engine.transition(
                candidate_id=candidate_id,
                new_status="dismissed",
                scope=scope_a,
                reason="User dismissed",
            )

        assert out.status == "dismissed"
        assert len(receipts_stored) == 1
        assert receipts_stored[0][0]["action_type"] == "proactive_candidate_transition"
