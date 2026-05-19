"""Tests for the all-5-stages orphan sweeper (P0-5 + P1-8).

Verifies:
- Sweeper marks classify/see/reason/procure stalled stages failed (not just ingest).
- Per-stage staleness thresholds are respected (5 min for classify, 8 see, 5 reason, 3 procure, 15 ingest).
- blueprint.pipeline.timed_out receipt is emitted with `stage=<the stalled stage>`.
- Existing ingest=in_progress > 15-min behavior is preserved (no regression).
- After marking failed, the orphan Storage object is deleted via storage_lifecycle.
"""

from __future__ import annotations

import os

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _row(stage: str, minutes_ago: int, *, project_id: str | None = None) -> dict:
    """Build a fake blueprint_projects row with the given stage in_progress."""
    pid = project_id or f"{stage}-proj-1234567890ab"
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "id": pid,
        "suite_id": "00000000-0000-0000-0000-000000000001",
        "office_id": "00000000-0000-0000-0000-000000000002",
        "upload_id": f"upload-{stage}-aaaa",
        "created_at": created.isoformat(),
        "stage_progress": {stage: "in_progress"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage,minutes_ago",
    [
        ("ingest", 16),    # threshold 15
        ("classify", 6),   # threshold 5
        ("see", 9),        # threshold 8
        ("reason", 6),     # threshold 5
        ("procure", 4),    # threshold 3
    ],
)
async def test_sweeper_marks_stalled_stage_failed(stage: str, minutes_ago: int) -> None:
    from aspire_orchestrator import server

    captured: dict = {"set_stage_calls": [], "receipts": [], "storage_deletes": []}

    async def fake_select(table, filters=None, **kw):
        assert table == "blueprint_projects"
        assert filters is not None and "or=(" in filters
        # All 5 stage filters must be present in the OR clause
        for s in ("ingest", "classify", "see", "reason", "procure"):
            assert f"stage_progress->>{s}.eq.in_progress" in filters
        return [_row(stage, minutes_ago)]

    async def fake_set_stage(*, project_id, stage, state, suite_id):
        captured["set_stage_calls"].append((project_id, stage, state, suite_id))

    def fake_store_receipts(rows):
        captured["receipts"].extend(rows)

    async def fake_delete(*, suite_id, upload_id, correlation_id):
        captured["storage_deletes"].append((suite_id, upload_id, correlation_id))
        return True

    with patch(
        "aspire_orchestrator.services.supabase_client.supabase_select",
        new=fake_select,
    ), patch(
        "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
        new=fake_set_stage,
    ), patch(
        "aspire_orchestrator.services.receipt_store.store_receipts",
        side_effect=fake_store_receipts,
    ), patch(
        "aspire_orchestrator.services.blueprint.storage_lifecycle.delete_orphaned_storage_object",
        new=fake_delete,
    ):
        await server._sweep_stale_blueprint_projects()

    # Exactly one stage marked failed, matching the stalled stage
    assert len(captured["set_stage_calls"]) == 1, captured["set_stage_calls"]
    _, marked_stage, state, _ = captured["set_stage_calls"][0]
    assert marked_stage == stage
    assert state == "failed"

    # timed_out receipt mentions the correct stage
    timed_out = [r for r in captured["receipts"] if r["receipt_type"] == "blueprint.pipeline.timed_out"]
    assert len(timed_out) == 1
    assert timed_out[0]["redacted_outputs"]["stage"] == stage

    # storage delete was called
    assert len(captured["storage_deletes"]) == 1

    # storage_orphan_deleted receipt emitted because delete returned True
    orphan_deleted = [r for r in captured["receipts"] if r["receipt_type"] == "blueprint.storage.orphan_deleted"]
    assert len(orphan_deleted) == 1
    assert orphan_deleted[0]["redacted_outputs"]["stage"] == stage


@pytest.mark.asyncio
async def test_sweeper_respects_grace_window() -> None:
    """A row 2 min old at classify (threshold 5) must NOT be swept."""
    from aspire_orchestrator import server

    captured: dict = {"set_stage_calls": [], "receipts": []}

    async def fake_select(table, filters=None, **kw):
        return [_row("classify", 2)]  # 2 min < 5 min threshold

    async def fake_set_stage(**kw):
        captured["set_stage_calls"].append(kw)

    def fake_store_receipts(rows):
        captured["receipts"].extend(rows)

    async def fake_delete(**kw):
        return True

    with patch(
        "aspire_orchestrator.services.supabase_client.supabase_select",
        new=fake_select,
    ), patch(
        "aspire_orchestrator.services.blueprint.stage_progress.set_stage_progress",
        new=fake_set_stage,
    ), patch(
        "aspire_orchestrator.services.receipt_store.store_receipts",
        side_effect=fake_store_receipts,
    ), patch(
        "aspire_orchestrator.services.blueprint.storage_lifecycle.delete_orphaned_storage_object",
        new=fake_delete,
    ):
        await server._sweep_stale_blueprint_projects()

    assert captured["set_stage_calls"] == []
    assert captured["receipts"] == []


@pytest.mark.asyncio
async def test_sweeper_handles_empty_db() -> None:
    from aspire_orchestrator import server

    async def fake_select(table, filters=None, **kw):
        return []

    with patch(
        "aspire_orchestrator.services.supabase_client.supabase_select",
        new=fake_select,
    ):
        # Should be a no-op and not raise.
        await server._sweep_stale_blueprint_projects()


@pytest.mark.asyncio
async def test_storage_orphan_sweep_deletes_blobs_without_db_row() -> None:
    """P1-8 second sweep: blobs older than 60 min with no DB row get deleted."""
    from aspire_orchestrator import server

    captured: dict = {"deletes": [], "receipts": []}

    fake_orphans = [
        {
            "suite_id": "00000000-0000-0000-0000-000000000001",
            "upload_id": "abandoned-upload-aaaaaa",
            "object_path": "00000000-0000-0000-0000-000000000001/abandoned-upload-aaaaaa/raw.pdf",
            "created_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "size_bytes": 1024,
        }
    ]

    async def fake_list_orphans(older_than_minutes=60):
        assert older_than_minutes == 60
        return list(fake_orphans)

    async def fake_delete(*, suite_id, upload_id, correlation_id):
        captured["deletes"].append((suite_id, upload_id))
        return True

    def fake_store_receipts(rows):
        captured["receipts"].extend(rows)

    with patch(
        "aspire_orchestrator.services.blueprint.storage_lifecycle.list_storage_orphans",
        new=fake_list_orphans,
    ), patch(
        "aspire_orchestrator.services.blueprint.storage_lifecycle.delete_orphaned_storage_object",
        new=fake_delete,
    ), patch(
        "aspire_orchestrator.services.receipt_store.store_receipts",
        side_effect=fake_store_receipts,
    ):
        await server._sweep_storage_orphans()

    assert len(captured["deletes"]) == 1
    assert captured["deletes"][0][1] == "abandoned-upload-aaaaaa"
    orphan_receipts = [r for r in captured["receipts"] if r["receipt_type"] == "blueprint.storage.orphan_deleted"]
    assert len(orphan_receipts) == 1
    assert orphan_receipts[0]["redacted_outputs"]["trigger"] == "no_db_row"


@pytest.mark.asyncio
async def test_delete_orphaned_storage_object_handles_404_as_success() -> None:
    """delete_orphaned_storage_object treats 404 (already gone) as success."""
    from aspire_orchestrator.services.blueprint import storage_lifecycle

    class _Resp:
        def __init__(self, status: int) -> None:
            self.status_code = status

    class _Client:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def delete(self, url, headers=None):
            return _Resp(404)

    with patch.object(storage_lifecycle, "httpx") as mock_httpx, \
         patch.object(storage_lifecycle.settings, "supabase_url", "https://x.supabase.co"), \
         patch.object(storage_lifecycle.settings, "supabase_service_role_key", "fake-key"):
        mock_httpx.AsyncClient = _Client
        mock_httpx.TimeoutException = Exception
        result = await storage_lifecycle.delete_orphaned_storage_object(
            suite_id="00000000-0000-0000-0000-000000000001",
            upload_id="upload-aaaa",
            correlation_id="corr-aaaa",
        )
    assert result is True
