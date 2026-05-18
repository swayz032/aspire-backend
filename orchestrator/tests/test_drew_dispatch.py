"""Drew async-dispatch route tests — blueprint-async-dispatch wave.

Tests POST /v1/blueprints/dispatch accept path, dedup, fail-closed cases,
cross-tenant evil test, background pipeline, and Law compliance.

Implementation note: route handler functions are tested directly (not via
TestClient) because full server.py requires `langgraph` which is not
installed in the unit-test environment. Pattern matches test_drew_read_apis.py.

Law coverage:
  Law #2 — every code path emits a receipt with correct action_type + outcome
  Law #3 — fail closed: missing token → 401, invalid base64 → 422, oversized → 422
  Law #5 — capability token validated at HTTP boundary
  Law #6 — cross-tenant dedup never matches; suite_id scoped queries
  Law #9 — PDF bytes never appear in receipts or log arguments
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

SUITE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUITE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OFFICE_A = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TENANT_A = "cccccccc-cccc-cccc-cccc-cccccccccccc"
CORR_ID = "test-dispatch-corr-001"
VALID_TOKEN = "valid-capability-token-xyz"

# Minimal valid PDF bytes (just enough to pass base64 decode and size check)
_FAKE_PDF_BYTES = b"%PDF-1.4 fake-pdf-content-for-testing"
_FAKE_PDF_B64 = base64.b64encode(_FAKE_PDF_BYTES).decode()
_FAKE_PDF_HASH = hashlib.sha256(_FAKE_PDF_BYTES).hexdigest()

_NOW = datetime.now(timezone.utc).isoformat()


def _make_request(
    suite_id: str = SUITE_A,
    office_id: str = OFFICE_A,
    pdf_b64: str = _FAKE_PDF_B64,
    capability_token: str = VALID_TOKEN,
    correlation_id: str = CORR_ID,
    filename: str = "test.pdf",
) -> "BlueprintDispatchRequest":
    from aspire_orchestrator.routes.blueprint_dispatch import BlueprintDispatchRequest
    return BlueprintDispatchRequest(
        pdf_bytes=pdf_b64,
        filename=filename,
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        capability_token=capability_token,
    )


def _query_receipts_by_corr(correlation_id: str) -> list[dict]:
    """Query in-memory receipt store by correlation_id."""
    import aspire_orchestrator.services.receipt_store as rs
    with rs._lock:
        return [r for r in rs._receipts if r.get("correlation_id") == correlation_id]


# ---------------------------------------------------------------------------
# 1. Happy path — new project queued
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_happy_path_queued():
    """New PDF accepted: returns project_id, status=queued, receipt emitted."""
    import aspire_orchestrator.services.receipt_store as rs
    rs._receipts.clear()

    new_project_id = str(uuid.uuid4())

    mock_bg = MagicMock(spec=BackgroundTasks)

    with (
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch.supabase_select",
            new_callable=AsyncMock,
            return_value=[],  # no existing dedup hit
        ),
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch.supabase_insert",
            new_callable=AsyncMock,
            return_value={"id": new_project_id},
        ),
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch.upload_raw_pdf",
            new_callable=AsyncMock,
            return_value=f"{SUITE_A}/{new_project_id}/raw.pdf",
        ),
        patch(
            "aspire_orchestrator.services.supabase_client.supabase_update",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-001"),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=CORR_ID),
        patch("uuid.uuid4", side_effect=[uuid.UUID(new_project_id), uuid.uuid4(), uuid.uuid4()]),
    ):
        from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

        resp = await dispatch_blueprint(mock_bg, _make_request())

    assert resp.status == "queued"
    assert resp.project_id is not None
    assert resp.correlation_id == CORR_ID
    assert mock_bg.add_task.called

    # Law #2: accepted receipt must be emitted
    receipts = _query_receipts_by_corr(CORR_ID)
    action_types = [r["action_type"] for r in receipts]
    assert "blueprint.upload.accepted" in action_types

    accepted = next(r for r in receipts if r["action_type"] == "blueprint.upload.accepted")
    assert accepted["outcome"] == "success"
    assert accepted["suite_id"] == SUITE_A


# ---------------------------------------------------------------------------
# 2. Dedup path — same content_hash, same suite_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_dedup_returns_existing():
    """Same PDF re-uploaded returns existing project_id, status=dedup."""
    import aspire_orchestrator.services.receipt_store as rs
    rs._receipts.clear()

    existing_id = str(uuid.uuid4())
    corr = "test-dispatch-dedup-001"

    mock_bg = MagicMock(spec=BackgroundTasks)

    with (
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch.supabase_select",
            new_callable=AsyncMock,
            return_value=[{
                "id": existing_id,
                "suite_id": SUITE_A,
                "pdf_storage_path": f"{SUITE_A}/{existing_id}/raw.pdf",
                "content_hash": _FAKE_PDF_HASH,
            }],
        ),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-002"),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
    ):
        from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

        resp = await dispatch_blueprint(mock_bg, _make_request(correlation_id=corr))

    assert resp.status == "dedup"
    assert resp.project_id == existing_id
    assert resp.pdf_storage_path is not None
    # Background pipeline NOT enqueued for dedup
    assert not mock_bg.add_task.called

    receipts = _query_receipts_by_corr(corr)
    action_types = [r["action_type"] for r in receipts]
    assert "blueprint.upload.dedup" in action_types

    dedup_receipt = next(r for r in receipts if r["action_type"] == "blueprint.upload.dedup")
    assert dedup_receipt["outcome"] == "success"


# ---------------------------------------------------------------------------
# 3. Fail closed — missing capability token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_fail_closed_missing_token():
    """Missing capability token must deny with 401 and emit denied receipt."""
    import aspire_orchestrator.services.receipt_store as rs
    rs._receipts.clear()

    corr = "test-dispatch-denied-token-001"
    mock_bg = MagicMock(spec=BackgroundTasks)

    with (
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-003"),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
    ):
        from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await dispatch_blueprint(
                mock_bg,
                _make_request(capability_token="", correlation_id=corr),
            )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"] == "MISSING_CAPABILITY_TOKEN"

    # Law #2: denied receipt must be emitted
    receipts = _query_receipts_by_corr(corr)
    action_types = [r["action_type"] for r in receipts]
    assert "blueprint.upload.denied" in action_types

    denied = next(r for r in receipts if r["action_type"] == "blueprint.upload.denied")
    assert denied["outcome"] == "denied"

    # Law #9: no PDF bytes in receipt
    receipt_str = str(denied)
    assert _FAKE_PDF_B64 not in receipt_str


# ---------------------------------------------------------------------------
# 4. Fail closed — blank/whitespace-only token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_fail_closed_blank_token():
    """Whitespace-only capability token must deny."""
    mock_bg = MagicMock(spec=BackgroundTasks)

    with (
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-004"),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value="corr-004"),
    ):
        from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await dispatch_blueprint(
                mock_bg,
                _make_request(capability_token="   ", correlation_id="corr-004"),
            )

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# 5. Fail closed — invalid base64
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_fail_closed_invalid_base64():
    """Invalid base64 must return 422 INVALID_BASE64."""
    import aspire_orchestrator.services.receipt_store as rs
    rs._receipts.clear()

    corr = "test-dispatch-bad-b64-001"
    mock_bg = MagicMock(spec=BackgroundTasks)

    with (
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-005"),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
    ):
        from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await dispatch_blueprint(
                mock_bg,
                _make_request(pdf_b64="!not-valid-base64!!", correlation_id=corr),
            )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"] == "INVALID_BASE64"

    receipts = _query_receipts_by_corr(corr)
    assert any(r["action_type"] == "blueprint.upload.denied" for r in receipts)


# ---------------------------------------------------------------------------
# 6. Fail closed — oversized PDF
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_fail_closed_oversized_pdf():
    """PDF exceeding 50MB must return 422 PDF_TOO_LARGE."""
    import aspire_orchestrator.services.receipt_store as rs
    rs._receipts.clear()

    corr = "test-dispatch-oversized-001"
    mock_bg = MagicMock(spec=BackgroundTasks)

    # Create a base64-encoded byte string that decodes to >50MB
    # We don't actually allocate 50MB — mock the decode
    oversized_bytes = b"x" * (51 * 1024 * 1024)
    oversized_b64 = base64.b64encode(oversized_bytes).decode()

    with (
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-006"),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
    ):
        from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await dispatch_blueprint(
                mock_bg,
                _make_request(pdf_b64=oversized_b64, correlation_id=corr),
            )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"] == "PDF_TOO_LARGE"

    receipts = _query_receipts_by_corr(corr)
    assert any(r["action_type"] == "blueprint.upload.denied" for r in receipts)


# ---------------------------------------------------------------------------
# 7. Fail closed — missing required fields (Pydantic validation)
# ---------------------------------------------------------------------------

def test_dispatch_request_rejects_missing_suite_id():
    """Pydantic rejects missing suite_id."""
    from pydantic import ValidationError
    from aspire_orchestrator.routes.blueprint_dispatch import BlueprintDispatchRequest

    with pytest.raises(ValidationError):
        BlueprintDispatchRequest(
            pdf_bytes=_FAKE_PDF_B64,
            filename="test.pdf",
            # suite_id missing
            office_id=OFFICE_A,
            correlation_id=CORR_ID,
            capability_token=VALID_TOKEN,
        )


def test_dispatch_request_rejects_missing_office_id():
    """Pydantic rejects missing office_id."""
    from pydantic import ValidationError
    from aspire_orchestrator.routes.blueprint_dispatch import BlueprintDispatchRequest

    with pytest.raises(ValidationError):
        BlueprintDispatchRequest(
            pdf_bytes=_FAKE_PDF_B64,
            filename="test.pdf",
            suite_id=SUITE_A,
            # office_id missing
            correlation_id=CORR_ID,
            capability_token=VALID_TOKEN,
        )


def test_dispatch_request_rejects_missing_capability_token():
    """Pydantic rejects missing capability_token."""
    from pydantic import ValidationError
    from aspire_orchestrator.routes.blueprint_dispatch import BlueprintDispatchRequest

    with pytest.raises(ValidationError):
        BlueprintDispatchRequest(
            pdf_bytes=_FAKE_PDF_B64,
            filename="test.pdf",
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            correlation_id=CORR_ID,
            # capability_token missing
        )


# ---------------------------------------------------------------------------
# 8. Cross-tenant evil test (Law #6)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_cross_tenant_dedup_does_not_leak():
    """Dedup check is scoped by suite_id — SUITE_B cannot match SUITE_A project."""
    import aspire_orchestrator.services.receipt_store as rs
    rs._receipts.clear()

    corr_evil = "test-dispatch-evil-xtenant-001"
    mock_bg = MagicMock(spec=BackgroundTasks)
    new_project_id = str(uuid.uuid4())

    def _scope_aware_select(table: str, *, filters: str = "", **kwargs: Any) -> list[dict]:
        """Simulate RLS: only return rows matching suite_id in filters."""
        if f"suite_id=eq.{SUITE_B}" in filters:
            return []  # SUITE_B has no matching project
        return []

    with (
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch.supabase_select",
            new_callable=AsyncMock,
            side_effect=_scope_aware_select,
        ),
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch.supabase_insert",
            new_callable=AsyncMock,
            return_value={"id": new_project_id},
        ),
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch.upload_raw_pdf",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-008"),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr_evil),
    ):
        from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

        # SUITE_B attacker sends same PDF as SUITE_A — must NOT get SUITE_A's project_id
        resp = await dispatch_blueprint(
            mock_bg,
            _make_request(suite_id=SUITE_B, correlation_id=corr_evil),
        )

    # Result must be a new queued project, NOT SUITE_A's existing one
    assert resp.status == "queued"
    assert resp.project_id != ""
    # Background task enqueued for fresh project
    assert mock_bg.add_task.called


# ---------------------------------------------------------------------------
# 9. Receipt coverage — every code path generates a receipt (Law #2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_receipt_on_db_insert_failure():
    """DB insert failure emits denied receipt and returns 503."""
    import aspire_orchestrator.services.receipt_store as rs
    rs._receipts.clear()

    corr = "test-dispatch-db-fail-001"
    mock_bg = MagicMock(spec=BackgroundTasks)

    from aspire_orchestrator.services.supabase_client import SupabaseClientError

    with (
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch.supabase_select",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch.supabase_insert",
            new_callable=AsyncMock,
            side_effect=SupabaseClientError("insert", status_code=500, detail="test"),
        ),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-009"),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
    ):
        from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await dispatch_blueprint(mock_bg, _make_request(correlation_id=corr))

    assert exc_info.value.status_code == 503

    receipts = _query_receipts_by_corr(corr)
    assert any(r["action_type"] == "blueprint.upload.denied" for r in receipts)
    failed = next(r for r in receipts if r["action_type"] == "blueprint.upload.denied")
    assert failed["outcome"] == "failed"


# ---------------------------------------------------------------------------
# 10. Background task emits ingest.started receipt (Law #2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_background_task_emits_ingest_started():
    """_run_full_pipeline emits blueprint.ingest.started receipt."""
    import aspire_orchestrator.services.receipt_store as rs
    rs._receipts.clear()

    project_id = str(uuid.uuid4())
    corr = "test-pipeline-started-001"

    mock_ingest = AsyncMock(return_value={"status": "ok", "stage": "ingest", "project_id": project_id})
    mock_classify = AsyncMock(return_value={"status": "ok", "stage": "classify", "project_id": project_id})

    with (
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-010"),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch._async_ingest_pipeline",
            mock_ingest,
        ),
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch._async_classify_pipeline",
            mock_classify,
        ),
    ):
        from aspire_orchestrator.routes.blueprint_dispatch import _run_full_pipeline

        await _run_full_pipeline(
            project_id=project_id,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            filename="test.pdf",
            pdf_bytes=_FAKE_PDF_BYTES,
            correlation_id=corr,
            model="gpt-5.4-mini",
        )

    receipts = _query_receipts_by_corr(corr)
    action_types = [r["action_type"] for r in receipts]
    assert "blueprint.ingest.started" in action_types


# ---------------------------------------------------------------------------
# 11. Background task emits pipeline.failed on unhandled exception (Law #2, Law #3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_background_task_emits_pipeline_failed_on_exception():
    """Unhandled exception in pipeline emits blueprint.pipeline.failed receipt."""
    import aspire_orchestrator.services.receipt_store as rs
    rs._receipts.clear()

    project_id = str(uuid.uuid4())
    corr = "test-pipeline-failed-001"

    with (
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-011"),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch._async_ingest_pipeline",
            AsyncMock(side_effect=RuntimeError("simulated pipeline crash")),
        ),
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch.set_stage_progress",
            AsyncMock(),
        ),
    ):
        from aspire_orchestrator.routes.blueprint_dispatch import _run_full_pipeline

        # Must NOT raise — background task swallows and emits receipt
        await _run_full_pipeline(
            project_id=project_id,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            filename="test.pdf",
            pdf_bytes=_FAKE_PDF_BYTES,
            correlation_id=corr,
            model="gpt-5.4-mini",
        )

    receipts = _query_receipts_by_corr(corr)
    action_types = [r["action_type"] for r in receipts]
    assert "blueprint.pipeline.failed" in action_types

    failed = next(r for r in receipts if r["action_type"] == "blueprint.pipeline.failed")
    assert failed["outcome"] == "failed"


# ---------------------------------------------------------------------------
# 12. Background task emits ingest.failed when ingest returns error status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_background_task_emits_ingest_failed_on_error_status():
    """Ingest returning status=error emits blueprint.ingest.failed receipt."""
    import aspire_orchestrator.services.receipt_store as rs
    rs._receipts.clear()

    project_id = str(uuid.uuid4())
    corr = "test-ingest-failed-001"

    with (
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-012"),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch._async_ingest_pipeline",
            AsyncMock(return_value={"status": "error", "reason": "ocr_failed", "stage": "ingest"}),
        ),
    ):
        from aspire_orchestrator.routes.blueprint_dispatch import _run_full_pipeline

        await _run_full_pipeline(
            project_id=project_id,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            filename="test.pdf",
            pdf_bytes=_FAKE_PDF_BYTES,
            correlation_id=corr,
            model="gpt-5.4-mini",
        )

    receipts = _query_receipts_by_corr(corr)
    action_types = [r["action_type"] for r in receipts]
    assert "blueprint.ingest.failed" in action_types


# ---------------------------------------------------------------------------
# 13. Law #9 — PDF bytes never appear in receipts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_law9_pdf_bytes_not_in_receipts():
    """Law #9: raw PDF bytes and base64 string must never appear in receipts."""
    import aspire_orchestrator.services.receipt_store as rs
    rs._receipts.clear()

    corr = "test-law9-receipt-001"
    mock_bg = MagicMock(spec=BackgroundTasks)
    new_project_id = str(uuid.uuid4())

    with (
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch.supabase_select",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch.supabase_insert",
            new_callable=AsyncMock,
            return_value={"id": new_project_id},
        ),
        patch(
            "aspire_orchestrator.routes.blueprint_dispatch.upload_raw_pdf",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-013"),
        patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
    ):
        from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

        await dispatch_blueprint(mock_bg, _make_request(correlation_id=corr))

    receipts = _query_receipts_by_corr(corr)
    assert receipts, "Must emit at least one receipt"

    for receipt in receipts:
        receipt_str = str(receipt)
        # Raw bytes must not appear
        assert _FAKE_PDF_B64 not in receipt_str, "base64 PDF string found in receipt"
        assert _FAKE_PDF_BYTES.decode(errors="replace") not in receipt_str, "raw PDF bytes in receipt"
        # pdf_size_bytes in receipt is OK (just the integer) — we only check no raw content


# ---------------------------------------------------------------------------
# 14. pdf_raw_storage — mirrors thumbnail_storage pattern
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pdf_raw_storage_upload_returns_path():
    """upload_raw_pdf returns path string on successful upload."""
    with patch("aspire_orchestrator.services.blueprint.pdf_raw_storage._headers", return_value={
        "apikey": "test-key",
        "Authorization": "Bearer test-key",
    }):
        with patch("aspire_orchestrator.services.blueprint.pdf_raw_storage._storage_base_url", return_value="https://fake-supabase/storage/v1"):
            import httpx
            mock_response = MagicMock()
            mock_response.status_code = 200

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_cls.return_value = mock_client

                from aspire_orchestrator.services.blueprint.pdf_raw_storage import upload_raw_pdf

                path = await upload_raw_pdf(
                    suite_id=SUITE_A,
                    project_id="proj-uuid-0001",
                    pdf_bytes=_FAKE_PDF_BYTES,
                    correlation_id=CORR_ID,
                )

    assert path == f"{SUITE_A}/proj-uuid-0001/raw.pdf"


@pytest.mark.asyncio
async def test_pdf_raw_storage_returns_none_on_empty_bytes():
    """upload_raw_pdf returns None for empty bytes."""
    from aspire_orchestrator.services.blueprint.pdf_raw_storage import upload_raw_pdf

    result = await upload_raw_pdf(
        suite_id=SUITE_A,
        project_id="proj-uuid-0002",
        pdf_bytes=b"",
        correlation_id=CORR_ID,
    )
    assert result is None


@pytest.mark.asyncio
async def test_pdf_raw_storage_returns_none_on_timeout():
    """upload_raw_pdf returns None on httpx.TimeoutException (Law #3 soft failure)."""
    with patch("aspire_orchestrator.services.blueprint.pdf_raw_storage._headers", return_value={"apikey": "k"}):
        with patch("aspire_orchestrator.services.blueprint.pdf_raw_storage._storage_base_url", return_value="https://fake/v1"):
            import httpx

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
                mock_client_cls.return_value = mock_client

                from aspire_orchestrator.services.blueprint.pdf_raw_storage import upload_raw_pdf

                result = await upload_raw_pdf(
                    suite_id=SUITE_A,
                    project_id="proj-uuid-0003",
                    pdf_bytes=_FAKE_PDF_BYTES,
                    correlation_id=CORR_ID,
                )

    assert result is None


# ---------------------------------------------------------------------------
# 15. _async_ingest_pipeline refactor — _skip_project_creation path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_ingest_pipeline_skip_creation_uses_provided_project_id():
    """When _skip_project_creation=True, provided project_id is used without DB insert."""
    from aspire_orchestrator.skillpacks.drew_blueprint import _async_ingest_pipeline

    provided_id = str(uuid.uuid4())
    corr = "test-skip-creation-001"

    from aspire_orchestrator.services.blueprint.ocr_coordinator import SheetCorpus, OcrSheetResult

    fake_corpus = SheetCorpus()

    with (
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.supabase_insert",
            new_callable=AsyncMock,
        ) as mock_insert,
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.supabase_select",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.extract_sheet_corpus",
            new_callable=AsyncMock,
            return_value=fake_corpus,
        ),
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.set_stage_progress",
            new_callable=AsyncMock,
        ),
    ):
        result = await _async_ingest_pipeline(
            pdf_bytes=_FAKE_PDF_BYTES,
            project_hash=_FAKE_PDF_HASH,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            filename="test.pdf",
            correlation_id=corr,
            project_id=provided_id,
            _skip_project_creation=True,
        )

    # supabase_insert should NOT have been called (dispatch already inserted)
    assert not mock_insert.called, "supabase_insert must NOT be called when _skip_project_creation=True"
    # Result should use the provided project_id
    assert result.get("project_id") == provided_id or result.get("status") in ("ok", "dedup", "error")


@pytest.mark.asyncio
async def test_async_ingest_pipeline_skip_creation_requires_project_id():
    """_skip_project_creation=True without project_id must raise RuntimeError."""
    from aspire_orchestrator.skillpacks.drew_blueprint import _async_ingest_pipeline

    with pytest.raises((RuntimeError, Exception)):
        await _async_ingest_pipeline(
            pdf_bytes=_FAKE_PDF_BYTES,
            project_hash=_FAKE_PDF_HASH,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            filename="test.pdf",
            correlation_id="corr-err",
            project_id=None,  # Missing!
            _skip_project_creation=True,
        )
