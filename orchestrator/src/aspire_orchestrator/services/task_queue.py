"""
Lightweight async task queue for non-streaming intent processing.
Uses asyncio tasks with a bounded semaphore instead of ARQ to avoid
adding a separate worker process dependency.

For Phase 4B of the scaling plan. Can be upgraded to ARQ/Celery later
if horizontal scaling requires separate worker processes.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class TaskQueueFullError(RuntimeError):
    """Raised when the bounded queue rejects new work."""


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskResult:
    task_id: str
    status: TaskStatus
    created_at: datetime
    completed_at: datetime | None = None
    result: Any = None
    error: str | None = None


class AsyncTaskQueue:
    """Bounded async task queue with result storage."""

    def __init__(self, max_concurrent: int = 20, max_results: int = 1000, max_pending: int = 500):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._results: dict[str, TaskResult] = {}
        self._max_results = max_results
        self._max_pending = max_pending
        self._running = False

    def start(self) -> None:
        self._running = True
        logger.info(
            "AsyncTaskQueue started (max_concurrent=%d max_pending=%d)",
            self._semaphore._value,
            self._max_pending,
        )

    def _pending_count(self) -> int:
        return sum(
            1 for result in self._results.values()
            if result.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
        )

    async def enqueue(self, coro_func: Any, *args: Any, **kwargs: Any) -> str:
        """Enqueue an async callable. Returns task_id immediately."""
        if not self._running:
            raise RuntimeError("TaskQueue not started")
        if self._pending_count() >= self._max_pending:
            raise TaskQueueFullError("TaskQueue is at capacity")

        task_id = str(uuid.uuid4())
        self._results[task_id] = TaskResult(
            task_id=task_id,
            status=TaskStatus.QUEUED,
            created_at=datetime.now(timezone.utc),
        )
        # Prune old results if over limit
        if len(self._results) > self._max_results:
            self._prune_results()

        asyncio.create_task(self._execute(task_id, coro_func, *args, **kwargs))
        return task_id

    async def _execute(self, task_id: str, coro_func: Any, *args: Any, **kwargs: Any) -> None:
        async with self._semaphore:
            task_result = self._results.get(task_id)
            if not task_result:
                return
            task_result.status = TaskStatus.RUNNING
            try:
                result = await coro_func(*args, **kwargs)
                task_result.status = TaskStatus.COMPLETED
                task_result.result = result
                task_result.completed_at = datetime.now(timezone.utc)
            except Exception as e:
                task_result.status = TaskStatus.FAILED
                task_result.error = str(e)
                task_result.completed_at = datetime.now(timezone.utc)
                logger.error("Task %s failed: %s", task_id, e, exc_info=True)

    def get_status(self, task_id: str) -> TaskResult | None:
        return self._results.get(task_id)

    def _prune_results(self) -> None:
        """Remove oldest completed/failed results."""
        completed = [
            (tid, r) for tid, r in self._results.items()
            if r.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        ]
        completed.sort(key=lambda x: x[1].created_at)
        to_remove = len(self._results) - self._max_results
        for tid, _ in completed[:to_remove]:
            del self._results[tid]

    async def shutdown(self) -> None:
        self._running = False
        logger.info("AsyncTaskQueue shutdown")


# Module-level singleton
_task_queue: AsyncTaskQueue | None = None


def get_task_queue() -> AsyncTaskQueue:
    global _task_queue
    if _task_queue is None:
        from aspire_orchestrator.config.settings import settings

        _task_queue = AsyncTaskQueue(
            max_concurrent=max(1, int(settings.task_queue_max_concurrent)),
            max_pending=max(10, int(settings.task_queue_max_pending)),
        )
    return _task_queue


def start_task_queue() -> None:
    get_task_queue().start()


async def stop_task_queue() -> None:
    global _task_queue
    if _task_queue:
        await _task_queue.shutdown()
        _task_queue = None
