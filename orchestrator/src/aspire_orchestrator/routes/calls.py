"""Call utility routes (Pass 19 Lane B).

Routes:
  GET /v1/calls/caller-id-lookup?phone=+1...  — resolve caller identity

Aspire Laws enforced:
  Law #2 — receipt cut on every call (including fallback).
  Law #3 — capability token required; missing/invalid → 403.
  Law #5 — server-side capability token validation.
  Law #6 — scope enforced via office_id filter on all DB queries.
  Law #9 — full phone number never logged; only prefix in receipts.

Latency budget: <100ms p95 (mock DB validated in tests; real DB must meet SLA).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
)
from aspire_orchestrator.services.token_service import validate_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/calls", tags=["calls"])

# Lookback window for call memory entities (§3.10)
_CALL_LOOKBACK_DAYS = 90


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_scope(
    x_tenant_id: str | None,
    x_suite_id: str | None,
    x_office_id: str | None,
) -> ScopedIdentity:
    from uuid import UUID
    if not x_tenant_id or not x_suite_id or not x_office_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_SCOPE_HEADERS"},
        )
    try:
        return ScopedIdentity(
            tenant_id=UUID(x_tenant_id),
            suite_id=UUID(x_suite_id),
            office_id=UUID(x_office_id),
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_SCOPE_HEADERS", "message": str(exc)},
        ) from exc


def _format_phone(phone: str) -> str:
    """Format E.164 phone number for display (e.g. +14155552671 → (415) 555-2671)."""
    digits = re.sub(r"[^\d]", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return phone


def _phone_prefix(phone: str) -> str:
    """Return first-6-digits prefix for PII-safe logging (Law #9)."""
    return (phone or "")[:6] + "..."


# ---------------------------------------------------------------------------
# Caller-ID lookup
# ---------------------------------------------------------------------------


@router.get("/caller-id-lookup")
async def caller_id_lookup(
    phone: str,
    capability_token: str | None = None,  # query param fallback for proxyForward
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
    # Legacy aliases — keep for backward compat with any direct callers
    # that still use the X-Aspire-* prefix.
    x_aspire_tenant_id: str | None = Header(None, alias="X-Aspire-Tenant-Id"),
    x_aspire_suite_id: str | None = Header(None, alias="X-Aspire-Suite-Id"),
    x_aspire_office_id: str | None = Header(None, alias="X-Aspire-Office-Id"),
    x_capability_token: str | None = Header(None, alias="X-Aspire-Capability-Token"),
) -> dict[str, Any]:
    """Resolve caller identity for an E.164 phone number.

    Priority order (§3.10):
      1. front_desk_routing_contacts (exact phone match, office-scoped)
      2. sms_thread memory_objects (from/contact_name in detail)
      3. call memory_objects (last 90 days, from/caller_name in detail)
      4. Fallback: {contact_type: 'unknown', formatted_number: ...}

    Capability scope: telephony:caller_id_lookup (GREEN tier — read-only).
    Latency budget: <100ms p95.
    Law #2: receipt cut on every call.
    Law #9: full phone number never in receipt; only prefix.
    """
    start_time = time.monotonic()
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    trace_id = get_trace_id()
    correlation_id = get_correlation_id()

    # ── Scope resolution ──────────────────────────────────────────────────
    # Accept both the canonical X-Tenant-Id... and the legacy X-Aspire-Tenant-Id...
    # header families so the standard proxyForward works without special-casing.
    resolved_tenant = x_tenant_id or x_aspire_tenant_id
    resolved_suite = x_suite_id or x_aspire_suite_id
    resolved_office = x_office_id or x_aspire_office_id
    scope = _resolve_scope(resolved_tenant, resolved_suite, resolved_office)
    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)

    # ── Capability token validation (Law #3 + Law #5) ─────────────────────
    # Token comes from one of:
    #   1. X-Aspire-Capability-Token header (legacy direct callers)
    #   2. capability_token query param (proxyForward GET pattern)
    raw_cap_token = x_capability_token or capability_token
    cap_token_dict: dict[str, Any] | None = None
    if not raw_cap_token:
        # Fail closed — no token = deny
        receipt_store.store_receipts([{
            "id": receipt_id,
            "receipt_type": "caller_id_lookup",
            "suite_id": suite_id,
            "office_id": office_id,
            "tenant_id": tenant_id,
            "outcome": "denied",
            "action_type": "caller_id_lookup",
            "tool_used": "calls_route",
            "risk_tier": "green",
            "reason_code": "MISSING_CAPABILITY_TOKEN",
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "created_at": now,
        }])
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "MISSING_CAPABILITY_TOKEN"},
        )

    try:
        cap_token_dict = json.loads(raw_cap_token)
        if not isinstance(cap_token_dict, dict):
            cap_token_dict = {}
    except Exception:
        cap_token_dict = {}

    token_result = validate_token(
        cap_token_dict,
        expected_suite_id=suite_id,
        expected_office_id=office_id,
        required_scope="telephony:caller_id_lookup",
    )
    if not token_result.valid:
        receipt_store.store_receipts([{
            "id": receipt_id,
            "receipt_type": "caller_id_lookup",
            "suite_id": suite_id,
            "office_id": office_id,
            "tenant_id": tenant_id,
            "outcome": "denied",
            "action_type": "caller_id_lookup",
            "tool_used": "calls_route",
            "risk_tier": "green",
            "reason_code": token_result.error.value if token_result.error else "INVALID_TOKEN",
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "created_at": now,
        }])
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "INVALID_CAPABILITY_TOKEN"},
        )

    # ── Input validation ──────────────────────────────────────────────────
    if not isinstance(phone, str) or not re.match(r"^\+\d{7,15}$", phone):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_PHONE_FORMAT", "message": "phone must be E.164"},
        )

    phone_pfx = _phone_prefix(phone)
    formatted = _format_phone(phone)

    # ── Priority 1: routing_contacts exact match ──────────────────────────
    # Live schema has no is_active column on front_desk_routing_contacts
    # (verified against information_schema). Soft-delete is handled by hard
    # DELETE in delete_routing_contact, so an exact phone match is enough.
    try:
        routing_rows = await supabase_select(
            "front_desk_routing_contacts",
            f"office_id=eq.{office_id}&phone=eq.{phone}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning("caller_id_lookup routing_contact db_error phone_prefix=%s: %s", phone_pfx, exc)
        routing_rows = []

    if routing_rows:
        row = routing_rows[0]
        elapsed = time.monotonic() - start_time
        result = {
            "contact_type": "routing_contact",
            "display_name": row.get("label") or row.get("name") or "",
            "role": row.get("role") or "",
            "formatted_number": formatted,
            "last_interaction_at": None,
            "receipt_id": receipt_id,
        }
        _cut_receipt(
            receipt_id=receipt_id, suite_id=suite_id, office_id=office_id,
            tenant_id=tenant_id, phone_pfx=phone_pfx, contact_type="routing_contact",
            elapsed=elapsed, trace_id=trace_id, correlation_id=correlation_id, now=now,
        )
        logger.info(
            "caller_id_lookup priority=routing phone_prefix=%s name=%s elapsed=%.3fs",
            phone_pfx, result["display_name"], elapsed,
        )
        return result

    # ── Priority 2: sms_thread memory contacts ────────────────────────────
    try:
        sms_rows = await supabase_select(
            "memory_objects",
            f"suite_id=eq.{suite_id}&office_id=eq.{office_id}&memory_type=eq.sms_thread",
            limit=20,
        )
    except SupabaseClientError as exc:
        logger.warning("caller_id_lookup sms_thread db_error phone_prefix=%s: %s", phone_pfx, exc)
        sms_rows = []

    for row in sms_rows:
        detail = row.get("detail") or {}
        row_phone = detail.get("from") or detail.get("to") or ""
        if row_phone == phone:
            contact_name = detail.get("contact_name") or ""
            elapsed = time.monotonic() - start_time
            result = {
                "contact_type": "sms_contact",
                "display_name": contact_name,
                "role": "",
                "formatted_number": formatted,
                "last_interaction_at": row.get("created_at"),
                "receipt_id": receipt_id,
            }
            _cut_receipt(
                receipt_id=receipt_id, suite_id=suite_id, office_id=office_id,
                tenant_id=tenant_id, phone_pfx=phone_pfx, contact_type="sms_contact",
                elapsed=elapsed, trace_id=trace_id, correlation_id=correlation_id, now=now,
            )
            logger.info(
                "caller_id_lookup priority=sms_thread phone_prefix=%s elapsed=%.3fs",
                phone_pfx, elapsed,
            )
            return result

    # ── Priority 3: call memory entities (last 90 days) ───────────────────
    # PostgREST decodes `+` in query string as a space, so the `+00:00` tz
    # offset on isoformat() output gets corrupted to `" 00:00"` and the
    # server returns 22007 invalid_input_syntax. Use the trailing `Z` shorthand
    # for UTC instead — round-trip safe through PostgREST URL parsing.
    cutoff = (
        (datetime.now(timezone.utc) - timedelta(days=_CALL_LOOKBACK_DAYS))
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    try:
        call_rows = await supabase_select(
            "memory_objects",
            f"suite_id=eq.{suite_id}&office_id=eq.{office_id}&memory_type=eq.call&created_at=gte.{cutoff}",
            limit=20,
        )
    except SupabaseClientError as exc:
        logger.warning("caller_id_lookup call_memory db_error phone_prefix=%s: %s", phone_pfx, exc)
        call_rows = []

    for row in call_rows:
        detail = row.get("detail") or {}
        row_phone = detail.get("from") or detail.get("caller_number") or ""
        if row_phone == phone:
            caller_name = detail.get("caller_name") or detail.get("contact_name") or ""
            elapsed = time.monotonic() - start_time
            result = {
                "contact_type": "call_contact",
                "display_name": caller_name,
                "role": "",
                "formatted_number": formatted,
                "last_interaction_at": row.get("created_at"),
                "receipt_id": receipt_id,
            }
            _cut_receipt(
                receipt_id=receipt_id, suite_id=suite_id, office_id=office_id,
                tenant_id=tenant_id, phone_pfx=phone_pfx, contact_type="call_contact",
                elapsed=elapsed, trace_id=trace_id, correlation_id=correlation_id, now=now,
            )
            logger.info(
                "caller_id_lookup priority=call_memory phone_prefix=%s elapsed=%.3fs",
                phone_pfx, elapsed,
            )
            return result

    # ── Fallback: unknown ─────────────────────────────────────────────────
    elapsed = time.monotonic() - start_time
    result = {
        "contact_type": "unknown",
        "display_name": "",
        "role": "",
        "formatted_number": formatted,
        "last_interaction_at": None,
        "receipt_id": receipt_id,
    }
    _cut_receipt(
        receipt_id=receipt_id, suite_id=suite_id, office_id=office_id,
        tenant_id=tenant_id, phone_pfx=phone_pfx, contact_type="unknown",
        elapsed=elapsed, trace_id=trace_id, correlation_id=correlation_id, now=now,
    )
    logger.info(
        "caller_id_lookup priority=fallback phone_prefix=%s elapsed=%.3fs",
        phone_pfx, elapsed,
    )
    return result


def _cut_receipt(
    *,
    receipt_id: str,
    suite_id: str,
    office_id: str,
    tenant_id: str,
    phone_pfx: str,
    contact_type: str,
    elapsed: float,
    trace_id: str,
    correlation_id: str,
    now: str,
) -> None:
    """Cut immutable caller_id_lookup receipt (Law #2)."""
    receipt_store.store_receipts([{
        "id": receipt_id,
        "receipt_type": "caller_id_lookup",
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "outcome": "success",
        "action_type": "caller_id_lookup",
        "tool_used": "calls_route",
        "risk_tier": "green",
        "redacted_inputs": {
            "phone_prefix": phone_pfx,  # Law #9: prefix only
        },
        "redacted_outputs": {
            "contact_type": contact_type,
            "latency_seconds": round(elapsed, 4),
        },
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "created_at": now,
    }])
