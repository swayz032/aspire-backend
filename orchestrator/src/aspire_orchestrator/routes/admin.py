"""Admin Ops Telemetry Facade API — Wave 8.

Read-only, LLM-safe telemetry facade for the Aspire Admin portal (Ava Admin).
Implements the OpenAPI spec at plan/contracts/ava-admin/ops_telemetry_facade.openapi.yaml.

Endpoints (9 total, ALL read-only except proposal approval):
  GET  /admin/ops/health          — Health check (no auth)
  GET  /admin/ops/incidents       — List incidents (filtered, paginated)
  GET  /admin/ops/incidents/{id}  — Get incident detail + timeline + evidence_pack
  GET  /admin/ops/receipts        — List receipts (filtered, paginated, PII-redacted)
  GET  /admin/ops/provider-calls  — List provider calls (filtered, redacted)
  GET  /admin/ops/outbox          — Outbox queue status (mock for now)
  GET  /admin/ops/rollouts        — List rollouts (empty for now)
  GET  /admin/proposals/pending   — List pending change proposals
  POST /admin/proposals/{id}/approve — Approve a change proposal

Auth: X-Admin-Token header (JWT validated with ASPIRE_ADMIN_JWT_SECRET).
Law compliance:
  - Law #2: ALL endpoints generate access receipts (even reads).
  - Law #3: Missing/invalid admin token -> 401 (fail closed).
  - Law #7: Read-only facade — no autonomous decisions.
  - Law #9: PII redacted in receipt/provider-call previews.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from aspire_orchestrator.services.receipt_store import query_receipts, store_receipts

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# In-Memory Stores (Phase 2 — will be replaced by Supabase in Phase 3)
# =============================================================================

_store_lock = threading.Lock()
_incidents: dict[str, dict[str, Any]] = {}
_provider_calls: list[dict[str, Any]] = []
_rollouts: list[dict[str, Any]] = []
_proposals: dict[str, dict[str, Any]] = {}
_builder_model_policy: dict[str, Any] = {
    "builder_primary_model": os.environ.get("ASPIRE_BUILDER_PRIMARY_MODEL", "codex-5.2"),
    "builder_fallback_model": os.environ.get("ASPIRE_BUILDER_FALLBACK_MODEL", "claude-opus-4.6"),
    "reasoning_model": os.environ.get("ASPIRE_BUILDER_REASONING_MODEL", "gpt-5.2"),
    "updated_at": datetime.now(timezone.utc).isoformat(),
    "updated_by": "system",
}
_supabase_client: Any | None = None


def register_incident(incident: dict[str, Any]) -> None:
    """Register an incident into the in-memory store AND the admin_store singleton.

    Called by other services (e.g., health monitors, circuit breakers)
    to publish incidents that the admin facade exposes.

    Dual-writes to both _incidents (local) and admin_store (singleton)
    to ensure ava_admin_desk and other consumers can always find incidents.
    """
    incident_id = incident.get("incident_id")
    if not incident_id:
        incident_id = str(uuid.uuid4())
        incident["incident_id"] = incident_id
    if not incident.get("trace_id"):
        incident["trace_id"] = incident.get("correlation_id", "")

    with _store_lock:
        _incidents[incident_id] = incident

    # Also write to admin_store singleton (ensures ava_admin_desk can find it)
    try:
        from aspire_orchestrator.services.admin_store import get_admin_store
        store = get_admin_store()
        store._incidents[incident_id] = incident
    except Exception:
        pass  # Non-blocking — in-memory store is the safety net


def register_provider_call(call: dict[str, Any]) -> None:
    """Register a provider call record.

    Called by tool executors after each external API call.
    """
    if not call.get("call_id"):
        call["call_id"] = str(uuid.uuid4())

    with _store_lock:
        _provider_calls.append(call)


def register_proposal(proposal: dict[str, Any]) -> None:
    """Register a change proposal for admin review."""
    proposal_id = proposal.get("proposal_id")
    if not proposal_id:
        proposal_id = str(uuid.uuid4())
        proposal["proposal_id"] = proposal_id

    with _store_lock:
        _proposals[proposal_id] = proposal


def clear_admin_stores() -> None:
    """Clear all in-memory admin stores. Testing only."""
    with _store_lock:
        _incidents.clear()
        _provider_calls.clear()
        _rollouts.clear()
        _proposals.clear()


def _get_supabase_client() -> Any | None:
    """Lazy-init Supabase client for admin read APIs."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    url = os.environ.get("ASPIRE_SUPABASE_URL", "").strip()
    key = os.environ.get("ASPIRE_SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        return None

    try:
        from supabase import create_client
        _supabase_client = create_client(url, key)
        return _supabase_client
    except Exception as exc:
        logger.warning("Failed to initialize Supabase client for admin facade: %s", exc)
        return None


# Initialize AdminSupabaseStore singleton with shared in-memory dicts.
# This ensures ava_admin_desk and any other consumer of get_admin_store()
# shares the same incident/provider_call data as this module.
from aspire_orchestrator.services.admin_store import get_admin_store as _init_admin_store
_shared_admin_store = _init_admin_store(incidents=_incidents, provider_calls=_provider_calls)


# =============================================================================
# Error Response Schema (OpsError per OpenAPI spec)
# =============================================================================


def _ops_error(
    *,
    code: str,
    message: str,
    correlation_id: str,
    status_code: int,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build a standardized OpsError response."""
    content: dict[str, Any] = {
        "code": code,
        "message": message,
        "correlation_id": correlation_id,
        "retryable": retryable,
    }
    if details:
        content["details"] = details
    return JSONResponse(status_code=status_code, content=content)


# =============================================================================
# Auth Middleware (Law #3: Fail Closed)
# =============================================================================


def _require_admin(request: Request) -> str | None:
    """Verify admin JWT from X-Admin-Token header (Law #3: fail closed).

    Returns actor_id if valid, None if invalid.
    Requires ASPIRE_ADMIN_JWT_SECRET to be set in ALL environments.
    No dev mode bypass — governance is the product.
    """
    import jwt as pyjwt

    token = request.headers.get("x-admin-token", "")
    if not token:
        return None

    secret = os.environ.get("ASPIRE_ADMIN_JWT_SECRET")
    if not secret:
        # Law #3: fail closed — no secret configured means deny
        logger.error("ASPIRE_ADMIN_JWT_SECRET not configured — denying admin access")
        # Register incident for missing critical config (observability)
        try:
            register_incident({
                "incident_id": f"missing-admin-jwt-{int(time.time())}",
                "severity": "sev1",
                "title": "ASPIRE_ADMIN_JWT_SECRET not configured — all admin access denied",
                "state": "open",
                "correlation_id": "",
            })
        except Exception:
            pass  # Incident registration failure never masks the auth denial
        return None

    try:
        payload = pyjwt.decode(token, secret, algorithms=["HS256"])
        actor_id = payload.get("sub", "admin")
        return actor_id
    except Exception as e:
        logger.warning("Admin JWT validation failed: %s", e)
        return None


# =============================================================================
# Correlation ID Helper
# =============================================================================


def _get_correlation_id(request: Request) -> str:
    """Extract or generate correlation ID from request headers."""
    return request.headers.get("x-correlation-id", str(uuid.uuid4()))


def _get_supabase_jwt_secret() -> str:
    """Resolve Supabase JWT secret from known env var names."""
    return (
        os.environ.get("SUPABASE_JWT_SECRET")
        or os.environ.get("JWT_SECRET")
        or ""
    ).strip()


@router.post("/admin/auth/exchange")
async def exchange_admin_token(request: Request) -> JSONResponse:
    """Exchange a verified Supabase access token for an admin facade JWT.

    This bridges admin-portal auth (Supabase session token) to backend admin auth
    (X-Admin-Token signed by ASPIRE_ADMIN_JWT_SECRET).
    """
    import jwt as pyjwt

    correlation_id = _get_correlation_id(request)
    auth_header = request.headers.get("authorization", "").strip()
    if not auth_header.lower().startswith("bearer "):
        return _ops_error(
            code="AUTH_REQUIRED",
            message="Missing Authorization bearer token",
            correlation_id=correlation_id,
            status_code=401,
        )

    access_token = auth_header.split(" ", 1)[1].strip()
    if not access_token:
        return _ops_error(
            code="AUTH_REQUIRED",
            message="Missing Authorization bearer token",
            correlation_id=correlation_id,
            status_code=401,
        )

    supabase_secret = _get_supabase_jwt_secret()
    admin_secret = (os.environ.get("ASPIRE_ADMIN_JWT_SECRET") or "").strip()
    if not supabase_secret or not admin_secret:
        return _ops_error(
            code="AUTH_CONFIG_MISSING",
            message="Admin auth secrets are not configured",
            correlation_id=correlation_id,
            status_code=503,
            retryable=False,
        )

    try:
        payload = pyjwt.decode(
            access_token,
            supabase_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except Exception:
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Invalid session token",
            correlation_id=correlation_id,
            status_code=401,
        )

    actor_id = str(payload.get("sub") or "").strip()
    if not actor_id:
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Session token missing subject",
            correlation_id=correlation_id,
            status_code=401,
        )

    email = str(payload.get("email") or "").strip().lower()
    role = str(payload.get("role") or "authenticated")
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=1)
    admin_payload = {
        "sub": actor_id,
        "email": email,
        "role": role,
        "scope": "admin_facade",
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    admin_token = pyjwt.encode(admin_payload, admin_secret, algorithm="HS256")

    return JSONResponse(
        status_code=200,
        content={
            "admin_token": admin_token,
            "expires_at": expires_at.isoformat(),
            "correlation_id": correlation_id,
        },
    )


# =============================================================================
# Pagination Helper
# =============================================================================


def _paginate(
    items: list[dict[str, Any]],
    cursor: str | None,
    limit: int,
    id_field: str = "incident_id",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Cursor-based pagination. Returns (page_items, page_info).

    Finds the item matching cursor, then returns `limit` items after it.
    If cursor is None, starts from the beginning.
    """
    start = 0
    if cursor:
        for i, item in enumerate(items):
            item_id = item.get(id_field, item.get("id", ""))
            if item_id == cursor:
                start = i + 1
                break

    page = items[start : start + limit]
    has_more = start + limit < len(items)
    next_cursor: str | None = None
    if has_more and page:
        last = page[-1]
        next_cursor = last.get(id_field, last.get("id"))

    return page, {"has_more": has_more, "next_cursor": next_cursor}


# =============================================================================
# Access Receipt Builder (Law #2)
# =============================================================================


def _build_access_receipt(
    *,
    correlation_id: str,
    actor_id: str,
    action_type: str,
    outcome: str,
    reason_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an access receipt for an admin API call (Law #2).

    Every admin endpoint call generates a receipt, even reads.
    """
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": "system",
        "office_id": "admin",
        "actor_type": "admin",
        "actor_id": actor_id,
        "action_type": action_type,
        "risk_tier": "green",
        "tool_used": "admin_facade",
        "outcome": outcome,
        "reason_code": reason_code,
        "created_at": now,
        "receipt_type": "admin_access",
        "receipt_hash": str(uuid.uuid4()),
        "redacted_inputs": None,
        "redacted_outputs": details,
    }


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# 1. GET /admin/ops/health — Health Check (no auth)
# =============================================================================


@router.get("/admin/ops/health")
async def admin_health(request: Request) -> JSONResponse:
    """Health check for the admin facade. No auth required."""
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "server_time": _now_iso(),
            "version": "3.0.0",
        },
    )


# =============================================================================
# 2. GET /admin/ops/incidents — List Incidents
# =============================================================================


@router.get("/admin/ops/incidents")
async def list_incidents(
    request: Request,
    state: str | None = Query(None, description="Filter by state"),
    severity: str | None = Query(None, description="Filter by severity"),
    trace_id: str | None = Query(None, description="Filter by trace_id"),
    cursor: str | None = Query(None, description="Pagination cursor"),
    limit: int = Query(50, ge=1, le=200, description="Page size"),
) -> JSONResponse:
    """List incidents with optional state/severity filters."""
    correlation_id = _get_correlation_id(request)

    # Auth check (Law #3)
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.incidents.list",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    # Build filtered list
    with _store_lock:
        all_incidents = list(_incidents.values())

    if state:
        valid_states = {"open", "investigating", "mitigated", "closed"}
        if state not in valid_states:
            return _ops_error(
                code="VALIDATION_ERROR",
                message=f"Invalid state filter: {state}. Valid: {sorted(valid_states)}",
                correlation_id=correlation_id,
                status_code=400,
            )
        all_incidents = [i for i in all_incidents if i.get("state") == state]

    if severity:
        valid_severities = {"sev1", "sev2", "sev3", "sev4"}
        if severity not in valid_severities:
            return _ops_error(
                code="VALIDATION_ERROR",
                message=f"Invalid severity filter: {severity}. Valid: {sorted(valid_severities)}",
                correlation_id=correlation_id,
                status_code=400,
            )
        all_incidents = [i for i in all_incidents if i.get("severity") == severity]
    if trace_id:
        all_incidents = [i for i in all_incidents if i.get("trace_id") == trace_id]

    # Sort by last_seen descending (newest first)
    all_incidents.sort(key=lambda x: x.get("last_seen", ""), reverse=True)

    page_items, page_info = _paginate(all_incidents, cursor, limit, id_field="incident_id")

    # Law #2: access receipt
    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.incidents.list",
        outcome="success",
        details={"count": len(page_items), "filters": {"state": state, "severity": severity, "trace_id": trace_id}},
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "items": page_items,
            "page": page_info,
            "server_time": _now_iso(),
        },
    )


# =============================================================================
# 3. GET /admin/ops/incidents/{incident_id} — Incident Detail
# =============================================================================


@router.get("/admin/ops/incidents/{incident_id}")
async def get_incident(request: Request, incident_id: str) -> JSONResponse:
    """Get detailed incident with timeline and evidence pack."""
    correlation_id = _get_correlation_id(request)

    # Auth check
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.incidents.get",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    with _store_lock:
        incident = _incidents.get(incident_id)

    if incident is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id=actor_id,
            action_type="admin.ops.incidents.get",
            outcome="failed",
            reason_code="NOT_FOUND",
            details={"incident_id": incident_id},
        )
        store_receipts([receipt])
        return _ops_error(
            code="NOT_FOUND",
            message=f"Incident not found: {incident_id}",
            correlation_id=correlation_id,
            status_code=404,
        )

    # Law #2: access receipt
    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.incidents.get",
        outcome="success",
        details={"incident_id": incident_id},
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "incident_id": incident["incident_id"],
            "state": incident.get("state", "open"),
            "severity": incident.get("severity", "sev4"),
            "title": incident.get("title", ""),
            "correlation_id": incident.get("correlation_id", ""),
            "trace_id": incident.get("trace_id", incident.get("correlation_id", "")),
            "suite_id": incident.get("suite_id"),
            "first_seen": incident.get("first_seen", ""),
            "last_seen": incident.get("last_seen", ""),
            "timeline": incident.get("timeline", []),
            "evidence_pack": incident.get("evidence_pack", {}),
            "server_time": _now_iso(),
        },
    )


# =============================================================================
# 4. GET /admin/ops/receipts — List Receipts (PII-redacted)
# =============================================================================


@router.get("/admin/ops/receipts")
async def list_admin_receipts(
    request: Request,
    correlation_id_filter: str | None = Query(
        None, alias="correlation_id", description="Filter by correlation_id"
    ),
    suite_id: str | None = Query(None, description="Filter by suite_id"),
    office_id: str | None = Query(None, description="Filter by office_id"),
    action_type: str | None = Query(None, description="Filter by action_type"),
    since: str | None = Query(None, description="Filter: created_at >= since (ISO 8601)"),
    until: str | None = Query(None, description="Filter: created_at <= until (ISO 8601)"),
    cursor: str | None = Query(None, description="Pagination cursor"),
    limit: int = Query(50, ge=1, le=200, description="Page size"),
) -> JSONResponse:
    """List receipts with optional filters. PII redacted (Law #9)."""
    correlation_id = _get_correlation_id(request)

    # Auth check
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.receipts.list",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    # Law #6: Tenant isolation enforced even for admin — suite_id required.
    # Cross-suite admin view is a Phase 3 feature (requires elevated scope model).
    if not suite_id:
        receipt = {
            "id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "suite_id": "unknown",
            "actor_type": "admin",
            "actor_id": actor_id,
            "action_type": "admin.receipts.query",
            "risk_tier": "yellow",
            "outcome": "denied",
            "reason_code": "missing_suite_id",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        store_receipts([receipt])
        return _ops_error(
            code="MISSING_SUITE_ID",
            message="suite_id query parameter is required (Law #6: tenant isolation)",
            correlation_id=correlation_id,
            status_code=400,
        )

    results = query_receipts(suite_id=suite_id, correlation_id=correlation_id_filter, action_type=action_type)

    # Apply office_id filter
    if office_id:
        results = [r for r in results if r.get("office_id") == office_id]

    # Apply time range filters
    if since:
        results = [r for r in results if r.get("created_at", "") >= since]
    if until:
        results = [r for r in results if r.get("created_at", "") <= until]

    # Sort newest first
    results.sort(key=lambda r: r.get("created_at", ""), reverse=True)

    # Map to ReceiptSummary schema (redacted — Law #9)
    receipt_summaries = [
        {
            "receipt_id": r.get("id", ""),
            "correlation_id": r.get("correlation_id", ""),
            "suite_id": r.get("suite_id", ""),
            "office_id": r.get("office_id", ""),
            "action_type": r.get("action_type", ""),
            "risk_tier": r.get("risk_tier", "green"),
            "outcome": r.get("outcome", ""),
            "created_at": r.get("created_at", ""),
        }
        for r in results
    ]

    # Paginate
    page_items, page_info = _paginate(receipt_summaries, cursor, limit, id_field="receipt_id")

    # Law #2: access receipt
    access_receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.receipts.list",
        outcome="success",
        details={"count": len(page_items)},
    )
    store_receipts([access_receipt])

    return JSONResponse(
        status_code=200,
        content={
            "items": page_items,
            "page": page_info,
            "server_time": _now_iso(),
        },
    )


# =============================================================================
# 5. GET /admin/ops/provider-calls — List Provider Calls (redacted)
# =============================================================================


@router.get("/admin/ops/provider-calls")
async def list_provider_calls(
    request: Request,
    provider: str | None = Query(None, description="Filter by provider"),
    status: str | None = Query(None, description="Filter by status (success/error)"),
    correlation_id_filter: str | None = Query(
        None, alias="correlation_id", description="Filter by correlation_id"
    ),
    trace_id_filter: str | None = Query(
        None, alias="trace_id", description="Filter by trace_id"
    ),
    cursor: str | None = Query(None, description="Pagination cursor"),
    limit: int = Query(50, ge=1, le=200, description="Page size"),
) -> JSONResponse:
    """List provider calls with optional filters. Payloads always redacted (Law #9)."""
    correlation_id = _get_correlation_id(request)

    # Auth check
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.provider_calls.list",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    with _store_lock:
        all_calls = list(_provider_calls)

    # Apply filters
    if provider:
        all_calls = [c for c in all_calls if c.get("provider") == provider]
    if status:
        if status not in ("success", "error"):
            return _ops_error(
                code="VALIDATION_ERROR",
                message=f"Invalid status filter: {status}. Valid: success, error",
                correlation_id=correlation_id,
                status_code=400,
            )
        all_calls = [c for c in all_calls if c.get("status") == status]
    if correlation_id_filter:
        all_calls = [c for c in all_calls if c.get("correlation_id") == correlation_id_filter]
    if trace_id_filter:
        all_calls = [c for c in all_calls if c.get("trace_id") == trace_id_filter]

    # Sort by started_at descending
    all_calls.sort(key=lambda x: x.get("started_at", ""), reverse=True)

    # Map to ProviderCallSummary (Law #9: always redact payload preview)
    call_summaries = [
        {
            "call_id": c.get("call_id", ""),
            "correlation_id": c.get("correlation_id", ""),
            "trace_id": c.get("trace_id", ""),
            "provider": c.get("provider", ""),
            "action": c.get("action", ""),
            "status": c.get("status", ""),
            "http_status": c.get("http_status"),
            "retry_count": c.get("retry_count", 0),
            "started_at": c.get("started_at", ""),
            "finished_at": c.get("finished_at"),
            "redacted_payload_preview": _redact_payload_preview(
                c.get("payload_preview", c.get("redacted_payload_preview", ""))
            ),
        }
        for c in all_calls
    ]

    page_items, page_info = _paginate(call_summaries, cursor, limit, id_field="call_id")

    # Law #2: access receipt
    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.provider_calls.list",
        outcome="success",
        details={"count": len(page_items)},
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "items": page_items,
            "page": page_info,
            "server_time": _now_iso(),
        },
    )


def _redact_payload_preview(preview: str) -> str:
    """Redact PII from a provider call payload preview (Law #9).

    Uses DLP service if available, otherwise applies basic truncation.
    Preview is always truncated to 200 chars max.
    """
    if not preview or not isinstance(preview, str):
        return ""

    # Attempt DLP redaction
    try:
        from aspire_orchestrator.services.dlp import redact_text

        preview = redact_text(preview)
    except Exception:
        logger.warning("DLP redaction unavailable for provider call preview, using truncation only")
        # Fallback: basic regex patterns for common PII
        preview = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', '<SSN_REDACTED>', preview)
        preview = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '<EMAIL_REDACTED>', preview)
        preview = re.sub(r'\b(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}\b', '<PHONE_REDACTED>', preview)

    # Always truncate to 200 chars
    if len(preview) > 200:
        preview = preview[:197] + "..."

    return preview


# =============================================================================
# 6. GET /admin/ops/outbox — Outbox Queue Status
# =============================================================================


@router.get("/admin/ops/outbox")
async def get_outbox_status(request: Request) -> JSONResponse:
    """Outbox queue status. Returns mock/zero values until outbox is built."""
    correlation_id = _get_correlation_id(request)

    # Auth check
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.outbox.status",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    # Get real outbox status
    try:
        from aspire_orchestrator.services.outbox_client import get_outbox_client
        outbox = get_outbox_client()
        queue_status = outbox.get_queue_status()
    except Exception as e:
        logger.warning("Outbox status query failed: %s", e)
        queue_status = {"queue_depth": 0, "oldest_age_seconds": 0, "stuck_jobs": 0, "server_time": _now_iso()}

    # Law #2: access receipt
    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.outbox.status",
        outcome="success",
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "server_time": queue_status.get("server_time", _now_iso()),
            "queue_depth": queue_status.get("queue_depth", 0),
            "oldest_age_seconds": queue_status.get("oldest_age_seconds", 0),
            "stuck_jobs": queue_status.get("stuck_jobs", 0),
        },
    )


# =============================================================================
# 7. GET /admin/ops/rollouts — List Rollouts
# =============================================================================


@router.get("/admin/ops/rollouts")
async def list_rollouts(
    request: Request,
    cursor: str | None = Query(None, description="Pagination cursor"),
    limit: int = Query(50, ge=1, le=200, description="Page size"),
) -> JSONResponse:
    """List rollouts. Returns empty list until rollout system is built."""
    correlation_id = _get_correlation_id(request)

    # Auth check
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.rollouts.list",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    with _store_lock:
        all_rollouts = list(_rollouts)

    page_items, page_info = _paginate(all_rollouts, cursor, limit, id_field="rollout_id")

    # Law #2: access receipt
    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.rollouts.list",
        outcome="success",
        details={"count": len(page_items)},
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "items": page_items,
            "page": page_info,
            "server_time": _now_iso(),
        },
    )


# =============================================================================
# 7A. GET /admin/ops/providers — Provider Connectivity Snapshot
# =============================================================================


@router.get("/admin/ops/providers")
async def list_providers(
    request: Request,
    provider: str | None = Query(None, description="Filter by provider name"),
    status: str | None = Query(None, description="Filter by status: connected|degraded|disconnected"),
) -> JSONResponse:
    """Return provider connectivity + recent reliability signals for admin portal."""
    correlation_id = _get_correlation_id(request)
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.providers.list",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    known_providers: list[dict[str, Any]] = [
        {"provider": "openai", "lane": "ai"},
        {"provider": "elevenlabs", "lane": "ai"},
        {"provider": "stripe", "lane": "payments"},
        {"provider": "plaid", "lane": "banking"},
        {"provider": "twilio", "lane": "telephony"},
        {"provider": "supabase", "lane": "storage"},
        {"provider": "railway", "lane": "infrastructure"},
        {"provider": "n8n", "lane": "automation"},
    ]

    now = datetime.now(timezone.utc)
    since_iso = (now - timedelta(hours=24)).isoformat()

    provider_rows: list[dict[str, Any]] = []
    call_rows: list[dict[str, Any]] = []
    webhook_rows: list[dict[str, Any]] = []
    db_errors: list[str] = []

    client = _get_supabase_client()
    if client is not None:
        try:
            provider_rows = client.table("finance_connections").select("*").limit(500).execute().data or []
        except Exception as exc:
            db_errors.append(f"finance_connections query failed: {exc}")
        try:
            call_rows = (
                client.table("provider_call_log")
                .select("provider,status,duration_ms,started_at")
                .gte("started_at", since_iso)
                .limit(5000)
                .execute()
                .data
                or []
            )
        except Exception as exc:
            db_errors.append(f"provider_call_log query failed: {exc}")
        try:
            webhook_rows = (
                client.table("webhook_deliveries")
                .select("provider,status,created_at")
                .gte("created_at", since_iso)
                .limit(5000)
                .execute()
                .data
                or []
            )
        except Exception:
            # webhook_deliveries may not exist yet; keep non-fatal
            webhook_rows = []

    stats_by_provider: dict[str, dict[str, float]] = {}
    for call in call_rows:
        key = str(call.get("provider") or "").strip().lower()
        if not key:
            continue
        stats = stats_by_provider.setdefault(
            key, {"total": 0.0, "failures": 0.0, "sum_ms": 0.0, "max_ms": 0.0}
        )
        stats["total"] += 1
        call_status = str(call.get("status") or "").strip().lower()
        if call_status not in {"success", "ok", "completed"}:
            stats["failures"] += 1
        duration_ms = float(call.get("duration_ms") or 0)
        stats["sum_ms"] += duration_ms
        stats["max_ms"] = max(stats["max_ms"], duration_ms)

    webhook_by_provider: dict[str, dict[str, float]] = {}
    for wb in webhook_rows:
        key = str(wb.get("provider") or "").strip().lower()
        if not key:
            continue
        stats = webhook_by_provider.setdefault(key, {"total": 0.0, "failed": 0.0})
        stats["total"] += 1
        wb_status = str(wb.get("status") or "").strip().lower()
        if wb_status in {"failed", "error", "timeout"}:
            stats["failed"] += 1

    items_map: dict[str, dict[str, Any]] = {}
    for row in provider_rows:
        key = str(row.get("provider") or row.get("provider_name") or "").strip().lower()
        if not key:
            continue
        conn_status = str(
            row.get("connection_status") or row.get("status") or row.get("state") or "connected"
        ).strip().lower()
        connected = conn_status in {"connected", "active", "healthy", "ok"}
        items_map[key] = {
            "provider": key,
            "lane": row.get("provider_type") or "unknown",
            "status": "connected" if connected else "disconnected",
            "connection_status": conn_status,
            "scopes": row.get("scopes") or [],
            "last_checked": row.get("last_webhook_at") or row.get("updated_at") or row.get("created_at"),
            "latency_ms": 0,
            "p95_latency_ms": 0,
            "error_rate": 0.0,
            "webhook_error_rate": 0.0,
        }

    for provider_meta in known_providers:
        key = provider_meta["provider"]
        items_map.setdefault(
            key,
            {
                "provider": key,
                "lane": provider_meta["lane"],
                "status": "disconnected",
                "connection_status": "unknown",
                "scopes": [],
                "last_checked": None,
                "latency_ms": 0,
                "p95_latency_ms": 0,
                "error_rate": 0.0,
                "webhook_error_rate": 0.0,
            },
        )

    for key, item in items_map.items():
        call_stats = stats_by_provider.get(key)
        if call_stats and call_stats["total"] > 0:
            error_rate = round((call_stats["failures"] / call_stats["total"]) * 100, 2)
            latency = int(call_stats["sum_ms"] / call_stats["total"])
            item["latency_ms"] = latency
            item["p95_latency_ms"] = int(call_stats["max_ms"])
            item["error_rate"] = error_rate
            if item["status"] == "connected" and (error_rate >= 5.0 or latency >= 2000):
                item["status"] = "degraded"
        wb_stats = webhook_by_provider.get(key)
        if wb_stats and wb_stats["total"] > 0:
            item["webhook_error_rate"] = round((wb_stats["failed"] / wb_stats["total"]) * 100, 2)

    items = list(items_map.values())
    if provider:
        wanted = provider.strip().lower()
        items = [p for p in items if p.get("provider") == wanted]
    if status:
        wanted_status = status.strip().lower()
        items = [p for p in items if str(p.get("status", "")).lower() == wanted_status]

    items.sort(key=lambda x: str(x.get("provider", "")))

    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.providers.list",
        outcome="success",
        details={"count": len(items)},
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "items": items,
            "count": len(items),
            "source": "supabase" if client is not None else "in_memory",
            "warnings": db_errors,
            "server_time": _now_iso(),
        },
    )


# =============================================================================
# 7B. GET /admin/ops/webhooks — Webhook Delivery Health
# =============================================================================


@router.get("/admin/ops/webhooks")
async def list_webhooks(
    request: Request,
    provider: str | None = Query(None, description="Filter by provider"),
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=500),
) -> JSONResponse:
    """Return recent webhook deliveries and delivery failure signals."""
    correlation_id = _get_correlation_id(request)
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.webhooks.list",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    items: list[dict[str, Any]] = []
    source = "in_memory"
    warnings: list[str] = []
    client = _get_supabase_client()
    if client is not None:
        try:
            query = client.table("webhook_deliveries").select("*").order("created_at", desc=True).limit(limit)
            if provider:
                query = query.eq("provider", provider)
            if status:
                query = query.eq("status", status)
            rows = query.execute().data or []
            source = "supabase:webhook_deliveries"
            items = [
                {
                    "webhook_id": r.get("id") or r.get("delivery_id") or r.get("event_id"),
                    "provider": r.get("provider", ""),
                    "event_type": r.get("event_type") or r.get("topic") or "unknown",
                    "status": r.get("status", "unknown"),
                    "http_status": r.get("http_status"),
                    "attempt": r.get("attempt") or r.get("attempt_count") or 1,
                    "latency_ms": r.get("latency_ms") or r.get("duration_ms") or 0,
                    "delivered_at": r.get("delivered_at") or r.get("created_at"),
                }
                for r in rows
            ]
        except Exception as exc:
            warnings.append(f"webhook_deliveries query failed: {exc}")

    if not items and client is not None:
        try:
            # Fallback: infer webhook status from provider call logs
            query = (
                client.table("provider_call_log")
                .select("call_id,provider,action,status,http_status,retry_count,duration_ms,started_at")
                .ilike("action", "%webhook%")
                .order("started_at", desc=True)
                .limit(limit)
            )
            if provider:
                query = query.eq("provider", provider)
            if status:
                query = query.eq("status", status)
            rows = query.execute().data or []
            source = "supabase:provider_call_log"
            items = [
                {
                    "webhook_id": r.get("call_id"),
                    "provider": r.get("provider", ""),
                    "event_type": r.get("action", "webhook"),
                    "status": r.get("status", "unknown"),
                    "http_status": r.get("http_status"),
                    "attempt": (r.get("retry_count") or 0) + 1,
                    "latency_ms": r.get("duration_ms") or 0,
                    "delivered_at": r.get("started_at"),
                }
                for r in rows
            ]
        except Exception as exc:
            warnings.append(f"provider_call_log webhook fallback failed: {exc}")

    total = len(items)
    failed = sum(
        1
        for i in items
        if str(i.get("status", "")).strip().lower() in {"failed", "error", "timeout"}
    )

    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.webhooks.list",
        outcome="success",
        details={"count": total, "failed": failed},
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "items": items,
            "count": total,
            "summary": {
                "total": total,
                "failed": failed,
                "success_rate": 0.0 if total == 0 else round(((total - failed) / total) * 100, 2),
            },
            "source": source,
            "warnings": warnings,
            "server_time": _now_iso(),
        },
    )


# =============================================================================
# 7C. GET/PUT /admin/ops/model-policy — Builder Model Routing Policy
# =============================================================================


@router.get("/admin/ops/model-policy")
async def get_model_policy(request: Request) -> JSONResponse:
    """Get current builder model policy used by admin/control-plane."""
    correlation_id = _get_correlation_id(request)
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.model_policy.get",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.model_policy.get",
        outcome="success",
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "policy": dict(_builder_model_policy),
            "allowed_models": [
                "codex-5.2",
                "gpt-5.2",
                "gpt-5",
                "gpt-5-mini",
                "claude-opus-4.6",
            ],
            "server_time": _now_iso(),
        },
    )


@router.put("/admin/ops/model-policy")
async def update_model_policy(request: Request) -> JSONResponse:
    """Update builder model policy (admin-governed, receipted)."""
    correlation_id = _get_correlation_id(request)
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.model_policy.update",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    try:
        body = await request.json()
    except Exception:
        return _ops_error(
            code="VALIDATION_ERROR",
            message="Invalid JSON body",
            correlation_id=correlation_id,
            status_code=400,
        )

    primary = str(body.get("builder_primary_model") or "").strip()
    fallback = str(body.get("builder_fallback_model") or "").strip()
    reasoning = str(body.get("reasoning_model") or "").strip()
    if not primary or not fallback or not reasoning:
        return _ops_error(
            code="VALIDATION_ERROR",
            message="builder_primary_model, builder_fallback_model, and reasoning_model are required",
            correlation_id=correlation_id,
            status_code=400,
        )

    with _store_lock:
        _builder_model_policy["builder_primary_model"] = primary
        _builder_model_policy["builder_fallback_model"] = fallback
        _builder_model_policy["reasoning_model"] = reasoning
        _builder_model_policy["updated_at"] = _now_iso()
        _builder_model_policy["updated_by"] = actor_id

    # Persist to process env for runtime consumers
    os.environ["ASPIRE_BUILDER_PRIMARY_MODEL"] = primary
    os.environ["ASPIRE_BUILDER_FALLBACK_MODEL"] = fallback
    os.environ["ASPIRE_BUILDER_REASONING_MODEL"] = reasoning

    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.model_policy.update",
        outcome="success",
        details={"primary": primary, "fallback": fallback, "reasoning": reasoning},
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "policy": dict(_builder_model_policy),
            "server_time": _now_iso(),
        },
    )


# =============================================================================
# 8. GET /admin/proposals/pending — List Pending Change Proposals
# =============================================================================


@router.get("/admin/proposals/pending")
async def list_pending_proposals(request: Request) -> JSONResponse:
    """List pending change proposals awaiting admin approval."""
    correlation_id = _get_correlation_id(request)

    # Auth check
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.proposals.list",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    with _store_lock:
        pending = [
            p for p in _proposals.values()
            if p.get("status", "pending") == "pending"
        ]

    # Law #2: access receipt
    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.proposals.list",
        outcome="success",
        details={"count": len(pending)},
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "items": pending,
            "count": len(pending),
            "server_time": _now_iso(),
        },
    )


# =============================================================================
# 9. POST /admin/proposals/{proposal_id}/approve — Approve a Change Proposal
# =============================================================================


@router.post("/admin/proposals/{proposal_id}/approve")
async def approve_proposal(request: Request, proposal_id: str) -> JSONResponse:
    """Approve a pending change proposal.

    RED tier proposals require a presence_token in the body.
    Generates an approval receipt (Law #2).
    """
    correlation_id = _get_correlation_id(request)

    # Auth check
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.proposals.approve",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    # Parse body
    try:
        body = await request.json()
    except Exception:
        return _ops_error(
            code="VALIDATION_ERROR",
            message="Invalid JSON body",
            correlation_id=correlation_id,
            status_code=400,
        )

    approver_id = body.get("approver_id", actor_id)
    approval_method = body.get("approval_method", "admin_portal")
    presence_token = body.get("presence_token")

    # Find proposal
    with _store_lock:
        proposal = _proposals.get(proposal_id)

    if proposal is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id=actor_id,
            action_type="admin.proposals.approve",
            outcome="failed",
            reason_code="NOT_FOUND",
            details={"proposal_id": proposal_id},
        )
        store_receipts([receipt])
        return _ops_error(
            code="NOT_FOUND",
            message=f"Proposal not found: {proposal_id}",
            correlation_id=correlation_id,
            status_code=404,
        )

    if proposal.get("status") != "pending":
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id=actor_id,
            action_type="admin.proposals.approve",
            outcome="denied",
            reason_code="ALREADY_PROCESSED",
            details={"proposal_id": proposal_id, "current_status": proposal.get("status")},
        )
        store_receipts([receipt])
        return _ops_error(
            code="ALREADY_PROCESSED",
            message=f"Proposal already {proposal.get('status')}: {proposal_id}",
            correlation_id=correlation_id,
            status_code=409,
        )

    # RED tier: require presence_token (Law #4)
    risk_tier = proposal.get("risk_tier", "green")
    if risk_tier == "red" and not presence_token:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id=actor_id,
            action_type="admin.proposals.approve",
            outcome="denied",
            reason_code="PRESENCE_REQUIRED",
            details={"proposal_id": proposal_id, "risk_tier": "red"},
        )
        store_receipts([receipt])
        return _ops_error(
            code="PRESENCE_REQUIRED",
            message="RED tier proposals require presence_token for approval",
            correlation_id=correlation_id,
            status_code=403,
        )

    # Approve the proposal
    now = _now_iso()
    with _store_lock:
        _proposals[proposal_id]["status"] = "approved"
        _proposals[proposal_id]["approved_at"] = now
        _proposals[proposal_id]["approved_by"] = approver_id
        _proposals[proposal_id]["approval_method"] = approval_method

    # Law #2: approval receipt
    approval_receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.proposals.approve",
        outcome="success",
        details={
            "proposal_id": proposal_id,
            "approver_id": approver_id,
            "approval_method": approval_method,
            "risk_tier": risk_tier,
            "has_presence_token": bool(presence_token),
        },
    )
    # Override risk_tier on the receipt to match the proposal
    approval_receipt["risk_tier"] = risk_tier
    store_receipts([approval_receipt])

    return JSONResponse(
        status_code=200,
        content={
            "proposal_id": proposal_id,
            "status": "approved",
            "approved_at": now,
            "approved_by": approver_id,
            "receipt_id": approval_receipt["id"],
            "server_time": now,
        },
    )


# =============================================================================
# Robot Dashboard (Wave 6A — GET /admin/ops/robots)
# =============================================================================


@router.get("/admin/ops/robots")
async def list_robot_runs(
    request: Request,
    status: str | None = Query(None, description="Filter by run status"),
    env: str | None = Query(None, description="Filter by environment"),
    limit: int = Query(50, ge=1, le=200),
) -> JSONResponse:
    """List robot runs from receipt store (filtered, paginated).

    Robot runs are stored as receipts with action_type containing 'robot.run'
    or 'incident.opened' from the /robots/ingest endpoint.
    """
    correlation_id = _get_correlation_id(request)

    # Auth check (Law #3: fail closed)
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.robots.list",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    # Query robot receipts from in-memory store
    robot_receipts = query_receipts(
        suite_id="system",
        limit=limit * 2,  # Over-fetch to allow filtering
    )

    # Filter for robot-related receipts
    robot_runs = []
    for r in robot_receipts:
        action = r.get("action_type", "")
        if "robot.run" in action or (action == "incident.opened" and r.get("tool_used") == "robot_runner"):
            entry = {
                "run_id": r.get("correlation_id", ""),
                "status": r.get("outcome", "unknown"),
                "action_type": action,
                "env": (r.get("redacted_inputs") or {}).get("env", "unknown"),
                "version_ref": (r.get("redacted_inputs") or {}).get("version_ref", "unknown"),
                "scenario_count": (r.get("redacted_inputs") or {}).get("scenario_count", 0),
                "summary": (r.get("redacted_outputs") or {}).get("summary", ""),
                "created_at": r.get("created_at", ""),
                "receipt_id": r.get("id", ""),
            }

            # Apply filters
            if status and entry["status"].lower() != status.lower():
                continue
            if env and entry["env"].lower() != env.lower():
                continue

            robot_runs.append(entry)

    robot_runs = robot_runs[:limit]

    # Access receipt (Law #2)
    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.robots.list",
        outcome="success",
        details={"count": len(robot_runs), "filters": {"status": status, "env": env}},
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "items": robot_runs,
            "page": {"has_more": len(robot_runs) == limit, "next_cursor": None},
            "server_time": _now_iso(),
        },
    )


@router.get("/admin/ops/robots/{run_id}")
async def get_robot_run(
    request: Request,
    run_id: str,
) -> JSONResponse:
    """Get detailed robot run by run_id (correlation_id).

    Returns all receipts associated with this robot run, plus timeline.
    """
    correlation_id = _get_correlation_id(request)

    # Auth check (Law #3: fail closed)
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.robots.detail",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    # Fetch all receipts for this run
    run_receipts = query_receipts(
        suite_id="system",
        correlation_id=run_id,
        limit=100,
    )

    if not run_receipts:
        return _ops_error(
            code="NOT_FOUND",
            message=f"Robot run {run_id} not found",
            correlation_id=correlation_id,
            status_code=404,
        )

    # Build timeline from receipts
    timeline = []
    for r in run_receipts:
        timeline.append({
            "timestamp": r.get("created_at", ""),
            "event": r.get("action_type", ""),
            "status": r.get("outcome", ""),
            "receipt_id": r.get("id", ""),
            "detail": (r.get("redacted_outputs") or {}).get("summary", ""),
        })
    timeline.sort(key=lambda x: x["timestamp"])

    # Extract run metadata from first receipt
    first = run_receipts[0] if run_receipts else {}
    run_detail = {
        "run_id": run_id,
        "status": first.get("outcome", "unknown"),
        "env": (first.get("redacted_inputs") or {}).get("env", "unknown"),
        "version_ref": (first.get("redacted_inputs") or {}).get("version_ref", "unknown"),
        "scenario_count": (first.get("redacted_inputs") or {}).get("scenario_count", 0),
        "timeline": timeline,
        "receipt_count": len(run_receipts),
        "created_at": first.get("created_at", ""),
    }

    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.robots.detail",
        outcome="success",
        details={"run_id": run_id},
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "run": run_detail,
            "server_time": _now_iso(),
        },
    )


# =============================================================================
# Admin Ava Ops Desk (Wave 6 — LLM OPS DESK endpoints)
# =============================================================================


@router.get("/admin/ops/health-pulse")
async def admin_health_pulse(request: Request) -> JSONResponse:
    """Admin Ava health pulse — aggregate platform health.

    Returns structured health report for the LLM OPS DESK.
    Includes subsystem status, metrics, and voice_id for TTS.
    """
    correlation_id = _get_correlation_id(request)

    # Auth check (Law #3: fail closed)
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.health_pulse",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    from aspire_orchestrator.skillpacks.ava_admin_desk import get_ava_admin_desk
    from aspire_orchestrator.services.agent_sdk_base import AgentContext

    admin_desk = get_ava_admin_desk()
    ctx = AgentContext(
        suite_id="system",
        office_id="system",
        correlation_id=correlation_id,
        risk_tier="green",
    )

    result = await admin_desk.get_health_pulse(ctx)

    if not result.success:
        return _ops_error(
            code="INTERNAL_ERROR",
            message=result.error or "Health pulse failed",
            correlation_id=correlation_id,
            status_code=500,
        )

    return JSONResponse(
        status_code=200,
        content={
            "pulse": result.data,
            "server_time": _now_iso(),
        },
    )


@router.get("/admin/ops/triage/{incident_id}")
async def admin_triage_incident(
    request: Request,
    incident_id: str,
) -> JSONResponse:
    """Admin Ava incident triage — Incident Commander Mode.

    Returns structured triage report with evidence, hypotheses, and recommendations.
    """
    correlation_id = _get_correlation_id(request)

    # Auth check (Law #3: fail closed)
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.triage",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    from aspire_orchestrator.skillpacks.ava_admin_desk import get_ava_admin_desk
    from aspire_orchestrator.services.agent_sdk_base import AgentContext

    admin_desk = get_ava_admin_desk()
    ctx = AgentContext(
        suite_id="system",
        office_id="system",
        correlation_id=correlation_id,
        risk_tier="green",
    )

    result = await admin_desk.triage_incident(ctx, incident_id=incident_id)

    if not result.success:
        status = 404 if "not found" in (result.error or "").lower() else 500
        return _ops_error(
            code="NOT_FOUND" if status == 404 else "INTERNAL_ERROR",
            message=result.error or "Triage failed",
            correlation_id=correlation_id,
            status_code=status,
        )

    return JSONResponse(
        status_code=200,
        content={
            "triage": result.data,
            "server_time": _now_iso(),
        },
    )


@router.get("/admin/ops/provider-analysis")
async def admin_provider_analysis(
    request: Request,
    provider: str | None = Query(None, description="Filter by provider name"),
    limit: int = Query(100, ge=1, le=500),
) -> JSONResponse:
    """Admin Ava provider error analysis — detect patterns and spikes."""
    correlation_id = _get_correlation_id(request)

    # Auth check (Law #3: fail closed)
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.provider_analysis",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    from aspire_orchestrator.skillpacks.ava_admin_desk import get_ava_admin_desk
    from aspire_orchestrator.services.agent_sdk_base import AgentContext

    admin_desk = get_ava_admin_desk()
    ctx = AgentContext(
        suite_id="system",
        office_id="system",
        correlation_id=correlation_id,
        risk_tier="green",
    )

    result = await admin_desk.analyze_provider_errors(ctx, provider=provider, limit=limit)

    if not result.success:
        return _ops_error(
            code="INTERNAL_ERROR",
            message=result.error or "Provider analysis failed",
            correlation_id=correlation_id,
            status_code=500,
        )

    return JSONResponse(
        status_code=200,
        content={
            "analysis": result.data,
            "server_time": _now_iso(),
        },
    )
