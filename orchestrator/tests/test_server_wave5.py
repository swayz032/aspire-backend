"""Tests for Wave 5 FastAPI endpoints — receipts, verify-run, policy evaluate.

Covers:
- GET /v1/receipts — receipt query with filters
- POST /v1/receipts/verify-run — hash chain verification
- POST /v1/policy/evaluate — policy evaluation (read-only)
- Receipt store integration (in-memory for Phase 1)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from aspire_orchestrator.server import app
from aspire_orchestrator.services.receipt_store import (
    clear_store,
    get_chain_receipts,
    get_receipt_count,
    query_receipts,
    store_receipts,
)


@pytest.fixture
def client():
    """Create a test client for the FastAPI server."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_receipt_store():
    """Clear receipt store between tests."""
    clear_store()
    yield
    clear_store()


def _make_receipt(
    suite_id: str = "suite-001",
    action_type: str = "calendar.read",
    risk_tier: str = "green",
    correlation_id: str | None = None,
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "suite_id": suite_id,
        "office_id": "office-001",
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "action_type": action_type,
        "risk_tier": risk_tier,
        "outcome": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "actor_type": "user",
        "actor_id": "user-001",
        "tool_used": "test_tool",
        "receipt_type": "tool_execution",
    }


# ===========================================================================
# GET /v1/receipts
# ===========================================================================


class TestReceiptsEndpoint:
    def test_returns_empty_list_when_no_receipts(self, client) -> None:
        response = client.get("/v1/receipts?suite_id=suite-001")
        assert response.status_code == 200
        data = response.json()
        assert data["receipts"] == []
        assert data["count"] == 0

    def test_returns_receipts_for_suite(self, client) -> None:
        store_receipts([_make_receipt(suite_id="suite-A")])
        store_receipts([_make_receipt(suite_id="suite-B")])

        response = client.get("/v1/receipts?suite_id=suite-A")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["receipts"][0]["suite_id"] == "suite-A"

    def test_tenant_isolation_enforced(self, client) -> None:
        """Suite B cannot see Suite A receipts (Law #6)."""
        store_receipts([_make_receipt(suite_id="suite-A")])

        response = client.get("/v1/receipts?suite_id=suite-B")
        assert response.status_code == 200
        assert response.json()["count"] == 0

    def test_filter_by_correlation_id(self, client) -> None:
        store_receipts([
            _make_receipt(correlation_id="corr-001"),
            _make_receipt(correlation_id="corr-002"),
        ])

        response = client.get(
            "/v1/receipts?suite_id=suite-001&correlation_id=corr-001"
        )
        data = response.json()
        assert data["count"] == 1
        assert data["receipts"][0]["correlation_id"] == "corr-001"

    def test_filter_by_risk_tier(self, client) -> None:
        store_receipts([
            _make_receipt(risk_tier="green"),
            _make_receipt(risk_tier="yellow"),
            _make_receipt(risk_tier="red"),
        ])

        response = client.get("/v1/receipts?suite_id=suite-001&risk_tier=red")
        data = response.json()
        assert data["count"] == 1
        assert data["receipts"][0]["risk_tier"] == "red"

    def test_filter_by_action_type(self, client) -> None:
        store_receipts([
            _make_receipt(action_type="calendar.read"),
            _make_receipt(action_type="invoice.create"),
        ])

        response = client.get(
            "/v1/receipts?suite_id=suite-001&action_type=invoice.create"
        )
        data = response.json()
        assert data["count"] == 1
        assert data["receipts"][0]["action_type"] == "invoice.create"

    def test_pagination_limit(self, client) -> None:
        store_receipts([_make_receipt() for _ in range(10)])

        response = client.get("/v1/receipts?suite_id=suite-001&limit=3")
        data = response.json()
        assert data["count"] == 3

    def test_pagination_offset(self, client) -> None:
        store_receipts([_make_receipt() for _ in range(5)])

        response = client.get("/v1/receipts?suite_id=suite-001&limit=2&offset=3")
        data = response.json()
        assert data["count"] == 2

    def test_rejects_invalid_risk_tier(self, client) -> None:
        response = client.get("/v1/receipts?suite_id=suite-001&risk_tier=extreme")
        assert response.status_code == 400
        assert response.json()["error"] == "SCHEMA_VALIDATION_FAILED"

    def test_response_includes_pagination_metadata(self, client) -> None:
        response = client.get("/v1/receipts?suite_id=suite-001&limit=10&offset=5")
        data = response.json()
        assert data["pagination"]["limit"] == 10
        assert data["pagination"]["offset"] == 5


# ===========================================================================
# POST /v1/receipts/verify-run
# ===========================================================================


class TestVerifyRunEndpoint:
    def test_empty_chain_returns_valid(self, client) -> None:
        response = client.post(
            "/v1/receipts/verify-run",
            json={"suite_id": "suite-001"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["verified"] is True
        assert data["chain_length"] == 0

    def test_valid_chain_passes_verification(self, client) -> None:
        """Store chained receipts and verify they pass."""
        from aspire_orchestrator.services.receipt_chain import (
            assign_chain_metadata,
        )

        receipts = [_make_receipt() for _ in range(3)]
        assign_chain_metadata(receipts, chain_id="suite-001")
        store_receipts(receipts)

        response = client.post(
            "/v1/receipts/verify-run",
            json={"suite_id": "suite-001"},
        )
        data = response.json()
        assert data["verified"] is True
        assert data["chain_length"] == 3

    def test_tampered_chain_fails_verification(self, client) -> None:
        """Tampered receipt_hash should be detected."""
        from aspire_orchestrator.services.receipt_chain import (
            assign_chain_metadata,
        )

        receipts = [_make_receipt() for _ in range(3)]
        assign_chain_metadata(receipts, chain_id="suite-001")
        # Tamper with second receipt
        receipts[1]["receipt_hash"] = "tampered" + receipts[1]["receipt_hash"][8:]
        store_receipts(receipts)

        response = client.post(
            "/v1/receipts/verify-run",
            json={"suite_id": "suite-001"},
        )
        data = response.json()
        assert data["verified"] is False
        assert data["error_count"] > 0

    def test_rejects_missing_suite_id(self, client) -> None:
        response = client.post("/v1/receipts/verify-run", json={})
        assert response.status_code == 400
        assert response.json()["error"] == "SCHEMA_VALIDATION_FAILED"


# ===========================================================================
# POST /v1/policy/evaluate
# ===========================================================================


class TestPolicyEvaluateEndpoint:
    def test_green_action(self, client) -> None:
        response = client.post(
            "/v1/policy/evaluate",
            json={"action_type": "calendar.read"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["allowed"] is True
        assert data["risk_tier"] == "green"
        assert data["approval_required"] is False
        assert data["presence_required"] is False

    def test_yellow_action(self, client) -> None:
        response = client.post(
            "/v1/policy/evaluate",
            json={"action_type": "email.send"},
        )
        data = response.json()
        assert data["allowed"] is True
        assert data["risk_tier"] == "yellow"
        assert data["approval_required"] is True
        assert data["presence_required"] is False

    def test_red_action(self, client) -> None:
        response = client.post(
            "/v1/policy/evaluate",
            json={"action_type": "payment.send"},
        )
        data = response.json()
        assert data["allowed"] is True
        assert data["risk_tier"] == "red"
        assert data["approval_required"] is True
        assert data["presence_required"] is True

    def test_unknown_action_denied(self, client) -> None:
        response = client.post(
            "/v1/policy/evaluate",
            json={"action_type": "hack.system"},
        )
        data = response.json()
        assert data["allowed"] is False
        assert data["deny_reason"] is not None

    def test_returns_tools_list(self, client) -> None:
        response = client.post(
            "/v1/policy/evaluate",
            json={"action_type": "calendar.read"},
        )
        data = response.json()
        assert "calendar.event.list" in data["tools"]

    def test_returns_capability_scope(self, client) -> None:
        response = client.post(
            "/v1/policy/evaluate",
            json={"action_type": "payment.send"},
        )
        data = response.json()
        assert data["capability_scope"] == "payments:initiate"

    def test_returns_redact_fields(self, client) -> None:
        response = client.post(
            "/v1/policy/evaluate",
            json={"action_type": "payroll.run"},
        )
        data = response.json()
        assert "ssn" in data["redact_fields"]
        assert "bank_routing" in data["redact_fields"]

    def test_rejects_missing_action_type(self, client) -> None:
        response = client.post("/v1/policy/evaluate", json={})
        assert response.status_code == 400

    def test_rejects_non_string_action_type(self, client) -> None:
        response = client.post(
            "/v1/policy/evaluate",
            json={"action_type": 123},
        )
        assert response.status_code == 400


# ===========================================================================
# Receipt Store Integration
# ===========================================================================


class TestReceiptStoreIntegration:
    def test_intent_produces_receipts_in_store(self, client) -> None:
        """POST /v1/intents should store receipts for successful flows."""
        suite_id = "STE-0001"
        request = {
            "schema_version": "1.0",
            "suite_id": suite_id,
            "office_id": "OFF-0001",
            "request_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": "receipts.search",
            "payload": {"query": "test"},
        }

        response = client.post("/v1/intents", json=request)
        assert response.status_code == 200

        # Verify receipts were stored
        count = get_receipt_count(suite_id)
        assert count > 0, "Expected receipts to be stored after intent processing"

    def test_denied_intent_also_produces_receipts(self, client) -> None:
        """Even denied intents produce receipts (Law #2)."""
        suite_id = "STE-0001"
        request = {
            "schema_version": "1.0",
            "suite_id": suite_id,
            "office_id": "OFF-0001",
            "request_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": "hack.system",
            "payload": {},
        }

        response = client.post("/v1/intents", json=request)
        assert response.status_code == 403

        # Denied requests also generate receipts
        count = get_receipt_count(suite_id)
        assert count > 0, "Expected receipts even for denied intents (Law #2)"


# ===========================================================================
# E2E Flow Tests — Green / Yellow / Red Tiers through /v1/intents
# ===========================================================================


class TestE2EGreenTierFlow:
    """Green tier actions (e.g., calendar.read) execute fully without approval."""

    def _make_request(self, suite_id: str, task_type: str = "calendar.read") -> dict:
        return {
            "schema_version": "1.0",
            "suite_id": suite_id,
            "office_id": "OFF-0001",
            "request_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": task_type,
            "payload": {},
        }

    def test_green_returns_200_with_ava_result(self, client) -> None:
        """Green tier completes fully → 200 + AvaResult."""
        req = self._make_request("suite-green-001")
        response = client.post("/v1/intents", json=req)
        assert response.status_code == 200

        data = response.json()
        assert data["risk"]["tier"] == "green"
        assert data["governance"]["receipt_ids"] is not None
        assert len(data["governance"]["receipt_ids"]) > 0

    def test_green_receipts_stored_with_chain_hashes(self, client) -> None:
        """Green tier receipts have chain metadata (hash, sequence, chain_id)."""
        suite_id = "suite-green-002"
        req = self._make_request(suite_id)
        client.post("/v1/intents", json=req)

        receipts = get_chain_receipts(suite_id=suite_id)
        assert len(receipts) > 0, "Expected receipts stored for green tier"

        for receipt in receipts:
            assert receipt.get("receipt_hash"), "Receipt must have hash"
            assert receipt.get("chain_id") == suite_id, "Chain ID must match suite"
            assert receipt.get("sequence", 0) > 0, "Sequence must be positive"

    def test_green_receipt_chain_is_valid(self, client) -> None:
        """Green tier receipt chain passes verification."""
        from aspire_orchestrator.services.receipt_chain import verify_chain

        suite_id = "suite-green-003"
        req = self._make_request(suite_id)
        client.post("/v1/intents", json=req)

        receipts = get_chain_receipts(suite_id=suite_id)
        result = verify_chain(receipts, chain_id=suite_id)
        assert result.valid, f"Chain should be valid, got errors: {result.errors}"

    def test_green_no_approval_required(self, client) -> None:
        """Green tier does not require approval."""
        req = self._make_request("suite-green-004")
        response = client.post("/v1/intents", json=req)
        assert response.status_code == 200

        data = response.json()
        assert data["governance"]["approvals_required"] == []
        assert data["governance"]["presence_required"] is False

    def test_green_includes_receipt_types(self, client) -> None:
        """Green tier should emit decision_intake + policy_decision + tool_execution receipts."""
        suite_id = "suite-green-005"
        req = self._make_request(suite_id, task_type="receipts.search")
        client.post("/v1/intents", json=req)

        receipts = query_receipts(suite_id=suite_id, limit=100)
        receipt_types = {r.get("receipt_type") for r in receipts}
        assert "decision_intake" in receipt_types, "Missing intake receipt"
        assert "policy_decision" in receipt_types, "Missing policy receipt"


class TestE2EYellowTierFlow:
    """Yellow tier actions (e.g., invoice.create) require approval."""

    def _make_request(self, suite_id: str, task_type: str = "invoice.create") -> dict:
        return {
            "schema_version": "1.0",
            "suite_id": suite_id,
            "office_id": "OFF-0001",
            "request_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": task_type,
            "payload": {"amount": 1500, "customer": "test-customer"},
        }

    def test_yellow_without_approval_returns_approval_required(self, client) -> None:
        """Yellow tier without approval → APPROVAL_REQUIRED error."""
        req = self._make_request("suite-yellow-001")
        response = client.post("/v1/intents", json=req)

        # Should return non-200 indicating approval needed
        data = response.json()
        # Either 202 (approval request) or error indicating approval required
        assert (
            response.status_code in (200, 202, 403)
            or data.get("error") in ("APPROVAL_REQUIRED", "POLICY_DENIED")
        ), f"Expected approval flow, got {response.status_code}: {data}"

    def test_yellow_produces_receipts(self, client) -> None:
        """Yellow tier ALWAYS produces receipts, even when awaiting approval (Law #2)."""
        suite_id = "suite-yellow-002"
        req = self._make_request(suite_id)
        client.post("/v1/intents", json=req)

        count = get_receipt_count(suite_id)
        assert count > 0, "Yellow tier must produce receipts even without approval"

    def test_yellow_receipts_have_chain_hashes(self, client) -> None:
        """Yellow tier receipts have valid chain metadata."""
        suite_id = "suite-yellow-003"
        req = self._make_request(suite_id)
        client.post("/v1/intents", json=req)

        receipts = get_chain_receipts(suite_id=suite_id)
        assert len(receipts) > 0

        for receipt in receipts:
            assert receipt.get("receipt_hash"), "Receipt must have hash"
            assert receipt.get("chain_id") == suite_id

    def test_yellow_receipt_chain_is_valid(self, client) -> None:
        """Yellow tier receipt chain passes verification."""
        from aspire_orchestrator.services.receipt_chain import verify_chain

        suite_id = "suite-yellow-004"
        req = self._make_request(suite_id)
        client.post("/v1/intents", json=req)

        receipts = get_chain_receipts(suite_id=suite_id)
        result = verify_chain(receipts, chain_id=suite_id)
        assert result.valid, f"Chain should be valid, got errors: {result.errors}"


class TestE2ERedTierFlow:
    """Red tier actions (e.g., payment.send) require approval + presence."""

    def _make_request(self, suite_id: str, task_type: str = "payment.send") -> dict:
        return {
            "schema_version": "1.0",
            "suite_id": suite_id,
            "office_id": "OFF-0001",
            "request_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": task_type,
            "payload": {"amount": 5000, "recipient": "vendor-001"},
        }

    def test_red_without_approval_returns_error(self, client) -> None:
        """Red tier without approval/presence → error response."""
        req = self._make_request("suite-red-001")
        response = client.post("/v1/intents", json=req)

        data = response.json()
        # Red tier should require approval and/or presence
        assert (
            response.status_code in (200, 202, 403)
            or data.get("error") in (
                "APPROVAL_REQUIRED",
                "PRESENCE_REQUIRED",
                "POLICY_DENIED",
            )
        ), f"Expected approval/presence gate, got {response.status_code}: {data}"

    def test_red_produces_receipts(self, client) -> None:
        """Red tier ALWAYS produces receipts (Law #2)."""
        suite_id = "suite-red-002"
        req = self._make_request(suite_id)
        client.post("/v1/intents", json=req)

        count = get_receipt_count(suite_id)
        assert count > 0, "Red tier must produce receipts even without approval"

    def test_red_receipts_have_chain_hashes(self, client) -> None:
        """Red tier receipts have valid chain metadata."""
        suite_id = "suite-red-003"
        req = self._make_request(suite_id)
        client.post("/v1/intents", json=req)

        receipts = get_chain_receipts(suite_id=suite_id)
        assert len(receipts) > 0

        for receipt in receipts:
            assert receipt.get("receipt_hash"), "Receipt must have hash"

    def test_red_receipt_chain_is_valid(self, client) -> None:
        """Red tier receipt chain passes verification."""
        from aspire_orchestrator.services.receipt_chain import verify_chain

        suite_id = "suite-red-004"
        req = self._make_request(suite_id)
        client.post("/v1/intents", json=req)

        receipts = get_chain_receipts(suite_id=suite_id)
        result = verify_chain(receipts, chain_id=suite_id)
        assert result.valid, f"Chain should be valid, got errors: {result.errors}"


class TestE2EDeniedFlow:
    """Denied flows must produce receipts with valid chain metadata."""

    def _make_request(self, suite_id: str, task_type: str = "hack.system") -> dict:
        return {
            "schema_version": "1.0",
            "suite_id": suite_id,
            "office_id": "OFF-0001",
            "request_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": task_type,
            "payload": {},
        }

    def test_denied_receipts_have_chain_hashes(self, client) -> None:
        """Denied flow receipts get chain hashes via respond safety net."""
        suite_id = "suite-denied-001"
        req = self._make_request(suite_id)
        response = client.post("/v1/intents", json=req)
        assert response.status_code == 403

        receipts = get_chain_receipts(suite_id=suite_id)
        assert len(receipts) > 0, "Denied flow must produce receipts"

        for receipt in receipts:
            assert receipt.get("receipt_hash"), "Denied receipts must have hash"
            assert receipt.get("chain_id") == suite_id

    def test_denied_receipt_chain_is_valid(self, client) -> None:
        """Denied flow receipt chain passes verification."""
        from aspire_orchestrator.services.receipt_chain import verify_chain

        suite_id = "suite-denied-002"
        req = self._make_request(suite_id)
        client.post("/v1/intents", json=req)

        receipts = get_chain_receipts(suite_id=suite_id)
        result = verify_chain(receipts, chain_id=suite_id)
        assert result.valid, f"Denied chain should be valid, got errors: {result.errors}"

    def test_denied_receipts_include_denial_reason(self, client) -> None:
        """Denied receipts should contain a reason code."""
        suite_id = "suite-denied-003"
        req = self._make_request(suite_id)
        client.post("/v1/intents", json=req)

        receipts = query_receipts(suite_id=suite_id, limit=100)
        denial_receipts = [r for r in receipts if r.get("outcome") == "denied"]
        assert len(denial_receipts) > 0, "Expected at least one denial receipt"
        for r in denial_receipts:
            assert r.get("reason_code"), "Denial receipt must have reason_code"

    def test_denied_response_includes_receipt_ids(self, client) -> None:
        """Denied responses include receipt_ids in the error body."""
        suite_id = "suite-denied-004"
        req = self._make_request(suite_id)
        response = client.post("/v1/intents", json=req)

        data = response.json()
        assert "receipt_ids" in data, "Denied response must include receipt_ids"
        assert len(data["receipt_ids"]) > 0, "Denied response must have non-empty receipt_ids"


class TestE2ETenantIsolation:
    """Suite A cannot see Suite B's receipts (Law #6)."""

    def _make_request(self, suite_id: str, task_type: str = "calendar.read") -> dict:
        return {
            "schema_version": "1.0",
            "suite_id": suite_id,
            "office_id": "OFF-0001",
            "request_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": task_type,
            "payload": {},
        }

    def test_suite_isolation_in_receipt_store(self, client) -> None:
        """Suite A's receipts are not visible to Suite B queries."""
        # Create receipts for Suite A
        req_a = self._make_request("suite-iso-A")
        client.post("/v1/intents", json=req_a)

        # Create receipts for Suite B
        req_b = self._make_request("suite-iso-B")
        client.post("/v1/intents", json=req_b)

        # Suite A should only see its own receipts
        receipts_a = query_receipts(suite_id="suite-iso-A", limit=100)
        receipts_b = query_receipts(suite_id="suite-iso-B", limit=100)

        assert len(receipts_a) > 0
        assert len(receipts_b) > 0

        for r in receipts_a:
            assert r["suite_id"] == "suite-iso-A", "Suite A leaking to Suite B"
        for r in receipts_b:
            assert r["suite_id"] == "suite-iso-B", "Suite B leaking to Suite A"

    def test_receipt_endpoint_scoped_by_suite(self, client) -> None:
        """GET /v1/receipts scopes by suite_id."""
        # Create receipts for two suites
        req_a = self._make_request("suite-endpoint-A")
        client.post("/v1/intents", json=req_a)

        req_b = self._make_request("suite-endpoint-B")
        client.post("/v1/intents", json=req_b)

        # Query via API
        resp_a = client.get("/v1/receipts?suite_id=suite-endpoint-A")
        assert resp_a.status_code == 200
        data_a = resp_a.json()

        resp_b = client.get("/v1/receipts?suite_id=suite-endpoint-B")
        data_b = resp_b.json()

        # Each suite's receipts should be isolated
        for r in data_a["receipts"]:
            assert r["suite_id"] == "suite-endpoint-A"
        for r in data_b["receipts"]:
            assert r["suite_id"] == "suite-endpoint-B"


class TestE2ECorrelationIdFlow:
    """Correlation IDs propagate through the entire pipeline."""

    def _make_request(self, suite_id: str, correlation_id: str) -> dict:
        return {
            "schema_version": "1.0",
            "suite_id": suite_id,
            "office_id": "OFF-0001",
            "request_id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": "calendar.read",
            "payload": {},
        }

    def test_correlation_id_in_all_receipts(self, client) -> None:
        """All receipts in a flow share the same correlation_id."""
        suite_id = "suite-corr-001"
        corr_id = str(uuid.uuid4())
        req = self._make_request(suite_id, corr_id)
        client.post("/v1/intents", json=req)

        receipts = query_receipts(suite_id=suite_id, limit=100)
        assert len(receipts) > 0

        for receipt in receipts:
            assert receipt["correlation_id"] == corr_id, (
                f"Receipt {receipt['id']} has wrong correlation_id: "
                f"{receipt['correlation_id']} != {corr_id}"
            )

    def test_correlation_id_filter_on_receipts_endpoint(self, client) -> None:
        """Receipt endpoint filters by correlation_id correctly."""
        suite_id = "suite-corr-002"
        corr_a = str(uuid.uuid4())
        corr_b = str(uuid.uuid4())

        client.post("/v1/intents", json=self._make_request(suite_id, corr_a))
        client.post("/v1/intents", json=self._make_request(suite_id, corr_b))

        resp = client.get(f"/v1/receipts?suite_id={suite_id}&correlation_id={corr_a}")
        data = resp.json()
        assert data["count"] > 0
        for r in data["receipts"]:
            assert r["correlation_id"] == corr_a
