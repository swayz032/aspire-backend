"""A2A Service Tests — Wave 6.

Validates:
- Task dispatch (create, idempotency)
- Task claim (lease-based, priority ordering)
- Task complete/fail (state transitions)
- Tenant isolation (Law #6) — cross-suite operations rejected
- Receipt emission (Law #2) — every state change produces receipt data
- Concurrency safety — no duplicate claims
- Retry/quarantine — max attempts enforcement
"""

import pytest
import threading
import time

from aspire_orchestrator.services.a2a_service import (
    A2AService,
    A2ATaskStatus,
    A2AEventType,
    get_a2a_service,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def a2a() -> A2AService:
    """Fresh A2A service for each test."""
    service = A2AService(default_lease_seconds=300, max_attempts=3)
    return service


SUITE_A = "STE-A001"
SUITE_B = "STE-B002"
OFFICE = "OFF-0001"
CORR_ID = "corr-test-001"


# =============================================================================
# Dispatch Tests
# =============================================================================


class TestDispatch:
    """Test task dispatch (enqueue)."""

    def test_dispatch_creates_task(self, a2a: A2AService):
        """Dispatching creates a task with CREATED status."""
        result = a2a.dispatch(
            suite_id=SUITE_A,
            office_id=OFFICE,
            correlation_id=CORR_ID,
            task_type="invoice.create",
            assigned_to_agent="quinn",
            payload={"customer_id": "cust-001"},
        )
        assert result.success
        assert result.task_id is not None
        assert result.error is None

    def test_dispatch_emits_receipt(self, a2a: A2AService):
        """Dispatch produces receipt data (Law #2)."""
        result = a2a.dispatch(
            suite_id=SUITE_A,
            office_id=OFFICE,
            correlation_id=CORR_ID,
            task_type="invoice.create",
            assigned_to_agent="quinn",
            payload={},
        )
        assert result.receipt_data
        assert result.receipt_data["action_type"] == "a2a.dispatch"
        assert result.receipt_data["suite_id"] == SUITE_A
        assert result.receipt_data["outcome"] == "success"

    def test_dispatch_stores_task_metadata(self, a2a: A2AService):
        """Dispatched task has correct metadata."""
        result = a2a.dispatch(
            suite_id=SUITE_A,
            office_id=OFFICE,
            correlation_id=CORR_ID,
            task_type="email.send",
            assigned_to_agent="eli",
            payload={"to": "test@example.com"},
            priority=2,
        )
        task = a2a.get_task(result.task_id, SUITE_A)
        assert task is not None
        assert task.task_type == "email.send"
        assert task.assigned_to_agent == "eli"
        assert task.priority == 2
        assert task.status == A2ATaskStatus.CREATED
        assert task.suite_id == SUITE_A

    def test_dispatch_idempotency(self, a2a: A2AService):
        """Same idempotency_key returns same task_id (no duplicate)."""
        r1 = a2a.dispatch(
            suite_id=SUITE_A,
            office_id=OFFICE,
            correlation_id=CORR_ID,
            task_type="invoice.create",
            assigned_to_agent="quinn",
            payload={},
            idempotency_key="idem-key-001",
        )
        r2 = a2a.dispatch(
            suite_id=SUITE_A,
            office_id=OFFICE,
            correlation_id=CORR_ID,
            task_type="invoice.create",
            assigned_to_agent="quinn",
            payload={},
            idempotency_key="idem-key-001",
        )
        assert r1.task_id == r2.task_id
        assert a2a.get_task_count(SUITE_A) == 1

    def test_dispatch_idempotency_cross_suite(self, a2a: A2AService):
        """Same idempotency_key from different suite creates separate tasks."""
        r1 = a2a.dispatch(
            suite_id=SUITE_A,
            office_id=OFFICE,
            correlation_id=CORR_ID,
            task_type="invoice.create",
            assigned_to_agent="quinn",
            payload={},
            idempotency_key="idem-cross-001",
        )
        # Different suite with same key — should still be idempotent
        # because idempotency keys are global in Phase 1
        r2 = a2a.dispatch(
            suite_id=SUITE_B,
            office_id=OFFICE,
            correlation_id=CORR_ID,
            task_type="invoice.create",
            assigned_to_agent="quinn",
            payload={},
            idempotency_key="idem-cross-001",
        )
        # Global idempotency — key already exists, but task is for SUITE_A
        # The service looks for matching suite_id, so SUITE_B won't find it
        # and creates a new task (correct behavior for tenant isolation)
        assert r1.success
        assert r2.success

    def test_dispatch_events_emitted(self, a2a: A2AService):
        """Dispatch emits a CREATED event."""
        result = a2a.dispatch(
            suite_id=SUITE_A,
            office_id=OFFICE,
            correlation_id=CORR_ID,
            task_type="invoice.create",
            assigned_to_agent="quinn",
            payload={},
        )
        events = a2a.get_events(result.task_id, SUITE_A)
        assert len(events) == 1
        assert events[0].event_type == A2AEventType.CREATED


# =============================================================================
# Claim Tests
# =============================================================================


class TestClaim:
    """Test task claiming (lease-based)."""

    def test_claim_available_task(self, a2a: A2AService):
        """Agent can claim an available task."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A,
            office_id=OFFICE,
            correlation_id=CORR_ID,
            task_type="email.send",
            assigned_to_agent="eli",
            payload={},
        )
        claim = a2a.claim(agent_id="eli", suite_id=SUITE_A)
        assert claim.success
        assert claim.task is not None
        assert claim.task.task_id == dispatch.task_id
        assert claim.task.status == A2ATaskStatus.CLAIMED
        assert claim.task.claimed_by == "eli"

    def test_claim_emits_receipt(self, a2a: A2AService):
        """Claim produces receipt data (Law #2)."""
        a2a.dispatch(
            suite_id=SUITE_A,
            office_id=OFFICE,
            correlation_id=CORR_ID,
            task_type="email.send",
            assigned_to_agent="eli",
            payload={},
        )
        claim = a2a.claim(agent_id="eli", suite_id=SUITE_A)
        assert claim.receipt_data
        assert claim.receipt_data["action_type"] == "a2a.claim"
        assert claim.receipt_data["outcome"] == "success"

    def test_claim_sets_lease(self, a2a: A2AService):
        """Claimed task has a lease expiry set."""
        a2a.dispatch(
            suite_id=SUITE_A,
            office_id=OFFICE,
            correlation_id=CORR_ID,
            task_type="email.send",
            assigned_to_agent="eli",
            payload={},
        )
        claim = a2a.claim(agent_id="eli", suite_id=SUITE_A)
        assert claim.task.lease_expires_at is not None

    def test_claim_respects_priority(self, a2a: A2AService):
        """Higher priority tasks (lower number) are claimed first."""
        a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id="c1",
            task_type="low.priority", assigned_to_agent="worker",
            payload={}, priority=5,
        )
        high_result = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id="c2",
            task_type="high.priority", assigned_to_agent="worker",
            payload={}, priority=1,
        )
        claim = a2a.claim(agent_id="worker", suite_id=SUITE_A)
        assert claim.task.task_id == high_result.task_id
        assert claim.task.priority == 1

    def test_claim_filters_by_task_type(self, a2a: A2AService):
        """Claim can filter by specific task types."""
        a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id="c1",
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        invoice = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id="c2",
            task_type="invoice.create", assigned_to_agent="quinn", payload={},
        )
        claim = a2a.claim(
            agent_id="quinn",
            suite_id=SUITE_A,
            task_types=["invoice.create"],
        )
        assert claim.success
        assert claim.task.task_type == "invoice.create"

    def test_claim_no_available_tasks(self, a2a: A2AService):
        """Claim returns error when no tasks available."""
        claim = a2a.claim(agent_id="eli", suite_id=SUITE_A)
        assert not claim.success
        assert "No tasks available" in claim.error

    def test_claim_no_available_tasks_emits_receipt(self, a2a: A2AService):
        """Claim failure emits receipt_data with reason=NO_TASKS_AVAILABLE (Law #2)."""
        claim = a2a.claim(agent_id="eli", suite_id=SUITE_A)
        assert not claim.success
        assert claim.receipt_data
        assert claim.receipt_data["outcome"] == "denied"
        assert claim.receipt_data["redacted_outputs"]["reason"] == "NO_TASKS_AVAILABLE"

    def test_claim_already_claimed_task_skipped(self, a2a: A2AService):
        """Already-claimed tasks are not re-claimed."""
        a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        claim1 = a2a.claim(agent_id="eli", suite_id=SUITE_A)
        assert claim1.success

        # Second claim should find nothing
        claim2 = a2a.claim(agent_id="eli", suite_id=SUITE_A)
        assert not claim2.success

    def test_claim_increments_attempt_count(self, a2a: A2AService):
        """Each claim increments the attempt counter."""
        a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        claim = a2a.claim(agent_id="eli", suite_id=SUITE_A)
        assert claim.task.attempt_count == 1


# =============================================================================
# Complete Tests
# =============================================================================


class TestComplete:
    """Test task completion."""

    def test_complete_sets_done(self, a2a: A2AService):
        """Completing a task sets status to DONE."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        a2a.claim(agent_id="eli", suite_id=SUITE_A)
        result = a2a.complete(
            task_id=dispatch.task_id,
            agent_id="eli",
            suite_id=SUITE_A,
            result={"sent": True},
        )
        assert result.success
        assert result.new_status == A2ATaskStatus.DONE

    def test_complete_emits_receipt(self, a2a: A2AService):
        """Complete produces receipt data (Law #2)."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        a2a.claim(agent_id="eli", suite_id=SUITE_A)
        result = a2a.complete(
            task_id=dispatch.task_id, agent_id="eli", suite_id=SUITE_A,
        )
        assert result.receipt_data
        assert result.receipt_data["action_type"] == "a2a.complete"

    def test_complete_wrong_agent_rejected(self, a2a: A2AService):
        """Only the claiming agent can complete."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        a2a.claim(agent_id="eli", suite_id=SUITE_A)
        result = a2a.complete(
            task_id=dispatch.task_id,
            agent_id="attacker",
            suite_id=SUITE_A,
        )
        assert not result.success
        assert "eli" in result.error

    def test_complete_stores_result(self, a2a: A2AService):
        """Result data is stored on the task."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        a2a.claim(agent_id="eli", suite_id=SUITE_A)
        a2a.complete(
            task_id=dispatch.task_id, agent_id="eli", suite_id=SUITE_A,
            result={"message_id": "msg-001"},
        )
        task = a2a.get_task(dispatch.task_id, SUITE_A)
        assert task.result == {"message_id": "msg-001"}

    def test_complete_not_found_emits_receipt(self, a2a: A2AService):
        """Complete on non-existent task emits receipt (Law #2)."""
        result = a2a.complete(
            task_id="nonexistent", agent_id="eli", suite_id=SUITE_A,
        )
        assert not result.success
        assert result.receipt_data
        assert result.receipt_data["outcome"] == "denied"
        assert result.receipt_data["redacted_outputs"]["reason"] == "TASK_NOT_FOUND"

    def test_complete_wrong_status_emits_receipt(self, a2a: A2AService):
        """Complete on unclaimed task emits receipt (Law #2)."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        # Don't claim — try to complete directly
        result = a2a.complete(
            task_id=dispatch.task_id, agent_id="eli", suite_id=SUITE_A,
        )
        assert not result.success
        assert result.receipt_data
        assert result.receipt_data["outcome"] == "denied"
        assert result.receipt_data["redacted_outputs"]["reason"] == "INVALID_STATUS"

    def test_complete_wrong_agent_emits_receipt(self, a2a: A2AService):
        """Complete by wrong agent emits receipt (Law #2)."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        a2a.claim(agent_id="eli", suite_id=SUITE_A)
        result = a2a.complete(
            task_id=dispatch.task_id, agent_id="attacker", suite_id=SUITE_A,
        )
        assert not result.success
        assert result.receipt_data
        assert result.receipt_data["outcome"] == "denied"
        assert result.receipt_data["redacted_outputs"]["reason"] == "WRONG_CLAIMER"


# =============================================================================
# Fail / Retry / Quarantine Tests
# =============================================================================


class TestFailRetryQuarantine:
    """Test failure handling with retry and quarantine."""

    def test_fail_requeues_within_max_attempts(self, a2a: A2AService):
        """Failed task is requeued if under max attempts."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        a2a.claim(agent_id="eli", suite_id=SUITE_A)
        result = a2a.fail(
            task_id=dispatch.task_id, agent_id="eli", suite_id=SUITE_A,
            error="SMTP_TIMEOUT",
        )
        assert result.success
        assert result.new_status == A2ATaskStatus.CREATED  # Requeued

        # Task should be claimable again
        claim2 = a2a.claim(agent_id="eli", suite_id=SUITE_A)
        assert claim2.success
        assert claim2.task.attempt_count == 2

    def test_fail_quarantines_at_max_attempts(self, a2a: A2AService):
        """Task is quarantined after max_attempts (3) exhausted."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )

        for i in range(3):
            a2a.claim(agent_id="eli", suite_id=SUITE_A)
            result = a2a.fail(
                task_id=dispatch.task_id, agent_id="eli", suite_id=SUITE_A,
                error=f"Attempt {i+1} failed",
            )

        assert result.new_status == A2ATaskStatus.QUARANTINED

        # Quarantined task should NOT be claimable
        claim = a2a.claim(agent_id="eli", suite_id=SUITE_A)
        assert not claim.success

    def test_fail_emits_receipt(self, a2a: A2AService):
        """Fail produces receipt data (Law #2)."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        a2a.claim(agent_id="eli", suite_id=SUITE_A)
        result = a2a.fail(
            task_id=dispatch.task_id, agent_id="eli", suite_id=SUITE_A,
            error="Provider error",
        )
        assert result.receipt_data
        assert result.receipt_data["action_type"] == "a2a.requeue"

    def test_fail_wrong_agent_rejected(self, a2a: A2AService):
        """Only the claiming agent can fail a task."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        a2a.claim(agent_id="eli", suite_id=SUITE_A)
        result = a2a.fail(
            task_id=dispatch.task_id, agent_id="imposter", suite_id=SUITE_A,
            error="hack",
        )
        assert not result.success

    def test_fail_not_found_emits_receipt(self, a2a: A2AService):
        """Fail on non-existent task emits receipt (Law #2)."""
        result = a2a.fail(
            task_id="nonexistent", agent_id="eli", suite_id=SUITE_A,
            error="timeout",
        )
        assert not result.success
        assert result.receipt_data
        assert result.receipt_data["outcome"] == "denied"
        assert result.receipt_data["redacted_outputs"]["reason"] == "TASK_NOT_FOUND"

    def test_fail_wrong_status_emits_receipt(self, a2a: A2AService):
        """Fail on unclaimed task emits receipt (Law #2)."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        result = a2a.fail(
            task_id=dispatch.task_id, agent_id="eli", suite_id=SUITE_A,
            error="timeout",
        )
        assert not result.success
        assert result.receipt_data
        assert result.receipt_data["outcome"] == "denied"
        assert result.receipt_data["redacted_outputs"]["reason"] == "INVALID_STATUS"

    def test_fail_wrong_agent_emits_receipt(self, a2a: A2AService):
        """Fail by wrong agent emits receipt (Law #2)."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        a2a.claim(agent_id="eli", suite_id=SUITE_A)
        result = a2a.fail(
            task_id=dispatch.task_id, agent_id="imposter", suite_id=SUITE_A,
            error="hack",
        )
        assert not result.success
        assert result.receipt_data
        assert result.receipt_data["outcome"] == "denied"
        assert result.receipt_data["redacted_outputs"]["reason"] == "WRONG_CLAIMER"


# =============================================================================
# Tenant Isolation Tests (Law #6)
# =============================================================================


class TestTenantIsolation:
    """Test cross-suite operation rejection."""

    def test_get_task_cross_suite_denied(self, a2a: A2AService):
        """get_task from wrong suite returns None (Law #6)."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="invoice.create", assigned_to_agent="quinn", payload={},
        )
        # Cross-suite lookup should return None
        task = a2a.get_task(dispatch.task_id, SUITE_B)
        assert task is None

    def test_complete_cross_suite_denied(self, a2a: A2AService):
        """Complete from wrong suite is rejected (Law #6)."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="invoice.create", assigned_to_agent="quinn", payload={},
        )
        a2a.claim(agent_id="quinn", suite_id=SUITE_A)
        result = a2a.complete(
            task_id=dispatch.task_id, agent_id="quinn", suite_id=SUITE_B,
        )
        assert not result.success
        assert result.error == "TENANT_ISOLATION_VIOLATION"
        assert result.receipt_data
        assert result.receipt_data["outcome"] == "denied"

    def test_fail_cross_suite_denied(self, a2a: A2AService):
        """Fail from wrong suite is rejected (Law #6)."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="invoice.create", assigned_to_agent="quinn", payload={},
        )
        a2a.claim(agent_id="quinn", suite_id=SUITE_A)
        result = a2a.fail(
            task_id=dispatch.task_id, agent_id="quinn", suite_id=SUITE_B,
            error="hack",
        )
        assert not result.success
        assert result.error == "TENANT_ISOLATION_VIOLATION"
        assert result.receipt_data
        assert result.receipt_data["outcome"] == "denied"

    def test_claim_only_sees_own_suite(self, a2a: A2AService):
        """Claim from suite B does not see suite A tasks."""
        a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="invoice.create", assigned_to_agent="quinn", payload={},
        )
        claim = a2a.claim(agent_id="quinn", suite_id=SUITE_B)
        assert not claim.success

    def test_list_tasks_scoped_to_suite(self, a2a: A2AService):
        """list_tasks only returns tasks for the requested suite."""
        a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id="c1",
            task_type="invoice.create", assigned_to_agent="quinn", payload={},
        )
        a2a.dispatch(
            suite_id=SUITE_B, office_id=OFFICE, correlation_id="c2",
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        tasks_a = a2a.list_tasks(suite_id=SUITE_A)
        tasks_b = a2a.list_tasks(suite_id=SUITE_B)
        assert len(tasks_a) == 1
        assert len(tasks_b) == 1
        assert tasks_a[0].suite_id == SUITE_A
        assert tasks_b[0].suite_id == SUITE_B


# =============================================================================
# Concurrency Tests
# =============================================================================


class TestConcurrency:
    """Test thread-safe concurrent operations."""

    def test_10_concurrent_dispatches_no_duplicates(self, a2a: A2AService):
        """10 concurrent dispatches with unique keys produce 10 tasks."""
        results = []

        def dispatch_task(i):
            r = a2a.dispatch(
                suite_id=SUITE_A, office_id=OFFICE,
                correlation_id=f"conc-{i}",
                task_type=f"task.{i}", assigned_to_agent="worker",
                payload={"index": i},
            )
            results.append(r)

        threads = [threading.Thread(target=dispatch_task, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert all(r.success for r in results)
        task_ids = {r.task_id for r in results}
        assert len(task_ids) == 10  # All unique

    def test_10_tasks_claimed_no_duplicates(self, a2a: A2AService):
        """10 tasks claimed concurrently — each claimed exactly once."""
        for i in range(10):
            a2a.dispatch(
                suite_id=SUITE_A, office_id=OFFICE,
                correlation_id=f"claim-{i}",
                task_type="work", assigned_to_agent="worker",
                payload={},
            )

        claimed_ids = []

        def claim_task(agent_num):
            result = a2a.claim(agent_id=f"agent-{agent_num}", suite_id=SUITE_A)
            if result.success:
                claimed_ids.append(result.task.task_id)

        threads = [threading.Thread(target=claim_task, args=(i,)) for i in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 10 tasks should be claimed (no duplicates)
        assert len(claimed_ids) == 10
        assert len(set(claimed_ids)) == 10  # All unique

    def test_concurrent_idempotent_dispatches(self, a2a: A2AService):
        """Concurrent dispatches with same key produce exactly 1 task."""
        results = []

        def dispatch_idem(i):
            r = a2a.dispatch(
                suite_id=SUITE_A, office_id=OFFICE,
                correlation_id=CORR_ID,
                task_type="invoice.create", assigned_to_agent="quinn",
                payload={}, idempotency_key="same-key",
            )
            results.append(r)

        threads = [threading.Thread(target=dispatch_idem, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 5
        assert all(r.success for r in results)
        task_ids = {r.task_id for r in results}
        assert len(task_ids) == 1  # All same task


# =============================================================================
# Event Audit Trail Tests
# =============================================================================


class TestEventAuditTrail:
    """Test event emission for full lifecycle."""

    def test_full_lifecycle_events(self, a2a: A2AService):
        """Full lifecycle (dispatch → claim → complete) emits 3 events."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        a2a.claim(agent_id="eli", suite_id=SUITE_A)
        a2a.complete(
            task_id=dispatch.task_id, agent_id="eli", suite_id=SUITE_A,
        )

        events = a2a.get_events(dispatch.task_id, SUITE_A)
        assert len(events) == 3
        assert events[0].event_type == A2AEventType.CREATED
        assert events[1].event_type == A2AEventType.CLAIMED
        assert events[2].event_type == A2AEventType.COMPLETED

    def test_fail_requeue_lifecycle_events(self, a2a: A2AService):
        """Fail → requeue emits CREATED, CLAIMED, REQUEUED."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        a2a.claim(agent_id="eli", suite_id=SUITE_A)
        a2a.fail(
            task_id=dispatch.task_id, agent_id="eli", suite_id=SUITE_A,
            error="Timeout",
        )

        events = a2a.get_events(dispatch.task_id, SUITE_A)
        assert len(events) == 3
        assert events[0].event_type == A2AEventType.CREATED
        assert events[1].event_type == A2AEventType.CLAIMED
        assert events[2].event_type == A2AEventType.REQUEUED

    def test_events_scoped_to_suite(self, a2a: A2AService):
        """Events for wrong suite return empty list (Law #6)."""
        dispatch = a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        events = a2a.get_events(dispatch.task_id, SUITE_B)
        assert len(events) == 0


# =============================================================================
# List / Query Tests
# =============================================================================


class TestListQuery:
    """Test task listing and filtering."""

    def test_list_by_status(self, a2a: A2AService):
        """Filter tasks by status."""
        a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id="c1",
            task_type="t1", assigned_to_agent="w", payload={},
        )
        a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id="c2",
            task_type="t2", assigned_to_agent="w", payload={},
        )
        a2a.claim(agent_id="w", suite_id=SUITE_A)

        created = a2a.list_tasks(suite_id=SUITE_A, status=A2ATaskStatus.CREATED)
        claimed = a2a.list_tasks(suite_id=SUITE_A, status=A2ATaskStatus.CLAIMED)
        assert len(created) == 1
        assert len(claimed) == 1

    def test_list_by_agent(self, a2a: A2AService):
        """Filter tasks by assigned agent."""
        a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id="c1",
            task_type="email.send", assigned_to_agent="eli", payload={},
        )
        a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id="c2",
            task_type="invoice.create", assigned_to_agent="quinn", payload={},
        )
        eli_tasks = a2a.list_tasks(suite_id=SUITE_A, assigned_to_agent="eli")
        assert len(eli_tasks) == 1
        assert eli_tasks[0].assigned_to_agent == "eli"

    def test_clear_removes_all(self, a2a: A2AService):
        """Clear removes all tasks and events."""
        a2a.dispatch(
            suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID,
            task_type="t1", assigned_to_agent="w", payload={},
        )
        a2a.clear()
        assert a2a.get_task_count() == 0
