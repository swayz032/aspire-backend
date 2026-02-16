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

import logging
import os
import time
from typing import Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

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

# Include Brain Layer routes
app.include_router(intents_router)

# Include Admin Ops Telemetry Facade routes (Wave 8)
app.include_router(admin_router)

# Include Robot Ingest routes (Wave 3 — Enterprise Sync)
app.include_router(robots_router)

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
    """Readiness probe — checks critical dependencies are configured."""
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

    all_ready = all(checks.values())
    return JSONResponse(
        status_code=200 if all_ready else 503,
        content={
            "status": "ready" if all_ready else "not_ready",
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


@app.post("/v1/intents")
async def process_intent(request: Request) -> JSONResponse:
    """Process an AvaOrchestratorRequest through the orchestrator graph.

    The graph executes the full pipeline:
    Intake -> Safety -> Policy -> Approval -> TokenMint -> Execute -> ReceiptWrite -> Respond
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
    actor_id = request.headers.get("x-actor-id", "unknown")
    suite_id = request.headers.get("x-suite-id")
    correlation_id = request.headers.get("x-correlation-id")

    initial_state: dict[str, Any] = {
        "request": body,
        "actor_id": actor_id,
    }
    if suite_id:
        initial_state["auth_suite_id"] = suite_id
    if correlation_id:
        initial_state["correlation_id"] = correlation_id

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
