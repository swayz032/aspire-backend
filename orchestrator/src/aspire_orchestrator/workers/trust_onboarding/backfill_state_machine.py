"""Trust onboarding BACKFILL state machine — Wave 10.

Drives existing-tenant migration from the shared master SHAKEN bundle to
their own per-tenant SHAKEN/CNAM bundles. One advance per ARQ job tick.

Single entry point: `advance_backfill(suite_id, *, worker_job_id)`.

Step sequence (8 steps; each step is idempotent and re-entrant):

    1. VERIFY: tenant has no existing tenant_trust_profiles row.
       If present and is_backfill=False → fail (already onboarded normally).
       If present and is_backfill=True → resume from current state.
       If absent → step 2 inserts the bootstrap row.

    2. INSERT: bootstrap tenant_trust_profiles row with `trust_state=
       'kyb_collected'` and `is_backfill=True`. The W10 admin route is
       expected to have already collected KYB out-of-band (or the admin
       knows the tenant + sends it through anyway). For Scott Painting
       and the user's own canary, KYB info comes from `suite_profiles` +
       a single Twilio-Console-mirrored manual entry; the bootstrap row
       carries `legal_business_name`, `business_type`, `address_*`
       fields by reading suite_profiles. EIN/DOB/SSN must be supplied
       through the regular W3 KYB intake before this state machine can
       advance past `kyb_collected` — that's enforced by the W3 state
       machine which this state machine REUSES via `advance_trust_state`.

    3. SUBMIT Customer Profile (REUSES W3 logic via `state_machine.
       advance_trust_state`). State advances kyb_collected →
       profile_drafted → profile_submitted. Halt for Twilio review.

    4. WAIT until trust_state='profile_approved' (Twilio-webhook driven
       in production; cron poll in W9 backstops). Re-enter this state
       machine on each advance until profile_approved is observed.

    5. SUBMIT per-tenant SHAKEN Trust Product (REUSES W4 logic via
       `state_machine.advance_trust_state`: profile_approved →
       shaken_created → shaken_submitted). Halt for Twilio review.

    6. WAIT until trust_state='shaken_approved'. Twilio review for
       SHAKEN is fast (typically minutes).

    7. CRITICAL — DETACH-FROM-SHARED ORDERING ENFORCEMENT.
       This is the W10-specific step. The state machine MUST NOT
       advance past step 6 until `shaken_approved` is observed. Once
       it is, step 7 issues exactly one Trust Hub call:
         a. List channel-endpoint-assignments on the shared SHAKEN
            bundle, find the RA SID for this tenant's phone number.
         b. DELETE that RA SID from the shared bundle.
         c. Cut a `number_detached_from_shared_profile` receipt.
       Step 7 is followed (in the same ARQ tick) by W4's existing
       attach-to-per-tenant-SHAKEN logic via
       `state_machine.advance_trust_state(..., from='shaken_approved')`
       — this advances trust_state to `cnam_created`, attaches the
       phone to the per-tenant SHAKEN, and starts CNAM processing.

       HARD INVARIANT: detach-from-shared NEVER fires before
       shaken_approved. If the state machine is invoked at any earlier
       state, step 7 is a no-op. The negative-path test in
       test_backfill_state_machine.py asserts this invariant.

    8. CONTINUE through CNAM submission + approval + number_attached
       (REUSES W5 logic). On reaching `number_attached`, the state
       machine cuts a final `backfill_complete` receipt and terminates.

Receipt flow (8+ per backfill, hash-chained per trust_profile_id):

    1. kyb_collected         (with redacted_inputs.is_backfill=true)
    2. customer_profile_created          (W3 reuse)
    3. customer_profile_submitted        (W3 reuse)
    4. customer_profile_approved         (webhook-driven, W5 reuse)
    5. shaken_trust_product_created      (W4 reuse)
    6. shaken_trust_product_submitted    (W4 reuse)
    7. shaken_trust_product_approved     (webhook-driven, W5 reuse)
    8. number_detached_from_shared_profile  (W10 NEW)
    9. cnam_trust_product_created        (W5 reuse)
   10. cnam_trust_product_submitted      (W5 reuse)
   11. cnam_trust_product_approved       (webhook-driven, W5 reuse)
   12. number_attached_to_profile        (W5 reuse)
   13. caller_id_lookup_enabled          (W5 reuse)
   14. backfill_complete                 (W10 NEW — terminal)

Aspire Laws enforced:
    Law #1  — single brain. State machine is mechanical; no decisions.
    Law #2  — every transition cuts an immutable receipt.
    Law #3  — fail closed on missing shared SHAKEN config, missing
              suite, missing phone number.
    Law #6  — service_role load with suite_id filter; never touches
              other tenants. Cross-tenant test in
              test_backfill_state_machine.py asserts.
    Law #9  — phone E.164 redacted in receipts (only Twilio SIDs).
    Law #10 — RetryableError propagates so ARQ retries with backoff.

Author: Aspire — Wave 10
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Final

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.providers import twilio_trust_hub as thub
from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
from aspire_orchestrator.services.resilience import RetryableError
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


# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class BackfillAbortError(Exception):
    """Raised when a backfill must abort (non-retryable, no DB writes pending)."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"[{reason_code}] {message}")
        self.reason_code = reason_code


# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------


# States at which the backfill flow is finished or in a terminal failure.
# Reaching `number_attached` cuts the `backfill_complete` receipt and stops.
_BACKFILL_TERMINAL_STATES: Final[frozenset[str]] = frozenset({
    "number_attached",
    "branded_calling_pending",
    "branded_calling_live",
    "failed",
    "suspended",
})


# Step 7 prerequisite: detach-from-shared MUST NOT fire until the per-tenant
# SHAKEN bundle is `twilio-approved`. The state machine reads `trust_state`
# and refuses to detach until this set is reached.
#
# Note: `cnam_created` and beyond imply step 7 already executed because the
# W3-W5 state machine cannot advance past `shaken_approved` without first
# attaching the phone to the per-tenant SHAKEN (which the backfill flow
# does AFTER detach-from-shared). The presence of these states means the
# detach is already done in a prior advance tick.
_DETACH_FROM_SHARED_ALLOWED_STATES: Final[frozenset[str]] = frozenset({
    "shaken_approved",
    "cnam_created",
    "cnam_submitted",
    "cnam_approved",
    "number_attached",
    "branded_calling_pending",
    "branded_calling_live",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _load_trust_profile_by_suite(suite_id: str) -> dict[str, Any] | None:
    """Load tenant_trust_profiles row for this suite, or None if absent.

    Service-role read — bypasses RLS. Only one row per suite (UNIQUE
    constraint on suite_id; see migration 109).
    """
    try:
        rows = await supabase_select(
            "tenant_trust_profiles",
            f"suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        raise BackfillAbortError(
            "DB_UNAVAILABLE",
            f"Cannot load tenant_trust_profiles for suite={suite_id}: {exc}",
        ) from exc
    return rows[0] if rows else None


async def _load_suite_profile(suite_id: str) -> dict[str, Any]:
    """Load suite_profiles row for KYB pre-fill on backfill bootstrap."""
    try:
        rows = await supabase_select(
            "suite_profiles",
            f"suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        raise BackfillAbortError(
            "DB_UNAVAILABLE",
            f"Cannot load suite_profiles for suite={suite_id}: {exc}",
        ) from exc
    if not rows:
        raise BackfillAbortError(
            "NO_SUITE_PROFILE",
            f"suite_profiles missing for suite_id={suite_id}; cannot bootstrap backfill",
        )
    return rows[0]


async def _load_active_phone_number(suite_id: str) -> dict[str, Any]:
    """Load the active tenant_phone_numbers row for this suite.

    Backfill targets the currently-active number (the one on the shared
    SHAKEN bundle today). Releasing/replacing numbers is W11 territory.
    """
    try:
        rows = await supabase_select(
            "tenant_phone_numbers",
            f"suite_id=eq.{suite_id}&status=eq.active",
            limit=1,
        )
    except SupabaseClientError as exc:
        raise BackfillAbortError(
            "DB_UNAVAILABLE",
            f"Cannot load tenant_phone_numbers for suite={suite_id}: {exc}",
        ) from exc
    if not rows:
        raise BackfillAbortError(
            "NO_ACTIVE_PHONE_NUMBER",
            f"No active tenant_phone_numbers for suite={suite_id}; cannot backfill",
        )
    return rows[0]


async def _insert_bootstrap_trust_profile(
    *,
    suite_id: str,
    suite_profile: dict[str, Any],
) -> dict[str, Any]:
    """Insert a tenant_trust_profiles row with trust_state='kyb_collected'.

    Pre-fills business identity fields from suite_profiles. EIN / DOB /
    SSN columns are NULL — the regular W3 KYB intake API must be called
    by ops to populate them via Vault before the state machine will
    advance past `kyb_collected`. The existing W3 logic in
    `state_machine._transition_kyb_collected` handles vault decryption.
    """
    tenant_id = str(suite_profile.get("tenant_id") or suite_profile.get("owner_id") or "")
    office_id = str(suite_profile.get("office_id") or suite_profile.get("primary_office_id") or "")

    if not tenant_id or not office_id:
        raise BackfillAbortError(
            "MISSING_SCOPE",
            f"suite_profiles for suite={suite_id} missing tenant_id or office_id; "
            f"got tenant_id={tenant_id!r} office_id={office_id!r}",
        )

    now = _now_iso()
    row: dict[str, Any] = {
        "tenant_id": tenant_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "trust_state": "kyb_collected",
        "is_backfill": True,
        "legal_business_name": suite_profile.get("business_name") or "",
        "dba_name": suite_profile.get("dba_name"),
        "business_type": suite_profile.get("entity_type") or "llc",
        "address_street": suite_profile.get("business_address_street") or "",
        "address_city": suite_profile.get("business_address_city") or "",
        "address_state": suite_profile.get("business_address_state") or "",
        "address_zip": suite_profile.get("business_address_zip") or "",
        "address_country": suite_profile.get("business_address_country") or "US",
        "kyb_collected_at": now,
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = await supabase_insert("tenant_trust_profiles", row)
    except SupabaseClientError as exc:
        raise BackfillAbortError(
            "DB_INSERT_FAILED",
            f"Insert into tenant_trust_profiles failed for suite={suite_id}: {exc}",
        ) from exc

    # PostgREST may return either a single dict or list; normalize.
    if isinstance(result, list):
        result = result[0] if result else {}
    return result if isinstance(result, dict) else row


# ---------------------------------------------------------------------------
# Step 7 — DETACH-FROM-SHARED (the W10-specific critical step)
# ---------------------------------------------------------------------------


async def _detach_number_from_shared_shaken(
    *,
    trust_profile: dict[str, Any],
    phone_number_row: dict[str, Any],
    worker_job_id: str | None,
) -> str | None:
    """Detach this tenant's number from the SHARED master SHAKEN bundle.

    HARD INVARIANT: caller MUST guarantee `trust_state` is one of
    `_DETACH_FROM_SHARED_ALLOWED_STATES`. This function double-checks
    and refuses to fire if the invariant is violated.

    Returns the cut receipt_id, or None if step was a no-op (already
    detached on a prior tick).

    Raises:
        RetryableError on Twilio 5xx (ARQ retries).
        BackfillAbortError on permanent fail (config missing).
        TrustReceiptError on receipt-cut failure.
    """
    trust_profile_id = str(trust_profile["id"])
    trust_state: str = str(trust_profile.get("trust_state", ""))

    # Belt-and-suspenders ordering check (Law #3 — fail closed on mistake).
    if trust_state not in _DETACH_FROM_SHARED_ALLOWED_STATES:
        logger.error(
            "backfill_sm detach_invariant_violation trust_profile_id=%s state=%s — "
            "REFUSING to detach number from shared SHAKEN before per-tenant SHAKEN approved",
            trust_profile_id, trust_state,
        )
        raise BackfillAbortError(
            "DETACH_INVARIANT_VIOLATED",
            f"Refused to detach number from shared SHAKEN: trust_state={trust_state} "
            f"is not in allowed set {sorted(_DETACH_FROM_SHARED_ALLOWED_STATES)}. "
            "Per-tenant SHAKEN must reach twilio-approved BEFORE shared detach.",
        )

    # Idempotency: if the trust_profile already records a detach receipt,
    # skip. We use trust_state_transitions as the durable signal.
    try:
        existing = await supabase_select(
            "trust_state_transitions",
            f"trust_profile_id=eq.{trust_profile_id}&"
            f"event_type=eq.number_detached_from_shared_profile",
            limit=1,
        )
    except SupabaseClientError as exc:
        # Not fatal — just be conservative and re-run the detach (it's
        # idempotent at the Trust Hub layer: 404 on already-deleted).
        logger.warning(
            "backfill_sm detach_idempotency_check_failed trust_profile_id=%s err=%s",
            trust_profile_id, exc,
        )
        existing = []

    if existing:
        logger.info(
            "backfill_sm detach_skipped_idempotent trust_profile_id=%s — "
            "number_detached_from_shared_profile already cut",
            trust_profile_id,
        )
        return None

    # Hard config: shared SHAKEN bundle SID required.
    shared_bundle_sid: str = (settings.twilio_shared_shaken_bundle_sid or "").strip()
    if not shared_bundle_sid:
        raise BackfillAbortError(
            "SHARED_SHAKEN_NOT_CONFIGURED",
            "ASPIRE_TWILIO_SHARED_SHAKEN_BUNDLE_SID not set; cannot detach. "
            "Configure the Railway env var with the master SHAKEN bundle SID before running W10 backfill.",
        )

    number_sid: str = str(
        phone_number_row.get("twilio_sid")
        or phone_number_row.get("phone_sid")
        or ""
    )
    if not number_sid:
        raise BackfillAbortError(
            "MISSING_NUMBER_SID",
            f"tenant_phone_numbers row has no twilio_sid for trust_profile={trust_profile_id}",
        )

    # ── Look up the channel-endpoint-assignment SID on the shared bundle.
    # Trust Hub doesn't accept ChannelEndpointSid as a filter on the list
    # endpoint (it returns all assignments for the bundle), so we list and
    # filter client-side. This is fine — shared bundles have at most a
    # few hundred assignments; well below pagination limits.
    try:
        assignments = await thub.list_channel_endpoint_assignments(
            shared_bundle_sid, kind="trust_product"
        )
    except TrustHubError as exc:
        if exc.status_code and exc.status_code >= 500:
            raise RetryableError(
                "BACKFILL_LIST_CEA_5XX",
                f"Twilio 5xx listing CEAs on shared SHAKEN bundle: {exc}",
            ) from exc
        raise

    target_ra_sid: str | None = None
    for assignment in assignments:
        # Twilio returns either `channel_endpoint_sid` or `ChannelEndpointSid`
        # depending on version; accept both.
        cea_pn = (
            assignment.get("channel_endpoint_sid")
            or assignment.get("ChannelEndpointSid")
            or ""
        )
        if cea_pn == number_sid:
            target_ra_sid = (
                assignment.get("sid")
                or assignment.get("Sid")
                or ""
            )
            break

    if not target_ra_sid:
        # Already detached (or never attached). Treat as success — cut
        # the receipt anyway so the audit trail has the no-op explicitly
        # recorded.
        logger.info(
            "backfill_sm detach_already_clean trust_profile_id=%s number_sid=%s — "
            "no CEA found on shared SHAKEN bundle",
            trust_profile_id, number_sid,
        )
    else:
        # ── Issue the DELETE.
        try:
            await thub.delete_channel_endpoint_assignment(
                shared_bundle_sid, target_ra_sid, kind="trust_product"
            )
        except TrustHubError as exc:
            if exc.status_code and exc.status_code >= 500:
                raise RetryableError(
                    "BACKFILL_DETACH_5XX",
                    f"Twilio 5xx deleting CEA from shared SHAKEN: {exc}",
                ) from exc
            raise

    # ── Cut the audit receipt.
    receipt_id = await cut_trust_receipt(
        receipt_type="number_detached_from_shared_profile",
        trust_profile=trust_profile,
        outcome="success",
        from_state=trust_state,
        to_state=trust_state,  # detach doesn't change trust_state itself
        twilio_resource_sid=target_ra_sid,
        worker_job_id=worker_job_id,
        redacted_inputs={
            "trust_profile_id": trust_profile_id,
            "step_name": "detach_from_shared_shaken",
            "is_backfill": True,
        },
        redacted_outputs={
            "twilio_resource_sid": target_ra_sid or "",
            "bundle_sid": shared_bundle_sid,
            # Whether we actually issued a DELETE or no-op'd because the
            # CEA was already gone.
            "ra_was_present": bool(target_ra_sid),
        },
    )
    return receipt_id


# ---------------------------------------------------------------------------
# Step 8 — terminal `backfill_complete` receipt
# ---------------------------------------------------------------------------


async def _cut_backfill_complete_receipt(
    *,
    trust_profile: dict[str, Any],
    worker_job_id: str | None,
) -> str:
    """Cut the W10 terminal receipt once trust_state reaches number_attached."""
    trust_profile_id = str(trust_profile["id"])

    # Idempotency: if backfill_complete already cut, skip.
    try:
        existing = await supabase_select(
            "trust_state_transitions",
            f"trust_profile_id=eq.{trust_profile_id}&"
            f"event_type=eq.backfill_complete",
            limit=1,
        )
    except SupabaseClientError:
        existing = []

    if existing:
        prior = existing[0].get("receipt_id") or ""
        return str(prior)

    return await cut_trust_receipt(
        receipt_type="backfill_complete",
        trust_profile=trust_profile,
        outcome="success",
        from_state="number_attached",
        to_state="number_attached",
        worker_job_id=worker_job_id,
        redacted_inputs={
            "trust_profile_id": trust_profile_id,
            "step_name": "backfill_complete",
            "is_backfill": True,
        },
        redacted_outputs={
            "twilio_resource_sid": str(
                trust_profile.get("twilio_secondary_profile_sid") or ""
            ),
        },
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def advance_backfill(
    suite_id: str,
    *,
    worker_job_id: str | None = None,
) -> dict[str, Any]:
    """Advance one tenant's backfill flow by one logical tick.

    Re-entrant: each invocation either:
      a. Bootstraps a tenant_trust_profiles row + cuts kyb_collected (step 1-2)
      b. Reuses W3 to advance through profile_drafted/submitted (step 3)
      c. Detects shaken_approved + runs detach-from-shared (step 7)
      d. Advances through CNAM via W5 reuse (step 8)
      e. Cuts backfill_complete on number_attached (terminal)

    Returns:
        {
            "suite_id": str,
            "trust_profile_id": str | None,
            "from_state": str,
            "to_state": str,
            "outcome": "success" | "halted" | "failed",
            "step": str,
            "receipt_id": str | None,
        }

    Raises:
        RetryableError — Twilio 5xx, ARQ retries with backoff (Law #10).

    Never raises BackfillAbortError to caller — those are converted to
    outcome="failed" so the worker doesn't infinitely retry on permanent
    config issues.
    """
    t_start = time.monotonic()
    logger.info(
        "backfill_sm advance suite_id=%s job_id=%s", suite_id, worker_job_id
    )

    # ── Step 1: VERIFY ────────────────────────────────────────────────────
    try:
        existing_profile = await _load_trust_profile_by_suite(suite_id)
    except BackfillAbortError as exc:
        return {
            "suite_id": suite_id,
            "trust_profile_id": None,
            "from_state": "unknown",
            "to_state": "unknown",
            "outcome": "failed",
            "step": "verify",
            "receipt_id": None,
            "reason_code": exc.reason_code,
        }

    if existing_profile is not None and not existing_profile.get("is_backfill", False):
        # Tenant already onboarded normally — refuse to clobber.
        logger.warning(
            "backfill_sm refused suite_id=%s — existing trust_profile is_backfill=False",
            suite_id,
        )
        return {
            "suite_id": suite_id,
            "trust_profile_id": str(existing_profile["id"]),
            "from_state": str(existing_profile.get("trust_state", "")),
            "to_state": str(existing_profile.get("trust_state", "")),
            "outcome": "failed",
            "step": "verify",
            "receipt_id": None,
            "reason_code": "ALREADY_ONBOARDED",
        }

    # ── Step 2: BOOTSTRAP if absent ───────────────────────────────────────
    if existing_profile is None:
        try:
            suite_profile = await _load_suite_profile(suite_id)
            new_profile = await _insert_bootstrap_trust_profile(
                suite_id=suite_id,
                suite_profile=suite_profile,
            )
        except BackfillAbortError as exc:
            return {
                "suite_id": suite_id,
                "trust_profile_id": None,
                "from_state": "unknown",
                "to_state": "unknown",
                "outcome": "failed",
                "step": "bootstrap",
                "receipt_id": None,
                "reason_code": exc.reason_code,
            }

        # Cut the kyb_collected receipt (with is_backfill=True flag).
        try:
            receipt_id = await cut_trust_receipt(
                receipt_type="kyb_collected",
                trust_profile=new_profile,
                outcome="success",
                from_state="bootstrap",
                to_state="kyb_collected",
                worker_job_id=worker_job_id,
                redacted_inputs={
                    "trust_profile_id": str(new_profile.get("id", "")),
                    "step_name": "backfill_bootstrap",
                    "is_backfill": True,
                },
                redacted_outputs={
                    "trust_profile_id": str(new_profile.get("id", "")),
                },
            )
        except TrustReceiptError as exc:
            logger.error(
                "backfill_sm bootstrap_receipt_failed suite_id=%s err=%s",
                suite_id, exc,
            )
            receipt_id = None

        return {
            "suite_id": suite_id,
            "trust_profile_id": str(new_profile.get("id", "")),
            "from_state": "bootstrap",
            "to_state": "kyb_collected",
            "outcome": "success",
            "step": "bootstrap",
            "receipt_id": receipt_id,
            "latency_seconds": round(time.monotonic() - t_start, 3),
        }

    # ── Existing-profile flow (steps 3-8) ─────────────────────────────────
    trust_profile = existing_profile
    trust_profile_id = str(trust_profile["id"])
    trust_state: str = str(trust_profile.get("trust_state", ""))

    # Terminal — cut backfill_complete + return.
    if trust_state == "number_attached":
        try:
            receipt_id = await _cut_backfill_complete_receipt(
                trust_profile=trust_profile,
                worker_job_id=worker_job_id,
            )
        except TrustReceiptError as exc:
            logger.error(
                "backfill_sm complete_receipt_failed trust_profile_id=%s err=%s",
                trust_profile_id, exc,
            )
            receipt_id = ""
        return {
            "suite_id": suite_id,
            "trust_profile_id": trust_profile_id,
            "from_state": trust_state,
            "to_state": trust_state,
            "outcome": "success",
            "step": "complete",
            "receipt_id": receipt_id or None,
            "latency_seconds": round(time.monotonic() - t_start, 3),
        }

    if trust_state in {"branded_calling_pending", "branded_calling_live"}:
        # Already past the W10 scope — backfill_complete is W6 territory; safe halt.
        return {
            "suite_id": suite_id,
            "trust_profile_id": trust_profile_id,
            "from_state": trust_state,
            "to_state": trust_state,
            "outcome": "halted",
            "step": "post_terminal",
            "receipt_id": None,
        }

    if trust_state in {"failed", "suspended", "profile_rejected"}:
        return {
            "suite_id": suite_id,
            "trust_profile_id": trust_profile_id,
            "from_state": trust_state,
            "to_state": trust_state,
            "outcome": "failed",
            "step": "terminal",
            "receipt_id": None,
            "reason_code": "TERMINAL_FAILURE_STATE",
        }

    # ── Step 7: detach-from-shared on shaken_approved ─────────────────────
    # Run BEFORE invoking advance_trust_state so the detach happens between
    # SHAKEN approval and per-tenant SHAKEN attach.
    if trust_state == "shaken_approved":
        try:
            phone_row = await _load_active_phone_number(suite_id)
        except BackfillAbortError as exc:
            return {
                "suite_id": suite_id,
                "trust_profile_id": trust_profile_id,
                "from_state": trust_state,
                "to_state": trust_state,
                "outcome": "failed",
                "step": "detach_load_phone",
                "receipt_id": None,
                "reason_code": exc.reason_code,
            }

        try:
            detach_receipt = await _detach_number_from_shared_shaken(
                trust_profile=trust_profile,
                phone_number_row=phone_row,
                worker_job_id=worker_job_id,
            )
        except BackfillAbortError as exc:
            return {
                "suite_id": suite_id,
                "trust_profile_id": trust_profile_id,
                "from_state": trust_state,
                "to_state": trust_state,
                "outcome": "failed",
                "step": "detach_from_shared",
                "receipt_id": None,
                "reason_code": exc.reason_code,
            }
        # RetryableError + TrustHubError surface to caller for ARQ retry/abort.

        # Continue immediately with W4 advance (shaken_approved → cnam_created
        # via the existing state machine).
        from aspire_orchestrator.workers.trust_onboarding.state_machine import (
            advance_trust_state as advance_w_inner,
        )
        try:
            inner_result = await advance_w_inner(
                trust_profile_id=trust_profile_id,
                worker_job_id=worker_job_id,
            )
        except Exception as exc:  # noqa: BLE001 — never crash worker
            logger.error(
                "backfill_sm reuse_advance_failed trust_profile_id=%s err=%s",
                trust_profile_id, exc,
                exc_info=True,
            )
            inner_result = {
                "from_state": trust_state,
                "to_state": trust_state,
                "outcome": "failed",
                "receipt_id": None,
            }

        return {
            "suite_id": suite_id,
            "trust_profile_id": trust_profile_id,
            "from_state": trust_state,
            "to_state": str(inner_result.get("to_state", trust_state)),
            "outcome": str(inner_result.get("outcome", "success")),
            "step": "detach_and_advance",
            "receipt_id": detach_receipt or inner_result.get("receipt_id"),
            "latency_seconds": round(time.monotonic() - t_start, 3),
        }

    # ── Steps 3-6 + 8: Reuse the W3-W5 state machine ─────────────────────
    # All other states (kyb_collected, profile_drafted, profile_approved,
    # shaken_created, cnam_created, cnam_approved, halt-states) delegate
    # to the W3-W5 state machine directly.
    from aspire_orchestrator.workers.trust_onboarding.state_machine import (
        advance_trust_state as advance_w_inner,
    )
    try:
        inner_result = await advance_w_inner(
            trust_profile_id=trust_profile_id,
            worker_job_id=worker_job_id,
        )
    except Exception as exc:  # noqa: BLE001 — never crash worker
        logger.error(
            "backfill_sm reuse_advance_failed trust_profile_id=%s err=%s",
            trust_profile_id, exc,
            exc_info=True,
        )
        return {
            "suite_id": suite_id,
            "trust_profile_id": trust_profile_id,
            "from_state": trust_state,
            "to_state": trust_state,
            "outcome": "failed",
            "step": "reuse",
            "receipt_id": None,
            "reason_code": "REUSE_ADVANCE_RAISED",
            "latency_seconds": round(time.monotonic() - t_start, 3),
        }

    # If the inner advance reached `number_attached` in this same tick,
    # cut the terminal receipt now (saves one ARQ round-trip).
    if inner_result.get("to_state") == "number_attached":
        # Re-load to pick up the latest SIDs the inner advance just wrote.
        try:
            refreshed = await _load_trust_profile_by_suite(suite_id)
        except BackfillAbortError:
            refreshed = None
        if refreshed is not None:
            try:
                final_receipt = await _cut_backfill_complete_receipt(
                    trust_profile=refreshed,
                    worker_job_id=worker_job_id,
                )
            except TrustReceiptError as exc:
                logger.error(
                    "backfill_sm complete_receipt_failed_inline trust_profile_id=%s err=%s",
                    trust_profile_id, exc,
                )
                final_receipt = ""
            return {
                "suite_id": suite_id,
                "trust_profile_id": trust_profile_id,
                "from_state": trust_state,
                "to_state": "number_attached",
                "outcome": "success",
                "step": "reuse_and_complete",
                "receipt_id": final_receipt or inner_result.get("receipt_id"),
                "latency_seconds": round(time.monotonic() - t_start, 3),
            }

    return {
        "suite_id": suite_id,
        "trust_profile_id": trust_profile_id,
        "from_state": str(inner_result.get("from_state", trust_state)),
        "to_state": str(inner_result.get("to_state", trust_state)),
        "outcome": str(inner_result.get("outcome", "success")),
        "step": "reuse",
        "receipt_id": inner_result.get("receipt_id"),
        "latency_seconds": round(time.monotonic() - t_start, 3),
    }


__all__ = [
    "advance_backfill",
    "BackfillAbortError",
]
