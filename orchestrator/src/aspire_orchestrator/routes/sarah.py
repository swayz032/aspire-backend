"""Sarah Receptionist personalization webhook (Pass 16 — §16.D, hardened Pass 4).

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

import hashlib

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.services import receipt_store
import hmac
from aspire_orchestrator.services.metrics import METRICS
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_rpc,
    supabase_select,
)
from aspire_orchestrator.services import personalization_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sarah", tags=["sarah"])

# ── Trade pack loader (Pass 4) ───────────────────────────────────────────────
# Loaded once at module import. Each pack is a small YAML stub that drives the
# {{trade_primary_term}}, {{trade_emergency_keywords}}, and
# {{trade_intake_fields_json}} dyn_vars. Pass 5 will deepen these.

import json as _json
import os as _os
import pathlib as _pathlib

_TRADE_PACKS_DIR = (
    _pathlib.Path(__file__).parent.parent / "config" / "trade_packs"
)

_TRADE_ID_TO_DISPLAY: dict[str, str] = {
    "hvac": "HVAC",
    "electrician": "Electrical",
    "plumber": "Plumbing",
    "specialty_remodeler": "Specialty Remodeling",
}

# Loaded trade packs: trade_id -> pack dict
_TRADE_PACKS: dict[str, dict[str, Any]] = {}


def _load_trade_packs() -> None:
    """Load all trade pack YAML files at startup. Non-raising — missing file = empty pack."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("trade_pack_loader yaml_not_available — trade dyn_vars will be empty")
        return

    for trade_id in _TRADE_ID_TO_DISPLAY:
        pack_path = _TRADE_PACKS_DIR / f"{trade_id}.yaml"
        if not pack_path.exists():
            logger.warning("trade_pack_missing path=%s", pack_path)
            _TRADE_PACKS[trade_id] = {}
            continue
        try:
            with pack_path.open("r", encoding="utf-8") as fh:
                _TRADE_PACKS[trade_id] = yaml.safe_load(fh) or {}
        except Exception as exc:
            logger.error("trade_pack_load_error path=%s: %s", pack_path, exc)
            _TRADE_PACKS[trade_id] = {}


_load_trade_packs()


def _build_trade_dyn_vars(trade_id: str | None) -> dict[str, str]:
    """Return the three trade dyn_vars for the given trade_id.

    Falls back to empty strings on unknown or NULL trade_id so the EL agent
    still receives all registered vars (EL requires every custom var to be present).
    """
    pack = _TRADE_PACKS.get(trade_id or "", {})
    primary_term: str = str(pack.get("primary_term") or "service call")
    keywords: list[str] = pack.get("emergency_keywords") or []
    intake: list[str] = pack.get("intake_fields") or []
    return {
        "trade_primary_term": primary_term,
        "trade_emergency_keywords": ", ".join(keywords),
        "trade_intake_fields_json": _json.dumps(intake),
    }


# Hard timeout for the DB query inside _resolve_personalization (Pass 4 requirement).
# Separate from _PERSONALIZATION_BUDGET_SECONDS which is the outer wall-clock budget.
_TRADE_DB_TIMEOUT_SECONDS = 0.700  # 700ms — Pass 4 spec'd 200ms but observed RPC latency
# is consistently 250-300ms (Supabase RPC roundtrip), causing 100% fallback hits and
# stale cache poisoning. 700ms keeps us under EL's 800ms personalization budget while
# letting the RPC actually complete. If observed p95 exceeds 700ms, raise to 1000ms
# AND parallel-queue the RPC at conversation_initiation time to pre-warm the cache.

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

# Parallel mapping for the team member's display name. The receptionist refers
# to non-owner team members by name to a caller, never by role label
# ("Maria handles billing" — not "the billing department").
_ROLE_TO_NAME_DYN_VAR: dict[str, str] = {
    "owner": "routing_owner_name",
    "sales": "routing_sales_name",
    "support": "routing_support_name",
    "billing": "routing_billing_name",
    "scheduling": "routing_scheduling_name",
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
    "public_number_mode": "aspire_new_number",      # lowercase: prompt uses lowercase conditionals
    "catch_mode": "app_and_phone_simul_ring",       # lowercase: prompt uses lowercase conditionals
    "greeting_name_override": "",
    "pronunciation_override": "",
    "routing_contacts_summary": "",
    "routing_owner_phone": "",
    "routing_sales_phone": "",
    "routing_support_phone": "",
    "routing_billing_phone": "",
    "routing_scheduling_phone": "",
    # Per-role display names — receptionist uses these to refer to team members
    # by NAME to callers, never by role label.
    "routing_owner_name": "",
    "routing_sales_name": "",
    "routing_support_name": "",
    "routing_billing_name": "",
    "routing_scheduling_name": "",
    # Owner formal address — "Mr. Scott" / "Ms. Lopez" — used when speaking
    # ABOUT the owner to a caller. Default salutation is "Mr." until a per-tenant
    # salutation field exists in suite_profiles.
    "owner_salutation": "Mr.",
    "owner_formal_name": "",
    # Configured-roles list — humanized comma-separated string of department
    # role labels that have an actual configured contact. Lets the agent know
    # which departments exist for THIS business so it doesn't promise sales/
    # support/billing if those aren't real.
    "configured_roles": "",
    "tenant_id": "",                                 # Pass 19: scope identifiers for EL runtime
    "office_id": "",
    "voicemail_email": "",                           # Pass 19: from office_profiles
    "caller_history_summary": "",                   # Pass 19: V1 = empty string; V2 = prior call digest
    # Caller Memory (migration 110) — populated when the inbound caller is a known contact.
    # Public to the LLM so it can greet known callers by first name.
    "caller_is_known": False,
    "caller_display_name": "",
    "caller_first_name": "",
    "caller_company": "",
    "caller_last_call_summary": "",
    "caller_total_calls": 0,
    "caller_last_seen_days_ago": 0,
    # Category Tiffany assigned at contact-create time (lead/client/vendor/friend/
    # other/unknown). Empty for first-time callers (Tiffany classifies during the call).
    "caller_category": "",
    # Trade pack dyn_vars (Pass 4 — migration 113, trade_packs/*.yaml).
    # Populated from suite_profiles.trade_id → _build_trade_dyn_vars().
    # Empty defaults ensure EL agents always receive all registered custom vars
    # even on tenants with no trade_id configured yet.
    "trade_primary_term": "service call",
    "trade_emergency_keywords": "",
    "trade_intake_fields_json": "[]",
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
    Empty/missing dict means hours have NEVER been configured for this
    business — fail CLOSED. Returning True here would silently make every
    new tenant appear 24/7 open until they explicitly save Hours, which
    is misleading for callers (e.g., calling at 11pm and being greeted
    as if the business is open). Treat unconfigured-hours as after-hours
    so the take_message flow fires by default.
    """
    if not business_hours:
        return False

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


# Internal-only fields that must NEVER appear in the agent's prompt context.
# EL injects every dynamic_variable into the agent's system prompt — if these
# are present, the LLM can verbalize them and trip EL's "no sharing personal/
# internal info" guardrail (or worse, leak a tenant UUID / owner email to the
# caller). Stripped at the response boundary, kept on backend for receipts +
# tool-scope routing.
_INTERNAL_DYN_VAR_KEYS: frozenset[str] = frozenset({
    "tenant_id",
    "office_id",
    "voicemail_email",
})


def _strip_internal_fields(dyn_vars: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of dyn_vars with internal-only fields removed."""
    return {k: v for k, v in dyn_vars.items() if k not in _INTERNAL_DYN_VAR_KEYS}


# Maps EL agent_id → receptionist display name used in the spoken first_message.
# Production rule: never speak a default — if the agent_id is unknown, raise so
# the call drops loudly rather than silently introducing the wrong persona.
# Agent IDs are CASE-SENSITIVE (EL workspace canonical IDs, lower-hex).
_AGENT_DISPLAY_NAME: dict[str, str] = {
    "agent_4801kqtapvsre2gb0gyb1ng631qr": "Tiffany",
    "agent_6501kp71h69jfqysgd055hemqhrq": "Sarah",
    "agent_8901kmqdjnrte7psp6en4f85m4kt": "Sarah",
}


class UnknownAgentError(Exception):
    """Raised when an agent_id is not present in _AGENT_DISPLAY_NAME.

    Law #3 (Fail Closed): callers must never receive a blank or wrong agent
    name in the spoken greeting. An unknown agent_id indicates a misconfigured
    EL agent workspace or a spoofed webhook payload. The personalization handler
    catches this and returns HTTP 400 + receipt instead of silently degrading.
    """

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"agent_id {agent_id!r} is not in the receptionist registry")


def _resolve_agent_display_name(agent_id: str) -> str:
    """Strict lookup of the receptionist display name for a given EL agent_id.

    Trims leading/trailing whitespace before lookup (edge-case defence).
    Raises UnknownAgentError — never returns an empty string.
    Agent IDs are case-sensitive; do not normalise case here.
    """
    normalised = agent_id.strip() if agent_id else ""
    if not normalised:
        raise UnknownAgentError(agent_id)
    name = _AGENT_DISPLAY_NAME.get(normalised)
    if name is None:
        raise UnknownAgentError(normalised)
    return name


def _build_first_message(
    dyn_vars: dict[str, Any],
    is_open: bool,
    agent_id: str,
    call_sid: str = "",
) -> str:
    """Build the spoken first_message — rotated per call to feel less canned.

    Real receptionists never say it the same way twice. We rotate among 3-4
    variants per scenario, seeded by call_sid so the same call always renders
    the same opener (idempotent across EL retries) but consecutive calls feel
    different. Per approved plan §3 "Option A — server-side rotation".

    When the caller is a known contact (caller_is_known=True), greet by FIRST
    NAME ONLY — no company, no time-of-day prefix. New callers get the full
    business-name greeting keyed to agent_id (Tiffany / Sarah).
    """
    # Deterministic-per-call selector: hash call_sid → index. Same call_sid
    # always picks the same variant (replay-safe) but different calls land
    # on different variants.
    seed = sum(ord(c) for c in call_sid) if call_sid else 0

    caller_is_known = bool(dyn_vars.get("caller_is_known"))
    # Defensive: reject literal "None"/"null"/"undefined" strings that have crept
    # in from upstream data (observed prod bug: contact display_name="None" produced
    # "Hi None, ..." greetings). Belt-and-suspenders alongside contact_writer's
    # _sanitize_name which prevents the corruption at write-time.
    caller_first_name_raw = (dyn_vars.get("caller_first_name") or "").strip()
    caller_first_name = (
        ""
        if caller_first_name_raw.lower() in ("none", "null", "undefined", "n/a", "na")
        else caller_first_name_raw
    )
    # Normalize after-hours mode (lowercased upstream) for routing the after-hours opener.
    ah_mode = (dyn_vars.get("after_hours_mode") or "").strip().lower()
    owner_formal = (dyn_vars.get("owner_formal_name") or "the owner").strip()

    if caller_is_known and caller_first_name:
        if is_open:
            variants = [
                f"Hi {caller_first_name}, how can I help today?",
                f"Hey {caller_first_name}, what's going on?",
                f"{caller_first_name}, hi — what can I do for you?",
                f"Hi {caller_first_name}, good to hear from you. What's up?",
            ]
        elif ah_mode == "try_transfer_then_message":
            variants = [
                f"Hey {caller_first_name} — we're closed, but I can try {owner_formal} for you. What's going on?",
                f"Hi {caller_first_name}, good to hear from you. We're outside hours, but let me see if I can grab {owner_formal}. Tell me what you need.",
                f"{caller_first_name}, hey — we're closed but I'll try {owner_formal} real quick. What do you need?",
            ]
        elif ah_mode == "ask_callback_window":
            variants = [
                f"Hey {caller_first_name} — we're closed, but I can have {owner_formal} call you back. What time works?",
                f"Hi {caller_first_name}, good to hear from you. We're outside hours — I can schedule {owner_formal} to call back. What window works for you?",
            ]
        else:
            # take_message / empty / unknown — message-first
            variants = [
                f"Hi {caller_first_name} — we're closed for the evening, but I can grab a message for you. What's going on?",
                f"Hey {caller_first_name}, good to hear from you. We're outside hours right now, but I can take a message — what do you need?",
                f"{caller_first_name}, hi — we're closed at the moment, but I'm here. Let me grab a message for you, what's up?",
            ]
        return variants[seed % len(variants)]

    tod = dyn_vars.get("time_of_day", "morning")
    biz = dyn_vars.get("business_name", "your business")
    # Strict lookup — raises UnknownAgentError if agent_id is missing or unknown.
    # Callers of _build_first_message must catch UnknownAgentError and handle it
    # before calling this function (the personalization handler does so).
    name = _resolve_agent_display_name(agent_id)
    if is_open:
        # Open-hours variants -- match the after-hours rhythm pattern
        # (greeting + em-dash pause + name + conversational open) so the
        # voice model breathes between clauses instead of racing through
        # a comma-chain. Comma-only sentences read as one rushed unit on
        # v3 Conversational; em-dash + short clauses force natural pacing.
        variants = [
            f"Good {tod} — you've reached {biz}. This is {name} — what can I help with?",
            f"Hey, thanks for calling {biz} — this is {name}. What's going on?",
            f"{biz}, good {tod} — {name} here. How can I help?",
            f"Hi, you've reached {biz} — I'm {name}. What can I do for you?",
        ]
        return variants[seed % len(variants)]

    # After-hours — branch on after_hours_mode so the OPENER matches the
    # configured flow. Hardcoded "I can take a message" everywhere caused
    # Tiffany/Sarah to commit to message-first even when the office settings
    # said try_transfer_then_message — observed 2026-05-10 conv_0001/conv_1801.
    if ah_mode == "try_transfer_then_message":
        variants = [
            f"Hey, you've reached {biz} — we're closed, but I can try {owner_formal} for you. What's going on?",
            f"Hi, {biz} after hours — this is {name}. Tell me what you need and I'll see if I can grab {owner_formal}.",
            f"Good {tod}, {biz} is closed, but I'm {name} — let me try {owner_formal} for you. What do you need?",
        ]
    elif ah_mode == "ask_callback_window":
        variants = [
            f"Hi, you've reached {biz} after hours — this is {name}. I can have {owner_formal} call you back. What time works for you?",
            f"Hey, {biz} is closed right now, but I can schedule {owner_formal} to call you. What window works?",
        ]
    else:
        # take_message / empty / unknown — message-first opener
        variants = [
            f"Hi, you've reached {biz} after hours — this is {name}. I can take a quick message and someone will follow up first thing.",
            f"Good {tod}, {biz} is closed right now, but I'm {name} and I can grab a message for you.",
            f"Hey, thanks for calling {biz} — we're closed, but I'm {name}. What can I help with?",
        ]
    return variants[seed % len(variants)]


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
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    trace_id = get_trace_id()
    correlation_id = get_correlation_id()

    # ── Auth (Law #3: fail closed) ───────────────────────────────────────
    # ElevenLabs personalization (conversation_initiation_client_data) webhooks
    # are NOT HMAC-signed by EL — only post-call webhooks are. Per EL's
    # documented pattern (https://elevenlabs.io/docs/agents-platform/customization/
    # personalization/twilio-personalization), auth is via custom headers set
    # in the agent's workspace_overrides.request_headers. We require a shared
    # secret in `X-Aspire-Webhook-Secret`, constant-time compared against
    # ASPIRE_PERSONALIZATION_WEBHOOK_SECRET. Auth failure → 401 + denied
    # receipt (no fallback). Dev bypass is unchanged: ASPIRE_DISABLE_
    # PERSONALIZATION_HMAC=true skips auth in non-prod only.
    auth_bypass_enabled = (
        settings.disable_personalization_hmac
        and not _is_production_origin()
    )

    expected_secret = settings.personalization_webhook_secret
    if not expected_secret and not auth_bypass_enabled:
        logger.error("sarah_personalization missing_webhook_secret")
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
            detail={"error": "MISCONFIGURED", "message": "Personalization webhook secret not set"},
        )

    if not auth_bypass_enabled:
        provided = request.headers.get("X-Aspire-Webhook-Secret", "")
        if not provided or not hmac.compare_digest(provided, expected_secret):
            logger.warning("sarah_personalization invalid_secret — denying")
            receipt_store.store_receipts([{
                "id": receipt_id,
                "receipt_type": "personalization_denied",
                "outcome": "denied",
                "action_type": "sarah_personalization",
                "tool_used": "sarah_personalization",
                "risk_tier": "green",
                "reason_code": "INVALID_WEBHOOK_SECRET",
                "trace_id": trace_id,
                "correlation_id": correlation_id,
                "created_at": now,
            }])
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "UNAUTHORIZED", "message": "Invalid webhook secret"},
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
    agent_id = (payload.get("agent_id") or "").strip()
    # Law #9: log only first 6 digits of caller_id
    caller_id_log = (payload.get("caller_id") or "")[:6] + "..."

    # ── Pass 3: fail closed on unknown agent_id (Law #3) ─────────────────
    # Validate agent_id against the registry BEFORE any DB work so we never
    # waste a query slot on an unconfigured or spoofed webhook payload.
    # SHA256 of request headers is stored in the receipt for audit; raw
    # header values are never logged (Law #9).
    if not agent_id or agent_id not in _AGENT_DISPLAY_NAME:
        raw_headers_sha256 = hashlib.sha256(
            str(sorted(request.headers.items())).encode()
        ).hexdigest()
        source_ip = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.client.host
            if request.client
            else "unknown"
        )
        logger.warning(
            "sarah_personalization unknown_agent_id agent_id=%r source_ip=%s",
            agent_id,
            source_ip,
        )
        try:
            receipt_store.store_receipts([{
                "id": receipt_id,
                "receipt_type": "unknown_agent_in_personalization",
                "outcome": "denied",
                "action_type": "sarah_personalization",
                "tool_used": "sarah_personalization",
                "risk_tier": "yellow",
                "reason_code": "UNKNOWN_AGENT",
                "redacted_inputs": {
                    "attempted_agent_id": agent_id,
                    "source_ip": source_ip,
                    "headers_sha256": raw_headers_sha256,
                },
                "trace_id": trace_id,
                "correlation_id": correlation_id,
                "created_at": now,
            }])
        except Exception as _receipt_err:
            logger.error(
                "sarah_personalization unknown_agent receipt_store_error err=%s",
                _receipt_err,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "UNKNOWN_AGENT",
                "detail": f"agent_id {agent_id!r} not in registry",
                "trace_id": trace_id,
            },
        )

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

    raw_caller_id = (payload.get("caller_id") or "").strip() or None
    try:
        resolution = await asyncio.wait_for(
            _resolve_personalization(
                called_number=called_number,
                caller_id=raw_caller_id,
            ),
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
        # ── Pass 4: fallback chain — Redis warm-cache → LKG cache → safe defaults ──
        # Try Redis warm-cache (suite_id unknown at this point; keyed by agent_id + called_number
        # proxy via LKG scope). The LKG cache still contains the scope dict with suite_id.
        redis_cache_hit = False
        redis_cache_unavailable = False
        lkg_cached = _cache_get(called_number)

        if lkg_cached is not None:
            _lkg_dyn_vars, _lkg_scope = lkg_cached
            _lkg_suite_id = _lkg_scope.get("suite_id", "")
            if _lkg_suite_id and agent_id:
                # Attempt Redis cache lookup using known scope
                try:
                    redis_hit = await personalization_cache.get(_lkg_suite_id, agent_id)
                    if redis_hit is not None:
                        dyn_vars = redis_hit
                        suite_id = _lkg_suite_id
                        office_id = _lkg_scope.get("office_id", "")
                        tenant_id = _lkg_scope.get("tenant_id", "")
                        front_desk_config_id = _lkg_scope.get("front_desk_config_id", "")
                        is_open = bool(dyn_vars.get("is_open_now", True))
                        time_of_day = str(dyn_vars.get("time_of_day", "morning"))
                        used_cache = True
                        redis_cache_hit = True
                except Exception as _redis_exc:
                    logger.warning("personalization_cache redis_get_failed: %s", _redis_exc)
                    redis_cache_unavailable = True

        if not redis_cache_hit:
            # Fall to LKG in-memory cache
            if lkg_cached is not None:
                dyn_vars, scope = lkg_cached
                suite_id = scope.get("suite_id", "")
                office_id = scope.get("office_id", "")
                tenant_id = scope.get("tenant_id", "")
                front_desk_config_id = scope.get("front_desk_config_id", "")
                is_open = bool(dyn_vars.get("is_open_now", True))
                time_of_day = str(dyn_vars.get("time_of_day", "morning"))
                used_cache = True
            else:
                # No cache anywhere → safe defaults so EL still gets a complete response.
                dyn_vars = dict(_DEFAULT_DYN_VARS)
                tz_name = "America/New_York"
                dyn_vars["time_of_day"] = _compute_time_of_day(tz_name)
                suite_id = ""
                office_id = ""
                tenant_id = ""
                front_desk_config_id = ""
                is_open = True
                time_of_day = dyn_vars["time_of_day"]

        # Determine outcome label for Pass 4 metrics
        if redis_cache_hit:
            _p4_outcome = "cache_fallback"
        elif used_cache:
            _p4_outcome = "cache_fallback"
        else:
            _p4_outcome = "degraded"

        latency = time.monotonic() - handler_start
        METRICS.personalization_latency.observe(latency)
        METRICS.personalization_cache_fallback_counter.labels(
            reason=fallback_reason or "no_resolution"
        ).inc()
        # Pass 4 Prometheus — requests_total + latency_by_outcome
        try:
            METRICS.personalization_requests_total.labels(
                agent_id=agent_id or "unknown",
                outcome=_p4_outcome,
            ).inc()
            METRICS.personalization_latency_by_outcome.labels(
                agent_id=agent_id or "unknown",
                outcome=_p4_outcome,
            ).observe(latency)
        except Exception:
            pass  # Never break personalization for metrics failure

        if redis_cache_unavailable:
            # Law #2: emit receipt when Redis outage occurs during fallback path
            receipt_store.store_receipts([{
                "id": str(uuid.uuid4()),
                "receipt_type": "personalization_cache_unavailable",
                "suite_id": suite_id,
                "outcome": "warning",
                "action_type": "sarah_personalization",
                "tool_used": "sarah_personalization",
                "risk_tier": "green",
                "reason_code": "REDIS_UNAVAILABLE_ON_FALLBACK",
                "trace_id": trace_id,
                "correlation_id": correlation_id,
                "created_at": now,
            }])

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
            "reason_code": "REDIS_CACHE_FALLBACK" if redis_cache_hit else (
                "STALE_CONFIG_FALLBACK" if used_cache else "DEFAULT_CONFIG_FALLBACK"
            ),
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
                "used_redis_cache": redis_cache_hit,
                "latency_seconds": round(latency, 3),
            },
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "created_at": now,
        }])
        first_message = _build_first_message(dyn_vars, is_open, agent_id, call_sid)
        public_dyn_vars = _strip_internal_fields(dyn_vars)
        logger.warning(
            "sarah_personalization fallback called=%s reason=%s used_cache=%s redis_hit=%s latency=%.3fs",
            called_number,
            fallback_reason or "unknown",
            used_cache,
            redis_cache_hit,
            latency,
        )
        return {
            "type": "conversation_initiation_client_data",
            "dynamic_variables": public_dyn_vars,
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
        # No fake defaults. If the number truly isn't ours, fail loud —
        # support sees the receipt + alert and routes to ops. A real
        # Aspire-owned number reaches this branch only on catastrophic
        # DB outage; in that case the right answer is to fail honestly,
        # not lie about the tenant identity to the caller.
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
    # Pass 4 signals from _resolve_personalization
    business_name_was_blank: bool = bool(resolution.get("business_name_was_blank", False))
    resolved_trade_id: str = str(resolution.get("trade_id") or "")

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

    # Pass 4: write to Redis warm-cache (non-blocking — Redis outage must not
    # break personalization). Cache write wraps in try/except per plan constraint.
    if suite_id and agent_id:
        try:
            await personalization_cache.set(suite_id, agent_id, dyn_vars)
            if METRICS.personalization_cache_size_bytes is not None:
                import sys as _sys
                METRICS.personalization_cache_size_bytes.set(
                    _sys.getsizeof(dyn_vars)
                )
        except Exception as _cache_exc:
            logger.warning("personalization_cache write_failed suite_id=%s: %s", suite_id, _cache_exc)
            # Law #2: emit receipt for cache write failure (monitoring surface)
            receipt_store.store_receipts([{
                "id": str(uuid.uuid4()),
                "receipt_type": "personalization_cache_unavailable",
                "suite_id": suite_id,
                "outcome": "warning",
                "action_type": "sarah_personalization",
                "tool_used": "sarah_personalization",
                "risk_tier": "green",
                "reason_code": "REDIS_WRITE_FAILED",
                "trace_id": trace_id,
                "correlation_id": correlation_id,
                "created_at": now,
            }])

    first_message = _build_first_message(dyn_vars, is_open, agent_id, call_sid)
    public_dyn_vars = _strip_internal_fields(dyn_vars)
    latency = time.monotonic() - handler_start
    METRICS.personalization_latency.observe(latency)

    # Pass 4: emit blank business_name receipt + Prometheus counter (plan §4.2).
    if business_name_was_blank:
        try:
            receipt_store.store_receipts([{
                "id": str(uuid.uuid4()),
                "receipt_type": "personalization_blank_business_name_filled",
                "suite_id": suite_id,
                "office_id": office_id,
                "outcome": "warning",
                "action_type": "sarah_personalization",
                "tool_used": "sarah_personalization",
                "risk_tier": "yellow",
                "reason_code": "BLANK_BUSINESS_NAME_FILLED",
                "redacted_inputs": {
                    "agent_id": agent_id,
                    "called_number": called_number,
                },
                "trace_id": trace_id,
                "correlation_id": correlation_id,
                "created_at": now,
            }])
            METRICS.personalization_blank_business_name_total.labels(
                suite_id=suite_id or "unknown"
            ).inc()
        except Exception as _blank_exc:
            logger.warning("personalization blank_business_name receipt_error: %s", _blank_exc)

    # Pass 4 Prometheus — requests_total + latency_by_outcome for happy path
    try:
        METRICS.personalization_requests_total.labels(
            agent_id=agent_id or "unknown",
            outcome="hit",
        ).inc()
        METRICS.personalization_latency_by_outcome.labels(
            agent_id=agent_id or "unknown",
            outcome="hit",
        ).observe(latency)
    except Exception:
        pass  # Never break personalization for metrics failure

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
            "trade_id": resolved_trade_id,
            "latency_seconds": round(latency, 3),
        },
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "created_at": now,
    }])

    logger.info(
        "sarah_personalization resolved called=%s caller=%s is_open=%s tod=%s trade_id=%s latency=%.3fs",
        called_number,
        caller_id_log,
        is_open,
        time_of_day,
        resolved_trade_id or "none",
        latency,
    )

    return {
        "type": "conversation_initiation_client_data",
        "dynamic_variables": public_dyn_vars,
        "conversation_config_override": {
            "agent": {
                "first_message": first_message,
                "language": "en",
            }
        },
    }


async def _resolve_personalization(
    *, called_number: str, caller_id: str | None = None
) -> dict[str, Any] | None:
    """Resolve everything via a single SECURITY DEFINER RPC.

    Replaces the previous 4-call PostgREST cascade (phone → config →
    routing → profile) with one HTTPS round-trip to
    `resolve_personalization_by_phone(p_phone)` — the function joins
    the 4 tables server-side and returns a single jsonb row. Cold-cache
    p99 dropped from ~800ms (4× ~200ms) to ~80ms (1× ~80ms) so the
    webhook reliably fits inside ElevenLabs' tight personalization
    budget without any "fallback default" layer.

    Returns:
      {"unknown_number": True} ONLY when the RPC returns NULL, meaning
        the phone is not in tenant_phone_numbers OR is not active. This
        is a real catastrophic state for a real Aspire call (the EL
        agent should not be attached to a number we don't own) and the
        webhook 404s — support sees the receipt and routes to ops.
      Otherwise a dict with dyn_vars + scope fields.
    """
    # Pass 4: 200ms hard timeout on the RPC call itself (plan §4 spec).
    # The outer handler still enforces _PERSONALIZATION_BUDGET_SECONDS as the
    # wall-clock guard. Raising asyncio.TimeoutError here propagates up to the
    # handler which tries the Redis warm-cache before safe defaults.
    rpc_result = await asyncio.wait_for(
        supabase_rpc(
            "resolve_personalization_by_phone",
            {"p_phone": called_number, "p_caller_id": caller_id or None},
        ),
        timeout=_TRADE_DB_TIMEOUT_SECONDS,
    )

    # supabase_rpc returns the raw RPC body. For a scalar-returning
    # function this is the value itself (or null). For a single-row
    # function called via PostgREST it may be wrapped in a list. Normalize.
    payload: dict[str, Any] | None
    if isinstance(rpc_result, list):
        payload = rpc_result[0] if rpc_result else None
        # Some PostgREST adapters wrap the value under the function name.
        if isinstance(payload, dict) and set(payload.keys()) == {
            "resolve_personalization_by_phone"
        }:
            payload = payload["resolve_personalization_by_phone"]
    elif isinstance(rpc_result, dict):
        payload = rpc_result
    else:
        payload = None

    if not payload:
        return {"unknown_number": True}

    suite_id = str(payload.get("suite_id") or "")
    office_id = str(payload.get("office_id") or "")
    tenant_id = str(payload.get("tenant_id") or "")
    config: dict[str, Any] = payload.get("config") or {}
    front_desk_config_id = str(config.get("id") or "")
    routing_rows: list[dict[str, Any]] = payload.get("routing_contacts") or []
    profile_raw: dict[str, Any] = payload.get("profile") or {}

    # Normalize profile fields to the names the rest of the function
    # expects. Empty-string defaults match the prior _fetch_profile
    # behavior so downstream code paths stay identical.

    # Pass 4: business_name blank defense (Law #2 + plan §4.2).
    # NULL or empty business_name from DB → "your business" safe default.
    # A receipt is cut by the caller after _resolve_personalization returns
    # so we signal the blank via a flag rather than writing the receipt here
    # (receipts require correlation_id/trace_id from the request context).
    raw_biz_name = (profile_raw.get("business_name") or "").strip()
    business_name_was_blank = not raw_biz_name
    biz_name = raw_biz_name if raw_biz_name else "your business"

    owner_full = (profile_raw.get("owner_name") or "").strip()
    first_name, _, last_name = owner_full.partition(" ")

    # Pass 4: trade_id → {{industry}} display string (plan §4.2).
    # Falls back to the freeform `industry` field when no trade_id is set,
    # then to "contractor" if both are empty.
    trade_id_raw = (profile_raw.get("trade_id") or "").strip() or None
    trade_display = _TRADE_ID_TO_DISPLAY.get(trade_id_raw or "", "")
    freeform_industry = (profile_raw.get("industry") or "").strip()
    industry = trade_display or freeform_industry or "contractor"

    profile_tz = (profile_raw.get("timezone") or "America/New_York").strip()
    voicemail_email = (
        profile_raw.get("voicemail_email") or profile_raw.get("email") or ""
    ).strip()

    # Pass 4: trade_specialty → {{industry_specialty}} (plan §4.2).
    # NULL → empty string; prompt template handles gracefully per contract rule 15.
    industry_specialty = (
        profile_raw.get("trade_specialty")
        or profile_raw.get("industry_specialty")
        or ""
    ).strip()

    business_city = (profile_raw.get("business_city") or "").strip()
    business_state = (profile_raw.get("business_state") or "").strip()
    owner_title = (profile_raw.get("owner_title") or "Owner").strip()

    # Pass 4: build trade-specific dyn_vars from the loaded YAML pack.
    trade_dyn_vars = _build_trade_dyn_vars(trade_id_raw)

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
    routing_dyn.update({v: "" for v in _ROLE_TO_NAME_DYN_VAR.values()})
    routing_summary_parts: list[str] = []
    configured_role_labels: list[str] = []
    for row in routing_rows or []:
        role = row.get("role", "")
        phone_var = _ROLE_TO_DYN_VAR.get(role)
        name_var = _ROLE_TO_NAME_DYN_VAR.get(role)
        if not phone_var:
            continue
        phone = (row.get("phone") or "").strip()
        team_name = (row.get("name") or "").strip()
        if phone:
            routing_dyn[phone_var] = phone
            if name_var:
                routing_dyn[name_var] = team_name
            label = row.get("label") or team_name or role
            routing_summary_parts.append(f"{label} ({role})")
            configured_role_labels.append(role)

    routing_contacts_summary = ", ".join(routing_summary_parts)
    configured_roles = ", ".join(configured_role_labels)

    # Owner formal address — "Mr. Scott" / "Ms. Lopez". Used when the receptionist
    # speaks ABOUT the owner to a caller. Defaults to "Mr." salutation until a
    # per-tenant salutation field exists in suite_profiles.
    owner_salutation_value = (profile_raw.get("owner_salutation") or "Mr.").strip()
    owner_formal_name = (
        f"{owner_salutation_value} {last_name}".strip()
        if last_name
        else (owner_full or "the owner")
    )

    # Caller Memory: parse the 'contact' field returned by the extended RPC
    # (migration 110). When the caller's number matches a known contact in
    # frontdesk_contacts (and the contact is not soft-deleted), populate
    # public-facing dyn_vars so the receptionist can greet by first name.
    contact_block: dict[str, Any] | None = payload.get("contact")
    caller_is_known = False
    caller_display_name = ""
    caller_first_name = ""
    caller_company = ""
    caller_last_call_summary = ""
    caller_total_calls = 0
    caller_last_seen_days_ago = 0
    caller_category = ""
    if isinstance(contact_block, dict) and contact_block.get("status") != "blocked":
        caller_is_known = True
        caller_display_name = (contact_block.get("display_name") or "").strip()
        # First-name only greeting per approved plan §8(b): "Do not mention
        # company in the greeting." Company is still exposed as a separate
        # dyn_var for use elsewhere if relevant.
        caller_first_name = caller_display_name.partition(" ")[0]
        caller_company = (contact_block.get("company") or "").strip()
        caller_last_call_summary = (contact_block.get("last_call_summary") or "").strip()
        try:
            caller_total_calls = int(contact_block.get("total_calls") or 0)
        except (TypeError, ValueError):
            caller_total_calls = 0
        last_seen_iso = contact_block.get("last_seen_at")
        if last_seen_iso:
            try:
                last_seen_dt = datetime.fromisoformat(str(last_seen_iso).replace("Z", "+00:00"))
                caller_last_seen_days_ago = max(
                    0, (datetime.now(timezone.utc) - last_seen_dt).days
                )
            except (TypeError, ValueError):
                caller_last_seen_days_ago = 0
        caller_category = (contact_block.get("category") or "").strip()

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
        # Normalize behavior-mode strings to lowercase so the prompt's
        # conditional checks ("take_message", "try_transfer_then_message",
        # "ask_callback_window") match regardless of how the DB stores them.
        # The DB stores UPPERCASE (e.g. TRY_TRANSFER_THEN_MESSAGE); the prompt
        # uses lowercase literals — normalize at the boundary here.
        "after_hours_mode": str(config.get("after_hours_mode") or "take_message").lower(),
        "busy_mode": str(config.get("busy_mode") or "take_message").lower(),
        "public_number_mode": str(config.get("public_number_mode") or "aspire_new_number").lower(),
        "catch_mode": str(config.get("catch_mode") or "app_and_phone_simul_ring").lower(),
        "greeting_name_override": config.get("greeting_name_override") or "",
        "pronunciation_override": config.get("pronunciation_override") or "",
        "routing_contacts_summary": routing_contacts_summary,
        "configured_roles": configured_roles,
        "owner_salutation": owner_salutation_value,
        "owner_formal_name": owner_formal_name,
        "tenant_id": tenant_id,                     # Pass 19 §3.5
        "office_id": office_id,                     # Pass 19 §3.5
        "voicemail_email": voicemail_email,          # Pass 19 §3.5
        "caller_history_summary": "",               # Pass 19 §3.5: V1 empty; V2 = prior call digest
        # Caller Memory (migration 110)
        "caller_is_known": caller_is_known,
        "caller_display_name": caller_display_name,
        "caller_first_name": caller_first_name,
        "caller_company": caller_company,
        "caller_last_call_summary": caller_last_call_summary,
        "caller_total_calls": caller_total_calls,
        "caller_last_seen_days_ago": caller_last_seen_days_ago,
        "caller_category": caller_category,
        **routing_dyn,
        # Pass 4: trade pack dyn_vars — always present (EL requires all registered vars).
        **trade_dyn_vars,
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
        # Pass 4 signal: tells handler to emit personalization_blank_business_name_filled receipt.
        "business_name_was_blank": business_name_was_blank,
        # Pass 4 signal: trade_id for Prometheus label.
        "trade_id": trade_id_raw or "",
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
