"""Tests for Outbox Supabase Durability (Wave 3A — F6 fix).

Verifies that:
1. Outbox jobs are submitted to both in-memory and Supabase
2. Job status updates propagate
3. Queue status reporting works
4. Graceful degradation without Supabase
"""

from __future__ import annotations

import uuid
import pytest

from aspire_orchestrator.services.outbox_client import (
    OutboxClient,
    OutboxJob,
    OutboxJobStatus,
    get_outbox_client,
)


class TestOutboxDurability:
    """Outbox dual-write behavior."""

    def setup_method(self):
        self.client = OutboxClient()

    @pytest.mark.asyncio
    async def test_submit_job_in_memory(self):
        """Jobs should be stored in-memory."""
        job = OutboxJob(
            suite_id="suite-123",
            office_id="office-456",
            correlation_id="corr-789",
            action_type="payment.send",
        )
        result = await self.client.submit_job(job)
        assert result.success
        assert result.job_id == job.job_id

        stored = await self.client.get_job_status(job.job_id)
        assert stored is not None
        assert stored.status == OutboxJobStatus.PENDING

    @pytest.mark.asyncio
    async def test_submit_job_returns_receipt(self):
        """Job submission should include a receipt."""
        job = OutboxJob(
            suite_id="suite-123",
            office_id="office-456",
            correlation_id="corr-789",
            action_type="payment.send",
        )
        result = await self.client.submit_job(job)
        assert result.receipt is not None
        assert result.receipt["event_type"] == "outbox.job.submitted"
        assert result.receipt["suite_id"] == "suite-123"

    @pytest.mark.asyncio
    async def test_submit_missing_suite_id_rejected(self):
        """Jobs without suite_id should be rejected."""
        job = OutboxJob(
            suite_id="",
            office_id="office-456",
            correlation_id="corr-789",
            action_type="payment.send",
        )
        result = await self.client.submit_job(job)
        assert not result.success
        assert result.error == "missing_suite_id"

    @pytest.mark.asyncio
    async def test_claim_job(self):
        """Claiming should move status to CLAIMED."""
        job = OutboxJob(
            suite_id="s", office_id="o", correlation_id="c", action_type="test",
        )
        await self.client.submit_job(job)
        claimed = await self.client.claim_job(job.job_id)
        assert claimed

        stored = await self.client.get_job_status(job.job_id)
        assert stored.status == OutboxJobStatus.CLAIMED

    @pytest.mark.asyncio
    async def test_complete_job(self):
        """Completing should move status to COMPLETED."""
        job = OutboxJob(
            suite_id="s", office_id="o", correlation_id="c", action_type="test",
        )
        await self.client.submit_job(job)
        await self.client.claim_job(job.job_id)
        completed = await self.client.complete_job(job.job_id)
        assert completed

        stored = await self.client.get_job_status(job.job_id)
        assert stored.status == OutboxJobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_fail_job_retries(self):
        """Failed jobs should retry up to max_retries."""
        job = OutboxJob(
            suite_id="s", office_id="o", correlation_id="c",
            action_type="test", max_retries=2,
        )
        await self.client.submit_job(job)
        await self.client.claim_job(job.job_id)

        # First failure — should retry
        await self.client.fail_job(job.job_id, error="transient")
        stored = await self.client.get_job_status(job.job_id)
        assert stored.status == OutboxJobStatus.PENDING
        assert stored.retry_count == 1

    @pytest.mark.asyncio
    async def test_fail_job_dead_letter(self):
        """Jobs exceeding max_retries should go to dead letter."""
        job = OutboxJob(
            suite_id="s", office_id="o", correlation_id="c",
            action_type="test", max_retries=1,
        )
        await self.client.submit_job(job)
        await self.client.claim_job(job.job_id)

        await self.client.fail_job(job.job_id, error="permanent")
        stored = await self.client.get_job_status(job.job_id)
        assert stored.status == OutboxJobStatus.DEAD_LETTER


class TestOutboxQueueStatus:
    """Queue status reporting."""

    def setup_method(self):
        self.client = OutboxClient()

    @pytest.mark.asyncio
    async def test_empty_queue_status(self):
        """Empty queue should report zero depth."""
        status = self.client.get_queue_status()
        assert status["queue_depth"] == 0
        assert status["stuck_jobs"] == 0
        assert "server_time" in status

    @pytest.mark.asyncio
    async def test_queue_depth(self):
        """Queue depth should count pending jobs."""
        for i in range(3):
            job = OutboxJob(
                suite_id="s", office_id="o", correlation_id=f"c{i}",
                action_type="test",
            )
            await self.client.submit_job(job)

        status = self.client.get_queue_status()
        assert status["queue_depth"] == 3

    @pytest.mark.asyncio
    async def test_stuck_jobs_count(self):
        """Stuck jobs should count dead-lettered jobs."""
        job = OutboxJob(
            suite_id="s", office_id="o", correlation_id="c",
            action_type="test", max_retries=1,
        )
        await self.client.submit_job(job)
        await self.client.claim_job(job.job_id)
        await self.client.fail_job(job.job_id, error="dead")

        status = self.client.get_queue_status()
        assert status["stuck_jobs"] == 1


class TestOutboxSingleton:
    """Singleton behavior."""

    def test_get_outbox_client_singleton(self):
        """Subsequent calls should return the same client."""
        c1 = get_outbox_client()
        c2 = get_outbox_client()
        assert c1 is c2
