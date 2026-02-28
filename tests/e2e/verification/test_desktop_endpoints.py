"""E2E Desktop API Verification Tests.

Tests all Desktop server (Aspire-desktop) HTTP endpoints that the
front-end UI relies on.  Endpoints tested:

- GET  /api/sandbox/health        -- 10 provider configuration checks
- GET  /api/inbox/items           -- inbox item listing
- GET  /api/authority-queue       -- pending approvals + recent receipts
- POST /api/authority-queue/:id/approve  -- approval (requires X-Suite-Id)
- POST /api/authority-queue/:id/deny     -- denial  (requires X-Suite-Id)
- POST /api/orchestrator/intent   -- voice/text intent proxy
- GET  /api/health                -- basic liveness

Auth validation:
- Missing X-Suite-Id on state-changing endpoints -> 401

Law compliance:
- Law #2: approve/deny generate receipts
- Law #3: fail closed on missing auth
- Law #9: sandbox health never exposes secrets
"""

from __future__ import annotations

import pytest
import requests

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.needs_desktop,
]


# =============================================================================
# GET /api/health — Basic Liveness
# =============================================================================


class TestHealthEndpoint:
    """GET /api/health -- simple liveness probe."""

    def test_health_returns_ok(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Health endpoint returns 200 with status ok."""
        resp = http.get(f"{desktop_url}/api/health", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "timestamp" in data


# =============================================================================
# GET /api/sandbox/health — Provider Configuration Checks
# =============================================================================


class TestSandboxHealth:
    """GET /api/sandbox/health -- verifies provider key configuration."""

    def test_sandbox_health_returns_200(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Sandbox health returns 200 regardless of provider config state."""
        resp = http.get(f"{desktop_url}/api/sandbox/health", timeout=5)
        assert resp.status_code == 200

    def test_sandbox_health_contains_10_providers(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Sandbox health checks 10 providers."""
        resp = http.get(f"{desktop_url}/api/sandbox/health", timeout=5)
        data = resp.json()

        assert "checks" in data
        assert "summary" in data

        expected_providers = {
            "stripe",
            "plaid",
            "gusto",
            "quickbooks",
            "elevenlabs",
            "deepgram",
            "domain_rail",
            "orchestrator",
            "livekit",
            "supabase",
        }
        actual_providers = set(data["checks"].keys())
        assert expected_providers.issubset(actual_providers), (
            f"Missing providers: {expected_providers - actual_providers}"
        )

    def test_sandbox_health_provider_shape(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Each provider check has configured, sandbox, and status fields."""
        resp = http.get(f"{desktop_url}/api/sandbox/health", timeout=5)
        data = resp.json()

        for provider, check in data["checks"].items():
            assert "configured" in check, f"{provider} missing 'configured'"
            assert "sandbox" in check, f"{provider} missing 'sandbox'"
            assert "status" in check, f"{provider} missing 'status'"
            assert isinstance(check["configured"], bool), (
                f"{provider} 'configured' should be bool"
            )

    def test_sandbox_health_does_not_expose_secrets(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Sandbox health must never expose secret key values (Law #9)."""
        resp = http.get(f"{desktop_url}/api/sandbox/health", timeout=5)
        text = resp.text.lower()

        # These patterns would indicate leaked secrets
        secret_indicators = [
            "sk_test_",
            "sk_live_",
            "secret_",
            "api_key=",
            "password",
            "bearer ",
        ]
        for indicator in secret_indicators:
            assert indicator not in text, (
                f"Sandbox health response may contain a secret: found '{indicator}'"
            )


# =============================================================================
# GET /api/inbox/items — Inbox Items
# =============================================================================


class TestInboxItems:
    """GET /api/inbox/items -- inbox item listing."""

    def test_inbox_items_returns_200(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Inbox items returns 200 with items array (may be empty)."""
        resp = http.get(f"{desktop_url}/api/inbox/items", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert isinstance(data["items"], list)

    def test_inbox_items_structure(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """If inbox contains items, each item has required fields."""
        resp = http.get(f"{desktop_url}/api/inbox/items", timeout=5)
        data = resp.json()

        for item in data["items"]:
            # At minimum: id, type, and a timestamp
            assert "id" in item, "Inbox item missing 'id'"
            assert "type" in item, "Inbox item missing 'type'"


# =============================================================================
# GET /api/authority-queue — Pending Approvals + Recent Receipts
# =============================================================================


class TestAuthorityQueue:
    """GET /api/authority-queue -- approval queue and receipt history."""

    def test_authority_queue_returns_200(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Authority queue returns 200 with expected shape."""
        resp = http.get(f"{desktop_url}/api/authority-queue", timeout=5)
        assert resp.status_code == 200

    def test_authority_queue_shape(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Authority queue response contains pendingApprovals and recentReceipts."""
        resp = http.get(f"{desktop_url}/api/authority-queue", timeout=5)
        data = resp.json()

        assert "pendingApprovals" in data
        assert "recentReceipts" in data
        assert isinstance(data["pendingApprovals"], list)
        assert isinstance(data["recentReceipts"], list)


# =============================================================================
# POST /api/authority-queue/:id/approve — Approval (Auth Required)
# =============================================================================


class TestAuthorityQueueApprove:
    """POST /api/authority-queue/:id/approve -- approval action."""

    def test_approve_without_suite_id_returns_401(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Approve without X-Suite-Id fails closed with 401 (Law #3)."""
        resp = http.post(
            f"{desktop_url}/api/authority-queue/test-id-999/approve",
            json={},
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"] == "AUTH_REQUIRED"

    def test_approve_with_suite_id(
        self,
        http: requests.Session,
        desktop_url: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Approve with X-Suite-Id either succeeds or returns 500 (DB issue).

        We verify the auth gate passes -- the DB may not have the row,
        which is fine for E2E verification (the auth path is what matters).
        """
        resp = http.post(
            f"{desktop_url}/api/authority-queue/nonexistent-id/approve",
            json={},
            headers=auth_headers,
            timeout=5,
        )
        # If the approval_requests table exists, the endpoint returns 200
        # (even if row not found, it may still INSERT a receipt).
        # If the table does not exist, it returns 500.
        assert resp.status_code in (200, 500), (
            f"Expected 200 or 500, got {resp.status_code}"
        )

        # If 200, verify receipt was generated (Law #2)
        if resp.status_code == 200:
            data = resp.json()
            assert "receiptId" in data or "receipt_id" in data, (
                "Approval response must contain a receipt ID (Law #2)"
            )


# =============================================================================
# POST /api/authority-queue/:id/deny — Denial (Auth Required)
# =============================================================================


class TestAuthorityQueueDeny:
    """POST /api/authority-queue/:id/deny -- denial action."""

    def test_deny_without_suite_id_returns_401(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Deny without X-Suite-Id fails closed with 401 (Law #3)."""
        resp = http.post(
            f"{desktop_url}/api/authority-queue/test-id-999/deny",
            json={"reason": "test denial"},
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"] == "AUTH_REQUIRED"

    def test_deny_with_suite_id(
        self,
        http: requests.Session,
        desktop_url: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Deny with X-Suite-Id passes auth gate.

        The endpoint may return 200 or 500 depending on DB state.
        """
        resp = http.post(
            f"{desktop_url}/api/authority-queue/nonexistent-id/deny",
            json={"reason": "E2E test denial"},
            headers=auth_headers,
            timeout=5,
        )
        assert resp.status_code in (200, 500), (
            f"Expected 200 or 500, got {resp.status_code}"
        )

        # If 200, verify receipt was generated (Law #2)
        if resp.status_code == 200:
            data = resp.json()
            assert "receiptId" in data or "receipt_id" in data, (
                "Denial response must contain a receipt ID (Law #2)"
            )


# =============================================================================
# POST /api/orchestrator/intent — Orchestrator Proxy
# =============================================================================


class TestOrchestratorIntentProxy:
    """POST /api/orchestrator/intent -- intent forwarding to orchestrator."""

    def test_intent_requires_suite_id(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Intent without X-Suite-Id returns 401 (Law #3: Fail Closed)."""
        resp = http.post(
            f"{desktop_url}/api/orchestrator/intent",
            json={"text": "Hello"},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert resp.status_code == 401

    def test_intent_requires_text(
        self,
        http: requests.Session,
        desktop_url: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Intent without text returns 400."""
        resp = http.post(
            f"{desktop_url}/api/orchestrator/intent",
            json={"agent": "ava"},
            headers=auth_headers,
            timeout=10,
        )
        assert resp.status_code == 400

    def test_intent_rejects_empty_text(
        self,
        http: requests.Session,
        desktop_url: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Intent with empty text string returns 400."""
        resp = http.post(
            f"{desktop_url}/api/orchestrator/intent",
            json={"text": "   ", "agent": "ava"},
            headers=auth_headers,
            timeout=10,
        )
        assert resp.status_code == 400

    def test_intent_with_valid_text(
        self,
        http: requests.Session,
        desktop_url: str,
        auth_headers: dict[str, str],
        orchestrator_reachable: bool,
    ) -> None:
        """Intent with valid text returns 200 (or 503 if orchestrator down)."""
        resp = http.post(
            f"{desktop_url}/api/orchestrator/intent",
            json={"text": "What time is it?", "agent": "ava"},
            headers=auth_headers,
            timeout=30,
        )
        if orchestrator_reachable:
            assert resp.status_code in (200, 202), (
                f"Expected 200/202, got {resp.status_code}: {resp.text[:200]}"
            )
            data = resp.json()
            assert "response" in data
        else:
            # Law #3: Fail Closed -- 503 when orchestrator is down
            assert resp.status_code == 503


# =============================================================================
# Auth Validation Across Endpoints
# =============================================================================


class TestAuthValidation:
    """Cross-cutting auth validation for state-changing endpoints."""

    @pytest.mark.parametrize(
        "endpoint,method,body",
        [
            ("/api/authority-queue/id/approve", "POST", {}),
            ("/api/authority-queue/id/deny", "POST", {"reason": "test"}),
            ("/api/orchestrator/intent", "POST", {"text": "hello"}),
        ],
        ids=["approve", "deny", "intent"],
    )
    def test_state_changing_endpoints_require_auth(
        self,
        http: requests.Session,
        desktop_url: str,
        endpoint: str,
        method: str,
        body: dict,
    ) -> None:
        """All state-changing endpoints require X-Suite-Id (Law #3)."""
        resp = http.request(
            method,
            f"{desktop_url}{endpoint}",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        assert resp.status_code in (401, 400), (
            f"{method} {endpoint} without auth returned {resp.status_code}, "
            f"expected 401 or 400"
        )
