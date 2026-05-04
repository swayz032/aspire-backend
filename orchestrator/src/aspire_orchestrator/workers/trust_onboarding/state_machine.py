"""Trust onboarding state machine — Wave 2-D.

Single entry point: `advance_trust_state(trust_profile_id, *, worker_job_id)`.

Drives each tenant forward by EXACTLY ONE state transition per invocation.
The ARQ worker (worker.py) calls this; the function never retries itself,
never falls back, never makes autonomous routing decisions — it executes the
deterministic next step and returns.

State graph (sequential — one advance per call):

    kyb_collected       -> profile_drafted
    profile_drafted     -> profile_submitted
    profile_submitted   -> HALT (wait for Twilio webhook)
    profile_approved    -> shaken_created
    shaken_created      -> shaken_submitted
    shaken_submitted    -> HALT (wait for Twilio webhook)
    shaken_approved     -> cnam_created
    cnam_created        -> cnam_submitted
    cnam_submitted      -> HALT (wait for Twilio webhook)
    cnam_approved       -> number_attached
    number_attached     -> branded_calling_pending (if flag) OR terminal HALT
    branded_calling_pending -> terminal HALT (W6 scope)
    profile_rejected / failed / unknown -> HALT with outcome="failed"

Idempotency:
    Every Twilio create-call is guarded by a SID-column check.  If the column
    is already populated (e.g. worker crashed after the Twilio call but before
    the DB update), the create is skipped and the worker continues.

PII:
    EIN / DOB / SSN are decrypted from Supabase Vault at call time, passed
    directly to the Twilio client, and never stored in variables that outlive
    the function or appear in receipts / logs.

Vault decryption pattern:
    SELECT decrypted_secret FROM vault.decrypted_secrets WHERE id = $uuid
    via supabase_rpc("get_vault_secret", {"secret_id": uuid}).
    Returns the plaintext value; we pass it to Twilio and immediately discard.

Aspire Laws enforced:
    Law #1  — single brain: no autonomous decisions, no retries, no fallbacks.
    Law #2  — receipts: cut_trust_receipt() called on EVERY transition path.
    Law #3  — fail closed: unknown state → outcome="failed", no Twilio calls.
    Law #7  — tools are hands: pure execution, no planning.
    Law #9  — PII: decrypted secrets never enter receipts/logs/returns.

Author: Aspire — Wave 2-D
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.providers import twilio_trust_hub as thub
from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_rpc,
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
# Internal constants
# ---------------------------------------------------------------------------

# States where the machine HALTs and waits for Twilio's status callback (W5).
# NOTE: "number_attached" is NOT here — it has a real dispatch handler
# (_transition_number_attached) that either halts for branded calling or
# advances to branded_calling_pending. "branded_calling_pending" is included
# because W6 is out of scope; that handler is a stub.
_HALT_STATES: frozenset[str] = frozenset({
    "profile_submitted",
    "shaken_submitted",
    "cnam_submitted",
    "branded_calling_pending",
    "branded_calling_live",
})

# Terminal failure states — machine cannot advance, orchestrator must decide.
_TERMINAL_FAILURE_STATES: frozenset[str] = frozenset({
    "profile_rejected",
    "failed",
    "suspended",
})

# E.164 phone-number prefix regex — for safe redaction in receipts.
_PHONE_PREFIX_RE = re.compile(r"(\+?\d{1,3}\d{3})\d{4,}")


# ---------------------------------------------------------------------------
# Phone redaction helper (mirrors twilio_voice.py:75)
# ---------------------------------------------------------------------------

def _redact_phone(phone: str | None) -> str:
    """Redact all but the first 7 digits of any E.164 phone number.

    Used to build the `caller_id_e164_redacted` field in receipts.
    Returns empty string for None / empty input.
    """
    if not phone:
        return ""
    return _PHONE_PREFIX_RE.sub(r"\1***", phone)


# ---------------------------------------------------------------------------
# Vault secret decryption helper
# ---------------------------------------------------------------------------

async def _decrypt_vault_secret(secret_id: str | None) -> str | None:
    """Decrypt a vault secret by UUID using the service_role view.

    Returns the plaintext secret value, or None if secret_id is None / empty.
    The caller is responsible for using and immediately discarding the value.
    NEVER pass the return value to receipt helpers or log it.
    """
    if not secret_id:
        return None
    try:
        result = await supabase_rpc("get_vault_secret", {"secret_id": str(secret_id)})
        return result.get("decrypted_secret") or result.get("secret")
    except SupabaseClientError as exc:
        logger.error(
            "state_machine vault_decrypt_failed secret_id=%s err=%s",
            secret_id, exc,
        )
        raise


# ---------------------------------------------------------------------------
# Supabase trust profile helpers
# ---------------------------------------------------------------------------

async def _load_trust_profile(trust_profile_id: str) -> dict[str, Any]:
    """Load a tenant_trust_profiles row via service_role (bypasses RLS).

    Raises SupabaseClientError if not found or DB unreachable.
    """
    rows = await supabase_select(
        "tenant_trust_profiles",
        f"id=eq.{trust_profile_id}",
        limit=1,
    )
    if not rows:
        raise SupabaseClientError(
            "select/tenant_trust_profiles",
            status_code=404,
            detail=f"trust_profile_id={trust_profile_id} not found",
        )
    return rows[0]


async def _load_authorized_reps(trust_profile_id: str) -> list[dict[str, Any]]:
    """Load all tenant_authorized_reps for this trust profile, ordered by rep_index."""
    return await supabase_select(
        "tenant_authorized_reps",
        f"trust_profile_id=eq.{trust_profile_id}",
        order_by="rep_index.asc",
    )


async def _load_phone_number(suite_id: str) -> dict[str, Any] | None:
    """Load the active tenant_phone_numbers row for this suite."""
    rows = await supabase_select(
        "tenant_phone_numbers",
        f"suite_id=eq.{suite_id}&status=eq.active",
        limit=1,
    )
    return rows[0] if rows else None


async def _load_suite_email(suite_id: str) -> str:
    """Fetch the owner email from suite_profiles for Twilio resource creation.

    Law #9: this value is NOT logged and NOT placed in receipts.
    """
    rows = await supabase_select(
        "suite_profiles",
        f"suite_id=eq.{suite_id}",
        limit=1,
    )
    if not rows:
        return f"noreply+{suite_id[:8]}@aspireos.app"
    return rows[0].get("email") or f"noreply+{suite_id[:8]}@aspireos.app"


async def _update_trust_profile(
    trust_profile_id: str,
    fields: dict[str, Any],
) -> None:
    """PATCH tenant_trust_profiles with new field values (service_role, bypasses RLS)."""
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    await supabase_update(
        "tenant_trust_profiles",
        f"id=eq.{trust_profile_id}",
        fields,
    )


# ---------------------------------------------------------------------------
# Failure helper
# ---------------------------------------------------------------------------

async def _fail(
    trust_profile: dict[str, Any],
    *,
    from_state: str,
    reason_code: str,
    reason_message: str,
    worker_job_id: str | None,
    twilio_rejection_code: str | None = None,
    twilio_rejection_reason: str | None = None,
    receipt_type: str = "customer_profile_rejected",
) -> dict[str, Any]:
    """Set trust_state='failed', cut a rejection receipt, return outcome='failed'."""
    trust_profile_id = str(trust_profile["id"])
    logger.error(
        "state_machine FAIL trust_profile_id=%s from=%s reason=%s: %s",
        trust_profile_id, from_state, reason_code, reason_message,
    )
    try:
        await _update_trust_profile(trust_profile_id, {
            "trust_state": "failed",
            "rejection_reason": reason_message[:500],
            "rejection_code": reason_code[:100],
        })
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "state_machine fail_update_error trust_profile_id=%s err=%s",
            trust_profile_id, exc,
        )

    receipt_id: str | None = None
    try:
        receipt_id = await cut_trust_receipt(
            receipt_type=receipt_type,
            trust_profile=trust_profile,
            outcome="failed",
            from_state=from_state,
            to_state="failed",
            reason_code=reason_code,
            twilio_rejection_code=twilio_rejection_code,
            twilio_rejection_reason=twilio_rejection_reason,
            worker_job_id=worker_job_id,
            redacted_inputs={"trust_profile_id": trust_profile_id, "step_name": from_state},
            redacted_outputs={},
        )
    except TrustReceiptError as exc:
        logger.error(
            "state_machine receipt_cut_failed_in_fail_path trust_profile_id=%s err=%s",
            trust_profile_id, exc,
        )

    return {
        "trust_profile_id": trust_profile_id,
        "from_state": from_state,
        "to_state": "failed",
        "outcome": "failed",
        "receipt_id": receipt_id,
        "reason_code": reason_code,
    }


# ---------------------------------------------------------------------------
# Per-state transition handlers
# ---------------------------------------------------------------------------

async def _transition_kyb_collected(
    trust_profile: dict[str, Any],
    *,
    worker_job_id: str | None,
) -> dict[str, Any]:
    """kyb_collected → profile_drafted.

    Steps:
    1. Fetch policy SID (cached after startup warm).
    2. If twilio_secondary_profile_sid already set → idempotency skip.
    3. create_secondary_customer_profile().
    4. For each authorized_rep: create_end_user(authorized_rep_{n}).
    5. Store all SIDs into tenant_trust_profiles + tenant_authorized_reps.
    6. Update trust_state to profile_drafted.
    7. Cut customer_profile_created receipt.
    """
    trust_profile_id = str(trust_profile["id"])
    suite_id = str(trust_profile["suite_id"])
    from_state = "kyb_collected"
    t_start = time.monotonic()

    # -- Step 1: policy SID --
    try:
        policy_sid = await thub.fetch_secondary_profile_policy_sid()
    except TrustHubError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="POLICY_SID_FETCH_FAILED", reason_message=str(exc),
            worker_job_id=worker_job_id,
        )

    # -- Step 2: Idempotency guard --
    profile_sid: str | None = trust_profile.get("twilio_secondary_profile_sid")

    if not profile_sid:
        # Law #9: email fetched server-side, never logged, never in receipts.
        email = await _load_suite_email(suite_id)
        legal_name = trust_profile.get("legal_business_name", "")
        idem_key = f"create-secondary-profile-{trust_profile_id}"
        try:
            result = await thub.create_secondary_customer_profile(
                suite_id=suite_id,
                legal_name=legal_name,
                email=email,
                policy_sid=policy_sid,
                idempotency_key=idem_key,
            )
            profile_sid = result.get("sid", "")
        except TrustHubError as exc:
            return await _fail(
                trust_profile, from_state=from_state,
                reason_code="CREATE_PROFILE_FAILED", reason_message=str(exc),
                worker_job_id=worker_job_id,
            )

    # -- Step 3: Create authorized_rep EndUsers --
    reps = await _load_authorized_reps(trust_profile_id)
    for rep in reps:
        rep_id = str(rep["id"])
        rep_index = int(rep.get("rep_index", 1))

        # Idempotency: skip if SID already stored.
        if rep.get("twilio_end_user_sid"):
            continue

        # Decrypt DOB from vault — never logged, never in receipts.
        dob_vault_id = rep.get("dob_vault_secret_id")
        dob_plain: str | None = None
        if dob_vault_id:
            try:
                dob_plain = await _decrypt_vault_secret(str(dob_vault_id))
            except SupabaseClientError as exc:
                return await _fail(
                    trust_profile, from_state=from_state,
                    reason_code="VAULT_DECRYPT_FAILED", reason_message=str(exc),
                    worker_job_id=worker_job_id,
                )

        # Decrypt SSN last-4 if present.
        ssn_vault_id = rep.get("ssn_last4_vault_secret_id")
        ssn_plain: str | None = None
        if ssn_vault_id:
            try:
                ssn_plain = await _decrypt_vault_secret(str(ssn_vault_id))
            except SupabaseClientError as exc:
                return await _fail(
                    trust_profile, from_state=from_state,
                    reason_code="VAULT_DECRYPT_FAILED", reason_message=str(exc),
                    worker_job_id=worker_job_id,
                )

        # Build attributes — PII values used here and discarded immediately.
        # Law #9: these attributes are NOT logged (provider client enforces this).
        attributes: dict[str, Any] = {
            "first_name": rep.get("first_name", ""),
            "last_name": rep.get("last_name", ""),
            "business_title": rep.get("business_title", ""),
            "email": rep.get("email", ""),
            "phone_number": rep.get("phone_e164", ""),
        }
        if dob_plain:
            attributes["dob"] = dob_plain
        if ssn_plain:
            attributes["ssn_last_4"] = ssn_plain

        # Discard plaintext immediately after building attributes dict.
        dob_plain = None
        ssn_plain = None

        eu_type = f"authorized_representative_{rep_index}"
        idem_key = f"create-end-user-{trust_profile_id}-rep{rep_index}"
        try:
            eu_result = await thub.create_end_user(
                profile_sid=profile_sid or "",
                end_user_type=eu_type,
                attributes=attributes,
                friendly_name=f"Aspire-rep{rep_index}-{suite_id[:8]}",
                idempotency_key=idem_key,
            )
        except TrustHubError as exc:
            return await _fail(
                trust_profile, from_state=from_state,
                reason_code="CREATE_END_USER_FAILED", reason_message=str(exc),
                worker_job_id=worker_job_id,
            )

        # Discard attributes dict containing PII.
        attributes.clear()

        # Store end_user_sid.
        rep_eu_sid = eu_result.get("sid", "")
        try:
            await supabase_update(
                "tenant_authorized_reps",
                f"id=eq.{rep_id}",
                {"twilio_end_user_sid": rep_eu_sid},
            )
        except SupabaseClientError as exc:
            return await _fail(
                trust_profile, from_state=from_state,
                reason_code="DB_UPDATE_FAILED", reason_message=str(exc),
                worker_job_id=worker_job_id,
            )

    # -- Step 4: Store profile SID + advance state --
    try:
        await _update_trust_profile(trust_profile_id, {
            "trust_state": "profile_drafted",
            "twilio_secondary_profile_sid": profile_sid,
        })
    except SupabaseClientError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="DB_UPDATE_FAILED", reason_message=str(exc),
            worker_job_id=worker_job_id,
        )

    latency = time.monotonic() - t_start
    receipt_id = await cut_trust_receipt(
        receipt_type="customer_profile_created",
        trust_profile=trust_profile,
        outcome="success",
        from_state=from_state,
        to_state="profile_drafted",
        twilio_resource_sid=profile_sid,
        twilio_status="draft",
        worker_job_id=worker_job_id,
        redacted_inputs={"trust_profile_id": trust_profile_id, "step_name": from_state},
        redacted_outputs={
            "twilio_resource_sid": profile_sid or "",
            "twilio_status": "draft",
            "latency_seconds": round(latency, 3),
        },
    )
    return {
        "trust_profile_id": trust_profile_id,
        "from_state": from_state,
        "to_state": "profile_drafted",
        "outcome": "success",
        "receipt_id": receipt_id,
    }


async def _transition_profile_drafted(
    trust_profile: dict[str, Any],
    *,
    worker_job_id: str | None,
) -> dict[str, Any]:
    """profile_drafted → profile_submitted.

    Steps:
    1. For each rep: assign_entity_to_profile (EntityAssignment).
    2. submit_customer_profile (Status=pending-review).
    3. Update trust_state to profile_submitted.
    4. Cut customer_profile_submitted receipt.
    """
    trust_profile_id = str(trust_profile["id"])
    suite_id = str(trust_profile["suite_id"])
    from_state = "profile_drafted"
    profile_sid = trust_profile.get("twilio_secondary_profile_sid", "")

    if not profile_sid:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="MISSING_PROFILE_SID",
            reason_message="twilio_secondary_profile_sid is null at profile_drafted stage",
            worker_job_id=worker_job_id,
        )

    t_start = time.monotonic()

    # Assign all authorized reps to the Secondary Customer Profile.
    reps = await _load_authorized_reps(trust_profile_id)
    for rep in reps:
        rep_index = int(rep.get("rep_index", 1))
        eu_sid = rep.get("twilio_end_user_sid")
        if not eu_sid:
            return await _fail(
                trust_profile, from_state=from_state,
                reason_code="MISSING_END_USER_SID",
                reason_message=f"rep {rep_index} has no twilio_end_user_sid; run kyb_collected first",
                worker_job_id=worker_job_id,
            )
        idem_key = f"assign-entity-profile-{trust_profile_id}-rep{rep_index}"
        try:
            await thub.assign_entity_to_profile(
                profile_sid, eu_sid, idempotency_key=idem_key,
            )
        except TrustHubError as exc:
            # 409 Conflict = already assigned — treat as idempotent success.
            if exc.status_code == 409:
                logger.info(
                    "state_machine assign_entity_to_profile 409-conflict (already assigned) "
                    "profile=%s entity=%s — skipping",
                    profile_sid, eu_sid,
                )
            else:
                return await _fail(
                    trust_profile, from_state=from_state,
                    reason_code="ASSIGN_ENTITY_FAILED", reason_message=str(exc),
                    worker_job_id=worker_job_id,
                )

    # Submit the profile for Twilio review.
    idem_key = f"submit-profile-{trust_profile_id}"
    try:
        sub_result = await thub.submit_customer_profile(
            profile_sid, idempotency_key=idem_key,
        )
    except TrustHubError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="SUBMIT_PROFILE_FAILED", reason_message=str(exc),
            worker_job_id=worker_job_id,
        )

    twilio_status = sub_result.get("status", "pending-review")

    try:
        await _update_trust_profile(trust_profile_id, {"trust_state": "profile_submitted"})
    except SupabaseClientError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="DB_UPDATE_FAILED", reason_message=str(exc),
            worker_job_id=worker_job_id,
        )

    latency = time.monotonic() - t_start
    receipt_id = await cut_trust_receipt(
        receipt_type="customer_profile_submitted",
        trust_profile=trust_profile,
        outcome="success",
        from_state=from_state,
        to_state="profile_submitted",
        twilio_resource_sid=profile_sid,
        twilio_status=twilio_status,
        worker_job_id=worker_job_id,
        redacted_inputs={
            "trust_profile_id": trust_profile_id,
            "step_name": from_state,
            "bundle_sid": profile_sid,
        },
        redacted_outputs={
            "twilio_resource_sid": profile_sid,
            "twilio_status": twilio_status,
            "latency_seconds": round(latency, 3),
        },
    )
    return {
        "trust_profile_id": trust_profile_id,
        "from_state": from_state,
        "to_state": "profile_submitted",
        "outcome": "success",
        "receipt_id": receipt_id,
    }


async def _transition_profile_approved(
    trust_profile: dict[str, Any],
    *,
    worker_job_id: str | None,
) -> dict[str, Any]:
    """profile_approved → shaken_created.

    Steps:
    1. Fetch SHAKEN policy SID.
    2. Idempotency: skip if shaken_bundle_sid already set.
    3. create_trust_product(SHAKEN policy SID).
    4. assign_entity_to_trust_product(shaken, secondary_profile_sid).
    5. Load phone number SID; add_phone_to_trust_product(shaken, number_sid).
    6. Store SIDs + advance state.
    7. Cut shaken_trust_product_created receipt.
    """
    trust_profile_id = str(trust_profile["id"])
    suite_id = str(trust_profile["suite_id"])
    from_state = "profile_approved"
    profile_sid = trust_profile.get("twilio_secondary_profile_sid", "")
    t_start = time.monotonic()

    # Fetch SHAKEN policy SID.
    try:
        shaken_policy_sid = await thub.fetch_shaken_policy_sid()
    except TrustHubError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="POLICY_SID_FETCH_FAILED", reason_message=str(exc),
            worker_job_id=worker_job_id,
            receipt_type="shaken_trust_product_rejected",
        )

    # Idempotency guard.
    shaken_sid: str | None = trust_profile.get("twilio_shaken_bundle_sid")

    if not shaken_sid:
        # Law #9: email not logged.
        email = await _load_suite_email(suite_id)
        legal_name = trust_profile.get("legal_business_name", "")
        idem_key = f"create-shaken-{trust_profile_id}"
        try:
            result = await thub.create_trust_product(
                friendly_name=f"Aspire-SHAKEN-{suite_id[:8]}-{legal_name[:30]}",
                email=email,
                policy_sid=shaken_policy_sid,
                idempotency_key=idem_key,
            )
            shaken_sid = result.get("sid", "")
        except TrustHubError as exc:
            return await _fail(
                trust_profile, from_state=from_state,
                reason_code="CREATE_SHAKEN_FAILED", reason_message=str(exc),
                worker_job_id=worker_job_id,
                receipt_type="shaken_trust_product_rejected",
            )

    # Assign secondary profile to SHAKEN bundle.
    if profile_sid:
        idem_key = f"assign-profile-to-shaken-{trust_profile_id}"
        try:
            await thub.assign_entity_to_trust_product(
                shaken_sid, profile_sid, idempotency_key=idem_key,
            )
        except TrustHubError as exc:
            if exc.status_code != 409:
                return await _fail(
                    trust_profile, from_state=from_state,
                    reason_code="ASSIGN_PROFILE_TO_SHAKEN_FAILED", reason_message=str(exc),
                    worker_job_id=worker_job_id,
                    receipt_type="shaken_trust_product_rejected",
                )

    # Attach the tenant's phone number.
    phone_row = await _load_phone_number(suite_id)
    number_sid: str | None = None
    if phone_row:
        number_sid = phone_row.get("twilio_sid") or phone_row.get("phone_sid")
        if number_sid:
            idem_key = f"add-phone-to-shaken-{trust_profile_id}"
            try:
                await thub.add_phone_to_trust_product(
                    shaken_sid, number_sid, idempotency_key=idem_key,
                )
            except TrustHubError as exc:
                if exc.status_code != 409:
                    return await _fail(
                        trust_profile, from_state=from_state,
                        reason_code="ADD_PHONE_TO_SHAKEN_FAILED", reason_message=str(exc),
                        worker_job_id=worker_job_id,
                        receipt_type="shaken_trust_product_rejected",
                    )

    # Persist SID + advance state.
    try:
        await _update_trust_profile(trust_profile_id, {
            "trust_state": "shaken_created",
            "twilio_shaken_bundle_sid": shaken_sid,
        })
    except SupabaseClientError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="DB_UPDATE_FAILED", reason_message=str(exc),
            worker_job_id=worker_job_id,
            receipt_type="shaken_trust_product_rejected",
        )

    latency = time.monotonic() - t_start
    receipt_id = await cut_trust_receipt(
        receipt_type="shaken_trust_product_created",
        trust_profile=trust_profile,
        outcome="success",
        from_state=from_state,
        to_state="shaken_created",
        twilio_resource_sid=shaken_sid,
        twilio_status="draft",
        worker_job_id=worker_job_id,
        redacted_inputs={"trust_profile_id": trust_profile_id, "step_name": from_state},
        redacted_outputs={
            "twilio_resource_sid": shaken_sid or "",
            "twilio_status": "draft",
            "latency_seconds": round(latency, 3),
        },
    )
    return {
        "trust_profile_id": trust_profile_id,
        "from_state": from_state,
        "to_state": "shaken_created",
        "outcome": "success",
        "receipt_id": receipt_id,
    }


async def _transition_shaken_created(
    trust_profile: dict[str, Any],
    *,
    worker_job_id: str | None,
) -> dict[str, Any]:
    """shaken_created → shaken_submitted.

    Steps:
    1. submit_trust_product(shaken_bundle_sid).
    2. Update trust_state to shaken_submitted.
    3. Cut shaken_trust_product_created receipt (submission variant).
    """
    trust_profile_id = str(trust_profile["id"])
    from_state = "shaken_created"
    shaken_sid = trust_profile.get("twilio_shaken_bundle_sid", "")
    t_start = time.monotonic()

    if not shaken_sid:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="MISSING_SHAKEN_SID",
            reason_message="twilio_shaken_bundle_sid is null at shaken_created stage",
            worker_job_id=worker_job_id,
            receipt_type="shaken_trust_product_rejected",
        )

    idem_key = f"submit-shaken-{trust_profile_id}"
    try:
        sub_result = await thub.submit_trust_product(
            shaken_sid, idempotency_key=idem_key,
        )
    except TrustHubError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="SUBMIT_SHAKEN_FAILED", reason_message=str(exc),
            worker_job_id=worker_job_id,
            receipt_type="shaken_trust_product_rejected",
        )

    twilio_status = sub_result.get("status", "pending-review")

    try:
        await _update_trust_profile(trust_profile_id, {"trust_state": "shaken_submitted"})
    except SupabaseClientError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="DB_UPDATE_FAILED", reason_message=str(exc),
            worker_job_id=worker_job_id,
            receipt_type="shaken_trust_product_rejected",
        )

    latency = time.monotonic() - t_start
    receipt_id = await cut_trust_receipt(
        receipt_type="shaken_trust_product_created",
        trust_profile=trust_profile,
        outcome="success",
        from_state=from_state,
        to_state="shaken_submitted",
        twilio_resource_sid=shaken_sid,
        twilio_status=twilio_status,
        worker_job_id=worker_job_id,
        redacted_inputs={
            "trust_profile_id": trust_profile_id,
            "step_name": from_state,
            "bundle_sid": shaken_sid,
        },
        redacted_outputs={
            "twilio_resource_sid": shaken_sid,
            "twilio_status": twilio_status,
            "latency_seconds": round(latency, 3),
        },
    )
    return {
        "trust_profile_id": trust_profile_id,
        "from_state": from_state,
        "to_state": "shaken_submitted",
        "outcome": "success",
        "receipt_id": receipt_id,
    }


async def _transition_shaken_approved(
    trust_profile: dict[str, Any],
    *,
    worker_job_id: str | None,
) -> dict[str, Any]:
    """shaken_approved → cnam_created.

    Steps (CNAM 8-step recipe, steps 1–4):
    1. Fetch CNAM policy SID.
    2. Idempotency: skip create if cnam_bundle_sid already set.
    3. create_trust_product(CNAM policy SID).
    4. assign_entity_to_trust_product(cnam_bundle, secondary_profile_sid).
    5. Sanitize business name → CNAM display name.
    6. create_end_user(cnam_information, {cnam_display_name: ...}).
    7. assign_entity_to_trust_product(cnam_bundle, cnam_end_user_sid).
    8. Store SIDs, advance state, cut cnam_trust_product_created + cnam_display_name_set receipts.
    """
    trust_profile_id = str(trust_profile["id"])
    suite_id = str(trust_profile["suite_id"])
    from_state = "shaken_approved"
    profile_sid = trust_profile.get("twilio_secondary_profile_sid", "")
    t_start = time.monotonic()

    # Fetch CNAM policy SID.
    try:
        cnam_policy_sid = await thub.fetch_cnam_policy_sid()
    except TrustHubError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="POLICY_SID_FETCH_FAILED", reason_message=str(exc),
            worker_job_id=worker_job_id,
            receipt_type="cnam_trust_product_rejected",
        )

    # Idempotency guard on CNAM bundle creation.
    cnam_sid: str | None = trust_profile.get("twilio_cnam_bundle_sid")

    if not cnam_sid:
        email = await _load_suite_email(suite_id)
        legal_name = trust_profile.get("legal_business_name", "")
        idem_key = f"create-cnam-{trust_profile_id}"
        try:
            result = await thub.create_trust_product(
                friendly_name=f"Aspire-CNAM-{suite_id[:8]}-{legal_name[:30]}",
                email=email,
                policy_sid=cnam_policy_sid,
                idempotency_key=idem_key,
            )
            cnam_sid = result.get("sid", "")
        except TrustHubError as exc:
            return await _fail(
                trust_profile, from_state=from_state,
                reason_code="CREATE_CNAM_FAILED", reason_message=str(exc),
                worker_job_id=worker_job_id,
                receipt_type="cnam_trust_product_rejected",
            )

    # Assign secondary profile to CNAM bundle.
    if profile_sid and cnam_sid:
        idem_key = f"assign-profile-to-cnam-{trust_profile_id}"
        try:
            await thub.assign_entity_to_trust_product(
                cnam_sid, profile_sid, idempotency_key=idem_key,
            )
        except TrustHubError as exc:
            if exc.status_code != 409:
                return await _fail(
                    trust_profile, from_state=from_state,
                    reason_code="ASSIGN_PROFILE_TO_CNAM_FAILED", reason_message=str(exc),
                    worker_job_id=worker_job_id,
                    receipt_type="cnam_trust_product_rejected",
                )

    # Derive CNAM display name from business_name.
    raw_business_name = trust_profile.get("legal_business_name", "")
    try:
        cnam_display_name = sanitize_cnam_display_name(raw_business_name)
    except ValueError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="CNAM_DISPLAY_NAME_INVALID", reason_message=str(exc),
            worker_job_id=worker_job_id,
            receipt_type="cnam_trust_product_rejected",
        )

    # Create CNAM EndUser — idempotency: check if cnam_records table has a sid.
    cnam_rows = await supabase_select(
        "tenant_cnam_records",
        f"trust_profile_id=eq.{trust_profile_id}",
        limit=1,
    )
    cnam_eu_sid: str | None = cnam_rows[0].get("twilio_cnam_end_user_sid") if cnam_rows else None

    if not cnam_eu_sid:
        idem_key = f"create-cnam-end-user-{trust_profile_id}"
        try:
            eu_result = await thub.create_end_user(
                profile_sid=cnam_sid or "",
                end_user_type="cnam_information",
                attributes={"cnam_display_name": cnam_display_name},
                friendly_name=f"Aspire-CNAM-EU-{suite_id[:8]}",
                idempotency_key=idem_key,
            )
            cnam_eu_sid = eu_result.get("sid", "")
        except TrustHubError as exc:
            return await _fail(
                trust_profile, from_state=from_state,
                reason_code="CREATE_CNAM_END_USER_FAILED", reason_message=str(exc),
                worker_job_id=worker_job_id,
                receipt_type="cnam_trust_product_rejected",
            )

    # Assign CNAM EndUser to CNAM bundle.
    if cnam_eu_sid and cnam_sid:
        idem_key = f"assign-cnam-eu-to-cnam-{trust_profile_id}"
        try:
            await thub.assign_entity_to_trust_product(
                cnam_sid, cnam_eu_sid, idempotency_key=idem_key,
            )
        except TrustHubError as exc:
            if exc.status_code != 409:
                return await _fail(
                    trust_profile, from_state=from_state,
                    reason_code="ASSIGN_CNAM_EU_FAILED", reason_message=str(exc),
                    worker_job_id=worker_job_id,
                    receipt_type="cnam_trust_product_rejected",
                )

    # Upsert the CNAM record.
    if cnam_rows:
        # Update existing row.
        try:
            await supabase_update(
                "tenant_cnam_records",
                f"trust_profile_id=eq.{trust_profile_id}",
                {
                    "twilio_cnam_bundle_sid": cnam_sid,
                    "twilio_cnam_end_user_sid": cnam_eu_sid,
                    "cnam_display_name": cnam_display_name,
                },
            )
        except SupabaseClientError as exc:
            return await _fail(
                trust_profile, from_state=from_state,
                reason_code="DB_UPDATE_FAILED", reason_message=str(exc),
                worker_job_id=worker_job_id,
                receipt_type="cnam_trust_product_rejected",
            )
    else:
        # Insert new CNAM record row.
        from aspire_orchestrator.services.supabase_client import supabase_insert
        try:
            await supabase_insert("tenant_cnam_records", {
                "trust_profile_id": trust_profile_id,
                "suite_id": suite_id,
                "tenant_id": str(trust_profile.get("tenant_id", "")),
                "twilio_cnam_bundle_sid": cnam_sid,
                "twilio_cnam_end_user_sid": cnam_eu_sid,
                "cnam_display_name": cnam_display_name,
            })
        except SupabaseClientError as exc:
            return await _fail(
                trust_profile, from_state=from_state,
                reason_code="DB_INSERT_FAILED", reason_message=str(exc),
                worker_job_id=worker_job_id,
                receipt_type="cnam_trust_product_rejected",
            )

    # Advance trust_state.
    try:
        await _update_trust_profile(trust_profile_id, {
            "trust_state": "cnam_created",
            "twilio_cnam_bundle_sid": cnam_sid,
        })
    except SupabaseClientError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="DB_UPDATE_FAILED", reason_message=str(exc),
            worker_job_id=worker_job_id,
            receipt_type="cnam_trust_product_rejected",
        )

    latency = time.monotonic() - t_start

    # Cut two receipts: one for the CNAM Trust Product creation, one for the display name.
    receipt_id = await cut_trust_receipt(
        receipt_type="cnam_trust_product_created",
        trust_profile=trust_profile,
        outcome="success",
        from_state=from_state,
        to_state="cnam_created",
        twilio_resource_sid=cnam_sid,
        twilio_status="draft",
        worker_job_id=worker_job_id,
        redacted_inputs={"trust_profile_id": trust_profile_id, "step_name": from_state},
        redacted_outputs={
            "twilio_resource_sid": cnam_sid or "",
            "end_user_sid": cnam_eu_sid or "",
            "cnam_display_name": cnam_display_name,
            "twilio_status": "draft",
            "latency_seconds": round(latency, 3),
        },
    )
    # CNAM display name set — receipt records the sanitized (public-facing) name only.
    await cut_trust_receipt(
        receipt_type="cnam_display_name_set",
        trust_profile=trust_profile,
        outcome="success",
        from_state=from_state,
        to_state="cnam_created",
        twilio_resource_sid=cnam_eu_sid,
        worker_job_id=worker_job_id,
        redacted_inputs={"trust_profile_id": trust_profile_id, "step_name": "cnam_display_name"},
        redacted_outputs={
            "end_user_sid": cnam_eu_sid or "",
            "cnam_display_name": cnam_display_name,
        },
    )

    return {
        "trust_profile_id": trust_profile_id,
        "from_state": from_state,
        "to_state": "cnam_created",
        "outcome": "success",
        "receipt_id": receipt_id,
    }


async def _transition_cnam_created(
    trust_profile: dict[str, Any],
    *,
    worker_job_id: str | None,
) -> dict[str, Any]:
    """cnam_created → cnam_submitted.

    Steps (CNAM 8-step recipe, steps 5–6):
    1. Load CNAM record (cnam_bundle_sid, channel_endpoint_sid).
    2. Idempotency: skip add_phone if channel_endpoint_sid already set.
    3. add_phone_to_trust_product(cnam_bundle, number_sid).
    4. Store channel_endpoint_sid.
    5. submit_trust_product(cnam_bundle).
    6. Advance state to cnam_submitted.
    7. Cut receipt.
    """
    trust_profile_id = str(trust_profile["id"])
    suite_id = str(trust_profile["suite_id"])
    from_state = "cnam_created"
    cnam_sid = trust_profile.get("twilio_cnam_bundle_sid", "")
    t_start = time.monotonic()

    if not cnam_sid:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="MISSING_CNAM_SID",
            reason_message="twilio_cnam_bundle_sid is null at cnam_created stage",
            worker_job_id=worker_job_id,
            receipt_type="cnam_trust_product_rejected",
        )

    # Load cnam_records for idempotency.
    cnam_rows = await supabase_select(
        "tenant_cnam_records",
        f"trust_profile_id=eq.{trust_profile_id}",
        limit=1,
    )
    channel_endpoint_sid: str | None = (
        cnam_rows[0].get("twilio_cnam_channel_endpoint_sid") if cnam_rows else None
    )

    if not channel_endpoint_sid:
        phone_row = await _load_phone_number(suite_id)
        if not phone_row:
            return await _fail(
                trust_profile, from_state=from_state,
                reason_code="NO_ACTIVE_PHONE_NUMBER",
                reason_message="No active phone number found for suite; cannot attach to CNAM bundle",
                worker_job_id=worker_job_id,
                receipt_type="cnam_trust_product_rejected",
            )
        number_sid = phone_row.get("twilio_sid") or phone_row.get("phone_sid", "")
        phone_e164: str = phone_row.get("phone_number") or phone_row.get("e164") or ""
        idem_key = f"add-phone-to-cnam-{trust_profile_id}"
        try:
            cea_result = await thub.add_phone_to_trust_product(
                cnam_sid, number_sid, idempotency_key=idem_key,
            )
            channel_endpoint_sid = cea_result.get("sid", "")
        except TrustHubError as exc:
            if exc.status_code == 409:
                channel_endpoint_sid = ""  # Already attached; proceed.
            else:
                return await _fail(
                    trust_profile, from_state=from_state,
                    reason_code="ADD_PHONE_TO_CNAM_FAILED", reason_message=str(exc),
                    worker_job_id=worker_job_id,
                    receipt_type="cnam_trust_product_rejected",
                )

        # Store channel endpoint SID.
        if cnam_rows and channel_endpoint_sid:
            try:
                await supabase_update(
                    "tenant_cnam_records",
                    f"trust_profile_id=eq.{trust_profile_id}",
                    {"twilio_cnam_channel_endpoint_sid": channel_endpoint_sid},
                )
            except SupabaseClientError as exc:
                return await _fail(
                    trust_profile, from_state=from_state,
                    reason_code="DB_UPDATE_FAILED", reason_message=str(exc),
                    worker_job_id=worker_job_id,
                    receipt_type="cnam_trust_product_rejected",
                )
    else:
        phone_e164 = ""
        number_sid = ""

    # Submit CNAM Trust Product for review.
    idem_key = f"submit-cnam-{trust_profile_id}"
    try:
        sub_result = await thub.submit_trust_product(
            cnam_sid, idempotency_key=idem_key,
        )
    except TrustHubError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="SUBMIT_CNAM_FAILED", reason_message=str(exc),
            worker_job_id=worker_job_id,
            receipt_type="cnam_trust_product_rejected",
        )

    twilio_status = sub_result.get("status", "pending-review")

    try:
        await _update_trust_profile(trust_profile_id, {"trust_state": "cnam_submitted"})
    except SupabaseClientError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="DB_UPDATE_FAILED", reason_message=str(exc),
            worker_job_id=worker_job_id,
            receipt_type="cnam_trust_product_rejected",
        )

    latency = time.monotonic() - t_start
    receipt_id = await cut_trust_receipt(
        receipt_type="cnam_trust_product_created",
        trust_profile=trust_profile,
        outcome="success",
        from_state=from_state,
        to_state="cnam_submitted",
        twilio_resource_sid=cnam_sid,
        twilio_status=twilio_status,
        worker_job_id=worker_job_id,
        redacted_inputs={
            "trust_profile_id": trust_profile_id,
            "step_name": from_state,
            "bundle_sid": cnam_sid,
        },
        redacted_outputs={
            "twilio_resource_sid": cnam_sid,
            "channel_endpoint_sid": channel_endpoint_sid or "",
            "twilio_status": twilio_status,
            "latency_seconds": round(latency, 3),
        },
    )
    return {
        "trust_profile_id": trust_profile_id,
        "from_state": from_state,
        "to_state": "cnam_submitted",
        "outcome": "success",
        "receipt_id": receipt_id,
    }


async def _transition_cnam_approved(
    trust_profile: dict[str, Any],
    *,
    worker_job_id: str | None,
) -> dict[str, Any]:
    """cnam_approved → number_attached.

    Steps (CNAM 8-step recipe, step 7):
    1. assign_number_to_profile(secondary_profile_sid, number_sid).
    2. enable_caller_id_lookup(number_sid).
    3. Advance state to number_attached.
    4. Cut number_attached_to_profile + caller_id_lookup_enabled receipts.
    """
    trust_profile_id = str(trust_profile["id"])
    suite_id = str(trust_profile["suite_id"])
    from_state = "cnam_approved"
    profile_sid = trust_profile.get("twilio_secondary_profile_sid", "")
    t_start = time.monotonic()

    phone_row = await _load_phone_number(suite_id)
    if not phone_row:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="NO_ACTIVE_PHONE_NUMBER",
            reason_message="No active phone number found for suite; cannot attach to Customer Profile",
            worker_job_id=worker_job_id,
        )

    number_sid = phone_row.get("twilio_sid") or phone_row.get("phone_sid", "")
    phone_e164: str = phone_row.get("phone_number") or phone_row.get("e164") or ""

    # Assign phone number to Secondary Customer Profile.
    if profile_sid and number_sid:
        idem_key = f"assign-number-to-profile-{trust_profile_id}"
        try:
            await thub.assign_number_to_profile(
                profile_sid, number_sid, idempotency_key=idem_key,
            )
        except TrustHubError as exc:
            if exc.status_code != 409:
                return await _fail(
                    trust_profile, from_state=from_state,
                    reason_code="ASSIGN_NUMBER_FAILED", reason_message=str(exc),
                    worker_job_id=worker_job_id,
                )

    # Enable VoiceCallerIdLookup.
    if number_sid:
        idem_key = f"enable-cid-lookup-{trust_profile_id}"
        try:
            await thub.enable_caller_id_lookup(
                number_sid, idempotency_key=idem_key,
            )
        except TrustHubError as exc:
            return await _fail(
                trust_profile, from_state=from_state,
                reason_code="ENABLE_CALLER_ID_LOOKUP_FAILED", reason_message=str(exc),
                worker_job_id=worker_job_id,
            )

    try:
        await _update_trust_profile(trust_profile_id, {
            "trust_state": "number_attached",
            "cnam_approved_at": datetime.now(timezone.utc).isoformat(),
        })
    except SupabaseClientError as exc:
        return await _fail(
            trust_profile, from_state=from_state,
            reason_code="DB_UPDATE_FAILED", reason_message=str(exc),
            worker_job_id=worker_job_id,
        )

    latency = time.monotonic() - t_start

    # Redact the E.164 number for the receipt — never the raw number.
    caller_id_redacted = _redact_phone(phone_e164)

    receipt_id = await cut_trust_receipt(
        receipt_type="number_attached_to_profile",
        trust_profile=trust_profile,
        outcome="success",
        from_state=from_state,
        to_state="number_attached",
        twilio_resource_sid=profile_sid,
        twilio_status="twilio-approved",
        worker_job_id=worker_job_id,
        redacted_inputs={"trust_profile_id": trust_profile_id, "step_name": from_state},
        redacted_outputs={
            "twilio_resource_sid": profile_sid or "",
            "bundle_sid": profile_sid or "",
            "caller_id_e164_redacted": caller_id_redacted,
            "latency_seconds": round(latency, 3),
        },
    )
    await cut_trust_receipt(
        receipt_type="caller_id_lookup_enabled",
        trust_profile=trust_profile,
        outcome="success",
        from_state=from_state,
        to_state="number_attached",
        twilio_resource_sid=number_sid,
        worker_job_id=worker_job_id,
        redacted_inputs={"trust_profile_id": trust_profile_id, "step_name": "enable_cid_lookup"},
        redacted_outputs={
            "twilio_resource_sid": number_sid or "",
            "caller_id_e164_redacted": caller_id_redacted,
        },
    )

    return {
        "trust_profile_id": trust_profile_id,
        "from_state": from_state,
        "to_state": "number_attached",
        "outcome": "success",
        "receipt_id": receipt_id,
    }


async def _transition_number_attached(
    trust_profile: dict[str, Any],
    *,
    worker_job_id: str | None,
) -> dict[str, Any]:
    """number_attached → branded_calling_pending (flag) OR terminal HALT.

    W6 scope: if BRANDED_CALLING_ENABLED=false (default), log and halt.
    The W6 author enables this branch; state machine returns outcome="halted".
    """
    trust_profile_id = str(trust_profile["id"])
    from_state = "number_attached"

    if not settings.branded_calling_enabled:
        logger.info(
            "state_machine branded_calling_disabled trust_profile_id=%s — "
            "BRANDED_CALLING_ENABLED is false; halting at number_attached (W6 scope)",
            trust_profile_id,
        )
        # This is a normal terminal halt — no failure, no receipt needed.
        # The tenant's CNAM is live; this is the successful end-state until W6.
        return {
            "trust_profile_id": trust_profile_id,
            "from_state": from_state,
            "to_state": from_state,  # stays in number_attached
            "outcome": "halted",
            "receipt_id": None,
        }

    # W6 stub — when flag is enabled, advance to branded_calling_pending.
    # The actual Branded Calling enrollment API call is W6 scope.
    logger.info(
        "state_machine branded_calling_pending trust_profile_id=%s — "
        "BRANDED_CALLING_ENABLED=true but enrollment is W6 scope (not yet implemented); halting",
        trust_profile_id,
    )
    try:
        await _update_trust_profile(trust_profile_id, {
            "trust_state": "branded_calling_pending",
        })
    except SupabaseClientError:
        pass  # Non-fatal — the W6 author will complete this.

    return {
        "trust_profile_id": trust_profile_id,
        "from_state": from_state,
        "to_state": "branded_calling_pending",
        "outcome": "halted",
        "receipt_id": None,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def advance_trust_state(
    trust_profile_id: str,
    *,
    worker_job_id: str | None = None,
) -> dict[str, Any]:
    """Advance a tenant's trust onboarding by EXACTLY ONE state transition.

    This is the public entry point called by the ARQ worker (worker.py).

    Args:
        trust_profile_id: UUID of the tenant_trust_profiles row.
        worker_job_id: ARQ job ID for traceability (optional).

    Returns:
        {
            "trust_profile_id": str,
            "from_state": str,
            "to_state": str,
            "outcome": "success" | "halted" | "failed",
            "receipt_id": str | None,
        }

    Never raises to caller — all errors are mapped to outcome="failed" with
    a receipt. The ARQ worker logs the result; the orchestrator decides retries.
    """
    logger.info(
        "state_machine advance_trust_state trust_profile_id=%s job_id=%s",
        trust_profile_id, worker_job_id,
    )

    # -- Load trust profile (service_role, bypasses RLS) --
    try:
        trust_profile = await _load_trust_profile(trust_profile_id)
    except SupabaseClientError as exc:
        logger.error(
            "state_machine load_trust_profile_failed trust_profile_id=%s err=%s",
            trust_profile_id, exc,
        )
        # Cannot cut a proper receipt without the profile (missing scope IDs).
        return {
            "trust_profile_id": trust_profile_id,
            "from_state": "unknown",
            "to_state": "unknown",
            "outcome": "failed",
            "receipt_id": None,
            "reason_code": "PROFILE_LOAD_FAILED",
        }

    trust_state: str = trust_profile.get("trust_state", "")

    # -- Halt states: normal pause, waiting for Twilio status callback (W5) --
    if trust_state in _HALT_STATES:
        logger.info(
            "state_machine halted trust_profile_id=%s state=%s (awaiting Twilio callback)",
            trust_profile_id, trust_state,
        )
        return {
            "trust_profile_id": trust_profile_id,
            "from_state": trust_state,
            "to_state": trust_state,
            "outcome": "halted",
            "receipt_id": None,
        }

    # -- Terminal failure states --
    if trust_state in _TERMINAL_FAILURE_STATES:
        logger.warning(
            "state_machine terminal_state trust_profile_id=%s state=%s — "
            "orchestrator must decide retry/dispute",
            trust_profile_id, trust_state,
        )
        return {
            "trust_profile_id": trust_profile_id,
            "from_state": trust_state,
            "to_state": trust_state,
            "outcome": "failed",
            "receipt_id": None,
            "reason_code": "TERMINAL_FAILURE_STATE",
        }

    # -- Dispatch to the correct transition handler --
    _DISPATCH: dict[str, Any] = {
        "kyb_collected": _transition_kyb_collected,
        "profile_drafted": _transition_profile_drafted,
        "profile_approved": _transition_profile_approved,
        "shaken_created": _transition_shaken_created,
        "shaken_approved": _transition_shaken_approved,
        "cnam_created": _transition_cnam_created,
        "cnam_approved": _transition_cnam_approved,
        "number_attached": _transition_number_attached,
    }

    handler = _DISPATCH.get(trust_state)
    if handler is None:
        # Law #3: unknown state → fail closed. No Twilio calls.
        logger.error(
            "state_machine unknown_state trust_profile_id=%s state=%r — "
            "fail-closed; no Twilio calls made",
            trust_profile_id, trust_state,
        )
        return await _fail(
            trust_profile,
            from_state=trust_state,
            reason_code="UNKNOWN_STATE",
            reason_message=f"Unrecognized trust_state={trust_state!r}; cannot advance",
            worker_job_id=worker_job_id,
        )

    try:
        result = await handler(trust_profile, worker_job_id=worker_job_id)
    except Exception as exc:  # noqa: BLE001 — last-resort catch so worker never crashes
        logger.error(
            "state_machine unhandled_exception trust_profile_id=%s state=%s err=%s",
            trust_profile_id, trust_state, exc,
            exc_info=True,
        )
        return await _fail(
            trust_profile,
            from_state=trust_state,
            reason_code="UNHANDLED_EXCEPTION",
            reason_message=str(exc)[:500],
            worker_job_id=worker_job_id,
        )

    return result


__all__ = ["advance_trust_state"]
