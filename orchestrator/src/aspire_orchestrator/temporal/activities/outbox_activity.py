"""Outbox activities — claim, complete, fail outbox jobs.

Wraps existing OutboxClient operations for Temporal execution.
Used by OutboxExecutionWorkflow for RED-tier job lifecycle.
"""

from __future__ import annotations

import logging

from temporalio import activity

from aspire_orchestrator.temporal.models import (
    ClaimJobInput,
    CompleteJobInput,
    FailJobInput,
)

logger = logging.getLogger(__name__)


@activity.defn
async def claim_outbox_job(input: ClaimJobInput) -> bool:
    """Claim an outbox job for execution (idempotent)."""
    from aspire_orchestrator.services.outbox_client import OutboxClient

    client = OutboxClient()
    try:
        result = await client.claim_job(
            job_id=input.job_id,
            suite_id=input.suite_id,
            worker_id=input.worker_id,
        )
        logger.info("Claimed outbox job: job_id=%s suite_id=%s", input.job_id, input.suite_id)
        return result
    except Exception:
        logger.exception("Failed to claim outbox job: job_id=%s", input.job_id)
        raise


@activity.defn
async def complete_outbox_job(input: CompleteJobInput) -> None:
    """Mark an outbox job as completed with result."""
    from aspire_orchestrator.services.outbox_client import OutboxClient

    client = OutboxClient()
    try:
        await client.complete_job(
            job_id=input.job_id,
            suite_id=input.suite_id,
            result=input.result,
        )
        logger.info("Completed outbox job: job_id=%s", input.job_id)
    except Exception:
        logger.exception("Failed to complete outbox job: job_id=%s", input.job_id)
        raise


@activity.defn
async def fail_outbox_job(input: FailJobInput) -> None:
    """Mark an outbox job as failed."""
    from aspire_orchestrator.services.outbox_client import OutboxClient

    client = OutboxClient()
    try:
        await client.fail_job(
            job_id=input.job_id,
            suite_id=input.suite_id,
            error=input.error,
        )
        logger.info("Failed outbox job: job_id=%s error=%s", input.job_id, input.error[:100])
    except Exception:
        logger.exception("Failed to record outbox failure: job_id=%s", input.job_id)
        raise
