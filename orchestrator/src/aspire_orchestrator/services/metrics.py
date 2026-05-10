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

# LLM request counter — endpoint/model/outcome visibility
LLM_REQUEST_COUNTER = Counter(
    "llm_request_total",
    "Total LLM requests by endpoint, resolved model, and outcome",
    ["endpoint", "resolved_model", "outcome"],
)

# LLM fallback counter — tracks model fallback transitions by profile
LLM_MODEL_FALLBACK_COUNTER = Counter(
    "llm_model_fallback_total",
    "Total LLM model fallbacks by profile and model transition",
    ["profile", "from_model", "to_model"],
)

RESPONSE_QUALITY_COUNTER = Counter(
    "aspire_response_quality_total",
    "Total response quality evaluations by agent, channel, and pass status",
    ["agent_id", "channel", "passed"],
)

RESPONSE_QUALITY_SCORE = Histogram(
    "aspire_response_quality_score",
    "Distribution of response quality scores",
    ["agent_id", "channel"],
    buckets=(0, 25, 50, 60, 70, 80, 90, 95, 100),
)

RETRIEVAL_ROUTER_COUNTER = Counter(
    "aspire_retrieval_router_total",
    "Total retrieval router executions by agent and status",
    ["agent_id", "status", "cache_hit"],
)

RETRIEVAL_GROUNDING_SCORE = Histogram(
    "aspire_retrieval_grounding_score",
    "Distribution of retrieval grounding scores by agent",
    ["agent_id", "status"],
    buckets=(0.0, 0.2, 0.4, 0.55, 0.7, 0.85, 1.0),
)

# =============================================================================
# Pass 18+ Lane 2 — Telephony / SMS / Personalization instruments
# =============================================================================

TELEPHONY_PURCHASE_COUNTER = Counter(
    "aspire_telephony_purchase_total",
    "Total Twilio phone-number purchase attempts by outcome",
    ["outcome"],  # success | failed | timeout | circuit_open | idempotent_replay
)

TELEPHONY_RELEASE_COUNTER = Counter(
    "aspire_telephony_release_total",
    "Total Twilio phone-number release attempts by outcome",
    ["outcome"],
)

SMS_SEND_COUNTER = Counter(
    "aspire_sms_send_total",
    "Total outbound SMS attempts by outcome",
    ["outcome"],  # success | failed | timeout | circuit_open
)

SMS_OUTBOUND_LATENCY = Histogram(
    "aspire_sms_outbound_latency_seconds",
    "Outbound SMS send latency (request -> Twilio response)",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0),
)

PERSONALIZATION_LATENCY = Histogram(
    "aspire_personalization_latency_seconds",
    "Sarah personalization webhook end-to-end latency",
    buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 0.65, 0.8, 1.0, 2.0),
)

PERSONALIZATION_CACHE_FALLBACK_COUNTER = Counter(
    "aspire_personalization_cache_fallback_total",
    "Sarah personalization fell back to cached config by reason",
    ["reason"],  # timeout | db_error | circuit_open
)

# =============================================================================
# Pass 4 — Trade-aware personalization hardening metrics
# =============================================================================

PERSONALIZATION_REQUESTS_TOTAL = Counter(
    "aspire_personalization_requests_total",
    "Total personalization webhook calls by agent and outcome",
    ["agent_id", "outcome"],
    # outcomes: hit | miss | timeout | cache_fallback | degraded
)

PERSONALIZATION_LATENCY_BY_OUTCOME = Histogram(
    "aspire_personalization_latency_seconds_v2",
    "Personalization webhook latency labelled by agent and outcome",
    ["agent_id", "outcome"],
    buckets=(0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.5, 3.0),
)

PERSONALIZATION_BLANK_BUSINESS_NAME_TOTAL = Counter(
    "aspire_personalization_blank_business_name_total",
    "DB row had NULL or empty business_name — filled with safe default",
    ["suite_id"],
    # Ops alert source: sustained hits on a suite_id = onboarding incomplete
)

PERSONALIZATION_CACHE_SIZE_BYTES = None  # Gauge placeholder — initialized lazily below
try:
    from prometheus_client import Gauge as _Gauge

    PERSONALIZATION_CACHE_SIZE_BYTES = _Gauge(
        "aspire_personalization_cache_size_bytes",
        "Approximate byte size of personalization payload stored in Redis cache",
    )
except Exception:  # pragma: no cover
    pass

INGESTION_COUNTER = Counter(
    "aspire_ingestion_total",
    "Inbound provider ingestion attempts by provider and outcome",
    ["provider", "outcome"],  # provider in {twilio_sms, twilio_voice, elevenlabs, ...}
)

# =============================================================================
# W1 — Receipt pipeline hardening metrics (INC-2026-05-07-001)
# =============================================================================

RECEIPT_FLUSH_ATTEMPTS = Counter(
    "aspire_receipt_flush_attempts_total",
    "Total receipt flush batch executions",
)

RECEIPT_FLUSH_FAILURES = Counter(
    "aspire_receipt_flush_failures_total",
    "Total receipt flush failures by error code",
    ["code"],  # 23505 | retry | loop_error | queue_saturated | dead_letter
)

try:
    from prometheus_client import Gauge as _Gauge2  # avoid re-import collision

    RECEIPT_QUEUE_DEPTH = _Gauge2(
        "aspire_receipt_queue_depth",
        "Current number of receipts buffered in-memory awaiting Supabase persistence",
    )
except Exception:  # pragma: no cover
    RECEIPT_QUEUE_DEPTH = None  # type: ignore[assignment]

RECEIPT_DUPLICATE_SKIPPED = Counter(
    "aspire_receipt_duplicate_skipped_total",
    "Total receipt rows skipped due to ON CONFLICT DO NOTHING (idempotent success)",
)

RECEIPT_DEAD_LETTERED = Counter(
    "aspire_receipt_dead_lettered_total",
    "Total receipt rows written to dead-letter store after exhausting flush retries",
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
    llm_request_counter = LLM_REQUEST_COUNTER
    llm_model_fallback_counter = LLM_MODEL_FALLBACK_COUNTER
    response_quality_counter = RESPONSE_QUALITY_COUNTER
    response_quality_score = RESPONSE_QUALITY_SCORE
    retrieval_router_counter = RETRIEVAL_ROUTER_COUNTER
    retrieval_grounding_score = RETRIEVAL_GROUNDING_SCORE
    telephony_purchase_counter = TELEPHONY_PURCHASE_COUNTER
    telephony_release_counter = TELEPHONY_RELEASE_COUNTER
    sms_send_counter = SMS_SEND_COUNTER
    sms_outbound_latency = SMS_OUTBOUND_LATENCY
    personalization_latency = PERSONALIZATION_LATENCY
    personalization_cache_fallback_counter = PERSONALIZATION_CACHE_FALLBACK_COUNTER
    ingestion_counter = INGESTION_COUNTER
    # Pass 4 — trade-aware personalization hardening
    personalization_requests_total = PERSONALIZATION_REQUESTS_TOTAL
    personalization_latency_by_outcome = PERSONALIZATION_LATENCY_BY_OUTCOME
    personalization_blank_business_name_total = PERSONALIZATION_BLANK_BUSINESS_NAME_TOTAL
    personalization_cache_size_bytes = PERSONALIZATION_CACHE_SIZE_BYTES
    # W1 — receipt pipeline hardening (INC-2026-05-07-001)
    receipt_flush_attempts = RECEIPT_FLUSH_ATTEMPTS
    receipt_flush_failures = RECEIPT_FLUSH_FAILURES
    receipt_queue_depth = RECEIPT_QUEUE_DEPTH
    receipt_duplicate_skipped = RECEIPT_DUPLICATE_SKIPPED
    receipt_dead_lettered = RECEIPT_DEAD_LETTERED

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

    def record_llm_request(
        self,
        *,
        endpoint: str,
        resolved_model: str,
        outcome: str,
        latency_ms: float | None = None,
    ) -> None:
        """Record LLM request outcome."""
        self.llm_request_counter.labels(
            endpoint=endpoint,
            resolved_model=resolved_model,
            outcome=outcome,
        ).inc()

    def record_llm_model_fallback(
        self,
        *,
        profile: str,
        from_model: str,
        to_model: str,
    ) -> None:
        """Record model fallback transition."""
        self.llm_model_fallback_counter.labels(
            profile=profile,
            from_model=from_model,
            to_model=to_model,
        ).inc()

    def record_response_quality(
        self,
        *,
        agent_id: str,
        channel: str,
        score: int,
        passed: bool,
    ) -> None:
        self.response_quality_counter.labels(
            agent_id=agent_id or "unknown",
            channel=channel or "unknown",
            passed=str(bool(passed)).lower(),
        ).inc()
        self.response_quality_score.labels(
            agent_id=agent_id or "unknown",
            channel=channel or "unknown",
        ).observe(max(0, min(100, int(score))))

    def record_retrieval_router(
        self,
        *,
        agent_id: str,
        status: str,
        cache_hit: bool,
        grounding_score: float,
    ) -> None:
        self.retrieval_router_counter.labels(
            agent_id=agent_id or "unknown",
            status=status or "unknown",
            cache_hit=str(bool(cache_hit)).lower(),
        ).inc()
        self.retrieval_grounding_score.labels(
            agent_id=agent_id or "unknown",
            status=status or "unknown",
        ).observe(max(0.0, min(1.0, float(grounding_score))))


# Module-level singleton
METRICS = MetricsCollector()
