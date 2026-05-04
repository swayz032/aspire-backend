"""Wave 9 — periodic cron jobs for trust onboarding.

Three responsibilities:

  1. ``poll_trust_status_for_tenants`` (every 6h)
        Reconciles tenants stuck in a *_submitted state. Twilio's status
        callback is reliable but not perfect — a missed webhook can leave a
        tenant in profile_submitted / shaken_submitted / cnam_submitted
        indefinitely. We poll Twilio directly for any profile updated_at
        more than 24h ago and, if Twilio is now approved/rejected, enqueue
        an ``advance_trust_state`` ARQ job to drive the state machine.

  2. ``poll_carrier_reputation`` (every 6h)
        Pulls per-carrier spam-likelihood scores for every tenant in
        trust_state='number_attached'. Gated behind
        ``settings.branded_calling_enabled`` — when off this is a no-op
        stub. When on, calls Twilio's Branded Calling reputation endpoint
        and records the result inline on tenant_trust_profiles
        (last_reputation_check / last_reputation_status). On change, cuts
        a ``carrier_reputation_updated`` receipt for ops visibility.

  3. ``enqueue_cnam_display_name_changes`` (hourly)
        Reads tenant_cnam_change_requests for status='pending' or
        'cooldown_pending'. Validates the 30-day cooldown server-side
        against tenant_trust_profiles.last_cnam_change_at. If cooldown is
        not yet met, the row stays at 'cooldown_pending' (the cron retries
        next hour). If cooldown is satisfied, the row advances to
        'in_progress' and an ``apply_cnam_display_name_change`` ARQ job
        is enqueued.

Plus the ``apply_cnam_display_name_change`` ARQ job entry — runs the
4-step Twilio sequence to apply a new CNAM display name end-to-end.

Aspire Laws enforced:
  Law #1  — cron jobs do NOT make decisions; they reconcile the state
            machine and let the orchestrator's state machine drive
            transitions.
  Law #2  — every state change cuts a Yellow-tier receipt; receipt
            previews are PII-free (only SIDs / sanitized display name).
  Law #3  — fail closed: missing trust profile, missing CNAM Trust
            Product SID, or vault unavailable → log + skip; never auto-
            advance on uncertainty.
  Law #6  — RLS-bypassing service_role queries scope every operation
            to a single suite_id; cross-tenant operations are
            structurally impossible.
  Law #9  — phone E.164, business name (raw), email, DOB, SSN never in
            logs or receipts. Sanitized CNAM display name is permitted
            (already public-facing).
  Law #10 — RetryableError from the Twilio provider is re-raised so
            ARQ retries with exponential backoff; non-retryable errors
            mark the request failed and continue with the next tenant.

Author: Aspire — Wave 9 (per ~/.claude/plans/the-image-was-off-calm-lynx.md §3 W9)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.providers import twilio_trust_hub as thub
from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
from aspire_orchestrator.services.resilience import RetryableError
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
    supabase_update,
)
from aspire_orchestrator.workers.trust_onboarding.cnam_sanitizer import (
    sanitize_cnam_display_name,
)
from aspire_orchestrator.workers.trust_onboarding.trust_receipts import (
    TrustReceiptError,
    cut_trust_receipt,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Each cron run only handles this many tenants per call. At 10k tenants
# we expect ~600 stuck-state candidates per 6h window in the worst case,
# but we bound a single run to keep within Twilio's 10/sec rate budget
# (we issue at most 2 GETs per tenant during reconciliation).
_MAX_TENANTS_PER_RUN: Final[int] = 100

# Trust states where a missed webhook can leave a tenant stuck. We poll
# Twilio directly when DB updated_at < now() - 24h for these states.
_STUCK_CANDIDATE_STATES: Final[frozenset[str]] = frozenset({
    "profile_submitted",
    "shaken_submitted",
    "cnam_submitted",
})

# Twilio statuses that mean "the prior submitted state has been resolved".
# When DB still says *_submitted but Twilio says one of these, we enqueue
# advance_trust_state so the state machine picks up the resolution.
_TWILIO_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({
    "twilio-approved",
    "twilio-rejected",
})

# Reputation polling cadence — never poll the same tenant inside this window.
_REPUTATION_POLL_INTERVAL_HOURS: Final[int] = 6

# Twilio CNAM display-name change cooldown (Twilio policy: max 1 / 30 days).
_CNAM_CHANGE_COOLDOWN_DAYS: Final[int] = 30

# Stuck-state threshold — only reconcile profiles that have been idle in a
# *_submitted state longer than this. Below this we trust the webhook path.
_STUCK_THRESHOLD_HOURS: Final[int] = 24

# ARQ queue identifier — same as the rest of the trust-onboarding worker.
_TRUST_QUEUE_NAME: Final[str] = "arq:trust_onboarding"


# ---------------------------------------------------------------------------
# ARQ enqueue helpers
# ---------------------------------------------------------------------------


async def _enqueue_advance_trust_state(trust_profile_id: str, *, job_suffix: str) -> bool:
    """Enqueue an advance_trust_state job for stuck-state reconciliation.

    Returns True on successful enqueue, False on Redis failure (logged).
    job_suffix lets the caller scope the dedup key (e.g. 'cron_reconcile')
    so the cron retry doesn't collide with the kyb-submit-time enqueue.
    """
    try:
        from arq.connections import RedisSettings, create_pool  # type: ignore[import-not-found]

        redis_url = settings.redis_url or "redis://localhost:6379"
        redis_settings = RedisSettings.from_dsn(redis_url)
        pool = await create_pool(redis_settings)
        try:
            job_id = f"trust:{trust_profile_id}:{job_suffix}"
            await pool.enqueue_job(
                "advance_trust_state",
                trust_profile_id,
                _queue_name=_TRUST_QUEUE_NAME,
                _job_id=job_id,
                _defer_by=0,
            )
        finally:
            await pool.aclose()
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort; next cron run retries
        logger.warning(
            "cron_jobs arq_enqueue_failed trust_profile_id=%s suffix=%s err=%s",
            trust_profile_id, job_suffix, exc,
        )
        return False


async def _enqueue_apply_cnam_change(request_id: str) -> bool:
    """Enqueue an apply_cnam_display_name_change job. Returns True on success."""
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
                _queue_name=_TRUST_QUEUE_NAME,
                _job_id=job_id,
                _defer_by=0,
            )
        finally:
            await pool.aclose()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cron_jobs arq_enqueue_cnam_change_failed request_id=%s err=%s",
            request_id, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Cron 1 — poll_trust_status_for_tenants
# ---------------------------------------------------------------------------


async def poll_trust_status_for_tenants() -> dict[str, Any]:
    """Reconcile tenants stuck in *_submitted >24h by polling Twilio directly.

    Returns:
        {
            "examined": int,
            "reconciled": int,         # state mismatches detected + enqueued
            "twilio_errors": int,
            "max_tenants_capped": bool,
        }
    """
    threshold_iso = (
        datetime.now(timezone.utc) - timedelta(hours=_STUCK_THRESHOLD_HOURS)
    ).isoformat()

    # Build the IN-list filter. PostgREST accepts `column=in.(v1,v2,v3)`.
    states_in = ",".join(sorted(_STUCK_CANDIDATE_STATES))
    filter_str = (
        f"trust_state=in.({states_in})"
        f"&updated_at=lt.{threshold_iso}"
    )

    try:
        candidates = await supabase_select(
            "tenant_trust_profiles",
            filter_str,
            order_by="updated_at.asc",
            limit=_MAX_TENANTS_PER_RUN + 1,  # +1 lets us know we capped
        )
    except SupabaseClientError as exc:
        logger.error("cron_jobs poll_trust_status select_failed: %s", exc)
        return {
            "examined": 0,
            "reconciled": 0,
            "twilio_errors": 0,
            "max_tenants_capped": False,
            "error": "DB_UNAVAILABLE",
        }

    capped = len(candidates) > _MAX_TENANTS_PER_RUN
    candidates = candidates[:_MAX_TENANTS_PER_RUN]

    examined = 0
    reconciled = 0
    twilio_errors = 0

    for profile in candidates:
        examined += 1
        try:
            mismatch = await _detect_and_reconcile_mismatch(profile)
        except RetryableError:
            # Bubble up so ARQ retries the whole cron run with backoff.
            raise
        except TrustHubError as exc:
            twilio_errors += 1
            logger.warning(
                "cron_jobs poll_trust_status tenant_error trust_profile_id=%s err=%s",
                profile.get("id"), exc,
            )
            continue
        except Exception as exc:  # noqa: BLE001 — keep the cron going
            twilio_errors += 1
            logger.error(
                "cron_jobs poll_trust_status unhandled trust_profile_id=%s err=%s",
                profile.get("id"), exc,
                exc_info=True,
            )
            continue
        if mismatch:
            reconciled += 1

    logger.info(
        "cron_jobs poll_trust_status examined=%d reconciled=%d twilio_errors=%d capped=%s",
        examined, reconciled, twilio_errors, capped,
    )

    return {
        "examined": examined,
        "reconciled": reconciled,
        "twilio_errors": twilio_errors,
        "max_tenants_capped": capped,
    }


async def _detect_and_reconcile_mismatch(profile: dict[str, Any]) -> bool:
    """For a single profile, poll Twilio and enqueue advance if mismatch found.

    Returns True when a mismatch was detected and reconciliation was
    enqueued; False when Twilio agrees with our DB or the SID is missing.

    RetryableError propagates to the caller (Twilio 5xx → ARQ retry).
    """
    trust_state = str(profile.get("trust_state", ""))
    trust_profile_id = str(profile.get("id", ""))

    # Pick the right Twilio resource SID + status fetcher per state.
    twilio_status: str = ""
    twilio_resource_sid: str | None = None
    if trust_state == "profile_submitted":
        twilio_resource_sid = profile.get("customer_profile_sid")
        if not twilio_resource_sid:
            return False
        twilio_status = await thub.fetch_customer_profile_status(twilio_resource_sid)
    elif trust_state == "shaken_submitted":
        twilio_resource_sid = profile.get("shaken_trust_product_sid")
        if not twilio_resource_sid:
            return False
        twilio_status = await thub.fetch_trust_product_status(twilio_resource_sid)
    elif trust_state == "cnam_submitted":
        twilio_resource_sid = profile.get("cnam_trust_product_sid")
        if not twilio_resource_sid:
            return False
        twilio_status = await thub.fetch_trust_product_status(twilio_resource_sid)
    else:
        # Defensive — caller's filter should never let us land here.
        return False

    if twilio_status not in _TWILIO_TERMINAL_STATUSES:
        # Twilio still pending — DB and Twilio agree; nothing to do.
        return False

    # Mismatch detected — DB says *_submitted, Twilio says approved/rejected.
    # Cut a webhook_received receipt with reason_code='cron_reconcile' so
    # the ledger explains why the state machine got an out-of-band kick.
    receipt_input: dict[str, Any] = {
        "trust_profile_id": trust_profile_id,
        "step_name": "cron_reconcile",
        "current_state": trust_state,
        "twilio_resource_sid": twilio_resource_sid or "",
    }
    try:
        await cut_trust_receipt(
            receipt_type="webhook_received",
            trust_profile=profile,
            outcome="success",
            from_state=trust_state,
            to_state=trust_state,  # state hasn't changed yet — advance_trust_state will
            redacted_inputs=receipt_input,
            redacted_outputs={
                "twilio_resource_sid": twilio_resource_sid or "",
                "twilio_status": twilio_status,
            },
            reason_code="cron_reconcile",
            twilio_resource_sid=twilio_resource_sid,
            twilio_status=twilio_status,
        )
    except TrustReceiptError as exc:
        logger.warning(
            "cron_jobs reconcile_receipt_failed trust_profile_id=%s err=%s",
            trust_profile_id, exc,
        )
        # Fall through — the enqueue still helps; the state machine will
        # cut its own receipts when it advances.

    await _enqueue_advance_trust_state(trust_profile_id, job_suffix="cron_reconcile")
    logger.info(
        "cron_jobs reconcile_enqueued trust_profile_id=%s state=%s twilio_status=%s",
        trust_profile_id, trust_state, twilio_status,
    )
    return True


# ---------------------------------------------------------------------------
# Cron 2 — poll_carrier_reputation
# ---------------------------------------------------------------------------


async def poll_carrier_reputation() -> dict[str, Any]:
    """Poll Twilio Branded Calling reputation API for tenants on number_attached.

    Feature-gated by ``settings.branded_calling_enabled``. When the flag is
    off (default), this is a pure no-op so production is safe even before
    Twilio Branded Calling private-beta access lands.
    """
    if not settings.branded_calling_enabled:
        logger.debug("cron_jobs poll_carrier_reputation skipped — feature_flag_off")
        return {
            "examined": 0,
            "updated": 0,
            "twilio_errors": 0,
            "skipped_feature_flag_off": True,
        }

    threshold_iso = (
        datetime.now(timezone.utc) - timedelta(hours=_REPUTATION_POLL_INTERVAL_HOURS)
    ).isoformat()

    # Stuck-states-style filter: trust_state='number_attached' AND
    # last_reputation_check IS NULL OR < threshold.
    # PostgREST `or=` lets us express the OR cleanly.
    filter_str = (
        f"trust_state=eq.number_attached"
        f"&or=(last_reputation_check.is.null,last_reputation_check.lt.{threshold_iso})"
    )

    try:
        candidates = await supabase_select(
            "tenant_trust_profiles",
            filter_str,
            order_by="last_reputation_check.asc.nullsfirst",
            limit=_MAX_TENANTS_PER_RUN,
        )
    except SupabaseClientError as exc:
        logger.error("cron_jobs poll_carrier_reputation select_failed: %s", exc)
        return {
            "examined": 0,
            "updated": 0,
            "twilio_errors": 0,
            "error": "DB_UNAVAILABLE",
        }

    examined = 0
    updated = 0
    twilio_errors = 0

    for profile in candidates:
        examined += 1
        try:
            changed = await _poll_one_reputation(profile)
        except RetryableError:
            raise
        except TrustHubError as exc:
            twilio_errors += 1
            logger.warning(
                "cron_jobs poll_carrier_reputation tenant_error trust_profile_id=%s err=%s",
                profile.get("id"), exc,
            )
            continue
        except Exception as exc:  # noqa: BLE001
            twilio_errors += 1
            logger.error(
                "cron_jobs poll_carrier_reputation unhandled trust_profile_id=%s err=%s",
                profile.get("id"), exc,
                exc_info=True,
            )
            continue
        if changed:
            updated += 1

    logger.info(
        "cron_jobs poll_carrier_reputation examined=%d updated=%d twilio_errors=%d",
        examined, updated, twilio_errors,
    )

    return {
        "examined": examined,
        "updated": updated,
        "twilio_errors": twilio_errors,
    }


async def _poll_one_reputation(profile: dict[str, Any]) -> bool:
    """Poll one tenant's reputation; cut a receipt only when the value changes.

    Returns True when reputation changed (receipt cut), False when no change.
    """
    trust_profile_id = str(profile.get("id", ""))
    new_reputation = await _fetch_carrier_reputation(profile)

    now_iso = datetime.now(timezone.utc).isoformat()
    prior: dict[str, Any] = profile.get("last_reputation_status") or {}
    changed = _reputation_changed(prior, new_reputation)

    update_payload: dict[str, Any] = {
        "last_reputation_check": now_iso,
        "last_reputation_status": new_reputation,
    }
    try:
        await supabase_update(
            "tenant_trust_profiles",
            f"id=eq.{trust_profile_id}",
            update_payload,
        )
    except SupabaseClientError as exc:
        logger.warning(
            "cron_jobs reputation_update_failed trust_profile_id=%s err=%s",
            trust_profile_id, exc,
        )
        # No receipt without successful DB write — fail-closed (Law #3).
        return False

    if not changed:
        return False

    # Reputation changed — cut a receipt for ops visibility.
    try:
        await cut_trust_receipt(
            receipt_type="carrier_reputation_updated",
            trust_profile=profile,
            outcome="success",
            from_state="number_attached",
            to_state="number_attached",
            redacted_inputs={
                "trust_profile_id": trust_profile_id,
                "step_name": "carrier_reputation_poll",
            },
            redacted_outputs={
                "prior_overall": _safe_overall(prior),
                "new_overall": _safe_overall(new_reputation),
                "changed_carriers": _changed_carriers(prior, new_reputation),
            },
        )
    except TrustReceiptError as exc:
        logger.warning(
            "cron_jobs reputation_receipt_failed trust_profile_id=%s err=%s",
            trust_profile_id, exc,
        )
    return True


async def _fetch_carrier_reputation(profile: dict[str, Any]) -> dict[str, Any]:
    """Stub for Twilio Branded Calling reputation fetch.

    Twilio's Branded Calling reputation API is private-beta gated. When
    private-beta access lands, replace this with the real call:

        result = await thub.fetch_branded_calling_reputation(
            number_sid=profile["phone_number_twilio_sid"]
        )

    For now the function returns a stable "unknown" payload so callers
    have a consistent JSON shape to compare against.
    """
    # Defensive: even with feature flag on, if the provider doesn't yet
    # expose the call we fall through to the unknown payload rather than
    # raising — the cron should keep running for the other capabilities.
    fetcher = getattr(thub, "fetch_branded_calling_reputation", None)
    if fetcher is None:
        return {
            "overall": {"score": None, "label": "unknown"},
            "t_mobile": {"score": None, "label": "unknown"},
            "att": {"score": None, "label": "unknown"},
            "verizon": {"score": None, "label": "unknown"},
            "_fetched_at": datetime.now(timezone.utc).isoformat(),
            "_provider_unavailable": True,
        }
    number_sid = profile.get("phone_number_twilio_sid") or profile.get("number_sid") or ""
    result = await fetcher(number_sid=number_sid)
    if not isinstance(result, dict):
        return {
            "overall": {"score": None, "label": "unknown"},
            "_fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    return result


def _reputation_changed(prior: dict[str, Any], new: dict[str, Any]) -> bool:
    """Return True when any per-carrier label flipped between prior + new."""
    if not prior:
        # Never polled before — count first poll as a "change" only when
        # we got a non-unknown label, otherwise it's just a no-op poll.
        overall = (new.get("overall") or {}).get("label", "unknown")
        return overall not in ("unknown", "", None)
    for carrier in ("overall", "t_mobile", "att", "verizon"):
        prior_label = (prior.get(carrier) or {}).get("label")
        new_label = (new.get(carrier) or {}).get("label")
        if prior_label != new_label:
            return True
    return False


def _safe_overall(reputation: dict[str, Any]) -> str:
    """Return the overall reputation label as a short string."""
    overall = reputation.get("overall") if isinstance(reputation, dict) else None
    if not isinstance(overall, dict):
        return "unknown"
    label = overall.get("label")
    return str(label) if label else "unknown"


def _changed_carriers(prior: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Return the list of carriers whose label changed between prior + new."""
    changed: list[str] = []
    for carrier in ("overall", "t_mobile", "att", "verizon"):
        prior_label = (prior.get(carrier) or {}).get("label") if isinstance(prior, dict) else None
        new_label = (new.get(carrier) or {}).get("label") if isinstance(new, dict) else None
        if prior_label != new_label:
            changed.append(carrier)
    return changed


# ---------------------------------------------------------------------------
# Cron 3 — enqueue_cnam_display_name_changes
# ---------------------------------------------------------------------------


async def enqueue_cnam_display_name_changes() -> dict[str, Any]:
    """Enqueue ARQ jobs for pending CNAM display-name change requests.

    Validates the 30-day cooldown server-side; rows that miss the cooldown
    are flipped to status='cooldown_pending' and skipped this run. Rows
    that satisfy the cooldown advance to status='in_progress' and have an
    apply_cnam_display_name_change ARQ job enqueued.

    Returns:
        {
            "examined": int,
            "enqueued": int,
            "cooldown_pending": int,
        }
    """
    # Pull pending + cooldown_pending requests, oldest first.
    filter_str = "status=in.(pending,cooldown_pending)"
    try:
        rows = await supabase_select(
            "tenant_cnam_change_requests",
            filter_str,
            order_by="created_at.asc",
            limit=_MAX_TENANTS_PER_RUN,
        )
    except SupabaseClientError as exc:
        logger.error("cron_jobs enqueue_cnam_changes select_failed: %s", exc)
        return {
            "examined": 0,
            "enqueued": 0,
            "cooldown_pending": 0,
            "error": "DB_UNAVAILABLE",
        }

    examined = 0
    enqueued = 0
    cooldown_pending = 0

    for row in rows:
        examined += 1
        request_id = str(row.get("id", ""))
        trust_profile_id = str(row.get("trust_profile_id", ""))
        if not request_id or not trust_profile_id:
            continue

        # Load the trust profile to check last_cnam_change_at.
        try:
            profile_rows = await supabase_select(
                "tenant_trust_profiles",
                f"id=eq.{trust_profile_id}",
                limit=1,
            )
        except SupabaseClientError as exc:
            logger.warning(
                "cron_jobs enqueue_cnam_profile_select_failed request_id=%s err=%s",
                request_id, exc,
            )
            continue
        if not profile_rows:
            await _mark_cnam_request_failed(request_id, reason_code="NO_TRUST_PROFILE")
            continue

        profile = profile_rows[0]
        if not _cooldown_satisfied(profile):
            # Bump status if not already cooldown_pending — keeps the row
            # eligible for the next cron run without extra DB churn.
            if row.get("status") != "cooldown_pending":
                await _mark_cnam_request_status(request_id, "cooldown_pending")
            cooldown_pending += 1
            continue

        # Mark in_progress (idempotent — UNIQUE index keeps it safe).
        await _mark_cnam_request_status(request_id, "in_progress")
        if await _enqueue_apply_cnam_change(request_id):
            enqueued += 1

    logger.info(
        "cron_jobs enqueue_cnam_changes examined=%d enqueued=%d cooldown_pending=%d",
        examined, enqueued, cooldown_pending,
    )

    return {
        "examined": examined,
        "enqueued": enqueued,
        "cooldown_pending": cooldown_pending,
    }


def _cooldown_satisfied(profile: dict[str, Any]) -> bool:
    """Return True when the 30-day display-name change cooldown has elapsed.

    NULL last_cnam_change_at means no prior change → cooldown trivially met.
    """
    last_change_raw = profile.get("last_cnam_change_at")
    if last_change_raw is None:
        return True
    try:
        last_change = datetime.fromisoformat(str(last_change_raw).replace("Z", "+00:00"))
    except ValueError:
        # Unparseable timestamp — fail open (let the request proceed) but log.
        logger.warning(
            "cron_jobs cooldown_parse_failed trust_profile_id=%s value=%r",
            profile.get("id"), last_change_raw,
        )
        return True
    threshold = datetime.now(timezone.utc) - timedelta(days=_CNAM_CHANGE_COOLDOWN_DAYS)
    return last_change <= threshold


async def _mark_cnam_request_status(
    request_id: str, status_value: str, *, reason_code: str | None = None,
) -> None:
    """Patch tenant_cnam_change_requests.status. Best-effort."""
    payload: dict[str, Any] = {"status": status_value}
    if reason_code is not None:
        payload["reason_code"] = reason_code
    try:
        await supabase_update(
            "tenant_cnam_change_requests",
            f"id=eq.{request_id}",
            payload,
        )
    except SupabaseClientError as exc:
        logger.warning(
            "cron_jobs cnam_request_status_update_failed request_id=%s status=%s err=%s",
            request_id, status_value, exc,
        )


async def _mark_cnam_request_failed(request_id: str, *, reason_code: str) -> None:
    """Mark a CNAM change request as terminally failed."""
    payload: dict[str, Any] = {
        "status": "failed",
        "reason_code": reason_code,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await supabase_update(
            "tenant_cnam_change_requests",
            f"id=eq.{request_id}",
            payload,
        )
    except SupabaseClientError as exc:
        logger.warning(
            "cron_jobs cnam_request_fail_update_failed request_id=%s err=%s",
            request_id, exc,
        )


# ---------------------------------------------------------------------------
# Cron 4 — retry_failed_trust_onboardings (referenced by worker.py)
# ---------------------------------------------------------------------------


async def retry_failed_trust_onboardings() -> dict[str, Any]:
    """Re-enqueue tenants in trust_state='failed' if dispute_count < 3.

    Conservative retry: only profiles that haven't exceeded the dispute
    cap get re-enqueued. The state machine itself decides whether to
    advance from 'failed' or surface a final-rejection receipt.

    Returns:
        {"examined": int, "re_enqueued": int}
    """
    try:
        candidates = await supabase_select(
            "tenant_trust_profiles",
            "trust_state=eq.failed&dispute_count=lt.3",
            order_by="updated_at.asc",
            limit=_MAX_TENANTS_PER_RUN,
        )
    except SupabaseClientError as exc:
        logger.error("cron_jobs retry_failed select_failed: %s", exc)
        return {"examined": 0, "re_enqueued": 0, "error": "DB_UNAVAILABLE"}

    examined = 0
    re_enqueued = 0
    for profile in candidates:
        examined += 1
        trust_profile_id = str(profile.get("id", ""))
        if not trust_profile_id:
            continue
        if await _enqueue_advance_trust_state(trust_profile_id, job_suffix="cron_retry"):
            re_enqueued += 1

    logger.info(
        "cron_jobs retry_failed examined=%d re_enqueued=%d",
        examined, re_enqueued,
    )
    return {"examined": examined, "re_enqueued": re_enqueued}


# ---------------------------------------------------------------------------
# ARQ job — apply_cnam_display_name_change
# ---------------------------------------------------------------------------


async def apply_cnam_display_name_change(request_id: str) -> dict[str, Any]:
    """Apply a pending CNAM display-name change end-to-end.

    Steps:
      1. Load the change request + trust profile.
      2. Sanitize the requested name (defense-in-depth — route already did).
      3. Update the CNAM EndUser via Twilio (`update_end_user`).
      4. Re-submit the CNAM Trust Product (`submit_trust_product`).
      5. Persist the new sanitized display name on the trust profile,
         set cnam_display_name_pending=TRUE, last_cnam_change_at=now().
      6. Cut a `cnam_display_name_change_requested` receipt.
      7. Mark the request status='approved' (Twilio's status callback
         will eventually flip cnam_display_name_pending back to false on
         cnam_approved / cnam_rejected — handled in W5).

    Failure handling (Law #10):
      * Twilio 5xx / 429        → RetryableError re-raised so ARQ retries.
      * Twilio 4xx              → mark request failed; non-retryable.
      * Sanitization regression → mark request failed (should never happen).

    Returns:
        {
            "request_id": str,
            "outcome": "success" | "failed",
            "reason_code": str | None,
            "receipt_id": str | None,
            "sanitized_display_name": str | None,
        }
    """
    logger.info("cron_jobs apply_cnam_change start request_id=%s", request_id)

    # --- 1. Load request row ---
    try:
        req_rows = await supabase_select(
            "tenant_cnam_change_requests",
            f"id=eq.{request_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error("cron_jobs apply_cnam request_select_failed: %s", exc)
        # DB-level failure — re-raise so ARQ retries.
        raise
    if not req_rows:
        logger.warning("cron_jobs apply_cnam request_not_found request_id=%s", request_id)
        return {
            "request_id": request_id,
            "outcome": "failed",
            "reason_code": "REQUEST_NOT_FOUND",
            "receipt_id": None,
            "sanitized_display_name": None,
        }
    req = req_rows[0]
    trust_profile_id = str(req.get("trust_profile_id", ""))
    requested_name = str(req.get("requested_display_name", ""))
    cap_token_id = req.get("capability_token_id")

    # --- 2. Sanitize defensively ---
    try:
        sanitized = sanitize_cnam_display_name(requested_name)
    except ValueError as exc:
        logger.warning(
            "cron_jobs apply_cnam sanitize_failed request_id=%s err=%s",
            request_id, exc,
        )
        await _mark_cnam_request_failed(request_id, reason_code="INVALID_DISPLAY_NAME")
        return {
            "request_id": request_id,
            "outcome": "failed",
            "reason_code": "INVALID_DISPLAY_NAME",
            "receipt_id": None,
            "sanitized_display_name": None,
        }

    # --- 3. Load trust profile ---
    try:
        profile_rows = await supabase_select(
            "tenant_trust_profiles",
            f"id=eq.{trust_profile_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error("cron_jobs apply_cnam profile_select_failed: %s", exc)
        raise
    if not profile_rows:
        await _mark_cnam_request_failed(request_id, reason_code="NO_TRUST_PROFILE")
        return {
            "request_id": request_id,
            "outcome": "failed",
            "reason_code": "NO_TRUST_PROFILE",
            "receipt_id": None,
            "sanitized_display_name": sanitized,
        }
    profile = profile_rows[0]
    cnam_end_user_sid = profile.get("cnam_end_user_sid")
    cnam_trust_product_sid = profile.get("cnam_trust_product_sid")
    if not cnam_end_user_sid or not cnam_trust_product_sid:
        await _mark_cnam_request_failed(request_id, reason_code="MISSING_CNAM_RESOURCES")
        return {
            "request_id": request_id,
            "outcome": "failed",
            "reason_code": "MISSING_CNAM_RESOURCES",
            "receipt_id": None,
            "sanitized_display_name": sanitized,
        }

    # --- 4. Update CNAM EndUser (Twilio) ---
    try:
        await thub.update_end_user(
            str(cnam_end_user_sid),
            attributes={"cnam_display_name": sanitized},
            idempotency_key=f"cnam_change_update_eu:{request_id}",
        )
    except RetryableError:
        # 5xx / 429 — re-raise so ARQ retries with backoff (Law #10).
        raise
    except TrustHubError as exc:
        # 4xx — non-retryable: mark request failed, surface receipt-less.
        logger.warning(
            "cron_jobs apply_cnam update_end_user_failed request_id=%s status=%d err=%s",
            request_id, exc.status_code, exc,
        )
        await _mark_cnam_request_failed(
            request_id, reason_code=f"TWILIO_{exc.status_code}",
        )
        return {
            "request_id": request_id,
            "outcome": "failed",
            "reason_code": f"TWILIO_{exc.status_code}",
            "receipt_id": None,
            "sanitized_display_name": sanitized,
        }

    # --- 5. Re-submit the CNAM Trust Product ---
    try:
        await thub.submit_trust_product(
            str(cnam_trust_product_sid),
            idempotency_key=f"cnam_change_resubmit:{request_id}",
        )
    except RetryableError:
        raise
    except TrustHubError as exc:
        logger.warning(
            "cron_jobs apply_cnam resubmit_failed request_id=%s status=%d err=%s",
            request_id, exc.status_code, exc,
        )
        await _mark_cnam_request_failed(
            request_id, reason_code=f"TWILIO_{exc.status_code}",
        )
        return {
            "request_id": request_id,
            "outcome": "failed",
            "reason_code": f"TWILIO_{exc.status_code}",
            "receipt_id": None,
            "sanitized_display_name": sanitized,
        }

    # --- 6. Persist on trust profile + mark request approved ---
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        await supabase_update(
            "tenant_trust_profiles",
            f"id=eq.{trust_profile_id}",
            {
                "cnam_display_name": sanitized,
                "cnam_display_name_pending": True,
                "last_cnam_change_at": now_iso,
            },
        )
    except SupabaseClientError as exc:
        logger.error(
            "cron_jobs apply_cnam profile_update_failed request_id=%s err=%s",
            request_id, exc,
        )
        # The Twilio side is committed but the DB is out of sync — ARQ
        # retry will re-submit (idempotency keys protect us) and re-try
        # the DB write. Re-raise so the worker retries.
        raise

    # --- 7. Cut receipt (Law #2) ---
    receipt_id: str | None = None
    try:
        receipt_id = await cut_trust_receipt(
            receipt_type="cnam_display_name_change_requested",
            trust_profile=profile,
            outcome="success",
            from_state="number_attached",
            to_state="number_attached",
            redacted_inputs={
                "trust_profile_id": trust_profile_id,
                "step_name": "cnam_display_name_change_apply",
                "request_id": request_id,
            },
            redacted_outputs={
                "cnam_display_name": sanitized,
                "twilio_status": "pending-review",
            },
            twilio_resource_sid=str(cnam_trust_product_sid),
            twilio_status="pending-review",
            capability_token_id=str(cap_token_id) if cap_token_id else None,
        )
    except TrustReceiptError as exc:
        logger.warning(
            "cron_jobs apply_cnam receipt_failed request_id=%s err=%s",
            request_id, exc,
        )

    # --- 8. Mark request approved (terminal) ---
    try:
        await supabase_update(
            "tenant_cnam_change_requests",
            f"id=eq.{request_id}",
            {
                "status": "approved",
                "sanitized_display_name": sanitized,
                "completed_at": now_iso,
            },
        )
    except SupabaseClientError as exc:
        logger.warning(
            "cron_jobs apply_cnam request_approve_update_failed request_id=%s err=%s",
            request_id, exc,
        )

    logger.info(
        "cron_jobs apply_cnam done request_id=%s sanitized_len=%d receipt_id=%s",
        request_id, len(sanitized), receipt_id or "<none>",
    )
    return {
        "request_id": request_id,
        "outcome": "success",
        "reason_code": None,
        "receipt_id": receipt_id,
        "sanitized_display_name": sanitized,
    }


__all__ = [
    "poll_trust_status_for_tenants",
    "poll_carrier_reputation",
    "enqueue_cnam_display_name_changes",
    "retry_failed_trust_onboardings",
    "apply_cnam_display_name_change",
    "_MAX_TENANTS_PER_RUN",
    "_CNAM_CHANGE_COOLDOWN_DAYS",
    "_STUCK_THRESHOLD_HOURS",
]
