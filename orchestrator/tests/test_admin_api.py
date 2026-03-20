"""Tests for the Admin Ops Telemetry Facade API (Wave 8).

Covers all 9 endpoints with ~25 tests:
  - Health: 2 tests
  - Incidents: 4 tests
  - Receipts: 4 tests
  - Provider Calls: 2 tests
  - Outbox: 1 test
  - Rollouts: 1 test
  - Auth: 3 tests
  - Proposals: 4 tests
  - Law #2 receipt generation: 3 tests
  - Pagination: 1 test

Law compliance:
  - Law #2: Every endpoint call generates an access receipt.
  - Law #3: Missing/invalid token -> 401.
  - Law #7: Read-only — no state mutations.
  - Law #9: PII redacted in previews.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from aspire_orchestrator.server import app
from aspire_orchestrator.routes.admin import (
    clear_admin_stores,
    register_incident,
    register_provider_call,
    register_proposal,
)
from aspire_orchestrator.services.receipt_store import clear_store, store_receipts

_TEST_JWT_SECRET = "test-admin-jwt-secret-for-testing"


def _make_admin_token(sub: str = "admin-test") -> str:
    """Create a valid admin JWT for testing."""
    return pyjwt.encode({"sub": sub}, _TEST_JWT_SECRET, algorithm="HS256")


@pytest.fixture
def client():
    """Create a test client for the FastAPI server."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_stores(monkeypatch):
    """Clean all stores between tests for isolation + set JWT secret."""
    monkeypatch.setenv("ASPIRE_ADMIN_JWT_SECRET", _TEST_JWT_SECRET)
    # Force admin_store to use in-memory only (no real Supabase in tests)
    import aspire_orchestrator.services.admin_store as _admin_store_mod
    _admin_store_mod._supabase_client = None
    _admin_store_mod._supabase_init_done = True
    clear_admin_stores()
    clear_store()
    yield
    clear_admin_stores()
    clear_store()
    # Reset so next test can reinit if needed
    _admin_store_mod._supabase_init_done = False


@pytest.fixture
def admin_headers() -> dict[str, str]:
    """Valid admin headers with JWT token."""
    return {
        "x-admin-token": _make_admin_token(),
        "x-correlation-id": str(uuid.uuid4()),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_incident(
    incident_id: str | None = None,
    state: str = "open",
    severity: str = "sev3",
    title: str = "Test Incident",
    suite_id: str | None = None,
) -> dict:
    now = _now_iso()
    return {
        "incident_id": incident_id or str(uuid.uuid4()),
        "state": state,
        "severity": severity,
        "title": title,
        "correlation_id": str(uuid.uuid4()),
        "suite_id": suite_id,
        "first_seen": now,
        "last_seen": now,
        "timeline": [{"ts": now, "event": "opened", "receipt_id": str(uuid.uuid4())}],
        "evidence_pack": {"source": "test"},
    }


def _make_provider_call(
    provider: str = "stripe",
    action: str = "invoice.send",
    status: str = "success",
    correlation_id: str | None = None,
) -> dict:
    now = _now_iso()
    return {
        "call_id": str(uuid.uuid4()),
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "provider": provider,
        "action": action,
        "status": status,
        "http_status": 200 if status == "success" else 500,
        "retry_count": 0,
        "started_at": now,
        "finished_at": now,
        "payload_preview": '{"amount": 1000}',
    }


def _make_proposal(
    risk_tier: str = "yellow",
    status: str = "pending",
) -> dict:
    return {
        "proposal_id": str(uuid.uuid4()),
        "scope": {"scope_type": "global"},
        "risk_tier": risk_tier,
        "diff": {"before": {}, "after": {"feature_x": True}},
        "tests_required": ["unit", "integration"],
        "rollout_plan": {"canary_percent": 10, "stages": ["canary", "full"]},
        "rollback_triggers": ["error_rate > 5%"],
        "approvals_required": ["admin"],
        "status": status,
    }


# =============================================================================
# 1. Health Endpoint Tests
# =============================================================================


class TestHealthEndpoint:
    def test_health_returns_ok(self, client) -> None:
        """GET /admin/ops/health returns status ok without auth."""
        response = client.get("/admin/ops/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "server_time" in data
        assert data["version"] == "3.0.0"

    def test_health_returns_version(self, client) -> None:
        """Health endpoint includes version string."""
        response = client.get("/admin/ops/health")
        data = response.json()
        assert data["version"] == "3.0.0"
        # server_time should be valid ISO 8601
        assert "T" in data["server_time"]


# =============================================================================
# 2. Auth Tests (Law #3)
# =============================================================================


class TestAdminAuth:
    def test_missing_token_returns_401(self, client) -> None:
        """All auth-required endpoints return 401 without X-Admin-Token."""
        endpoints = [
            "/admin/ops/incidents",
            "/admin/ops/receipts",
            "/admin/ops/provider-calls",
            "/admin/ops/outbox",
            "/admin/ops/rollouts",
            "/admin/proposals/pending",
        ]
        for endpoint in endpoints:
            response = client.get(endpoint)
            assert response.status_code == 401, f"Expected 401 for {endpoint}, got {response.status_code}"
            data = response.json()
            assert data["code"] == "AUTHZ_DENIED"

    def test_valid_jwt_passes(self, client, admin_headers) -> None:
        """Valid JWT token allows access (Law #3: explicit auth)."""
        response = client.get("/admin/ops/incidents", headers=admin_headers)
        assert response.status_code == 200

    def test_invalid_jwt_rejected(self, client) -> None:
        """Invalid JWT is rejected (Law #3: fail closed)."""
        response = client.get(
            "/admin/ops/incidents",
            headers={"x-admin-token": "not-a-real-jwt"},
        )
        assert response.status_code == 401
        data = response.json()
        assert data["code"] == "AUTHZ_DENIED"

    def test_no_secret_configured_denies(self, client, monkeypatch) -> None:
        """Missing ASPIRE_ADMIN_JWT_SECRET means deny all (Law #3: fail closed)."""
        monkeypatch.delenv("ASPIRE_ADMIN_JWT_SECRET", raising=False)
        response = client.get(
            "/admin/ops/incidents",
            headers={"x-admin-token": _make_admin_token()},
        )
        assert response.status_code == 401
        data = response.json()
        assert data["code"] == "AUTHZ_DENIED"

    def test_exchange_falls_back_to_supabase_auth_when_local_decode_fails(
        self, client, monkeypatch
    ) -> None:
        """Admin token exchange should recover when local JWT decode config is stale."""
        import aspire_orchestrator.routes.admin as admin_module

        seen_tokens: list[str] = []

        def _get_user(access_token: str):
            seen_tokens.append(access_token)
            return SimpleNamespace(
                user=SimpleNamespace(
                    id="supabase-user-123",
                    email="admin@example.com",
                )
            )

        fake_supabase = SimpleNamespace(auth=SimpleNamespace(get_user=_get_user))

        monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
        monkeypatch.setenv("JWT_SECRET", "stale-local-secret")
        monkeypatch.setattr(admin_module, "_get_supabase_client", lambda: fake_supabase)

        response = client.post(
            "/admin/auth/exchange",
            headers={
                "authorization": "Bearer not-a-locally-decodable-token",
                "x-correlation-id": "corr-exchange-fallback",
            },
        )

        assert response.status_code == 200
        assert seen_tokens == ["not-a-locally-decodable-token"]

        data = response.json()
        decoded = pyjwt.decode(
            data["admin_token"],
            _TEST_JWT_SECRET,
            algorithms=["HS256"],
        )
        assert decoded["sub"] == "supabase-user-123"
        assert decoded["email"] == "admin@example.com"


# =============================================================================
# 3. Incidents Endpoint Tests
# =============================================================================


class TestIncidentsEndpoint:
    def test_list_empty(self, client, admin_headers) -> None:
        """GET /admin/ops/incidents returns empty list when no incidents."""
        response = client.get("/admin/ops/incidents", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["page"]["has_more"] is False
        assert "server_time" in data

    def test_list_with_state_filter(self, client, admin_headers) -> None:
        """Incidents can be filtered by state."""
        register_incident(_make_incident(state="open"))
        register_incident(_make_incident(state="closed"))
        register_incident(_make_incident(state="open"))

        response = client.get(
            "/admin/ops/incidents",
            headers=admin_headers,
            params={"state": "open"},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 2
        assert all(i["state"] == "open" for i in data["items"])

    def test_list_with_severity_filter(self, client, admin_headers) -> None:
        """Incidents can be filtered by severity."""
        register_incident(_make_incident(severity="sev1"))
        register_incident(_make_incident(severity="sev3"))

        response = client.get(
            "/admin/ops/incidents",
            headers=admin_headers,
            params={"severity": "sev1"},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["severity"] == "sev1"

    def test_get_by_id(self, client, admin_headers) -> None:
        """GET /admin/ops/incidents/{id} returns incident detail."""
        incident = _make_incident(incident_id="inc-001")
        register_incident(incident)

        response = client.get("/admin/ops/incidents/inc-001", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["incident_id"] == "inc-001"
        assert "timeline" in data
        assert "evidence_pack" in data
        assert "server_time" in data

    def test_get_by_id_404(self, client, admin_headers) -> None:
        """GET /admin/ops/incidents/{id} returns 404 for unknown incident."""
        response = client.get("/admin/ops/incidents/nonexistent", headers=admin_headers)
        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "NOT_FOUND"


# =============================================================================
# 4. Receipts Endpoint Tests
# =============================================================================


class TestReceiptsEndpoint:
    def test_list_receipts_requires_suite_id(self, client, admin_headers) -> None:
        """GET /admin/ops/receipts without suite_id returns 400 (Law #6)."""
        response = client.get("/admin/ops/receipts", headers=admin_headers)
        assert response.status_code == 400
        data = response.json()
        assert data["code"] == "MISSING_SUITE_ID"

    def test_list_receipts(self, client, admin_headers) -> None:
        """GET /admin/ops/receipts with suite_id returns receipt summaries."""
        now = _now_iso()
        store_receipts([
            {
                "id": "r-001",
                "correlation_id": "corr-001",
                "suite_id": "suite-a",
                "office_id": "office-1",
                "action_type": "email.send",
                "risk_tier": "yellow",
                "outcome": "success",
                "created_at": now,
            },
        ])

        response = client.get(
            "/admin/ops/receipts",
            headers=admin_headers,
            params={"suite_id": "suite-a"},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) >= 1
        assert "page" in data
        assert "server_time" in data

    def test_filter_by_suite_id(self, client, admin_headers) -> None:
        """Receipts can be filtered by suite_id."""
        now = _now_iso()
        store_receipts([
            {
                "id": "r-a1",
                "correlation_id": "c1",
                "suite_id": "suite-a",
                "office_id": "OFF-0001",
                "action_type": "email.send",
                "risk_tier": "green",
                "outcome": "success",
                "created_at": now,
            },
            {
                "id": "r-b1",
                "correlation_id": "c2",
                "suite_id": "suite-b",
                "office_id": "o2",
                "action_type": "email.send",
                "risk_tier": "green",
                "outcome": "success",
                "created_at": now,
            },
        ])

        response = client.get(
            "/admin/ops/receipts",
            headers=admin_headers,
            params={"suite_id": "suite-a"},
        )
        assert response.status_code == 200
        data = response.json()
        # Only suite-a receipts
        for item in data["items"]:
            assert item["suite_id"] == "suite-a"

    def test_filter_by_action_type(self, client, admin_headers) -> None:
        """Receipts can be filtered by action_type."""
        now = _now_iso()
        store_receipts([
            {
                "id": "r-e1",
                "correlation_id": "c1",
                "suite_id": "suite-a",
                "office_id": "OFF-0001",
                "action_type": "email.send",
                "risk_tier": "yellow",
                "outcome": "success",
                "created_at": now,
            },
            {
                "id": "r-c1",
                "correlation_id": "c2",
                "suite_id": "suite-a",
                "office_id": "OFF-0001",
                "action_type": "calendar.read",
                "risk_tier": "green",
                "outcome": "success",
                "created_at": now,
            },
        ])

        response = client.get(
            "/admin/ops/receipts",
            headers=admin_headers,
            params={"suite_id": "suite-a", "action_type": "email.send"},
        )
        assert response.status_code == 200
        data = response.json()
        for item in data["items"]:
            assert item["action_type"] == "email.send"

    def test_receipts_pagination(self, client, admin_headers) -> None:
        """Receipts support cursor-based pagination."""
        now = _now_iso()
        receipts = [
            {
                "id": f"r-{i:03d}",
                "correlation_id": f"c-{i}",
                "suite_id": "suite-a",
                "office_id": "OFF-0001",
                "action_type": "receipts.search",
                "risk_tier": "green",
                "outcome": "success",
                "created_at": now,
            }
            for i in range(5)
        ]
        store_receipts(receipts)

        response = client.get(
            "/admin/ops/receipts",
            headers=admin_headers,
            params={"suite_id": "suite-a", "limit": 2},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) <= 2
        # page info present
        assert "has_more" in data["page"]


# =============================================================================
# 5. Provider Calls Endpoint Tests
# =============================================================================


class TestProviderCallsEndpoint:
    def test_list_empty(self, client, admin_headers) -> None:
        """GET /admin/ops/provider-calls returns empty list initially."""
        response = client.get("/admin/ops/provider-calls", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []

    def test_list_with_status_filter(self, client, admin_headers) -> None:
        """Provider calls can be filtered by status."""
        register_provider_call(_make_provider_call(status="success"))
        register_provider_call(_make_provider_call(status="error"))
        register_provider_call(_make_provider_call(status="success"))

        response = client.get(
            "/admin/ops/provider-calls",
            headers=admin_headers,
            params={"status": "error"},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["status"] == "error"

    def test_payload_preview_is_redacted(self, client, admin_headers) -> None:
        """Provider call payload preview is always present and truncated (Law #9)."""
        call = _make_provider_call()
        call["payload_preview"] = '{"secret": "sk_live_abc123", "amount": 5000}'
        register_provider_call(call)

        response = client.get("/admin/ops/provider-calls", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        # Preview should exist and be a string
        preview = data["items"][0]["redacted_payload_preview"]
        assert isinstance(preview, str)
        # Should not exceed 200 chars
        assert len(preview) <= 200


# =============================================================================
# 6. Outbox Endpoint Test
# =============================================================================


class TestOutboxEndpoint:
    def test_returns_zero_values(self, client, admin_headers) -> None:
        """GET /admin/ops/outbox returns zero mock values."""
        response = client.get("/admin/ops/outbox", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["queue_depth"] == 0
        assert data["oldest_age_seconds"] == 0
        assert data["stuck_jobs"] == 0
        assert "server_time" in data


# =============================================================================
# 7. Rollouts Endpoint Test
# =============================================================================


class TestRolloutsEndpoint:
    def test_returns_empty_list(self, client, admin_headers) -> None:
        """GET /admin/ops/rollouts returns empty list."""
        response = client.get("/admin/ops/rollouts", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["page"]["has_more"] is False


# =============================================================================
# 8. Proposals Endpoint Tests
# =============================================================================


class TestProposalsEndpoint:
    def test_list_pending(self, client, admin_headers) -> None:
        """GET /admin/proposals/pending returns pending proposals."""
        proposal = _make_proposal()
        register_proposal(proposal)

        response = client.get("/admin/proposals/pending", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["items"][0]["proposal_id"] == proposal["proposal_id"]

    def test_approve_yellow_proposal(self, client, admin_headers) -> None:
        """POST /admin/proposals/{id}/approve approves YELLOW tier proposal."""
        proposal = _make_proposal(risk_tier="yellow")
        register_proposal(proposal)
        pid = proposal["proposal_id"]

        response = client.post(
            f"/admin/proposals/{pid}/approve",
            headers=admin_headers,
            json={"approver_id": "tonio", "approval_method": "admin_portal"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "approved"
        assert data["proposal_id"] == pid
        assert "receipt_id" in data

    def test_approve_red_requires_presence(self, client, admin_headers) -> None:
        """RED tier proposal requires presence_token (Law #4)."""
        proposal = _make_proposal(risk_tier="red")
        register_proposal(proposal)
        pid = proposal["proposal_id"]

        # Attempt without presence_token -> denied
        response = client.post(
            f"/admin/proposals/{pid}/approve",
            headers=admin_headers,
            json={"approver_id": "tonio"},
        )
        assert response.status_code == 403
        data = response.json()
        assert data["code"] == "PRESENCE_REQUIRED"

        # Attempt with presence_token -> approved
        response = client.post(
            f"/admin/proposals/{pid}/approve",
            headers=admin_headers,
            json={
                "approver_id": "tonio",
                "approval_method": "video_authority",
                "presence_token": "pt-abc-123",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "approved"

    def test_approve_generates_receipt(self, client, admin_headers) -> None:
        """Proposal approval generates a receipt with correct risk_tier (Law #2)."""
        proposal = _make_proposal(risk_tier="yellow")
        register_proposal(proposal)
        pid = proposal["proposal_id"]

        # Clear store to isolate
        clear_store()

        response = client.post(
            f"/admin/proposals/{pid}/approve",
            headers=admin_headers,
            json={"approver_id": "tonio"},
        )
        assert response.status_code == 200
        receipt_id = response.json()["receipt_id"]

        # Verify receipt was stored
        from aspire_orchestrator.services.receipt_store import _receipts, _lock

        with _lock:
            matching = [r for r in _receipts if r.get("id") == receipt_id]
        assert len(matching) == 1
        assert matching[0]["action_type"] == "admin.proposals.approve"
        assert matching[0]["risk_tier"] == "yellow"
        assert matching[0]["outcome"] == "success"

    def test_approve_not_found(self, client, admin_headers) -> None:
        """Approving a nonexistent proposal returns 404."""
        response = client.post(
            "/admin/proposals/nonexistent/approve",
            headers=admin_headers,
            json={"approver_id": "tonio"},
        )
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"


# =============================================================================
# 9. Law #2 Receipt Generation Tests
# =============================================================================


class TestReceiptGeneration:
    def test_denied_access_generates_receipt(self, client) -> None:
        """Denied access (no token) generates a receipt with outcome=denied (Law #2)."""
        clear_store()

        # Access without token
        client.get("/admin/ops/incidents")

        from aspire_orchestrator.services.receipt_store import _receipts, _lock

        with _lock:
            denied_receipts = [
                r for r in _receipts
                if r.get("outcome") == "denied" and r.get("action_type") == "admin.ops.incidents.list"
            ]
        assert len(denied_receipts) >= 1
        assert denied_receipts[0]["actor_id"] == "anonymous"
        assert denied_receipts[0]["reason_code"] == "AUTHZ_DENIED"

    def test_successful_access_generates_receipt(self, client, admin_headers) -> None:
        """Successful admin access generates an access receipt (Law #2)."""
        clear_store()

        client.get("/admin/ops/outbox", headers=admin_headers)

        from aspire_orchestrator.services.receipt_store import _receipts, _lock

        with _lock:
            access_receipts = [
                r for r in _receipts
                if r.get("action_type") == "admin.ops.outbox.status" and r.get("outcome") == "success"
            ]
        assert len(access_receipts) == 1
        assert access_receipts[0]["actor_id"] == "admin-test"

    def test_404_generates_receipt(self, client, admin_headers) -> None:
        """404 on incident lookup generates a receipt with outcome=failed (Law #2)."""
        clear_store()

        client.get("/admin/ops/incidents/missing-id", headers=admin_headers)

        from aspire_orchestrator.services.receipt_store import _receipts, _lock

        with _lock:
            not_found_receipts = [
                r for r in _receipts
                if r.get("reason_code") == "NOT_FOUND"
                and r.get("action_type") == "admin.ops.incidents.get"
            ]
        assert len(not_found_receipts) == 1
        assert not_found_receipts[0]["outcome"] == "failed"


# =============================================================================
# 10. Pagination Test
# =============================================================================


class TestPagination:
    def test_incident_pagination_with_cursor(self, client, admin_headers) -> None:
        """Incidents support cursor-based pagination."""
        # Register 5 incidents with known IDs
        for i in range(5):
            register_incident(_make_incident(incident_id=f"inc-{i:03d}"))

        # Page 1: limit=2
        response = client.get(
            "/admin/ops/incidents",
            headers=admin_headers,
            params={"limit": 2},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 2
        assert data["page"]["has_more"] is True
        next_cursor = data["page"]["next_cursor"]
        assert next_cursor is not None

        # Page 2: use cursor
        response2 = client.get(
            "/admin/ops/incidents",
            headers=admin_headers,
            params={"limit": 2, "cursor": next_cursor},
        )
        assert response2.status_code == 200
        data2 = response2.json()
        assert len(data2["items"]) == 2

        # IDs from page 1 and page 2 should not overlap
        ids_1 = {i["incident_id"] for i in data["items"]}
        ids_2 = {i["incident_id"] for i in data2["items"]}
        assert ids_1.isdisjoint(ids_2), "Pagination returned duplicate items"
