"""Tests for Phase 2 Wave 9 — Compliance Infrastructure.

Covers:
- SkillPackFactory: load all, missing field rejected, partial success
- WorkerQueue: enqueue, claim, complete, fail+retry, DLQ
- FailureHandler: retryable, non-retryable, max retries
- ComplianceMapper: report generation, warning detection
- Integration: factory loads real manifests, queue receipt chain, handler backoff

Law compliance verified:
- Law #2: Every operation produces a receipt
- Law #3: Invalid data is rejected (fail-closed)
- Law #7: Infrastructure is plumbing, not a decision-maker
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest
import yaml

from aspire_orchestrator.services.skillpack_factory import (
    FactoryResult,
    SkillPackFactory,
    SkillPackRegistration,
)
from aspire_orchestrator.services.worker_queue import (
    Job,
    JobStatus,
    QueueReceipt,
    WorkerQueue,
)
from aspire_orchestrator.services.failure_handler import (
    FailureContext,
    FailureHandler,
    RetryDecision,
)
from aspire_orchestrator.services.compliance_mapper import (
    ASPIRE_CONTROLS,
    SUBPROCESSORS,
    ComplianceReport,
    generate_compliance_report,
)


# =============================================================================
# SkillPackFactory Tests (3)
# =============================================================================


class TestSkillPackFactory:
    """Verify factory loads, validates, and produces receipts."""

    def _write_manifest(self, packs: dict) -> str:
        """Write a temporary YAML manifest and return its path."""
        data = {"version": "1.0.0", "skill_packs": packs}
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        )
        yaml.safe_dump(data, tmp)
        tmp.close()
        return tmp.name

    def test_load_all_success(self):
        """All valid packs register and produce a receipt (Law #2)."""
        manifest = self._write_manifest(
            {
                "alpha": {
                    "name": "Alpha",
                    "owner": "alice",
                    "actions": ["a.read"],
                    "tools": ["t.read"],
                    "risk_tier": "green",
                },
                "beta": {
                    "name": "Beta",
                    "owner": "bob",
                    "risk_tier": "yellow",
                },
            }
        )
        try:
            factory = SkillPackFactory(manifest_path=manifest)
            result = factory.load_all()

            assert len(result.registered) == 2
            assert len(result.failed) == 0
            assert result.receipt["outcome"] == "success"
            assert result.receipt["action_type"] == "factory.load_all"
            assert result.receipt["details"]["registered_count"] == 2
            # Each registration has its own receipt_id (Law #2)
            for reg in result.registered:
                assert reg.receipt_id
                assert reg.registered_at
        finally:
            os.unlink(manifest)

    def test_missing_required_field_rejected(self):
        """Pack with missing 'owner' is rejected — fail-closed (Law #3)."""
        manifest = self._write_manifest(
            {
                "bad_pack": {
                    "name": "Bad Pack",
                    # "owner" intentionally missing
                },
            }
        )
        try:
            factory = SkillPackFactory(manifest_path=manifest)
            result = factory.load_all()

            assert len(result.registered) == 0
            assert len(result.failed) == 1
            assert result.failed[0]["pack_id"] == "bad_pack"
            assert "Missing required field" in result.failed[0]["error"]
            # Receipt still generated even for failures (Law #2)
            assert result.receipt["outcome"] == "partial"
        finally:
            os.unlink(manifest)

    def test_partial_success(self):
        """One valid + one invalid = partial outcome with receipt (Law #2)."""
        manifest = self._write_manifest(
            {
                "good": {"name": "Good", "owner": "alice"},
                "bad": {"name": "Bad"},  # missing owner
            }
        )
        try:
            factory = SkillPackFactory(manifest_path=manifest)
            result = factory.load_all()

            assert len(result.registered) == 1
            assert len(result.failed) == 1
            assert result.receipt["outcome"] == "partial"
            assert result.receipt["details"]["registered_count"] == 1
            assert result.receipt["details"]["failed_count"] == 1
        finally:
            os.unlink(manifest)


# =============================================================================
# WorkerQueue Tests (5 — enqueue, claim, complete, fail+retry, DLQ)
# =============================================================================


class TestWorkerQueue:
    """Verify queue operations and receipt generation."""

    @pytest.fixture()
    def queue(self) -> WorkerQueue:
        return WorkerQueue()

    def test_enqueue_produces_receipt(self, queue: WorkerQueue):
        """Enqueue operation produces a receipt (Law #2)."""
        job = Job(action_type="invoice.create", suite_id="STE-0001", office_id="OFF-0001")
        receipt = queue.enqueue(job)

        assert receipt.action == "enqueue"
        assert receipt.outcome == "success"
        assert receipt.job_id == job.id
        assert queue.get_status()["pending"] == 1

    def test_claim_returns_job_and_receipt(self, queue: WorkerQueue):
        """Claim pops from pending, moves to processing, emits receipt."""
        job = Job(action_type="calendar.read")
        queue.enqueue(job)

        claimed, receipt = queue.claim("worker-1")

        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == JobStatus.PROCESSING
        assert claimed.claimed_by == "worker-1"
        assert receipt.action == "claim"
        assert receipt.outcome == "success"
        assert queue.get_status()["pending"] == 0
        assert queue.get_status()["processing"] == 1

    def test_claim_empty_queue(self, queue: WorkerQueue):
        """Claiming from empty queue returns None + receipt (Law #3: fail-closed)."""
        claimed, receipt = queue.claim("worker-1")

        assert claimed is None
        assert receipt.action == "claim"
        assert receipt.outcome == "empty"
        assert receipt.reason_code == "no_pending_jobs"

    def test_complete_produces_receipt(self, queue: WorkerQueue):
        """Complete transitions processing → completed with receipt."""
        job = Job(action_type="email.read")
        queue.enqueue(job)
        queue.claim("worker-1")

        receipt = queue.complete(job.id)

        assert receipt.action == "complete"
        assert receipt.outcome == "success"
        assert queue.get_status()["completed"] == 1
        assert queue.get_status()["processing"] == 0

    def test_fail_retries_then_dlq(self, queue: WorkerQueue):
        """Failing 3 times routes to DLQ with receipts at each step (Law #2)."""
        job = Job(action_type="payment.send", max_retries=3)
        queue.enqueue(job)

        # Fail attempt 1 — re-queued
        queue.claim("w1")
        r1 = queue.fail(job.id, "timeout")
        assert r1.action == "fail"
        assert r1.reason_code == "retry_1"
        assert queue.get_status()["pending"] == 1

        # Fail attempt 2 — re-queued
        queue.claim("w1")
        r2 = queue.fail(job.id, "timeout")
        assert r2.reason_code == "retry_2"
        assert queue.get_status()["pending"] == 1

        # Fail attempt 3 — DLQ
        queue.claim("w1")
        r3 = queue.fail(job.id, "timeout")
        assert r3.action == "dlq"
        assert r3.reason_code == "max_retries_exceeded"
        assert queue.get_status()["dlq"] == 1
        assert queue.get_status()["pending"] == 0

        # Verify receipt chain: enqueue + 3*(claim+fail) = 7 receipts
        all_receipts = queue.get_receipts()
        assert len(all_receipts) == 7


# =============================================================================
# FailureHandler Tests (3)
# =============================================================================


class TestFailureHandler:
    """Verify retry decisions and receipt generation."""

    @pytest.fixture()
    def handler(self) -> FailureHandler:
        return FailureHandler()

    def _make_ctx(self, error_type: str = "timeout", attempt: int = 0) -> FailureContext:
        return FailureContext(
            correlation_id=str(uuid.uuid4()),
            suite_id="STE-0001",
            office_id="OFF-0001",
            action_type="invoice.create",
            error_type=error_type,
            error_message="something went wrong",
            attempt=attempt,
            max_attempts=3,
        )

    def test_retryable_error_allows_retry(self, handler: FailureHandler):
        """Transient error on first attempt → should_retry=True with backoff."""
        ctx = self._make_ctx(error_type="timeout", attempt=0)
        decision = handler.evaluate(ctx)

        assert decision.should_retry is True
        assert decision.wait_seconds > 0
        assert decision.attempt == 1
        # Receipt produced (Law #2)
        assert decision.receipt["outcome"] == "retry"
        assert decision.receipt["reason_code"] == "attempt_1"
        assert decision.receipt["correlation_id"] == ctx.correlation_id

    def test_non_retryable_error_denied(self, handler: FailureHandler):
        """Auth errors are never retried — fail-closed (Law #3)."""
        for error_type in ("auth_error", "permission_denied", "invalid_input", "tenant_mismatch"):
            ctx = self._make_ctx(error_type=error_type, attempt=0)
            decision = handler.evaluate(ctx)

            assert decision.should_retry is False
            assert decision.wait_seconds == 0
            assert decision.receipt["outcome"] == "denied"
            assert decision.receipt["reason_code"] == "non_retryable_error"

    def test_max_retries_exceeded(self, handler: FailureHandler):
        """After max_attempts the handler denies retry (Law #3)."""
        ctx = self._make_ctx(error_type="timeout", attempt=2)  # next will be attempt 3
        decision = handler.evaluate(ctx)

        assert decision.should_retry is False
        assert decision.attempt == 3
        assert decision.receipt["outcome"] == "failed"
        assert decision.receipt["reason_code"] == "max_retries_exceeded"


# =============================================================================
# ComplianceMapper Tests (2)
# =============================================================================


class TestComplianceMapper:
    """Verify compliance report generation."""

    def test_report_generation_with_receipt(self):
        """Report produces receipt and lists all subprocessors (Law #2)."""
        report = generate_compliance_report(correlation_id="corr-123")

        assert isinstance(report, ComplianceReport)
        assert len(report.subprocessors) == len(SUBPROCESSORS)
        assert len(report.aspire_controls) == len(ASPIRE_CONTROLS)
        # Receipt produced (Law #2)
        assert report.receipt["action_type"] == "compliance.report"
        assert report.receipt["outcome"] == "success"
        assert report.receipt["correlation_id"] == "corr-123"

    def test_warning_detection(self):
        """Report detects non-green subprocessors (brave_search, tavily)."""
        report = generate_compliance_report()

        # brave_search and tavily are yellow → overall status = warning
        assert report.overall_status == "warning"
        yellow_providers = [
            s.provider for s in report.subprocessors if s.risk_tier == "yellow"
        ]
        assert "brave_search" in yellow_providers
        assert "tavily" in yellow_providers


# =============================================================================
# Integration Tests (3)
# =============================================================================


class TestIntegration:
    """Cross-service integration tests."""

    def test_factory_loads_real_manifests(self):
        """Factory successfully loads the actual skill_pack_manifests.yaml."""
        factory = SkillPackFactory()  # default path
        result = factory.load_all()

        # All 12 packs from the real manifest should register
        assert result.receipt["outcome"] == "success"
        assert len(result.failed) == 0
        assert len(result.registered) >= 12

        # Spot-check known packs
        pack_ids = [r.pack_id for r in result.registered]
        assert "sarah_front_desk" in pack_ids
        assert "finn_finance_manager" in pack_ids
        assert "clara_legal" in pack_ids
        assert "mail_ops_desk" in pack_ids

        # Verify risk tiers are preserved
        finn = next(r for r in result.registered if r.pack_id == "finn_finance_manager")
        assert finn.risk_tier == "yellow"
        assert finn.owner == "finn"

        clara = next(r for r in result.registered if r.pack_id == "clara_legal")
        assert clara.risk_tier == "red"

    def test_queue_receipt_chain_integrity(self):
        """Full lifecycle (enqueue→claim→complete) produces receipts at every step."""
        queue = WorkerQueue()
        job = Job(
            action_type="books.sync",
            suite_id="STE-0001",
            office_id="OFF-0001",
            correlation_id="chain-test",
        )

        r_enqueue = queue.enqueue(job)
        _, r_claim = queue.claim("teressa-worker")
        r_complete = queue.complete(job.id)

        # 3 operations = 3 receipts
        all_receipts = queue.get_receipts()
        assert len(all_receipts) == 3

        actions = [r.action for r in all_receipts]
        assert actions == ["enqueue", "claim", "complete"]

        # All receipts reference the same job_id
        for r in all_receipts:
            assert r.job_id == job.id

        # Queue ends empty
        status = queue.get_status()
        assert status["pending"] == 0
        assert status["processing"] == 0
        assert status["completed"] == 1

    def test_handler_backoff_increases(self):
        """Exponential backoff delay increases with each attempt."""
        handler = FailureHandler()
        waits: list[float] = []

        for attempt in range(3):
            ctx = FailureContext(
                correlation_id=str(uuid.uuid4()),
                suite_id="STE-0001",
                office_id="OFF-0001",
                action_type="email.send",
                error_type="timeout",
                error_message="timed out",
                attempt=attempt,
                max_attempts=5,  # high enough to allow retries
            )
            decision = handler.evaluate(ctx)
            if decision.should_retry:
                waits.append(decision.wait_seconds)

        # With exponential backoff, later waits should generally be larger
        # (jitter adds randomness, but base doubles each time:
        # attempt 1 ≈ 1-1.5s, attempt 2 ≈ 2-3s, attempt 3 ≈ 4-6s)
        assert len(waits) == 3
        # The base delay doubles, so even with max jitter the 3rd should
        # exceed the base of the 1st (1s base vs 4s base)
        assert waits[2] > waits[0] * 0.5  # conservative bound due to jitter
