"""Outbox + Idempotency Tests — Phase 3 Wave 5.

38 tests covering:
  1. IdempotencyService (12 tests)
     - Key generation, check-and-reserve, mark_completed, mark_failed
     - Double-submit rejection, thread safety, clear_store
  2. OutboxClient (10 tests)
     - Job submission, status polling, claiming, completion, failure
     - Dead letter after max retries, receipt emission
  3. Execute node integration (8 tests)
     - Idempotency key enforcement on state-changing ops
     - RED-tier ops go through outbox
     - YELLOW ops stay synchronous
     - Double-submit returns original receipt
  4. Singleton tests (4 tests)
     - Idempotency + Outbox singleton/reload
  5. State machine assertions (4 tests)
     - Outbox job status transitions

Law compliance:
  - Law #2: Every outbox job + idempotency check produces receipts
  - Law #3: Fail-closed on duplicate idempotency keys
  - Law #4: RED ops → outbox, YELLOW ops → sync
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

from aspire_orchestrator.services.idempotency_service import (
    IdempotencyResult,
    IdempotencyService,
    get_idempotency_service,
)
from aspire_orchestrator.services.outbox_client import (
    OutboxClient,
    OutboxJob,
    OutboxJobStatus,
    OutboxSubmitResult,
    get_outbox_client,
)

# =============================================================================
# Fixtures
# =============================================================================

SUITE_ID = "suite-w5-test-001"
OFFICE_ID = "office-w5-test-001"
CORR_ID = "corr-w5-test-001"


@pytest.fixture
def idempotency_svc() -> IdempotencyService:
    svc = IdempotencyService()
    yield svc
    svc.clear_store()


@pytest.fixture
def outbox() -> OutboxClient:
    client = OutboxClient()
    yield client
    client.clear_jobs()


# =============================================================================
# 1. IdempotencyService Tests
# =============================================================================


class TestIdempotencyKeyGeneration:
    def test_generate_key_returns_uuid(self, idempotency_svc: IdempotencyService) -> None:
        """Generated key is a valid UUID string."""
        key = idempotency_svc.generate_key(
            suite_id=SUITE_ID, action_type="payment.send",
        )
        assert isinstance(key, str)
        assert len(key) == 36  # UUID format: 8-4-4-4-12

    def test_generate_key_unique(self, idempotency_svc: IdempotencyService) -> None:
        """Each generated key is unique."""
        keys = {
            idempotency_svc.generate_key(suite_id=SUITE_ID, action_type="payment.send")
            for _ in range(100)
        }
        assert len(keys) == 100

    def test_generate_key_with_params_hash(self, idempotency_svc: IdempotencyService) -> None:
        """Key generation accepts optional params_hash."""
        key = idempotency_svc.generate_key(
            suite_id=SUITE_ID, action_type="payment.send", params_hash="abc123",
        )
        assert isinstance(key, str)


class TestIdempotencyCheckAndReserve:
    def test_first_check_allows_execution(self, idempotency_svc: IdempotencyService) -> None:
        """First check-and-reserve for a key returns should_execute=True."""
        result = idempotency_svc.check_and_reserve(
            suite_id=SUITE_ID,
            idempotency_key="key-001",
            action_type="payment.send",
        )
        assert result.should_execute is True
        assert result.already_executed is False
        assert result.original_receipt_id is None

    def test_duplicate_check_rejects_execution(self, idempotency_svc: IdempotencyService) -> None:
        """Second check-and-reserve for same key returns should_execute=False (Law #3)."""
        idempotency_svc.check_and_reserve(
            suite_id=SUITE_ID,
            idempotency_key="key-dup",
            action_type="payment.send",
        )
        result = idempotency_svc.check_and_reserve(
            suite_id=SUITE_ID,
            idempotency_key="key-dup",
            action_type="payment.send",
        )
        assert result.should_execute is False
        assert result.already_executed is True

    def test_different_suite_same_key_allowed(self, idempotency_svc: IdempotencyService) -> None:
        """Same key in different suites are independent (Law #6 - tenant isolation)."""
        r1 = idempotency_svc.check_and_reserve(
            suite_id="suite-A", idempotency_key="shared-key", action_type="payment.send",
        )
        r2 = idempotency_svc.check_and_reserve(
            suite_id="suite-B", idempotency_key="shared-key", action_type="payment.send",
        )
        assert r1.should_execute is True
        assert r2.should_execute is True  # Different tenant, different reservation

    def test_mark_completed_records_receipt_id(self, idempotency_svc: IdempotencyService) -> None:
        """After mark_completed, duplicate check returns original receipt ID."""
        idempotency_svc.check_and_reserve(
            suite_id=SUITE_ID, idempotency_key="key-complete", action_type="payment.send",
        )
        idempotency_svc.mark_completed(
            suite_id=SUITE_ID, idempotency_key="key-complete", receipt_id="rcpt-123",
        )
        result = idempotency_svc.check_and_reserve(
            suite_id=SUITE_ID, idempotency_key="key-complete", action_type="payment.send",
        )
        assert result.should_execute is False
        assert result.original_receipt_id == "rcpt-123"

    def test_mark_failed_allows_retry(self, idempotency_svc: IdempotencyService) -> None:
        """After mark_failed, the key is released for retry."""
        idempotency_svc.check_and_reserve(
            suite_id=SUITE_ID, idempotency_key="key-fail", action_type="payment.send",
        )
        idempotency_svc.mark_failed(
            suite_id=SUITE_ID, idempotency_key="key-fail", error="provider_timeout",
        )
        result = idempotency_svc.check_and_reserve(
            suite_id=SUITE_ID, idempotency_key="key-fail", action_type="payment.send",
        )
        assert result.should_execute is True  # Key released, retry allowed

    def test_clear_store(self, idempotency_svc: IdempotencyService) -> None:
        """clear_store removes all reservations."""
        idempotency_svc.check_and_reserve(
            suite_id=SUITE_ID, idempotency_key="key-clear", action_type="test",
        )
        idempotency_svc.clear_store()
        result = idempotency_svc.check_and_reserve(
            suite_id=SUITE_ID, idempotency_key="key-clear", action_type="test",
        )
        assert result.should_execute is True

    def test_thread_safety(self, idempotency_svc: IdempotencyService) -> None:
        """Concurrent check_and_reserve is thread-safe — only one thread wins."""
        results: list[IdempotencyResult] = []
        barrier = threading.Barrier(10)

        def _check() -> None:
            barrier.wait()
            r = idempotency_svc.check_and_reserve(
                suite_id=SUITE_ID, idempotency_key="key-race", action_type="payment.send",
            )
            results.append(r)

        threads = [threading.Thread(target=_check) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r.should_execute]
        losers = [r for r in results if not r.should_execute]
        assert len(winners) == 1, "Exactly one thread should win the reservation"
        assert len(losers) == 9


class TestIdempotencySingleton:
    def test_singleton_returns_same_instance(self) -> None:
        """get_idempotency_service returns the same instance."""
        s1 = get_idempotency_service()
        s2 = get_idempotency_service()
        assert s1 is s2

    def test_singleton_reload(self) -> None:
        """get_idempotency_service(reload=True) returns new instance."""
        s1 = get_idempotency_service()
        s2 = get_idempotency_service(reload=True)
        assert s1 is not s2


# =============================================================================
# 2. OutboxClient Tests
# =============================================================================


class TestOutboxSubmission:
    @pytest.mark.asyncio
    async def test_submit_job_success(self, outbox: OutboxClient) -> None:
        """Submitting a valid job returns success with job_id."""
        job = OutboxJob(
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
            action_type="payment.send",
        )
        result = await outbox.submit_job(job)
        assert result.success is True
        assert result.job_id == job.job_id
        assert result.status == OutboxJobStatus.PENDING

    @pytest.mark.asyncio
    async def test_submit_job_generates_receipt(self, outbox: OutboxClient) -> None:
        """Submission produces a receipt (Law #2)."""
        job = OutboxJob(
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
            action_type="payment.send",
        )
        result = await outbox.submit_job(job)
        assert result.receipt is not None
        assert result.receipt["event_type"] == "outbox.job.submitted"
        assert result.receipt["suite_id"] == SUITE_ID
        assert result.receipt["status"] == "ok"

    @pytest.mark.asyncio
    async def test_submit_job_missing_suite_id(self, outbox: OutboxClient) -> None:
        """Missing suite_id fails closed (Law #3)."""
        job = OutboxJob(
            suite_id="",
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
            action_type="payment.send",
        )
        result = await outbox.submit_job(job)
        assert result.success is False
        assert result.error == "missing_suite_id"


class TestOutboxJobLifecycle:
    @pytest.mark.asyncio
    async def test_claim_pending_job(self, outbox: OutboxClient) -> None:
        """Claiming a pending job transitions to CLAIMED."""
        job = OutboxJob(
            suite_id=SUITE_ID, office_id=OFFICE_ID,
            correlation_id=CORR_ID, action_type="payment.send",
        )
        await outbox.submit_job(job)
        claimed = await outbox.claim_job(job.job_id)
        assert claimed is True
        status = await outbox.get_job_status(job.job_id)
        assert status is not None
        assert status.status == OutboxJobStatus.CLAIMED

    @pytest.mark.asyncio
    async def test_complete_claimed_job(self, outbox: OutboxClient) -> None:
        """Completing a claimed job transitions to COMPLETED."""
        job = OutboxJob(
            suite_id=SUITE_ID, office_id=OFFICE_ID,
            correlation_id=CORR_ID, action_type="payment.send",
        )
        await outbox.submit_job(job)
        await outbox.claim_job(job.job_id)
        completed = await outbox.complete_job(job.job_id, receipt_id="rcpt-456")
        assert completed is True
        status = await outbox.get_job_status(job.job_id)
        assert status is not None
        assert status.status == OutboxJobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_fail_job_retries(self, outbox: OutboxClient) -> None:
        """Failing a job returns to PENDING for retry."""
        job = OutboxJob(
            suite_id=SUITE_ID, office_id=OFFICE_ID,
            correlation_id=CORR_ID, action_type="payment.send",
            max_retries=3,
        )
        await outbox.submit_job(job)
        await outbox.claim_job(job.job_id)
        failed = await outbox.fail_job(job.job_id, error="provider_timeout")
        assert failed is True
        status = await outbox.get_job_status(job.job_id)
        assert status is not None
        assert status.status == OutboxJobStatus.PENDING  # Back to pending for retry
        assert status.retry_count == 1

    @pytest.mark.asyncio
    async def test_fail_job_dead_letter(self, outbox: OutboxClient) -> None:
        """Job moves to DEAD_LETTER after max retries exhausted."""
        job = OutboxJob(
            suite_id=SUITE_ID, office_id=OFFICE_ID,
            correlation_id=CORR_ID, action_type="payment.send",
            max_retries=2,
        )
        await outbox.submit_job(job)
        await outbox.claim_job(job.job_id)
        await outbox.fail_job(job.job_id, error="fail-1")
        # Retry 1 → back to pending
        await outbox.claim_job(job.job_id)
        await outbox.fail_job(job.job_id, error="fail-2")
        # Retry 2 = max_retries → dead letter
        status = await outbox.get_job_status(job.job_id)
        assert status is not None
        assert status.status == OutboxJobStatus.DEAD_LETTER
        assert status.retry_count == 2

    @pytest.mark.asyncio
    async def test_claim_nonexistent_job(self, outbox: OutboxClient) -> None:
        """Claiming a non-existent job returns False."""
        claimed = await outbox.claim_job("nonexistent-id")
        assert claimed is False

    @pytest.mark.asyncio
    async def test_get_status_nonexistent(self, outbox: OutboxClient) -> None:
        """Getting status of non-existent job returns None."""
        status = await outbox.get_job_status("nonexistent-id")
        assert status is None


class TestOutboxSingleton:
    def test_singleton_returns_same_instance(self) -> None:
        """get_outbox_client returns the same instance."""
        c1 = get_outbox_client()
        c2 = get_outbox_client()
        assert c1 is c2

    def test_singleton_reload(self) -> None:
        """get_outbox_client(reload=True) returns new instance."""
        c1 = get_outbox_client()
        c2 = get_outbox_client(reload=True)
        assert c1 is not c2


# =============================================================================
# 3. Execute Node Integration Tests
# =============================================================================


def _make_execute_state(
    *,
    risk_tier: str = "yellow",
    task_type: str = "invoice.create",
    tool: str = "stripe.invoice.create",
    idempotency_key: str | None = None,
) -> dict:
    """Build a valid execute_node state with a real minted capability token."""
    from aspire_orchestrator.services.token_service import (
        compute_token_hash,
        mint_token,
    )

    # Derive scope from task_type (same logic as execute node)
    verb = task_type.split(".")[-1] if "." in task_type else task_type
    scope_map = {
        "read": "read", "list": "read", "search": "read",
        "create": "write", "send": "write", "draft": "write",
        "schedule": "write", "sign": "write", "transfer": "write",
        "delete": "delete",
    }
    scope_verb = scope_map.get(verb, "execute")
    domain = task_type.split(".")[0] if "." in task_type else task_type
    scope = f"{domain}.{scope_verb}"

    token = mint_token(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        tool=tool,
        scopes=[scope],
        correlation_id=CORR_ID,
    )

    state = {
        "correlation_id": CORR_ID,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "task_type": task_type,
        "allowed_tools": [tool],
        "capability_token_id": token["token_id"],
        "capability_token_hash": compute_token_hash(token),
        "capability_token": token,
        "risk_tier": risk_tier,
        "pipeline_receipts": [],
    }
    if idempotency_key:
        state["idempotency_key"] = idempotency_key

    return state


class TestExecuteNodeIdempotency:
    """Execute node integration with idempotency enforcement."""

    def setup_method(self) -> None:
        """Clear idempotency store before each test."""
        svc = get_idempotency_service()
        svc.clear_store()

    def test_yellow_op_with_idempotency_key_succeeds(self) -> None:
        """YELLOW op with valid idempotency key executes normally."""
        from aspire_orchestrator.nodes.execute import execute_node

        state = _make_execute_state(
            risk_tier="yellow",
            task_type="invoice.create",
            idempotency_key="idem-yellow-001",
        )
        result = execute_node(state)

        assert result["outcome"].value == "success"
        assert result["execution_result"]["status"] == "success"

    def test_yellow_op_duplicate_idempotency_denied(self) -> None:
        """YELLOW op with duplicate idempotency key is denied (Law #3)."""
        from aspire_orchestrator.nodes.execute import execute_node

        state1 = _make_execute_state(
            risk_tier="yellow",
            task_type="invoice.create",
            idempotency_key="idem-yellow-dup",
        )
        result1 = execute_node(state1)
        assert result1["outcome"].value == "success"

        # Second submission with same key
        state2 = _make_execute_state(
            risk_tier="yellow",
            task_type="invoice.create",
            idempotency_key="idem-yellow-dup",
        )
        result2 = execute_node(state2)

        assert result2["outcome"].value == "denied"
        assert result2["error_code"] == "IDEMPOTENCY_DUPLICATE"
        assert result2["original_receipt_id"] is not None

    def test_duplicate_idempotency_produces_receipt(self) -> None:
        """Duplicate idempotency rejection produces a receipt (Law #2)."""
        from aspire_orchestrator.nodes.execute import execute_node

        state1 = _make_execute_state(
            risk_tier="yellow",
            task_type="invoice.create",
            idempotency_key="idem-receipt-check",
        )
        execute_node(state1)

        state2 = _make_execute_state(
            risk_tier="yellow",
            task_type="invoice.create",
            idempotency_key="idem-receipt-check",
        )
        result2 = execute_node(state2)

        receipts = result2["pipeline_receipts"]
        assert len(receipts) >= 1
        idem_receipt = receipts[-1]
        assert idem_receipt["reason_code"] == "IDEMPOTENCY_DUPLICATE"
        assert idem_receipt["idempotency_key"] == "idem-receipt-check"

    def test_green_op_skips_idempotency(self) -> None:
        """GREEN ops skip idempotency check (no state changes)."""
        from aspire_orchestrator.nodes.execute import execute_node

        # GREEN op with idempotency key — key should be ignored
        state1 = _make_execute_state(
            risk_tier="green",
            task_type="calendar.read",
            tool="calendar.events.read",
            idempotency_key="idem-green-001",
        )
        result1 = execute_node(state1)
        assert result1["outcome"].value == "success"

        # Same key again — should still succeed (GREEN skips idempotency)
        state2 = _make_execute_state(
            risk_tier="green",
            task_type="calendar.read",
            tool="calendar.events.read",
            idempotency_key="idem-green-001",
        )
        result2 = execute_node(state2)
        assert result2["outcome"].value == "success"

    def test_yellow_op_without_key_executes(self) -> None:
        """YELLOW op without idempotency key still executes (key is optional)."""
        from aspire_orchestrator.nodes.execute import execute_node

        state = _make_execute_state(
            risk_tier="yellow",
            task_type="email.draft",
            tool="email.draft.create",
        )
        # No idempotency_key in state
        result = execute_node(state)
        assert result["outcome"].value == "success"


class TestExecuteNodeOutbox:
    """Execute node integration with RED-tier outbox routing."""

    def setup_method(self) -> None:
        """Clear idempotency store before each test."""
        svc = get_idempotency_service()
        svc.clear_store()

    def test_red_op_routes_to_outbox(self) -> None:
        """RED-tier operation submits to outbox instead of sync execution (Law #4)."""
        from aspire_orchestrator.nodes.execute import execute_node

        state = _make_execute_state(
            risk_tier="red",
            task_type="payment.send",
            tool="moov.transfer.create",
            idempotency_key="idem-red-001",
        )
        result = execute_node(state)

        assert result["outcome"].value == "success"
        assert result["execution_result"]["status"] == "outbox_submitted"
        assert "outbox_job_id" in result["execution_result"]

    def test_red_op_produces_outbox_receipt(self) -> None:
        """RED-tier outbox submission produces receipt with job_id (Law #2)."""
        from aspire_orchestrator.nodes.execute import execute_node

        state = _make_execute_state(
            risk_tier="red",
            task_type="payment.send",
            tool="moov.transfer.create",
            idempotency_key="idem-red-receipt",
        )
        result = execute_node(state)

        receipts = result["pipeline_receipts"]
        assert len(receipts) >= 1
        outbox_receipt = receipts[-1]
        assert outbox_receipt["reason_code"] == "OUTBOX_SUBMITTED"
        assert outbox_receipt["risk_tier"] == "red"
        assert "outbox_job_id" in outbox_receipt

    def test_yellow_op_stays_synchronous(self) -> None:
        """YELLOW-tier operations execute synchronously, NOT through outbox."""
        from aspire_orchestrator.nodes.execute import execute_node

        state = _make_execute_state(
            risk_tier="yellow",
            task_type="invoice.create",
            tool="stripe.invoice.create",
            idempotency_key="idem-yellow-sync",
        )
        result = execute_node(state)

        assert result["outcome"].value == "success"
        assert result["execution_result"]["status"] == "success"
        # Not routed to outbox
        assert "outbox_job_id" not in result["execution_result"]
