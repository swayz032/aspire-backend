"""Receipt Coverage Audit — Pass 11.

Asserts that every state-changing method in the Memory spine calls
store_receipts exactly once on the success path.

Methods covered:
  MemoryService.write              → 'memory_write' receipt
  MemoryService.update_status      → 'memory_status_change' receipt
  MemoryService.mark_superseded    → 'memory_status_change' receipt
  ProactiveCandidateEngine.create_candidate → 'proactive_candidate_created' receipt
  ProactiveCandidateEngine.transition       → 'proactive_candidate_transition' receipt

Law #2: Every state change produces an immutable, append-only receipt. 100% coverage.

Pattern: mock store_receipts, call the method, assert_called_once.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call
from uuid import UUID

import pytest

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    Provenance,
    ProactiveCandidateIn,
    ScopedIdentity,
)
from aspire_orchestrator.services.memory_service import MemoryService
from aspire_orchestrator.services.proactive_candidate_engine import ProactiveCandidateEngine

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

TENANT = UUID("cc330000-0000-0000-0000-000000000001")
SUITE = UUID("cc330000-0000-0000-0000-000000000002")
OFFICE = UUID("cc330000-0000-0000-0000-000000000003")

NOW = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)


def _scope() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE)


def _prov() -> Provenance:
    return Provenance(
        source_surface="ava_voice",
        source_agent="ava",
        runtime_family="elevenlabs",
        channel="voice",
        trace_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
    )


def _memory_row(memory_id: UUID, memory_type: str = "session_summary", status: str | None = None) -> dict:
    return {
        "memory_id": str(memory_id),
        "tenant_id": str(TENANT),
        "suite_id": str(SUITE),
        "office_id": str(OFFICE),
        "trace_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "memory_type": memory_type,
        "summary": "Audit test stub.",
        "status": status,
        "created_at": NOW.isoformat(),
        "last_activity_at": NOW.isoformat(),
    }


def _candidate_row(candidate_id: UUID, status: str = "open") -> dict:
    return {
        "candidate_id": str(candidate_id),
        "schema_version": "v1",
        "tenant_id": str(TENANT),
        "suite_id": str(SUITE),
        "office_id": str(OFFICE),
        "owner_agent": "ava",
        "recommended_action": "route_to_agent",
        "action_class": "approval_request",
        "why_now": "Audit test.",
        "confidence": 0.8,
        "risk_tier": "green",
        "needs_approval": False,
        "receipt_required": True,
        "status": status,
        "created_at": NOW.isoformat(),
        "last_activity_at": NOW.isoformat(),
    }


# ---------------------------------------------------------------------------
# MemoryService receipt coverage
# ---------------------------------------------------------------------------


class TestMemoryServiceReceiptCoverage:
    """Law #2: MemoryService.write/update_status/mark_superseded each call store_receipts once."""

    @pytest.mark.asyncio
    async def test_write_calls_store_receipts_exactly_once(self) -> None:
        """MemoryService.write on success path → store_receipts called exactly once."""
        memory_id = uuid.uuid4()
        scope = _scope()
        envelope = MemoryObjectIn(
            scope=scope,
            provenance=_prov(),
            memory_type="session_summary",
            summary="Audit receipt coverage test.",
            idempotency_key=f"audit-write-{uuid.uuid4()}",
        )

        with (
            patch(
                "aspire_orchestrator.services.memory_service.supabase_insert",
                new=AsyncMock(return_value=_memory_row(memory_id)),
            ),
            patch(
                "aspire_orchestrator.services.memory_service.store_receipts"
            ) as mock_store,
        ):
            mem_svc = MemoryService()
            await mem_svc.write(envelope, scope=scope, embed=False)

        mock_store.assert_called_once()
        stored_receipts = mock_store.call_args[0][0]
        assert len(stored_receipts) == 1
        assert stored_receipts[0]["action_type"] == "memory_write"

    @pytest.mark.asyncio
    async def test_write_idempotency_hit_does_not_call_store_receipts(self) -> None:
        """Idempotency dedup path: existing row returned → store_receipts NOT called."""
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        memory_id = uuid.uuid4()
        scope = _scope()
        existing_row = _memory_row(memory_id)

        envelope = MemoryObjectIn(
            scope=scope,
            provenance=_prov(),
            memory_type="session_summary",
            summary="Dedup test.",
            idempotency_key=f"audit-dedup-{uuid.uuid4()}",
        )

        with (
            patch(
                "aspire_orchestrator.services.memory_service.supabase_insert",
                new=AsyncMock(
                    side_effect=SupabaseClientError(
                        "insert", status_code=409, detail="unique violation 23505"
                    )
                ),
            ),
            patch(
                "aspire_orchestrator.services.memory_service.supabase_select",
                new=AsyncMock(return_value=[existing_row]),
            ),
            patch(
                "aspire_orchestrator.services.memory_service.store_receipts"
            ) as mock_store,
        ):
            mem_svc = MemoryService()
            out = await mem_svc.write(envelope, scope=scope, embed=False)

        # Dedup → no receipt re-emission
        mock_store.assert_not_called()
        assert str(out.memory_id) == str(memory_id)

    @pytest.mark.asyncio
    async def test_update_status_calls_store_receipts_exactly_once(self) -> None:
        """MemoryService.update_status on success path → store_receipts called once."""
        memory_id = uuid.uuid4()
        scope = _scope()
        drafted_row = _memory_row(memory_id, status="drafted")
        approved_row = _memory_row(memory_id, status="approved")

        with (
            patch(
                "aspire_orchestrator.services.memory_service.supabase_select",
                new=AsyncMock(return_value=[drafted_row]),
            ),
            patch(
                "aspire_orchestrator.services.memory_service.supabase_update",
                new=AsyncMock(return_value=approved_row),
            ),
            patch(
                "aspire_orchestrator.services.memory_service.store_receipts"
            ) as mock_store,
        ):
            mem_svc = MemoryService()
            await mem_svc.update_status(
                memory_id=memory_id,
                new_status="approved",
                scope=scope,
            )

        mock_store.assert_called_once()
        stored_receipts = mock_store.call_args[0][0]
        assert stored_receipts[0]["action_type"] == "memory_status_change"

    @pytest.mark.asyncio
    async def test_mark_superseded_calls_store_receipts_exactly_once(self) -> None:
        """MemoryService.mark_superseded on success path → store_receipts called once."""
        memory_id = uuid.uuid4()
        by_id = uuid.uuid4()
        scope = _scope()

        drafted_row = _memory_row(memory_id, status="drafted")
        superseded_row = _memory_row(memory_id, status="superseded")

        with (
            patch(
                "aspire_orchestrator.services.memory_service.supabase_select",
                new=AsyncMock(return_value=[drafted_row]),
            ),
            patch(
                "aspire_orchestrator.services.memory_service.supabase_update",
                new=AsyncMock(return_value=superseded_row),
            ),
            patch(
                "aspire_orchestrator.services.memory_service.store_receipts"
            ) as mock_store,
        ):
            mem_svc = MemoryService()
            await mem_svc.mark_superseded(
                memory_id=memory_id,
                by_id=by_id,
                scope=scope,
            )

        mock_store.assert_called_once()
        stored_receipts = mock_store.call_args[0][0]
        assert stored_receipts[0]["action_type"] == "memory_status_change"


# ---------------------------------------------------------------------------
# ProactiveCandidateEngine receipt coverage
# ---------------------------------------------------------------------------


class TestCandidateEngineReceiptCoverage:
    """Law #2: ProactiveCandidateEngine.create_candidate / transition each emit one receipt."""

    @pytest.mark.asyncio
    async def test_create_candidate_calls_store_receipts_exactly_once(self) -> None:
        """create_candidate success → store_receipts called exactly once."""
        candidate_id = uuid.uuid4()
        scope = _scope()

        candidate_in = ProactiveCandidateIn(
            tenant_id=TENANT,
            suite_id=SUITE,
            office_id=OFFICE,
            owner_agent="ava",
            recommended_action="route_to_agent",
            action_class="approval_request",
            why_now="Coverage audit.",
            confidence=0.8,
            risk_tier="green",
            needs_approval=False,
            receipt_required=True,
        )

        with (
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
                new=AsyncMock(return_value=[]),  # no dedup match
            ),
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.supabase_insert",
                new=AsyncMock(return_value=_candidate_row(candidate_id)),
            ),
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.store_receipts"
            ) as mock_store,
        ):
            engine = ProactiveCandidateEngine()
            out = await engine.create_candidate(candidate_in, scope=scope)

        mock_store.assert_called_once()
        stored_receipts = mock_store.call_args[0][0]
        assert stored_receipts[0]["action_type"] == "proactive_candidate_created"

    @pytest.mark.asyncio
    async def test_create_candidate_dedup_hit_does_not_call_store_receipts(self) -> None:
        """Dedup hit (existing active candidate) → store_receipts NOT called."""
        candidate_id = uuid.uuid4()
        scope = _scope()

        candidate_in = ProactiveCandidateIn(
            tenant_id=TENANT,
            suite_id=SUITE,
            office_id=OFFICE,
            owner_agent="ava",
            recommended_action="route_to_agent",
            action_class="approval_request",
            why_now="Coverage audit dedup.",
            confidence=0.8,
            risk_tier="green",
            needs_approval=False,
            receipt_required=True,
        )

        with (
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
                new=AsyncMock(return_value=[_candidate_row(candidate_id)]),  # existing match
            ),
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.store_receipts"
            ) as mock_store,
        ):
            engine = ProactiveCandidateEngine()
            out = await engine.create_candidate(candidate_in, scope=scope)

        # Dedup → no receipt
        mock_store.assert_not_called()
        assert str(out.candidate_id) == str(candidate_id)

    @pytest.mark.asyncio
    async def test_transition_calls_store_receipts_exactly_once(self) -> None:
        """transition success → store_receipts called exactly once."""
        candidate_id = uuid.uuid4()
        scope = _scope()

        open_row = _candidate_row(candidate_id, status="open")
        approved_row = _candidate_row(candidate_id, status="approved")

        with (
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.supabase_select",
                new=AsyncMock(return_value=[open_row]),
            ),
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.supabase_update",
                new=AsyncMock(return_value=approved_row),
            ),
            patch(
                "aspire_orchestrator.services.proactive_candidate_engine.store_receipts"
            ) as mock_store,
        ):
            engine = ProactiveCandidateEngine()
            await engine.transition(
                candidate_id=candidate_id,
                new_status="approved",
                scope=scope,
                reason="Approved via audit test",
            )

        mock_store.assert_called_once()
        stored_receipts = mock_store.call_args[0][0]
        assert stored_receipts[0]["action_type"] == "proactive_candidate_transition"


# ---------------------------------------------------------------------------
# Brief Materializer — no direct receipt emission (by design; verified here)
# ---------------------------------------------------------------------------


class TestBriefMaterializerNoDirectReceipt:
    """Brief Materializer is read-only derivation — does NOT emit receipts directly.

    Plan §4 explicitly documents this design decision: brief refreshes are read-only
    projections of already-receipted source tables. Re-emitting on every refresh
    would multiply receipt volume without audit value.

    This test verifies the design intention: build_office_brief does NOT call store_receipts.
    """

    @pytest.mark.asyncio
    async def test_build_office_brief_does_not_emit_receipt(self) -> None:
        """build_office_brief is read-only — must NOT call store_receipts."""
        from aspire_orchestrator.services.brief_materializer import BriefMaterializer

        scope = _scope()
        office_id = OFFICE

        # Simulate DB returning a stale cache (triggers recompute)
        stale_cache = {
            "tenant_id": str(TENANT),
            "suite_id": str(SUITE),
            "office_id": str(OFFICE),
            "brief_text": "Stale brief",
            "brief_json": {},
            "freshness_seq": 1,
            "last_built_at": "2000-01-01T00:00:00+00:00",  # very stale
        }

        memory_rows: list[dict] = []
        candidate_rows: list[dict] = []

        async def mock_select(table: str, filter_str: str, **kwargs) -> list[dict]:
            if "brief_cache" in table:
                return [stale_cache]
            if "memory_objects" in table:
                return memory_rows
            if "proactive_candidates" in table:
                return candidate_rows
            return []

        async def mock_upsert(table: str, row: dict, conflict_target: str = "") -> dict:
            return {**row, "freshness_seq": 2, "last_built_at": NOW.isoformat()}

        with (
            patch(
                "aspire_orchestrator.services.brief_materializer.supabase_select",
                new=mock_select,
            ),
            patch(
                "aspire_orchestrator.services.brief_materializer.supabase_upsert",
                new=mock_upsert,
            ),
            patch(
                "aspire_orchestrator.services.brief_materializer.store_receipts"
            ) as mock_receipt_store,
        ):
            materializer = BriefMaterializer()
            await materializer.build_office_brief(office_id=office_id, scope=scope)

        # Brief materializer must NOT emit receipts (read-only derivation)
        mock_receipt_store.assert_not_called()
