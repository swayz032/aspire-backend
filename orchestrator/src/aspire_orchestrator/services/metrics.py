"""Prometheus Metrics Service — Gate 2 Observability.

Exposes metrics matching SLI definitions in infrastructure/observability/SLI_SLO.md:
- aspire_orchestrator_requests_total (Counter)
- aspire_orchestrator_request_duration_seconds (Histogram)
- aspire_tool_execution_total (Counter)
- aspire_receipt_write_total (Counter)
- aspire_token_mint_total (Counter)
- aspire_a2a_tasks_total (Counter)

Usage:
    from aspire_orchestrator.services.metrics import METRICS
    METRICS.request_counter.labels(status="success", risk_tier="green", task_type="email.send").inc()
"""

from __future__ import annotations

import logging

from prometheus_client import Counter, Histogram, Info

logger = logging.getLogger(__name__)


# =============================================================================
# Metric Definitions (matching SLI_SLO.md)
# =============================================================================

# Orchestrator request counter — tracks overall request outcomes
REQUEST_COUNTER = Counter(
    "aspire_orchestrator_requests_total",
    "Total orchestrator requests by outcome",
    ["status", "risk_tier", "task_type"],
)

# Orchestrator request duration — tracks latency per node
REQUEST_DURATION = Histogram(
    "aspire_orchestrator_request_duration_seconds",
    "Request duration in seconds per pipeline node",
    ["node"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# Tool execution counter — tracks tool call outcomes
TOOL_EXECUTION_COUNTER = Counter(
    "aspire_tool_execution_total",
    "Total tool executions by outcome",
    ["tool", "status", "live"],
)

# Receipt write counter — tracks receipt persistence
RECEIPT_WRITE_COUNTER = Counter(
    "aspire_receipt_write_total",
    "Total receipt writes by type and outcome",
    ["receipt_type", "status"],
)

# Token mint counter — tracks capability token minting
TOKEN_MINT_COUNTER = Counter(
    "aspire_token_mint_total",
    "Total token mint operations by outcome",
    ["status"],
)

# A2A task counter — tracks agent-to-agent task lifecycle
A2A_TASK_COUNTER = Counter(
    "aspire_a2a_tasks_total",
    "Total A2A task operations by action and outcome",
    ["action", "status"],
)

# Service info — static labels for service identification
SERVICE_INFO = Info(
    "aspire_orchestrator",
    "Aspire orchestrator service information",
)
SERVICE_INFO.info({
    "version": "0.1.0",
    "service": "aspire-orchestrator",
    "law1": "single_brain",
})


class MetricsCollector:
    """Convenience wrapper for all Prometheus metrics.

    Provides a single import point and helper methods for common patterns.
    All counter increments are non-blocking.
    """

    request_counter = REQUEST_COUNTER
    request_duration = REQUEST_DURATION
    tool_execution_counter = TOOL_EXECUTION_COUNTER
    receipt_write_counter = RECEIPT_WRITE_COUNTER
    token_mint_counter = TOKEN_MINT_COUNTER
    a2a_task_counter = A2A_TASK_COUNTER

    def record_request(
        self,
        *,
        status: str,
        risk_tier: str = "unknown",
        task_type: str = "unknown",
    ) -> None:
        """Record a completed orchestrator request."""
        self.request_counter.labels(
            status=status,
            risk_tier=risk_tier,
            task_type=task_type,
        ).inc()

    def record_tool_execution(
        self,
        *,
        tool: str,
        status: str,
        live: bool = True,
    ) -> None:
        """Record a tool execution outcome."""
        self.tool_execution_counter.labels(
            tool=tool,
            status=status,
            live=str(live).lower(),
        ).inc()

    def record_receipt_write(
        self,
        *,
        receipt_type: str,
        status: str = "success",
    ) -> None:
        """Record a receipt write operation."""
        self.receipt_write_counter.labels(
            receipt_type=receipt_type,
            status=status,
        ).inc()

    def record_token_mint(self, *, status: str) -> None:
        """Record a token mint operation."""
        self.token_mint_counter.labels(status=status).inc()

    def record_a2a_task(self, *, action: str, status: str) -> None:
        """Record an A2A task operation."""
        self.a2a_task_counter.labels(action=action, status=status).inc()


# Module-level singleton
METRICS = MetricsCollector()
