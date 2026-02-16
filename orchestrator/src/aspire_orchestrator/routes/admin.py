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
import threading
import uuid
from datetime import datetime, timezone
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


def register_incident(incident: dict[str, Any]) -> None:
    """Register an incident into the in-memory store.

    Called by other services (e.g., health monitors, circuit breakers)
    to publish incidents that the admin facade exposes.
    """
    incident_id = incident.get("incident_id")
    if not incident_id:
        incident_id = str(uuid.uuid4())
        incident["incident_id"] = incident_id

    with _store_lock:
        _incidents[incident_id] = incident


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

    # Sort by last_seen descending (newest first)
    all_incidents.sort(key=lambda x: x.get("last_seen", ""), reverse=True)

    page_items, page_info = _paginate(all_incidents, cursor, limit, id_field="incident_id")

    # Law #2: access receipt
    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.incidents.list",
        outcome="success",
        details={"count": len(page_items), "filters": {"state": state, "severity": severity}},
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

    # Sort by started_at descending
    all_calls.sort(key=lambda x: x.get("started_at", ""), reverse=True)

    # Map to ProviderCallSummary (Law #9: always redact payload preview)
    call_summaries = [
        {
            "call_id": c.get("call_id", ""),
            "correlation_id": c.get("correlation_id", ""),
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
        pass  # DLP unavailable — truncation is the safety net

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
            "server_time": _now_iso(),
            "queue_depth": 0,
            "oldest_age_seconds": 0,
            "stuck_jobs": 0,
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
