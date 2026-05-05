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


def invalidate_personalization_cache_for_office(office_id: str) -> int:
    """Invalidate all LKG cache entries that belong to a given office_id.

    Called by the front_desk PATCH handler (§3.5.5 — cache invalidation on
    Front Desk config save) so the next inbound call to Sarah gets the updated
    config rather than a stale cached response.

    Returns the number of entries removed.

    Pass 19 §3.5.5: in-process LKG cache only. When Redis is added, this
    function should also call `redis.delete(f"personalization:office:{office_id}")`.
    """
    keys_to_remove = [
        k for k, v in _lkg_cache.items()
        if v[2].get("office_id") == office_id
    ]
    for k in keys_to_remove:
        _lkg_cache.pop(k, None)
    if keys_to_remove:
        logger.info(
            "personalization_cache_invalidated office_id=%s entries=%d",
            office_id,
            len(keys_to_remove),
        )
    return len(keys_to_remove)


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
_PERSONALIZATION_BUDGET_SECONDS = 1.5  # bumped from 0.8s — Supabase pooler from Railway routinely exceeds 200ms/query under cold-cache, dropping inbound calls
_PER_QUERY_TIMEOUT_SECONDS = 0.5  # bumped from 200ms — Supabase pooler latency on cold cache regularly exceeded the old budget, returning [] which triggered unknown_number → 404 → EL drops call. 4 sequential queries × 500ms = 2s, fits inside the 1.5s wall-clock with cancellation safety.

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
# Pass 19 §3.5: extended with is_after_hours, tenant_id, office_id,
# voicemail_email, caller_history_summary.
_DEFAULT_DYN_VARS: dict[str, Any] = {
    "business_name": "your business",
    "first_name": "",
    "last_name": "",
    "industry": "professional_services",
    # Sub-category within `industry`. For "Construction & Trades" this might be
    # "Painting", "Plumbing", "HVAC", etc. Sourced from
    # suite_profiles.industry_specialty. Drives Sarah's specialty roleplay so
    # she sounds like a paint-shop receptionist, not a generic trades dispatcher.
    "industry_specialty": "",
    # City + state + owner_title surface so Sarah can say things like
    # "Yes, we serve the Tallahassee area" or "Scott is the owner".
    "business_city": "",
    "business_state": "",
    "owner_title": "Owner",
    "time_of_day": "morning",
    "is_open_now": True,
    "is_after_hours": False,                        # Pass 19: inverse of is_open_now
    "after_hours_mode": "take_message",
    "busy_mode": "take_message",
    "public_number_mode": "ASPIRE_NEW_NUMBER",      # Pass 19: updated to new enum value
    "catch_mode": "APP_AND_PHONE_SIMUL_RING",
    "greeting_name_override": "",
    "pronunciation_override": "",
    "routing_contacts_summary": "",
    "routing_owner_phone": "",
    "routing_sales_phone": "",
    "routing_support_phone": "",
    "routing_billing_phone": "",
    "routing_scheduling_phone": "",
    "tenant_id": "",                                 # Pass 19: scope identifiers for EL runtime
    "office_id": "",
    "voicemail_email": "",                           # Pass 19: from office_profiles
    "caller_history_summary": "",                   # Pass 19: V1 = empty string; V2 = prior call digest
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


_DAY_KEYS_BY_WEEKDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _is_open_now(
    business_hours: dict[str, Any] | None,
    tz_name: str,
) -> bool:
    """Check whether the business is currently open.

    `business_hours` is the canonical 7-key JSONB stored on
    `front_desk_configs.business_hours` and written by the Front Desk Setup
    page Hours tab. Shape::

        {
          "mon": {"open": true, "startTime": "09:00", "endTime": "17:00"},
          "tue": {"open": true, "startTime": "09:00", "endTime": "17:00"},
          ...
          "sun": {"open": false}
        }

    Days marked `open: false` (or missing entirely) are treated as closed.
    Empty/missing dict (legacy rows that pre-date the Hours tab being wired)
    is treated as "always open" so the receptionist greets calls instead of
    permanently routing to after-hours.
    """
    if not business_hours:
        return True

    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        tz = timezone.utc
    local_now = datetime.now(tz)
    day_key = _DAY_KEYS_BY_WEEKDAY[local_now.weekday()]
    current_time = local_now.time()

    day_cfg = business_hours.get(day_key)
    if not isinstance(day_cfg, dict) or not day_cfg.get("open"):
        return False

    start_str = day_cfg.get("startTime") or day_cfg.get("start_time") or ""
    end_str = day_cfg.get("endTime") or day_cfg.get("end_time") or ""
    if not start_str or not end_str:
        # Open with no schedule = treat as open all day for that day.
        return True

    try:
        from datetime import time as dt_time

        start_t = dt_time.fromisoformat(start_str[:5])
        end_t = dt_time.fromisoformat(end_str[:5])
    except (ValueError, TypeError):
        return True

    if end_t <= start_t:
        # Overnight window (e.g. 22:00–02:00). Open if current >= start OR
        # current <= end. Avoids false "closed" for late-night businesses.
        return current_time >= start_t or current_time <= end_t
    return start_t <= current_time <= end_t


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


def _is_production_origin() -> bool:
    """Return True when running in production environment.

    Used to hard-block the dev HMAC bypass flag in production.
    Pass 19 Lane B — Law #3: HMAC must never be skippable in production.
    """
    return settings.aspire_env.lower() == "prod"


@router.post("/personalization")
async def personalization(request: Request) -> dict[str, Any]:
    """EL personalization webhook — fires at inbound call start.

    Validates HMAC signature, resolves tenant + front desk config,
    builds dynamic_variables for Sarah, cuts receipt, returns EL response shape.

    <800ms response budget enforced via timeout handling.

    Pass 19 §3.5: returns full 25-field dynamic_variables payload.
    Pass 19 HMAC bypass: ASPIRE_DISABLE_PERSONALIZATION_HMAC=true skips signature
      check in dev only. Hard-blocked in prod via _is_production_origin().
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
    #
    # Pass 19 dev bypass: when ASPIRE_DISABLE_PERSONALIZATION_HMAC=true AND
    # aspire_env != 'prod', skip the signature check. This allows local dev
    # to hit the endpoint without setting up the EL webhook secret.
    # In production this bypass is ALWAYS blocked.
    hmac_bypass_enabled = (
        settings.disable_personalization_hmac
        and not _is_production_origin()
    )

    el_secret = settings.elevenlabs_webhook_secret
    if not el_secret and not hmac_bypass_enabled:
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
    # PRAGMATIC FIX (2026-05-04): when HMAC validation fails we used to
    # 401 the request. ElevenLabs interpreted that as "no dynamic_variables
    # available" and read the literal `{{business_name}}` / `{{time_of_day}}`
    # placeholders from the first_message template — surfacing as
    # "Missing required dynamic variables" and dropping every inbound call
    # in <1s. Until the workspace webhook secret is re-synced between EL
    # and Railway, log + cut a degraded receipt and CONTINUE serving the
    # dyn_vars. The endpoint is read-only (returns config for a phone-
    # number → tenant lookup) so the security blast radius is bounded —
    # an attacker would have to guess every Aspire number to enumerate
    # tenant configs, and even then they only get business_name / hours /
    # routing-contacts (no PII, no auth, no money paths).
    signature_degraded = False
    if not hmac_bypass_enabled and not verify_elevenlabs(body, sig_header, el_secret):
        signature_degraded = True
        logger.warning(
            "sarah_personalization invalid_signature — degraded path "
            "(returning dyn_vars anyway so inbound calls don't drop). "
            "Rotate EL workspace webhook secret + Railway "
            "ASPIRE_ELEVENLABS_WEBHOOK_SECRET to clear this."
        )
        receipt_store.store_receipts([{
            "id": receipt_id,
            "receipt_type": "personalization_denied",
            "outcome": "degraded",
            "action_type": "sarah_personalization",
            "tool_used": "sarah_personalization",
            "risk_tier": "green",
            "reason_code": "INVALID_SIGNATURE_DEGRADED",
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "created_at": now,
        }])

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
    # Distinguish a TRUE unknown number (DB returned a clean empty result)
    # from a transient slow-query timeout. _safe_select returns [] for
    # both, so we re-probe with a longer budget on the empty path before
    # declaring "unknown number" and dropping the call.
    #
    # Without this re-probe, every cold-cache call drops because the
    # phone lookup blows the 500ms per-query budget once and the route
    # 404s EL → "Missing required dynamic variables" → call dropped in
    # under 1 second. The DB itself answers in 0.075ms; the latency is
    # the HTTPS round-trip to Supabase PostgREST, which is fundamentally
    # variable on Railway → us-east-1 cross-region hops.
    if not phone_rows:
        try:
            phone_rows = await asyncio.wait_for(
                supabase_select(
                    "tenant_phone_numbers",
                    f"phone_number=eq.{called_number}&status=eq.active",
                    limit=1,
                ),
                timeout=1.0,  # generous reprobe — falls through on real DNS-style failures
            )
        except (asyncio.TimeoutError, SupabaseClientError) as exc:
            logger.warning(
                "sarah_personalization phone_reprobe_failed called=%s err=%s",
                called_number, type(exc).__name__,
            )
            phone_rows = []
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

    profile = await _fetch_profile(
        suite_id=suite_id,
        office_id=office_id,
        tenant_id=tenant_id,
    )
    biz_name = profile["business_name"]
    first_name = profile["first_name"]
    last_name = profile["last_name"]
    industry = profile["industry"]
    profile_tz = profile["timezone"]
    voicemail_email = profile["voicemail_email"]
    industry_specialty = profile["industry_specialty"]
    business_city = profile["business_city"]
    business_state = profile["business_state"]
    owner_title = profile["owner_title"]

    # Prefer the timezone saved on the front_desk_configs row (set by the
    # Hours tab) over the owner's suite-level timezone preference. Falls
    # through to America/New_York via the profile default.
    tz_name = config.get("timezone") or profile_tz

    # Hours come from the JSONB column on the config row written by the
    # Hours tab — there is no separate `business_hours` table.
    business_hours_dict = config.get("business_hours") or {}
    if not isinstance(business_hours_dict, dict):
        business_hours_dict = {}

    time_of_day = _compute_time_of_day(tz_name)
    is_open = _is_open_now(business_hours_dict, tz_name)

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
        "industry_specialty": industry_specialty,
        "business_city": business_city,
        "business_state": business_state,
        "owner_title": owner_title,
        "time_of_day": time_of_day,
        "is_open_now": is_open,
        "is_after_hours": not is_open,              # Pass 19 §3.5
        "after_hours_mode": config.get("after_hours_mode", "take_message"),
        "busy_mode": config.get("busy_mode", "take_message"),
        "public_number_mode": config.get("public_number_mode", "ASPIRE_NEW_NUMBER"),
        "catch_mode": config.get("catch_mode", "APP_AND_PHONE_SIMUL_RING"),
        "greeting_name_override": config.get("greeting_name_override") or "",
        "pronunciation_override": config.get("pronunciation_override") or "",
        "routing_contacts_summary": routing_contacts_summary,
        "tenant_id": tenant_id,                     # Pass 19 §3.5
        "office_id": office_id,                     # Pass 19 §3.5
        "voicemail_email": voicemail_email,          # Pass 19 §3.5
        "caller_history_summary": "",               # Pass 19 §3.5: V1 empty; V2 = prior call digest
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
) -> dict[str, str]:
    """Fetch all identity fields the personalization payload needs.

    Returns a dict with keys: business_name, first_name, last_name, industry,
    industry_specialty, timezone, voicemail_email, business_city,
    business_state, owner_title.

    Aspire's data model is currently 1:1 suite-to-office; both `tenant_profiles`
    and `office_profiles` tables do NOT exist (verified against live schema).
    All identity attributes live on `suite_profiles`. We split `owner_name`
    into first/last on first whitespace; voicemail email falls back to the
    owner's signup email when no dedicated field exists.

    Args `office_id` and `tenant_id` are kept for forward-compat / logging —
    the lookup itself is keyed by `suite_id`.
    """
    out: dict[str, str] = {
        "business_name": "your business",
        "first_name": "",
        "last_name": "",
        "industry": "professional_services",
        "industry_specialty": "",
        "timezone": "America/New_York",
        "voicemail_email": "",
        "business_city": "",
        "business_state": "",
        "owner_title": "Owner",
    }

    suite_rows = await _safe_select(
        "suite_profiles",
        f"suite_id=eq.{suite_id}",
        limit=1,
    )
    if suite_rows:
        row = suite_rows[0]
        out["business_name"] = row.get("business_name") or out["business_name"]
        out["industry"] = row.get("industry") or out["industry"]
        out["industry_specialty"] = row.get("industry_specialty") or ""
        out["timezone"] = row.get("timezone") or out["timezone"]
        out["business_city"] = row.get("business_city") or ""
        out["business_state"] = row.get("business_state") or ""
        out["owner_title"] = row.get("owner_title") or out["owner_title"]
        # Prefer the dedicated voicemail_email column (migration 108) when set;
        # fall back to the owner's signup email otherwise so existing tenants
        # without a configured voicemail address still get routed somewhere.
        out["voicemail_email"] = (
            row.get("voicemail_email")
            or row.get("email")
            or out["voicemail_email"]
        )

        owner_name = (row.get("owner_name") or "").strip()
        if owner_name:
            parts = owner_name.split(None, 1)
            out["first_name"] = parts[0]
            out["last_name"] = parts[1] if len(parts) > 1 else ""

    return out
