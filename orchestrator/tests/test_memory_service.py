"""Tests for MemoryService.

Mocks the supabase_client module-level functions and receipt_store.
No real Supabase connection required.

Covers:
- write returns MemoryObjectOut with memory_id populated.
- idempotency_key dedup returns existing row; no second receipt emitted.
- Cross-tenant scope mismatch on get raises MemoryServiceError.
- update_status from 'pending_approval' → 'approved' succeeds.
- update_status from 'executed' → anything raises MemoryServiceError.
- mark_superseded sets status='superseded' and links by_id in linked_artifact_ids.
- receipt emitted on every successful write.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    Provenance,
    ScopedIdentity,
)
from aspire_orchestrator.services.memory_service import (
    MemoryService,
    MemoryServiceError,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
SUITE_A = uuid.uuid4()
SUITE_B = uuid.uuid4()
OFFICE_A = uuid.uuid4()
TRACE = uuid.uuid4()
CORR = uuid.uuid4()
NOW_ISO = datetime.now(tz=timezone.utc).isoformat()
MEMORY_ID = uuid.uuid4()


def _scope_a() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)


def _scope_b() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT_B, suite_id=SUITE_B, office_id=OFFICE_A)


def _prov() -> Provenance:
    return Provenance(trace_id=TRACE, correlation_id=CORR)


def _envelope(**kwargs) -> MemoryObjectIn:
    defaults = dict(
        scope=_scope_a(),
        provenance=_prov(),
        memory_type="session_summary",
        summary="Test session ended.",
    )
    defaults.update(kwargs)
    return MemoryObjectIn(**defaults)


def _fake_db_row(
    *,
    memory_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    suite_id: uuid.UUID | None = None,
    office_id: uuid.UUID | None = None,
    status: str | None = None,
    idempotency_key: str | None = None,
    linked_artifact_ids: list | None = None,
) -> dict:
    """Build a minimal fake DB row that _row_to_memory_out can parse."""
    tid = tenant_id or TENANT_A
    sid = suite_id or SUITE_A
    oid = office_id or OFFICE_A
    mid = memory_id or MEMORY_ID
    return {
        "memory_id": str(mid),
        "tenant_id": str(tid),
        "suite_id": str(sid),
        "office_id": str(oid),
        "trace_id": str(TRACE),
        "correlation_id": str(CORR),
        "memory_type": "session_summary",
        "summary": "Test session ended.",
        "detail": {},
        "visibility_scope": "office",
        "status": status,
        "idempotency_key": idempotency_key,
        "linked_receipt_ids": [],
        "linked_approval_ids": [],
        "linked_artifact_ids": linked_artifact_ids or [],
        "linked_workflow_run_ids": [],
        "created_at": NOW_ISO,
        "last_activity_at": NOW_ISO,
    }


# ---------------------------------------------------------------------------
# write: success path
# ---------------------------------------------------------------------------


class TestMemoryServiceWrite:
    @pytest.mark.asyncio
    async def test_write_returns_memory_object_out_with_id(self) -> None:
        svc = MemoryService()
        fake_row = _fake_db_row()

        with (
            patch(
                "aspire_orchestrator.services.memory_service.supabase_insert",
                new_callable=AsyncMock,
                return_value=fake_row,
            ),
            patch(
                "aspire_orchestrator.services.memory_service.store_receipts",
            ) as mock_receipts,
        ):
            # embed=False: skip actual OpenAI call
            result = await svc.write(_envelope(), scope=_scope_a(), embed=False)

        assert result.memory_id == uuid.UUID(fake_row["memory_id"])
        assert result.memory_type == "session_summary"
        assert result.scope.tenant_id == TENANT_A

    @pytest.mark.asyncio
    async def test_write_emits_receipt(self) -> None:
        svc = MemoryService()
        fake_row = _fake_db_row()

        with (
            patch(
                "aspire_orchestrator.services.memory_service.supabase_insert",
                new_callable=AsyncMock,
                return_value=fake_row,
            ),
            patch(
                "aspire_orchestrator.services.memory_service.store_receipts",
            ) as mock_receipts,
        ):
            await svc.write(_envelope(), scope=_scope_a(), embed=False)

        mock_receipts.assert_called_once()
        call_args = mock_receipts.call_args[0][0]
        assert len(call_args) == 1
        receipt = call_args[0]
        assert receipt["receipt_type"] == "memory_write"
        assert receipt["outcome"] == "success"
        # PII check: summary must NOT appear in receipt
        assert "Test session ended" not in str(receipt)

    @pytest.mark.asyncio
    async def test_scope_mismatch_raises(self) -> None:
        """Calling write with scope_a envelope but scope_b context raises."""
        svc = MemoryService()
        env = _envelope(scope=_scope_a())

        with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
            await svc.write(env, scope=_scope_b(), embed=False)


# ---------------------------------------------------------------------------
# write: idempotency dedup
# ---------------------------------------------------------------------------


class TestMemoryServiceIdempotency:
    @pytest.mark.asyncio
    async def test_dedup_returns_existing_row_no_second_receipt(self) -> None:
        svc = MemoryService()
        idem_key = "test-idem-001"
        fake_existing = _fake_db_row(idempotency_key=idem_key)
        env = _envelope(idempotency_key=idem_key)

        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        conflict_error = SupabaseClientError(
            "insert/memory_objects", status_code=409, detail="23505 unique_violation"
        )

        with (
            patch(
                "aspire_orchestrator.services.memory_service.supabase_insert",
                new_callable=AsyncMock,
                side_effect=conflict_error,
            ),
            patch(
                "aspire_orchestrator.services.memory_service.supabase_select",
                new_callable=AsyncMock,
                return_value=[fake_existing],
            ),
            patch(
                "aspire_orchestrator.services.memory_service.store_receipts",
            ) as mock_receipts,
        ):
            result = await svc.write(env, scope=_scope_a(), embed=False)

        # Returns cached row
        assert result.idempotency_key == idem_key
        # No receipt emitted (idempotent replay)
        mock_receipts.assert_not_called()


# ---------------------------------------------------------------------------
# get: scope isolation
# ---------------------------------------------------------------------------


class TestMemoryServiceGet:
    @pytest.mark.asyncio
    async def test_get_cross_tenant_raises(self) -> None:
        """Row belongs to tenant A, caller presents scope B → MemoryServiceError."""
        svc = MemoryService()
        # Row has tenant_a scope
        fake_row = _fake_db_row(tenant_id=TENANT_A, suite_id=SUITE_A)

        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new_callable=AsyncMock,
            return_value=[fake_row],
        ):
            with pytest.raises(MemoryServiceError, match="TENANT_ISOLATION_VIOLATION"):
                await svc.get(MEMORY_ID, scope=_scope_b())

    @pytest.mark.asyncio
    async def test_get_returns_none_when_not_found(self) -> None:
        svc = MemoryService()
        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await svc.get(MEMORY_ID, scope=_scope_a())
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_object_when_found(self) -> None:
        svc = MemoryService()
        fake_row = _fake_db_row()
        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new_callable=AsyncMock,
            return_value=[fake_row],
        ):
            result = await svc.get(MEMORY_ID, scope=_scope_a())
        assert result is not None
        assert result.memory_id == uuid.UUID(fake_row["memory_id"])


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------


class TestMemoryServiceUpdateStatus:
    @pytest.mark.asyncio
    async def test_pending_approval_to_approved_succeeds(self) -> None:
        svc = MemoryService()
        current_row = _fake_db_row(status="pending_approval")
        updated_row = _fake_db_row(status="approved")

        with (
            patch(
                "aspire_orchestrator.services.memory_service.supabase_select",
                new_callable=AsyncMock,
                return_value=[current_row],
            ),
            patch(
                "aspire_orchestrator.services.memory_service.supabase_update",
                new_callable=AsyncMock,
                return_value=updated_row,
            ),
            patch(
                "aspire_orchestrator.services.memory_service.store_receipts",
            ) as mock_receipts,
        ):
            result = await svc.update_status(MEMORY_ID, "approved", scope=_scope_a())

        assert result.status == "approved"
        mock_receipts.assert_called_once()
        receipt = mock_receipts.call_args[0][0][0]
        assert receipt["receipt_type"] == "memory_status_change"

    @pytest.mark.asyncio
    async def test_cannot_set_executed_via_update_status(self) -> None:
        """Attempting to set 'executed' via update_status must raise immediately."""
        svc = MemoryService()
        with pytest.raises(MemoryServiceError, match="IMMUTABLE_STATE_TRANSITION"):
            await svc.update_status(MEMORY_ID, "executed", scope=_scope_a())

    @pytest.mark.asyncio
    async def test_transition_from_executed_raises(self) -> None:
        """Row already in 'executed' state cannot be updated (Law #2)."""
        svc = MemoryService()
        executed_row = _fake_db_row(status="executed")

        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new_callable=AsyncMock,
            return_value=[executed_row],
        ):
            with pytest.raises(MemoryServiceError, match="IMMUTABLE_STATE_TRANSITION"):
                await svc.update_status(MEMORY_ID, "superseded", scope=_scope_a())

    @pytest.mark.asyncio
    async def test_not_found_raises(self) -> None:
        svc = MemoryService()
        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new_callable=AsyncMock,
            return_value=[],
        ):
            with pytest.raises(MemoryServiceError, match="NOT_FOUND"):
                await svc.update_status(MEMORY_ID, "approved", scope=_scope_a())


# ---------------------------------------------------------------------------
# mark_superseded
# ---------------------------------------------------------------------------


class TestMemoryServiceMarkSuperseded:
    @pytest.mark.asyncio
    async def test_sets_superseded_status_and_links_by_id(self) -> None:
        svc = MemoryService()
        by_id = uuid.uuid4()
        current_row = _fake_db_row(status="approved")
        updated_row = _fake_db_row(
            status="superseded",
            linked_artifact_ids=[str(by_id)],
        )

        with (
            patch(
                "aspire_orchestrator.services.memory_service.supabase_select",
                new_callable=AsyncMock,
                return_value=[current_row],
            ),
            patch(
                "aspire_orchestrator.services.memory_service.supabase_update",
                new_callable=AsyncMock,
                return_value=updated_row,
            ),
            patch(
                "aspire_orchestrator.services.memory_service.store_receipts",
            ) as mock_receipts,
        ):
            result = await svc.mark_superseded(MEMORY_ID, by_id, scope=_scope_a())

        assert result.status == "superseded"
        assert uuid.UUID(str(by_id)) in result.linked_artifact_ids
        # Verify receipt content
        receipt = mock_receipts.call_args[0][0][0]
        assert "superseded_by_" in receipt["reason_code"]

    @pytest.mark.asyncio
    async def test_executed_row_cannot_be_superseded(self) -> None:
        svc = MemoryService()
        executed_row = _fake_db_row(status="executed")

        with patch(
            "aspire_orchestrator.services.memory_service.supabase_select",
            new_callable=AsyncMock,
            return_value=[executed_row],
        ):
            with pytest.raises(MemoryServiceError, match="IMMUTABLE_STATE_TRANSITION"):
                await svc.mark_superseded(MEMORY_ID, uuid.uuid4(), scope=_scope_a())

    @pytest.mark.asyncio
    async def test_superseded_preserves_existing_artifact_links(self) -> None:
        """mark_superseded must merge by_id with pre-existing linked_artifact_ids."""
        svc = MemoryService()
        by_id = uuid.uuid4()
        existing_artifact = uuid.uuid4()
        current_row = _fake_db_row(
            status="approved",
            linked_artifact_ids=[str(existing_artifact)],
        )
        updated_row = _fake_db_row(
            status="superseded",
            linked_artifact_ids=[str(existing_artifact), str(by_id)],
        )

        with (
            patch(
                "aspire_orchestrator.services.memory_service.supabase_select",
                new_callable=AsyncMock,
                return_value=[current_row],
            ),
            patch(
                "aspire_orchestrator.services.memory_service.supabase_update",
                new_callable=AsyncMock,
                return_value=updated_row,
            ) as mock_update,
            patch("aspire_orchestrator.services.memory_service.store_receipts"),
        ):
            await svc.mark_superseded(MEMORY_ID, by_id, scope=_scope_a())

        # Verify the patch payload contains both IDs
        patch_data = mock_update.call_args[0][2]
        assert str(by_id) in patch_data["linked_artifact_ids"]
        assert str(existing_artifact) in patch_data["linked_artifact_ids"]
