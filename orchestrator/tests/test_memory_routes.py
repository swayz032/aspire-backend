"""Tests for Memory Spine + Memory Pages routes (Pass 4).

Uses FastAPI TestClient with app.dependency_overrides to mock services.
No real Supabase connection or external service calls.

Coverage:
  Contract tests:
  - POST /v1/memory-events without scope headers → 401
  - POST /v1/memory-events with valid scope + envelope → 200, event_id returned
  - GET  /v1/briefs/office/{office_id} returns the cached brief shape
  - POST /v1/office-memory/save-session-summary → 200, returns memory_id
  - POST /v1/office-memory/promote-artifact → 200, status='promoted'
  - Cross-tenant attempt: scope headers tenant A, body references tenant B → 403

  Law #3 fail-closed tests:
  - Missing X-Tenant-Id → 401 SCOPE_MISSING
  - Missing X-Suite-Id → 401 SCOPE_MISSING
  - Missing X-Office-Id → 401 SCOPE_MISSING
  - POST /v1/memory-events with mismatched tenant → 403 TENANT_ISOLATION_VIOLATION
  - GET /v1/briefs/office/{office_id} with wrong office_id in path → 403

  Page route tests:
  - POST /v1/office-memory/create-handoff-note → 200, memory_id
  - POST /v1/finance-memory/save-session-summary → 200, memory_id
  - POST /v1/finance-memory/promote-artifact → 200, status='promoted'
  - POST /v1/office-memory/get-memory-brief → 200, brief shape
  - POST /v1/finance-memory/get-memory-brief → 200, brief shape
  - POST /v1/office-memory/search-memory → 200, empty stub with note
  - POST /v1/proactive-candidates/query → 200, list response
  - POST /v1/session-broker/start → 200, session_id + allowed_tools
  - POST /v1/refinery/run → 422 on bad event_id (service raises RefineryError)
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# MUST be set before any app/middleware imports
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Shared test UUIDs
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


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Minimal stub factories
# ---------------------------------------------------------------------------


def _office_brief_stub() -> dict:
    return {
        "tenant_id": TENANT_A,
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
        "brief_text": "stub brief",
        "brief_json": {},
        "due_now_count": 0,
        "overdue_count": 0,
        "pending_approval_count": 0,
        "recent_receipts_count": 0,
        "last_built_at": _now_iso(),
        "freshness_seq": 1,
    }


def _finance_brief_stub() -> dict:
    return {
        "tenant_id": TENANT_A,
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
        "brief_text": "finance brief",
        "brief_json": {},
        "due_now_count": 0,
        "overdue_count": 0,
        "pending_approval_count": 0,
        "recent_receipts_count": 0,
        "provider_health": {},
        "aging_summary": {},
        "cash_narrative": None,
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


def _memory_out_stub(memory_id: str | None = None, status: str = "drafted") -> dict:
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
        "visibility_scope": "office",
        "status": status,
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
# Fixtures: client with service mocks via dependency_overrides
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_with_mocks():
    """TestClient with all heavy services mocked via dependency_overrides + patches."""
    from aspire_orchestrator.server import app
    from aspire_orchestrator.routes.memory import get_scope

    # We patch at the service module level so constructors in routes return mocks.
    with (
        patch(
            "aspire_orchestrator.routes.memory.supabase_insert",
            new=AsyncMock(return_value=[{"event_id": str(uuid.uuid4())}]),
        ),
        patch(
            "aspire_orchestrator.routes.memory.BriefMaterializer",
            autospec=False,
        ) as MockBriefMat,
        patch(
            "aspire_orchestrator.routes.memory.MemoryService",
            autospec=False,
        ) as MockMemSvc,
        patch(
            "aspire_orchestrator.routes.memory.ProactiveCandidateEngine",
            autospec=False,
        ) as MockCandEng,
        patch(
            "aspire_orchestrator.routes.memory.TranscriptEventRefinery",
            autospec=False,
        ) as MockRefinery,
        patch(
            "aspire_orchestrator.routes.memory.store_receipts",
            new=MagicMock(),
        ),
        patch(
            "aspire_orchestrator.routes.memory_pages.MemoryService",
            autospec=False,
        ) as MockMemSvcPages,
        patch(
            "aspire_orchestrator.routes.memory_pages.BriefMaterializer",
            autospec=False,
        ) as MockBriefMatPages,
        patch(
            "aspire_orchestrator.routes.memory.MemorySearchService",
            autospec=False,
        ) as MockSearchSvc,
        patch(
            "aspire_orchestrator.routes.memory_pages.MemorySearchService",
            autospec=False,
        ) as MockSearchSvcPages,
        patch(
            "aspire_orchestrator.routes.memory._kick_refinery_async",
            new=MagicMock(),
        ),
    ):
        from aspire_orchestrator.schemas.memory_v1 import (
            OfficeBriefOut,
            FinanceBriefOut,
            ThreadBriefOut,
            RefineResult,
            ProactiveCandidateOut,
        )

        # BriefMaterializer stubs
        mock_mat_instance = AsyncMock()
        mock_mat_instance.build_office_brief = AsyncMock(
            return_value=OfficeBriefOut(**_office_brief_stub())
        )
        mock_mat_instance.build_finance_brief = AsyncMock(
            return_value=FinanceBriefOut(**_finance_brief_stub())
        )
        mock_mat_instance.build_thread_brief = AsyncMock(
            return_value=ThreadBriefOut(**_thread_brief_stub())
        )
        MockBriefMat.return_value = mock_mat_instance
        MockBriefMatPages.return_value = mock_mat_instance

        # MemoryService stubs
        mock_mem_instance = AsyncMock()
        from aspire_orchestrator.schemas.memory_v1 import MemoryObjectOut

        stub_obj = _memory_out_stub(MEMORY_A)
        # Build a real MemoryObjectOut from stub (validates schema)
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
        mock_mem_instance.write = AsyncMock(return_value=mem_out)
        mock_mem_instance.list_by_thread = AsyncMock(return_value=([], None))
        mock_mem_instance.list_by_entity = AsyncMock(return_value=[])
        MockMemSvc.return_value = mock_mem_instance
        MockMemSvcPages.return_value = mock_mem_instance

        # ProactiveCandidateEngine stub
        mock_cand_instance = AsyncMock()
        mock_cand_instance.query = AsyncMock(return_value=[])
        MockCandEng.return_value = mock_cand_instance

        # TranscriptEventRefinery stub
        mock_refinery_instance = AsyncMock()
        mock_refinery_instance.refine = AsyncMock(
            return_value=RefineResult(memory_ids=[], candidate_ids=[])
        )
        MockRefinery.return_value = mock_refinery_instance

        # MemorySearchService stub — Pass 5 wires it into routes; tests mock
        # it to return an empty result so route contract behavior is what's
        # being asserted, not the SQL ranker.
        from aspire_orchestrator.schemas.memory_v1 import MemorySearchResponse
        mock_search_instance = AsyncMock()
        mock_search_instance.search = AsyncMock(
            return_value=MemorySearchResponse(items=[], total=0, next_cursor=None)
        )
        MockSearchSvc.return_value = mock_search_instance
        MockSearchSvcPages.return_value = mock_search_instance

        with TestClient(app, raise_server_exceptions=False) as c:
            yield c, {
                "brief_mat": mock_mat_instance,
                "mem_svc": mock_mem_instance,
                "cand_eng": mock_cand_instance,
                "refinery": mock_refinery_instance,
            }


# ---------------------------------------------------------------------------
# Helpers for common payload building
# ---------------------------------------------------------------------------


def _scope_body(tenant_id: str = TENANT_A) -> dict:
    return {
        "tenant_id": tenant_id,
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
    }


def _memory_event_body(tenant_id: str = TENANT_A) -> dict:
    return {
        "tenant_id": tenant_id,
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
        "event_type": "voice_transcript",
        "trace_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "event_at": _now_iso(),
        "idempotency_key": f"idk-{uuid.uuid4()}",
        "risk_tier": "green",
    }


# ===========================================================================
# Law #3 Fail-Closed Tests — Missing headers
# ===========================================================================


class TestScopeHeaderValidation:
    def test_missing_all_headers_returns_401(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post("/v1/memory-events", json=_memory_event_body())
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "SCOPE_MISSING"

    def test_missing_tenant_id_returns_401(self, client_with_mocks):
        client, _ = client_with_mocks
        headers = {"X-Suite-Id": SUITE_A, "X-Office-Id": OFFICE_A}
        resp = client.post("/v1/memory-events", json=_memory_event_body(), headers=headers)
        assert resp.status_code == 401
        detail = resp.json()["detail"]
        assert detail["code"] == "SCOPE_MISSING"
        assert "X-Tenant-Id" in detail["message"]

    def test_missing_suite_id_returns_401(self, client_with_mocks):
        client, _ = client_with_mocks
        headers = {"X-Tenant-Id": TENANT_A, "X-Office-Id": OFFICE_A}
        resp = client.post("/v1/memory-events", json=_memory_event_body(), headers=headers)
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "SCOPE_MISSING"

    def test_missing_office_id_returns_401(self, client_with_mocks):
        client, _ = client_with_mocks
        headers = {"X-Tenant-Id": TENANT_A, "X-Suite-Id": SUITE_A}
        resp = client.post("/v1/memory-events", json=_memory_event_body(), headers=headers)
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "SCOPE_MISSING"


# ===========================================================================
# POST /v1/memory-events — Contract Tests
# ===========================================================================


class TestMemoryEvents:
    def test_valid_envelope_returns_200_with_event_id(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/memory-events",
            json=_memory_event_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "event_id" in data
        assert data["status"] == "pending"
        assert "trace_id" in data

    def test_cross_tenant_attempt_returns_403(self, client_with_mocks):
        client, _ = client_with_mocks
        # scope headers say TENANT_A but envelope body says TENANT_B
        body = _memory_event_body(tenant_id=TENANT_B)
        resp = client.post("/v1/memory-events", json=body, headers=_SCOPE_HEADERS_A)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TENANT_ISOLATION_VIOLATION"

    def test_malformed_envelope_returns_422(self, client_with_mocks):
        client, _ = client_with_mocks
        # Missing required fields: event_type, trace_id, correlation_id, event_at, idempotency_key
        resp = client.post(
            "/v1/memory-events",
            json={"tenant_id": TENANT_A},
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 422


# ===========================================================================
# GET /v1/briefs/office/{office_id} — Contract Tests
# ===========================================================================


class TestBriefOffice:
    def test_returns_office_brief_shape(self, client_with_mocks):
        client, mocks = client_with_mocks
        resp = client.get(
            f"/v1/briefs/office/{OFFICE_A}",
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["office_id"] == OFFICE_A
        assert "brief_text" in data
        assert "freshness_seq" in data

    def test_office_id_mismatch_returns_403(self, client_with_mocks):
        client, _ = client_with_mocks
        other_office = str(uuid.uuid4())
        resp = client.get(
            f"/v1/briefs/office/{other_office}",
            headers=_SCOPE_HEADERS_A,  # X-Office-Id = OFFICE_A, path = other_office
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TENANT_ISOLATION_VIOLATION"

    def test_missing_headers_returns_401(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.get(f"/v1/briefs/office/{OFFICE_A}")
        assert resp.status_code == 401


# ===========================================================================
# GET /v1/briefs/finance/{office_id}
# ===========================================================================


class TestBriefFinance:
    def test_returns_finance_brief_shape(self, client_with_mocks):
        client, mocks = client_with_mocks
        resp = client.get(
            f"/v1/briefs/finance/{OFFICE_A}",
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["office_id"] == OFFICE_A
        assert "provider_health" in data


# ===========================================================================
# GET /v1/briefs/thread/{thread_id}
# ===========================================================================


class TestBriefThread:
    def test_returns_thread_brief_shape(self, client_with_mocks):
        client, mocks = client_with_mocks
        resp = client.get(
            f"/v1/briefs/thread/{THREAD_A}",
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["thread_id"] == THREAD_A
        assert "summary" in data


# ===========================================================================
# POST /v1/session-broker/start
# ===========================================================================


class TestSessionBrokerStart:
    def test_returns_session_id_and_allowed_tools(self, client_with_mocks):
        client, _ = client_with_mocks
        body = {
            "agent_name": "ava",
            "scope": _scope_body(),
            "channel": "voice",
        }
        resp = client.post(
            "/v1/session-broker/start",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "session_id" in data
        assert "allowed_tools" in data
        assert "trace_id" in data

    def test_cross_tenant_body_returns_403(self, client_with_mocks):
        client, _ = client_with_mocks
        body = {
            "agent_name": "ava",
            "scope": _scope_body(tenant_id=TENANT_B),
            "channel": "voice",
        }
        resp = client.post(
            "/v1/session-broker/start",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TENANT_ISOLATION_VIOLATION"


# ===========================================================================
# POST /v1/refinery/run
# ===========================================================================


class TestRefineryRun:
    def test_valid_event_id_returns_refine_result(self, client_with_mocks):
        client, mocks = client_with_mocks
        # Mock returns RefineResult(memory_ids=[], candidate_ids=[])
        body = {"event_id": str(uuid.uuid4())}
        resp = client.post("/v1/refinery/run", json=body, headers=_SCOPE_HEADERS_A)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "memory_ids" in data
        assert "candidate_ids" in data

    def test_refinery_error_returns_422(self, client_with_mocks):
        client, mocks = client_with_mocks
        from aspire_orchestrator.services.transcript_event_refinery import RefineryError

        mocks["refinery"].refine = AsyncMock(
            side_effect=RefineryError(
                "Event not found",
                code="EVENT_NOT_FOUND",
            )
        )
        body = {"event_id": str(uuid.uuid4())}
        resp = client.post("/v1/refinery/run", json=body, headers=_SCOPE_HEADERS_A)
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "EVENT_NOT_FOUND"


# ===========================================================================
# POST /v1/memory/search — stub
# ===========================================================================


class TestMemorySearch:
    def test_returns_empty_results(self, client_with_mocks):
        """Pass 5 wired the route to MemorySearchService; the mocked service
        returns an empty result, so the route returns 200 + empty results."""
        client, _ = client_with_mocks
        body = {"scope": _scope_body(), "q": "test query"}
        resp = client.post("/v1/memory/search", json=body, headers=_SCOPE_HEADERS_A)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["results"] == []
        assert data["total"] == 0

    def test_cross_tenant_returns_403(self, client_with_mocks):
        client, _ = client_with_mocks
        body = {"scope": _scope_body(tenant_id=TENANT_B), "q": "test"}
        resp = client.post("/v1/memory/search", json=body, headers=_SCOPE_HEADERS_A)
        assert resp.status_code == 403


# ===========================================================================
# POST /v1/proactive-candidates/query
# ===========================================================================


class TestProactiveCandidatesQuery:
    def test_returns_empty_list(self, client_with_mocks):
        client, _ = client_with_mocks
        body = {
            "tenant_id": TENANT_A,
            "suite_id": SUITE_A,
            "office_id": OFFICE_A,
        }
        resp = client.post(
            "/v1/proactive-candidates/query",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    def test_cross_tenant_returns_403(self, client_with_mocks):
        client, _ = client_with_mocks
        body = {"tenant_id": TENANT_B, "suite_id": SUITE_A, "office_id": OFFICE_A}
        resp = client.post(
            "/v1/proactive-candidates/query",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403


# ===========================================================================
# POST /v1/receipts/write
# ===========================================================================


class TestReceiptsWrite:
    def test_valid_receipts_returns_200(self, client_with_mocks):
        client, _ = client_with_mocks
        receipt = {
            "id": str(uuid.uuid4()),
            "suite_id": SUITE_A,
            "outcome": "success",
        }
        resp = client.post(
            "/v1/receipts/write",
            json={"receipts": [receipt]},
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["receipt_ids"]) == 1

    def test_mismatched_suite_id_returns_403(self, client_with_mocks):
        client, _ = client_with_mocks
        receipt = {
            "id": str(uuid.uuid4()),
            "suite_id": str(uuid.uuid4()),  # different suite
        }
        resp = client.post(
            "/v1/receipts/write",
            json={"receipts": [receipt]},
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TENANT_ISOLATION_VIOLATION"


# ===========================================================================
# Office Memory Page Routes
# ===========================================================================


def _page_write_body(
    memory_type: str = "session_summary",
    tenant_id: str = TENANT_A,
) -> dict:
    return {
        "scope": _scope_body(tenant_id=tenant_id),
        "summary": f"Test {memory_type} summary",
        "title": f"Test {memory_type}",
        "correlation_id": str(uuid.uuid4()),
        "trace_id": str(uuid.uuid4()),
        "source_agent": "ava",
    }


class TestOfficeMemoryRoutes:
    def test_save_session_summary_returns_memory_id(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/office-memory/save-session-summary",
            json=_page_write_body("session_summary"),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "memory_id" in data
        assert data["memory_id"] == MEMORY_A
        assert data["status"] == "success"

    def test_promote_artifact_returns_promoted_status(self, client_with_mocks):
        client, _ = client_with_mocks
        body = {
            **_page_write_body("artifact_reference"),
            "linked_artifact_ids": [],
        }
        resp = client.post(
            "/v1/office-memory/promote-artifact",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "promoted"
        assert "memory_id" in data

    def test_create_handoff_note_returns_memory_id(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/office-memory/create-handoff-note",
            json=_page_write_body("handoff_note"),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["memory_id"] == MEMORY_A

    def test_get_memory_brief_returns_brief_shape(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/office-memory/get-memory-brief",
            json={"scope": _scope_body()},
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["office_id"] == OFFICE_A
        assert "brief_text" in data

    def test_search_memory_returns_empty_results(self, client_with_mocks):
        """Pass 5 wires this route to MemorySearchService; mocked service
        returns empty result with visibility_scope='office' enforced."""
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/office-memory/search-memory",
            json={"scope": _scope_body(), "q": "test"},
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["results"] == []

    def test_cross_tenant_save_session_summary_returns_403(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/office-memory/save-session-summary",
            json=_page_write_body(tenant_id=TENANT_B),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TENANT_ISOLATION_VIOLATION"

    def test_cross_tenant_promote_artifact_returns_403(self, client_with_mocks):
        client, _ = client_with_mocks
        body = {**_page_write_body(tenant_id=TENANT_B), "linked_artifact_ids": []}
        resp = client.post(
            "/v1/office-memory/promote-artifact",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403

    def test_get_thread_memory_returns_objects_and_brief(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/office-memory/get-thread-memory",
            json={"scope": _scope_body(), "thread_id": THREAD_A},
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "objects" in data
        assert "total" in data


# ===========================================================================
# Finance Memory Page Routes
# ===========================================================================


class TestFinanceMemoryRoutes:
    def test_save_session_summary_returns_memory_id(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/finance-memory/save-session-summary",
            json=_page_write_body("session_summary"),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["memory_id"] == MEMORY_A
        assert data["status"] == "success"

    def test_promote_artifact_returns_promoted(self, client_with_mocks):
        client, _ = client_with_mocks
        body = {**_page_write_body("artifact_reference"), "linked_artifact_ids": []}
        resp = client.post(
            "/v1/finance-memory/promote-artifact",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "promoted"

    def test_create_handoff_note_returns_memory_id(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/finance-memory/create-handoff-note",
            json=_page_write_body("handoff_note"),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        assert "memory_id" in resp.json()

    def test_get_memory_brief_returns_finance_shape(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/finance-memory/get-memory-brief",
            json={"scope": _scope_body()},
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "provider_health" in data

    def test_search_memory_returns_empty_results(self, client_with_mocks):
        """Pass 5 wires this route to MemorySearchService with
        visibility_scope='finance' enforced; mocked service returns empty."""
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/finance-memory/search-memory",
            json={"scope": _scope_body(), "q": "test"},
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["results"] == []

    def test_cross_tenant_returns_403(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/finance-memory/save-session-summary",
            json=_page_write_body(tenant_id=TENANT_B),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403

    def test_get_thread_memory_returns_objects(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/finance-memory/get-thread-memory",
            json={"scope": _scope_body(), "thread_id": THREAD_A},
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200, resp.text
        assert "objects" in resp.json()
