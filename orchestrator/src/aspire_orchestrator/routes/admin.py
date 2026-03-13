"""Admin Ops Telemetry Facade API — Wave 8.

Read-only, LLM-safe telemetry facade for the Aspire Admin portal (Ava Admin).
Implements the OpenAPI spec at plan/contracts/ava-admin/ops_telemetry_facade.openapi.yaml.

Endpoints (10 total, admin facade read-only except proposal approval, plus one internal ingest path):
  GET  /admin/ops/health          — Health check (no auth)
  GET  /admin/ops/incidents       — List incidents (filtered, paginated)
  POST /admin/ops/incidents/report — Internal incident ingest for desktop/gateway surfaces
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
  - Law #7: Read-only facade for admin consumers — the ingest path is service-only and stores telemetry, not decisions.
  - Law #9: PII redacted in receipt/provider-call previews.
"""

from __future__ import annotations

import logging
import hmac
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncio

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from aspire_orchestrator.services.receipt_store import query_receipts, store_receipts
from aspire_orchestrator.services.outbox_client import get_outbox_client
from aspire_orchestrator.services.a2a_service import get_a2a_service

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
    "builder_primary_model": os.environ.get("ASPIRE_BUILDER_PRIMARY_MODEL", "gpt-5-mini"),
    "builder_fallback_model": os.environ.get("ASPIRE_BUILDER_FALLBACK_MODEL", "claude-sonnet-4.6"),
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
    try:
        with _provider_health_lock:
            _provider_health.clear()
    except NameError:
        pass


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


def _compare_tokens(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _get_incident_reporter_secrets() -> list[str]:
    secrets = [
        os.environ.get("ASPIRE_ADMIN_INCIDENT_S2S_SECRET", "").strip(),
        os.environ.get("S2S_HMAC_SECRET_ACTIVE", "").strip(),
        os.environ.get("DOMAIN_RAIL_HMAC_SECRET", "").strip(),
        os.environ.get("S2S_HMAC_SECRET", "").strip(),
    ]
    return [secret for secret in secrets if secret]


def _require_incident_reporter(request: Request) -> tuple[str | None, str]:
    actor_id = _require_admin(request)
    if actor_id is not None:
        return actor_id, "admin"

    auth_header = request.headers.get("authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        provided = auth_header.split(" ", 1)[1].strip()
        for secret in _get_incident_reporter_secrets():
            if _compare_tokens(provided, secret):
                internal_actor = (
                    request.headers.get("x-actor-id")
                    or request.headers.get("x-reporter-id")
                    or "internal_reporter"
                )
                return internal_actor, "service"

    return None, "anonymous"


def _incident_rank(severity: str) -> int:
    return {"sev1": 1, "sev2": 2, "sev3": 3, "sev4": 4}.get(severity, 4)


def _sanitize_incident_severity(value: Any) -> str:
    severity = str(value or "").strip().lower()
    if severity in {"sev1", "sev2", "sev3", "sev4"}:
        return severity
    return "sev2"


def _sanitize_incident_state(value: Any) -> str:
    state = str(value or "").strip().lower()
    if state in {"open", "investigating", "mitigated", "closed"}:
        return state
    return "open"


def _upsert_reported_incident(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    trace_id: str,
    reporter_id: str,
    receipt_id: str,
) -> tuple[dict[str, Any], bool]:
    now_iso = _now_iso()
    title = str(payload.get("title") or "").strip() or "Reported incident"
    severity = _sanitize_incident_severity(payload.get("severity"))
    state = _sanitize_incident_state(payload.get("state"))
    source = str(payload.get("source") or "external").strip() or "external"
    component = str(payload.get("component") or "unknown").strip() or "unknown"
    suite_id = str(payload.get("suite_id") or "").strip() or None
    fingerprint = str(payload.get("fingerprint") or "").strip()
    error_code = str(payload.get("error_code") or "").strip() or None
    message = str(payload.get("message") or "").strip() or None
    agent = str(payload.get("agent") or "").strip() or None
    evidence_pack = payload.get("evidence_pack") if isinstance(payload.get("evidence_pack"), dict) else {}
    timeline_event = {
        "ts": now_iso,
        "event": "reported",
        "receipt_id": receipt_id,
    }

    incident: dict[str, Any]
    deduped = False

    with _store_lock:
        existing: dict[str, Any] | None = None
        requested_incident_id = str(payload.get("incident_id") or "").strip()
        if requested_incident_id:
            existing = _incidents.get(requested_incident_id)
        if existing is None and fingerprint:
            for candidate in _incidents.values():
                if (
                    candidate.get("fingerprint") == fingerprint
                    and candidate.get("state") in {"open", "investigating", "mitigated"}
                ):
                    existing = candidate
                    break

        if existing is not None:
            deduped = True
            merged_timeline = list(existing.get("timeline") or [])
            merged_timeline.append(timeline_event)
            merged_evidence = dict(existing.get("evidence_pack") or {})
            merged_evidence.update(evidence_pack)
            merged_evidence["source"] = source
            merged_evidence["component"] = component
            merged_evidence["last_reporter"] = reporter_id
            merged_evidence["report_count"] = int(merged_evidence.get("report_count", 1)) + 1
            if error_code:
                merged_evidence["error_code"] = error_code
            if message:
                merged_evidence["message"] = message

            incident = dict(existing)
            incident["title"] = title or incident.get("title", "Reported incident")
            incident["severity"] = severity if _incident_rank(severity) < _incident_rank(str(existing.get("severity", "sev4"))) else existing.get("severity", severity)
            incident["state"] = state if state != "closed" else existing.get("state", "open")
            incident["last_seen"] = now_iso
            incident["correlation_id"] = correlation_id
            incident["trace_id"] = trace_id or correlation_id
            incident["suite_id"] = suite_id or incident.get("suite_id")
            incident["timeline"] = merged_timeline
            incident["fingerprint"] = fingerprint or incident.get("fingerprint")
            incident["evidence_pack"] = merged_evidence
            if agent:
                incident["agent"] = agent
            _incidents[str(incident["incident_id"])] = incident
        else:
            incident_id = requested_incident_id or str(uuid.uuid4())
            incident = {
                "incident_id": incident_id,
                "state": state,
                "severity": severity,
                "title": title,
                "correlation_id": correlation_id,
                "trace_id": trace_id or correlation_id,
                "suite_id": suite_id,
                "first_seen": now_iso,
                "last_seen": now_iso,
                "timeline": [timeline_event],
                "fingerprint": fingerprint or f"{source}:{component}:{error_code or title.lower()}",
                "agent": agent,
                "evidence_pack": {
                    **evidence_pack,
                    "source": source,
                    "component": component,
                    "last_reporter": reporter_id,
                    "report_count": 1,
                    **({"error_code": error_code} if error_code else {}),
                    **({"message": message} if message else {}),
                },
            }
            _incidents[incident_id] = incident

    try:
        from aspire_orchestrator.services.admin_store import get_admin_store
        store = get_admin_store()
        store._incidents[str(incident["incident_id"])] = incident
    except Exception:
        pass

    return incident, deduped


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
# 3. POST /admin/ops/incidents/report — Internal Incident Ingest
# =============================================================================


@router.post("/admin/ops/incidents/report")
async def report_incident(request: Request) -> JSONResponse:
    """Internal/admin incident ingest for fast cross-surface visibility."""
    correlation_id = _get_correlation_id(request)
    trace_id = request.headers.get("x-trace-id", correlation_id).strip() or correlation_id
    actor_id, actor_type = _require_incident_reporter(request)
    if actor_id is None:
        denied_receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.incidents.report",
            outcome="denied",
            reason_code="AUTHZ_DENIED",
        )
        store_receipts([denied_receipt])
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid incident reporter credentials",
            correlation_id=correlation_id,
            status_code=401,
        )

    try:
        payload = await request.json()
    except Exception:
        return _ops_error(
            code="VALIDATION_ERROR",
            message="Invalid JSON body",
            correlation_id=correlation_id,
            status_code=400,
        )

    if not isinstance(payload, dict):
        return _ops_error(
            code="VALIDATION_ERROR",
            message="Incident report body must be an object",
            correlation_id=correlation_id,
            status_code=400,
        )

    title = str(payload.get("title") or "").strip()
    if not title:
        return _ops_error(
            code="VALIDATION_ERROR",
            message="title is required",
            correlation_id=correlation_id,
            status_code=400,
        )

    receipt_id = str(uuid.uuid4())
    incident, deduped = _upsert_reported_incident(
        payload=payload,
        correlation_id=str(payload.get("correlation_id") or correlation_id).strip() or correlation_id,
        trace_id=str(payload.get("trace_id") or trace_id).strip() or trace_id,
        reporter_id=actor_id,
        receipt_id=receipt_id,
    )

    receipt = {
        "id": receipt_id,
        "correlation_id": incident.get("correlation_id", correlation_id),
        "suite_id": incident.get("suite_id") or "system",
        "office_id": "system",
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action_type": "admin.ops.incidents.report",
        "risk_tier": "yellow",
        "tool_used": "incident_reporter",
        "outcome": "success",
        "created_at": _now_iso(),
        "reason_code": "DEDUPED" if deduped else "CREATED",
        "redacted_inputs": {
            "source": incident.get("evidence_pack", {}).get("source"),
            "component": incident.get("evidence_pack", {}).get("component"),
            "severity": incident.get("severity"),
        },
        "redacted_outputs": {
            "incident_id": incident.get("incident_id"),
            "deduped": deduped,
        },
    }
    store_receipts([receipt])

    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "incident_id": incident.get("incident_id"),
            "deduped": deduped,
            "state": incident.get("state"),
            "severity": incident.get("severity"),
            "correlation_id": incident.get("correlation_id", correlation_id),
            "trace_id": incident.get("trace_id", trace_id),
            "receipt_id": receipt_id,
            "server_time": _now_iso(),
        },
    )


# =============================================================================
# 4. GET /admin/ops/incidents/{incident_id} — Incident Detail
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


@router.get("/admin/ops/readiness-contract")
async def readiness_contract(request: Request) -> JSONResponse:
    """Expose runtime backend contract for enterprise operations checks."""
    correlation_id = _get_correlation_id(request)
    actor_id = _require_admin(request)
    if actor_id is None:
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    aspire_env = (os.environ.get("ASPIRE_ENV") or "").strip().lower() or "development"
    redis_url = (os.environ.get("ASPIRE_REDIS_URL") or os.environ.get("REDIS_URL") or "").strip()
    rate_limiter_backend = "redis" if redis_url else "memory"
    outbox_backend = get_outbox_client().backend
    a2a_backend = get_a2a_service().backend
    checkpointer_backend = (os.environ.get("ASPIRE_LANGGRAPH_CHECKPOINTER") or "memory").strip().lower()

    replica_safe = (
        rate_limiter_backend == "redis"
        and outbox_backend == "supabase"
        and a2a_backend == "supabase"
        and checkpointer_backend == "postgres"
    )

    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.readiness_contract.get",
        outcome="success",
        details={
            "replica_safe": replica_safe,
            "rate_limiter_backend": rate_limiter_backend,
            "outbox_backend": outbox_backend,
            "a2a_backend": a2a_backend,
            "checkpointer_backend": checkpointer_backend,
        },
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "environment": aspire_env,
            "replica_safe": replica_safe,
            "rate_limiter_backend": rate_limiter_backend,
            "outbox_backend": outbox_backend,
            "a2a_backend": a2a_backend,
            "checkpointer_backend": checkpointer_backend,
            "server_time": _now_iso(),
        },
    )


@router.get("/admin/ops/voice/config")
async def voice_config(request: Request) -> JSONResponse:
    """Return effective voice configuration for Admin Ops Desk."""
    correlation_id = _get_correlation_id(request)
    actor_id = _require_admin(request)
    if actor_id is None:
        return _ops_error(
            code="AUTHZ_DENIED",
            message="Missing or invalid admin token",
            correlation_id=correlation_id,
            status_code=401,
        )

    has_key = bool((os.environ.get("ASPIRE_ELEVENLABS_API_KEY") or os.environ.get("ELEVENLABS_API_KEY") or "").strip())
    voices = {
        "ava": os.environ.get("ASPIRE_AVA_VOICE_ID") or os.environ.get("AVA_VOICE_ID") or "",
        "finn": os.environ.get("ASPIRE_FINN_VOICE_ID") or os.environ.get("FINN_VOICE_ID") or "",
        "nora": os.environ.get("ASPIRE_NORA_VOICE_ID") or os.environ.get("NORA_VOICE_ID") or "",
        "eli": os.environ.get("ASPIRE_ELI_VOICE_ID") or os.environ.get("ELI_VOICE_ID") or "",
    }
    configured_agents = sorted([agent for agent, voice_id in voices.items() if isinstance(voice_id, str) and voice_id.strip()])

    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.voice.config.get",
        outcome="success",
        details={"has_elevenlabs_key": has_key, "configured_agents": configured_agents},
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "provider": "elevenlabs",
            "configured": has_key and len(configured_agents) > 0,
            "has_api_key": has_key,
            "configured_agents": configured_agents,
            "voices": voices,
            "server_time": _now_iso(),
        },
    )


# =============================================================================
# 7A. GET /admin/ops/providers — Provider Connectivity Snapshot
# =============================================================================

_PROVIDER_ALIAS_MAP: dict[str, str] = {
    "qbo": "quickbooks",
}

_PROVIDER_RUNTIME_CATALOG: tuple[dict[str, Any], ...] = (
    {"provider": "openai", "lane": "ai", "env_vars": ("ASPIRE_OPENAI_API_KEY", "OPENAI_API_KEY"), "rotation_mode": "automated", "secret_source": "aws_secrets_manager"},
    {"provider": "elevenlabs", "lane": "voice", "env_vars": ("ASPIRE_ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "deepgram", "lane": "transcription", "env_vars": ("ASPIRE_DEEPGRAM_API_KEY", "DEEPGRAM_API_KEY"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "livekit", "lane": "conference", "env_vars": ("ASPIRE_LIVEKIT_API_KEY", "LIVEKIT_API_KEY"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "twilio", "lane": "telephony", "env_vars": ("ASPIRE_TWILIO_ACCOUNT_SID", "TWILIO_ACCOUNT_SID"), "rotation_mode": "automated", "secret_source": "aws_secrets_manager"},
    {"provider": "stripe", "lane": "payments", "env_vars": ("ASPIRE_STRIPE_API_KEY", "STRIPE_SECRET_KEY"), "rotation_mode": "automated", "secret_source": "aws_secrets_manager"},
    {"provider": "plaid", "lane": "banking", "env_vars": ("ASPIRE_PLAID_CLIENT_ID", "PLAID_CLIENT_ID"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "pandadoc", "lane": "documents", "env_vars": ("ASPIRE_PANDADOC_API_KEY", "PANDADOC_API_KEY"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "quickbooks", "lane": "accounting", "env_vars": ("ASPIRE_QUICKBOOKS_CLIENT_ID", "QUICKBOOKS_CLIENT_ID"), "aliases": ("qbo",), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "gusto", "lane": "payroll", "env_vars": ("ASPIRE_GUSTO_CLIENT_ID", "GUSTO_CLIENT_ID"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "brave", "lane": "search", "env_vars": ("ASPIRE_BRAVE_API_KEY", "BRAVE_API_KEY"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "tavily", "lane": "search", "env_vars": ("ASPIRE_TAVILY_API_KEY", "TAVILY_API_KEY"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "google_places", "lane": "places", "env_vars": ("ASPIRE_GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_API_KEY"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "here", "lane": "places", "env_vars": ("ASPIRE_HERE_API_KEY", "HERE_API_KEY"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "foursquare", "lane": "places", "env_vars": ("ASPIRE_FOURSQUARE_API_KEY", "FOURSQUARE_API_KEY"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "mapbox", "lane": "maps", "env_vars": ("ASPIRE_MAPBOX_ACCESS_TOKEN", "MAPBOX_ACCESS_TOKEN"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "tomtom", "lane": "places", "env_vars": ("ASPIRE_TOMTOM_API_KEY", "TOMTOM_API_KEY"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "anam", "lane": "avatar", "env_vars": ("ASPIRE_ANAM_API_KEY", "ANAM_API_KEY"), "rotation_mode": "manual_alerted", "secret_source": "aws_secrets_manager"},
    {"provider": "supabase", "lane": "database", "env_vars": ("ASPIRE_SUPABASE_URL", "SUPABASE_URL"), "always_include": True, "rotation_mode": "automated", "secret_source": "aws_secrets_manager"},
    {"provider": "n8n", "lane": "automation", "env_vars": ("N8N_API_KEY", "N8N_WEBHOOK_SECRET"), "always_include": True, "rotation_mode": "manual_alerted", "secret_source": "railway_runtime"},
    {"provider": "railway", "lane": "infrastructure", "env_vars": ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_NAME", "ASPIRE_DOMAIN_RAIL_URL"), "always_include": True, "rotation_mode": "infrastructure", "secret_source": "railway_runtime"},
    {"provider": "secret_manager", "lane": "security", "env_vars": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"), "always_include": True, "rotation_mode": "infrastructure", "secret_source": "aws_secrets_manager"},
)


def _normalize_provider_key(value: Any) -> str:
    key = str(value or "").strip().lower()
    if not key:
        return ""
    return _PROVIDER_ALIAS_MAP.get(key, key)


def _provider_env_configured(meta: dict[str, Any]) -> bool:
    return any(str(os.environ.get(name, "")).strip() for name in meta.get("env_vars", ()))


def _make_provider_snapshot_item(
    *,
    provider: str,
    lane: str,
    status: str,
    connection_status: str,
    last_checked: str | None = None,
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "lane": lane,
        "status": status,
        "connection_status": connection_status,
        "scopes": scopes or [],
        "last_checked": last_checked,
        "latency_ms": 0,
        "p95_latency_ms": 0,
        "error_rate": 0.0,
        "webhook_error_rate": 0.0,
        "rotation_mode": "unknown",
        "secret_source": "unknown",
        "production_verified": False,
    }


def _build_runtime_provider_items() -> dict[str, dict[str, Any]]:
    now_iso = _now_iso()
    items: dict[str, dict[str, Any]] = {}

    for meta in _PROVIDER_RUNTIME_CATALOG:
        provider = meta["provider"]
        configured = _provider_env_configured(meta)
        if not configured and not bool(meta.get("always_include")):
            continue

        if provider == "secret_manager":
            connection_status = "aws_sm" if configured else "unknown"
        elif provider == "railway":
            connection_status = "configured" if configured else "unknown"
        elif provider == "n8n":
            connection_status = "configured" if configured else "unknown"
        else:
            connection_status = "configured" if configured else "unknown"

        items[provider] = _make_provider_snapshot_item(
            provider=provider,
            lane=str(meta.get("lane") or "unknown"),
            status="connected" if configured else "disconnected",
            connection_status=connection_status,
            last_checked=now_iso,
        )
        items[provider]["rotation_mode"] = str(meta.get("rotation_mode") or "unknown")
        items[provider]["secret_source"] = str(meta.get("secret_source") or "unknown")
        items[provider]["production_verified"] = configured

        for alias in meta.get("aliases", ()):
            _PROVIDER_ALIAS_MAP.setdefault(str(alias).strip().lower(), provider)

    return items


def _overlay_live_provider_health(items_map: dict[str, dict[str, Any]]) -> None:
    with _provider_health_lock:
        current_health = list(_provider_health.values())

    for live in current_health:
        key = _normalize_provider_key(live.get("provider"))
        if not key:
            continue
        item = items_map.setdefault(
            key,
            _make_provider_snapshot_item(
                provider=key,
                lane=str(live.get("lane") or "unknown"),
                status="disconnected",
                connection_status="unknown",
            ),
        )
        item["lane"] = str(live.get("lane") or item.get("lane") or "unknown")
        item["status"] = str(live.get("status") or item.get("status") or "disconnected")
        item["connection_status"] = item["status"]
        item["last_checked"] = live.get("lastChecked") or item.get("last_checked")
        latency_ms = int(live.get("latencyMs") or 0)
        item["latency_ms"] = latency_ms
        item["p95_latency_ms"] = max(int(item.get("p95_latency_ms") or 0), latency_ms)
        item["error_rate"] = float(live.get("errorRate") or item.get("error_rate") or 0.0)
        item["production_verified"] = bool(item.get("production_verified")) or item["status"] in {"connected", "degraded"}


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
        key = _normalize_provider_key(row.get("provider") or row.get("provider_name"))
        if not key:
            continue
        conn_status = str(
            row.get("connection_status") or row.get("status") or row.get("state") or "connected"
        ).strip().lower()
        connected = conn_status in {"connected", "active", "healthy", "ok"}
        items_map[key] = _make_provider_snapshot_item(
            provider=key,
            lane=str(row.get("provider_type") or row.get("lane") or "unknown"),
            status="connected" if connected else "disconnected",
            connection_status=conn_status,
            last_checked=row.get("last_webhook_at") or row.get("updated_at") or row.get("created_at"),
            scopes=list(row.get("scopes") or []),
        )

    for key, item in _build_runtime_provider_items().items():
        existing = items_map.setdefault(key, item)
        existing["rotation_mode"] = item.get("rotation_mode", existing.get("rotation_mode", "unknown"))
        existing["secret_source"] = item.get("secret_source", existing.get("secret_source", "unknown"))
        existing["production_verified"] = bool(existing.get("production_verified")) or bool(item.get("production_verified"))

    for key, item in items_map.items():
        call_stats = stats_by_provider.get(key)
        if call_stats and call_stats["total"] > 0:
            error_rate = round((call_stats["failures"] / call_stats["total"]) * 100, 2)
            latency = int(call_stats["sum_ms"] / call_stats["total"])
            item["latency_ms"] = latency
            item["p95_latency_ms"] = int(call_stats["max_ms"])
            item["error_rate"] = error_rate
            if str(item.get("status")) == "disconnected":
                item["status"] = "connected"
                item["connection_status"] = "recent_activity"
            if item["status"] == "connected" and (error_rate >= 5.0 or latency >= 2000):
                item["status"] = "degraded"
        wb_stats = webhook_by_provider.get(key)
        if wb_stats and wb_stats["total"] > 0:
            item["webhook_error_rate"] = round((wb_stats["failed"] / wb_stats["total"]) * 100, 2)
            if item["status"] == "connected" and item["webhook_error_rate"] >= 5.0:
                item["status"] = "degraded"

    _overlay_live_provider_health(items_map)

    items = list(items_map.values())
    if provider:
        wanted = _normalize_provider_key(provider)
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


# =============================================================================
# SSE Streaming Endpoints (Wave 8 — Admin Portal Realtime)
# =============================================================================
#
# Enterprise-grade SSE streaming with full governance compliance:
#   1. Admin auth (Law #3: fail closed)
#   2. Connection tracking (per-tenant limit via sse_manager)
#   3. Initial state snapshot on connect
#   4. Heartbeat every 15s (prevents proxy/LB timeouts)
#   5. Push new events via polling internal stores
#   6. Rate limiting (10 events/s via StreamRateLimiter)
#   7. Receipts for stream lifecycle AND errors (Law #2)
#   8. PII redaction on all outbound data (Law #9)
#   9. Max stream duration enforcement (30 min — reconnect expected)
#  10. Structured logging with correlation IDs (Gate 2: Observability)
#  11. Graceful disconnect on client abort (asyncio.CancelledError)
#  12. Correlation ID propagation on every SSE event
# =============================================================================

from aspire_orchestrator.services.sse_manager import (
    format_sse_event,
    build_stream_receipt,
    get_connection_tracker,
    StreamRateLimiter,
    redact_pii,
    HEARTBEAT_INTERVAL_SECONDS,
)

# Maximum stream duration before server-initiated close (client should reconnect)
_MAX_STREAM_DURATION_SECONDS = 30 * 60  # 30 minutes


def _sse_auth_deny(correlation_id: str, stream_type: str) -> JSONResponse:
    """Return a 401 response and store denial receipt (Law #2 + #3)."""
    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id="anonymous",
        action_type=f"admin.ops.{stream_type}.stream",
        outcome="denied",
        reason_code="AUTHZ_DENIED",
    )
    store_receipts([receipt])
    logger.warning(
        "SSE auth denied: stream=%s correlation=%s",
        stream_type, correlation_id[:12],
    )
    return JSONResponse(
        status_code=401,
        content={
            "code": "AUTHZ_DENIED",
            "message": "Missing or invalid admin token",
            "correlation_id": correlation_id,
        },
    )


def _sse_connection_deny(correlation_id: str, actor_id: str, stream_type: str, suite_id: str) -> JSONResponse:
    """Return a 429 response when connection limit is exceeded (Law #3)."""
    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type=f"admin.ops.{stream_type}.stream",
        outcome="denied",
        reason_code="CONNECTION_LIMIT",
        details={"suite_id": suite_id},
    )
    store_receipts([receipt])
    logger.warning(
        "SSE connection limit: stream=%s suite=%s actor=%s",
        stream_type, suite_id[:8], actor_id[:12],
    )
    return JSONResponse(
        status_code=429,
        content={
            "code": "CONNECTION_LIMIT",
            "message": "Too many concurrent SSE connections for this tenant",
            "correlation_id": correlation_id,
            "retryable": True,
        },
    )


def _sse_stream_init(
    stream_type: str, suite_id: str, actor_id: str, correlation_id: str, stream_id: str,
) -> None:
    """Log and receipt for SSE stream initiation."""
    init_receipt = build_stream_receipt(
        action_type="stream.initiate",
        suite_id=suite_id,
        office_id="admin",
        actor_id=actor_id,
        correlation_id=correlation_id,
        outcome="success",
        stream_id=stream_id,
        details={"stream_type": stream_type},
    )
    store_receipts([init_receipt])
    logger.info(
        "SSE stream opened: type=%s stream=%s suite=%s actor=%s corr=%s",
        stream_type, stream_id[:8], suite_id[:8], actor_id[:12], correlation_id[:12],
    )


def _sse_stream_close(
    stream_type: str, suite_id: str, actor_id: str, correlation_id: str,
    stream_id: str, tracker: Any, reason: str = "client_disconnect",
) -> None:
    """Clean up connection tracking, log, and receipt on stream close."""
    tracker.disconnect(suite_id, stream_id)
    meta = tracker.get_metadata(stream_id)
    event_count = (meta or {}).get("event_count", 0) if meta else 0
    close_receipt = build_stream_receipt(
        action_type="stream.complete",
        suite_id=suite_id,
        office_id="admin",
        actor_id=actor_id,
        correlation_id=correlation_id,
        outcome="success",
        stream_id=stream_id,
        details={"stream_type": stream_type, "reason": reason, "events_sent": event_count},
    )
    store_receipts([close_receipt])
    logger.info(
        "SSE stream closed: type=%s stream=%s reason=%s events=%d",
        stream_type, stream_id[:8], reason, event_count,
    )


# ---------------------------------------------------------------------------
# 1. GET /admin/ops/incidents/stream — Live error/incident feed
# ---------------------------------------------------------------------------


@router.get("/admin/ops/incidents/stream")
async def stream_incidents(request: Request) -> StreamingResponse:
    """SSE stream of new incidents and errors.

    Frontend consumer: useErrorStream.ts
    Event shape: {id, severity, message, timestamp, correlation_id, stack_trace, provider}

    Enterprise features:
      - Max 30min stream duration (client reconnects automatically)
      - Correlation ID on every event for traceability
      - Rate-limited to 10 events/s
      - PII redacted (Law #9)
      - Connection tracked per-tenant (Law #6)
    """
    correlation_id = _get_correlation_id(request)
    actor_id = _require_admin(request)
    if actor_id is None:
        return _sse_auth_deny(correlation_id, "incidents")

    stream_id = str(uuid.uuid4())
    suite_id = request.headers.get("x-suite-id", "system")
    tracker = get_connection_tracker()

    if not tracker.try_connect(suite_id, stream_id, actor_id=actor_id, correlation_id=correlation_id):
        return _sse_connection_deny(correlation_id, actor_id, "incidents", suite_id)

    _sse_stream_init("incidents", suite_id, actor_id, correlation_id, stream_id)

    async def event_generator():
        limiter = StreamRateLimiter()
        last_heartbeat = time.monotonic()
        stream_start = time.monotonic()
        seen_ids: set[str] = set()

        try:
            # Initial snapshot of recent incidents
            with _store_lock:
                snapshot = list(_incidents.values())[-50:]
            for inc in snapshot:
                event = {
                    "id": inc.get("incident_id", ""),
                    "severity": _map_severity(inc.get("severity", "sev4")),
                    "message": redact_pii(inc.get("title", "")),
                    "timestamp": inc.get("last_seen", inc.get("first_seen", _now_iso())),
                    "correlation_id": inc.get("correlation_id", ""),
                    "provider": inc.get("provider", ""),
                    "stream_correlation_id": correlation_id,
                }
                seen_ids.add(event["id"])
                yield format_sse_event(event, event_type="incident")

            # Poll for new incidents
            while True:
                # Max stream duration enforcement
                if time.monotonic() - stream_start > _MAX_STREAM_DURATION_SECONDS:
                    yield format_sse_event(
                        {"type": "stream_expired", "message": "Max stream duration reached. Reconnect.", "server_time": _now_iso()},
                        event_type="control",
                    )
                    break

                await asyncio.sleep(2.0)

                # Heartbeat
                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    yield format_sse_event({"type": "heartbeat", "server_time": _now_iso()}, event_type="heartbeat")
                    last_heartbeat = now

                # Check for new incidents
                with _store_lock:
                    current = list(_incidents.values())

                for inc in current:
                    inc_id = inc.get("incident_id", "")
                    if inc_id and inc_id not in seen_ids and limiter.check():
                        seen_ids.add(inc_id)
                        tracker.increment_event_count(stream_id)
                        event = {
                            "id": inc_id,
                            "severity": _map_severity(inc.get("severity", "sev4")),
                            "message": redact_pii(inc.get("title", "")),
                            "timestamp": inc.get("last_seen", _now_iso()),
                            "correlation_id": inc.get("correlation_id", ""),
                            "provider": inc.get("provider", ""),
                            "stream_correlation_id": correlation_id,
                        }
                        yield format_sse_event(event, event_type="incident")

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("SSE incidents stream error: %s stream=%s", exc, stream_id[:8])
            err_receipt = build_stream_receipt(
                action_type="stream.error",
                suite_id=suite_id,
                office_id="admin",
                actor_id=actor_id,
                correlation_id=correlation_id,
                outcome="failed",
                stream_id=stream_id,
                reason_code="STREAM_ERROR",
                details={"error": str(exc)[:200]},
            )
            store_receipts([err_receipt])
        finally:
            _sse_stream_close("incidents", suite_id, actor_id, correlation_id, stream_id, tracker)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Stream-Id": stream_id,
            "X-Correlation-Id": correlation_id,
        },
    )


def _map_severity(sev: str) -> str:
    """Map internal severity to P0-P3 for frontend."""
    return {"sev1": "P0", "sev2": "P1", "sev3": "P2", "sev4": "P3"}.get(sev, "P3")


# ---------------------------------------------------------------------------
# 2. GET /admin/ops/providers/stream — Live provider health
# ---------------------------------------------------------------------------

# In-memory provider health store (updated by health monitors)
_provider_health_lock = threading.Lock()
_provider_health: dict[str, dict[str, Any]] = {}


def update_provider_health(provider: str, health: dict[str, Any]) -> None:
    """Update provider health status. Called by health monitors."""
    with _provider_health_lock:
        _provider_health[provider] = {
            "provider": provider,
            "lane": health.get("lane", "unknown"),
            "status": health.get("status", "connected"),
            "latencyMs": health.get("latency_ms", 0),
            "errorRate": health.get("error_rate", 0.0),
            "lastChecked": _now_iso(),
            "lastSuccessfulCall": health.get("last_successful_call"),
        }


def _build_stream_provider_snapshot() -> list[dict[str, Any]]:
    defaults = _build_runtime_provider_items()
    snapshot_map: dict[str, dict[str, Any]] = {
        key: {
            "provider": item["provider"],
            "lane": item["lane"],
            "status": item["status"],
            "latencyMs": int(item.get("latency_ms") or 0),
            "errorRate": float(item.get("error_rate") or 0.0),
            "lastChecked": item.get("last_checked") or _now_iso(),
        }
        for key, item in defaults.items()
    }

    with _provider_health_lock:
        current = list(_provider_health.values())

    for live in current:
        key = _normalize_provider_key(live.get("provider"))
        if not key:
            continue
        snapshot_map[key] = {
            "provider": key,
            "lane": str(live.get("lane") or snapshot_map.get(key, {}).get("lane") or "unknown"),
            "status": str(live.get("status") or snapshot_map.get(key, {}).get("status") or "disconnected"),
            "latencyMs": int(live.get("latencyMs") or 0),
            "errorRate": float(live.get("errorRate") or 0.0),
            "lastChecked": live.get("lastChecked") or snapshot_map.get(key, {}).get("lastChecked") or _now_iso(),
            "lastSuccessfulCall": live.get("lastSuccessfulCall"),
        }

    return [snapshot_map[key] for key in sorted(snapshot_map)]


@router.get("/admin/ops/providers/stream")
async def stream_provider_health(request: Request) -> StreamingResponse:
    """SSE stream of provider connectivity status.

    Frontend consumer: useProviderHealthStream.ts
    Event shape: ProviderHealth[] (full) or ProviderHealth (single update)
    """
    correlation_id = _get_correlation_id(request)
    actor_id = _require_admin(request)
    if actor_id is None:
        return _sse_auth_deny(correlation_id, "providers")

    stream_id = str(uuid.uuid4())
    suite_id = request.headers.get("x-suite-id", "system")
    tracker = get_connection_tracker()

    if not tracker.try_connect(suite_id, stream_id, actor_id=actor_id, correlation_id=correlation_id):
        return _sse_connection_deny(correlation_id, actor_id, "providers", suite_id)

    _sse_stream_init("providers", suite_id, actor_id, correlation_id, stream_id)

    async def event_generator():
        limiter = StreamRateLimiter()
        last_heartbeat = time.monotonic()
        stream_start = time.monotonic()
        last_snapshot_hash = ""

        try:
            # Send initial full snapshot
            snapshot = _build_stream_provider_snapshot()
            if not snapshot:
                snapshot = _seed_provider_health()

            yield format_sse_event(snapshot, event_type="providers")
            last_snapshot_hash = str(hash(str(snapshot)))

            # Poll for changes
            while True:
                if time.monotonic() - stream_start > _MAX_STREAM_DURATION_SECONDS:
                    yield format_sse_event(
                        {"type": "stream_expired", "message": "Max stream duration reached. Reconnect.", "server_time": _now_iso()},
                        event_type="control",
                    )
                    break

                await asyncio.sleep(5.0)

                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    yield format_sse_event({"type": "heartbeat", "server_time": _now_iso()}, event_type="heartbeat")
                    last_heartbeat = now

                current = _build_stream_provider_snapshot()

                current_hash = str(hash(str(current)))
                if current_hash != last_snapshot_hash and limiter.check():
                    tracker.increment_event_count(stream_id)
                    yield format_sse_event(current, event_type="providers")
                    last_snapshot_hash = current_hash

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("SSE providers stream error: %s stream=%s", exc, stream_id[:8])
            err_receipt = build_stream_receipt(
                action_type="stream.error",
                suite_id=suite_id,
                office_id="admin",
                actor_id=actor_id,
                correlation_id=correlation_id,
                outcome="failed",
                stream_id=stream_id,
                reason_code="STREAM_ERROR",
                details={"error": str(exc)[:200]},
            )
            store_receipts([err_receipt])
        finally:
            _sse_stream_close("providers", suite_id, actor_id, correlation_id, stream_id, tracker)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Stream-Id": stream_id,
            "X-Correlation-Id": correlation_id,
        },
    )


def _seed_provider_health() -> list[dict[str, Any]]:
    """Seed provider health from the same runtime provider catalog as the snapshot API."""
    providers = list(_build_runtime_provider_items().values())
    result: list[dict[str, Any]] = []
    for p in sorted(providers, key=lambda item: str(item.get("provider", ""))):
        entry = {
            "provider": p["provider"],
            "lane": p["lane"],
            "status": p["status"],
            "latencyMs": int(p.get("latency_ms") or 0),
            "errorRate": float(p.get("error_rate") or 0.0),
            "lastChecked": p.get("last_checked") or _now_iso(),
        }
        result.append(entry)
        with _provider_health_lock:
            _provider_health[p["provider"]] = entry
    return result


# ---------------------------------------------------------------------------
# 3. GET /admin/ops/health-pulse/stream — Live health metrics
# ---------------------------------------------------------------------------


@router.get("/admin/ops/health-pulse/stream")
async def stream_health_pulse(request: Request) -> StreamingResponse:
    """SSE stream of aggregate health pulse metrics.

    Frontend consumer: Dashboard page (useSSEStream)
    Pushes health snapshot every 5s.
    """
    correlation_id = _get_correlation_id(request)
    actor_id = _require_admin(request)
    if actor_id is None:
        return _sse_auth_deny(correlation_id, "health_pulse")

    stream_id = str(uuid.uuid4())
    suite_id = request.headers.get("x-suite-id", "system")
    tracker = get_connection_tracker()

    if not tracker.try_connect(suite_id, stream_id, actor_id=actor_id, correlation_id=correlation_id):
        return _sse_connection_deny(correlation_id, actor_id, "health_pulse", suite_id)

    _sse_stream_init("health_pulse", suite_id, actor_id, correlation_id, stream_id)

    async def event_generator():
        last_heartbeat = time.monotonic()
        stream_start = time.monotonic()

        try:
            while True:
                if time.monotonic() - stream_start > _MAX_STREAM_DURATION_SECONDS:
                    yield format_sse_event(
                        {"type": "stream_expired", "message": "Max stream duration reached. Reconnect.", "server_time": _now_iso()},
                        event_type="control",
                    )
                    break

                # Build health pulse from in-memory stores
                with _store_lock:
                    open_incidents = sum(1 for i in _incidents.values() if i.get("state") != "closed")
                    total_incidents = len(_incidents)

                provider_snapshot = _build_stream_provider_snapshot()
                providers_up = sum(1 for p in provider_snapshot if p.get("status") == "connected")
                providers_total = len(provider_snapshot) or 4
                degraded_count = sum(1 for p in provider_snapshot if p.get("status") == "degraded")
                disconnected_count = sum(1 for p in provider_snapshot if p.get("status") == "disconnected")

                status = "healthy"
                if disconnected_count > 0 or open_incidents > 0:
                    status = "degraded"
                if disconnected_count >= providers_total // 2:
                    status = "critical"

                pulse = {
                    "status": status,
                    "open_incidents": open_incidents,
                    "total_incidents": total_incidents,
                    "providers_up": providers_up,
                    "providers_degraded": degraded_count,
                    "providers_disconnected": disconnected_count,
                    "providers_total": providers_total,
                    "server_time": _now_iso(),
                    "stream_correlation_id": correlation_id,
                }

                tracker.increment_event_count(stream_id)
                yield format_sse_event(pulse, event_type="health_pulse")

                await asyncio.sleep(5.0)

                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    yield format_sse_event({"type": "heartbeat", "server_time": _now_iso()}, event_type="heartbeat")
                    last_heartbeat = now

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("SSE health_pulse stream error: %s stream=%s", exc, stream_id[:8])
        finally:
            _sse_stream_close("health_pulse", suite_id, actor_id, correlation_id, stream_id, tracker)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Stream-Id": stream_id,
            "X-Correlation-Id": correlation_id,
        },
    )


# ---------------------------------------------------------------------------
# 4. GET /admin/ops/outbox/stream — Outbox queue changes
# ---------------------------------------------------------------------------


@router.get("/admin/ops/outbox/stream")
async def stream_outbox(request: Request) -> StreamingResponse:
    """SSE stream of outbox queue status changes.

    Frontend consumer: Outbox page (useSSEStream)
    Emits on state change or every 3s.
    """
    correlation_id = _get_correlation_id(request)
    actor_id = _require_admin(request)
    if actor_id is None:
        return _sse_auth_deny(correlation_id, "outbox")

    stream_id = str(uuid.uuid4())
    suite_id = request.headers.get("x-suite-id", "system")
    tracker = get_connection_tracker()

    if not tracker.try_connect(suite_id, stream_id, actor_id=actor_id, correlation_id=correlation_id):
        return _sse_connection_deny(correlation_id, actor_id, "outbox", suite_id)

    _sse_stream_init("outbox", suite_id, actor_id, correlation_id, stream_id)

    async def event_generator():
        last_heartbeat = time.monotonic()
        stream_start = time.monotonic()
        last_count = -1

        try:
            while True:
                if time.monotonic() - stream_start > _MAX_STREAM_DURATION_SECONDS:
                    yield format_sse_event(
                        {"type": "stream_expired", "message": "Max stream duration reached. Reconnect.", "server_time": _now_iso()},
                        event_type="control",
                    )
                    break

                # Query outbox status
                try:
                    outbox = get_outbox_client()
                    pending = outbox.get_pending_count() if outbox else 0
                    failed = outbox.get_failed_count() if outbox else 0
                    processed = outbox.get_processed_count() if outbox else 0
                except Exception:
                    pending, failed, processed = 0, 0, 0

                current_count = pending + failed
                if current_count != last_count:
                    tracker.increment_event_count(stream_id)
                    event = {
                        "pending": pending,
                        "failed": failed,
                        "processed": processed,
                        "server_time": _now_iso(),
                        "stream_correlation_id": correlation_id,
                    }
                    yield format_sse_event(event, event_type="outbox")
                    last_count = current_count

                await asyncio.sleep(3.0)

                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    yield format_sse_event({"type": "heartbeat", "server_time": _now_iso()}, event_type="heartbeat")
                    last_heartbeat = now

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("SSE outbox stream error: %s stream=%s", exc, stream_id[:8])
        finally:
            _sse_stream_close("outbox", suite_id, actor_id, correlation_id, stream_id, tracker)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Stream-Id": stream_id,
            "X-Correlation-Id": correlation_id,
        },
    )


# ---------------------------------------------------------------------------
# 5. GET /admin/ops/council/{session_id} — Meeting of the Minds session
# ---------------------------------------------------------------------------


@router.get("/admin/ops/council/{session_id}")
async def get_council_session(request: Request, session_id: str) -> JSONResponse:
    """Get council session status and advisor proposals.

    Frontend consumer: useCouncilSession.ts (polled, not SSE)
    Returns advisor slots with proposals, confidence, and adjudication.
    """
    correlation_id = _get_correlation_id(request)
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.council.get",
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

    # Council sessions stored in receipt store keyed by correlation_id
    council_receipts = query_receipts(
        suite_id="system",
        correlation_id=session_id,
        action_type="council",
        limit=50,
    )

    if not council_receipts:
        return _ops_error(
            code="NOT_FOUND",
            message=f"Council session {session_id} not found",
            correlation_id=correlation_id,
            status_code=404,
        )

    # Build advisor slots from receipts
    advisors = []
    adjudication = None
    session_status = "deliberating"

    for r in council_receipts:
        action = r.get("action_type", "")
        outputs = r.get("redacted_outputs") or {}

        if "council.advisor" in action:
            advisors.append({
                "role": outputs.get("role", "advisor"),
                "model": outputs.get("model", "unknown"),
                "proposal": outputs.get("proposal", ""),
                "confidence": outputs.get("confidence", 0.0),
                "reasoning": outputs.get("reasoning", ""),
                "submitted_at": r.get("created_at", ""),
            })
        elif "council.adjudicate" in action:
            adjudication = {
                "decision": outputs.get("decision", ""),
                "rationale": outputs.get("rationale", ""),
                "selected_advisor": outputs.get("selected_advisor", ""),
                "decided_at": r.get("created_at", ""),
            }
            session_status = "decided"

    if len(advisors) < 3 and not adjudication:
        session_status = "collecting"

    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.council.get",
        outcome="success",
        details={"session_id": session_id},
    )
    store_receipts([receipt])

    return JSONResponse(
        status_code=200,
        content={
            "session_id": session_id,
            "status": session_status,
            "advisors": advisors,
            "adjudication": adjudication,
            "server_time": _now_iso(),
        },
    )


# ---------------------------------------------------------------------------
# 6. POST /admin/ops/voice/stt — Proxy to STT service
# ---------------------------------------------------------------------------


@router.post("/admin/ops/voice/stt")
async def voice_stt_proxy(request: Request) -> JSONResponse:
    """Proxy audio to STT service (ElevenLabs Scribe or Deepgram).

    Frontend consumer: useElevenLabsSTT.ts
    Accepts audio blob, returns transcript text.
    """
    correlation_id = _get_correlation_id(request)
    actor_id = _require_admin(request)
    if actor_id is None:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id="anonymous",
            action_type="admin.ops.voice.stt",
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
        body = await request.body()
        if not body:
            return _ops_error(
                code="VALIDATION_ERROR",
                message="Request body (audio data) is required",
                correlation_id=correlation_id,
                status_code=400,
            )

        # Determine STT provider
        stt_provider = os.environ.get("ASPIRE_STT_PROVIDER", "deepgram")

        if stt_provider == "elevenlabs":
            transcript = await _stt_elevenlabs(body, correlation_id)
        else:
            transcript = await _stt_deepgram(body, correlation_id)

        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id=actor_id,
            action_type="admin.ops.voice.stt",
            outcome="success",
            details={"provider": stt_provider, "transcript_length": len(transcript)},
        )
        store_receipts([receipt])

        return JSONResponse(
            status_code=200,
            content={
                "transcript": transcript,
                "provider": stt_provider,
                "correlation_id": correlation_id,
            },
        )

    except Exception as exc:
        logger.error("STT proxy error: %s", exc)
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id=actor_id,
            action_type="admin.ops.voice.stt",
            outcome="failed",
            reason_code="STT_ERROR",
            details={"error": str(exc)[:200]},
        )
        store_receipts([receipt])
        return _ops_error(
            code="STT_ERROR",
            message=f"STT transcription failed: {str(exc)[:200]}",
            correlation_id=correlation_id,
            status_code=502,
            retryable=True,
        )


async def _stt_deepgram(audio: bytes, correlation_id: str) -> str:
    """Transcribe audio via Deepgram Nova-3."""
    import httpx

    api_key = os.environ.get("ASPIRE_DEEPGRAM_API_KEY", "").strip()
    if not api_key:
        raise ValueError("ASPIRE_DEEPGRAM_API_KEY not configured")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true",
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "audio/webm",
            },
            content=audio,
        )
        resp.raise_for_status()
        data = resp.json()

    channels = data.get("results", {}).get("channels", [])
    if channels:
        alternatives = channels[0].get("alternatives", [])
        if alternatives:
            return alternatives[0].get("transcript", "")
    return ""


async def _stt_elevenlabs(audio: bytes, correlation_id: str) -> str:
    """Transcribe audio via ElevenLabs Scribe."""
    import httpx

    api_key = os.environ.get("ASPIRE_ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("ASPIRE_ELEVENLABS_API_KEY not configured")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": api_key},
            files={"file": ("audio.webm", audio, "audio/webm")},
            data={"model_id": "scribe_v1"},
        )
        resp.raise_for_status()
        data = resp.json()

    return data.get("text", "")


# ---------------------------------------------------------------------------
# 7. POST /admin/ops/voice/tts/stream — Streaming TTS proxy
# ---------------------------------------------------------------------------


@router.post("/admin/ops/voice/tts/stream")
async def voice_tts_stream(request: Request) -> StreamingResponse:
    """Stream TTS audio from ElevenLabs.

    Frontend consumer: useAdminVoice.ts
    Accepts JSON {text, voice_id?}, streams audio/mpeg chunks.
    """
    correlation_id = _get_correlation_id(request)
    actor_id = _require_admin(request)
    if actor_id is None:
        return JSONResponse(
            status_code=401,
            content={"code": "AUTHZ_DENIED", "message": "Missing or invalid admin token", "correlation_id": correlation_id},
        )

    try:
        body = await request.json()
    except Exception:
        return _ops_error(
            code="VALIDATION_ERROR",
            message="Request body must be JSON with 'text' field",
            correlation_id=correlation_id,
            status_code=400,
        )

    text = body.get("text", "").strip()
    if not text:
        return _ops_error(
            code="VALIDATION_ERROR",
            message="'text' field is required and must be non-empty",
            correlation_id=correlation_id,
            status_code=400,
        )

    # Default to Ava's voice
    voice_id = body.get("voice_id", os.environ.get("ASPIRE_AVA_VOICE_ID", "uYXf8XasLslADfZ2MB4u"))
    api_key = os.environ.get("ASPIRE_ELEVENLABS_API_KEY", "").strip()

    if not api_key:
        receipt = _build_access_receipt(
            correlation_id=correlation_id,
            actor_id=actor_id,
            action_type="admin.ops.voice.tts",
            outcome="failed",
            reason_code="TTS_CONFIG_MISSING",
        )
        store_receipts([receipt])
        return _ops_error(
            code="TTS_CONFIG_MISSING",
            message="ElevenLabs API key not configured",
            correlation_id=correlation_id,
            status_code=503,
        )

    # Redact PII from text before sending to TTS (Law #9)
    safe_text = redact_pii(text)

    receipt = _build_access_receipt(
        correlation_id=correlation_id,
        actor_id=actor_id,
        action_type="admin.ops.voice.tts",
        outcome="success",
        details={"voice_id": voice_id, "text_length": len(safe_text)},
    )
    store_receipts([receipt])

    async def audio_generator():
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream(
                    "POST",
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream",
                    headers={
                        "xi-api-key": api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "text": safe_text,
                        "model_id": "eleven_turbo_v2_5",
                        "voice_settings": {
                            "stability": 0.5,
                            "similarity_boost": 0.75,
                        },
                    },
                ) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(chunk_size=4096):
                        yield chunk
        except Exception as exc:
            logger.error("TTS streaming error: %s", exc)

    return StreamingResponse(
        audio_generator(),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-cache"},
    )
