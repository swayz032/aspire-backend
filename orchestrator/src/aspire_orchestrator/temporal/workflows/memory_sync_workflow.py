"""Memory sync workflow — durable orchestration for inbox → memory_objects.

V1 is single-step: invoke `memory_refinery_activity` with retries. Future
passes will extend to multi-step (ingest → resolve → refine → write →
candidate → materialize → receipt) once the basic refinement loop is stable.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from temporalio import workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)


_REFINERY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=4,
    non_retryable_error_types=[
        # Refinery raises MemoryServiceError with structured codes; auth/policy
        # codes should not retry. Temporal matches by exception class name.
        "AuthError",
        "PolicyDeniedError",
        "TenantMismatchError",
        "SafetyBlockedError",
    ],
)


@workflow.defn
class MemorySyncWorkflow:
    """Drives a single memory_event_inbox row to terminal state.

    Caller is the inbox poller (a future cron / pg_listen worker). Workflow
    is keyed by `event_id` so duplicate triggers are deterministically
    no-ops at the Temporal level.
    """

    @workflow.run
    async def run(self, event_id: str) -> dict:
        """Run the refinery for `event_id`. Returns the RefineResult dict."""
        # Validate input shape inside the workflow (workflows should not raise
        # on transient infra issues but should reject obviously bad inputs).
        try:
            UUID(event_id)
        except ValueError:
            workflow.logger.error(
                "MemorySyncWorkflow.run: invalid event_id=%s", event_id
            )
            raise

        result = await workflow.execute_activity(
            "memory_refinery_activity",
            event_id,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_REFINERY_RETRY,
        )
        return result
