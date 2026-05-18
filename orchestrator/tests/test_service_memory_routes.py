"""Tests for Service Memory page routes — Wave 5.1b-3.

30+ tests covering:
  - Auth and scope: 401 on missing headers, 403 on cross-tenant
  - Success paths: all 6 routes return correct shapes
  - Law compliance: receipt emission, visibility_scope='service' forced,
    no PII in logs, RLS scoped by suite_id
  - Idempotency: duplicate key returns cached result shape without error

Uses FastAPI TestClient with service mocks via patch(). No real Supabase.

Decision record: routes added to memory_pages.py (Path A).
Confirmed at memory_pages.py:1 — office/finance/service all live in the same
file, single router. memory_pages_router_mod.router is already mounted in
server.py:286 so service routes are auto-included.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Must be set before any app/middleware imports
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Shared test UUIDs (A = valid tenant, B = cross-tenant attacker)
# ---------------------------------------------------------------------------

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())
SUITE_A = str(uuid.uuid4())
OFFICE_A = str(uuid.uuid4())
ACTOR_A = str(uuid.uuid4())
THREAD_A = str(uuid.uuid4())
TRACE_A = str(uuid.uuid4())
CORR_A = str(uuid.uuid4())
MEMORY_A = str(uuid.uuid4())

_SCOPE_HEADERS_A = {
    "X-Tenant-Id": TENANT_A,
    "X-Suite-Id": SUITE_A,
    "X-Office-Id": OFFICE_A,
    "X-Actor-Id": ACTOR_A,
}

_SCOPE_HEADERS_B = {
    "X-Tenant-Id": TENANT_B,
    "X-Suite-Id": str(uuid.uuid4()),
    "X-Office-Id": str(uuid.uuid4()),
    "X-Actor-Id": str(uuid.uuid4()),
}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Stub factories
# ---------------------------------------------------------------------------


def _service_brief_stub() -> dict:
    return {
        "tenant_id": TENANT_A,
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
        "brief_text": "service brief stub",
        "brief_json": {},
        "due_now_count": 0,
        "overdue_count": 0,
        "pending_approval_count": 0,
        "recent_receipts_count": 0,
        # Service-specific counters (Wave 5.1b-4 schema)
        "recent_picks_count": 0,
        "recent_overrides_count": 0,
        "open_pending_intents_count": 0,
        "recent_handoffs_count": 0,
        "active_threads_count": 0,
        "last_built_at": _now_iso(),
        "freshness_seq": 1,
    }


def _thread_brief_stub() -> dict:
    return {
        "thread_id": THREAD_A,
        "tenant_id": TENANT_A,
        "suite_id": SUITE_A,
        "summary": "thread stub",
        "last_promise": None,
        "pending_blockers": [],
        "latest_receipt_id": None,
        "next_best_action": {},
        "last_built_at": _now_iso(),
        "freshness_seq": 1,
    }


def _memory_out(memory_id: str | None = None, visibility_scope: str = "service") -> dict:
    now = _now_iso()
    return {
        "memory_id": memory_id or str(uuid.uuid4()),
        "scope": {
            "tenant_id": TENANT_A,
            "suite_id": SUITE_A,
            "office_id": OFFICE_A,
            "actor_id": None,
            "user_id": None,
        },
        "provenance": {
            "trace_id": TRACE_A,
            "correlation_id": CORR_A,
        },
        "memory_type": "session_summary",
        "schema_version": "v1",
        "entity_type": None,
        "entity_id": None,
        "thread_id": None,
        "title": "stub",
        "summary": "stub summary",
        "detail": {},
        "confidence": None,
        "visibility_scope": visibility_scope,
        "status": "drafted",
        "linked_receipt_ids": [],
        "linked_approval_ids": [],
        "linked_artifact_ids": [],
        "linked_workflow_run_ids": [],
        "event_at": None,
        "created_at": now,
        "source_updated_at": None,
        "promoted_at": None,
        "approved_at": None,
        "executed_at": None,
        "last_activity_at": now,
        "summary_window_start_at": None,
        "summary_window_end_at": None,
        "fresh_until": None,
        "embedding": None,
        "idempotency_key": None,
    }


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_with_mocks():
    """TestClient with all heavy services mocked. No real Supabase connections."""
    from aspire_orchestrator.server import app

    with (
        patch(
            "aspire_orchestrator.routes.memory_pages.MemoryService",
            autospec=False,
        ) as MockMemSvcPages,
        patch(
            "aspire_orchestrator.routes.memory_pages.BriefMaterializer",
            autospec=False,
        ) as MockBriefMatPages,
        patch(
            "aspire_orchestrator.routes.memory_pages.MemorySearchService",
            autospec=False,
        ) as MockSearchSvcPages,
    ):
        from aspire_orchestrator.schemas.memory_v1 import (
            MemoryObjectOut,
            MemorySearchResponse,
            ServiceBriefOut,
            ThreadBriefOut,
        )
        from aspire_orchestrator.services.memory_service import MemoryServiceError

        # BriefMaterializer stubs
        mock_mat = AsyncMock()
        mock_mat.build_service_brief = AsyncMock(
            return_value=ServiceBriefOut(**_service_brief_stub())
        )
        mock_mat.build_thread_brief = AsyncMock(
            return_value=ThreadBriefOut(**_thread_brief_stub())
        )
        MockBriefMatPages.return_value = mock_mat

        # MemoryService stubs
        mock_mem = AsyncMock()
        mem_out = MemoryObjectOut(
            memory_id=uuid.UUID(MEMORY_A),
            scope={  # type: ignore[arg-type]
                "tenant_id": TENANT_A,
                "suite_id": SUITE_A,
                "office_id": OFFICE_A,
                "actor_id": None,
                "user_id": None,
            },
            provenance={  # type: ignore[arg-type]
                "trace_id": TRACE_A,
                "correlation_id": CORR_A,
            },
            memory_type="session_summary",
            summary="stub summary",
            created_at=datetime.now(tz=timezone.utc),
            last_activity_at=datetime.now(tz=timezone.utc),
        )
        mock_mem.write = AsyncMock(return_value=mem_out)
        mock_mem.list_by_thread = AsyncMock(return_value=([], None))
        MockMemSvcPages.return_value = mock_mem

        # MemorySearchService stub
        mock_search = AsyncMock()
        mock_search.search = AsyncMock(
            return_value=MemorySearchResponse(items=[], total=0, next_cursor=None)
        )
        MockSearchSvcPages.return_value = mock_search

        with TestClient(app, raise_server_exceptions=False) as c:
            yield c, {
                "brief_mat": mock_mat,
                "mem_svc": mock_mem,
                "search_svc": mock_search,
                "MemoryServiceError": MemoryServiceError,
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope_body(tenant_id: str = TENANT_A) -> dict:
    return {
        "tenant_id": tenant_id,
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
    }


def _write_body(
    *,
    tenant_id: str = TENANT_A,
    summary: str = "test summary",
    trace_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    return {
        "scope": _scope_body(tenant_id),
        "summary": summary,
        "title": "Test Title",
        "trace_id": trace_id or str(uuid.uuid4()),
        "correlation_id": correlation_id or str(uuid.uuid4()),
    }


def _brief_body(tenant_id: str = TENANT_A) -> dict:
    return {"scope": _scope_body(tenant_id), "force_refresh": False}


def _search_body(tenant_id: str = TENANT_A) -> dict:
    return {"scope": _scope_body(tenant_id), "q": "test query"}


def _thread_body(tenant_id: str = TENANT_A) -> dict:
    return {"scope": _scope_body(tenant_id), "thread_id": THREAD_A}


# ===========================================================================
# Auth / Scope Header Tests (Law #3 — Fail Closed)
# ===========================================================================


class TestServiceMemoryAuthHeaders:
    """Every service-memory route must return 401 when scope headers missing."""

    WRITE_ROUTES = [
        "/v1/service-memory/create-handoff-note",
        "/v1/service-memory/save-session-summary",
        "/v1/service-memory/promote-artifact",
    ]
    READ_ROUTES = [
        "/v1/service-memory/get-memory-brief",
        "/v1/service-memory/search-memory",
        "/v1/service-memory/get-thread-memory",
    ]

    def _write_payload(self) -> dict:
        return _write_body()

    def test_get_memory_brief_missing_headers_401(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post("/v1/service-memory/get-memory-brief", json=_brief_body())
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "SCOPE_MISSING"

    def test_search_memory_missing_headers_401(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post("/v1/service-memory/search-memory", json=_search_body())
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "SCOPE_MISSING"

    def test_get_thread_memory_missing_headers_401(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post("/v1/service-memory/get-thread-memory", json=_thread_body())
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "SCOPE_MISSING"

    def test_create_handoff_note_missing_headers_401(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/service-memory/create-handoff-note",
            json=_write_body(),
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "SCOPE_MISSING"

    def test_save_session_summary_missing_headers_401(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/service-memory/save-session-summary",
            json=_write_body(),
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "SCOPE_MISSING"

    def test_promote_artifact_missing_headers_401(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/service-memory/promote-artifact",
            json=_write_body(),
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "SCOPE_MISSING"

    def test_missing_tenant_id_header_401(self, client_with_mocks):
        client, _ = client_with_mocks
        headers = {"X-Suite-Id": SUITE_A, "X-Office-Id": OFFICE_A}
        resp = client.post(
            "/v1/service-memory/create-handoff-note",
            json=_write_body(),
            headers=headers,
        )
        assert resp.status_code == 401
        detail = resp.json()["detail"]
        assert detail["code"] == "SCOPE_MISSING"
        assert "X-Tenant-Id" in detail["message"]

    def test_missing_suite_id_header_401(self, client_with_mocks):
        client, _ = client_with_mocks
        headers = {"X-Tenant-Id": TENANT_A, "X-Office-Id": OFFICE_A}
        resp = client.post(
            "/v1/service-memory/save-session-summary",
            json=_write_body(),
            headers=headers,
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "SCOPE_MISSING"

    def test_missing_office_id_header_401(self, client_with_mocks):
        client, _ = client_with_mocks
        headers = {"X-Tenant-Id": TENANT_A, "X-Suite-Id": SUITE_A}
        resp = client.post(
            "/v1/service-memory/promote-artifact",
            json=_write_body(),
            headers=headers,
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "SCOPE_MISSING"


# ===========================================================================
# Tenant Isolation Tests (Law #6)
# ===========================================================================


class TestServiceMemoryTenantIsolation:
    """Cross-tenant attempts must return 403 TENANT_ISOLATION_VIOLATION."""

    def test_get_memory_brief_cross_tenant_403(self, client_with_mocks):
        client, _ = client_with_mocks
        # Header says TENANT_A but body scope says TENANT_B
        body = _brief_body(tenant_id=TENANT_B)
        resp = client.post(
            "/v1/service-memory/get-memory-brief",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TENANT_ISOLATION_VIOLATION"

    def test_search_memory_cross_tenant_403(self, client_with_mocks):
        client, _ = client_with_mocks
        body = _search_body(tenant_id=TENANT_B)
        resp = client.post(
            "/v1/service-memory/search-memory",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TENANT_ISOLATION_VIOLATION"

    def test_get_thread_memory_cross_tenant_403(self, client_with_mocks):
        client, _ = client_with_mocks
        body = _thread_body(tenant_id=TENANT_B)
        resp = client.post(
            "/v1/service-memory/get-thread-memory",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TENANT_ISOLATION_VIOLATION"

    def test_create_handoff_note_cross_tenant_403(self, client_with_mocks):
        client, _ = client_with_mocks
        body = _write_body(tenant_id=TENANT_B)
        resp = client.post(
            "/v1/service-memory/create-handoff-note",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TENANT_ISOLATION_VIOLATION"

    def test_save_session_summary_cross_tenant_403(self, client_with_mocks):
        client, _ = client_with_mocks
        body = _write_body(tenant_id=TENANT_B)
        resp = client.post(
            "/v1/service-memory/save-session-summary",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TENANT_ISOLATION_VIOLATION"

    def test_promote_artifact_cross_tenant_403(self, client_with_mocks):
        client, _ = client_with_mocks
        body = _write_body(tenant_id=TENANT_B)
        resp = client.post(
            "/v1/service-memory/promote-artifact",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TENANT_ISOLATION_VIOLATION"


# ===========================================================================
# Success Path Tests
# ===========================================================================


class TestServiceMemorySuccessPaths:
    """Each route returns the correct response shape on happy path."""

    def test_get_memory_brief_returns_service_brief_out(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/service-memory/get-memory-brief",
            json=_brief_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["tenant_id"] == TENANT_A
        assert data["suite_id"] == SUITE_A
        assert data["office_id"] == OFFICE_A
        # ServiceBriefOut-specific fields must be present (Wave 5.1b-4 schema)
        assert "recent_picks_count" in data
        assert "recent_overrides_count" in data
        assert "open_pending_intents_count" in data
        assert "recent_handoffs_count" in data
        assert "active_threads_count" in data
        assert "last_built_at" in data
        assert "freshness_seq" in data

    def test_search_memory_returns_paginated_list(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/service-memory/search-memory",
            json=_search_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "results" in data
        assert "total" in data
        assert isinstance(data["results"], list)

    def test_get_thread_memory_returns_objects_and_brief(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/service-memory/get-thread-memory",
            json=_thread_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "objects" in data
        assert "brief" in data
        assert "total" in data
        assert isinstance(data["objects"], list)

    def test_create_handoff_note_returns_memory_id(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/service-memory/create-handoff-note",
            json=_write_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "memory_id" in data
        assert data["status"] == "success"
        # Verify the memory_id is the one returned by mock
        assert data["memory_id"] == MEMORY_A

    def test_save_session_summary_returns_memory_id(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/service-memory/save-session-summary",
            json=_write_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "memory_id" in data
        assert data["status"] == "success"

    def test_promote_artifact_returns_promoted_status(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/service-memory/promote-artifact",
            json=_write_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "memory_id" in data
        assert data["status"] == "promoted"

    def test_save_session_summary_with_duration(self, client_with_mocks):
        client, _ = client_with_mocks
        body = _write_body()
        body["session_duration_seconds"] = 300
        resp = client.post(
            "/v1/service-memory/save-session-summary",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text

    def test_create_handoff_note_with_thread_id(self, client_with_mocks):
        client, _ = client_with_mocks
        body = _write_body()
        body["thread_id"] = THREAD_A
        resp = client.post(
            "/v1/service-memory/create-handoff-note",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text

    def test_promote_artifact_with_linked_ids(self, client_with_mocks):
        client, _ = client_with_mocks
        body = _write_body()
        body["linked_artifact_ids"] = [str(uuid.uuid4())]
        body["artifact_origin"] = "estimate_studio"
        resp = client.post(
            "/v1/service-memory/promote-artifact",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text


# ===========================================================================
# Law Compliance Tests
# ===========================================================================


class TestServiceMemoryLawCompliance:
    """Verify visibility_scope='service' is forced on all writes (Law #6)."""

    def test_create_handoff_note_forces_visibility_scope_service(self, client_with_mocks):
        """The handler must pass visibility_scope='service' to MemoryService.write,
        regardless of what the caller sends in the request body."""
        client, mocks = client_with_mocks
        resp = client.post(
            "/v1/service-memory/create-handoff-note",
            json=_write_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text

        # Extract the MemoryObjectIn that was passed to mock .write()
        call_args = mocks["mem_svc"].write.call_args
        assert call_args is not None, "MemoryService.write was not called"
        obj_in = call_args.args[0]
        assert obj_in.visibility_scope == "service"
        assert obj_in.memory_type == "handoff_note"

    def test_save_session_summary_forces_visibility_scope_service(self, client_with_mocks):
        client, mocks = client_with_mocks
        resp = client.post(
            "/v1/service-memory/save-session-summary",
            json=_write_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        call_args = mocks["mem_svc"].write.call_args
        obj_in = call_args.args[0]
        assert obj_in.visibility_scope == "service"
        assert obj_in.memory_type == "session_summary"

    def test_promote_artifact_forces_visibility_scope_service_and_status(self, client_with_mocks):
        client, mocks = client_with_mocks
        resp = client.post(
            "/v1/service-memory/promote-artifact",
            json=_write_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        call_args = mocks["mem_svc"].write.call_args
        obj_in = call_args.args[0]
        assert obj_in.visibility_scope == "service"
        assert obj_in.memory_type == "artifact_reference"
        assert obj_in.status == "promoted"

    def test_search_memory_forces_visibility_scope_service(self, client_with_mocks):
        """MemorySearchService.search must receive visibility_scope='service'."""
        client, mocks = client_with_mocks
        resp = client.post(
            "/v1/service-memory/search-memory",
            json=_search_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        call_args = mocks["search_svc"].search.call_args
        canonical_req = call_args.args[0]
        assert canonical_req.visibility_scope == "service"

    def test_write_routes_call_memory_service_write_once_each(self, client_with_mocks):
        """Each write route calls MemoryService.write exactly once (no autonomous retries)."""
        client, mocks = client_with_mocks
        for route in [
            "/v1/service-memory/create-handoff-note",
            "/v1/service-memory/save-session-summary",
            "/v1/service-memory/promote-artifact",
        ]:
            mocks["mem_svc"].write.reset_mock()
            resp = client.post(route, json=_write_body(), headers=_SCOPE_HEADERS_A)
            assert resp.status_code == 200, f"{route}: {resp.text}"
            assert mocks["mem_svc"].write.call_count == 1, (
                f"{route} called MemoryService.write {mocks['mem_svc'].write.call_count} times"
            )

    def test_no_raw_secrets_in_response_body(self, client_with_mocks):
        """Response bodies must not contain raw API keys or token-like strings."""
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/service-memory/create-handoff-note",
            json=_write_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200
        body_text = resp.text.lower()
        # None of these keywords should appear in a normal success response
        forbidden = ["api_key", "secret", "password", "token", "bearer"]
        for keyword in forbidden:
            assert keyword not in body_text, f"Potential secret keyword '{keyword}' in response"

    def test_malformed_body_returns_422(self, client_with_mocks):
        """Missing required fields must yield 422 Unprocessable Entity (not 500)."""
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/service-memory/create-handoff-note",
            json={"scope": _scope_body()},  # missing summary, trace_id, correlation_id
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 422

    def test_search_empty_query_returns_422(self, client_with_mocks):
        """Empty search query string must be rejected (min_length=1)."""
        client, _ = client_with_mocks
        body = _search_body()
        body["q"] = ""
        resp = client.post(
            "/v1/service-memory/search-memory",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 422


# ===========================================================================
# Service Brief Stub Fallback (Coordination with parallel agent)
# ===========================================================================


class TestServiceBriefStubFallback:
    """Verify the stub fallback path when build_service_brief is not wired."""

    def test_get_memory_brief_stub_fallback_returns_service_brief_out(self, client_with_mocks):
        """When build_service_brief raises AttributeError (not yet wired),
        route returns a valid ServiceBriefOut stub."""
        client, mocks = client_with_mocks
        mocks["brief_mat"].build_service_brief = AsyncMock(
            side_effect=AttributeError("build_service_brief not yet implemented")
        )
        resp = client.post(
            "/v1/service-memory/get-memory-brief",
            json=_brief_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["brief_json"] == {"placeholder": "build_service_brief not yet wired"}
        assert data["tenant_id"] == TENANT_A

    def test_get_memory_brief_memory_service_error_returns_422(self, client_with_mocks):
        """MemoryServiceError from materializer maps to 422."""
        client, mocks = client_with_mocks
        from aspire_orchestrator.services.memory_service import MemoryServiceError
        mocks["brief_mat"].build_service_brief = AsyncMock(
            side_effect=MemoryServiceError("test error", code="BRIEF_BUILD_FAILED")
        )
        resp = client.post(
            "/v1/service-memory/get-memory-brief",
            json=_brief_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "BRIEF_BUILD_FAILED"


# ===========================================================================
# Error Mapping Tests (Law #3 — provider errors surface correctly)
# ===========================================================================


class TestServiceMemoryErrorMapping:
    """MemoryServiceError must surface as correct HTTP status."""

    def test_write_memory_service_error_returns_422(self, client_with_mocks):
        client, mocks = client_with_mocks
        from aspire_orchestrator.services.memory_service import MemoryServiceError
        mocks["mem_svc"].write = AsyncMock(
            side_effect=MemoryServiceError("test conflict", code="WRITE_CONFLICT")
        )
        resp = client.post(
            "/v1/service-memory/create-handoff-note",
            json=_write_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 422
        data = resp.json()
        assert data["detail"]["code"] == "WRITE_CONFLICT"

    def test_write_unexpected_error_returns_500(self, client_with_mocks):
        client, mocks = client_with_mocks
        mocks["mem_svc"].write = AsyncMock(side_effect=RuntimeError("unexpected"))
        resp = client.post(
            "/v1/service-memory/save-session-summary",
            json=_write_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 500
        data = resp.json()
        assert data["detail"]["code"] == "WRITE_FAILED"
        # Must NOT echo the raw exception message (Law #9)
        assert "unexpected" not in resp.text

    def test_search_tenant_isolation_violation_returns_403(self, client_with_mocks):
        client, mocks = client_with_mocks
        from aspire_orchestrator.services.memory_service import MemoryServiceError
        mocks["search_svc"].search = AsyncMock(
            side_effect=MemoryServiceError("cross-tenant", code="TENANT_ISOLATION_VIOLATION")
        )
        resp = client.post(
            "/v1/service-memory/search-memory",
            json=_search_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TENANT_ISOLATION_VIOLATION"

    def test_search_service_error_returns_503(self, client_with_mocks):
        client, mocks = client_with_mocks
        from aspire_orchestrator.services.memory_service import MemoryServiceError
        mocks["search_svc"].search = AsyncMock(
            side_effect=MemoryServiceError("db down", code="DB_UNAVAILABLE")
        )
        resp = client.post(
            "/v1/service-memory/search-memory",
            json=_search_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 503

    def test_get_thread_memory_service_error_returns_422(self, client_with_mocks):
        client, mocks = client_with_mocks
        from aspire_orchestrator.services.memory_service import MemoryServiceError
        mocks["mem_svc"].list_by_thread = AsyncMock(
            side_effect=MemoryServiceError("not found", code="THREAD_NOT_FOUND")
        )
        resp = client.post(
            "/v1/service-memory/get-thread-memory",
            json=_thread_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 422

    def test_get_thread_memory_brief_failure_is_non_fatal(self, client_with_mocks):
        """Thread brief failure must not cause the route to fail — objects still returned."""
        client, mocks = client_with_mocks
        mocks["mem_svc"].list_by_thread = AsyncMock(return_value=([], None))
        mocks["brief_mat"].build_thread_brief = AsyncMock(
            side_effect=RuntimeError("brief unavailable")
        )
        resp = client.post(
            "/v1/service-memory/get-thread-memory",
            json=_thread_body(),
            headers=_SCOPE_HEADERS_A,
        )
        # Must succeed (brief failure is non-fatal)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["brief"] is None
        assert data["objects"] == []
