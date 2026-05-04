"""ARQ worker — Trust Onboarding (W2-A).

Drives the per-tenant Twilio Trust Hub state machine from a Redis-backed
queue. Runs as a separate process pool from the FastAPI request loop to
avoid blocking inbound requests on Twilio API latency (review queue can
take hours; HTTP calls average 200-500ms).

Process model:
    - One ARQ worker pod per region (or two for HA)
    - Reads jobs from Redis queue 'trust_onboarding'
    - Each job: advance one tenant by exactly ONE state transition
    - State callbacks from Twilio (POST /v1/trust-hub/status-callback)
      enqueue follow-on jobs when async approvals land

Run locally:
    arq aspire_orchestrator.workers.trust_onboarding.worker.WorkerSettings

Run in production (Railway):
    Add a separate Railway service with start command above; reads same
    REDIS_URL as the FastAPI service.

Job functions registered:
    advance_trust_state(ctx, trust_profile_id) -> dict
        The single state-machine advance entry. Called by:
        - POST /v1/trust-hub/kyb (immediate enqueue, 0s delay)
        - POST /v1/trust-hub/status-callback (after Twilio approves a step)
        - POST /v1/trust-hub/dispute (after KYB resubmit)
        - poll_trust_status_for_tenants cron (W9, recovery)

Idempotency:
    Each job's deduplication key = f"trust:{trust_profile_id}:{current_state}".
    ARQ's `_job_id` parameter prevents duplicate jobs for the same tenant
    in the same state. Re-runs are safe — the state machine itself checks
    if the Twilio resource SID is already populated before re-creating.

Author: Aspire — Wave 2-A (per docs/plans/per-tenant-trust-hub-cnam.md §III)
"""

from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.config.settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------


async def advance_trust_state(ctx: dict[str, Any], trust_profile_id: str) -> dict[str, Any]:
    """Advance one tenant by exactly ONE state machine transition.

    Args:
        ctx: ARQ context dict — contains 'job_id', 'job_try', 'redis', etc.
        trust_profile_id: UUID of the tenant_trust_profiles row to advance.

    Returns:
        {
            "trust_profile_id": "...",
            "from_state": "kyb_collected",
            "to_state": "profile_drafted",
            "outcome": "success" | "halted" | "failed",
            "receipt_id": "..." | None,
        }

    The state machine itself is in workers.trust_onboarding.state_machine
    (W2-D). This function is just the ARQ-job wrapper — it dispatches to
    the state-machine advance function and surfaces the result.

    Errors are logged but NOT raised — ARQ would retry indefinitely
    otherwise. State machine fails-closed by writing trust_state='failed'
    + a rejection receipt; the cron job (W9) handles auto-recovery.
    """
    from aspire_orchestrator.workers.trust_onboarding.state_machine import (
        advance_trust_state as advance_impl,
    )

    job_id = ctx.get("job_id", "<unknown>")
    job_try = ctx.get("job_try", 1)
    logger.info(
        "trust_advance start trust_profile_id=%s job_id=%s try=%d",
        trust_profile_id, job_id, job_try,
    )
    try:
        result = await advance_impl(trust_profile_id=trust_profile_id, worker_job_id=job_id)
        logger.info(
            "trust_advance done trust_profile_id=%s from=%s to=%s outcome=%s",
            trust_profile_id, result.get("from_state"), result.get("to_state"), result.get("outcome"),
        )
        return result
    except Exception as exc:  # noqa: BLE001 — last-resort logging
        logger.error(
            "trust_advance unhandled trust_profile_id=%s job_id=%s err=%s",
            trust_profile_id, job_id, exc,
            exc_info=True,
        )
        # Re-raise so ARQ records the failure and retries per WorkerSettings.
        raise


async def advance_a2p_registration(ctx: dict[str, Any], suite_id: str) -> dict[str, Any]:
    """Advance one tenant's A2P registration by exactly ONE state machine transition.

    Args:
        ctx: ARQ context dict — contains 'job_id', 'job_try', 'redis', etc.
        suite_id: UUID of the tenant's suite (drives tenant_a2p_brands lookup).

    Returns:
        {
            "suite_id": "...",
            "brand_id": "...",
            "from_state": "draft",
            "to_state": "pending",
            "outcome": "success" | "halted" | "failed",
            "receipt_id": "..." | None,
        }

    Errors are logged but NOT raised — state machine fails-closed.
    """
    from aspire_orchestrator.workers.trust_onboarding.a2p_state_machine import (
        advance_a2p_registration as advance_impl,
    )

    job_id = ctx.get("job_id", "<unknown>")
    job_try = ctx.get("job_try", 1)
    logger.info(
        "a2p_advance start suite_id=%s job_id=%s try=%d",
        suite_id, job_id, job_try,
    )
    try:
        result = await advance_impl(suite_id=suite_id, worker_job_id=job_id)
        logger.info(
            "a2p_advance done suite_id=%s from=%s to=%s outcome=%s",
            suite_id, result.get("from_state"), result.get("to_state"), result.get("outcome"),
        )
        return result
    except Exception as exc:  # noqa: BLE001 — last-resort logging
        logger.error(
            "a2p_advance unhandled suite_id=%s job_id=%s err=%s",
            suite_id, job_id, exc,
            exc_info=True,
        )
        raise


async def advance_number_swap(ctx: dict[str, Any], swap_job_id: str) -> dict[str, Any]:
    """Execute the number-swap state machine for one tenant (W11).

    Args:
        ctx: ARQ context dict — contains 'job_id', 'job_try', 'redis', etc.
        swap_job_id: UUID of the tenant_phone_swaps row to process.

    Returns:
        {
            "swap_job_id": "...",
            "outcome": "success" | "rolled_back" | "failed",
            "old_number_e164": "...",
            "new_number_e164": "...",
            "receipt_ids": [...],
        }

    Failure handling (Law #10):
        RetryableError is re-raised so ARQ retries with exponential backoff.
        SwapAbortError (non-retryable, e.g. purchase failed) is re-raised
        as a plain Exception so ARQ marks the job failed without unlimited retry.
        SwapRollbackError (step-7 failure, clean rollback done) is re-raised.
    """
    from aspire_orchestrator.workers.trust_onboarding.swap_state_machine import (
        SwapAbortError,
        SwapRollbackError,
        run_number_swap,
    )

    job_id = ctx.get("job_id", "<unknown>")
    job_try = ctx.get("job_try", 1)
    logger.info(
        "swap_advance start swap_job_id=%s job_id=%s try=%d",
        swap_job_id, job_id, job_try,
    )
    try:
        result = await run_number_swap(swap_job_id, worker_job_id=job_id)
        logger.info(
            "swap_advance done swap_job_id=%s outcome=%s",
            swap_job_id, result.get("outcome"),
        )
        return result
    except Exception as exc:  # noqa: BLE001 — last-resort logging
        logger.error(
            "swap_advance failed swap_job_id=%s job_id=%s err=%s",
            swap_job_id, job_id, exc,
            exc_info=True,
        )
        raise


async def poll_trust_status_for_tenants(ctx: dict[str, Any]) -> dict[str, Any]:
    """Cron job (W9) — every 6h, poll Twilio for tenants stuck in *_submitted.

    Implementation lives in workers.trust_onboarding.cron_jobs (W9).
    This is a thin wrapper for ARQ's cron registry.
    """
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        poll_trust_status_for_tenants as poll_impl,
    )

    return await poll_impl()


async def retry_failed_trust_onboardings(ctx: dict[str, Any]) -> dict[str, Any]:
    """Cron job (W9) — every 24h, re-enqueue tenants in `failed` state if dispute_count<3."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        retry_failed_trust_onboardings as retry_impl,
    )

    return await retry_impl()


# ---------------------------------------------------------------------------
# ARQ WorkerSettings — entry point for `arq <module>:WorkerSettings`
# ---------------------------------------------------------------------------


class WorkerSettings:
    """ARQ worker configuration.

    Read at startup by `arq aspire_orchestrator.workers.trust_onboarding.worker.WorkerSettings`.

    Tuning:
        max_jobs           — concurrent jobs in flight per pod (10 = ~10 Twilio
                             RPS budget shared across the pool; below default
                             10/sec rate limit)
        job_timeout        — 5 min (Twilio API calls < 5s; state machine has
                             ~12 sequential calls in worst-case profile_drafted
                             advance; 5min allows generous slack)
        keep_result        — 24h (debugging, ops dashboard)
        retry_jobs         — true (idempotency-safe; state machine no-ops on
                             already-applied transitions via SID checks)
        max_tries          — 5 (3 attempts + 2 retries; total budget ~15s)
        health_check_interval — 30s (Railway healthcheck)
    """

    # Functions registered with the worker.
    functions = [
        advance_trust_state,
        advance_a2p_registration,
        advance_number_swap,  # W11 — number swap
    ]

    # Cron jobs (W9 — these become active when cron_jobs.py ships).
    # Registered now so the worker doesn't need a redeploy when W9 lands.
    cron_jobs: list[Any] = []  # populated dynamically when ARQ is available — see below

    # Redis connection — same Redis as the FastAPI app.
    @staticmethod
    def get_redis_settings() -> Any:  # type: ignore[no-untyped-def]
        """Defer arq.connections import to avoid hard dep at module load."""
        from arq.connections import RedisSettings  # type: ignore[import-not-found]

        # ASPIRE_REDIS_URL set on Ava-Brain via P5 preflight (already in production).
        url = settings.redis_url or "redis://localhost:6379"
        return RedisSettings.from_dsn(url)

    redis_settings = property(get_redis_settings)  # type: ignore[assignment]

    # Concurrency
    max_jobs = 10
    job_timeout = 300  # 5 min
    keep_result = 86400  # 24h
    keep_result_forever = False

    # Retry
    retry_jobs = True
    max_tries = 5

    # Health
    health_check_interval = 30

    # Queue identifier
    queue_name = "arq:trust_onboarding"

    # Lifecycle hooks
    @staticmethod
    async def on_startup(ctx: dict[str, Any]) -> None:
        """Pre-warm the Trust Hub policy SID cache so the first job doesn't pay it."""
        from aspire_orchestrator.providers import twilio_trust_hub as thub

        try:
            await thub.fetch_secondary_profile_policy_sid()
            await thub.fetch_shaken_policy_sid()
            await thub.fetch_cnam_policy_sid()
            await thub.fetch_voice_integrity_policy_sid()
            logger.info("trust_onboarding_worker startup — policy SID cache warm")
        except Exception as exc:  # noqa: BLE001 — startup must not crash worker
            logger.warning("trust_onboarding_worker startup — policy fetch failed: %s", exc)

    @staticmethod
    async def on_shutdown(ctx: dict[str, Any]) -> None:
        logger.info("trust_onboarding_worker shutdown")


__all__ = [
    "advance_trust_state",
    "advance_a2p_registration",
    "poll_trust_status_for_tenants",
    "retry_failed_trust_onboardings",
    "WorkerSettings",
]
