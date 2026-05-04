"""CNAM display-name change REST API — Wave 9.

Routes:
  POST /v1/trust-hub/cnam-change   — Yellow tier, scope trust_hub:cnam_change

Law compliance:
  Law #1 — no autonomous decisions; route only validates + enqueues.
  Law #2 — receipt cut by the apply_cnam_display_name_change job (W9 cron).
  Law #3 — fail closed: missing token → 401, vault down → 503,
           cooldown not met → 409 COOLDOWN_NOT_MET.
  Law #5 — capability tokens validated server-side (Yellow-gated route).
  Law #6 — tenant scope from X- headers only; never from request body.
  Law #9 — raw business name accepted as input but the receipt only ever
           sees the SANITIZED 15-char display string.

Table assumptions (migration 118):
  tenant_cnam_change_requests — INSERTed by this route, ARQ job picked up
                                by the W9 hourly cron.
  tenant_trust_profiles       — read for last_cnam_change_at + trust_state.

ARQ queue: 'arq:trust_onboarding' — job 'apply_cnam_display_name_change'
is enqueued IMMEDIATELY when the cooldown is satisfied; otherwise the
hourly cron retries the request when the cooldown elapses.

Author: Aspire — Wave 9 (per ~/.claude/plans/the-image-was-off-calm-lynx.md §3 W9)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.routes.front_desk import (
    _cap_token_id,
    _resolve_scope,
    _validate_cap_token,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
)
from aspire_orchestrator.workers.trust_onboarding.cnam_sanitizer import (
    sanitize_cnam_display_name,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/trust-hub", tags=["trust-hub"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHANGE_QUEUE_NAME: Final[str] = "arq:trust_onboarding"

# Twilio's CNAM display-name change cooldown (kept in lockstep with cron_jobs.py).
_CNAM_CHANGE_COOLDOWN_DAYS: Final[int] = 30

# A change request is only valid when the tenant has a CNAM bundle. We
# require the trust profile to be in number_attached (the only state that
# guarantees the CNAM Trust Product + EndUser exist).
_CHANGE_ALLOWED_STATES: Final[frozenset[str]] = frozenset({
    "number_attached",
    "branded_calling_pending",
    "branded_calling_live",
})

# Average Twilio CNAM re-review takes 24-72h; we surface 48h as a friendly
# midpoint estimate for the FE banner.
_ESTIMATED_COMPLETION_HOURS: Final[int] = 48


# ---------------------------------------------------------------------------
# ARQ enqueue helper
# ---------------------------------------------------------------------------


async def _enqueue_apply_cnam_change(request_id: str) -> bool:
    """Best-effort enqueue of apply_cnam_display_name_change. Returns success bool.

    On Redis failure we still return 200 to the caller — the W9 hourly
    cron will pick the request up on its next run.
    """
    try:
        from arq.connections import RedisSettings, create_pool  # type: ignore[import-not-found]

        redis_url = settings.redis_url or "redis://localhost:6379"
        redis_settings = RedisSettings.from_dsn(redis_url)
        pool = await create_pool(redis_settings)
        try:
            job_id = f"cnam_change:{request_id}:apply"
            await pool.enqueue_job(
                "apply_cnam_display_name_change",
                request_id,
                _queue_name=_CHANGE_QUEUE_NAME,
                _job_id=job_id,
                _defer_by=0,
            )
        finally:
            await pool.aclose()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "trust_cnam_change arq_enqueue_failed request_id=%s err=%s",
            request_id, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CnamChangeRequest(BaseModel):
    new_display_name: str = Field(..., min_length=2, max_length=120)
    capability_token: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# POST /v1/trust-hub/cnam-change — Yellow tier, scope trust_hub:cnam_change
# ---------------------------------------------------------------------------


@router.post("/cnam-change", status_code=status.HTTP_202_ACCEPTED)
async def cnam_change(
    body: CnamChangeRequest,
    x_tenant_id: str | None = Header(None),
    x_suite_id: str | None = Header(None),
    x_office_id: str | None = Header(None),
) -> dict[str, Any]:
    """Initiate a CNAM display-name change for the tenant.

    Validates:
      1. Capability token (Yellow, scope trust_hub:cnam_change).
      2. Trust profile exists and is in an allowed state.
      3. 30-day Twilio cooldown not violated server-side.
      4. New display name passes the cnam_sanitizer (15-char rules).
      5. No in-flight change for the same suite (UNIQUE index defends).

    Creates:
      - tenant_cnam_change_requests row (status='pending').
      - Enqueues apply_cnam_display_name_change ARQ job immediately.

    Returns:
      {
        "request_id": "uuid",
        "sanitized_display_name": "SCOTT PAINTING",
        "estimated_completion": "2026-05-06T14:00:00Z"
      }
    """
    # --- 1. Scope resolution (Law #6) ---
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)

    # --- 2. Capability token (Law #5, Yellow tier) ---
    _validate_cap_token(body.capability_token, scope, "trust_hub:cnam_change")
    cap_token_id = _cap_token_id(body.capability_token)

    suite_id = str(scope.suite_id)
    tenant_id = str(scope.tenant_id)
    office_id = str(scope.office_id)

    # --- 3. Sanitize new display name (fail-closed on garbage) ---
    try:
        sanitized = sanitize_cnam_display_name(body.new_display_name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "INVALID_DISPLAY_NAME",
                "reason_code": "INVALID_DISPLAY_NAME",
                "message": str(exc),
            },
        ) from exc

    # --- 4. Load trust profile (must exist and be in an allowed state) ---
    try:
        profile_rows = await supabase_select(
            "tenant_trust_profiles",
            f"suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error("trust_cnam_change profile_select_failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    if not profile_rows:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "NO_TRUST_PROFILE",
                "reason_code": "NO_TRUST_PROFILE",
                "message": (
                    "No trust profile found. Complete KYB onboarding and CNAM "
                    "verification before changing your display name."
                ),
            },
        )

    profile = profile_rows[0]
    trust_profile_id = str(profile["id"])
    trust_state = str(profile.get("trust_state", ""))
    if trust_state not in _CHANGE_ALLOWED_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "PROFILE_NOT_READY_FOR_CHANGE",
                "reason_code": "PROFILE_NOT_READY_FOR_CHANGE",
                "trust_state": trust_state,
                "message": (
                    f"CNAM display-name change requires trust_state in "
                    f"{sorted(_CHANGE_ALLOWED_STATES)!r}. "
                    f"Current state: {trust_state!r}."
                ),
            },
        )

    # --- 5. 30-day cooldown enforcement (Twilio policy) ---
    last_change_raw = profile.get("last_cnam_change_at")
    if last_change_raw:
        try:
            last_change = datetime.fromisoformat(
                str(last_change_raw).replace("Z", "+00:00")
            )
        except ValueError:
            # Unparseable — fail closed: surface 503 so ops can audit.
            logger.error(
                "trust_cnam_change cooldown_parse_failed suite=%s value=%r",
                suite_id, last_change_raw,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "COOLDOWN_PARSE_FAILED"},
            )
        next_eligible = last_change + timedelta(days=_CNAM_CHANGE_COOLDOWN_DAYS)
        if datetime.now(timezone.utc) < next_eligible:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "COOLDOWN_NOT_MET",
                    "reason_code": "COOLDOWN_NOT_MET",
                    "next_eligible_at": next_eligible.isoformat(),
                    "cooldown_days": _CNAM_CHANGE_COOLDOWN_DAYS,
                    "message": (
                        "Twilio policy allows at most one CNAM display-name "
                        f"change per {_CNAM_CHANGE_COOLDOWN_DAYS} days. Try "
                        f"again after {next_eligible.isoformat()}."
                    ),
                },
            )

    # --- 6. INSERT request row ---
    request_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    estimated_completion = (
        datetime.now(timezone.utc) + timedelta(hours=_ESTIMATED_COMPLETION_HOURS)
    ).isoformat()

    request_row: dict[str, Any] = {
        "id": request_id,
        "tenant_id": tenant_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "trust_profile_id": trust_profile_id,
        "requested_display_name": body.new_display_name,
        "sanitized_display_name": sanitized,
        "status": "pending",
        "capability_token_id": cap_token_id or None,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    try:
        await supabase_insert("tenant_cnam_change_requests", request_row)
    except SupabaseClientError as exc:
        detail_str = str(exc.detail or "").lower()
        if "duplicate" in detail_str or "unique" in detail_str or exc.status_code == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "CHANGE_ALREADY_IN_PROGRESS",
                    "reason_code": "CHANGE_ALREADY_IN_PROGRESS",
                },
            ) from exc
        logger.error("trust_cnam_change request_insert_failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    # --- 7. Enqueue ARQ job (best-effort; cron retries on Redis failure) ---
    await _enqueue_apply_cnam_change(request_id)

    logger.info(
        "trust_cnam_change request_created request_id=%s suite=%s sanitized_len=%d cap_token=%s",
        request_id, suite_id, len(sanitized), cap_token_id or "<none>",
    )

    return {
        "request_id": request_id,
        "sanitized_display_name": sanitized,
        "estimated_completion": estimated_completion,
    }


__all__ = ["router"]
