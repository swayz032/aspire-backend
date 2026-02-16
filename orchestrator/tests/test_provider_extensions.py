"""Tests for Provider Adapter Extensions — Wave 6.

Coverage:
  - PreflightResult / SimulateResult dataclasses
  - BaseProviderClient.preflight() default validation
  - BaseProviderClient.simulate() default simulation
  - Preflight detects missing fields
  - Preflight warns on open circuit breaker
  - Simulate returns simulated response
  - Simulate fails if preflight fails
  - Export scripts (export_receipts, export_provider_calls)
  - Evidence bundle generation
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    CircuitBreaker,
    CircuitState,
    PreflightResult,
    ProviderRequest,
    ProviderResponse,
    SimulateResult,
)
from aspire_orchestrator.services.receipt_store import (
    clear_store,
    get_receipt_count,
    store_receipts,
)


# ---------------------------------------------------------------------------
# Concrete test client (minimal subclass)
# ---------------------------------------------------------------------------


class StubProviderClient(BaseProviderClient):
    """Minimal concrete subclass for testing base class methods."""

    provider_id = "test_provider"
    base_url = "https://api.test.example.com"
    timeout_seconds = 5.0

    async def _authenticate_headers(self, request: ProviderRequest) -> dict[str, str]:
        return {"Authorization": "Bearer test_token"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> StubProviderClient:
    return StubProviderClient()


@pytest.fixture
def valid_request() -> ProviderRequest:
    return ProviderRequest(
        method="POST",
        path="/v1/invoices",
        body={"amount": 1500, "customer": "acme"},
        correlation_id=str(uuid.uuid4()),
        suite_id=str(uuid.uuid4()),
        office_id=str(uuid.uuid4()),
    )


@pytest.fixture
def empty_request() -> ProviderRequest:
    return ProviderRequest(
        method="",
        path="",
        body=None,
        correlation_id="",
        suite_id="",
        office_id="",
    )


# ---------------------------------------------------------------------------
# PreflightResult dataclass
# ---------------------------------------------------------------------------


class TestPreflightResult:
    def test_valid_preflight_summary(self) -> None:
        result = PreflightResult(valid=True, issues=[], provider_id="stripe")
        assert "OK" in result.summary
        assert "stripe" in result.summary

    def test_failed_preflight_summary(self) -> None:
        result = PreflightResult(
            valid=False,
            issues=["method is required", "path is required"],
            provider_id="gusto",
        )
        assert "FAILED" in result.summary
        assert "method is required" in result.summary

    def test_preflight_with_circuit_warning(self) -> None:
        result = PreflightResult(
            valid=True,
            issues=[],
            provider_id="stripe",
            circuit_warning="Circuit breaker OPEN",
        )
        assert "WARNING" in result.summary
        assert "Circuit breaker OPEN" in result.summary


# ---------------------------------------------------------------------------
# SimulateResult dataclass
# ---------------------------------------------------------------------------


class TestSimulateResult:
    def test_successful_simulate(self) -> None:
        result = SimulateResult(
            success=True,
            simulated_response={"id": "sim_abc123"},
            latency_estimate_ms=500.0,
            provider_id="stripe",
            receipt_data={"id": "r1", "outcome": "success"},
        )
        assert result.success is True
        assert "sim_abc123" in result.simulated_response["id"]

    def test_failed_simulate(self) -> None:
        result = SimulateResult(
            success=False,
            simulated_response={"error": "PREFLIGHT_FAILED"},
            latency_estimate_ms=0.0,
            provider_id="gusto",
            receipt_data={"id": "r2", "outcome": "failed"},
        )
        assert result.success is False


# ---------------------------------------------------------------------------
# BaseProviderClient.preflight()
# ---------------------------------------------------------------------------


class TestPreflight:
    @pytest.mark.asyncio
    async def test_valid_request_passes_preflight(
        self, client: StubProviderClient, valid_request: ProviderRequest
    ) -> None:
        result = await client.preflight(valid_request)
        assert result.valid is True
        assert result.issues == []
        assert result.provider_id == "test_provider"

    @pytest.mark.asyncio
    async def test_empty_request_fails_preflight(
        self, client: StubProviderClient, empty_request: ProviderRequest
    ) -> None:
        result = await client.preflight(empty_request)
        assert result.valid is False
        assert len(result.issues) == 4  # method, path, suite_id, correlation_id

    @pytest.mark.asyncio
    async def test_missing_suite_id_flagged(self, client: StubProviderClient) -> None:
        req = ProviderRequest(
            method="GET",
            path="/v1/test",
            suite_id="",
            correlation_id=str(uuid.uuid4()),
        )
        result = await client.preflight(req)
        assert result.valid is False
        assert any("suite_id" in issue for issue in result.issues)

    @pytest.mark.asyncio
    async def test_missing_correlation_id_flagged(self, client: StubProviderClient) -> None:
        req = ProviderRequest(
            method="GET",
            path="/v1/test",
            suite_id=str(uuid.uuid4()),
            correlation_id="",
        )
        result = await client.preflight(req)
        assert result.valid is False
        assert any("correlation_id" in issue for issue in result.issues)

    @pytest.mark.asyncio
    async def test_circuit_breaker_fails_preflight(
        self, client: StubProviderClient, valid_request: ProviderRequest
    ) -> None:
        """Law #3: fail-closed — preflight must return valid=False when circuit is OPEN."""
        # Force circuit breaker open
        for _ in range(10):
            client._circuit.record_failure()
        assert client._circuit.state == CircuitState.OPEN

        result = await client.preflight(valid_request)
        assert result.valid is False  # Law #3: fail-closed
        assert result.circuit_warning is not None
        assert "OPEN" in result.circuit_warning
        assert "circuit breaker OPEN" in result.issues


# ---------------------------------------------------------------------------
# BaseProviderClient.simulate()
# ---------------------------------------------------------------------------


class TestSimulate:
    @pytest.mark.asyncio
    async def test_valid_request_simulates_success(
        self, client: StubProviderClient, valid_request: ProviderRequest
    ) -> None:
        result = await client.simulate(valid_request)
        assert result.success is True
        assert "sim_" in result.simulated_response["id"]
        assert result.simulated_response["provider"] == "test_provider"
        assert result.simulated_response["status"] == "simulated"
        assert result.latency_estimate_ms > 0

    @pytest.mark.asyncio
    async def test_simulate_generates_receipt_data(
        self, client: StubProviderClient, valid_request: ProviderRequest
    ) -> None:
        result = await client.simulate(valid_request)
        assert result.receipt_data is not None
        assert result.receipt_data["outcome"] == "success"
        assert result.receipt_data["reason_code"] == "SIMULATED"

    @pytest.mark.asyncio
    async def test_simulate_fails_on_invalid_request(
        self, client: StubProviderClient, empty_request: ProviderRequest
    ) -> None:
        result = await client.simulate(empty_request)
        assert result.success is False
        assert result.simulated_response["error"] == "PREFLIGHT_FAILED"
        assert result.receipt_data["outcome"] == "failed"
        assert result.receipt_data["reason_code"] == "PREFLIGHT_FAILED"

    @pytest.mark.asyncio
    async def test_simulate_never_makes_http_calls(
        self, client: StubProviderClient, valid_request: ProviderRequest
    ) -> None:
        """Simulate should NEVER make actual HTTP calls (Law #7: tools don't act autonomously)."""
        # If simulate made HTTP calls, this would fail with connection error
        # since test_provider base_url doesn't exist
        result = await client.simulate(valid_request)
        assert result.success is True  # Should succeed without HTTP


# ---------------------------------------------------------------------------
# Export Scripts
# ---------------------------------------------------------------------------


class TestExportReceipts:
    def setup_method(self) -> None:
        clear_store()
        # Clear admin token env var for clean tests
        import os
        os.environ.pop("ASPIRE_ADMIN_TOKEN", None)

    def teardown_method(self) -> None:
        import os
        os.environ.pop("ASPIRE_ADMIN_TOKEN", None)

    def test_export_no_suite_no_token_raises(self) -> None:
        """Law #6: cross-tenant export without admin token must fail."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        # Force reimport to get updated code
        import importlib
        import export_receipts as er
        importlib.reload(er)

        with pytest.raises(ValueError, match="ASPIRE_ADMIN_TOKEN"):
            er.export_receipts()

    def test_export_no_suite_with_token_succeeds(self) -> None:
        """Law #6: cross-tenant export with admin token should succeed."""
        import os
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        import importlib
        import export_receipts as er
        importlib.reload(er)

        os.environ["ASPIRE_ADMIN_TOKEN"] = "test-admin-token"
        result = er.export_receipts()
        assert result == []

    def test_export_no_suite_with_arg_token_succeeds(self) -> None:
        """Law #6: cross-tenant export with admin_token arg should succeed."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        import importlib
        import export_receipts as er
        importlib.reload(er)

        result = er.export_receipts(admin_token="explicit-token")
        assert result == []

    def test_export_empty_store_with_suite_id(self) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        import importlib
        import export_receipts as er
        importlib.reload(er)

        result = er.export_receipts(suite_id="some-suite")
        assert result == []

    def test_export_with_receipts(self) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from export_receipts import export_receipts

        suite = str(uuid.uuid4())
        store_receipts([
            {"id": str(uuid.uuid4()), "suite_id": suite, "outcome": "success", "receipt_type": "test"},
            {"id": str(uuid.uuid4()), "suite_id": suite, "outcome": "denied", "receipt_type": "test"},
        ])

        result = export_receipts(suite_id=suite)
        assert len(result) == 2

    def test_export_with_limit(self) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        import importlib
        import export_receipts as er
        importlib.reload(er)

        store_receipts([
            {"id": str(uuid.uuid4()), "suite_id": "s1", "outcome": "success"},
            {"id": str(uuid.uuid4()), "suite_id": "s1", "outcome": "success"},
            {"id": str(uuid.uuid4()), "suite_id": "s1", "outcome": "success"},
        ])

        result = er.export_receipts(limit=2, admin_token="test-admin")
        assert len(result) == 2


class TestExportProviderCalls:
    def setup_method(self) -> None:
        clear_store()

    def test_export_provider_calls_empty(self) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from export_provider_calls import export_provider_calls

        result = export_provider_calls()
        assert result == []

    def test_export_filters_to_tool_execution(self) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from export_provider_calls import export_provider_calls

        store_receipts([
            {
                "id": str(uuid.uuid4()),
                "suite_id": "s1",
                "actor_id": "provider.stripe",
                "tool_used": "stripe.invoice.create",
                "receipt_type": "tool.execution",
                "outcome": "success",
            },
            {
                "id": str(uuid.uuid4()),
                "suite_id": "s1",
                "actor_id": "policy_engine",
                "receipt_type": "policy.decision",
                "outcome": "success",
            },
        ])

        result = export_provider_calls()
        assert len(result) == 1
        assert result[0]["provider"] == "stripe"


class TestEvidenceBundle:
    def setup_method(self) -> None:
        clear_store()

    def test_generate_bundle(self, tmp_path: Path) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from generate_evidence_bundle import generate_bundle

        suite = str(uuid.uuid4())
        store_receipts([
            {
                "id": str(uuid.uuid4()),
                "suite_id": suite,
                "actor_id": "provider.stripe",
                "receipt_type": "tool.execution",
                "outcome": "success",
            },
        ])

        counts = generate_bundle(suite_id=suite, output_dir=str(tmp_path / "bundle"))

        bundle_dir = tmp_path / "bundle"
        assert (bundle_dir / "receipts.json").exists()
        assert (bundle_dir / "provider_calls.json").exists()
        assert (bundle_dir / "metadata.json").exists()

        meta = json.loads((bundle_dir / "metadata.json").read_text())
        assert meta["suite_id"] == suite
        assert meta["governance"]["law_6_tenant_scoped"] is True
