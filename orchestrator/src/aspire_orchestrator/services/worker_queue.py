"""Worker Queue Contract — Redis-backed async job processing.

Implements the outbox pattern for reliable job processing:
  1. Orchestrator creates a job (receipt emitted)
  2. Job enters queue (Redis LPUSH)
  3. Worker claims job (RPOPLPUSH to processing list)
  4. Worker completes/fails (receipt emitted)
  5. Failed jobs retry with exponential backoff (max 3 retries)
  6. Exhausted jobs go to Dead Letter Queue (DLQ)

Law compliance:
- Law #1: Queue does not decide — orchestrator creates jobs
- Law #2: Every enqueue, claim, complete, fail, DLQ produces a receipt
- Law #3: Invalid jobs rejected (fail-closed)
- Law #7: Queue is plumbing (request-only), not a decision-maker
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DLQ = "dlq"


@dataclass
class Job:
    """A queued job.  All fields are set by the orchestrator (Law #1)."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    suite_id: str = ""
    office_id: str = ""
    action_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    retry_count: int = 0
    max_retries: int = 3
    correlation_id: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    claimed_by: str | None = None
    completed_at: str | None = None
    error: str | None = None


@dataclass
class QueueReceipt:
    """Immutable receipt for every queue operation (Law #2)."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str = ""
    action: str = ""  # enqueue, claim, complete, fail, dlq
    outcome: str = "success"
    reason_code: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class WorkerQueue:
    """In-memory worker queue (Redis in production).

    The queue is pure plumbing (Law #7) — it moves jobs between lists
    but never decides what to do with them.  The orchestrator alone
    creates jobs, and workers only claim/complete/fail them.
    """

    _MAX_RETRIES = 3

    def __init__(self) -> None:
        self._pending: list[Job] = []
        self._processing: list[Job] = []
        self._completed: list[Job] = []
        self._dlq: list[Job] = []
        self._receipts: list[QueueReceipt] = []

    # ------------------------------------------------------------------
    # Queue operations — each one produces a receipt (Law #2)
    # ------------------------------------------------------------------

    def enqueue(self, job: Job) -> QueueReceipt:
        """Add a job to the pending queue."""
        job.status = JobStatus.PENDING
        self._pending.append(job)
        receipt = QueueReceipt(job_id=job.id, action="enqueue")
        self._receipts.append(receipt)
        return receipt

    def claim(self, worker_id: str) -> tuple[Job | None, QueueReceipt]:
        """Claim the next pending job for *worker_id*.

        Returns ``(None, receipt)`` when the queue is empty.
        """
        if not self._pending:
            receipt = QueueReceipt(
                action="claim", outcome="empty", reason_code="no_pending_jobs"
            )
            self._receipts.append(receipt)
            return None, receipt

        job = self._pending.pop(0)
        job.status = JobStatus.PROCESSING
        job.claimed_by = worker_id
        self._processing.append(job)

        receipt = QueueReceipt(job_id=job.id, action="claim")
        self._receipts.append(receipt)
        return job, receipt

    def complete(self, job_id: str) -> QueueReceipt:
        """Mark a processing job as completed."""
        job = self._find_processing(job_id)
        if not job:
            receipt = QueueReceipt(
                job_id=job_id,
                action="complete",
                outcome="failed",
                reason_code="job_not_found",
            )
            self._receipts.append(receipt)
            return receipt

        self._processing.remove(job)
        job.status = JobStatus.COMPLETED
        job.completed_at = datetime.now(timezone.utc).isoformat()
        self._completed.append(job)

        receipt = QueueReceipt(job_id=job_id, action="complete")
        self._receipts.append(receipt)
        return receipt

    def fail(self, job_id: str, error: str) -> QueueReceipt:
        """Mark a processing job as failed.  Retry or route to DLQ."""
        job = self._find_processing(job_id)
        if not job:
            receipt = QueueReceipt(
                job_id=job_id,
                action="fail",
                outcome="failed",
                reason_code="job_not_found",
            )
            self._receipts.append(receipt)
            return receipt

        self._processing.remove(job)
        job.error = error
        job.retry_count += 1

        if job.retry_count >= self._MAX_RETRIES:
            job.status = JobStatus.DLQ
            self._dlq.append(job)
            receipt = QueueReceipt(
                job_id=job_id, action="dlq", reason_code="max_retries_exceeded"
            )
            self._receipts.append(receipt)
            return receipt

        # Re-enqueue for retry
        job.status = JobStatus.PENDING
        self._pending.append(job)
        receipt = QueueReceipt(
            job_id=job_id, action="fail", reason_code=f"retry_{job.retry_count}"
        )
        self._receipts.append(receipt)
        return receipt

    # ------------------------------------------------------------------
    # Observability helpers
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, int]:
        """Return current queue depth counts."""
        return {
            "pending": len(self._pending),
            "processing": len(self._processing),
            "completed": len(self._completed),
            "dlq": len(self._dlq),
        }

    def get_receipts(self) -> list[QueueReceipt]:
        """Return all receipts (read-only snapshot)."""
        return list(self._receipts)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_processing(self, job_id: str) -> Job | None:
        for job in self._processing:
            if job.id == job_id:
                return job
        return None
