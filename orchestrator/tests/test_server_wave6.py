"""Server Endpoint Tests — Wave 6 (Registry + A2A).

Tests the HTTP API layer for:
- GET /v1/registry/capabilities — Capability discovery
- GET /v1/registry/skill-packs/:id — Skill pack lookup
- GET /v1/registry/route/:action — Action routing
- POST /v1/a2a/dispatch — Task dispatch
- POST /v1/a2a/claim — Task claiming
- POST /v1/a2a/complete — Task completion
- POST /v1/a2a/fail — Task failure
- GET /v1/a2a/tasks — Task listing

All endpoints enforce tenant isolation (Law #6) and produce receipts (Law #2).
"""

import pytest
from httpx import AsyncClient, ASGITransport

from aspire_orchestrator.server import app
from aspire_orchestrator.services.a2a_service import get_a2a_service
from aspire_orchestrator.services.receipt_store import clear_store


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def cleanup():
    """Clean stores between tests."""
    clear_store()
    get_a2a_service().clear()
    yield
    clear_store()
    get_a2a_service().clear()


@pytest.fixture
async def client():
    """Async HTTP client for the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


SUITE_ID = "suite-test-001"
OFFICE_ID = "office-test-001"


# =============================================================================
# Registry Endpoint Tests
# =============================================================================


class TestRegistryCapabilities:
    """GET /v1/registry/capabilities"""

    @pytest.mark.asyncio
    async def test_list_all_capabilities(self, client):
        """Lists all registered skill packs."""
        res = await client.get("/v1/registry/capabilities")
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 11
        assert len(body["capabilities"]) == 11

    @pytest.mark.asyncio
    async def test_filter_by_category(self, client):
        """Filters by category."""
        res = await client.get("/v1/registry/capabilities?category=channel")
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 6
        assert all(c["category"] == "channel" for c in body["capabilities"])

    @pytest.mark.asyncio
    async def test_filter_by_risk_tier(self, client):
        """Filters by risk tier."""
        res = await client.get("/v1/registry/capabilities?risk_tier=red")
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 3
        assert all(c["risk_tier"] == "red" for c in body["capabilities"])

    @pytest.mark.asyncio
    async def test_invalid_risk_tier(self, client):
        """Invalid risk tier returns 400."""
        res = await client.get("/v1/registry/capabilities?risk_tier=extreme")
        assert res.status_code == 400
        assert res.json()["error"] == "SCHEMA_VALIDATION_FAILED"

    @pytest.mark.asyncio
    async def test_includes_stats(self, client):
        """Response includes registry stats."""
        res = await client.get("/v1/registry/capabilities")
        body = res.json()
        assert "stats" in body
        assert body["stats"]["total_skill_packs"] == 11


class TestRegistrySkillPacks:
    """GET /v1/registry/skill-packs/:id"""

    @pytest.mark.asyncio
    async def test_get_known_pack(self, client):
        """Returns known skill pack."""
        res = await client.get("/v1/registry/skill-packs/quinn_invoicing")
        assert res.status_code == 200
        body = res.json()
        assert body["skill_pack_id"] == "quinn_invoicing"
        assert body["owner"] == "quinn"
        assert body["risk_tier"] == "yellow"

    @pytest.mark.asyncio
    async def test_get_unknown_pack(self, client):
        """Returns 404 for unknown pack."""
        res = await client.get("/v1/registry/skill-packs/nonexistent")
        assert res.status_code == 404
        assert res.json()["error"] == "NOT_FOUND"


class TestRegistryRouting:
    """GET /v1/registry/route/:action"""

    @pytest.mark.asyncio
    async def test_route_known_action(self, client):
        """Routes known action to correct skill pack."""
        res = await client.get("/v1/registry/route/invoice.create")
        assert res.status_code == 200
        body = res.json()
        assert body["found"] is True
        assert body["skill_pack_id"] == "quinn_invoicing"
        assert body["owner"] == "quinn"

    @pytest.mark.asyncio
    async def test_route_unknown_action(self, client):
        """Unknown action returns 404."""
        res = await client.get("/v1/registry/route/hack.system")
        assert res.status_code == 404
        body = res.json()
        assert body["found"] is False

    @pytest.mark.asyncio
    async def test_route_includes_tools(self, client):
        """Route response includes tool identifiers."""
        res = await client.get("/v1/registry/route/payment.send")
        body = res.json()
        assert body["found"] is True
        assert "moov.payment.send" in body["tools"]


# =============================================================================
# A2A Endpoint Tests
# =============================================================================


class TestA2ADispatch:
    """POST /v1/a2a/dispatch"""

    @pytest.mark.asyncio
    async def test_dispatch_creates_task(self, client):
        """Dispatching creates a task."""
        res = await client.post("/v1/a2a/dispatch", json={
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "correlation_id": "corr-001",
            "task_type": "invoice.create",
            "assigned_to_agent": "quinn",
            "payload": {"customer_id": "cust-001"},
        })
        assert res.status_code == 201
        body = res.json()
        assert body["success"] is True
        assert body["task_id"] is not None
        assert body["receipt_id"] is not None

    @pytest.mark.asyncio
    async def test_dispatch_missing_fields(self, client):
        """Missing required fields returns 400."""
        res = await client.post("/v1/a2a/dispatch", json={
            "suite_id": SUITE_ID,
        })
        assert res.status_code == 400
        assert res.json()["error"] == "SCHEMA_VALIDATION_FAILED"

    @pytest.mark.asyncio
    async def test_dispatch_idempotency(self, client):
        """Same idempotency_key returns same task."""
        payload = {
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "correlation_id": "corr-001",
            "task_type": "invoice.create",
            "assigned_to_agent": "quinn",
            "payload": {},
            "idempotency_key": "idem-001",
        }
        r1 = await client.post("/v1/a2a/dispatch", json=payload)
        r2 = await client.post("/v1/a2a/dispatch", json=payload)
        assert r1.json()["task_id"] == r2.json()["task_id"]


class TestA2AClaim:
    """POST /v1/a2a/claim"""

    @pytest.mark.asyncio
    async def test_claim_available_task(self, client):
        """Agent claims an available task."""
        await client.post("/v1/a2a/dispatch", json={
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "correlation_id": "corr-001",
            "task_type": "email.send",
            "assigned_to_agent": "eli",
            "payload": {},
        })
        res = await client.post("/v1/a2a/claim", json={
            "agent_id": "eli",
            "suite_id": SUITE_ID,
        })
        assert res.status_code == 200
        body = res.json()
        assert body["success"] is True
        assert body["task"]["task_type"] == "email.send"
        assert body["receipt_id"] is not None

    @pytest.mark.asyncio
    async def test_claim_no_tasks(self, client):
        """Claim with no tasks returns 404."""
        res = await client.post("/v1/a2a/claim", json={
            "agent_id": "eli",
            "suite_id": SUITE_ID,
        })
        assert res.status_code == 404

    @pytest.mark.asyncio
    async def test_claim_missing_agent_id(self, client):
        """Missing agent_id returns 400."""
        res = await client.post("/v1/a2a/claim", json={
            "suite_id": SUITE_ID,
        })
        assert res.status_code == 400


class TestA2AComplete:
    """POST /v1/a2a/complete"""

    @pytest.mark.asyncio
    async def test_complete_claimed_task(self, client):
        """Complete a claimed task."""
        dispatch = await client.post("/v1/a2a/dispatch", json={
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "correlation_id": "corr-001",
            "task_type": "email.send",
            "assigned_to_agent": "eli",
            "payload": {},
        })
        task_id = dispatch.json()["task_id"]

        await client.post("/v1/a2a/claim", json={
            "agent_id": "eli",
            "suite_id": SUITE_ID,
        })

        res = await client.post("/v1/a2a/complete", json={
            "task_id": task_id,
            "agent_id": "eli",
            "suite_id": SUITE_ID,
            "result": {"sent": True},
        })
        assert res.status_code == 200
        body = res.json()
        assert body["success"] is True
        assert body["new_status"] == "done"

    @pytest.mark.asyncio
    async def test_complete_cross_suite_denied(self, client):
        """Complete from wrong suite returns 403 (Law #6)."""
        dispatch = await client.post("/v1/a2a/dispatch", json={
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "correlation_id": "corr-001",
            "task_type": "email.send",
            "assigned_to_agent": "eli",
            "payload": {},
        })
        task_id = dispatch.json()["task_id"]

        await client.post("/v1/a2a/claim", json={
            "agent_id": "eli",
            "suite_id": SUITE_ID,
        })

        res = await client.post("/v1/a2a/complete", json={
            "task_id": task_id,
            "agent_id": "eli",
            "suite_id": "attacker-suite",
        })
        assert res.status_code == 403
        assert res.json()["error"] == "TENANT_ISOLATION_VIOLATION"


class TestA2AFail:
    """POST /v1/a2a/fail"""

    @pytest.mark.asyncio
    async def test_fail_requeues_task(self, client):
        """Failing a task requeues it for retry."""
        dispatch = await client.post("/v1/a2a/dispatch", json={
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "correlation_id": "corr-001",
            "task_type": "email.send",
            "assigned_to_agent": "eli",
            "payload": {},
        })
        task_id = dispatch.json()["task_id"]

        await client.post("/v1/a2a/claim", json={
            "agent_id": "eli",
            "suite_id": SUITE_ID,
        })

        res = await client.post("/v1/a2a/fail", json={
            "task_id": task_id,
            "agent_id": "eli",
            "suite_id": SUITE_ID,
            "error": "Provider timeout",
        })
        assert res.status_code == 200
        body = res.json()
        assert body["new_status"] == "created"  # Requeued

    @pytest.mark.asyncio
    async def test_fail_missing_error(self, client):
        """Missing error field returns 400."""
        res = await client.post("/v1/a2a/fail", json={
            "task_id": "some-id",
            "agent_id": "eli",
            "suite_id": SUITE_ID,
        })
        assert res.status_code == 400


class TestA2AListTasks:
    """GET /v1/a2a/tasks"""

    @pytest.mark.asyncio
    async def test_list_tasks(self, client):
        """Lists tasks for a suite."""
        await client.post("/v1/a2a/dispatch", json={
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "correlation_id": "corr-001",
            "task_type": "email.send",
            "assigned_to_agent": "eli",
            "payload": {},
        })
        res = await client.get(f"/v1/a2a/tasks?suite_id={SUITE_ID}")
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 1
        assert body["tasks"][0]["task_type"] == "email.send"

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, client):
        """Filter tasks by status."""
        await client.post("/v1/a2a/dispatch", json={
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "correlation_id": "corr-001",
            "task_type": "email.send",
            "assigned_to_agent": "eli",
            "payload": {},
        })
        res = await client.get(f"/v1/a2a/tasks?suite_id={SUITE_ID}&status=created")
        assert res.status_code == 200
        assert res.json()["count"] == 1

        res = await client.get(f"/v1/a2a/tasks?suite_id={SUITE_ID}&status=done")
        assert res.status_code == 200
        assert res.json()["count"] == 0

    @pytest.mark.asyncio
    async def test_list_invalid_status(self, client):
        """Invalid status returns 400."""
        res = await client.get(f"/v1/a2a/tasks?suite_id={SUITE_ID}&status=invalid")
        assert res.status_code == 400
        assert res.json()["error"] == "SCHEMA_VALIDATION_FAILED"

    @pytest.mark.asyncio
    async def test_list_cross_suite_isolation(self, client):
        """Tasks from other suites not visible (Law #6)."""
        await client.post("/v1/a2a/dispatch", json={
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "correlation_id": "corr-001",
            "task_type": "email.send",
            "assigned_to_agent": "eli",
            "payload": {},
        })
        res = await client.get("/v1/a2a/tasks?suite_id=other-suite")
        assert res.status_code == 200
        assert res.json()["count"] == 0
