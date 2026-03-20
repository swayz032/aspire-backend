"""Temporal worker entrypoint — 4-queue priority-based topology (Enhancement #12).

Registers all workflows and activities per queue.
Designed to run as a separate process alongside the FastAPI orchestrator.

Usage:
    python -m aspire_orchestrator.temporal.worker --queue ava-intent-high
    python -m aspire_orchestrator.temporal.worker --queue ava-background
    python -m aspire_orchestrator.temporal.worker --queue ava-callbacks
    python -m aspire_orchestrator.temporal.worker --queue ava-scheduled
    python -m aspire_orchestrator.temporal.worker --all  # all queues (dev only)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from temporalio.worker import Worker

from aspire_orchestrator.temporal.activities import (
    claim_outbox_job,
    complete_outbox_job,
    emit_client_event,
    execute_provider_call,
    execute_webhook_provider_call,
    fail_outbox_job,
    persist_receipts,
    run_langgraph_turn,
    sync_workflow_execution,
)
from aspire_orchestrator.temporal.client import get_temporal_client
from aspire_orchestrator.temporal.config import (
    ALL_QUEUES,
    QUEUE_BACKGROUND,
    QUEUE_CALLBACKS,
    QUEUE_INTENT_HIGH,
    QUEUE_SCHEDULED,
)
from aspire_orchestrator.temporal.interceptors import AspireInterceptor
from aspire_orchestrator.temporal.workflows import (
    AgentFanOutWorkflow,
    ApprovalWorkflow,
    AvaIntentWorkflow,
    OutboxExecutionWorkflow,
    ProviderCallbackWorkflow,
    SpecialistAgentWorkflow,
)

logger = logging.getLogger(__name__)

# Queue → (workflows, activities) mapping
QUEUE_REGISTRY: dict[str, tuple[list[type], list]] = {
    QUEUE_INTENT_HIGH: (
        [AvaIntentWorkflow],
        [run_langgraph_turn, persist_receipts, sync_workflow_execution, emit_client_event],
    ),
    QUEUE_BACKGROUND: (
        [AgentFanOutWorkflow, SpecialistAgentWorkflow, OutboxExecutionWorkflow],
        [
            run_langgraph_turn, persist_receipts, sync_workflow_execution,
            emit_client_event, claim_outbox_job, complete_outbox_job,
            fail_outbox_job, execute_provider_call,
        ],
    ),
    QUEUE_CALLBACKS: (
        [ProviderCallbackWorkflow, ApprovalWorkflow],
        [
            persist_receipts, sync_workflow_execution, emit_client_event,
            execute_webhook_provider_call, execute_provider_call,
        ],
    ),
    QUEUE_SCHEDULED: (
        [ApprovalWorkflow],
        [persist_receipts, sync_workflow_execution, emit_client_event],
    ),
}


async def run_worker(queue: str) -> None:
    """Run a single worker for the specified queue."""
    client = await get_temporal_client()

    workflows, activities = QUEUE_REGISTRY.get(queue, ([], []))
    if not workflows and not activities:
        logger.error("Unknown queue: %s. Valid queues: %s", queue, ALL_QUEUES)
        sys.exit(1)

    logger.info(
        "Starting worker: queue=%s workflows=%d activities=%d",
        queue,
        len(workflows),
        len(activities),
    )

    worker = Worker(
        client,
        task_queue=queue,
        workflows=workflows,
        activities=activities,
        interceptors=[AspireInterceptor()],
    )

    await worker.run()


async def run_all_workers() -> None:
    """Run workers for all queues (dev only)."""
    client = await get_temporal_client()

    workers = []
    for queue in ALL_QUEUES:
        workflows, activities = QUEUE_REGISTRY.get(queue, ([], []))
        worker = Worker(
            client,
            task_queue=queue,
            workflows=workflows,
            activities=activities,
            interceptors=[AspireInterceptor()],
        )
        workers.append(worker)

    logger.info("Starting all %d workers (dev mode)", len(workers))

    # Run all workers concurrently
    await asyncio.gather(*[w.run() for w in workers])


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Aspire Temporal Worker")
    parser.add_argument(
        "--queue",
        choices=ALL_QUEUES,
        help="Task queue to poll",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run workers for all queues (dev only)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.all:
        asyncio.run(run_all_workers())
    elif args.queue:
        asyncio.run(run_worker(args.queue))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
