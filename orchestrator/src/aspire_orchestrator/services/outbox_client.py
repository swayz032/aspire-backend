"""Outbox Client — Phase 3 Wave 5.

Wires the orchestrator to Supabase outbox Edge Functions for durable
async execution of RED-tier operations.

Pattern:
  YELLOW ops: Synchronous execution (existing path)
  RED ops: Submit to outbox -> durable processing -> receipt on completion

The outbox pattern ensures that if the orchestrator crashes mid-execution,
the operation is retried from the outbox queue.

Ecosystem source: platform/trust-spine/06_ADDONS/db/sql/outbox_claim.sql

Law compliance:
  - Law #2: Every outbox job produces receipts (submission + completion/failure).
  - Law #3: Outbox unavailable -> fail-closed (do NOT execute without durability).
  - Law #4: Only RED-tier operations go through outbox.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# =============================================================================
# Models
# =============================================================================


class OutboxJobStatus(str, Enum):
    """Outbox job lifecycle states."""

    PENDING = "pending"
    CLAIMED = "claimed"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class OutboxJob(BaseModel):
    """An outbox job for durable async execution."""

    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    suite_id: str
    office_id: str
    correlation_id: str
    action_type: str
    risk_tier: str = "red"
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    capability_token_id: str | None = None
    status: OutboxJobStatus = OutboxJobStatus.PENDING
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    max_retries: int = 3
    retry_count: int = 0


class OutboxSubmitResult(BaseModel):
    """Result of submitting a job to the outbox."""

    success: bool
    job_id: str
    status: OutboxJobStatus = OutboxJobStatus.PENDING
    error: str | None = None
    receipt: dict[str, Any] | None = None


# =============================================================================
# Outbox Client
# =============================================================================


class OutboxClient:
    """Client for submitting and polling outbox jobs.

    Phase 3: In-memory implementation for development.
    Production: Supabase outbox Edge Functions.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, OutboxJob] = {}
        logger.info("OutboxClient initialized (in-memory mode)")

    async def submit_job(self, job: OutboxJob) -> OutboxSubmitResult:
        """Submit a job to the outbox for durable execution.

        Args:
            job: The outbox job to submit.

        Returns:
            OutboxSubmitResult with job_id and status.
        """
        if not job.suite_id:
            return OutboxSubmitResult(
                success=False,
                job_id=job.job_id,
                error="missing_suite_id",
            )

        self._jobs[job.job_id] = job

        # Build submission receipt
        receipt = {
            "receipt_version": "1.0",
            "receipt_id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": "outbox.job.submitted",
            "suite_id": job.suite_id,
            "office_id": job.office_id,
            "correlation_id": job.correlation_id,
            "actor": "service:outbox_client",
            "status": "ok",
            "data": {
                "job_id": job.job_id,
                "action_type": job.action_type,
                "risk_tier": job.risk_tier,
                "idempotency_key": job.idempotency_key,
            },
            "policy": {
                "decision": "allow",
                "policy_id": "outbox-v1",
                "reasons": [],
            },
            "redactions": [],
        }

        logger.info(
            "Outbox job submitted: job_id=%s action=%s suite=%s",
            job.job_id[:8], job.action_type, job.suite_id[:8],
        )

        return OutboxSubmitResult(
            success=True,
            job_id=job.job_id,
            status=OutboxJobStatus.PENDING,
            receipt=receipt,
        )

    async def get_job_status(self, job_id: str) -> OutboxJob | None:
        """Poll the status of an outbox job."""
        return self._jobs.get(job_id)

    async def claim_job(self, job_id: str) -> bool:
        """Claim a pending job for execution."""
        job = self._jobs.get(job_id)
        if job and job.status == OutboxJobStatus.PENDING:
            job.status = OutboxJobStatus.CLAIMED
            return True
        return False

    async def complete_job(
        self,
        job_id: str,
        *,
        receipt_id: str | None = None,
    ) -> bool:
        """Mark a job as completed."""
        job = self._jobs.get(job_id)
        if job and job.status in (OutboxJobStatus.CLAIMED, OutboxJobStatus.EXECUTING):
            job.status = OutboxJobStatus.COMPLETED
            return True
        return False

    async def fail_job(
        self,
        job_id: str,
        *,
        error: str,
    ) -> bool:
        """Mark a job as failed. May retry if under max_retries."""
        job = self._jobs.get(job_id)
        if not job:
            return False

        job.retry_count += 1
        if job.retry_count >= job.max_retries:
            job.status = OutboxJobStatus.DEAD_LETTER
            logger.warning(
                "Outbox job moved to dead letter: job_id=%s retries=%d",
                job_id[:8], job.retry_count,
            )
        else:
            job.status = OutboxJobStatus.PENDING  # Back to pending for retry
            logger.info(
                "Outbox job retrying: job_id=%s retry=%d/%d",
                job_id[:8], job.retry_count, job.max_retries,
            )
        return True

    def get_queue_status(self) -> dict[str, Any]:
        """Return OutboxStatus per OpenAPI contract.

        Returns queue_depth, oldest_age_seconds, stuck_jobs, server_time.
        """
        now = datetime.now(timezone.utc)
        active_statuses = (OutboxJobStatus.PENDING, OutboxJobStatus.CLAIMED, OutboxJobStatus.EXECUTING)
        pending_jobs = [
            j for j in self._jobs.values()
            if j.status in active_statuses
        ]
        dead_letter_jobs = [
            j for j in self._jobs.values()
            if j.status == OutboxJobStatus.DEAD_LETTER
        ]
        oldest_age = 0.0
        stuck = len(dead_letter_jobs)  # Dead-lettered jobs always count as stuck
        for j in pending_jobs:
            try:
                created = datetime.fromisoformat(j.created_at)
                age = (now - created).total_seconds()
                oldest_age = max(oldest_age, age)
                # Stuck = pending for >5 minutes
                if age > 300:
                    stuck += 1
            except (ValueError, TypeError):
                pass

        return {
            "queue_depth": len(pending_jobs),
            "oldest_age_seconds": int(oldest_age),
            "stuck_jobs": stuck,
            "server_time": now.isoformat(),
        }

    def clear_jobs(self) -> None:
        """Clear all jobs (testing only)."""
        self._jobs.clear()


# =============================================================================
# Module-level singleton
# =============================================================================

_client: OutboxClient | None = None


def get_outbox_client(*, reload: bool = False) -> OutboxClient:
    """Get the cached OutboxClient singleton."""
    global _client
    if _client is None or reload:
        _client = OutboxClient()
    return _client
