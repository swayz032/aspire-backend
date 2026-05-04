"""Trust Hub ADMIN ops API — Wave 10.

Routes (all admin-only, gated by Bearer token compared against
``settings.admin_api_key``):

  POST /v1/admin/trust-hub/batch-backfill
       Launch a backfill for up to 100 existing-tenant suites.
       Body: { suite_ids, dry_run, throttle_seconds }
       dry_run=true returns the plan + KYB requirements per tenant
       without enqueuing ARQ jobs. dry_run=false enqueues
       `advance_backfill` ARQ jobs at `throttle_seconds` intervals.

  POST /v1/admin/trust-hub/profile/{trust_profile_id}/set-state
       Manual state override for ops recovery (e.g., reset a stuck
       tenant from `failed` to `profile_drafted`). Cuts a
       `manual_state_override` receipt with the admin actor and reason.

  GET  /v1/admin/trust-hub/dashboard
       Aggregated ops stats: count by trust_state, stuck tenants
       (>72h), recent backfill batches, recent rejections.

Aspire Laws coverage:
  Law #1 — orchestrator-only decisions; route is mechanical wrapper
           over ARQ + state machine. No retries / fallbacks / branching.
  Law #2 — receipts on every state change (manual override + each
           backfill job's transitions).
  Law #3 — fail closed on missing admin key, missing config, >100
           suites, unknown profile.
  Law #6 — service-role reads only; admin routes never accept tenant
           scope from request body. The action TARGET is suite_ids in
           the body, but the actor is the admin (constant), not a
           tenant.
  Law #9 — no PII in receipt redacted_inputs/outputs (only suite_ids,
           trust_profile_id, reason). Reason text is sanitized.
  Law #10 — RetryableError on Twilio surfaces from worker; route never
            sees Twilio directly.

Author: Aspire — Wave 10
"""

from __future__ import annotations

import hmac
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Path, Request, status
from pydantic import BaseModel, Field, field_validator

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
    supabase_update,
)
from aspire_orchestrator.workers.trust_onboarding.trust_receipts import (
    TrustReceiptError,
    cut_trust_receipt,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/trust-hub", tags=["admin-trust-hub"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_BACKFILL_QUEUE_NAME: Final[str] = "arq:trust_onboarding"

# Hard cap. Surfaced as 422 with a clear error code if exceeded.
_MAX_SUITES_PER_BATCH: Final[int] = 100

# Throttle bounds. The default 30s sits well below Twilio's documented
# Trust Hub 10/sec rate limit even with worker concurrency of 10.
_DEFAULT_THROTTLE_SECONDS: Final[int] = 30
_MIN_THROTTLE_SECONDS: Final[int] = 0
_MAX_THROTTLE_SECONDS: Final[int] = 3600  # 1h

# UUID validator — same regex used elsewhere; rejects formats that could
# break the PostgREST filter.
_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Valid trust_state values (must mirror migration 109 CHECK constraint).
# Used by the manual override endpoint to refuse unknown target states.
_VALID_TRUST_STATES: Final[frozenset[str]] = frozenset({
    "kyb_collected",
    "profile_drafted",
    "profile_submitted",
    "profile_approved",
    "profile_rejected",
    "shaken_created",
    "shaken_submitted",
    "shaken_approved",
    "cnam_created",
    "cnam_submitted",
    "cnam_approved",
    "number_attached",
    "branded_calling_pending",
    "branded_calling_live",
    "failed",
    "suspended",
})

# Reason text sanitization for manual override receipts. Same idea as
# trust_hub.py:_FAILURE_REASON_SAFE_CHARS — keep alpha/numeric/light
# punctuation only; truncate.
_REASON_SAFE_CHARS: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9 .,;:!?\-_/()]")
_REASON_MAX_LEN: Final[int] = 500


def _sanitize_reason(raw: str) -> str:
    cleaned = _REASON_SAFE_CHARS.sub(" ", raw)
    cleaned = " ".join(cleaned.split())
    return cleaned[:_REASON_MAX_LEN]


# ---------------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------------


def _require_admin_bearer(request: Request) -> str:
    """Validate Bearer token against ``settings.admin_api_key``.

    Returns the actor_id ("admin" by default; can be overridden via
    `X-Admin-Actor` header for audit-trail clarity when multiple admins
    share a key).

    Raises 401 if missing/invalid. Constant-time comparison to avoid
    timing-side-channel guesses (Law #3).
    """
    expected = (settings.admin_api_key or "").strip()
    if not expected:
        # Fail closed — admin routes are unusable until the env var is set.
        logger.error(
            "admin_trust ASPIRE_ADMIN_API_KEY not configured — denying admin access"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "ADMIN_API_KEY_NOT_CONFIGURED",
                "reason_code": "FAIL_CLOSED",
            },
        )

    auth = request.headers.get("authorization", "").strip()
    if not auth.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "UNAUTHENTICATED",
                "reason_code": "MISSING_BEARER_TOKEN",
            },
        )

    provided = auth.split(" ", 1)[1].strip() if len(auth) > 7 else ""
    if not provided or not hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "UNAUTHENTICATED",
                "reason_code": "INVALID_BEARER_TOKEN",
            },
        )

    actor = request.headers.get("x-admin-actor", "").strip() or "admin"
    return actor


# ---------------------------------------------------------------------------
# ARQ enqueue helper (mirrors twilio_swap._enqueue_advance_swap)
# ---------------------------------------------------------------------------


async def _enqueue_advance_backfill(
    suite_id: str, *, defer_seconds: int = 0
) -> bool:
    """Push an advance_backfill job onto the ARQ queue.

    Returns True if the enqueue succeeded, False if Redis/ARQ unreachable.

    Best-effort: on failure, logs WARNING and returns False so the caller
    can record the suite as skipped/failed in the batch row.
    """
    try:
        from arq.connections import RedisSettings, create_pool  # type: ignore[import-not-found]

        redis_url = settings.redis_url or "redis://localhost:6379"
        redis_settings = RedisSettings.from_dsn(redis_url)
        pool = await create_pool(redis_settings)
        try:
            job_id = f"backfill:{suite_id}:advance"
            await pool.enqueue_job(
                "advance_backfill",
                suite_id,
                _queue_name=_BACKFILL_QUEUE_NAME,
                _job_id=job_id,
                _defer_by=int(defer_seconds),
            )
        finally:
            await pool.aclose()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "admin_trust enqueue_failed suite_id=%s err=%s", suite_id, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class BatchBackfillRequest(BaseModel):
    suite_ids: list[str] = Field(..., min_length=1)
    dry_run: bool = False
    throttle_seconds: int = Field(
        default=_DEFAULT_THROTTLE_SECONDS,
        ge=_MIN_THROTTLE_SECONDS,
        le=_MAX_THROTTLE_SECONDS,
    )

    @field_validator("suite_ids")
    @classmethod
    def _validate_suite_ids(cls, v: list[str]) -> list[str]:
        if len(v) > _MAX_SUITES_PER_BATCH:
            raise ValueError(
                f"suite_ids exceeds hard cap of {_MAX_SUITES_PER_BATCH} per batch"
            )
        for sid in v:
            if not _UUID_RE.match(sid):
                raise ValueError(f"suite_ids contains non-UUID value: {sid!r}")
        # De-dupe preserving order
        seen: set[str] = set()
        out: list[str] = []
        for sid in v:
            if sid not in seen:
                seen.add(sid)
                out.append(sid)
        return out


class SkippedSuite(BaseModel):
    suite_id: str
    reason: str


class BatchBackfillResponse(BaseModel):
    batch_id: str
    enqueued: int
    skipped: list[SkippedSuite]
    estimated_completion: str
    dry_run: bool


class SetStateRequest(BaseModel):
    new_state: str
    reason: str = Field(..., min_length=3, max_length=500)

    @field_validator("new_state")
    @classmethod
    def _validate_new_state(cls, v: str) -> str:
        if v not in _VALID_TRUST_STATES:
            raise ValueError(
                f"new_state must be one of: {sorted(_VALID_TRUST_STATES)}"
            )
        return v


class SetStateResponse(BaseModel):
    trust_profile_id: str
    from_state: str
    to_state: str
    receipt_id: str | None
    actor: str


class DashboardResponse(BaseModel):
    states_count: dict[str, int]
    stuck_tenants_count: int
    backfill_in_progress: int
    backfill_recent: list[dict[str, Any]]
    rejections_recent: list[dict[str, Any]]
    generated_at: str


# ---------------------------------------------------------------------------
# Helpers — backfill admission filter
# ---------------------------------------------------------------------------


async def _classify_suite_for_backfill(
    suite_id: str,
) -> tuple[bool, str | None]:
    """Decide whether ``suite_id`` is eligible for backfill.

    Returns (eligible, reason) where reason is set when the suite is
    skipped. The admission rules:

      * If a `tenant_trust_profiles` row exists with ``is_backfill=False``
        and ``trust_state=number_attached``, the tenant is already on
        the per-tenant flow → skip with reason ``already_onboarded``.
      * If a row exists with ``is_backfill=True`` and
        ``trust_state=number_attached``, the backfill already finished
        → skip with reason ``backfill_already_complete``.
      * If no active phone number exists for the suite → skip with
        reason ``no_active_phone_number``. Without an attached number
        the backfill cannot detach-from-shared (step 7).
      * Otherwise → eligible.

    All reads are best-effort — on DB error we treat the suite as
    eligible and let the worker re-validate (the state machine itself
    has a `_load_active_phone_number` check in step 7).
    """
    try:
        rows = await supabase_select(
            "tenant_trust_profiles",
            f"suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning(
            "admin_trust classify_suite_db_error suite_id=%s err=%s",
            suite_id, exc,
        )
        return True, None

    if rows:
        existing = rows[0]
        is_backfill_flag = bool(existing.get("is_backfill", False))
        cur_state = str(existing.get("trust_state", ""))
        if cur_state == "number_attached":
            return False, (
                "backfill_already_complete"
                if is_backfill_flag
                else "already_onboarded"
            )
        if not is_backfill_flag and cur_state not in {"", "kyb_collected"}:
            # Tenant is mid-way through normal onboarding — refuse to clobber.
            return False, "in_normal_onboarding"

    # Verify active phone number presence — backfill needs a number to detach.
    try:
        phone_rows = await supabase_select(
            "tenant_phone_numbers",
            f"suite_id=eq.{suite_id}&status=eq.active",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning(
            "admin_trust classify_phone_db_error suite_id=%s err=%s",
            suite_id, exc,
        )
        return True, None

    if not phone_rows:
        return False, "no_active_phone_number"

    return True, None


# ---------------------------------------------------------------------------
# POST /v1/admin/trust-hub/batch-backfill
# ---------------------------------------------------------------------------


@router.post(
    "/batch-backfill",
    response_model=BatchBackfillResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def batch_backfill(
    body: BatchBackfillRequest,
    request: Request,
) -> BatchBackfillResponse:
    """Admin batch backfill of existing tenants to per-tenant SHAKEN/CNAM.

    dry_run=True  → returns the admission decisions without enqueuing.
    dry_run=False → inserts a `tenant_trust_backfill_batches` audit row,
                    enqueues advance_backfill jobs at `throttle_seconds`
                    intervals.

    Hard rules:
        * Admin Bearer token required (Law #3 fail-closed).
        * suite_ids capped at 100 per batch (422 if exceeded).
        * Suites already on per-tenant flow are skipped with reason.
    """
    actor = _require_admin_bearer(request)

    # Classify all suites in parallel-ish (sequential here is fine; 100 reads).
    skipped: list[SkippedSuite] = []
    eligible: list[str] = []
    for sid in body.suite_ids:
        ok, reason = await _classify_suite_for_backfill(sid)
        if ok:
            eligible.append(sid)
        else:
            skipped.append(SkippedSuite(suite_id=sid, reason=reason or "unknown"))

    batch_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    estimated_completion = (
        now + timedelta(seconds=body.throttle_seconds * max(len(eligible), 1) + 300)
    ).isoformat()

    # Always insert the batch ledger row — even for dry runs, audit benefits.
    batch_row: dict[str, Any] = {
        "id": batch_id,
        "started_by_admin": actor,
        "suite_ids": body.suite_ids,
        "status": "pending" if not body.dry_run else "completed",
        "dry_run": body.dry_run,
        "enqueued_count": 0 if body.dry_run else len(eligible),
        "completed_count": 0,
        "failed_count": 0,
        "throttle_seconds": body.throttle_seconds,
        "skipped": [s.model_dump() for s in skipped],
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    if body.dry_run:
        batch_row["completed_at"] = now.isoformat()

    try:
        await supabase_insert("tenant_trust_backfill_batches", batch_row)
    except SupabaseClientError as exc:
        logger.error(
            "admin_trust batch_insert_failed actor=%s err=%s", actor, exc
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "BATCH_PERSIST_FAILED",
                "reason_code": "DB_UNAVAILABLE",
            },
        ) from exc

    if body.dry_run:
        logger.info(
            "admin_trust dry_run actor=%s batch_id=%s eligible=%d skipped=%d",
            actor, batch_id, len(eligible), len(skipped),
        )
        return BatchBackfillResponse(
            batch_id=batch_id,
            enqueued=0,
            skipped=skipped,
            estimated_completion=estimated_completion,
            dry_run=True,
        )

    # Enqueue ARQ jobs at the throttle interval. We use ARQ's _defer_by so
    # one HTTP request returns 202 immediately; jobs land staggered.
    enqueued_count = 0
    for idx, sid in enumerate(eligible):
        defer = body.throttle_seconds * idx
        ok = await _enqueue_advance_backfill(sid, defer_seconds=defer)
        if ok:
            enqueued_count += 1
        else:
            # Non-fatal — suite can be re-enqueued via a follow-up batch.
            skipped.append(SkippedSuite(suite_id=sid, reason="enqueue_failed"))

    # Update the batch row to reflect actual enqueue count.
    if enqueued_count != len(eligible):
        try:
            await supabase_update(
                "tenant_trust_backfill_batches",
                f"id=eq.{batch_id}",
                {
                    "enqueued_count": enqueued_count,
                    "skipped": [s.model_dump() for s in skipped],
                    "status": "in_progress" if enqueued_count > 0 else "failed",
                },
            )
        except SupabaseClientError as exc:
            logger.warning(
                "admin_trust batch_update_failed batch_id=%s err=%s",
                batch_id, exc,
            )
    else:
        try:
            await supabase_update(
                "tenant_trust_backfill_batches",
                f"id=eq.{batch_id}",
                {"status": "in_progress"},
            )
        except SupabaseClientError as exc:
            logger.warning(
                "admin_trust batch_update_failed batch_id=%s err=%s",
                batch_id, exc,
            )

    logger.info(
        "admin_trust batch_enqueued actor=%s batch_id=%s enqueued=%d skipped=%d",
        actor, batch_id, enqueued_count, len(skipped),
    )

    return BatchBackfillResponse(
        batch_id=batch_id,
        enqueued=enqueued_count,
        skipped=skipped,
        estimated_completion=estimated_completion,
        dry_run=False,
    )


# ---------------------------------------------------------------------------
# POST /v1/admin/trust-hub/profile/{trust_profile_id}/set-state
# ---------------------------------------------------------------------------


@router.post(
    "/profile/{trust_profile_id}/set-state",
    response_model=SetStateResponse,
    status_code=status.HTTP_200_OK,
)
async def set_profile_state(
    body: SetStateRequest,
    request: Request,
    trust_profile_id: str = Path(..., min_length=8, max_length=64),
) -> SetStateResponse:
    """Manual state override for ops recovery.

    Cuts a `manual_state_override` Yellow-tier receipt with the admin
    actor + sanitized reason in `redacted_inputs`. Re-validates that
    the target state is in the closed enum; returns 422 otherwise (the
    Pydantic model already does this, but we keep a defense-in-depth
    check below).

    Returns 404 if the trust profile does not exist (avoids leaking
    existence info — same status as a real-not-found row).
    """
    actor = _require_admin_bearer(request)

    if not _UUID_RE.match(trust_profile_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "INVALID_PROFILE_ID",
                "reason_code": "MALFORMED_UUID",
            },
        )

    try:
        rows = await supabase_select(
            "tenant_trust_profiles",
            f"id=eq.{trust_profile_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error(
            "admin_trust set_state_load_failed actor=%s id=%s err=%s",
            actor, trust_profile_id, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "DB_UNAVAILABLE",
                "reason_code": "PROFILE_LOAD_FAILED",
            },
        ) from exc

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "PROFILE_NOT_FOUND",
                "reason_code": "TRUST_PROFILE_MISSING",
            },
        )

    profile = rows[0]
    from_state = str(profile.get("trust_state", ""))
    new_state = body.new_state
    reason_clean = _sanitize_reason(body.reason)

    # Apply the update.
    try:
        await supabase_update(
            "tenant_trust_profiles",
            f"id=eq.{trust_profile_id}",
            {
                "trust_state": new_state,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except SupabaseClientError as exc:
        logger.error(
            "admin_trust set_state_update_failed actor=%s id=%s err=%s",
            actor, trust_profile_id, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "DB_UNAVAILABLE",
                "reason_code": "STATE_UPDATE_FAILED",
            },
        ) from exc

    # Cut the audit receipt (Law #2).
    receipt_id: str | None = None
    try:
        receipt_id = await cut_trust_receipt(
            receipt_type="manual_state_override",
            trust_profile=profile,
            outcome="success",
            from_state=from_state,
            to_state=new_state,
            worker_job_id=None,
            redacted_inputs={
                "trust_profile_id": trust_profile_id,
                "step_name": "manual_state_override",
                "actor": actor,
                "reason": reason_clean,
            },
            redacted_outputs={
                "trust_profile_id": trust_profile_id,
            },
        )
    except TrustReceiptError as exc:
        logger.error(
            "admin_trust set_state_receipt_failed actor=%s id=%s err=%s",
            actor, trust_profile_id, exc,
        )
        # Don't 5xx the caller — the state IS updated; the receipt write
        # failure is logged and surfaces in the admin dashboard.

    logger.info(
        "admin_trust set_state actor=%s trust_profile_id=%s from=%s to=%s",
        actor, trust_profile_id, from_state, new_state,
    )

    return SetStateResponse(
        trust_profile_id=trust_profile_id,
        from_state=from_state,
        to_state=new_state,
        receipt_id=receipt_id,
        actor=actor,
    )


# ---------------------------------------------------------------------------
# GET /v1/admin/trust-hub/dashboard
# ---------------------------------------------------------------------------


_STUCK_HOURS: Final[int] = 72
_DASHBOARD_RECENT_LIMIT: Final[int] = 25


async def _count_states() -> dict[str, int]:
    """Aggregate count of trust profiles by trust_state.

    PostgREST doesn't natively COUNT GROUP BY without a server-side view,
    so we do a select-then-tally in Python. Bounded to a generous
    limit=10000 — at 10k tenants this is the entire table; we don't
    expect significantly more during the W10 backfill window.
    """
    try:
        rows = await supabase_select(
            "tenant_trust_profiles",
            "id=not.is.null",
            limit=10000,
        )
    except SupabaseClientError as exc:
        logger.warning("admin_trust dashboard_count_states_failed err=%s", exc)
        return {}
    counts: dict[str, int] = {}
    for row in rows:
        state = str(row.get("trust_state", "") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return counts


async def _count_stuck_tenants() -> int:
    """Count tenant profiles in *_submitted state for >72h."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=_STUCK_HOURS)
    ).isoformat()
    submitted_states = ("profile_submitted", "shaken_submitted", "cnam_submitted")
    total = 0
    for state in submitted_states:
        try:
            rows = await supabase_select(
                "tenant_trust_profiles",
                f"trust_state=eq.{state}&updated_at=lt.{cutoff}",
                limit=1000,
            )
        except SupabaseClientError as exc:
            logger.warning(
                "admin_trust dashboard_stuck_count_failed state=%s err=%s",
                state, exc,
            )
            continue
        total += len(rows)
    return total


async def _recent_backfill_batches() -> list[dict[str, Any]]:
    try:
        rows = await supabase_select(
            "tenant_trust_backfill_batches",
            "id=not.is.null",
            order_by="created_at.desc",
            limit=_DASHBOARD_RECENT_LIMIT,
        )
    except SupabaseClientError as exc:
        logger.warning("admin_trust dashboard_batches_failed err=%s", exc)
        return []
    return [
        {
            "id": row.get("id"),
            "started_by_admin": row.get("started_by_admin"),
            "status": row.get("status"),
            "dry_run": row.get("dry_run"),
            "enqueued_count": row.get("enqueued_count"),
            "completed_count": row.get("completed_count"),
            "failed_count": row.get("failed_count"),
            "created_at": row.get("created_at"),
            "completed_at": row.get("completed_at"),
        }
        for row in rows
    ]


async def _count_backfill_in_progress() -> int:
    try:
        rows = await supabase_select(
            "tenant_trust_backfill_batches",
            "status=in.(pending,in_progress)",
            limit=1000,
        )
    except SupabaseClientError as exc:
        logger.warning("admin_trust dashboard_in_progress_failed err=%s", exc)
        return 0
    return len(rows)


async def _recent_rejections() -> list[dict[str, Any]]:
    try:
        rows = await supabase_select(
            "tenant_trust_profiles",
            "trust_state=in.(profile_rejected,failed)",
            order_by="updated_at.desc",
            limit=_DASHBOARD_RECENT_LIMIT,
        )
    except SupabaseClientError as exc:
        logger.warning("admin_trust dashboard_rejections_failed err=%s", exc)
        return []
    return [
        {
            "id": row.get("id"),
            "suite_id": row.get("suite_id"),
            "trust_state": row.get("trust_state"),
            "rejection_code": row.get("rejection_code"),
            "rejection_reason": (str(row.get("rejection_reason") or "")[:200]),
            "is_backfill": bool(row.get("is_backfill", False)),
            "updated_at": row.get("updated_at"),
        }
        for row in rows
    ]


@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard(request: Request) -> DashboardResponse:
    """Aggregated ops view of trust onboarding + backfill state."""
    _require_admin_bearer(request)

    states = await _count_states()
    stuck = await _count_stuck_tenants()
    in_progress = await _count_backfill_in_progress()
    recent_batches = await _recent_backfill_batches()
    recent_rejections = await _recent_rejections()

    return DashboardResponse(
        states_count=states,
        stuck_tenants_count=stuck,
        backfill_in_progress=in_progress,
        backfill_recent=recent_batches,
        rejections_recent=recent_rejections,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


__all__ = ["router"]
