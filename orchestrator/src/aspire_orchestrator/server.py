"""Aspire Orchestrator FastAPI Server — Wave 8 Complete.

Endpoints:
  POST /v1/intents — Primary: process AvaOrchestratorRequest through LangGraph pipeline
  POST /v1/intents/classify — Brain Layer: classify utterance + return routing plan
  GET  /v1/receipts — Query receipts (RLS-scoped by suite_id from auth headers)
  POST /v1/receipts/verify-run — Verify receipt hash chain for a correlation_id
  POST /v1/policy/evaluate — Evaluate policy for an action_type (read-only)
  GET  /v1/registry/capabilities — Capability discovery (skill packs, tools, providers)
  GET  /v1/registry/skill-packs/:id — Get specific skill pack manifest
  POST /v1/a2a/dispatch — Dispatch a task to a skill pack agent
  POST /v1/a2a/claim — Claim available tasks for an agent
  POST /v1/a2a/complete — Mark a task as completed
  POST /v1/a2a/fail — Mark a task as failed (with retry/quarantine)
  GET  /v1/a2a/tasks — List tasks for a suite

Admin Ops Telemetry Facade (Wave 8):
  GET  /admin/ops/health — Admin health check (no auth)
  GET  /admin/ops/incidents — List incidents (filtered, paginated)
  POST /admin/ops/incidents/report — Internal incident ingest for connected surfaces
  GET  /admin/ops/incidents/:id — Get incident detail + timeline + evidence_pack
  GET  /admin/ops/receipts — List receipts (admin cross-suite, PII-redacted)
  GET  /admin/ops/provider-calls — List provider calls (redacted)
  GET  /admin/ops/outbox — Outbox queue status
  GET  /admin/ops/rollouts — List rollouts
  GET  /admin/proposals/pending — List pending change proposals
  POST /admin/proposals/:id/approve — Approve a change proposal

Robot Infrastructure (Wave 3):
  POST /robots/ingest — Accept RobotRun results from CI/CD (S2S HMAC auth)

Health & Observability:
  GET /healthz — Liveness probe
  GET /livez — Kubernetes-style liveness probe
  GET /readyz — Readiness probe (dependency checks)
  GET /metrics — Prometheus metrics (Gate 2)

Auth: suite_id/office_id/actor_id come from Gateway via X- headers.
The Gateway derives these from JWT; the orchestrator trusts the Gateway.
Admin endpoints use X-Admin-Token header (JWT in production, dev token in dev).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Load .env BEFORE any other imports that read os.environ
# Pydantic BaseSettings reads .env for its own fields, but os.environ.get() calls
# in intent_classifier, param_extract, respond etc. need dotenv to inject values.
from dotenv import load_dotenv


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')

_ASPIRE_ENV = (os.getenv("ASPIRE_ENV") or "").strip().lower()
_ALLOW_LOCAL_DOTENV = _is_truthy(os.getenv("ASPIRE_ENABLE_LOCAL_DOTENV"))
if _ALLOW_LOCAL_DOTENV and _ASPIRE_ENV not in {"prod", "production"}:
    load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)

from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from aspire_orchestrator.config.secrets import load_secrets
from aspire_orchestrator.middleware.exception_handler import GlobalExceptionMiddleware
from aspire_orchestrator.middleware.correlation import CorrelationIdMiddleware
from aspire_orchestrator.middleware.rate_limiter import RateLimitMiddleware
from aspire_orchestrator.middleware.chaos import maybe_add_chaos
from aspire_orchestrator.middleware.sentry_middleware import init_sentry

# Initialize Sentry early — before app/middleware setup so it captures startup errors.
# No-op if SENTRY_DSN is not set (Law #9: PII stripped in before_send hook).
init_sentry()
from aspire_orchestrator.graph import (
    close_checkpointer_runtime,
    get_checkpointer_runtime,
    probe_checkpointer_roundtrip,
)
from aspire_orchestrator.services.policy_engine import get_policy_matrix
from aspire_orchestrator.services.receipt_store import query_receipts, get_chain_receipts, store_receipts
from aspire_orchestrator.services.receipt_store import start_receipt_writer, stop_receipt_writer
from aspire_orchestrator.services.receipt_chain import verify_chain
from aspire_orchestrator.services.registry import get_registry
from aspire_orchestrator.services.a2a_service import get_a2a_service, A2ATaskStatus
from aspire_orchestrator.services.outbox_client import get_outbox_client
from aspire_orchestrator.services.metrics import METRICS
from aspire_orchestrator.services.openai_client import (
    get_model_probe_status,
    probe_models_startup,
)
from aspire_orchestrator.routes.intents import router as intents_router
from aspire_orchestrator.routes.admin import router as admin_router
from aspire_orchestrator.routes.robots import router as robots_router
from aspire_orchestrator.routes.webhooks import router as webhooks_router
from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.services.orchestrator_runtime import (
    GraphInvokeUnavailableError,
    invoke_orchestrator_graph,
    resolve_thread_id,
    resume_orchestrator_graph,
    warm_orchestrator_graph,
)

logger = logging.getLogger(__name__)

from aspire_orchestrator.services.supabase_client import close_pools as close_supabase_pools


@asynccontextmanager
async def _app_lifespan(_: FastAPI):
    """Own startup and shutdown resources without deprecated FastAPI hooks."""
    # Configure structured logging FIRST (before any other initialization logs)
    from aspire_orchestrator.config.logging_config import configure_logging
    configure_logging()

    await warm_orchestrator_graph()

    # Warm the LLM connection pool — prevents first-request cold penalty
    try:
        import uuid as _uuid
        warmup_state = {
            "utterance": "warmup",
            "suite_id": "system",
            "office_id": "system",
            "actor_id": "warmup",
            "correlation_id": f"warmup-{_uuid.uuid4()}",
            "channel": "system",
        }
        await invoke_orchestrator_graph(warmup_state, thread_id="system:warmup")
        logger.info("Startup warmup: graph + LLM connection pool ready")
    except Exception as e:
        logger.warning(f"Startup warmup failed (non-blocking): {e}")

    start_receipt_writer()
    from aspire_orchestrator.services.task_queue import start_task_queue, stop_task_queue
    start_task_queue()
    from aspire_orchestrator.services.sre_triage import start_sre_triage, stop_sre_triage
    start_sre_triage()

    try:
        yield
    finally:
        await stop_sre_triage()
        await stop_task_queue()
        await stop_receipt_writer()
        await close_checkpointer_runtime()
        await close_supabase_pools()


app = FastAPI(
    title="Aspire Orchestrator",
    description="LangGraph Orchestrator ??? the Single Brain (Law #1)",
    version="0.1.0",
    lifespan=_app_lifespan,
)

# ── Middleware Stack ──────────────────────────────────────────────────────
# Starlette: last added = outermost. Order matters!
# Request flow: CORS → CorrelationId → RateLimit → GlobalException → ChaosMonkey → Route
# CORS MUST be outermost so preflight responses always have CORS headers.

# ChaosMonkey — controlled failure injection (innermost, only when CHAOS_ENABLED=true)
maybe_add_chaos(app)

# Global Exception Handler — catches unhandled exceptions, creates incident + receipt
app.add_middleware(GlobalExceptionMiddleware)

# Rate Limiting — per-tenant sliding window (B-H7, Enterprise Remediation Wave 4)
# Defaults live in rate_limiter.py (500/60s). Override via ASPIRE_RATE_LIMIT env var.
app.add_middleware(RateLimitMiddleware)

# Correlation ID — extracts/generates X-Correlation-Id, propagates via contextvars
app.add_middleware(CorrelationIdMiddleware)

# CORS — OUTERMOST so every response (including 429, 500, etc.) gets CORS headers
_CORS_DEFAULTS = [
    "http://localhost:3100",
    "http://127.0.0.1:3100",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://admin.aspireos.app",
    "https://www.aspireos.app",
]
_cors_env = os.environ.get("ASPIRE_CORS_ORIGINS", "").strip()
_CORS_ORIGINS = _cors_env.split(",") if _cors_env else _CORS_DEFAULTS
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-Correlation-Id",
        "X-Admin-Token",
        "X-Suite-Id",
        "X-Office-Id",
        "X-Actor-Id",
        "X-Trace-Id",
        # Browser tracing integrations add these during cross-origin fetches.
        "sentry-trace",
        "baggage",
        "traceparent",
        "tracestate",
    ],
)

# Include Brain Layer routes
app.include_router(intents_router)

# Include Admin Ops Telemetry Facade routes (Wave 8)
app.include_router(admin_router)

# Include Robot Ingest routes (Wave 3 — Enterprise Sync)
app.include_router(robots_router)

# Include Webhook routes (Stripe Connect callbacks)
app.include_router(webhooks_router)

# Load secrets from AWS Secrets Manager (production) or .env (dev)
# Must happen BEFORE graph build, which may read provider keys from os.environ
load_secrets()

# Retry Sentry init after secrets load so DSNs sourced from AWS are honored.
init_sentry()

def _verify_environment_parity() -> None:
    """Fail closed on contradictory production environment markers."""
    aspire_env = os.getenv("ASPIRE_ENV", "").strip().lower()
    node_env = os.getenv("NODE_ENV", "").strip().lower()
    aspire_prod = aspire_env == "production"
    node_prod = node_env == "production"
    if aspire_prod != node_prod and (aspire_env or node_env):
        raise SystemExit(
            f"Environment mismatch: ASPIRE_ENV={aspire_env or 'unset'} NODE_ENV={node_env or 'unset'}. "
            "Production requires both to resolve consistently."
        )

_verify_environment_parity()

# Verify critical settings are populated (Policy Gate P0: fail-closed startup)
# Empty provider keys = Ava gives generic "Done" responses (F1 root cause)
from aspire_orchestrator.config.secrets import verify_settings_coverage
_settings_warnings = verify_settings_coverage()
if os.getenv("ASPIRE_ENV") == "production" and _settings_warnings:
    # In production, missing critical settings = crash startup (Law #3)
    for _w in _settings_warnings:
        logger.error("CRITICAL SETTINGS MISSING: %s", _w)
    raise SystemExit(1)
elif _settings_warnings:
    for _w in _settings_warnings:
        logger.warning("Settings gap (dev mode, non-blocking): %s", _w)

# Startup model probe + profile fallback resolution cache
_MODEL_PROBE_BOOT: dict[str, Any] = {}
try:
    asyncio.get_running_loop()
except RuntimeError:
    try:
        _MODEL_PROBE_BOOT = asyncio.run(probe_models_startup())
    except Exception as _probe_err:
        logger.warning("Startup model probe failed: %s", _probe_err)
        _MODEL_PROBE_BOOT = {"status": "failed", "profiles": {}, "models": {}}
else:
    # If an event loop is already active (common in tests and some embedded runtimes),
    # defer probe warming to readiness/runtime to avoid creating an un-awaited coroutine.
    _MODEL_PROBE_BOOT = {"status": "deferred", "profiles": {}, "models": {}}

if os.getenv("ASPIRE_ENV", "").strip().lower() == "production":
    profiles = _MODEL_PROBE_BOOT.get("profiles", {}) if isinstance(_MODEL_PROBE_BOOT, dict) else {}
    if profiles:
        bad_profiles = [
            p for p, details in profiles.items()
            if not bool((details or {}).get("available"))
        ]
        if bad_profiles:
            logger.error("Startup model probe unresolved profiles: %s", ", ".join(bad_profiles))
            raise SystemExit(1)

    # Enterprise hard gates: production cannot run mutable state backends in-memory.
    # Emergency override exists only to restore service while provisioning Redis.
    allow_inmem_rate_limit = (os.getenv("ASPIRE_ALLOW_INMEM_RATE_LIMIT_IN_PROD") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    redis_url = (os.getenv("ASPIRE_REDIS_URL") or os.getenv("REDIS_URL") or "").strip()
    if not redis_url and not allow_inmem_rate_limit:
        logger.error("Production requires ASPIRE_REDIS_URL/REDIS_URL for shared rate limiting.")
        raise SystemExit(1)
    if not redis_url and allow_inmem_rate_limit:
        logger.warning(
            "ASPIRE_ALLOW_INMEM_RATE_LIMIT_IN_PROD enabled: using in-memory rate limiting in production until Redis is configured."
        )

    outbox_backend = get_outbox_client().backend
    if outbox_backend != "supabase":
        logger.error("Production requires durable outbox backend=supabase, found=%s", outbox_backend)
        raise SystemExit(1)

    a2a_backend = get_a2a_service().backend
    if a2a_backend != "supabase":
        logger.error("Production requires durable a2a backend=supabase, found=%s", a2a_backend)
        raise SystemExit(1)


# =============================================================================
# Health Probes
# =============================================================================


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe — always returns ok if process is running."""
    return {"status": "ok", "service": "aspire-orchestrator", "version": "0.1.0"}


@app.get("/livez")
async def livez() -> dict[str, str]:
    """Kubernetes-style liveness probe (alias for /healthz)."""
    return {"status": "ok", "service": "aspire-orchestrator", "version": "0.1.0"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness probe — checks critical dependencies are configured and reachable (B-H10).

    Enhanced to verify downstream dependencies beyond just configuration:
    - Signing key configured
    - Graph built
    - DLP initialized
    - Receipt store reachable (in-memory always passes; Supabase checked if configured)
    - Redis reachable (if configured)
    """
    from aspire_orchestrator.config.settings import settings

    checks: dict[str, bool] = {
        "signing_key_configured": bool(
            (settings.token_signing_key and settings.token_signing_key != "UNCONFIGURED-FAIL-CLOSED")
            or os.environ.get("ASPIRE_TOKEN_SIGNING_KEY")
        ),
        "graph_built": True,
    }

    # Check DLP initialization (must verify Presidio actually loaded, not just object exists)
    try:
        from aspire_orchestrator.services.dlp import get_dlp_service
        dlp = get_dlp_service()
        checks["dlp_initialized"] = dlp.available
    except Exception:
        checks["dlp_initialized"] = False

    # Check receipt store is functional (B-H10 enhancement)
    try:
        from aspire_orchestrator.services.receipt_store import query_receipts
        # Quick read probe — if store is broken this will raise
        query_receipts(suite_id="health_probe", limit=1)
        checks["receipt_store"] = True
    except Exception:
        checks["receipt_store"] = False

    # Check Redis connectivity (if configured) — run sync ping in thread to avoid blocking event loop
    redis_url = os.environ.get("REDIS_URL") or os.environ.get("ASPIRE_REDIS_URL")
    if redis_url:
        try:
            import asyncio
            import redis as _redis_lib

            def _redis_ping() -> bool:
                r = _redis_lib.from_url(redis_url, socket_timeout=2)
                try:
                    return bool(r.ping())
                finally:
                    r.close()

            checks["redis"] = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _redis_ping),
                timeout=3.0,
            )
        except Exception:
            checks["redis"] = False

    # Check policy engine loaded
    try:
        matrix = get_policy_matrix()
        checks["policy_engine"] = matrix is not None and len(getattr(matrix, "actions", {})) > 0
    except Exception:
        checks["policy_engine"] = False

    # Check LangGraph checkpointer mode/runtime
    try:
        cp = get_checkpointer_runtime()
        checks["langgraph_checkpointer"] = bool(cp.get("backend"))
        if cp.get("mode") == "postgres":
            checks["langgraph_checkpoint_store"] = bool(settings.langgraph_postgres_dsn)
        else:
            checks["langgraph_checkpoint_store"] = True
        checks["langgraph_checkpoint_roundtrip"] = await probe_checkpointer_roundtrip()
    except Exception:
        checks["langgraph_checkpointer"] = False
        checks["langgraph_checkpoint_store"] = False
        checks["langgraph_checkpoint_roundtrip"] = False

    # Check model probe cache health
    try:
        probe = get_model_probe_status()
        if not probe.get("models"):
            # Warm probe cache lazily for environments where startup probe was deferred.
            await probe_models_startup()
            probe = get_model_probe_status()
        checks["model_probe_cache"] = bool(probe.get("models"))
        checks["model_probe_healthy"] = bool(probe.get("healthy"))
    except Exception:
        checks["model_probe_cache"] = False
        checks["model_probe_healthy"] = False

    require_model_probe_env = os.environ.get("ASPIRE_REQUIRE_MODEL_PROBE_HEALTH")
    if require_model_probe_env is None:
        env_label = (
            os.environ.get("ASPIRE_ENV")
            or os.environ.get("ENV")
            or os.environ.get("NODE_ENV")
            or ""
        ).strip().lower()
        require_model_probe_health = env_label in {"prod", "production"}
    else:
        require_model_probe_health = require_model_probe_env.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    all_ready = all(checks.values())
    # Determine if partially ready (some non-critical deps down)
    critical_names = {
        "signing_key_configured",
        "graph_built",
        "receipt_store",
        "langgraph_checkpointer",
        "langgraph_checkpoint_store",
        "langgraph_checkpoint_roundtrip",
    }
    if require_model_probe_health:
        critical_names.add("model_probe_healthy")

    critical_checks = {
        k: v for k, v in checks.items()
        if k in critical_names
    }
    critical_ready = all(critical_checks.values())

    status = "ready" if all_ready else ("degraded" if critical_ready else "not_ready")
    return JSONResponse(
        status_code=200 if critical_ready else 503,
        content={
            "status": status,
            "service": "aspire-orchestrator",
            "checks": checks,
            "require_model_probe_health": require_model_probe_health,
            "checkpointer": get_checkpointer_runtime(),
            "model_probe": get_model_probe_status(),
        },
    )


@app.get("/metrics")
async def metrics(request: Request) -> Response:
    """Prometheus metrics endpoint (Gate 2: Observability).

    Security: Only accessible from localhost/internal networks OR with
    a valid metrics auth token (ASPIRE_METRICS_TOKEN).
    In production, Prometheus scrapes from within the private network.
    """
    # Restrict to internal access only (Gate 5: Security)
    # ASPIRE_METRICS_ALLOW_EXTERNAL=1 disables this check (testing only).
    if not os.environ.get("ASPIRE_METRICS_ALLOW_EXTERNAL"):
        # Check metrics auth token first (defense-in-depth)
        metrics_token = os.environ.get("ASPIRE_METRICS_TOKEN")
        if metrics_token:
            provided = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if provided == metrics_token:
                return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

        client_host = request.client.host if request.client else "unknown"
        allowed_hosts = {"127.0.0.1", "::1", "localhost"}
        is_private = (
            client_host in allowed_hosts
            or client_host.startswith("10.")
            or client_host.startswith("172.")
            or client_host.startswith("192.168.")
        )
        if not is_private:
            return Response(status_code=403, content=b"Forbidden: metrics internal only")
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# =============================================================================
# POST /v1/intents — Primary Orchestrator Endpoint
# =============================================================================


async def stream_agent_activity(
    initial_state: dict[str, Any],
    *,
    suite_id: str,
    office_id: str,
    actor_id: str,
    correlation_id: str,
    thread_id: str,
) -> Any:
    """Generator for real-time Server-Sent Events during orchestrator execution."""
    import asyncio
    from aspire_orchestrator.skillpacks.adam_research import set_activity_event_callback
    from aspire_orchestrator.services.sse_manager import (
        format_sse_event,
        get_connection_tracker,
        build_stream_receipt,
        StreamRateLimiter,
        HEARTBEAT_INTERVAL_SECONDS,
    )

    stream_id = str(uuid.uuid4())
    tracker = get_connection_tracker()
    rate_limiter = StreamRateLimiter()
    emitted_event_count = 0
    graph_task: asyncio.Task[dict[str, Any]] | None = None

    if not tracker.try_connect(
        suite_id,
        stream_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
    ):
        deny_receipt = build_stream_receipt(
            action_type="stream.denied",
            suite_id=suite_id,
            office_id=office_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            outcome="DENIED",
            stream_id=stream_id,
            reason_code="CONNECTION_LIMIT_EXCEEDED",
            details={"limit": 100, "current": tracker.get_connection_count(suite_id)},
        )
        try:
            store_receipts([deny_receipt])
        except Exception as e:
            logger.warning("SSE receipt storage failed: %s", e)
        yield format_sse_event({
            "type": "error",
            "message": "Connection limit exceeded for tenant",
            "code": "CONNECTION_LIMIT_EXCEEDED",
            "timestamp": int(time.time() * 1000),
        })
        return

    initiation_receipt = build_stream_receipt(
        action_type="stream.initiate",
        suite_id=suite_id,
        office_id=office_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        outcome="success",
        stream_id=stream_id,
    )
    receipt_id = initiation_receipt["id"]
    try:
        store_receipts([initiation_receipt])
    except Exception as e:
        logger.warning("SSE receipt storage failed: %s", e)

    loop = asyncio.get_running_loop()
    event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)

    def _enqueue_event(event: dict[str, Any]) -> None:
        try:
            event_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("SSE event queue full for stream %s; dropping event", stream_id[:8])

    def collect_event(event: dict[str, Any]) -> None:
        # Thread-safe enqueue for sync invoke fallback worker threads.
        loop.call_soon_threadsafe(_enqueue_event, event)

    set_activity_event_callback(collect_event)

    yield format_sse_event({
        "type": "connected",
        "receipt_id": receipt_id,
        "stream_id": stream_id,
        "correlation_id": correlation_id,
        "timestamp": int(time.time() * 1000),
    })

    # Activity callback is set via set_activity_event_callback() above (line 605).
    # Do NOT inject into state — functions can't be msgpack-serialized by the
    # LangGraph Postgres checkpointer. Nodes use get_activity_event_callback().

    last_heartbeat = time.monotonic()
    try:
        graph_task = asyncio.create_task(
            invoke_orchestrator_graph(
                initial_state,
                thread_id=thread_id,
            ),
        )

        while True:
            now = time.monotonic()
            heartbeat_remaining = max(0.0, HEARTBEAT_INTERVAL_SECONDS - (now - last_heartbeat))
            # Fast-flush: if the graph already finished (e.g. greeting fast path),
            # poll with 100ms timeout instead of waiting the full heartbeat interval.
            # For in-progress graphs, poll every 0.5s so responses flush quickly.
            if graph_task.done():
                timeout = 0.1
            elif heartbeat_remaining > 0.5:
                timeout = 0.5
            else:
                timeout = heartbeat_remaining
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=timeout)
                if rate_limiter.check():
                    emitted_event_count += 1
                    tracker.increment_event_count(stream_id)
                    yield format_sse_event(event)
                else:
                    logger.debug("SSE rate limited: skipping event for stream %s", stream_id[:8])
            except asyncio.TimeoutError:
                yield format_sse_event({"type": "heartbeat", "timestamp": int(time.time() * 1000)})
                last_heartbeat = time.monotonic()

            if graph_task.done() and event_queue.empty():
                # Emit final response event before closing stream
                try:
                    graph_result = graph_task.result()
                    response = graph_result.get("response")
                    response_text = ""
                    if isinstance(response, dict):
                        response_text = response.get("text", "")
                    elif isinstance(response, str):
                        response_text = response
                    # Also check conversation_response (from agent_dispatch)
                    if not response_text and graph_result.get("conversation_response"):
                        response_text = graph_result["conversation_response"]

                    # Resolve agent identity using canonical precedence chain
                    # (requested_agent > agent_target > domain fallback).
                    # BUG FIX: was using raw graph_result["agent_target"] which
                    # comes from the LLM classifier — it can return "ava_admin"
                    # for business/ops questions even on Desktop user-Ava sessions.
                    from aspire_orchestrator.services.agent_identity import resolve_assigned_agent
                    _resolved_agent = resolve_assigned_agent(graph_result)
                    # Fall back to response-level assigned_agent if available
                    if _resolved_agent == "ava" and isinstance(response, dict):
                        _resp_agent = response.get("assigned_agent")
                        if isinstance(_resp_agent, str) and _resp_agent.strip():
                            _resolved_agent = _resp_agent.strip().lower()

                    yield format_sse_event({
                        "type": "response",
                        "content": response_text,
                        "message": response_text,
                        "agent": _resolved_agent,
                        "timestamp": int(time.time() * 1000),
                        "data": {
                            "text": response_text,
                            "correlation_id": correlation_id,
                            "receipt_ids": graph_result.get("receipt_ids", []),
                            "assigned_agent": _resolved_agent,
                        },
                    })
                except Exception as e:
                    logger.error("SSE: failed to emit final response: %s", e)
                    yield format_sse_event({
                        "type": "error",
                        "message": "I processed that but had trouble sending the result. The action may have completed — check your dashboard.",
                        "timestamp": int(time.time() * 1000),
                    })
                break

        try:
            result = await graph_task
        except Exception as e:
            logger.error("Graph task failed: %s", e)
            yield format_sse_event({
                "type": "error",
                "message": f"I hit an issue processing that request. Let me try again — {str(e)[:150]}",
                "icon": "error",
                "timestamp": int(time.time() * 1000),
            })
            return

        while not event_queue.empty():
            event = event_queue.get_nowait()
            if rate_limiter.check():
                emitted_event_count += 1
                tracker.increment_event_count(stream_id)
                yield format_sse_event(event)

        response = result.get("response", {}) if isinstance(result, dict) else {}
        if response.get("error"):
            yield format_sse_event({
                "type": "error",
                "message": response.get("message", "Something went wrong on my end. Try again in a moment."),
                "icon": "error",
                "timestamp": int(time.time() * 1000),
            })
        else:
            yield format_sse_event({
                "type": "done",
                "message": "Request completed",
                "icon": "done",
                "timestamp": int(time.time() * 1000),
            })

        # Emit sentence-level partial_response events for streaming TTS
        response_text = response.get("text", "") if isinstance(response, dict) else ""
        if response_text:
            sentences = _SENTENCE_SPLIT_RE.split(response_text)
            sentences = [s.strip() for s in sentences if s.strip()]
            for i, sentence in enumerate(sentences):
                partial_event = {
                    "type": "partial_response",
                    "text": sentence,
                    "index": i,
                    "is_last": i == len(sentences) - 1,
                    "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                }
                yield format_sse_event(partial_event)

        # [DONE] sentinel — signals end of SSE stream to clients
        yield "data: [DONE]\n\n"

        completion_receipt = build_stream_receipt(
            action_type="stream.complete",
            suite_id=suite_id,
            office_id=office_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            outcome="success",
            stream_id=stream_id,
            details={
                "event_count": emitted_event_count,
                "stream_id": stream_id,
            },
        )
        try:
            store_receipts([completion_receipt])
        except Exception as e:
            logger.warning("SSE receipt storage failed: %s", e)

    except asyncio.CancelledError:
        logger.info("SSE stream cancelled (client disconnect): %s", stream_id[:8])
        try:
            store_receipts([build_stream_receipt(
                action_type="stream.cancelled",
                suite_id=suite_id,
                office_id=office_id,
                actor_id=actor_id,
                correlation_id=correlation_id,
                outcome="success",
                stream_id=stream_id,
                reason_code="CLIENT_DISCONNECT",
            )])
        except Exception as e:
            logger.warning("SSE receipt storage failed: %s", e)

    except Exception as e:
        logger.exception("SSE stream error: %s", e)
        yield format_sse_event({
            "type": "error",
            "message": "Stream interrupted",
            "icon": "error",
            "timestamp": int(time.time() * 1000),
        })
        try:
            store_receipts([build_stream_receipt(
                action_type="stream.error",
                suite_id=suite_id,
                office_id=office_id,
                actor_id=actor_id,
                correlation_id=correlation_id,
                outcome="FAILED",
                stream_id=stream_id,
                reason_code="STREAM_ERROR",
                details={"error_type": type(e).__name__},
            )])
        except Exception as e:
            logger.warning("SSE receipt storage failed: %s", e)

    finally:
        if graph_task and not graph_task.done():
            graph_task.cancel()
        set_activity_event_callback(None)
        tracker.disconnect(suite_id, stream_id)


@app.post("/v1/intents", response_model=None)
async def process_intent(request: Request, stream: bool = Query(default=False)) -> JSONResponse | StreamingResponse:
    """Process an AvaOrchestratorRequest through the orchestrator graph.

    The graph executes the full pipeline:
    Intake -> Safety -> Policy -> Approval -> TokenMint -> Execute -> ReceiptWrite -> Respond

    Query Parameters:
        stream: If true, return Server-Sent Events stream with intermediate agent activity (Wave 4).
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "error": "SCHEMA_VALIDATION_FAILED",
                "message": "Invalid JSON body",
                "correlation_id": "unknown",
            },
        )

    # Auth context propagated from Gateway via X- headers
    # Law #3: Fail closed — missing actor_id is denied with receipt
    actor_id = request.headers.get("x-actor-id")
    suite_id = request.headers.get("x-suite-id")
    correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())

    if not actor_id:
        logger.warning("Missing x-actor-id header [correlation_id=%s]", correlation_id)
        # Law #2: Emit deny receipt for missing actor
        try:
            store_receipts([{
                "id": str(uuid.uuid4()),
                "correlation_id": correlation_id,
                "suite_id": suite_id or "unknown",
                "office_id": request.headers.get("x-office-id", "unknown"),
                "actor_type": "system",
                "actor_id": "fail_closed_guard",
                "action_type": "intent.process",
                "risk_tier": "green",
                "tool_used": "orchestrator.auth_guard",
                "outcome": "DENIED",
                "reason_code": "MISSING_ACTOR_ID",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "receipt_type": "auth_denial",
                "receipt_hash": "",
            }])
        except Exception:
            pass  # Receipt failure must not mask the auth denial
        return JSONResponse(
            status_code=401,
            content={
                "error": "AUTH_REQUIRED",
                "message": "Missing x-actor-id header (Law #3: fail closed)",
                "correlation_id": correlation_id,
            },
        )

    thread_id = resolve_thread_id(
        body,
        suite_id=suite_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
    )

    initial_state: dict[str, Any] = {
        "request": body,
        "actor_id": actor_id,
        "thread_id": thread_id,
    }
    if suite_id:
        initial_state["auth_suite_id"] = suite_id
    if correlation_id:
        initial_state["correlation_id"] = correlation_id

    # Extract approval_evidence from request body for YELLOW/RED re-submit flows.
    # The client sends approval_evidence in the top-level body; the graph expects
    # it as a top-level state field (approval_check reads state["approval_evidence"]).
    if isinstance(body, dict) and "approval_evidence" in body:
        initial_state["approval_evidence"] = body["approval_evidence"]
    if isinstance(body, dict) and "presence_token" in body:
        initial_state["presence_token"] = body["presence_token"]

    # Wave 4 + SSE Enterprise: If stream=true, return SSE stream instead of JSON response
    if stream:
        office_id = request.headers.get("x-office-id", "")
        return StreamingResponse(
            stream_agent_activity(
                initial_state,
                suite_id=suite_id or "unknown",
                office_id=office_id,
                actor_id=actor_id,
                correlation_id=correlation_id,
                thread_id=thread_id,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
                "X-Correlation-Id": correlation_id,
            },
        )

    # Phase 6: Track workflow execution for admin portal visibility
    workflow_id: str | None = None
    try:
        from aspire_orchestrator.services.workflow_tracker import get_workflow_tracker
        workflow_id = get_workflow_tracker().start_workflow(
            tenant_id=suite_id or "unknown",
            workflow_type="intent",
            correlation_id=correlation_id,
            input_summary={"task_type": body.get("task_type") if isinstance(body, dict) else "unknown"},
        )
    except Exception:
        pass  # Workflow tracking is best-effort

    start_time = time.monotonic()
    try:
        result = await invoke_orchestrator_graph(
            initial_state,
            thread_id=thread_id,
        )

        response = result.get("response")
        if response is None:
            METRICS.record_request(status="failed", task_type=body.get("task_type", "unknown") if isinstance(body, dict) else "unknown")
            return JSONResponse(
                status_code=500,
                content={
                    "error": "INTERNAL_ERROR",
                    "message": "Orchestrator returned no response",
                    "correlation_id": result.get("correlation_id", "unknown"),
                },
            )

        error = response.get("error")
        if error:
            status_map = {
                "SCHEMA_VALIDATION_FAILED": 400,
                "APPROVAL_REQUIRED": 202,
                "PRESENCE_REQUIRED": 202,
                "CAPABILITY_TOKEN_REQUIRED": 403,
                "CAPABILITY_TOKEN_EXPIRED": 403,
                "TENANT_ISOLATION_VIOLATION": 403,
                "POLICY_DENIED": 403,
                "SAFETY_BLOCKED": 403,
                "RECEIPT_WRITE_FAILED": 503,
                "MODEL_UNAVAILABLE": 503,
                "CHECKPOINTER_UNAVAILABLE": 503,
                "UPSTREAM_TIMEOUT": 504,
                "ROUTER_FALLBACK_ACTIVE": 200,
                "PROVIDER_AUTH_MISSING": 503,
                "PROVIDER_ALL_FAILED": 503,
                "ROUTING_DENIED": 400,
                "PARAM_EXTRACTION_FAILED": 400,
                "CLASSIFICATION_UNCLEAR": 200,
                "EXECUTION_FAILED": 500,
                "INTERNAL_ERROR": 500,
            }
            status_code = status_map.get(error, 500)
            if status_code == 200:
                outcome_status = "success"
            else:
                outcome_status = "denied" if status_code in (403, 202) else "failed"
            METRICS.record_request(
                status=outcome_status,
                risk_tier=response.get("risk_tier", "unknown"),
                task_type=body.get("task_type", "unknown") if isinstance(body, dict) else "unknown",
            )
            return JSONResponse(status_code=status_code, content=response)

        METRICS.record_request(
            status="success",
            risk_tier=response.get("risk_tier", "unknown"),
            task_type=body.get("task_type", "unknown") if isinstance(body, dict) else "unknown",
        )
        # Phase 6: Record workflow completion
        if workflow_id:
            try:
                get_workflow_tracker().complete_workflow(
                    workflow_id=workflow_id,
                    output_summary={"status": "success", "risk_tier": response.get("risk_tier")},
                )
            except Exception:
                pass
        return JSONResponse(status_code=200, content=response)

    except GraphInvokeUnavailableError as e:
        logger.exception("Orchestrator checkpointer unavailable: %s", e)
        METRICS.record_request(status="failed", task_type=body.get("task_type", "unknown") if isinstance(body, dict) else "unknown")
        if workflow_id:
            try:
                get_workflow_tracker().fail_workflow(
                    workflow_id=workflow_id, error_type="CHECKPOINTER_UNAVAILABLE", error_message=str(e)[:500],
                )
            except Exception:
                pass
        return JSONResponse(
            status_code=503,
            content={
                "error": "CHECKPOINTER_UNAVAILABLE",
                "message": "Ava memory service is temporarily unavailable",
                "correlation_id": body.get("correlation_id", "unknown") if isinstance(body, dict) else "unknown",
            },
        )
    except Exception as e:
        logger.exception("Orchestrator error: %s", e)
        METRICS.record_request(status="failed", task_type=body.get("task_type", "unknown") if isinstance(body, dict) else "unknown")
        if workflow_id:
            try:
                get_workflow_tracker().fail_workflow(
                    workflow_id=workflow_id, error_type=type(e).__name__, error_message=str(e)[:500],
                )
            except Exception:
                pass
        return JSONResponse(
            status_code=500,
            content={
                "error": "INTERNAL_ERROR",
                "message": str(e),
                "correlation_id": body.get("correlation_id", "unknown") if isinstance(body, dict) else "unknown",
            },
        )
    finally:
        duration = time.monotonic() - start_time
        METRICS.request_duration.labels(node="full_pipeline").observe(duration)


# =============================================================================
# GET /v1/receipts — Receipt Query (RLS-scoped)
# =============================================================================


@app.get("/v1/receipts")
async def get_receipts(
    request: Request,
    suite_id: str = Query(..., description="Suite ID (from auth, enforced at gateway)"),
    correlation_id: str | None = Query(None),
    action_type: str | None = Query(None),
    risk_tier: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    """Query receipts for a given suite_id with optional filters.

    suite_id is always the auth-derived value from the Gateway.
    This enforces tenant isolation (Law #6) at the query level.
    """
    # Law #6: Validate auth header matches query suite_id (tenant isolation)
    auth_suite_id = request.headers.get("x-suite-id")
    if auth_suite_id and suite_id != auth_suite_id:
        return JSONResponse(
            status_code=403,
            content={
                "error": "TENANT_ISOLATION_VIOLATION",
                "message": "suite_id query param does not match authenticated suite",
                "correlation_id": request.headers.get("x-correlation-id", "unknown"),
            },
        )

    if risk_tier and risk_tier not in ("green", "yellow", "red"):
        return JSONResponse(
            status_code=400,
            content={
                "error": "SCHEMA_VALIDATION_FAILED",
                "message": f"Invalid risk_tier: {risk_tier}",
                "correlation_id": request.headers.get("x-correlation-id", "unknown"),
            },
        )

    receipts = query_receipts(
        suite_id=suite_id,
        correlation_id=correlation_id,
        action_type=action_type,
        risk_tier=risk_tier,
        limit=limit,
        offset=offset,
    )

    return JSONResponse(
        status_code=200,
        content={
            "receipts": receipts,
            "count": len(receipts),
            "suite_id": suite_id,
            "filters": {
                "correlation_id": correlation_id,
                "action_type": action_type,
                "risk_tier": risk_tier,
            },
            "pagination": {
                "limit": limit,
                "offset": offset,
            },
        },
    )


# --- Phase 4B: Background Task Status ---


@app.get("/v1/tasks/{task_id}/status")
async def get_task_status(task_id: str) -> JSONResponse:
    """Query the status of a background task by ID (Phase 4B)."""
    from aspire_orchestrator.services.task_queue import get_task_queue

    result = get_task_queue().get_status(task_id)
    if not result:
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found", "task_id": task_id},
        )
    return JSONResponse(
        status_code=200,
        content={
            "task_id": result.task_id,
            "status": result.status.value,
            "created_at": result.created_at.isoformat(),
            "completed_at": result.completed_at.isoformat() if result.completed_at else None,
            "error": result.error,
        },
    )


# =============================================================================
# POST /v1/receipts/verify-run — Hash Chain Verification
# =============================================================================


@app.post("/v1/receipts/verify-run")
async def verify_receipt_chain(request: Request) -> JSONResponse:
    """Verify receipt hash chain integrity for a correlation_id.

    Uses the receipt_chain verifier to recompute hashes and detect tampering.
    Returns verification result with chain_length, broken_links, etc.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "error": "SCHEMA_VALIDATION_FAILED",
                "message": "Invalid JSON body",
                "correlation_id": "unknown",
            },
        )

    suite_id = body.get("suite_id")
    if not suite_id:
        return JSONResponse(
            status_code=400,
            content={
                "error": "SCHEMA_VALIDATION_FAILED",
                "message": "suite_id is required",
                "correlation_id": request.headers.get("x-correlation-id", "unknown"),
            },
        )

    chain_receipts = get_chain_receipts(suite_id=suite_id, chain_id=suite_id)

    if not chain_receipts:
        return JSONResponse(
            status_code=200,
            content={
                "verified": True,
                "chain_length": 0,
                "broken_links": [],
                "message": "No receipts found for this suite",
            },
        )

    verification = verify_chain(chain_receipts, chain_id=suite_id)

    return JSONResponse(
        status_code=200,
        content={
            "verified": verification.valid,
            "chain_length": verification.receipts_verified,
            "error_count": verification.error_count,
            "broken_links": [
                {
                    "sequence": err.sequence,
                    "message": str(err),
                    "details": err.details,
                }
                for err in verification.errors
            ],
        },
    )


# =============================================================================
# POST /v1/policy/evaluate — Policy Evaluation (Read-Only)
# =============================================================================


@app.post("/v1/policy/evaluate")
async def evaluate_policy(request: Request) -> JSONResponse:
    """Evaluate policy for an action type without executing.

    Returns risk tier, approval requirements, tools, and capability scope.
    Used by the UI to show risk indicators before user commits.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "error": "SCHEMA_VALIDATION_FAILED",
                "message": "Invalid JSON body",
                "correlation_id": "unknown",
            },
        )

    action_type = body.get("action_type")
    if not action_type or not isinstance(action_type, str):
        return JSONResponse(
            status_code=400,
            content={
                "error": "SCHEMA_VALIDATION_FAILED",
                "message": "action_type is required (string)",
                "correlation_id": request.headers.get("x-correlation-id", "unknown"),
            },
        )

    matrix = get_policy_matrix()
    eval_result = matrix.evaluate(action_type)

    return JSONResponse(
        status_code=200,
        content={
            "action_type": action_type,
            "allowed": eval_result.allowed,
            "risk_tier": eval_result.risk_tier.value,
            "approval_required": eval_result.approval_required,
            "presence_required": eval_result.presence_required,
            "tools": eval_result.tools,
            "capability_scope": eval_result.capability_scope,
            "redact_fields": eval_result.redact_fields,
            "deny_reason": eval_result.deny_reason,
        },
    )


# =============================================================================
# GET /v1/registry/capabilities — Capability Discovery
# =============================================================================


@app.get("/v1/registry/capabilities")
async def list_capabilities(
    category: str | None = Query(None),
    risk_tier: str | None = Query(None),
    status: str | None = Query(None),
) -> JSONResponse:
    """List all registered capabilities for discovery.

    Used by the UI to show available actions, agents, and risk tiers.
    No auth required beyond Gateway-level auth (read-only, no tenant secrets).
    """
    registry = get_registry()

    risk_tier_enum = None
    if risk_tier:
        from aspire_orchestrator.models import RiskTier as RT
        try:
            risk_tier_enum = RT(risk_tier)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "SCHEMA_VALIDATION_FAILED",
                    "message": f"Invalid risk_tier: {risk_tier}",
                },
            )

    packs = registry.list_skill_packs(
        category=category,
        risk_tier=risk_tier_enum,
        status=status,
    )

    return JSONResponse(
        status_code=200,
        content={
            "capabilities": [
                {
                    "skill_pack_id": p.id,
                    "name": p.name,
                    "owner": p.owner,
                    "category": p.category,
                    "risk_tier": p.risk_tier.value,
                    "status": p.status,
                    "description": p.description,
                    "actions": p.actions,
                    "capability_scopes": p.capability_scopes,
                    "tools": p.tools,
                    "providers": p.providers,
                }
                for p in packs
            ],
            "count": len(packs),
            "stats": registry.get_stats(),
        },
    )


@app.get("/v1/registry/skill-packs/{pack_id}")
async def get_skill_pack(pack_id: str) -> JSONResponse:
    """Get a specific skill pack manifest."""
    registry = get_registry()
    pack = registry.get_skill_pack(pack_id)

    if pack is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "NOT_FOUND",
                "message": f"Skill pack not found: {pack_id}",
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "skill_pack_id": pack.id,
            "name": pack.name,
            "owner": pack.owner,
            "category": pack.category,
            "risk_tier": pack.risk_tier.value,
            "status": pack.status,
            "description": pack.description,
            "actions": pack.actions,
            "capability_scopes": pack.capability_scopes,
            "tools": pack.tools,
            "providers": pack.providers,
            "per_suite_enabled": pack.per_suite_enabled,
        },
    )


@app.get("/v1/registry/route/{action_type:path}")
async def route_action(action_type: str) -> JSONResponse:
    """Route an action_type to the responsible skill pack.

    Used by the orchestrator internally and by clients for preview.
    """
    registry = get_registry()
    result = registry.route_action(action_type)

    if not result.found:
        return JSONResponse(
            status_code=404,
            content={
                "found": False,
                "action_type": action_type,
                "message": f"No skill pack registered for action: {action_type}",
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "found": True,
            "action_type": action_type,
            "skill_pack_id": result.skill_pack_id,
            "skill_pack_name": result.skill_pack_name,
            "owner": result.owner,
            "risk_tier": result.risk_tier.value if result.risk_tier else None,
            "tools": result.tools,
            "providers": result.providers,
        },
    )


# =============================================================================
# A2A Router Endpoints
# =============================================================================


@app.post("/v1/a2a/dispatch")
async def a2a_dispatch(request: Request) -> JSONResponse:
    """Dispatch a task to a skill pack agent.

    Only the orchestrator should call this (Law #1: Single Brain).
    The Gateway enforces auth; the orchestrator trusts its own internal calls.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "SCHEMA_VALIDATION_FAILED", "message": "Invalid JSON body"},
        )

    suite_id = body.get("suite_id")
    office_id = body.get("office_id")
    correlation_id = body.get("correlation_id")
    task_type = body.get("task_type")
    assigned_to_agent = body.get("assigned_to_agent")

    if not all([suite_id, office_id, correlation_id, task_type, assigned_to_agent]):
        return JSONResponse(
            status_code=400,
            content={
                "error": "SCHEMA_VALIDATION_FAILED",
                "message": "Required: suite_id, office_id, correlation_id, task_type, assigned_to_agent",
            },
        )

    a2a = get_a2a_service()
    result = a2a.dispatch(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        task_type=task_type,
        assigned_to_agent=assigned_to_agent,
        payload=body.get("payload", {}),
        priority=body.get("priority", 3),
        idempotency_key=body.get("idempotency_key"),
    )

    if result.receipt_data:
        store_receipts([result.receipt_data])

    return JSONResponse(
        status_code=201 if result.success else 400,
        content={
            "success": result.success,
            "task_id": result.task_id,
            "error": result.error,
            "receipt_id": result.receipt_data.get("id") if result.receipt_data else None,
        },
    )


# =============================================================================
# POST /v1/agents/invoke-sync — Thin sync API (no LangGraph, no task queue)
# =============================================================================


SYNC_INVOKE_AGENTS = {"quinn", "adam", "tec"}


@app.post("/v1/agents/invoke-sync")
async def agents_invoke_sync(request: Request) -> JSONResponse:
    """Call a skillpack agent directly and return the response inline.

    This is the v1 voice-friendly endpoint — synchronous, no LangGraph,
    no task queue. Calls the agent's run_agentic_loop directly and returns
    the conversational response within the request lifecycle.

    Used by ElevenLabs Ava's invoke_quinn/invoke_adam/invoke_tec tools
    via the Desktop server proxy.

    Law #1: Ava (the orchestrator) decides who to call.
    Law #2: Agent emits receipts internally.
    Law #7: Agent executes bounded commands only.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "INVALID_JSON", "message": "Invalid JSON body"},
        )

    suite_id = body.get("suite_id", "")
    office_id = body.get("office_id", suite_id)
    correlation_id = body.get("correlation_id", "")
    agent = body.get("agent", "")
    task = body.get("task", "")
    details = body.get("details", "")

    if not agent or agent not in SYNC_INVOKE_AGENTS:
        return JSONResponse(
            status_code=400,
            content={
                "error": "INVALID_AGENT",
                "message": f"Agent must be one of: {', '.join(sorted(SYNC_INVOKE_AGENTS))}",
            },
        )

    if not task:
        return JSONResponse(
            status_code=400,
            content={"error": "MISSING_TASK", "message": "Task is required"},
        )

    try:
        from aspire_orchestrator.nodes.agent_dispatch import _init_registry
        from aspire_orchestrator.services.agent_sdk_base import AgentContext

        registry = _init_registry()
        skill_pack = registry.get(agent)

        if not skill_pack:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "AGENT_NOT_LOADED",
                    "message": f"Agent '{agent}' could not be loaded",
                },
            )

        # Validate suite_id — Supabase requires a real UUID, not empty string
        import uuid as _uuid
        def _ensure_uuid(val: str) -> str:
            if not val:
                return str(_uuid.uuid4())
            try:
                _uuid.UUID(val)
                return val
            except ValueError:
                return str(_uuid.uuid4())

        safe_suite_id = _ensure_uuid(suite_id)
        safe_office_id = _ensure_uuid(office_id) if office_id else safe_suite_id

        ctx = AgentContext(
            suite_id=safe_suite_id,
            office_id=safe_office_id,
            correlation_id=correlation_id or f"sync-{agent}-{_uuid.uuid4().hex[:8]}",
            risk_tier="yellow" if agent == "quinn" else "green",
        )

        # Combine task + details into a single prompt
        full_task = task
        if details:
            full_task = f"{task}. Additional details: {details}"

        # ── Adam: Next-Gen Research Engine (v6) ──
        # Multi-mode: vendor (places-first), strategy (web-first 3-stream),
        # competitive (places+web), topic (web-only). Mode detected by GPT-5.2.
        if agent == "adam":
            import asyncio as _asyncio
            import json as _json
            import os as _os
            import re as _re
            from aspire_orchestrator.services.openai_client import generate_text_async
            from aspire_orchestrator.services.search_router import (
                _web_search_chain, _places_search_chain, _geocode_chain,
            )
            from aspire_orchestrator.models import Outcome

            api_key = _os.environ.get("ASPIRE_OPENAI_API_KEY") or _os.environ.get("OPENAI_API_KEY")

            # ── Step 0: Mode Detection (GPT-5.2) ──
            mode = "vendor"  # default
            try:
                mode_resp = await _asyncio.wait_for(
                    generate_text_async(
                        model="gpt-5.2",
                        messages=[
                            {"role": "developer", "content": "Classify this research task into exactly one mode. Return ONLY the mode name, nothing else: vendor, strategy, competitive, or topic"},
                            {"role": "user", "content": f"Task: {full_task}\n\nvendor = find specific businesses/stores/suppliers\nstrategy = plan/start/grow a business, build a strategy\ncompetitive = analyze competitors, competitive landscape\ntopic = specific question about regulations/licenses/how-to"},
                        ],
                        api_key=api_key,
                        base_url="https://api.openai.com/v1",
                        timeout_seconds=8.0,
                        max_output_tokens=50,
                        prefer_responses_api=True,
                    ),
                    timeout=6.0,
                )
                detected = mode_resp.strip().lower().split()[0] if mode_resp else "vendor"
                if detected in ("vendor", "strategy", "competitive", "topic"):
                    mode = detected
                logger.info("Adam mode detected: %s for task: %s", mode, full_task[:80])
            except Exception as mode_err:
                logger.warning("Adam mode detection failed, defaulting to vendor: %s", type(mode_err).__name__)

            # ── STRATEGY MODE: 3 parallel research streams ──
            if mode == "strategy":
                common_kwargs_web = dict(
                    correlation_id=ctx.correlation_id,
                    suite_id=safe_suite_id,
                    office_id=safe_office_id,
                    risk_tier="green",
                )

                # Plan 3 research streams
                try:
                    plan_resp = await _asyncio.wait_for(
                        generate_text_async(
                            model="gpt-5.2",
                            messages=[
                                {"role": "developer", "content": "Return ONLY a JSON object with 3 keys: stream_a (market queries), stream_b (operations queries), stream_c (revenue queries). Each key has an array of 2 search query strings. No explanation."},
                                {"role": "user", "content": f"Plan 3 research streams for this task. Each stream needs 2 web search queries.\nTask: {full_task}"},
                            ],
                            api_key=api_key,
                            base_url="https://api.openai.com/v1",
                            timeout_seconds=12.0,
                            max_output_tokens=4096,
                            prefer_responses_api=True,
                        ),
                        timeout=10.0,
                    )
                    cleaned = plan_resp.strip()
                    if cleaned.startswith("```"):
                        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                        cleaned = cleaned.rsplit("```", 1)[0]
                    streams = _json.loads(cleaned.strip())
                except Exception as plan_err:
                    logger.warning("Adam strategy plan failed: %s, using template", type(plan_err).__name__)
                    streams = {
                        "stream_a": [f"{full_task} market demand competitors", f"{full_task} industry trends 2026"],
                        "stream_b": [f"{full_task} startup costs equipment licenses", f"{full_task} hiring requirements insurance"],
                        "stream_c": [f"{full_task} pricing rates revenue benchmarks", f"{full_task} profit margins growth path"],
                    }

                # Execute all streams in parallel (web-first — Brave + Tavily)
                async def _web_search(query: str) -> list[dict]:
                    results = []
                    for pname, executor_fn in _web_search_chain():
                        try:
                            result = await _asyncio.wait_for(
                                executor_fn(payload={"query": query}, **common_kwargs_web),
                                timeout=10.0,
                            )
                            if result.outcome == Outcome.SUCCESS and result.data:
                                for r in result.data.get("results", [])[:5]:
                                    results.append({
                                        "title": r.get("title", r.get("name", "")),
                                        "url": r.get("url", ""),
                                        "snippet": r.get("snippet", r.get("description", "")),
                                        "source": pname,
                                    })
                            if results:
                                break  # Got results from first provider, skip fallback
                        except Exception:
                            continue
                    return results

                # Flatten all queries and search in parallel
                all_queries = []
                stream_labels = []
                for label in ("stream_a", "stream_b", "stream_c"):
                    for q in streams.get(label, [])[:2]:
                        all_queries.append(q)
                        stream_labels.append(label)

                search_results = await _asyncio.gather(*[_web_search(q) for q in all_queries])

                # Group results by stream
                stream_data = {"stream_a": [], "stream_b": [], "stream_c": []}
                for i, results in enumerate(search_results):
                    label = stream_labels[i] if i < len(stream_labels) else "stream_a"
                    stream_data[label].extend(results)

                total_results = sum(len(v) for v in stream_data.values())
                logger.info("Adam strategy search: %d total results across 3 streams", total_results)

                # Synthesize with GPT-5.2
                try:
                    synthesis_prompt = (
                        f"You are Adam, a strategic research specialist for small business owners.\n"
                        f"Task: {full_task}\n\n"
                        f"Stream A (Market):\n{_json.dumps(stream_data['stream_a'][:8], indent=2, default=str)}\n\n"
                        f"Stream B (Operations):\n{_json.dumps(stream_data['stream_b'][:8], indent=2, default=str)}\n\n"
                        f"Stream C (Revenue):\n{_json.dumps(stream_data['stream_c'][:8], indent=2, default=str)}\n\n"
                        f"Create a strategic brief for a small business owner. Structure as JSON:\n"
                        f'{{"market": {{"summary": "...", "competitors": ["..."], "demand": "..."}},\n'
                        f' "operations": {{"startup_costs": "...", "equipment": ["..."], "licenses": ["..."], "hiring": "..."}},\n'
                        f' "revenue": {{"pricing": "...", "contract_value": "...", "targets": "...", "margins": "..."}},\n'
                        f' "recommended_approach": "one paragraph recommendation",\n'
                        f' "confidence": "high/medium/low",\n'
                        f' "flags": ["anything only found in 1 source"]}}\n\n'
                        f"Use ONLY data from the search results. If you estimate a number, say so. Be specific."
                    )

                    synthesis = await generate_text_async(
                        model="gpt-5.2",
                        messages=[
                            {"role": "developer", "content": "Create a strategic research brief. Return valid JSON. Be specific with numbers from the search results."},
                            {"role": "user", "content": synthesis_prompt},
                        ],
                        api_key=api_key,
                        base_url="https://api.openai.com/v1",
                        timeout_seconds=18.0,
                        max_output_tokens=4096,
                        prefer_responses_api=True,
                    )

                    # Parse the brief
                    brief_text = synthesis.strip()
                    if brief_text.startswith("```"):
                        brief_text = brief_text.split("\n", 1)[1] if "\n" in brief_text else brief_text[3:]
                        brief_text = brief_text.rsplit("```", 1)[0]
                    try:
                        brief = _json.loads(brief_text.strip())
                    except _json.JSONDecodeError:
                        brief = {"recommended_approach": brief_text, "confidence": "medium"}

                    # Build voice-friendly summary from the brief
                    rec = brief.get("recommended_approach", "")
                    market_summary = brief.get("market", {}).get("summary", "")
                    response_text = rec if rec else market_summary if market_summary else synthesis[:500]

                except Exception as synth_err:
                    logger.warning("Adam strategy synthesis failed: %s", synth_err)
                    brief = {}
                    response_text = f"I researched {full_task} but had trouble synthesizing the results. Here's what I found across {total_results} sources."

                return JSONResponse(
                    status_code=200,
                    content={
                        "success": total_results > 0,
                        "agent": "adam",
                        "result": response_text,
                        "data": {
                            "mode": "strategy",
                            "brief": brief,
                            "stream_results": {k: len(v) for k, v in stream_data.items()},
                            "total_results": total_results,
                            "queries_used": all_queries,
                        },
                        "receipt_id": ctx.correlation_id,
                        "error": None if total_results > 0 else "No results found",
                    },
                )

            # ── TOPIC MODE: web-only focused research ──
            if mode == "topic":
                common_kwargs_web = dict(
                    correlation_id=ctx.correlation_id,
                    suite_id=safe_suite_id,
                    office_id=safe_office_id,
                    risk_tier="green",
                )

                # Search with 2 query variants
                queries = [full_task, f"{full_task} guide requirements 2026"]
                all_results = []
                for q in queries:
                    for pname, executor_fn in _web_search_chain():
                        try:
                            result = await _asyncio.wait_for(
                                executor_fn(payload={"query": q}, **common_kwargs_web),
                                timeout=10.0,
                            )
                            if result.outcome == Outcome.SUCCESS and result.data:
                                for r in result.data.get("results", [])[:5]:
                                    all_results.append({
                                        "title": r.get("title", r.get("name", "")),
                                        "url": r.get("url", ""),
                                        "snippet": r.get("snippet", r.get("description", "")),
                                        "source": pname,
                                    })
                                break
                        except Exception:
                            continue

                # Synthesize
                try:
                    synthesis = await generate_text_async(
                        model="gpt-5.2",
                        messages=[
                            {"role": "developer", "content": "Summarize research findings for a small business owner. Be specific and cite sources. Under 200 words."},
                            {"role": "user", "content": f"Task: {full_task}\n\nSearch results:\n{_json.dumps(all_results[:10], indent=2, default=str)}\n\nSummarize the key findings. Include specific requirements, steps, or answers."},
                        ],
                        api_key=api_key,
                        base_url="https://api.openai.com/v1",
                        timeout_seconds=18.0,
                        max_output_tokens=4096,
                        prefer_responses_api=True,
                    )
                    response_text = synthesis
                except Exception as synth_err:
                    logger.warning("Adam topic synthesis failed: %s", synth_err)
                    if all_results:
                        response_text = f"Found {len(all_results)} results. Top: {all_results[0].get('title', 'Unknown')}."
                    else:
                        response_text = f"No results found for: {full_task}"

                return JSONResponse(
                    status_code=200,
                    content={
                        "success": len(all_results) > 0,
                        "agent": "adam",
                        "result": response_text,
                        "data": {"mode": "topic", "results": all_results[:10], "total": len(all_results)},
                        "receipt_id": ctx.correlation_id,
                    },
                )

            # ── VENDOR MODE (existing v5 pipeline) ──
            # Falls through for vendor and competitive modes

            api_key = _os.environ.get("ASPIRE_OPENAI_API_KEY") or _os.environ.get("OPENAI_API_KEY")
            common_kwargs = dict(
                correlation_id=ctx.correlation_id,
                suite_id=safe_suite_id,
                office_id=safe_office_id,
                risk_tier="green",
            )

            # ── Step 1: Extract location + build query variants (instant, no LLM) ──

            # Extract clean location: "in Atlanta Georgia" → "Atlanta, Georgia"
            clean_location = ""
            _loc_patterns = [
                r'(?:in|near|around|from)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*[,\s]+[A-Z][A-Za-z]+)',
                r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*),?\s+([A-Z]{2})\b',
                r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*),?\s+(Georgia|Texas|Florida|California|New York|Illinois|Ohio|North Carolina|Virginia|Tennessee|Alabama|South Carolina|Louisiana|Maryland|Arizona|Colorado|Oregon|Washington|Michigan|Indiana|Missouri|Wisconsin|Minnesota|Mississippi|Arkansas|Kansas|Utah|Nevada|Nebraska|Idaho|Maine|Montana|Delaware|New Hampshire|Vermont|Wyoming|Hawaii|Alaska|Connecticut|Oklahoma|Kentucky|Iowa|New Mexico|Rhode Island)',
            ]
            for pattern in _loc_patterns:
                m = _re.search(pattern, full_task)
                if m:
                    clean_location = m.group(0).strip().lstrip("in ").lstrip("near ").lstrip("around ").lstrip("from ")
                    break
            logger.info("Adam location: '%s'", clean_location)

            # Extract search subject (remove location and filler words)
            search_subject = full_task
            if clean_location:
                search_subject = full_task.replace(clean_location, "").strip()
            # Remove common filler
            for filler in ["Find ", "find ", "Search for ", "search for ", "Look for ", "look for ",
                           "that buy ", "who buy ", "that sell ", "who sell ",
                           "Additional details:", ". Additional details:"]:
                search_subject = search_subject.replace(filler, "")
            search_subject = _re.sub(r'\s+', ' ', search_subject).strip().rstrip('.')

            # Build query variants — different for web vs places
            loc_str = clean_location or ""

            # Web queries: detailed, include context (Brave/Tavily handle long queries well)
            web_queries = [
                f"{search_subject} {loc_str}".strip(),
                f"{search_subject} near {loc_str}".strip() if loc_str else search_subject,
                f"{search_subject} wholesale supplier {loc_str}".strip(),
            ]

            # Places queries: SIMPLE business type + location (Google/HERE/Foursquare need clean queries)
            # Extract just the core business type (first 4-5 meaningful words)
            _places_subject = search_subject
            # Remove technical specs, details, and extra context
            for noise in [" new ", " brand new ", " GMA ", " 48x40 ", " 48 by 40 ",
                          " style ", " grade ", " that buy ", " who buy ", " who deal ",
                          " who only ", " that only ", " targeting ", " looking to ",
                          " month profit", " month revenue"]:
                _places_subject = _places_subject.replace(noise, " ")
            _places_subject = _re.sub(r'\s+', ' ', _places_subject).strip()
            # Take the first meaningful chunk (usually the business type)
            _places_words = _places_subject.split()[:6]
            _places_query = " ".join(_places_words).strip()

            places_queries = [
                f"{_places_query} {loc_str}".strip(),
            ]
            if _places_query != search_subject:
                places_queries.append(f"{search_subject.split()[0]} {loc_str}".strip() if search_subject else loc_str)

            # Remove dupes
            seen_q: set[str] = set()
            web_queries = [q for q in web_queries if q and q.lower() not in seen_q and not seen_q.add(q.lower())]
            places_queries = [q for q in places_queries if q and q.lower() not in seen_q and not seen_q.add(q.lower())]

            logger.info("Adam web queries: %s", web_queries)
            logger.info("Adam places queries: %s", places_queries)
            # Combined for metadata
            queries = web_queries + places_queries

            # ── Step 2: Geocode location (Mapbox primary → HERE fallback) ──
            coordinates = None
            if clean_location:
                # Try Mapbox first
                for pname, geo_fn in _geocode_chain():
                    try:
                        geo_result = await _asyncio.wait_for(
                            geo_fn(payload={"query": clean_location}, **common_kwargs),
                            timeout=5.0,
                        )
                        if geo_result.outcome == Outcome.SUCCESS and geo_result.data:
                            geo_results = geo_result.data.get("results", [])
                            if geo_results:
                                first = geo_results[0]
                                lat = first.get("lat") or first.get("latitude")
                                lng = first.get("lng") or first.get("longitude")
                                if lat and lng:
                                    coordinates = {"lat": float(lat), "lng": float(lng)}
                                    logger.info("Adam geocoded (mapbox) '%s' → %s", clean_location, coordinates)
                        break
                    except Exception as e:
                        logger.warning("Adam mapbox geocode failed: %s", type(e).__name__)

                # Fallback: HERE geocode if Mapbox failed
                if not coordinates:
                    try:
                        import httpx
                        here_key = _os.environ.get("ASPIRE_HERE_API_KEY", "")
                        if here_key:
                            async with httpx.AsyncClient(timeout=5.0) as hc:
                                hr = await hc.get(
                                    "https://geocode.search.hereapi.com/v1/geocode",
                                    params={"q": clean_location, "apiKey": here_key, "limit": "1"},
                                )
                                if hr.status_code == 200:
                                    items = hr.json().get("items", [])
                                    if items:
                                        pos = items[0].get("position", {})
                                        if pos.get("lat") and pos.get("lng"):
                                            coordinates = {"lat": float(pos["lat"]), "lng": float(pos["lng"])}
                                            logger.info("Adam geocoded (here) '%s' → %s", clean_location, coordinates)
                    except Exception as e:
                        logger.warning("Adam here geocode failed: %s", type(e).__name__)

            coord_str = f"{coordinates['lat']},{coordinates['lng']}" if coordinates else ""

            # ── Step 3: Multi-provider parallel search ──
            async def _safe_search(provider_name: str, executor_fn, payload: dict) -> tuple[str, dict | None]:
                try:
                    result = await _asyncio.wait_for(
                        executor_fn(payload=payload, **common_kwargs),
                        timeout=10.0,
                    )
                    if result.outcome == Outcome.SUCCESS and result.data:
                        return (provider_name, result.data)
                except _asyncio.TimeoutError:
                    logger.debug("Adam %s timed out", provider_name)
                except Exception as e:
                    logger.debug("Adam %s failed: %s", provider_name, type(e).__name__)
                return (provider_name, None)

            search_tasks = []

            # Web: Brave + Tavily × web query variants (detailed, context-rich)
            for query in web_queries:
                for pname, executor_fn in _web_search_chain():
                    search_tasks.append(_safe_search(pname, executor_fn, {"query": query}))

            # Places: EVERY provider fires with CLEAN business-type queries.
            # text location as fallback. Production grade = all providers contribute.
            for pname, executor_fn in _places_search_chain():
                p_payload: dict[str, Any] = {"query": places_queries[0] if places_queries else web_queries[0]}

                if pname == "google_places":
                    if coord_str:
                        p_payload["location"] = coord_str
                        p_payload["radius"] = "32000"
                    # Google Places text search works without coords too

                elif pname == "here":
                    if coord_str:
                        p_payload["at"] = coord_str
                    else:
                        # HERE strictly requires at — skip only if truly no location
                        if not clean_location:
                            continue
                        # We already tried HERE geocode above, if coords still missing skip HERE discover
                        continue

                elif pname == "foursquare":
                    if coord_str:
                        p_payload["ll"] = coord_str
                        p_payload["radius"] = "32000"
                    elif clean_location:
                        # Foursquare supports text-based 'near' param
                        p_payload["near"] = clean_location
                    # Always fires — uses ll OR near

                elif pname == "tomtom":
                    if coordinates:
                        p_payload["lat"] = str(coordinates["lat"])
                        p_payload["lon"] = str(coordinates["lng"])
                    # TomTom works without coords — includes location in query text

                elif pname == "osm_overpass":
                    continue  # 10s+ response blows the 30s Railway timeout

                search_tasks.append(_safe_search(pname, executor_fn, p_payload))

            # Fire all in parallel
            all_results = await _asyncio.gather(*search_tasks)

            # ── Step 4: Merge, dedup, cross-validate, score ──
            web_items_all: list[dict] = []
            places_items_all: list[dict] = []
            providers_used: set[str] = set()
            provider_counts: dict[str, int] = {}

            web_providers = {"brave", "tavily"}
            for pname, data in all_results:
                if data is None:
                    continue
                providers_used.add(pname)
                results_list = data.get("results", [])
                provider_counts[pname] = len(results_list)

                if pname in web_providers:
                    for r in results_list[:5]:
                        web_items_all.append({
                            "name": r.get("title", r.get("name", "")),
                            "url": r.get("url", ""),
                            "snippet": r.get("snippet", r.get("description", "")),
                            "source": pname,
                        })
                else:
                    for r in results_list[:8]:
                        # Normalize fields across providers (Google, HERE, Foursquare, TomTom)
                        name = r.get("name", r.get("title", ""))
                        phone = r.get("phone", "") or r.get("formatted_phone_number", "") or r.get("tel", "")
                        website = r.get("website", "") or r.get("url", "")
                        email = r.get("email", "")
                        address = r.get("address", "") or r.get("formatted_address", "")
                        rating = r.get("rating")
                        categories = r.get("categories", [])
                        place_id = r.get("place_id", r.get("fsq_id", r.get("fsq_place_id", r.get("id", ""))))

                        if not name:
                            continue

                        places_items_all.append({
                            "name": name, "address": address, "rating": rating,
                            "phone": phone, "website": website, "email": email,
                            "categories": categories, "place_id": place_id, "source": pname,
                        })

            # ── Provider priority: Google Places + HERE > Foursquare > TomTom ──
            # Sort so higher-quality providers appear first in dedup (first occurrence wins)
            # NO hardcoded category filtering — what's "noise" depends on the user's trade.
            # A painter looking for commercial leads WANTS gyms, salons, hotels, churches.
            # GPT-5.2 synthesis handles relevance filtering with full task context.
            _PROVIDER_PRIORITY = {"google_places": 0, "here": 1, "foursquare": 2, "tomtom": 3}
            places_items_all.sort(key=lambda x: _PROVIDER_PRIORITY.get(x.get("source", ""), 9))

            # Dedup by name — merge contact data across providers
            seen_names: dict[str, dict] = {}
            for item in places_items_all:
                key = item["name"].lower().strip()
                if not key or len(key) < 3:
                    continue
                if key in seen_names:
                    seen_names[key]["sources"].add(item["source"])
                    for field in ("rating", "address", "phone", "website", "email", "place_id"):
                        if item.get(field) and not seen_names[key].get(field):
                            seen_names[key][field] = item[field]
                    existing_cats = set(seen_names[key].get("categories", []))
                    for cat in item.get("categories", []):
                        if cat and cat not in existing_cats:
                            seen_names[key].setdefault("categories", []).append(cat)
                            existing_cats.add(cat)
                else:
                    seen_names[key] = {**item, "sources": {item["source"]}}

            # Cross-validate with web results + enrich
            for key, item in seen_names.items():
                for web in web_items_all:
                    web_text = (web.get("name", "") + " " + web.get("snippet", "")).lower()
                    if key in web_text or any(word in web_text for word in key.split() if len(word) > 4):
                        item["sources"].add("web_mention")
                        if not item.get("website") and web.get("url"):
                            item["website"] = web["url"]
                        if not item.get("email") and web.get("snippet"):
                            em = _re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', web["snippet"])
                            if em:
                                item["email"] = em.group(0)
                        break

            # ── Confidence scoring: provider quality + contact completeness ──
            _PROVIDER_WEIGHT = {
                "google_places": 3,  # Verified businesses with ratings
                "here": 3,          # Rich contact data (phone + email + website)
                "foursquare": 1,    # Venue-heavy, often noisy
                "tomtom": 2,        # Good commercial/industrial data
                "web_mention": 2,   # Cross-validated in web search
            }

            for item in seen_names.values():
                sources = item["sources"]
                # Provider quality score (weighted sum, not just count)
                provider_score = sum(_PROVIDER_WEIGHT.get(s, 1) for s in sources)

                # Contact completeness score (0-5)
                contact_score = sum([
                    2 if item.get("phone") else 0,
                    1 if item.get("email") else 0,
                    1 if item.get("website") else 0,
                    1 if item.get("address") else 0,
                ])

                # Rating bonus
                rating_score = 1 if item.get("rating") else 0

                # Total quality score
                total_score = provider_score + contact_score + rating_score
                item["_quality_score"] = total_score

                # Confidence label based on total score
                if total_score >= 8:
                    item["confidence"] = "high"
                elif total_score >= 5:
                    item["confidence"] = "medium"
                else:
                    item["confidence"] = "low"

                item["sources"] = list(sources)

            # Dedup web by URL
            seen_urls: set[str] = set()
            deduped_web = [w for w in web_items_all if w.get("url") and w["url"] not in seen_urls and not seen_urls.add(w["url"])]

            # Sort by quality score (highest first) — captures provider quality + contact completeness + rating
            sorted_places = sorted(
                seen_names.values(),
                key=lambda x: -x.get("_quality_score", 0),
            )

            raw_findings = {
                "web_results": deduped_web[:10],
                "places_results": [{k: v for k, v in p.items()} for p in sorted_places[:10]],
                "providers_used": sorted(providers_used),
                "provider_counts": provider_counts,
                "queries_used": queries,
                "geocoded": coordinates,
                "location_extracted": clean_location,
                "total_raw_results": len(web_items_all) + len(places_items_all),
                "total_deduped": len(deduped_web) + len(sorted_places),
            }

            # ── Step 5: GPT-5.2 synthesis ──
            try:
                synthesis_prompt = (
                    f"You are Adam, the Research Specialist at Aspire.\n"
                    f"Task: {full_task}\n"
                    f"Location: {clean_location or 'not specified'}\n\n"
                    f"Searched {len(providers_used)} providers ({', '.join(sorted(providers_used))}) "
                    f"with {len(queries)} queries. {raw_findings['total_deduped']} unique results.\n\n"
                    f"Top places (with contact data + confidence):\n{_json.dumps(sorted_places[:8], indent=2, default=str)}\n\n"
                    f"Top web results:\n{_json.dumps(deduped_web[:5], indent=2, default=str)}\n\n"
                    f"Synthesize for a small business owner:\n"
                    f"- Pick the top 3-5 results that are MOST RELEVANT to the user's actual task\n"
                    f"- A painter looking for commercial leads WANTS gyms, salons, hotels, churches — those are customers\n"
                    f"- A pallet company looking for buyers does NOT want restaurants or parks\n"
                    f"- Think about WHO the user is and WHAT they need before deciding relevance\n"
                    f"- For each relevant result: name, full address, phone, email, website\n"
                    f"- Confidence level per result\n"
                    f"- Skip results that don't match the user's specific need — explain why briefly\n"
                    f"- Flag gaps: missing phone, no email, unverified\n"
                    f"- Concrete next step: who to call first and what to say\n"
                    f"Under 150 words. No markdown. Natural speech."
                )

                synthesis = await generate_text_async(
                    model="gpt-5.2",
                    messages=[
                        {"role": "developer", "content": "You are Adam, Aspire's research specialist. Be specific — name businesses, include phone numbers and emails. Filter out irrelevant results (restaurants, parks, stadiums). Flag uncertainty honestly."},
                        {"role": "user", "content": synthesis_prompt},
                    ],
                    api_key=api_key,
                    base_url="https://api.openai.com/v1",
                    timeout_seconds=18.0,
                    max_output_tokens=4096,
                    prefer_responses_api=True,
                )
                response_text = synthesis
            except Exception as synth_err:
                logger.warning("Adam synthesis failed: %s", synth_err)
                relevant = [p for p in sorted_places if p.get("confidence") != "low"][:3]
                if not relevant:
                    relevant = sorted_places[:3]
                if relevant:
                    parts = []
                    for p in relevant:
                        part = p["name"]
                        if p.get("phone"):
                            part += f", {p['phone']}"
                        parts.append(part)
                    response_text = f"Found {len(sorted_places)} businesses. Top matches: {'; '.join(parts)}."
                elif deduped_web:
                    names = [w.get("name", "Unknown") for w in deduped_web[:3]]
                    response_text = f"Found {len(deduped_web)} web results. Top: {', '.join(names)}."
                else:
                    response_text = f"No results found for: {task}. Try a different location or broader search terms."

            return JSONResponse(
                status_code=200,
                content={
                    "success": len(providers_used) > 0,
                    "agent": "adam",
                    "result": response_text,
                    "data": raw_findings,
                    "receipt_id": ctx.correlation_id,
                    "error": None if providers_used else "All search providers failed",
                },
            )

        # ── Quinn: Direct Stripe pipeline (v2 — real Stripe calls) ──
        # Parse intent → verify math → Stripe customer resolve → Stripe draft invoice → authority queue
        if agent == "quinn":
            import json as _json
            import os as _os
            from aspire_orchestrator.services.openai_client import generate_text_async
            from aspire_orchestrator.skillpacks.quinn_invoicing import (
                QuinnInvoicingSkillPack, QuinnContext,
            )

            api_key = _os.environ.get("ASPIRE_OPENAI_API_KEY") or _os.environ.get("OPENAI_API_KEY")

            # Step 1: GPT-5.2 parses invoice intent from natural language
            try:
                parse_prompt = (
                    "You are Quinn, an invoicing specialist. Extract structured invoice data from the user's request.\n\n"
                    f"Request: {full_task}\n\n"
                    "Return ONLY a JSON object with these fields:\n"
                    '{\n'
                    '  "customer_name": "string — business or person name",\n'
                    '  "customer_email": "string or null — email if mentioned",\n'
                    '  "customer_first_name": "string or null",\n'
                    '  "customer_last_name": "string or null",\n'
                    '  "customer_company": "string or null — business/company name if different from person name",\n'
                    '  "customer_phone": "string or null",\n'
                    '  "customer_address": "string or null — billing address only",\n'
                    '  "line_items": [{"description": "string — the SERVICE or PRODUCT being billed, NOT the address", "quantity": number, "unit_price_cents": number}],\n'
                    '  "total_cents": number — total in cents (e.g., 475000 for $4,750.00),\n'
                    '  "due_days": number — days until due. Use 0 for "immediately", "due now", or "upon receipt". Default 30 if not specified.,\n'
                    '  "currency": "usd",\n'
                    '  "notes": "string or empty",\n'
                    '  "is_quote": false\n'
                    '}\n\n'
                    "IMPORTANT:\n"
                    "- line_items description must be the SERVICE or PRODUCT (e.g. 'Painting job', 'Roofing repair', '500 GMA pallets'). NEVER put an address in the description.\n"
                    "- customer_address is a SEPARATE field — do not mix it with line item descriptions.\n"
                    "- 'due immediately' or 'due now' or 'upon receipt' = due_days: 0\n"
                    "- Convert dollars to cents (9.50 = 950 cents, 4500 = 450000 cents).\n"
                    "- If any required field is unclear, set it to null."
                )

                parse_resp = await generate_text_async(
                    model="gpt-5.2",
                    messages=[
                        {"role": "developer", "content": "Extract invoice data. Return ONLY valid JSON. No explanation."},
                        {"role": "user", "content": parse_prompt},
                    ],
                    api_key=api_key,
                    base_url="https://api.openai.com/v1",
                    timeout_seconds=15.0,
                    max_output_tokens=4096,
                    prefer_responses_api=True,
                )

                # Clean and parse JSON
                cleaned = parse_resp.strip()
                if cleaned.startswith("```"):
                    cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                    cleaned = cleaned.rsplit("```", 1)[0]
                invoice_data = _json.loads(cleaned.strip())

            except Exception as parse_err:
                logger.warning("Quinn parse failed: %s", parse_err)
                return JSONResponse(status_code=200, content={
                    "success": False, "agent": "quinn",
                    "result": "I couldn't understand the invoice details. Can you tell me who it's for, what you're billing, and how much?",
                    "error": str(parse_err),
                })

            customer_name = invoice_data.get("customer_name", "")
            customer_email = invoice_data.get("customer_email")
            line_items = invoice_data.get("line_items", [])
            total_cents = invoice_data.get("total_cents", 0)
            due_days = invoice_data.get("due_days", 30)
            currency = invoice_data.get("currency", "usd")
            notes = invoice_data.get("notes", "")
            is_quote = invoice_data.get("is_quote", False)

            if not customer_name:
                return JSONResponse(status_code=200, content={
                    "success": False, "agent": "quinn",
                    "result": "I need a customer name for this invoice. Who are you billing?",
                })

            # ── Customer lookup mode: just a name, no line items yet ──
            # Ava calls Quinn early with just the customer name to check Stripe
            if not line_items or not total_cents:
                # Search Stripe for the customer by name
                from aspire_orchestrator.providers.stripe_client import _get_client as _get_stripe
                stripe_client = _get_stripe()
                try:
                    search_resp = await stripe_client._request(
                        __import__("aspire_orchestrator.providers.base_client", fromlist=["ProviderRequest"]).ProviderRequest(
                            method="GET",
                            path=f"/customers/search?query=name%3A%27{customer_name}%27&limit=3",
                            body={},
                            correlation_id=ctx.correlation_id,
                            suite_id=safe_suite_id,
                            office_id=safe_office_id,
                        )
                    )
                    found_customers = []
                    if search_resp.success:
                        found_customers = search_resp.body.get("data", [])
                except Exception as stripe_err:
                    logger.warning("Quinn Stripe customer search failed: %s", stripe_err)
                    found_customers = []

                if found_customers:
                    cust = found_customers[0]
                    cust_name = cust.get("name", customer_name)
                    cust_email = cust.get("email", "")
                    return JSONResponse(status_code=200, content={
                        "success": True,
                        "agent": "quinn",
                        "result": f"{cust_name} is in the system. Email on file is {cust_email}. Go ahead — what are you billing them for and how much?",
                        "data": {
                            "status": "customer_found",
                            "customer_name": cust_name,
                            "customer_id": cust.get("id"),
                            "customer_email": cust_email,
                        },
                    })
                else:
                    return JSONResponse(status_code=200, content={
                        "success": True,
                        "agent": "quinn",
                        "result": (
                            f"{customer_name} isn't in the system yet. One-time setup — what's their first name, last name, and email? Company, phone, and address are optional."
                        ),
                        "data": {
                            "status": "needs_onboarding",
                            "customer_name": customer_name,
                        },
                    })

            # Step 2: Verify math
            computed_total = sum(
                item.get("quantity", 0) * item.get("unit_price_cents", 0)
                for item in line_items
            )
            if total_cents and abs(computed_total - total_cents) > 100:  # Allow $1 rounding
                # Math mismatch — ask user to clarify
                computed_dollars = computed_total / 100
                stated_dollars = total_cents / 100
                return JSONResponse(status_code=200, content={
                    "success": False, "agent": "quinn",
                    "result": f"The math doesn't add up. Items total {computed_dollars:.0f} dollars but you said {stated_dollars:.0f}. Which one's right?",
                })

            if not total_cents:
                total_cents = computed_total

            # Step 3: Format amounts for voice
            total_dollars = total_cents / 100
            items_summary = []
            for item in line_items:
                qty = item.get("quantity", 0)
                price = item.get("unit_price_cents", 0) / 100
                desc = item.get("description", "item")
                items_summary.append(f"{qty} {desc} at {price:.2f} each")

            # Auto-create tenant if needed (prevents FK violation on authority queue write)
            try:
                from aspire_orchestrator.services.supabase_client import supabase_upsert as _su
                await _su("tenants", {"tenant_id": safe_suite_id, "name": customer_name or "Auto-created"}, on_conflict="tenant_id")
            except Exception:
                pass

            # Step 4: Check if we have email — required for Stripe
            if not customer_email:
                # No email — can't create Stripe customer. Tell Ava to ask for onboarding info.
                return JSONResponse(
                    status_code=200,
                    content={
                        "success": True,
                        "agent": "quinn",
                        "result": (
                            f"{customer_name} isn't in your system yet. "
                            f"It's a one-time setup — I just need their first name, last name, and email. "
                            f"Company, phone, and billing address are nice to have but we can skip them if you don't have them."
                        ),
                        "data": {
                            "status": "needs_onboarding",
                            "customer_name": customer_name,
                            "line_items": line_items,
                            "total_cents": total_cents,
                            "total_dollars": total_dollars,
                            "due_days": due_days,
                        },
                    },
                )

            # Step 5: Create REAL draft invoice in Stripe
            from aspire_orchestrator.providers.stripe_client import execute_stripe_invoice_create

            # Generate premium invoice number: INV-YYYYMMDD-XXXX
            import random as _random
            _inv_date = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y%m%d")
            _inv_seq = _random.randint(1000, 9999)
            invoice_number = f"INV-{_inv_date}-{_inv_seq}" if not is_quote else f"QTE-{_inv_date}-{_inv_seq}"

            # Build line item description from parsed data (NOT address)
            line_item_desc = ", ".join(items_summary) if items_summary else notes or "Services rendered"

            stripe_result = await execute_stripe_invoice_create(
                payload={
                    "customer_email": customer_email,
                    "customer_name": customer_name,
                    "customer_first_name": invoice_data.get("customer_first_name"),
                    "customer_last_name": invoice_data.get("customer_last_name"),
                    "customer_business_name": invoice_data.get("customer_company"),
                    "customer_phone": invoice_data.get("customer_phone"),
                    "customer_address": invoice_data.get("customer_address"),
                    "amount_cents": total_cents,
                    "currency": currency,
                    "description": line_item_desc,
                    "due_days": due_days,
                    "invoice_number": invoice_number,
                    "metadata": {"aspire_correlation": ctx.correlation_id},
                },
                correlation_id=ctx.correlation_id,
                suite_id=safe_suite_id,
                office_id=safe_office_id,
                risk_tier="yellow",
            )

            if stripe_result.outcome != __import__("aspire_orchestrator.models", fromlist=["Outcome"]).Outcome.SUCCESS:
                # Stripe call failed — tell Ava
                error_msg = stripe_result.error or "Stripe returned an error"
                logger.error("Quinn Stripe invoice creation failed: %s", error_msg)
                return JSONResponse(
                    status_code=200,
                    content={
                        "success": False,
                        "agent": "quinn",
                        "result": f"I ran into an issue creating the invoice in Stripe. {error_msg}. Want me to try again?",
                        "error": error_msg,
                    },
                )

            # Extract Stripe data
            stripe_data = stripe_result.data or {}
            stripe_invoice_id = stripe_data.get("invoice_id", "")
            hosted_invoice_url = stripe_data.get("hosted_invoice_url", "")
            invoice_pdf = stripe_data.get("invoice_pdf", "")

            # Step 6: Submit to authority queue WITH real Stripe data
            approval_id = None
            try:
                from aspire_orchestrator.services.supabase_client import supabase_upsert
                import hashlib as _hashlib

                _approval_id = f"appr-quinn-{_uuid.uuid4().hex[:12]}"
                payload = {
                    "customer_name": customer_name,
                    "customer_email": customer_email,
                    "line_items": line_items,
                    "total_cents": total_cents,
                    "total_dollars": total_dollars,
                    "due_days": due_days,
                    "currency": currency,
                    "notes": notes,
                    "is_quote": is_quote,
                    "stripe_invoice_id": stripe_invoice_id,
                    "hosted_invoice_url": hosted_invoice_url,
                    "invoice_pdf": invoice_pdf,
                }
                payload_json = _json.dumps(payload, default=str)

                await supabase_upsert(
                    "approval_requests",
                    {
                        "approval_id": _approval_id,
                        "tenant_id": safe_suite_id,
                        "run_id": ctx.correlation_id,
                        "orchestrator": "invoke-sync",
                        "tool": "invoke_quinn",
                        "operation": "quote.create" if is_quote else "invoice.create",
                        "resource_type": "quote" if is_quote else "invoice",
                        "resource_id": stripe_invoice_id,
                        "risk_tier": "yellow",
                        "policy_version": "v1",
                        "approval_hash": _hashlib.sha256(payload_json.encode()).hexdigest(),
                        "payload_redacted": payload,
                        "constraints": {"max_amount": total_cents + 10000, "currency": currency},
                        "status": "pending",
                        "expires_at": (
                            __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                            + __import__("datetime").timedelta(hours=24)
                        ).isoformat(),
                        "draft_summary": f"{'Quote' if is_quote else 'Invoice'} for {customer_name} — {', '.join(items_summary)}, {total_dollars:.0f} dollars total, due in {due_days} days",
                        "assigned_agent": "quinn",
                        "execution_payload": payload,
                    },
                    on_conflict="approval_id",
                )
                approval_id = _approval_id
                logger.info("Quinn submitted to authority queue: %s (stripe: %s)", _approval_id, stripe_invoice_id)
            except Exception as aq_err:
                logger.warning("Quinn authority queue write failed: %s", aq_err)

            # Step 7: Build voice-friendly response for Ava
            doc_type = "quote" if is_quote else "invoice"
            response_text = (
                f"Alright, I've drafted that {doc_type} for {customer_name}. "
                f"{', '.join(items_summary)} — {total_dollars:.0f} dollars total, due in {due_days} days. "
                f"It's in your approval queue with a preview. Go check it out when you get a chance."
            )

            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "agent": "quinn",
                    "result": response_text,
                    "data": {
                        "customer_name": customer_name,
                        "customer_email": customer_email,
                        "line_items": line_items,
                        "total_cents": total_cents,
                        "total_dollars": total_dollars,
                        "due_days": due_days,
                        "currency": currency,
                        "is_quote": is_quote,
                        "stripe_invoice_id": stripe_invoice_id,
                        "hosted_invoice_url": hosted_invoice_url,
                        "invoice_pdf": invoice_pdf,
                        "approval_id": approval_id,
                        "approval_required": True,
                    },
                    "receipt_id": stripe_result.receipt_data.get("receipt_id") if stripe_result.receipt_data else None,
                    "approval_id": approval_id,
                },
            )

        # ── Tec: Use agentic loop ──
        result = await skill_pack.run_agentic_loop(
            task=full_task,
            ctx=ctx,
            max_steps=5,
            timeout_s=55,  # 55s — leaves buffer within 60s LLM timeout
        )

        # Extract the conversational response from the agent result.
        response_text = (
            result.data.get("response")
            or result.data.get("content")
            or result.data.get("summary")
            or result.data.get("plan", {}).get("summary") if isinstance(result.data.get("plan"), dict) else None
            or ""
        )

        # If Quinn drafted an invoice/quote, submit to authority queue for user approval
        approval_id = None
        if agent == "quinn" and result.success:
            draft_data = result.data
            if any(k in str(draft_data).lower() for k in ["invoice", "quote", "drafted"]):
                try:
                    from aspire_orchestrator.services.supabase_client import supabase_upsert
                    import hashlib

                    _approval_id = f"appr-{agent}-{_uuid.uuid4().hex[:12]}"
                    payload = {
                        "agent": agent,
                        "task": full_task,
                        "result": draft_data,
                    }
                    payload_json = __import__("json").dumps(payload, default=str)

                    await supabase_upsert(
                        "approval_requests",
                        {
                            "approval_id": _approval_id,
                            "tenant_id": safe_suite_id,
                            "run_id": ctx.correlation_id,
                            "orchestrator": "invoke-sync",
                            "tool": f"invoke_{agent}",
                            "operation": "create_invoice",
                            "resource_type": "invoice",
                            "risk_tier": "yellow",
                            "policy_version": "v1",
                            "approval_hash": hashlib.sha256(payload_json.encode()).hexdigest(),
                            "payload_redacted": payload,
                            "constraints": {"max_amount": 50000, "currency": "usd"},
                            "status": "pending",
                            "expires_at": (__import__("datetime").datetime.now(__import__("datetime").timezone.utc) + __import__("datetime").timedelta(hours=24)).isoformat(),
                            "draft_summary": response_text[:500] if response_text else full_task,
                            "assigned_agent": agent,
                        },
                        conflict_columns=["approval_id"],
                    )
                    approval_id = _approval_id
                    logger.info("Submitted to authority queue: %s", _approval_id)
                except Exception as aq_err:
                    logger.warning("Authority queue write failed (non-fatal): %s", aq_err)

        return JSONResponse(
            status_code=200,
            content={
                "success": result.success,
                "agent": agent,
                "result": response_text,
                "data": result.data,
                "receipt_id": result.receipt.get("id", result.receipt.get("receipt_id")) if result.receipt else None,
                "approval_id": approval_id,
                "error": result.error,
            },
        )

    except Exception as e:
        logger.error("invoke-sync error for %s: %s", agent, e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "agent": agent,
                "result": f"I was not able to reach {agent} right now. Please try again.",
                "error": str(e),
            },
        )


@app.post("/v1/a2a/claim")
async def a2a_claim(request: Request) -> JSONResponse:
    """Claim available tasks for an agent.

    Agents call this to pull work from the queue.
    Tenant-scoped (Law #6).
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "SCHEMA_VALIDATION_FAILED", "message": "Invalid JSON body"},
        )

    agent_id = body.get("agent_id")
    suite_id = body.get("suite_id")

    if not agent_id or not suite_id:
        return JSONResponse(
            status_code=400,
            content={
                "error": "SCHEMA_VALIDATION_FAILED",
                "message": "Required: agent_id, suite_id",
            },
        )

    a2a = get_a2a_service()
    result = a2a.claim(
        agent_id=agent_id,
        suite_id=suite_id,
        task_types=body.get("task_types"),
        max_tasks=body.get("max_tasks", 1),
        lease_seconds=body.get("lease_seconds"),
    )

    if result.receipt_data:
        store_receipts([result.receipt_data])

    task_data = None
    if result.task:
        task_data = {
            "task_id": result.task.task_id,
            "task_type": result.task.task_type,
            "payload": result.task.payload,
            "priority": result.task.priority,
            "correlation_id": result.task.correlation_id,
            "attempt_count": result.task.attempt_count,
            "lease_expires_at": result.task.lease_expires_at,
        }

    return JSONResponse(
        status_code=200 if result.success else 404,
        content={
            "success": result.success,
            "task": task_data,
            "error": result.error,
            "receipt_id": result.receipt_data.get("id") if result.receipt_data else None,
        },
    )


@app.post("/v1/a2a/complete")
async def a2a_complete(request: Request) -> JSONResponse:
    """Mark a claimed task as completed."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "SCHEMA_VALIDATION_FAILED", "message": "Invalid JSON body"},
        )

    task_id = body.get("task_id")
    agent_id = body.get("agent_id")
    suite_id = body.get("suite_id")

    if not all([task_id, agent_id, suite_id]):
        return JSONResponse(
            status_code=400,
            content={
                "error": "SCHEMA_VALIDATION_FAILED",
                "message": "Required: task_id, agent_id, suite_id",
            },
        )

    a2a = get_a2a_service()
    result = a2a.complete(
        task_id=task_id,
        agent_id=agent_id,
        suite_id=suite_id,
        result=body.get("result"),
    )

    if result.receipt_data:
        store_receipts([result.receipt_data])

    status_code = 200
    if not result.success:
        status_code = 403 if result.error == "TENANT_ISOLATION_VIOLATION" else 400

    return JSONResponse(
        status_code=status_code,
        content={
            "success": result.success,
            "task_id": result.task_id,
            "new_status": result.new_status.value if result.new_status else None,
            "error": result.error,
            "receipt_id": result.receipt_data.get("id") if result.receipt_data else None,
        },
    )


@app.post("/v1/a2a/fail")
async def a2a_fail(request: Request) -> JSONResponse:
    """Mark a claimed task as failed (may requeue or quarantine)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "SCHEMA_VALIDATION_FAILED", "message": "Invalid JSON body"},
        )

    task_id = body.get("task_id")
    agent_id = body.get("agent_id")
    suite_id = body.get("suite_id")
    error = body.get("error")

    if not all([task_id, agent_id, suite_id, error]):
        return JSONResponse(
            status_code=400,
            content={
                "error": "SCHEMA_VALIDATION_FAILED",
                "message": "Required: task_id, agent_id, suite_id, error",
            },
        )

    a2a = get_a2a_service()
    result = a2a.fail(
        task_id=task_id,
        agent_id=agent_id,
        suite_id=suite_id,
        error=error,
    )

    if result.receipt_data:
        store_receipts([result.receipt_data])

    status_code = 200
    if not result.success:
        status_code = 403 if result.error == "TENANT_ISOLATION_VIOLATION" else 400

    return JSONResponse(
        status_code=status_code,
        content={
            "success": result.success,
            "task_id": result.task_id,
            "new_status": result.new_status.value if result.new_status else None,
            "error": result.error,
            "receipt_id": result.receipt_data.get("id") if result.receipt_data else None,
        },
    )


@app.post("/v1/resume/{approval_id}")
async def resume_execution(approval_id: str, request: Request) -> JSONResponse:
    """Resume execution of an approved draft operation."""
    import re

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    # Validate UUID format
    if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', approval_id, re.I):
        return JSONResponse(status_code=400, content={"error": "INVALID_ID", "message": "Invalid approval ID format"})

    # Auth from headers (same pattern as /v1/intents — fail-closed on missing)
    suite_id = request.headers.get("x-suite-id", "")
    office_id = request.headers.get("x-office-id", "")
    actor_id = request.headers.get("x-actor-id", "")
    correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())

    if not suite_id or not actor_id:
        missing = []
        if not suite_id:
            missing.append("x-suite-id")
        if not actor_id:
            missing.append("x-actor-id")
        # Law #2: emit denial receipt before 401
        from aspire_orchestrator.services.receipt_store import store_receipts as _store
        _store([{
            "id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "suite_id": suite_id or "unknown",
            "office_id": office_id or "unknown",
            "actor_type": "system",
            "actor_id": "fail_closed_guard",
            "action_type": "resume.execute",
            "risk_tier": "yellow",
            "tool_used": "orchestrator.resume",
            "outcome": "DENIED",
            "reason_code": "AUTH_REQUIRED",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "receipt_type": "auth_denial",
            "receipt_hash": "",
        }])
        return JSONResponse(status_code=401, content={
            "error": "AUTH_REQUIRED",
            "message": f"Missing required headers: {', '.join(missing)}",
            "correlation_id": correlation_id,
        })

    try:
        from aspire_orchestrator.nodes.resume import resume_after_approval
        result = await resume_after_approval(
            approval_id,
            suite_id,
            office_id,
            actor_id,
            presence_token=body.get("presence_token"),
            resume_runner=resume_orchestrator_graph,
        )

        if result.get("success"):
            return JSONResponse(content={
                "narration": result["narration_text"],
                "receipt_id": result["receipt_id"],
                "data": result.get("execution_result"),
            })
        else:
            status = 403 if result.get("error_code") in ("TENANT_ISOLATION_VIOLATION",) else 400
            return JSONResponse(status_code=status, content={
                "error": result["error_code"],
                "message": result["error_message"],
                "receipt_id": result.get("receipt_id"),
            })

    except Exception as e:
        logger.exception("Resume endpoint failed: %s", e)
        return JSONResponse(status_code=500, content={"error": "INTERNAL_ERROR", "message": str(e)})


@app.post("/v1/void-invoice/{invoice_id}")
async def void_invoice_endpoint(invoice_id: str, request: Request) -> JSONResponse:
    """Void a finalized invoice — called when user denies in Authority Queue.

    Since invoice.create auto-finalizes to generate preview URLs, denial must
    void the invoice in Stripe so it doesn't sit as 'open' forever.
    """
    import re

    if not re.match(r'^in_[a-zA-Z0-9]+$', invoice_id):
        return JSONResponse(status_code=400, content={"error": "INVALID_ID", "message": "Invalid invoice ID format"})

    suite_id = request.headers.get("x-suite-id", "")
    correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())

    if not suite_id:
        return JSONResponse(status_code=401, content={"error": "AUTH_REQUIRED", "message": "Missing x-suite-id header"})

    try:
        from aspire_orchestrator.providers.stripe_client import execute_stripe_invoice_void

        result = await execute_stripe_invoice_void(
            payload={"invoice_id": invoice_id},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=request.headers.get("x-office-id", ""),
            risk_tier="yellow",
        )

        if result.outcome.value == "success":
            return JSONResponse(content={
                "status": "voided",
                "invoice_id": invoice_id,
            })
        else:
            return JSONResponse(status_code=400, content={
                "error": "VOID_FAILED",
                "message": result.error or "Failed to void invoice",
            })
    except Exception as e:
        logger.exception("Void invoice failed: %s", e)
        return JSONResponse(status_code=500, content={"error": "INTERNAL_ERROR", "message": str(e)})


@app.post("/v1/cancel-quote/{quote_id}")
async def cancel_quote_endpoint(quote_id: str, request: Request) -> JSONResponse:
    """Cancel a finalized quote — called when user denies in Authority Queue.

    Since quote.create auto-finalizes, denial must cancel the quote in Stripe
    so it doesn't sit as 'open' forever.
    """
    import re

    if not re.match(r'^qt_[a-zA-Z0-9]+$', quote_id):
        return JSONResponse(status_code=400, content={"error": "INVALID_ID", "message": "Invalid quote ID format"})

    suite_id = request.headers.get("x-suite-id", "")
    correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())

    if not suite_id:
        return JSONResponse(status_code=401, content={"error": "AUTH_REQUIRED", "message": "Missing x-suite-id header"})

    try:
        from aspire_orchestrator.providers.stripe_client import execute_stripe_quote_cancel

        result = await execute_stripe_quote_cancel(
            payload={"quote_id": quote_id},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=request.headers.get("x-office-id", ""),
            risk_tier="yellow",
        )

        if result.outcome.value == "success":
            return JSONResponse(content={
                "status": "canceled",
                "quote_id": quote_id,
            })
        else:
            return JSONResponse(status_code=400, content={
                "error": "CANCEL_FAILED",
                "message": result.error or "Failed to cancel quote",
            })
    except Exception as e:
        logger.exception("Cancel quote failed: %s", e)
        return JSONResponse(status_code=500, content={"error": "INTERNAL_ERROR", "message": str(e)})


@app.get("/v1/a2a/tasks")
async def a2a_list_tasks(
    suite_id: str = Query(..., description="Suite ID (from auth)"),
    status: str | None = Query(None),
    assigned_to_agent: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> JSONResponse:
    """List A2A tasks for a suite with optional filters."""
    task_status = None
    if status:
        try:
            task_status = A2ATaskStatus(status)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "SCHEMA_VALIDATION_FAILED",
                    "message": f"Invalid status: {status}. Valid: {[s.value for s in A2ATaskStatus]}",
                },
            )

    a2a = get_a2a_service()
    tasks = a2a.list_tasks(
        suite_id=suite_id,
        status=task_status,
        assigned_to_agent=assigned_to_agent,
        limit=limit,
    )

    return JSONResponse(
        status_code=200,
        content={
            "tasks": [
                {
                    "task_id": t.task_id,
                    "task_type": t.task_type,
                    "assigned_to_agent": t.assigned_to_agent,
                    "status": t.status.value,
                    "priority": t.priority,
                    "correlation_id": t.correlation_id,
                    "created_at": t.created_at,
                    "claimed_by": t.claimed_by,
                    "claimed_at": t.claimed_at,
                    "attempt_count": t.attempt_count,
                }
                for t in tasks
            ],
            "count": len(tasks),
            "suite_id": suite_id,
        },
    )


# =============================================================================
# Client Event Ingestion (Wave 4I — F7 fix)
# =============================================================================

# Rate limit tracker: suite_id -> list of timestamps (within 60s window)
_client_event_counts: dict[str, list[float]] = {}

_VALID_SEVERITIES = {"debug", "info", "warning", "error", "critical"}
_RATE_LIMIT_MAX = 10
_RATE_LIMIT_WINDOW = 60.0  # seconds


def _redact_pii(text: str) -> str:
    """Redact PII from text (Law #9).

    Handles: SSN, credit card numbers, email addresses, phone numbers.
    """
    # SSN: 123-45-6789
    text = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', '<SSN_REDACTED>', text)
    # Credit card: 13-19 digit sequences
    text = re.sub(r'\b\d{13,19}\b', '<CC_REDACTED>', text)
    # Email addresses
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '<EMAIL_REDACTED>', text)
    # Phone numbers: various formats (555-123-4567, (555) 123-4567, 5551234567)
    text = re.sub(r'(?:\+?1[-.\s]?)?(?:\(\d{3}\)\s?|\b\d{3}[-.\s])\d{3}[-.\s]?\d{4}\b', '<PHONE_REDACTED>', text)
    return text


@app.post("/v1/client/events")
async def client_event_ingest(request: Request) -> JSONResponse:
    """Accept client-side error/event reports (Wave 4I — F7 fix).

    Allows the frontend to report errors, blank screens, and UI failures
    so the backend has visibility into client-side problems.

    Rate limited to 10 events/min per suite (Law #3 — prevent abuse).
    PII redacted from messages (Law #9).
    """
    # Parse JSON body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "INVALID_JSON", "message": "Request body must be valid JSON"},
        )

    # Validate event_type (required)
    event_type = body.get("event_type")
    if not event_type:
        return JSONResponse(
            status_code=400,
            content={"error": "MISSING_EVENT_TYPE", "message": "event_type is required"},
        )

    # Resolve suite_id from body or header
    suite_id = body.get("suite_id") or request.headers.get("x-suite-id", "")
    if not suite_id:
        return JSONResponse(
            status_code=400,
            content={"error": "MISSING_SUITE_ID", "message": "suite_id is required (body or X-Suite-Id header)"},
        )

    # Validate suite_id format (Law #3 — reject injection payloads)
    # Accepts: UUID format OR premium display_id (STE-XXX)
    _SUITE_ID_RE = re.compile(
        r'^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|STE-\d{1,6})$',
        re.IGNORECASE,
    )
    if not _SUITE_ID_RE.match(suite_id):
        return JSONResponse(
            status_code=400,
            content={"error": "INVALID_SUITE_ID", "message": "suite_id must be a valid UUID or STE-XXX display ID"},
        )

    # Validate severity if provided
    severity = body.get("severity", "info")
    if severity not in _VALID_SEVERITIES:
        return JSONResponse(
            status_code=400,
            content={
                "error": "INVALID_SEVERITY",
                "message": f"severity must be one of: {sorted(_VALID_SEVERITIES)}",
            },
        )

    # Rate limiting: 10 events/min per suite
    now = time.time()
    timestamps = _client_event_counts.setdefault(suite_id, [])
    # Prune old timestamps outside window
    timestamps[:] = [ts for ts in timestamps if now - ts < _RATE_LIMIT_WINDOW]
    if len(timestamps) >= _RATE_LIMIT_MAX:
        return JSONResponse(
            status_code=429,
            content={"error": "RATE_LIMITED", "message": "Maximum 10 events per minute per suite"},
        )
    timestamps.append(now)

    # Redact PII from message and truncate
    message = body.get("message", "")
    message = _redact_pii(message)
    if len(message) > 2000:
        message = message[:2000]

    # Cap metadata size (10KB)
    metadata = body.get("metadata")
    if metadata:
        try:
            meta_str = json.dumps(metadata)
            if len(meta_str) > 10240:
                metadata = {"_truncated": True, "original_size": len(meta_str)}
        except (TypeError, ValueError):
            metadata = None

    event_id = str(uuid.uuid4())
    correlation_id = request.headers.get("x-correlation-id", str(uuid.uuid4()))

    logger.info(
        "Client event: type=%s suite=%s severity=%s corr=%s",
        event_type, suite_id[:8], severity, correlation_id[:8],
    )

    # Law #2: Receipt for client event ingestion
    try:
        from aspire_orchestrator.services.receipt_store import store_receipts

        receipt = {
            "id": event_id,
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": "",
            "actor_type": "client",
            "actor_id": suite_id,
            "action_type": f"client.event.{event_type}",
            "risk_tier": "green",
            "tool_used": "",
            "capability_token_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "outcome": "success",
            "reason_code": "INGESTED",
            "receipt_type": "telemetry",
            "receipt_hash": "",
            "action": {"event_type": event_type, "severity": severity},
            "result": {"event_id": event_id, "correlation_id": correlation_id},
        }
        store_receipts([receipt])
    except Exception:
        pass  # Receipt failure never blocks client event ingestion (GREEN tier)

    return JSONResponse(
        status_code=201,
        content={
            "event_id": event_id,
            "correlation_id": correlation_id,
        },
    )


# ---------------------------------------------------------------------------
# Browser Automation Endpoints (Hybrid Browser View — Wave 4)
# ---------------------------------------------------------------------------


@app.post("/v1/browser/navigate")
async def browser_navigate_endpoint(request: Request):
    """Navigate browser to URL and capture screenshot (Wave 4).

    Directly invokes browser_service for admin/testing purposes.
    Production flow: User intent → LangGraph → Adam skill pack → browser_service

    Request body:
        {
            "url": "https://www.bing.com/search?q=aspire",
            "suite_id": "uuid",
            "viewport_width": 1280,  // optional
            "viewport_height": 800    // optional
        }

    Response:
        {
            "success": true,
            "screenshot_id": "uuid",
            "screenshot_url": "https://s3.../screenshot.png",
            "page_url": "https://www.bing.com/search",  // redacted
            "page_title": "Bing Search Results",
            "receipt_id": "uuid"
        }

    Security:
        - Domain allowlist enforced (SSRF prevention)
        - PII redaction on page_url and page_title
        - Receipt generated for all navigation attempts
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON body"},
        )

    url = body.get("url", "").strip()
    suite_id = body.get("suite_id", "").strip()
    viewport_width = body.get("viewport_width", 1280)
    viewport_height = body.get("viewport_height", 800)

    # Validate required fields
    if not url:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing required field: url"},
        )

    if not suite_id:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing required field: suite_id"},
        )

    screenshot_id = str(uuid.uuid4())
    correlation_id = request.headers.get("x-correlation-id", str(uuid.uuid4()))

    try:
        from aspire_orchestrator.services.browser_service import (
            get_browser_service,
            DomainDeniedError,
            NavigationTimeoutError,
            ScreenshotUploadError,
        )

        browser_service = get_browser_service()

        # Navigate and capture screenshot
        screenshot_result = await browser_service.navigate_and_screenshot(
            url=url,
            screenshot_id=screenshot_id,
            suite_id=suite_id,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
        )

        # Generate receipt (Law #2)
        receipt_id = str(uuid.uuid4())
        receipt = {
            "id": receipt_id,
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": "",
            "actor_type": "system",
            "actor_id": "browser_endpoint",
            "action_type": "browser.navigate",
            "risk_tier": "yellow",
            "tool_used": "browser_service",
            "capability_token_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "outcome": "success",
            "reason_code": "SCREENSHOT_CAPTURED",
            "receipt_type": "action",
            "receipt_hash": "",
            "action": {"url": screenshot_result.page_url},  # Redacted URL
            "result": {
                "screenshot_id": screenshot_result.screenshot_id,
                "screenshot_url": screenshot_result.screenshot_url,
                "page_title": screenshot_result.page_title,  # PII-redacted
                "page_load_time_ms": screenshot_result.page_load_time_ms,
            },
        }
        store_receipts([receipt])

        logger.info(
            "Browser navigation successful",
            extra={
                "screenshot_id": screenshot_id,
                "page_url": screenshot_result.page_url,
                "correlation_id": correlation_id,
            }
        )

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "screenshot_id": screenshot_result.screenshot_id,
                "screenshot_url": screenshot_result.screenshot_url,
                "page_url": screenshot_result.page_url,
                "page_title": screenshot_result.page_title,
                "viewport_width": screenshot_result.viewport_width,
                "viewport_height": screenshot_result.viewport_height,
                "page_load_time_ms": screenshot_result.page_load_time_ms,
                "receipt_id": receipt_id,
            }
        )

    except DomainDeniedError as e:
        # Domain not in allowlist (SSRF blocked)
        logger.warning(f"Browser navigation denied: {e}", extra={"url": url})

        receipt_id = str(uuid.uuid4())
        receipt = {
            "id": receipt_id,
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": "",
            "actor_type": "system",
            "actor_id": "browser_endpoint",
            "action_type": "browser.navigate",
            "risk_tier": "yellow",
            "tool_used": "browser_service",
            "capability_token_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "outcome": "denied",
            "reason_code": "DOMAIN_NOT_ALLOWED",
            "receipt_type": "action",
            "receipt_hash": "",
            "action": {"url": url},
            "result": {"error": str(e)},
        }
        store_receipts([receipt])

        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "error": f"Domain denied: {str(e)}",
                "receipt_id": receipt_id,
            }
        )

    except NavigationTimeoutError as e:
        # Page load timeout (>30s)
        logger.error(f"Browser navigation timeout: {e}", extra={"url": url})

        receipt_id = str(uuid.uuid4())
        receipt = {
            "id": receipt_id,
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": "",
            "actor_type": "system",
            "actor_id": "browser_endpoint",
            "action_type": "browser.navigate",
            "risk_tier": "yellow",
            "tool_used": "browser_service",
            "capability_token_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "outcome": "failed",
            "reason_code": "TIMEOUT",
            "receipt_type": "action",
            "receipt_hash": "",
            "action": {"url": url},
            "result": {"error": str(e)},
        }
        store_receipts([receipt])

        return JSONResponse(
            status_code=504,
            content={
                "success": False,
                "error": f"Navigation timeout: {str(e)}",
                "receipt_id": receipt_id,
            }
        )

    except ScreenshotUploadError as e:
        # S3 upload failed
        logger.error(f"Screenshot upload failed: {e}", extra={"url": url})

        receipt_id = str(uuid.uuid4())
        receipt = {
            "id": receipt_id,
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": "",
            "actor_type": "system",
            "actor_id": "browser_endpoint",
            "action_type": "browser.navigate",
            "risk_tier": "yellow",
            "tool_used": "browser_service",
            "capability_token_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "outcome": "failed",
            "reason_code": "UPLOAD_FAILED",
            "receipt_type": "action",
            "receipt_hash": "",
            "action": {"url": url},
            "result": {"error": str(e)},
        }
        store_receipts([receipt])

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": f"Screenshot upload failed: {str(e)}",
                "receipt_id": receipt_id,
            }
        )

    except Exception as e:
        # Unexpected error
        logger.error(f"Browser navigation failed: {e}", exc_info=True, extra={"url": url})

        receipt_id = str(uuid.uuid4())
        receipt = {
            "id": receipt_id,
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": "",
            "actor_type": "system",
            "actor_id": "browser_endpoint",
            "action_type": "browser.navigate",
            "risk_tier": "yellow",
            "tool_used": "browser_service",
            "capability_token_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "outcome": "failed",
            "reason_code": "UNEXPECTED_ERROR",
            "receipt_type": "action",
            "receipt_hash": "",
            "action": {"url": url},
            "result": {"error": str(e)},
        }
        store_receipts([receipt])

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": f"Unexpected error: {str(e)}",
                "receipt_id": receipt_id,
            }
        )


@app.get("/v1/browser/screenshot/{screenshot_id}")
async def get_screenshot_endpoint(screenshot_id: str, request: Request):
    """Retrieve presigned S3 URL for existing screenshot (Wave 4).

    Path params:
        screenshot_id: UUID of screenshot

    Response:
        {
            "screenshot_url": "https://s3.../screenshot.png",
            "expires_at": 1234567890  // Unix timestamp
        }

    Security:
        - Presigned URL acts as capability token (no additional auth required)
        - URL expires in 1 hour
        - S3 path is tenant-scoped (suite_id embedded in S3 key)
    """
    # Note: In production, this endpoint should validate suite_id from X-Suite-Id header
    # and verify the screenshot belongs to the requesting tenant. For now, we rely on
    # presigned URL security (S3 path includes suite_id, URL is unguessable).

    suite_id = request.headers.get("x-suite-id", "")

    if not suite_id:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing X-Suite-Id header"},
        )

    try:
        from aspire_orchestrator.services.browser_service import get_browser_service
        import boto3
        from datetime import timedelta

        browser_service = get_browser_service()
        s3_key = f"{suite_id}/{screenshot_id}.png"

        # Generate new presigned URL (1hr expiry)
        presigned_url = browser_service.s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": browser_service.s3_bucket, "Key": s3_key},
            ExpiresIn=3600,  # 1 hour
        )

        expires_at = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())

        logger.info(
            "Screenshot presigned URL generated",
            extra={"screenshot_id": screenshot_id, "suite_id": suite_id[:8]}
        )

        return JSONResponse(
            status_code=200,
            content={
                "screenshot_url": presigned_url,
                "expires_at": expires_at,
            }
        )

    except Exception as e:
        logger.error(
            f"Failed to generate presigned URL: {e}",
            exc_info=True,
            extra={"screenshot_id": screenshot_id}
        )

        return JSONResponse(
            status_code=404,
            content={"error": "Screenshot not found or access denied"},
        )
