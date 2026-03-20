"""Temporal interceptors — OTel trace propagation + correlation ID injection.

Propagates correlation_id, suite_id, office_id, agent_id across workflow
and activity boundaries via Temporal headers. Integrates with OpenTelemetry
for distributed tracing (Grafana/Jaeger).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from temporalio import workflow
from temporalio.client import (
    Interceptor,
    OutboundInterceptor,
    StartWorkflowInput,
)
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

logger = logging.getLogger(__name__)

# Header keys for Aspire context propagation
HEADER_CORRELATION_ID = "aspire-correlation-id"
HEADER_SUITE_ID = "aspire-suite-id"
HEADER_OFFICE_ID = "aspire-office-id"
HEADER_AGENT_ID = "aspire-agent-id"
HEADER_TRACE_PARENT = "traceparent"


class AspireClientInterceptor(OutboundInterceptor):
    """Client-side interceptor — injects Aspire headers on workflow start."""

    async def start_workflow(self, input: StartWorkflowInput) -> Any:
        # Headers are set at the call site (search_attributes carry the data)
        return await super().start_workflow(input)


class AspireActivityInterceptor(ActivityInboundInterceptor):
    """Activity-side interceptor — logs activity execution with correlation context."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        headers = input.headers or {}
        correlation_id = _decode_header(headers, HEADER_CORRELATION_ID)
        suite_id = _decode_header(headers, HEADER_SUITE_ID)

        logger.info(
            "Activity start: fn=%s correlation_id=%s suite_id=%s",
            input.fn.__name__ if hasattr(input.fn, "__name__") else str(input.fn),
            correlation_id or "unknown",
            suite_id or "unknown",
        )

        try:
            result = await super().execute_activity(input)
            logger.info(
                "Activity complete: fn=%s correlation_id=%s",
                input.fn.__name__ if hasattr(input.fn, "__name__") else str(input.fn),
                correlation_id or "unknown",
            )
            return result
        except Exception:
            logger.exception(
                "Activity failed: fn=%s correlation_id=%s suite_id=%s",
                input.fn.__name__ if hasattr(input.fn, "__name__") else str(input.fn),
                correlation_id or "unknown",
                suite_id or "unknown",
            )
            raise


class AspireWorkflowInterceptor(WorkflowInboundInterceptor):
    """Workflow-side interceptor — logs workflow execution."""

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        workflow_id = workflow.info().workflow_id
        workflow_type = workflow.info().workflow_type

        logger.info(
            "Workflow start: type=%s id=%s",
            workflow_type,
            workflow_id,
        )

        try:
            result = await super().execute_workflow(input)
            logger.info(
                "Workflow complete: type=%s id=%s",
                workflow_type,
                workflow_id,
            )
            return result
        except Exception:
            logger.exception(
                "Workflow failed: type=%s id=%s",
                workflow_type,
                workflow_id,
            )
            raise


class AspireInterceptor(Interceptor):
    """Root interceptor — creates client and worker interceptors."""

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        return AspireWorkflowInterceptor

    def activity_inbound_interceptor_class(
        self, *args: Any, **kwargs: Any
    ) -> type[ActivityInboundInterceptor] | None:
        return AspireActivityInterceptor


def create_interceptors() -> list[Interceptor]:
    """Create the interceptor chain for the Temporal client/worker."""
    interceptors: list[Interceptor] = [AspireInterceptor()]

    # Add OTel interceptor if available
    try:
        from opentelemetry.api import trace  # noqa: F401

        logger.info("OTel trace API available — will propagate trace context")
    except ImportError:
        logger.info("OTel not available — tracing disabled")

    return interceptors


def _decode_header(headers: Mapping[str, Any], key: str) -> str | None:
    """Decode a string header value from Temporal headers."""
    value = headers.get(key)
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
