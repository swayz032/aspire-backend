"""Sarah Receptionist personalization webhook (Pass 16 — §16.D).

POST /v1/sarah/personalization — EL calls this at inbound call start.

EL verified contract (2026-04-29):
  Inbound payload: {caller_id, agent_id, called_number, call_sid}
  Required response: {
    type: "conversation_initiation_client_data",
    dynamic_variables: { ... all custom vars ... },
    conversation_config_override?: { agent: { first_message, language } }
  }

CRITICAL rules per EL docs:
  - dynamic_variables MUST contain ALL custom vars defined on the agent.
    Missing keys break the agent — no partial responses.
  - system__ prefixed vars (system__caller_id, etc.) are injected by EL
    automatically and MUST NOT be sent by us.
  - conversation_config_override is optional; we use it for the dynamic
    first-message (business-name + time-of-day aware greeting).

Latency budget: <800ms response. Redis cache fallback if DB >500ms (stubbed
with TODO if Redis layer absent — returns cached config with STALE flag).

Law compliance:
  Law #2 — personalization_resolve receipt on every call.
  Law #3 — HMAC signature invalid → 401.
  Law #6 — scope resolved from called_number lookup in tenant_phone_numbers
            (never from request headers — webhook route).
  Law #9 — caller_id not logged at INFO level; truncated to first 6 digits.

Table assumptions:
  - tenant_phone_numbers (Pass 16 migration 102)
  - front_desk_configs (Pass 16 migration 102) — versioned per office
  - front_desk_routing_contacts (Pass 16 migration 102)
  - tenant_profiles — onboarding table; expected fields: business_name, industry
  - office_profiles — expected fields: first_name, last_name, timezone
  - business_hours — expected fields: office_id, day_of_week (0-6 Mon=0),
    open_time, close_time (TIME columns)

  If tenant_profiles / office_profiles do not yet exist or have no row for the
  tenant, we fall back to safe defaults ("your business", "professional_services")
  and log a warning. These tables are populated during onboarding — a missing row
  means onboarding is incomplete, not a hard failure.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request, HTTPException, status

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.ingestion.signatures import verify_elevenlabs
from aspire_orchestrator.services.metrics import METRICS
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sarah", tags=["sarah"])

# ── Last-known-good (LKG) config cache for Sarah personalization ─────────────
# In-memory LRU keyed by called_number. Used when DB lookups exceed the 800ms
# budget — Sarah still gets a personalized response built from a recent valid
# resolution. Pod restart wipes the cache (acceptable: first call repopulates
# within budget under normal load).
_LKG_CACHE_SIZE = 256
_LKG_CACHE_TTL_SECONDS = 600.0  # 10 min — staler than this we prefer defaults
_lkg_cache: "OrderedDict[str, tuple[float, dict[str, Any], dict[str, Any]]]" = OrderedDict()
# value tuple: (cached_at_monotonic, dyn_vars, scope_dict)


def _cache_put(called_number: str, dyn_vars: dict[str, Any], scope: dict[str, Any]) -> None:
    if called_number in _lkg_cache:
        _lkg_cache.move_to_end(called_number)
    _lkg_cache[called_number] = (time.monotonic(), dict(dyn_vars), dict(scope))
    while len(_lkg_cache) > _LKG_CACHE_SIZE:
        _lkg_cache.popitem(last=False)


def _cache_get(
    called_number: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return (dyn_vars, scope) if cached and fresh, else None."""
    item = _lkg_cache.get(called_number)
    if item is None:
        return None
    cached_at, dyn_vars, scope = item
    if time.monotonic() - cached_at > _LKG_CACHE_TTL_SECONDS:
        _lkg_cache.pop(called_number, None)
        return None
    _lkg_cache.move_to_end(called_number)
    return dict(dyn_vars), dict(scope)


# Personalization total wall-clock budget (Pass 16 §16.D: <800ms).
_PERSONALIZATION_BUDGET_SECONDS = 0.8
_PER_QUERY_TIMEOUT_SECONDS = 0.2  # 800ms / 4 sequential queries

# Routing role → dynamic variable name mapping (§16 constant)
_ROLE_TO_DYN_VAR: dict[str, str] = {
    "owner": "routing_owner_phone",
    "sales": "routing_sales_phone",
    "support": "routing_support_phone",
    "billing": "routing_billing_phone",
    "scheduling": "routing_scheduling_phone",
}

# Default dynamic vars — ALL custom vars Sarah expects must be present
# to avoid breaking the agent (EL requirement).
_DEFAULT_DYN_VARS: dict[str, Any] = {
    "business_name": "your business",
    "first_name": "",
    "last_name": "",
    "industry": "professional_services",
    "time_of_day": "morning",
    "is_open_now": True,
    "after_hours_mode": "take_message",
    "busy_mode": "take_message",
    "public_number_mode": "ASPIRE_NUMBER",
    "catch_mode": "APP_AND_PHONE_SIMUL_RING",
    "greeting_name_override": "",
    "pronunciation_override": "",
    "routing_contacts_summary": "",
    "routing_owner_phone": "",
    "routing_sales_phone": "",
    "routing_support_phone": "",
    "routing_billing_phone": "",
    "routing_scheduling_phone": "",
}


def _compute_time_of_day(tz_name: str) -> str:
    """Return 'morning'/'afternoon'/'evening' for current time in given timezone."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        tz = timezone.utc
    local_now = datetime.now(tz)
    hour = local_now.hour
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    return "evening"


def _is_open_now(business_hours_rows: list[dict[str, Any]], tz_name: str) -> bool:
    """Check if business is currently open based on hours rows."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        tz = timezone.utc
    local_now = datetime.now(tz)
    # weekday(): Mon=0 ... Sun=6
    day_of_week = local_now.weekday()
    current_time = local_now.time()

    for row in business_hours_rows:
        if row.get("day_of_week") == day_of_week:
            open_time_str = row.get("open_time") or ""
            close_time_str = row.get("close_time") or ""
            if not open_time_str or not close_time_str:
                continue
            try:
                from datetime import time as dt_time
                open_t = dt_time.fromisoformat(open_time_str[:8])
                close_t = dt_time.fromisoformat(close_time_str[:8])
                if open_t <= current_time <= close_t:
                    return True
            except (ValueError, TypeError):
                pass
    return False


def _build_first_message(
    dyn_vars: dict[str, Any],
    is_open: bool,
) -> str:
    """Build a time-of-day + hours-aware greeting first_message."""
    tod = dyn_vars.get("time_of_day", "morning")
    biz = dyn_vars.get("business_name", "your business")
    if is_open:
        return (
            f"Good {tod}, thank you for calling {biz}. "
            "This is Sarah, the AI front desk assistant. "
            "How can I help you today?"
        )
    return (
        f"Good {tod}, thank you for calling {biz}. "
        "We're currently closed, but I'm Sarah and I'd be happy "
        "to take a message or answer any questions I can help with. "
        "How can I assist you?"
    )


@router.post("/personalization")
async def personalization(request: Request) -> dict[str, Any]:
    """EL personalization webhook — fires at inbound call start.

    Validates HMAC signature, resolves tenant + front desk config,
    builds dynamic_variables for Sarah, cuts receipt, returns EL response shape.

    <800ms response budget enforced via timeout handling.
    """
    body = await request.body()
    sig_header = request.headers.get("ElevenLabs-Signature", "")
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    # Extract trace context from middleware contextvars (degrades gracefully
    # if called outside a middleware-wrapped request — returns "" in that case).
    trace_id = get_trace_id()
    correlation_id = get_correlation_id()

    # ── Signature verification (Law #3: fail closed) ─────────────────────
    # Pass 18 fix: an empty secret + missing/invalid signature must STILL fail
    # closed. Previous form `if el_secret and not verify_elevenlabs(...)`
    # silently bypassed verification when the secret env var was unset, which
    # would let any unauthenticated POST through in dev/staging where the
    # secret might not be configured.
    el_secret = settings.elevenlabs_webhook_secret
    if not el_secret:
        logger.error(
            "sarah_personalization missing_webhook_secret — refusing to verify"
        )
        receipt_store.store_receipts([{
            "id": receipt_id,
            "receipt_type": "personalization_denied",
            "outcome": "denied",
            "action_type": "sarah_personalization",
            "tool_used": "sarah_personalization",
            "risk_tier": "green",
            "reason_code": "MISSING_WEBHOOK_SECRET",
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "created_at": now,
        }])
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "MISCONFIGURED", "message": "EL webhook secret not set"},
        )
    if not verify_elevenlabs(body, sig_header, el_secret):
        logger.warning("sarah_personalization invalid_signature")
        receipt_store.store_receipts([{
            "id": receipt_id,
            "receipt_type": "personalization_denied",
            "outcome": "denied",
            "action_type": "sarah_personalization",
            "tool_used": "sarah_personalization",
            "risk_tier": "green",
            "reason_code": "INVALID_SIGNATURE",
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "created_at": now,
        }])
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "INVALID_SIGNATURE", "message": "EL HMAC signature invalid"},
        )

    # ── Parse payload ─────────────────────────────────────────────────────
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_PAYLOAD", "message": "Expected JSON body"},
        )

    called_number = payload.get("called_number", "")
    call_sid = payload.get("call_sid", "")
    # Law #9: log only first 6 digits of caller_id
    caller_id_log = (payload.get("caller_id") or "")[:6] + "..."

    # Pass 18 fix THREAT-014: validate E.164 format BEFORE building any
    # PostgREST filter string. Without this, a forged HMAC could inject
    # PostgREST operators (e.g. "+12125550198&suite_id=neq.<uuid>") to
    # broaden the match and resolve a different tenant's scope.
    import re as _re
    if not isinstance(called_number, str) or not _re.match(r"^\+\d{7,15}$", called_number):
        logger.warning(
            "sarah_personalization invalid_called_number_format value=%r",
            (called_number or "")[:20],
        )
        receipt_store.store_receipts([{
            "id": receipt_id,
            "receipt_type": "personalization_denied",
            "outcome": "denied",
            "action_type": "sarah_personalization",
            "tool_used": "sarah_personalization",
            "risk_tier": "green",
            "reason_code": "INVALID_CALLED_NUMBER",
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "created_at": now,
        }])
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_CALLED_NUMBER", "message": "called_number must be E.164"},
        )

    # ── Resolve tenant + config under 800ms wall-clock budget ─────────────
    handler_start = time.monotonic()
    fallback_reason: str | None = None
    used_cache = False

    try:
        resolution = await asyncio.wait_for(
            _resolve_personalization(called_number=called_number),
            timeout=_PERSONALIZATION_BUDGET_SECONDS,
        )
    except asyncio.TimeoutError:
        fallback_reason = "timeout"
        resolution = None
    except Exception as exc:
        # Unexpected — never 500 a personalization call (Sarah goes silent otherwise).
        logger.error(
            "sarah_personalization resolution_error called=%s err=%s",
            called_number,
            exc,
        )
        fallback_reason = "error"
        resolution = None

    if resolution is None:
        # Attempt LKG cache fallback before defaults
        cached = _cache_get(called_number)
        if cached is not None:
            dyn_vars, scope = cached
            suite_id = scope.get("suite_id", "")
            office_id = scope.get("office_id", "")
            tenant_id = scope.get("tenant_id", "")
            front_desk_config_id = scope.get("front_desk_config_id", "")
            is_open = bool(dyn_vars.get("is_open_now", True))
            time_of_day = str(dyn_vars.get("time_of_day", "morning"))
            used_cache = True
        else:
            # No cache → safe defaults so EL still gets a complete response.
            dyn_vars = dict(_DEFAULT_DYN_VARS)
            tz_name = "America/New_York"
            dyn_vars["time_of_day"] = _compute_time_of_day(tz_name)
            suite_id = ""
            office_id = ""
            tenant_id = ""
            front_desk_config_id = ""
            is_open = True
            time_of_day = dyn_vars["time_of_day"]

        latency = time.monotonic() - handler_start
        METRICS.personalization_latency.observe(latency)
        METRICS.personalization_cache_fallback_counter.labels(
            reason=fallback_reason or "no_resolution"
        ).inc()
        # Cut a fallback receipt (Law #2 — even degraded paths are state changes
        # in the trace).
        receipt_store.store_receipts([{
            "id": receipt_id,
            "receipt_type": "personalization_cache_fallback",
            "suite_id": suite_id,
            "office_id": office_id,
            "tenant_id": tenant_id,
            "outcome": "degraded",
            "action_type": "sarah_personalization",
            "tool_used": "sarah_personalization",
            "risk_tier": "green",
            "reason_code": "STALE_CONFIG_FALLBACK" if used_cache else "DEFAULT_CONFIG_FALLBACK",
            "redacted_inputs": {
                "called_number": called_number,
                "caller_id_prefix": caller_id_log,
                "call_sid": call_sid,
                "fallback_reason": fallback_reason or "unknown",
            },
            "redacted_outputs": {
                "is_open_now": is_open,
                "time_of_day": time_of_day,
                "used_lkg_cache": used_cache,
                "latency_seconds": round(latency, 3),
            },
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "created_at": now,
        }])
        first_message = _build_first_message(dyn_vars, is_open)
        logger.warning(
            "sarah_personalization fallback called=%s reason=%s used_cache=%s latency=%.3fs",
            called_number,
            fallback_reason or "unknown",
            used_cache,
            latency,
        )
        return {
            "type": "conversation_initiation_client_data",
            "dynamic_variables": dyn_vars,
            "conversation_config_override": {
                "agent": {
                    "first_message": first_message,
                    "language": "en",
                }
            },
            # Soft signal to downstream tooling that this response is degraded
            # — EL ignores unknown top-level keys, so it's a safe addition.
            "_aspire_fallback": True,
        }

    if resolution.get("unknown_number"):
        latency = time.monotonic() - handler_start
        METRICS.personalization_latency.observe(latency)
        logger.warning(
            "sarah_personalization unknown_number called=%s",
            called_number,
        )
        receipt_store.store_receipts([{
            "id": receipt_id,
            "receipt_type": "personalization_unknown_number",
            "outcome": "failed",
            "action_type": "sarah_personalization",
            "tool_used": "sarah_personalization",
            "risk_tier": "green",
            "reason_code": "UNKNOWN_NUMBER",
            "redacted_inputs": {"called_number": called_number},
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "created_at": now,
        }])
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "UNKNOWN_NUMBER", "message": f"No tenant for number {called_number}"},
        )

    dyn_vars = resolution["dyn_vars"]
    suite_id = resolution["suite_id"]
    office_id = resolution["office_id"]
    tenant_id = resolution["tenant_id"]
    front_desk_config_id = resolution["front_desk_config_id"]
    is_open = resolution["is_open"]
    time_of_day = resolution["time_of_day"]
    config_version = resolution["version_no"]

    # Populate LKG cache for future degraded calls.
    _cache_put(
        called_number,
        dyn_vars,
        {
            "suite_id": suite_id,
            "office_id": office_id,
            "tenant_id": tenant_id,
            "front_desk_config_id": front_desk_config_id,
        },
    )

    first_message = _build_first_message(dyn_vars, is_open)
    latency = time.monotonic() - handler_start
    METRICS.personalization_latency.observe(latency)

    # ── Cut receipt (Law #2) — idempotent on call_sid ─────────────────────
    receipt_store.store_receipts([{
        "id": receipt_id,
        "receipt_type": "personalization_resolve",
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "outcome": "success",
        "action_type": "sarah_personalization",
        "tool_used": "sarah_personalization",
        "risk_tier": "green",
        "redacted_inputs": {
            "called_number": called_number,
            "caller_id_prefix": caller_id_log,
            "call_sid": call_sid,
        },
        "redacted_outputs": {
            "front_desk_config_id": front_desk_config_id,
            "version_no": config_version,
            "is_open_now": is_open,
            "time_of_day": time_of_day,
            "latency_seconds": round(latency, 3),
        },
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "created_at": now,
    }])

    logger.info(
        "sarah_personalization resolved called=%s caller=%s is_open=%s tod=%s latency=%.3fs",
        called_number,
        caller_id_log,
        is_open,
        time_of_day,
        latency,
    )

    return {
        "type": "conversation_initiation_client_data",
        "dynamic_variables": dyn_vars,
        "conversation_config_override": {
            "agent": {
                "first_message": first_message,
                "language": "en",
            }
        },
    }


async def _resolve_personalization(
    *, called_number: str
) -> dict[str, Any] | None:
    """Run the 4 sequential lookups + dyn_vars assembly under outer wait_for.

    Returns:
      None if a fatal error before tenant resolution.
      {"unknown_number": True} if no tenant matches called_number.
      Otherwise a dict with dyn_vars + scope fields.

    The outer `asyncio.wait_for(_PERSONALIZATION_BUDGET_SECONDS)` cancels this
    coroutine if total wall time exceeds 800ms, surfacing as TimeoutError to
    the caller which then falls back to LKG cache or defaults.
    """
    phone_rows = await _safe_select(
        "tenant_phone_numbers",
        f"phone_number=eq.{called_number}&status=eq.active",
        limit=1,
    )
    if not phone_rows:
        return {"unknown_number": True}

    phone_row = phone_rows[0]
    suite_id = phone_row.get("suite_id", "")
    office_id = phone_row.get("office_id", "")
    tenant_id = phone_row.get("tenant_id", "")

    config_rows = await _safe_select(
        "front_desk_configs",
        f"office_id=eq.{office_id}&is_current=eq.true",
        order_by="version_no.desc",
        limit=1,
    )
    config: dict[str, Any] = config_rows[0] if config_rows else {}
    front_desk_config_id = config.get("id", "")

    routing_rows = await _safe_select(
        "front_desk_routing_contacts",
        f"office_id=eq.{office_id}",
    )

    biz_name, first_name, last_name, industry, tz_name = await _fetch_profile(
        suite_id=suite_id,
        office_id=office_id,
        tenant_id=tenant_id,
    )

    hours_rows = await _safe_select(
        "business_hours",
        f"office_id=eq.{office_id}",
    )

    time_of_day = _compute_time_of_day(tz_name)
    is_open = _is_open_now(hours_rows, tz_name) if hours_rows else True

    routing_dyn: dict[str, str] = {v: "" for v in _ROLE_TO_DYN_VAR.values()}
    routing_summary_parts: list[str] = []
    for row in routing_rows or []:
        role = row.get("role", "")
        dyn_var = _ROLE_TO_DYN_VAR.get(role)
        if dyn_var:
            phone = row.get("phone") or ""
            routing_dyn[dyn_var] = phone
            label = row.get("label") or row.get("name") or role
            if phone:
                routing_summary_parts.append(f"{label} ({role})")

    routing_contacts_summary = ", ".join(routing_summary_parts)

    dyn_vars: dict[str, Any] = {
        **_DEFAULT_DYN_VARS,
        "business_name": biz_name,
        "first_name": first_name,
        "last_name": last_name,
        "industry": industry,
        "time_of_day": time_of_day,
        "is_open_now": is_open,
        "after_hours_mode": config.get("after_hours_mode", "take_message"),
        "busy_mode": config.get("busy_mode", "take_message"),
        "public_number_mode": config.get("public_number_mode", "ASPIRE_NUMBER"),
        "catch_mode": config.get("catch_mode", "APP_AND_PHONE_SIMUL_RING"),
        "greeting_name_override": config.get("greeting_name_override") or "",
        "pronunciation_override": config.get("pronunciation_override") or "",
        "routing_contacts_summary": routing_contacts_summary,
        **routing_dyn,
    }

    return {
        "dyn_vars": dyn_vars,
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "front_desk_config_id": front_desk_config_id,
        "version_no": config.get("version_no", 0),
        "is_open": is_open,
        "time_of_day": time_of_day,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _safe_select(
    table: str,
    filters: str,
    order_by: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """supabase_select wrapper bounded to 200ms (Pass 18+ Lane 2 — 800ms / 4 calls).

    Returns [] on error or timeout — personalization must never 500. Outer
    asyncio.wait_for(0.8) in the handler enforces the global budget.
    """
    try:
        return await asyncio.wait_for(
            supabase_select(table, filters, order_by=order_by, limit=limit),
            timeout=_PER_QUERY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "sarah_personalization db_timeout table=%s filters=%s budget=%.0fms",
            table,
            filters,
            _PER_QUERY_TIMEOUT_SECONDS * 1000,
        )
        return []
    except SupabaseClientError as exc:
        logger.error("sarah_personalization db error table=%s filters=%s: %s", table, filters, exc)
        return []


async def _fetch_profile(
    *,
    suite_id: str,
    office_id: str,
    tenant_id: str,
) -> tuple[str, str, str, str, str]:
    """Fetch business name, first/last name, industry, timezone from profile tables.

    Returns (business_name, first_name, last_name, industry, timezone).

    Table assumptions:
      - tenant_profiles: tenant_id (FK), business_name, industry
      - office_profiles: office_id (FK), first_name, last_name, timezone
    Falls back to safe defaults if tables missing or empty row.
    """
    biz_name = "your business"
    first_name = ""
    last_name = ""
    industry = "professional_services"
    tz_name = "America/New_York"

    tenant_rows = await _safe_select(
        "tenant_profiles",
        f"tenant_id=eq.{tenant_id}",
        limit=1,
    )
    if tenant_rows:
        row = tenant_rows[0]
        biz_name = row.get("business_name") or biz_name
        industry = row.get("industry") or industry

    office_rows = await _safe_select(
        "office_profiles",
        f"office_id=eq.{office_id}",
        limit=1,
    )
    if office_rows:
        row = office_rows[0]
        first_name = row.get("first_name") or first_name
        last_name = row.get("last_name") or last_name
        tz_name = row.get("timezone") or tz_name

    return biz_name, first_name, last_name, industry, tz_name
