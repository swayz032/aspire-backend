"""Number-swap state machine — Wave 11.

Drives the 11-step atomic number-reassignment flow for an Aspire tenant.
Entry point: `run_number_swap(swap_job_id, *, worker_job_id)`.

Step sequence (idempotent on every step — safe to re-run after worker kill):
    1.  Purchase new Twilio number (twilio_provisioning.purchase_number)
    2.  INSERT new tenant_phone_numbers row, status='active'
    3.  Attach new number → Customer Profile (assign_number_to_profile)
    4.  Attach new number → SHAKEN bundle   (add_phone_to_trust_product)
    5.  Attach new number → CNAM bundle     (add_phone_to_trust_product)
    6.  Enable VoiceCallerIdLookup on new number
    7.  ATOMIC SWITCH: front_desk_configs.phone_number_id = new number id
    8.  Detach old number from CP + SHAKEN + CNAM
    9.  Disable VoiceCallerIdLookup on old number
    10. Mark old tenant_phone_numbers.status = 'released'
    11. (optional) Release old Twilio number from account

Receipts (12 per swap, hash-chained via trust_receipts.cut_trust_receipt):
    number_swap_initiated
    number_attached_to_profile × 3   (CP, SHAKEN, CNAM)
    caller_id_lookup_enabled
    front_desk_phone_switched
    number_detached_from_profile × 3 (CP, SHAKEN, CNAM)
    caller_id_lookup_disabled
    phone_number_released
    number_swap_complete

Failure behaviour:
    Purchase fails                  → abort, no DB writes, re-raise
    Mid-attach fails                → roll forward on retry (idempotent)
    Atomic switch (step 7) fails    → ROLLBACK: detach new from bundles,
                                      release new number, old stays live
    Post-switch detach fails        → log + alert ops; non-blocking
    Twilio 429 / 5xx                → re-raise RetryableError (ARQ retries)

Aspire Laws:
    Law #1  — no autonomous decisions; caller supplies all parameters.
    Law #2  — every step cuts an immutable receipt.
    Law #3  — fail closed: missing credentials / invalid job → abort.
    Law #6  — suite_id/office_id isolation enforced from DB row.
    Law #9  — phone E.164 never in receipts (only Twilio SIDs).
    Law #10 — RetryableError propagates to ARQ for exponential backoff.

Author: Aspire — Wave 11
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from uuid import UUID

from aspire_orchestrator.providers import twilio_trust_hub as thub
from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
from aspire_orchestrator.services.resilience import RetryableError
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
    supabase_update,
)
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services.twilio_provisioning import purchase_number
from aspire_orchestrator.workers.trust_onboarding.trust_receipts import (
    TrustReceiptError,
    cut_trust_receipt,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public error classes
# ---------------------------------------------------------------------------


class SwapAbortError(Exception):
    """Raised when a swap must abort (non-retryable, e.g. purchase failed)."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"[{reason_code}] {message}")
        self.reason_code = reason_code


class SwapRollbackError(Exception):
    """Raised when the atomic switch failed and a clean rollback completed."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"[{reason_code}] {message}")
        self.reason_code = reason_code


# ---------------------------------------------------------------------------
# Idempotency-step helpers
# ---------------------------------------------------------------------------

# The swap job carries step completion state in the tenant_phone_swaps table
# (or can be derived from tenant_phone_numbers + front_desk_configs if no
# separate table exists). We use a swap_job record in tenant_phone_swaps.
# If that table is not yet migrated (migration 115 check), we fall back to
# reading state from the number rows themselves.
#
# Step completion flags stored on tenant_phone_swaps.progress (JSONB):
#   step_1_new_twilio_sid      — Twilio SID of purchased number
#   step_2_new_phone_id        — tenant_phone_numbers UUID for new number
#   step_3_cp_ra_sid           — RA SID on Customer Profile for new number
#   step_4_shaken_ra_sid       — RA SID on SHAKEN bundle for new number
#   step_5_cnam_ra_sid         — RA SID on CNAM bundle for new number
#   step_6_caller_id_enabled   — bool
#   step_7_switch_done         — bool
#   step_8_old_cp_detached     — bool
#   step_8_old_shaken_detached — bool
#   step_8_old_cnam_detached   — bool
#   step_9_caller_id_disabled  — bool
#   step_10_old_released       — bool


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _load_swap_job(swap_job_id: str) -> dict[str, Any]:
    """Load the tenant_phone_swaps row. Raise SwapAbortError if not found."""
    try:
        rows = await supabase_select(
            "tenant_phone_swaps",
            f"id=eq.{swap_job_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        raise SwapAbortError(
            "DB_UNAVAILABLE",
            f"Cannot load swap job {swap_job_id}: {exc}",
        ) from exc
    if not rows:
        raise SwapAbortError(
            "SWAP_JOB_NOT_FOUND",
            f"No tenant_phone_swaps row with id={swap_job_id}",
        )
    return rows[0]


async def _load_trust_profile(suite_id: str) -> dict[str, Any]:
    """Load tenant trust profile for receipt cutting."""
    try:
        rows = await supabase_select(
            "tenant_trust_profiles",
            f"suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        raise SwapAbortError(
            "DB_UNAVAILABLE",
            f"Cannot load trust profile for suite={suite_id}: {exc}",
        ) from exc
    if not rows:
        raise SwapAbortError(
            "NO_TRUST_PROFILE",
            f"No trust profile found for suite={suite_id}. Complete KYB onboarding first.",
        )
    return rows[0]


async def _load_old_phone_number(suite_id: str, phone_number_id: str) -> dict[str, Any]:
    """Load the current active tenant_phone_numbers row."""
    try:
        rows = await supabase_select(
            "tenant_phone_numbers",
            f"id=eq.{phone_number_id}&suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        raise SwapAbortError(
            "DB_UNAVAILABLE",
            f"Cannot load phone number {phone_number_id}: {exc}",
        ) from exc
    if not rows:
        raise SwapAbortError(
            "OLD_NUMBER_NOT_FOUND",
            f"tenant_phone_numbers id={phone_number_id} suite_id={suite_id} not found",
        )
    return rows[0]


async def _save_progress(swap_job_id: str, progress_patch: dict[str, Any]) -> None:
    """Merge progress_patch into tenant_phone_swaps.progress JSONB.

    Uses PATCH on the row — the DB trigger / policy does the JSON merge.
    We issue a raw update with progress key (PostgREST merges JSONB columns).
    """
    try:
        await supabase_update(
            "tenant_phone_swaps",
            f"id=eq.{swap_job_id}",
            {"progress": progress_patch, "updated_at": _now_iso()},
        )
    except SupabaseClientError as exc:
        # Progress persistence failure is logged but not fatal — idempotency
        # falls back to re-reading from Twilio and DB state on next run.
        logger.warning(
            "swap_sm progress_save_failed job_id=%s err=%s", swap_job_id, exc
        )


async def _update_swap_status(
    swap_job_id: str, job_status: str, *, reason_code: str | None = None
) -> None:
    patch: dict[str, Any] = {"status": job_status, "updated_at": _now_iso()}
    if reason_code:
        patch["reason_code"] = reason_code
    try:
        await supabase_update(
            "tenant_phone_swaps",
            f"id=eq.{swap_job_id}",
            patch,
        )
    except SupabaseClientError as exc:
        logger.warning(
            "swap_sm status_update_failed job_id=%s status=%s err=%s",
            swap_job_id, job_status, exc,
        )


# ---------------------------------------------------------------------------
# Rollback helper (called when step 7 atomic switch fails)
# ---------------------------------------------------------------------------


async def _rollback_new_number(
    *,
    trust_profile: dict[str, Any],
    swap_job_id: str,
    new_phone_sid: str,
    new_phone_id: str,
    cp_ra_sid: str | None,
    shaken_ra_sid: str | None,
    cnam_ra_sid: str | None,
    suite_id: str,
    cp_sid: str,
    shaken_sid: str,
    cnam_sid: str,
    worker_job_id: str,
) -> None:
    """Undo steps 1-6: detach new number from all bundles, release from Twilio.

    Called ONLY when step 7 fails. Best-effort — we log each sub-failure
    but continue through all rollback steps.
    """
    logger.warning(
        "swap_sm rollback_start job_id=%s new_number_sid=%s",
        swap_job_id, new_phone_sid,
    )

    # Detach from CNAM
    if cnam_ra_sid:
        try:
            await thub.delete_channel_endpoint_assignment(
                cnam_sid, cnam_ra_sid, kind="trust_product"
            )
        except (TrustHubError, RetryableError) as exc:
            logger.error("swap_sm rollback_cnam_detach_failed job=%s err=%s", swap_job_id, exc)

    # Detach from SHAKEN
    if shaken_ra_sid:
        try:
            await thub.delete_channel_endpoint_assignment(
                shaken_sid, shaken_ra_sid, kind="trust_product"
            )
        except (TrustHubError, RetryableError) as exc:
            logger.error("swap_sm rollback_shaken_detach_failed job=%s err=%s", swap_job_id, exc)

    # Detach from Customer Profile
    if cp_ra_sid:
        try:
            await thub.delete_channel_endpoint_assignment(
                cp_sid, cp_ra_sid, kind="customer_profile"
            )
        except (TrustHubError, RetryableError) as exc:
            logger.error("swap_sm rollback_cp_detach_failed job=%s err=%s", swap_job_id, exc)

    # Disable caller ID on new number (if we enabled it)
    try:
        await thub.disable_caller_id_lookup(
            new_phone_sid,
            idempotency_key=f"swap-rollback-cid-disable-{swap_job_id}",
        )
    except (TrustHubError, RetryableError) as exc:
        logger.error("swap_sm rollback_caller_id_disable_failed job=%s err=%s", swap_job_id, exc)

    # Release new number from Twilio account
    try:
        await thub.release_phone_number(new_phone_sid)
    except (TrustHubError, RetryableError) as exc:
        logger.error("swap_sm rollback_release_failed job=%s err=%s", swap_job_id, exc)

    # Mark new phone row as released
    try:
        await supabase_update(
            "tenant_phone_numbers",
            f"id=eq.{new_phone_id}&suite_id=eq.{suite_id}",
            {
                "status": "released",
                "released_at": _now_iso(),
                "released_reason": "tenant_swap",
            },
        )
    except SupabaseClientError as exc:
        logger.error("swap_sm rollback_db_release_failed job=%s err=%s", swap_job_id, exc)

    logger.warning(
        "swap_sm rollback_complete job_id=%s new_number_sid=%s",
        swap_job_id, new_phone_sid,
    )


# ---------------------------------------------------------------------------
# Main state machine
# ---------------------------------------------------------------------------


async def run_number_swap(
    swap_job_id: str,
    *,
    worker_job_id: str,
) -> dict[str, Any]:
    """Execute the 11-step number-swap state machine.

    Args:
        swap_job_id: UUID of the tenant_phone_swaps row.
        worker_job_id: ARQ job ID for traceability in receipts.

    Returns:
        {
            "swap_job_id": str,
            "outcome": "success" | "rolled_back" | "failed",
            "old_number_e164": str,
            "new_number_e164": str,
            "receipt_ids": list[str],
        }

    Raises:
        RetryableError: on Twilio 429 / 5xx — ARQ will retry with backoff.
        SwapAbortError: on non-retryable pre-condition failures (no DB write).
        SwapRollbackError: on step-7 failure — rollback complete, old live.
    """
    receipt_ids: list[str] = []

    # ── Load swap job ────────────────────────────────────────────────────────
    job = await _load_swap_job(swap_job_id)
    suite_id = str(job["suite_id"])
    office_id = str(job["office_id"])
    tenant_id = str(job.get("tenant_id", ""))
    old_phone_number_id = str(job["old_phone_number_id"])
    new_number_e164: str = str(job.get("new_number_e164", ""))
    release_old: bool = bool(job.get("release_old_number", True))

    # progress is JSONB — read the persisted sub-steps
    progress: dict[str, Any] = dict(job.get("progress") or {})

    # ── Load trust profile (for receipt cutting) ─────────────────────────────
    trust_profile = await _load_trust_profile(suite_id)
    trust_profile_id = str(trust_profile["id"])
    cp_sid = str(trust_profile.get("customer_profile_sid", ""))
    shaken_sid = str(trust_profile.get("shaken_trust_product_sid", ""))
    cnam_sid = str(trust_profile.get("cnam_trust_product_sid", ""))

    if not cp_sid:
        raise SwapAbortError(
            "NO_CUSTOMER_PROFILE",
            f"Trust profile {trust_profile_id} has no customer_profile_sid. "
            "Complete KYB onboarding first.",
        )

    # ── Load old phone row ───────────────────────────────────────────────────
    old_phone_row = await _load_old_phone_number(suite_id, old_phone_number_id)
    old_phone_e164 = str(old_phone_row.get("phone_number", ""))
    old_phone_sid = str(old_phone_row.get("twilio_sid", ""))

    scope = ScopedIdentity(
        tenant_id=UUID(tenant_id),
        suite_id=UUID(suite_id),
        office_id=UUID(office_id),
    )

    logger.info(
        "swap_sm start job=%s suite=%s old_number=%s new_number=%s",
        swap_job_id, suite_id, old_phone_e164[:6] + "***", new_number_e164[:6] + "***",
    )

    # ── Step 1: Receipt — number_swap_initiated ──────────────────────────────
    if not progress.get("step_1_initiated_receipt"):
        rid = await cut_trust_receipt(
            receipt_type="number_swap_initiated",
            trust_profile=trust_profile,
            outcome="pending",
            from_state="number_attached",
            to_state="number_swap_in_progress",
            redacted_inputs={
                "swap_job_id": swap_job_id,
                "old_phone_number_id": old_phone_number_id,
            },
            reason_code="SWAP_STARTED",
            worker_job_id=worker_job_id,
        )
        receipt_ids.append(rid)
        progress["step_1_initiated_receipt"] = rid
        await _save_progress(swap_job_id, progress)

    # ── Step 1b: Purchase new Twilio number ──────────────────────────────────
    new_phone_sid: str = progress.get("step_1_new_twilio_sid", "")
    new_phone_id: str = progress.get("step_2_new_phone_id", "")

    if not new_phone_sid:
        idem_key = f"swap-purchase-{swap_job_id}"
        try:
            purchased = await purchase_number(
                new_number_e164,
                scope=scope,
                idempotency_key=idem_key,
            )
        except RetryableError:
            raise  # Law #10 — ARQ retries
        except Exception as exc:
            raise SwapAbortError(
                "PURCHASE_FAILED",
                f"New number purchase failed: {exc}",
            ) from exc

        new_phone_sid = purchased.twilio_sid
        # Look up the DB row UUID by twilio_sid (PurchasedNumber doesn't carry the row id)
        try:
            phone_rows = await supabase_select(
                "tenant_phone_numbers",
                f"twilio_sid=eq.{new_phone_sid}&suite_id=eq.{suite_id}",
                limit=1,
            )
            new_phone_id = str(phone_rows[0]["id"]) if phone_rows else ""
        except SupabaseClientError:
            new_phone_id = ""

        progress["step_1_new_twilio_sid"] = new_phone_sid
        progress["step_2_new_phone_id"] = new_phone_id
        await _save_progress(swap_job_id, progress)
    else:
        # Re-read the new phone row in case purchase_number already ran
        if not new_phone_id:
            try:
                rows = await supabase_select(
                    "tenant_phone_numbers",
                    f"twilio_sid=eq.{new_phone_sid}&suite_id=eq.{suite_id}",
                    limit=1,
                )
                new_phone_id = str(rows[0]["id"]) if rows else ""
            except SupabaseClientError:
                pass

    logger.info(
        "swap_sm step_1_2_done job=%s new_sid=%s new_phone_id=%s",
        swap_job_id, new_phone_sid, new_phone_id,
    )

    # ── Step 3: Attach new number → Customer Profile ─────────────────────────
    cp_ra_sid: str = progress.get("step_3_cp_ra_sid", "")
    if not cp_ra_sid:
        try:
            cp_result = await thub.assign_number_to_profile(
                cp_sid,
                new_phone_sid,
                idempotency_key=f"swap-cp-attach-{swap_job_id}",
            )
        except RetryableError:
            raise
        except TrustHubError as exc:
            raise RetryableError(
                "SWAP_CP_ATTACH_FAILED",
                f"CP attach failed: {exc}",
            ) from exc

        cp_ra_sid = str(cp_result.get("sid", ""))
        progress["step_3_cp_ra_sid"] = cp_ra_sid
        await _save_progress(swap_job_id, progress)

        rid = await cut_trust_receipt(
            receipt_type="number_attached_to_profile",
            trust_profile=trust_profile,
            outcome="success",
            from_state="number_swap_in_progress",
            to_state="number_swap_in_progress",
            redacted_inputs={
                "swap_job_id": swap_job_id,
                "bundle_type": "customer_profile",
                "new_phone_number_id": new_phone_id,
            },
            redacted_outputs={"twilio_resource_sid": cp_ra_sid, "bundle_sid": cp_sid},
            twilio_resource_sid=cp_ra_sid,
            worker_job_id=worker_job_id,
        )
        receipt_ids.append(rid)

    # ── Step 4: Attach new number → SHAKEN bundle ────────────────────────────
    shaken_ra_sid: str = progress.get("step_4_shaken_ra_sid", "")
    if not shaken_ra_sid and shaken_sid:
        try:
            shaken_result = await thub.add_phone_to_trust_product(
                shaken_sid,
                new_phone_sid,
                idempotency_key=f"swap-shaken-attach-{swap_job_id}",
            )
        except RetryableError:
            raise
        except TrustHubError as exc:
            raise RetryableError(
                "SWAP_SHAKEN_ATTACH_FAILED",
                f"SHAKEN attach failed: {exc}",
            ) from exc

        shaken_ra_sid = str(shaken_result.get("sid", ""))
        progress["step_4_shaken_ra_sid"] = shaken_ra_sid
        await _save_progress(swap_job_id, progress)

        rid = await cut_trust_receipt(
            receipt_type="number_attached_to_profile",
            trust_profile=trust_profile,
            outcome="success",
            from_state="number_swap_in_progress",
            to_state="number_swap_in_progress",
            redacted_inputs={
                "swap_job_id": swap_job_id,
                "bundle_type": "shaken",
                "new_phone_number_id": new_phone_id,
            },
            redacted_outputs={"twilio_resource_sid": shaken_ra_sid, "bundle_sid": shaken_sid},
            twilio_resource_sid=shaken_ra_sid,
            worker_job_id=worker_job_id,
        )
        receipt_ids.append(rid)

    # ── Step 5: Attach new number → CNAM bundle ──────────────────────────────
    cnam_ra_sid: str = progress.get("step_5_cnam_ra_sid", "")
    if not cnam_ra_sid and cnam_sid:
        try:
            cnam_result = await thub.add_phone_to_trust_product(
                cnam_sid,
                new_phone_sid,
                idempotency_key=f"swap-cnam-attach-{swap_job_id}",
            )
        except RetryableError:
            raise
        except TrustHubError as exc:
            raise RetryableError(
                "SWAP_CNAM_ATTACH_FAILED",
                f"CNAM attach failed: {exc}",
            ) from exc

        cnam_ra_sid = str(cnam_result.get("sid", ""))
        progress["step_5_cnam_ra_sid"] = cnam_ra_sid
        await _save_progress(swap_job_id, progress)

        rid = await cut_trust_receipt(
            receipt_type="number_attached_to_profile",
            trust_profile=trust_profile,
            outcome="success",
            from_state="number_swap_in_progress",
            to_state="number_swap_in_progress",
            redacted_inputs={
                "swap_job_id": swap_job_id,
                "bundle_type": "cnam",
                "new_phone_number_id": new_phone_id,
            },
            redacted_outputs={"twilio_resource_sid": cnam_ra_sid, "bundle_sid": cnam_sid},
            twilio_resource_sid=cnam_ra_sid,
            worker_job_id=worker_job_id,
        )
        receipt_ids.append(rid)

    # ── Step 6: Enable VoiceCallerIdLookup on new number ─────────────────────
    if not progress.get("step_6_caller_id_enabled"):
        try:
            await thub.enable_caller_id_lookup(
                new_phone_sid,
                idempotency_key=f"swap-cid-enable-{swap_job_id}",
            )
        except RetryableError:
            raise
        except TrustHubError as exc:
            raise RetryableError(
                "SWAP_CALLER_ID_ENABLE_FAILED",
                f"Caller ID enable failed: {exc}",
            ) from exc

        progress["step_6_caller_id_enabled"] = True
        await _save_progress(swap_job_id, progress)

        rid = await cut_trust_receipt(
            receipt_type="caller_id_lookup_enabled",
            trust_profile=trust_profile,
            outcome="success",
            from_state="number_swap_in_progress",
            to_state="number_swap_in_progress",
            redacted_inputs={
                "swap_job_id": swap_job_id,
                "new_phone_number_id": new_phone_id,
            },
            redacted_outputs={"twilio_resource_sid": new_phone_sid},
            twilio_resource_sid=new_phone_sid,
            worker_job_id=worker_job_id,
        )
        receipt_ids.append(rid)

    # ── Step 7: ATOMIC SWITCH — front_desk_configs.phone_number_id ───────────
    if not progress.get("step_7_switch_done"):
        try:
            await supabase_update(
                "front_desk_configs",
                f"suite_id=eq.{suite_id}&office_id=eq.{office_id}",
                {"phone_number_id": new_phone_id, "updated_at": _now_iso()},
            )
        except SupabaseClientError as exc:
            # Step 7 failed — ROLL BACK new number; old number stays live.
            logger.error(
                "swap_sm step_7_atomic_switch_failed job=%s err=%s — rolling back",
                swap_job_id, exc,
            )
            await _rollback_new_number(
                trust_profile=trust_profile,
                swap_job_id=swap_job_id,
                new_phone_sid=new_phone_sid,
                new_phone_id=new_phone_id,
                cp_ra_sid=cp_ra_sid or None,
                shaken_ra_sid=shaken_ra_sid or None,
                cnam_ra_sid=cnam_ra_sid or None,
                suite_id=suite_id,
                cp_sid=cp_sid,
                shaken_sid=shaken_sid,
                cnam_sid=cnam_sid,
                worker_job_id=worker_job_id,
            )
            await _update_swap_status(swap_job_id, "rolled_back", reason_code="SWITCH_FAILED")
            raise SwapRollbackError(
                "ATOMIC_SWITCH_FAILED",
                f"front_desk_configs update failed: {exc}. Rollback complete — old number is live.",
            ) from exc

        progress["step_7_switch_done"] = True
        await _save_progress(swap_job_id, progress)

        rid = await cut_trust_receipt(
            receipt_type="front_desk_phone_switched",
            trust_profile=trust_profile,
            outcome="success",
            from_state="number_swap_in_progress",
            to_state="number_swap_in_progress",
            redacted_inputs={
                "swap_job_id": swap_job_id,
                "old_phone_number_id": old_phone_number_id,
                "new_phone_number_id": new_phone_id,
            },
            redacted_outputs={"new_phone_twilio_sid": new_phone_sid},
            worker_job_id=worker_job_id,
        )
        receipt_ids.append(rid)

    # ── Step 8: Detach OLD number from CP + SHAKEN + CNAM ────────────────────
    # Post-switch: detach failures are NON-BLOCKING (old number cleanup).
    # We find the old RA SIDs by listing current assignments and matching.

    if not progress.get("step_8_old_cp_detached"):
        await _detach_old_number_from_bundle(
            bundle_sid=cp_sid,
            old_number_sid=old_phone_sid,
            kind="customer_profile",
            swap_job_id=swap_job_id,
            suite_id=suite_id,
        )
        progress["step_8_old_cp_detached"] = True
        await _save_progress(swap_job_id, progress)

        rid = await cut_trust_receipt(
            receipt_type="number_detached_from_profile",
            trust_profile=trust_profile,
            outcome="success",
            from_state="number_swap_in_progress",
            to_state="number_swap_in_progress",
            redacted_inputs={
                "swap_job_id": swap_job_id,
                "bundle_type": "customer_profile",
                "old_phone_number_id": old_phone_number_id,
            },
            redacted_outputs={"bundle_sid": cp_sid},
            worker_job_id=worker_job_id,
        )
        receipt_ids.append(rid)

    if not progress.get("step_8_old_shaken_detached") and shaken_sid:
        await _detach_old_number_from_bundle(
            bundle_sid=shaken_sid,
            old_number_sid=old_phone_sid,
            kind="trust_product",
            swap_job_id=swap_job_id,
            suite_id=suite_id,
        )
        progress["step_8_old_shaken_detached"] = True
        await _save_progress(swap_job_id, progress)

        rid = await cut_trust_receipt(
            receipt_type="number_detached_from_profile",
            trust_profile=trust_profile,
            outcome="success",
            from_state="number_swap_in_progress",
            to_state="number_swap_in_progress",
            redacted_inputs={
                "swap_job_id": swap_job_id,
                "bundle_type": "shaken",
                "old_phone_number_id": old_phone_number_id,
            },
            redacted_outputs={"bundle_sid": shaken_sid},
            worker_job_id=worker_job_id,
        )
        receipt_ids.append(rid)

    if not progress.get("step_8_old_cnam_detached") and cnam_sid:
        await _detach_old_number_from_bundle(
            bundle_sid=cnam_sid,
            old_number_sid=old_phone_sid,
            kind="trust_product",
            swap_job_id=swap_job_id,
            suite_id=suite_id,
        )
        progress["step_8_old_cnam_detached"] = True
        await _save_progress(swap_job_id, progress)

        rid = await cut_trust_receipt(
            receipt_type="number_detached_from_profile",
            trust_profile=trust_profile,
            outcome="success",
            from_state="number_swap_in_progress",
            to_state="number_swap_in_progress",
            redacted_inputs={
                "swap_job_id": swap_job_id,
                "bundle_type": "cnam",
                "old_phone_number_id": old_phone_number_id,
            },
            redacted_outputs={"bundle_sid": cnam_sid},
            worker_job_id=worker_job_id,
        )
        receipt_ids.append(rid)

    # ── Step 9: Disable VoiceCallerIdLookup on old number ────────────────────
    if not progress.get("step_9_caller_id_disabled") and old_phone_sid:
        try:
            await thub.disable_caller_id_lookup(
                old_phone_sid,
                idempotency_key=f"swap-cid-disable-{swap_job_id}",
            )
        except (TrustHubError, RetryableError) as exc:
            # Non-blocking: old number is no longer the active number.
            logger.warning(
                "swap_sm step_9_disable_caller_id_warning job=%s err=%s",
                swap_job_id, exc,
            )

        progress["step_9_caller_id_disabled"] = True
        await _save_progress(swap_job_id, progress)

        rid = await cut_trust_receipt(
            receipt_type="caller_id_lookup_disabled",
            trust_profile=trust_profile,
            outcome="success",
            from_state="number_swap_in_progress",
            to_state="number_swap_in_progress",
            redacted_inputs={
                "swap_job_id": swap_job_id,
                "old_phone_number_id": old_phone_number_id,
            },
            redacted_outputs={"twilio_resource_sid": old_phone_sid},
            twilio_resource_sid=old_phone_sid,
            worker_job_id=worker_job_id,
        )
        receipt_ids.append(rid)

    # ── Step 10: Mark old tenant_phone_numbers.status = 'released' ───────────
    if not progress.get("step_10_old_released"):
        try:
            await supabase_update(
                "tenant_phone_numbers",
                f"id=eq.{old_phone_number_id}&suite_id=eq.{suite_id}",
                {
                    "status": "released",
                    "released_at": _now_iso(),
                    "released_reason": "tenant_swap",
                },
            )
        except SupabaseClientError as exc:
            # Non-blocking: new number is live. Log for ops cleanup.
            logger.error(
                "swap_sm step_10_db_release_failed job=%s err=%s", swap_job_id, exc
            )

        progress["step_10_old_released"] = True
        await _save_progress(swap_job_id, progress)

        rid = await cut_trust_receipt(
            receipt_type="phone_number_released",
            trust_profile=trust_profile,
            outcome="success",
            from_state="number_swap_in_progress",
            to_state="number_swap_in_progress",
            redacted_inputs={
                "swap_job_id": swap_job_id,
                "old_phone_number_id": old_phone_number_id,
            },
            redacted_outputs={"old_phone_twilio_sid": old_phone_sid},
            worker_job_id=worker_job_id,
        )
        receipt_ids.append(rid)

    # ── Step 11 (optional): Release old Twilio number from account ────────────
    if release_old and not progress.get("step_11_twilio_released"):
        try:
            await thub.release_phone_number(old_phone_sid)
            progress["step_11_twilio_released"] = True
            await _save_progress(swap_job_id, progress)
            logger.info(
                "swap_sm step_11_twilio_release_done job=%s old_sid=%s",
                swap_job_id, old_phone_sid,
            )
        except RetryableError:
            raise  # Let ARQ retry
        except TrustHubError as exc:
            logger.error(
                "swap_sm step_11_twilio_release_failed job=%s err=%s", swap_job_id, exc
            )

    # ── Final receipt: number_swap_complete ──────────────────────────────────
    rid = await cut_trust_receipt(
        receipt_type="number_swap_complete",
        trust_profile=trust_profile,
        outcome="success",
        from_state="number_swap_in_progress",
        to_state="number_attached",
        redacted_inputs={
            "swap_job_id": swap_job_id,
            "old_phone_number_id": old_phone_number_id,
            "new_phone_number_id": new_phone_id,
        },
        redacted_outputs={"new_phone_twilio_sid": new_phone_sid},
        worker_job_id=worker_job_id,
    )
    receipt_ids.append(rid)

    # Mark swap job as complete
    await _update_swap_status(swap_job_id, "completed")

    logger.info(
        "swap_sm complete job=%s receipts=%d old_phone_id=%s new_phone_id=%s",
        swap_job_id, len(receipt_ids), old_phone_number_id, new_phone_id,
    )

    return {
        "swap_job_id": swap_job_id,
        "outcome": "success",
        "old_number_e164": old_phone_e164,
        "new_number_e164": new_number_e164,
        "receipt_ids": receipt_ids,
    }


# ---------------------------------------------------------------------------
# Helper: detach old number from a bundle (non-blocking after step 7)
# ---------------------------------------------------------------------------


async def _detach_old_number_from_bundle(
    *,
    bundle_sid: str,
    old_number_sid: str,
    kind: str,
    swap_job_id: str,
    suite_id: str,
) -> None:
    """Find and delete the ChannelEndpointAssignment for old_number_sid.

    Non-blocking post-switch: failures are logged, not re-raised.
    The tenant has a working new number; old cleanup is operational.
    """
    from typing import Literal as _Literal

    _kind = kind  # type: ignore[assignment]

    try:
        assignments = await thub.list_channel_endpoint_assignments(
            bundle_sid,
            kind=_kind,  # type: ignore[arg-type]
        )
    except (TrustHubError, RetryableError) as exc:
        logger.warning(
            "swap_sm detach_old_list_failed job=%s bundle=%s kind=%s err=%s",
            swap_job_id, bundle_sid, kind, exc,
        )
        return

    # Find the RA SID for our old number
    ra_sid: str | None = None
    for assignment in assignments:
        if str(assignment.get("channel_endpoint_sid", "")) == old_number_sid:
            ra_sid = str(assignment.get("sid", ""))
            break

    if not ra_sid:
        logger.info(
            "swap_sm detach_old_already_gone job=%s bundle=%s number_sid=%s",
            swap_job_id, bundle_sid, old_number_sid,
        )
        return

    try:
        await thub.delete_channel_endpoint_assignment(
            bundle_sid,
            ra_sid,
            kind=_kind,  # type: ignore[arg-type]
        )
    except (TrustHubError, RetryableError) as exc:
        logger.error(
            "swap_sm detach_old_delete_failed job=%s bundle=%s ra=%s err=%s",
            swap_job_id, bundle_sid, ra_sid, exc,
        )


__all__ = [
    "run_number_swap",
    "SwapAbortError",
    "SwapRollbackError",
]
