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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Load .env BEFORE any other imports that read os.environ
# Pydantic BaseSettings reads .env for its own fields, but os.environ.get() calls
# in intent_classifier, param_extract, respond etc. need dotenv to inject values.
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from aspire_orchestrator.config.secrets import load_secrets
from aspire_orchestrator.middleware.exception_handler import GlobalExceptionMiddleware
from aspire_orchestrator.middleware.correlation import CorrelationIdMiddleware
from aspire_orchestrator.middleware.rate_limiter import RateLimitMiddleware
from aspire_orchestrator.graph import build_orchestrator_graph
from aspire_orchestrator.services.policy_engine import get_policy_matrix
from aspire_orchestrator.services.receipt_store import query_receipts, get_chain_receipts, store_receipts
from aspire_orchestrator.services.receipt_chain import verify_chain
from aspire_orchestrator.services.registry import get_registry
from aspire_orchestrator.services.a2a_service import get_a2a_service, A2ATaskStatus
from aspire_orchestrator.services.metrics import METRICS
from aspire_orchestrator.routes.intents import router as intents_router
from aspire_orchestrator.routes.admin import router as admin_router
from aspire_orchestrator.routes.robots import router as robots_router

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Aspire Orchestrator",
    description="LangGraph Orchestrator — the Single Brain (Law #1)",
    version="0.1.0",
)

# CORS — restricted to Gateway only (security reviewer P1 fix)
# In production, only the Gateway (localhost:3100) talks to the orchestrator.
# External clients go through Gateway, never directly to orchestrator.
_CORS_ORIGINS = os.environ.get(
    "ASPIRE_CORS_ORIGINS",
    "http://localhost:3100,http://127.0.0.1:3100",
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Correlation-Id"],
)

# Global Exception Handler — catches unhandled exceptions, creates incident + receipt (Wave 1B)
# Added AFTER CORS so it wraps all routes (Starlette: last added = outermost)
app.add_middleware(GlobalExceptionMiddleware)

# Rate Limiting — per-tenant sliding window (B-H7, Enterprise Remediation Wave 4)
# 100 req/60s per tenant, health/metrics endpoints exempt
_rate_limit = int(os.environ.get("ASPIRE_RATE_LIMIT", "100"))
_rate_window = int(os.environ.get("ASPIRE_RATE_WINDOW_SECONDS", "60"))
app.add_middleware(RateLimitMiddleware, limit=_rate_limit, window_seconds=_rate_window)

# Correlation ID — extracts/generates X-Correlation-Id, propagates via contextvars (Wave 2A)
# Added LAST so it runs FIRST (outermost): sets correlation ID before anything else
app.add_middleware(CorrelationIdMiddleware)

# Include Brain Layer routes
app.include_router(intents_router)

# Include Admin Ops Telemetry Facade routes (Wave 8)
app.include_router(admin_router)

# Include Robot Ingest routes (Wave 3 — Enterprise Sync)
app.include_router(robots_router)

# Load secrets from AWS Secrets Manager (production) or .env (dev)
# Must happen BEFORE graph build, which may read provider keys from os.environ
load_secrets()

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

# Build the graph once at startup
orchestrator_graph = build_orchestrator_graph()


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
            settings.token_signing_key or os.environ.get("ASPIRE_TOKEN_SIGNING_KEY")
        ),
        "graph_built": orchestrator_graph is not None,
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

    # Check Redis connectivity (if configured)
    redis_url = os.environ.get("REDIS_URL") or os.environ.get("ASPIRE_REDIS_URL")
    if redis_url:
        try:
            import redis
            r = redis.from_url(redis_url, socket_timeout=2)
            r.ping()
            checks["redis"] = True
        except Exception:
            checks["redis"] = False

    # Check policy engine loaded
    try:
        matrix = get_policy_matrix()
        checks["policy_engine"] = matrix is not None and len(matrix) > 0
    except Exception:
        checks["policy_engine"] = False

    all_ready = all(checks.values())
    # Determine if partially ready (some non-critical deps down)
    critical_checks = {
        k: v for k, v in checks.items()
        if k in ("signing_key_configured", "graph_built", "receipt_store")
    }
    critical_ready = all(critical_checks.values())

    status = "ready" if all_ready else ("degraded" if critical_ready else "not_ready")
    return JSONResponse(
        status_code=200 if critical_ready else 503,
        content={
            "status": status,
            "service": "aspire-orchestrator",
            "checks": checks,
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
) -> Any:
    """Generator for Server-Sent Events streaming during orchestrator execution.

    Enterprise-grade SSE with:
      - Connection tracking (max 100 per tenant, Law #3)
      - Heartbeat every 15s (prevents proxy/load-balancer timeout)
      - Per-stream rate limiting (max 10 events/second)
      - PII redaction on all messages (Law #9)
      - Receipt generation for stream lifecycle (Law #2)
      - Correlation ID propagation

    Used for Canvas Chat Mode live updates (Wave 4 + Wave 5 + SSE Enterprise).
    """
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

    # -- Connection limit check (Law #3: fail closed) --------------------------
    if not tracker.try_connect(
        suite_id,
        stream_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
    ):
        # Law #2: Receipt for denied connection
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
        except Exception:
            pass  # Receipt failure must not mask the denial
        yield format_sse_event({
            "type": "error",
            "message": "Connection limit exceeded for tenant",
            "code": "CONNECTION_LIMIT_EXCEEDED",
            "timestamp": int(time.time() * 1000),
        })
        return

    # -- Receipt for stream initiation (Law #2) --------------------------------
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
    except Exception:
        pass  # Receipt failure must not block streaming

    # -- Event queue for collecting Adam's activity events ---------------------
    event_queue: list[dict[str, Any]] = []

    def collect_event(event: dict[str, Any]) -> None:
        """Callback to collect activity events from Adam (Wave 5)."""
        event_queue.append(event)

    set_activity_event_callback(collect_event)

    # Emit initial "connected" event with receipt_id
    yield format_sse_event({
        "type": "connected",
        "receipt_id": receipt_id,
        "stream_id": stream_id,
        "correlation_id": correlation_id,
        "timestamp": int(time.time() * 1000),
    })

    last_heartbeat = time.monotonic()

    try:
        # Execute graph (Adam events collected via callback)
        result = await orchestrator_graph.ainvoke(initial_state)

        # Emit collected Adam events (rate-limited)
        for event in event_queue:
            if rate_limiter.check():
                tracker.increment_event_count(stream_id)
                yield format_sse_event(event)
            else:
                # Rate limited — skip event but log
                logger.debug(
                    "SSE rate limited: skipping event for stream %s",
                    stream_id[:8],
                )

            # Heartbeat check between events
            now = time.monotonic()
            if now - last_heartbeat > HEARTBEAT_INTERVAL_SECONDS:
                yield format_sse_event({"type": "heartbeat", "timestamp": int(time.time() * 1000)})
                last_heartbeat = now

        # Emit final status event
        response = result.get("response", {})
        if response.get("error"):
            yield format_sse_event({
                "type": "error",
                "message": response.get("message", "Request failed"),
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

        # Emit final response payload
        yield format_sse_event({"type": "response", "data": response})

        # Law #2: Receipt for stream completion
        completion_receipt = build_stream_receipt(
            action_type="stream.complete",
            suite_id=suite_id,
            office_id=office_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            outcome="success",
            stream_id=stream_id,
            details={
                "event_count": len(event_queue),
                "stream_id": stream_id,
            },
        )
        try:
            store_receipts([completion_receipt])
        except Exception:
            pass

    except asyncio.CancelledError:
        # Client disconnected — graceful cleanup
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
        except Exception:
            pass

    except Exception as e:
        logger.exception("SSE stream error: %s", e)
        yield format_sse_event({
            "type": "error",
            "message": "Stream interrupted",
            "icon": "error",
            "timestamp": int(time.time() * 1000),
        })

        # Law #2: Receipt for stream error
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
        except Exception:
            pass

    finally:
        # Clean up: callback + connection tracking
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

    initial_state: dict[str, Any] = {
        "request": body,
        "actor_id": actor_id,
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
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
                "X-Correlation-Id": correlation_id,
            },
        )

    start_time = time.monotonic()
    try:
        result = await orchestrator_graph.ainvoke(initial_state)

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
                "INTERNAL_ERROR": 500,
            }
            status_code = status_map.get(error, 500)
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
        return JSONResponse(status_code=200, content=response)

    except Exception as e:
        logger.exception("Orchestrator error: %s", e)
        METRICS.record_request(status="failed", task_type=body.get("task_type", "unknown") if isinstance(body, dict) else "unknown")
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
        result = await resume_after_approval(approval_id, suite_id, office_id, actor_id)

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
