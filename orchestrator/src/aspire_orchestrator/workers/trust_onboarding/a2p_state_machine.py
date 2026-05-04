"""A2P 10DLC state machine — Wave 7.

Single entry point: `advance_a2p_registration(suite_id, *, worker_job_id)`.

Drives each tenant's A2P brand + campaign through the 6-step Sole Proprietor
registration flow.  Standard Brand is an upgrade path — same machine, brand_type
controls the Twilio payload.

State graph (brand_status — one advance per call):

    draft           -> pending       (step 1: BrandRegistrations POST)
                                     HALT — wait for OTP (human action)
    otp_confirmed   -> pending       (steps 2-3: SoleProprietorVettings + OTP submit)
                                     HALT — wait for Twilio brand approval
    approved (brand)-> campaign work (steps 4-6: Messaging Service + phone + campaign)
                                     → campaign_status draft → pending
    campaign approved -> terminal HALT

States on tenant_a2p_brands.brand_status:
    draft, pending, otp_confirmed, approved, rejected, suspended

States on tenant_a2p_campaigns.campaign_status:
    draft, pending, approved, rejected, suspended

NOTE: `otp_confirmed` is an application-level synthetic state written by
the /v1/a2p/verify-otp route (W8 dependency). The state machine checks for
it and advances to the vetting POST.

Idempotency:
    Every Twilio create-call is guarded by a SID-column check.  If the SID
    column is already populated, the create is skipped.  Idempotency keys:
    `a2p-{operation}-{suite_id}`.

PII:
    phone_e164 (authorized rep OTP target) is NEVER logged or placed in
    receipts.  Only brand_id, campaign_id, and Twilio SIDs appear in
    redacted_inputs/redacted_outputs.

Aspire Laws enforced:
    Law #1  — single brain: no autonomous decisions, no retries, no fallbacks.
    Law #2  — receipts: cut_trust_receipt() called on every transition path.
    Law #3  — fail closed: unknown state → outcome="failed", no Twilio calls.
    Law #7  — tools are hands: pure execution, no planning.
    Law #9  — PII: phone_e164 never enters receipts/logs/returns.

Author: Aspire — Wave 7
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.providers import twilio_trust_hub as thub
from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
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
# Internal constants
# ---------------------------------------------------------------------------

# States where the machine HALTs and waits for external input.
_HALT_STATES: frozenset[str] = frozenset({
    "pending",       # Waiting for OTP (after step 1) OR Twilio brand approval
})

# Terminal failure/rejection states.
_TERMINAL_FAILURE_STATES: frozenset[str] = frozenset({
    "rejected",
    "suspended",
})

# Valid campaign use cases (mirrors migration 111 CHECK constraint).
_VALID_USE_CASES: frozenset[str] = frozenset({
    "MIXED",
    "2FA",
    "ACCOUNT_NOTIFICATION",
    "CUSTOMER_CARE",
    "DELIVERY_NOTIFICATION",
    "FRAUD_ALERT",
    "HIGHER_EDUCATION",
    "LOW_VOLUME",
    "MARKETING",
    "POLLING_VOTING",
    "PUBLIC_SERVICE_ANNOUNCEMENT",
})

# OTP lockout threshold (3 failed attempts → brand_status=suspended)
_OTP_MAX_ATTEMPTS: int = 3

# Prefix stored in rejection_reason to track OTP attempt count while brand is
# still in the OTP verification phase (before lockout).
_OTP_ATTEMPT_PREFIX: str = "OTP_ATTEMPT:"


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------


async def _load_brand(suite_id: str) -> dict[str, Any] | None:
    """Load the tenant_a2p_brands row for this suite (service_role)."""
    rows = await supabase_select(
        "tenant_a2p_brands",
        f"suite_id=eq.{suite_id}",
        limit=1,
    )
    return rows[0] if rows else None


async def _load_campaign(brand_id: str) -> dict[str, Any] | None:
    """Load the tenant_a2p_campaigns row for this brand (service_role)."""
    rows = await supabase_select(
        "tenant_a2p_campaigns",
        f"brand_id=eq.{brand_id}",
        limit=1,
    )
    return rows[0] if rows else None


async def _load_trust_profile_by_suite(suite_id: str) -> dict[str, Any] | None:
    """Load tenant_trust_profiles for the suite — needed for receipt scope IDs."""
    rows = await supabase_select(
        "tenant_trust_profiles",
        f"suite_id=eq.{suite_id}",
        limit=1,
    )
    return rows[0] if rows else None


async def _load_phone_number(suite_id: str) -> dict[str, Any] | None:
    """Load the active tenant_phone_numbers row for this suite."""
    rows = await supabase_select(
        "tenant_phone_numbers",
        f"suite_id=eq.{suite_id}&status=eq.active",
        limit=1,
    )
    return rows[0] if rows else None


async def _update_brand(brand_id: str, fields: dict[str, Any]) -> None:
    """PATCH tenant_a2p_brands (service_role)."""
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    await supabase_update(
        "tenant_a2p_brands",
        f"id=eq.{brand_id}",
        fields,
    )


async def _update_campaign(campaign_id: str, fields: dict[str, Any]) -> None:
    """PATCH tenant_a2p_campaigns (service_role)."""
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    await supabase_update(
        "tenant_a2p_campaigns",
        f"id=eq.{campaign_id}",
        fields,
    )


# ---------------------------------------------------------------------------
# Receipt-scope helper
# ---------------------------------------------------------------------------


def _make_receipt_scope(
    brand: dict[str, Any],
    trust_profile: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the minimal dict that cut_trust_receipt expects.

    A2P receipts hash-chain to the SAME trust_profile_id chain per architect
    mandate (unified audit ledger per tenant).  If no trust profile exists
    yet (edge case), we fall back to suite-only scope — this is tolerated
    because A2P requires profile_approved (so the profile WILL exist).
    """
    suite_id = str(brand.get("suite_id", ""))
    tenant_id = str(brand.get("tenant_id", ""))

    if trust_profile:
        return {
            "id": str(trust_profile.get("id", "")),
            "suite_id": suite_id,
            "tenant_id": tenant_id,
            "office_id": str(trust_profile.get("office_id", "")),
        }
    # Fallback — A2P route always ensures trust profile exists before enqueue.
    return {
        "id": str(brand.get("id", "")),  # brand id as a stand-in (non-ideal but safe)
        "suite_id": suite_id,
        "tenant_id": tenant_id,
        "office_id": "",
    }


# ---------------------------------------------------------------------------
# Failure helper
# ---------------------------------------------------------------------------


async def _fail_brand(
    brand: dict[str, Any],
    trust_profile: dict[str, Any] | None,
    *,
    from_state: str,
    reason_code: str,
    reason_message: str,
    worker_job_id: str | None,
) -> dict[str, Any]:
    """Set brand_status='rejected', cut a2p_brand_registered receipt with outcome=failed."""
    brand_id = str(brand["id"])
    suite_id = str(brand.get("suite_id", ""))
    logger.error(
        "a2p_state_machine FAIL brand_id=%s from=%s reason=%s: %s",
        brand_id, from_state, reason_code, reason_message,
    )
    try:
        await _update_brand(brand_id, {
            "brand_status": "rejected",
            "rejection_reason": reason_message[:500],
        })
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "a2p_state_machine fail_update_error brand_id=%s err=%s", brand_id, exc,
        )

    receipt_scope = _make_receipt_scope(brand, trust_profile)
    receipt_id: str | None = None
    try:
        receipt_id = await cut_trust_receipt(
            receipt_type="a2p_brand_registered",
            trust_profile=receipt_scope,
            outcome="failed",
            from_state=from_state,
            to_state="rejected",
            reason_code=reason_code,
            worker_job_id=worker_job_id,
            redacted_inputs={"brand_id": brand_id, "step_name": from_state},
            redacted_outputs={},
        )
    except TrustReceiptError as exc:
        logger.error(
            "a2p_state_machine receipt_cut_failed brand_id=%s err=%s", brand_id, exc,
        )

    return {
        "suite_id": suite_id,
        "brand_id": brand_id,
        "from_state": from_state,
        "to_state": "rejected",
        "outcome": "failed",
        "receipt_id": receipt_id,
        "reason_code": reason_code,
    }


# ---------------------------------------------------------------------------
# Step 1: draft → pending (BrandRegistrations POST)
# ---------------------------------------------------------------------------


async def _transition_draft(
    brand: dict[str, Any],
    trust_profile: dict[str, Any] | None,
    *,
    worker_job_id: str | None,
) -> dict[str, Any]:
    """draft → pending.

    POST /v1/a2p/BrandRegistrations with CustomerProfileBundleSid from trust_profile.
    Requires trust_profile.trust_state IN ('profile_approved', 'shaken_approved',
    'cnam_approved', 'number_attached', ...) — checked in the route, but we
    re-validate here as defense-in-depth.

    Idempotency: skip if twilio_brand_registration_sid already set.
    """
    brand_id = str(brand["id"])
    suite_id = str(brand.get("suite_id", ""))
    from_state = "draft"
    t_start = time.monotonic()

    # Defense-in-depth: profile must be approved.
    if not trust_profile:
        return await _fail_brand(
            brand, trust_profile,
            from_state=from_state,
            reason_code="PROFILE_NOT_FOUND",
            reason_message="No tenant_trust_profiles row found — profile_approved required before A2P",
            worker_job_id=worker_job_id,
        )

    _APPROVED_STATES = frozenset({
        "profile_approved", "shaken_created", "shaken_submitted", "shaken_approved",
        "cnam_created", "cnam_submitted", "cnam_approved", "number_attached",
        "branded_calling_pending", "branded_calling_live",
    })
    trust_state = str(trust_profile.get("trust_state", ""))
    if trust_state not in _APPROVED_STATES:
        return await _fail_brand(
            brand, trust_profile,
            from_state=from_state,
            reason_code="PROFILE_NOT_APPROVED",
            reason_message=(
                f"Customer Profile must be approved before A2P registration. "
                f"Current trust_state={trust_state!r}"
            ),
            worker_job_id=worker_job_id,
        )

    secondary_profile_sid = str(trust_profile.get("twilio_secondary_profile_sid") or "")
    if not secondary_profile_sid:
        return await _fail_brand(
            brand, trust_profile,
            from_state=from_state,
            reason_code="MISSING_SECONDARY_PROFILE_SID",
            reason_message="twilio_secondary_profile_sid is null; profile submission required first",
            worker_job_id=worker_job_id,
        )

    # Idempotency: skip Twilio call if brand_registration_sid already stored.
    brand_reg_sid: str | None = brand.get("twilio_brand_registration_sid")

    if not brand_reg_sid:
        is_sole_prop = brand.get("brand_type", "sole_proprietor") == "sole_proprietor"
        idem_key = f"a2p-brand-register-{suite_id}"
        try:
            result = await thub.create_a2p_brand_registration(
                customer_profile_sid=secondary_profile_sid,
                a2p_profile_sid=secondary_profile_sid,  # Sole Prop: same SID for A2P bundle
                sole_prop=is_sole_prop,
                idempotency_key=idem_key,
            )
            brand_reg_sid = result.get("sid") or result.get("brandRegistrationSid", "")
            # Twilio also returns a "brandSid" (BN...) distinct from the registration SID (BR...)
            twilio_brand_sid = result.get("brandSid") or result.get("brand_sid", "")
        except TrustHubError as exc:
            return await _fail_brand(
                brand, trust_profile,
                from_state=from_state,
                reason_code="CREATE_BRAND_REGISTRATION_FAILED",
                reason_message=str(exc),
                worker_job_id=worker_job_id,
            )
    else:
        twilio_brand_sid = brand.get("twilio_brand_sid", "")

    # Advance state and store SIDs.
    try:
        await _update_brand(brand_id, {
            "brand_status": "pending",
            "twilio_brand_registration_sid": brand_reg_sid,
            "twilio_brand_sid": twilio_brand_sid or None,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "otp_sent_at": datetime.now(timezone.utc).isoformat(),
        })
    except SupabaseClientError as exc:
        return await _fail_brand(
            brand, trust_profile,
            from_state=from_state,
            reason_code="DB_UPDATE_FAILED",
            reason_message=str(exc),
            worker_job_id=worker_job_id,
        )

    latency = time.monotonic() - t_start
    receipt_scope = _make_receipt_scope(brand, trust_profile)
    receipt_id = await cut_trust_receipt(
        receipt_type="a2p_brand_registered",
        trust_profile=receipt_scope,
        outcome="success",
        from_state=from_state,
        to_state="pending",
        twilio_resource_sid=brand_reg_sid or "",
        twilio_status="pending",
        worker_job_id=worker_job_id,
        redacted_inputs={"brand_id": brand_id, "step_name": from_state},
        redacted_outputs={
            "twilio_resource_sid": brand_reg_sid or "",
            "twilio_status": "pending",
            "latency_seconds": round(latency, 3),
        },
    )

    return {
        "suite_id": suite_id,
        "brand_id": brand_id,
        "from_state": from_state,
        "to_state": "pending",
        "outcome": "success",
        "receipt_id": receipt_id,
        "otp_required": True,
    }


# ---------------------------------------------------------------------------
# Steps 2-3: otp_confirmed → pending (SoleProprietorVettings + OTP submit)
# ---------------------------------------------------------------------------


async def _transition_otp_confirmed(
    brand: dict[str, Any],
    trust_profile: dict[str, Any] | None,
    *,
    worker_job_id: str | None,
) -> dict[str, Any]:
    """otp_confirmed → pending (awaiting Twilio brand approval).

    POST /v1/a2p/BrandRegistrations/{Sid}/SoleProprietorVettings.
    The OTP code was already submitted by /v1/a2p/verify-otp (W8 route).
    This step fires the formal vetting endpoint so Twilio progresses the brand.
    Idempotency: skip if twilio_brand_sid already starts with 'BV' (vetting SID).
    """
    brand_id = str(brand["id"])
    suite_id = str(brand.get("suite_id", ""))
    from_state = "otp_confirmed"
    t_start = time.monotonic()

    brand_reg_sid: str = str(brand.get("twilio_brand_registration_sid") or "")
    if not brand_reg_sid:
        return await _fail_brand(
            brand, trust_profile,
            from_state=from_state,
            reason_code="MISSING_BRAND_REGISTRATION_SID",
            reason_message="twilio_brand_registration_sid is null at otp_confirmed stage",
            worker_job_id=worker_job_id,
        )

    # Idempotency: skip if vetting already submitted.
    vetting_sid: str | None = brand.get("twilio_brand_vetting_sid")
    if not vetting_sid:
        idem_key = f"a2p-sole-prop-vetting-{suite_id}"
        try:
            vet_result = await thub.create_sole_proprietor_vetting(
                brand_registration_sid=brand_reg_sid,
                idempotency_key=idem_key,
            )
            vetting_sid = vet_result.get("sid", "")
        except TrustHubError as exc:
            return await _fail_brand(
                brand, trust_profile,
                from_state=from_state,
                reason_code="SOLE_PROP_VETTING_FAILED",
                reason_message=str(exc),
                worker_job_id=worker_job_id,
            )

    # Store vetting SID + set back to pending (awaiting Twilio approval).
    try:
        await _update_brand(brand_id, {
            "brand_status": "pending",
            "twilio_brand_vetting_sid": vetting_sid,
        })
    except SupabaseClientError as exc:
        return await _fail_brand(
            brand, trust_profile,
            from_state=from_state,
            reason_code="DB_UPDATE_FAILED",
            reason_message=str(exc),
            worker_job_id=worker_job_id,
        )

    latency = time.monotonic() - t_start
    receipt_scope = _make_receipt_scope(brand, trust_profile)
    receipt_id = await cut_trust_receipt(
        receipt_type="a2p_brand_registered",
        trust_profile=receipt_scope,
        outcome="success",
        from_state=from_state,
        to_state="pending",
        twilio_resource_sid=vetting_sid or "",
        twilio_status="pending",
        worker_job_id=worker_job_id,
        redacted_inputs={"brand_id": brand_id, "step_name": "sole_prop_vetting"},
        redacted_outputs={
            "twilio_resource_sid": vetting_sid or "",
            "twilio_status": "pending",
            "latency_seconds": round(latency, 3),
        },
    )

    return {
        "suite_id": suite_id,
        "brand_id": brand_id,
        "from_state": from_state,
        "to_state": "pending",
        "outcome": "success",
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# Steps 4-6: brand approved → campaign draft → pending
# ---------------------------------------------------------------------------


async def _transition_brand_approved(
    brand: dict[str, Any],
    campaign: dict[str, Any],
    trust_profile: dict[str, Any] | None,
    *,
    worker_job_id: str | None,
) -> dict[str, Any]:
    """brand approved → campaign draft → campaign pending.

    Steps:
    4. Create Messaging Service (idempotency: skip if twilio_messaging_service_sid set).
    5. Add tenant phone to Messaging Service (idempotency: skip if already added).
    6. Register campaign via UsAppToPerson (idempotency: skip if twilio_campaign_sid set).
    7. Advance campaign_status to pending.
    8. Cut a2p_campaign_approved receipt (outcome=pending — approved when Twilio callback fires).
    """
    brand_id = str(brand["id"])
    campaign_id = str(campaign["id"])
    suite_id = str(brand.get("suite_id", ""))
    from_state = "approved"
    t_start = time.monotonic()

    # Step 4: Create Messaging Service.
    messaging_service_sid: str | None = campaign.get("twilio_messaging_service_sid")
    if not messaging_service_sid:
        idem_key = f"a2p-messaging-service-{suite_id}"
        try:
            svc_result = await thub.create_messaging_service(
                friendly_name=f"Aspire-A2P-{suite_id[:8]}",
                idempotency_key=idem_key,
            )
            messaging_service_sid = svc_result.get("sid", "")
        except TrustHubError as exc:
            return await _fail_brand(
                brand, trust_profile,
                from_state=from_state,
                reason_code="CREATE_MESSAGING_SERVICE_FAILED",
                reason_message=str(exc),
                worker_job_id=worker_job_id,
            )

        # Persist immediately so Step 5 is idempotent on re-run.
        try:
            await _update_campaign(campaign_id, {
                "twilio_messaging_service_sid": messaging_service_sid,
            })
        except SupabaseClientError as exc:
            return await _fail_brand(
                brand, trust_profile,
                from_state=from_state,
                reason_code="DB_UPDATE_FAILED",
                reason_message=str(exc),
                worker_job_id=worker_job_id,
            )

    # Step 5: Add tenant phone number to Messaging Service.
    phone_row = await _load_phone_number(suite_id)
    if not phone_row:
        return await _fail_brand(
            brand, trust_profile,
            from_state=from_state,
            reason_code="NO_ACTIVE_PHONE_NUMBER",
            reason_message="No active phone number found for suite; cannot add to Messaging Service",
            worker_job_id=worker_job_id,
        )

    number_sid = str(phone_row.get("twilio_sid") or phone_row.get("phone_sid") or "")
    if number_sid:
        idem_key = f"a2p-add-phone-to-service-{suite_id}"
        try:
            await thub.add_phone_to_messaging_service(
                messaging_service_sid,
                number_sid,
                idempotency_key=idem_key,
            )
        except TrustHubError as exc:
            if exc.status_code != 409:  # 409 = already added, idempotent
                return await _fail_brand(
                    brand, trust_profile,
                    from_state=from_state,
                    reason_code="ADD_PHONE_TO_MESSAGING_SERVICE_FAILED",
                    reason_message=str(exc),
                    worker_job_id=worker_job_id,
                )

    # Step 6: Register campaign.
    campaign_sid: str | None = campaign.get("twilio_campaign_sid")
    if not campaign_sid:
        idem_key = f"a2p-campaign-{suite_id}"
        use_case = str(campaign.get("campaign_use_case", "MIXED"))
        description = str(campaign.get("campaign_description", ""))
        sample_messages: list[str] = list(campaign.get("sample_messages") or [])
        has_links = bool(campaign.get("has_embedded_links", False))
        has_phone = bool(campaign.get("has_embedded_phone", False))

        try:
            cmp_result = await thub.create_a2p_campaign(
                messaging_service_sid=messaging_service_sid or "",
                description=description,
                message_samples=sample_messages,
                use_case=use_case,
                has_embedded_links=has_links,
                has_embedded_phone=has_phone,
                idempotency_key=idem_key,
            )
            campaign_sid = cmp_result.get("sid", "")
        except TrustHubError as exc:
            return await _fail_brand(
                brand, trust_profile,
                from_state=from_state,
                reason_code="CREATE_CAMPAIGN_FAILED",
                reason_message=str(exc),
                worker_job_id=worker_job_id,
            )

    # Advance campaign to pending.
    try:
        await _update_campaign(campaign_id, {
            "campaign_status": "pending",
            "twilio_campaign_sid": campaign_sid,
            "twilio_messaging_service_sid": messaging_service_sid,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        })
    except SupabaseClientError as exc:
        return await _fail_brand(
            brand, trust_profile,
            from_state=from_state,
            reason_code="DB_UPDATE_FAILED",
            reason_message=str(exc),
            worker_job_id=worker_job_id,
        )

    latency = time.monotonic() - t_start
    receipt_scope = _make_receipt_scope(brand, trust_profile)
    receipt_id = await cut_trust_receipt(
        receipt_type="a2p_campaign_approved",
        trust_profile=receipt_scope,
        outcome="pending",   # campaign is pending Twilio approval; receipt records the submission
        from_state=from_state,
        to_state="campaign_pending",
        twilio_resource_sid=campaign_sid or "",
        twilio_status="pending",
        worker_job_id=worker_job_id,
        redacted_inputs={
            "brand_id": brand_id,
            "campaign_id": campaign_id,
            "step_name": "campaign_registration",
        },
        redacted_outputs={
            "twilio_resource_sid": campaign_sid or "",
            "twilio_status": "pending",
            "latency_seconds": round(latency, 3),
        },
    )

    return {
        "suite_id": suite_id,
        "brand_id": brand_id,
        "campaign_id": campaign_id,
        "from_state": from_state,
        "to_state": "campaign_pending",
        "outcome": "success",
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# OTP submission helper (called by /v1/a2p/verify-otp route)
# ---------------------------------------------------------------------------


async def submit_a2p_otp(
    suite_id: str,
    otp_code: str,
    *,
    worker_job_id: str | None = None,
) -> dict[str, Any]:
    """Submit OTP code to Twilio and advance brand to otp_confirmed state.

    Called by POST /v1/a2p/verify-otp (W8 route).  Returns:
        {
            "success": bool,
            "brand_id": str,
            "brand_status": str,
            "otp_attempts": int,
            "locked_out": bool,
            "receipt_id": str | None,
        }

    OTP failure increments the attempt counter stored in rejection_reason
    as f"{_OTP_ATTEMPT_PREFIX}{n}".  On 3rd failure, brand_status → suspended.

    Law #9: otp_code is NEVER logged or placed in receipts.
    """
    brand = await _load_brand(suite_id)
    if not brand:
        return {
            "success": False,
            "brand_id": "",
            "brand_status": "unknown",
            "otp_attempts": 0,
            "locked_out": False,
            "receipt_id": None,
            "reason_code": "NO_BRAND_RECORD",
        }

    brand_id = str(brand["id"])
    brand_reg_sid = str(brand.get("twilio_brand_registration_sid") or "")
    current_status = str(brand.get("brand_status", ""))

    # Already locked out
    if current_status == "suspended":
        return {
            "success": False,
            "brand_id": brand_id,
            "brand_status": "suspended",
            "otp_attempts": _OTP_MAX_ATTEMPTS,
            "locked_out": True,
            "receipt_id": None,
            "reason_code": "OTP_LOCKED_OUT",
        }

    # Parse current attempt count from rejection_reason
    raw_reason = str(brand.get("rejection_reason") or "")
    current_attempts = 0
    if raw_reason.startswith(_OTP_ATTEMPT_PREFIX):
        try:
            current_attempts = int(raw_reason[len(_OTP_ATTEMPT_PREFIX):])
        except ValueError:
            current_attempts = 0

    if not brand_reg_sid:
        return {
            "success": False,
            "brand_id": brand_id,
            "brand_status": current_status,
            "otp_attempts": current_attempts,
            "locked_out": False,
            "receipt_id": None,
            "reason_code": "MISSING_BRAND_REGISTRATION_SID",
        }

    trust_profile = await _load_trust_profile_by_suite(suite_id)

    # Submit OTP to Twilio
    idem_key = f"a2p-otp-verify-{suite_id}"
    try:
        await thub.submit_a2p_otp(
            brand_registration_sid=brand_reg_sid,
            otp_code=otp_code,
            idempotency_key=idem_key,
        )
    except TrustHubError as exc:
        # OTP wrong code — increment attempts, possibly lock out.
        new_attempts = current_attempts + 1
        logger.warning(
            "a2p_state_machine otp_verify_failed brand_id=%s attempt=%d/%d",
            brand_id, new_attempts, _OTP_MAX_ATTEMPTS,
        )
        locked_out = new_attempts >= _OTP_MAX_ATTEMPTS

        update_fields: dict[str, Any] = {
            "rejection_reason": f"{_OTP_ATTEMPT_PREFIX}{new_attempts}",
        }
        if locked_out:
            update_fields["brand_status"] = "suspended"
            update_fields["rejection_reason"] = (
                f"OTP_LOCKED_OUT after {new_attempts} failed attempts"
            )

        try:
            await _update_brand(brand_id, update_fields)
        except SupabaseClientError as db_exc:
            logger.error(
                "a2p_state_machine otp_fail_update_error brand_id=%s err=%s",
                brand_id, db_exc,
            )

        return {
            "success": False,
            "brand_id": brand_id,
            "brand_status": "suspended" if locked_out else current_status,
            "otp_attempts": new_attempts,
            "locked_out": locked_out,
            "receipt_id": None,
            "reason_code": "OTP_LOCKED_OUT" if locked_out else "INVALID_OTP",
        }

    # OTP accepted — advance to otp_confirmed
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        await _update_brand(brand_id, {
            "brand_status": "otp_confirmed",
            "otp_verified_at": now_iso,
            "rejection_reason": None,
        })
    except SupabaseClientError as exc:
        logger.error(
            "a2p_state_machine otp_confirm_update_error brand_id=%s err=%s",
            brand_id, exc,
        )
        return {
            "success": False,
            "brand_id": brand_id,
            "brand_status": current_status,
            "otp_attempts": current_attempts,
            "locked_out": False,
            "receipt_id": None,
            "reason_code": "DB_UPDATE_FAILED",
        }

    receipt_scope = _make_receipt_scope(brand, trust_profile)
    receipt_id: str | None = None
    try:
        receipt_id = await cut_trust_receipt(
            receipt_type="a2p_brand_registered",
            trust_profile=receipt_scope,
            outcome="success",
            from_state="pending",
            to_state="otp_confirmed",
            twilio_resource_sid=brand_reg_sid,
            twilio_status="otp_confirmed",
            worker_job_id=worker_job_id,
            redacted_inputs={"brand_id": brand_id, "step_name": "otp_verification"},
            redacted_outputs={"twilio_resource_sid": brand_reg_sid, "twilio_status": "otp_confirmed"},
        )
    except TrustReceiptError as exc:
        logger.error(
            "a2p_state_machine otp_receipt_failed brand_id=%s err=%s", brand_id, exc,
        )

    return {
        "success": True,
        "brand_id": brand_id,
        "brand_status": "otp_confirmed",
        "otp_attempts": current_attempts,
        "locked_out": False,
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def advance_a2p_registration(
    suite_id: str,
    *,
    worker_job_id: str | None = None,
) -> dict[str, Any]:
    """Advance a tenant's A2P registration by EXACTLY ONE state transition.

    Called by the ARQ worker (worker.py).

    Args:
        suite_id: UUID of the tenant's suite.
        worker_job_id: ARQ job ID for traceability.

    Returns:
        {
            "suite_id": str,
            "brand_id": str,
            "from_state": str,
            "to_state": str,
            "outcome": "success" | "halted" | "failed",
            "receipt_id": str | None,
        }

    Never raises to caller — all errors are mapped to outcome="failed".
    """
    logger.info(
        "a2p_state_machine advance_a2p_registration suite_id=%s job_id=%s",
        suite_id, worker_job_id,
    )

    # Load brand row
    try:
        brand = await _load_brand(suite_id)
    except SupabaseClientError as exc:
        logger.error(
            "a2p_state_machine load_brand_failed suite_id=%s err=%s", suite_id, exc,
        )
        return {
            "suite_id": suite_id,
            "brand_id": "",
            "from_state": "unknown",
            "to_state": "unknown",
            "outcome": "failed",
            "receipt_id": None,
            "reason_code": "BRAND_LOAD_FAILED",
        }

    if not brand:
        logger.error(
            "a2p_state_machine no_brand_found suite_id=%s — cannot advance", suite_id,
        )
        return {
            "suite_id": suite_id,
            "brand_id": "",
            "from_state": "unknown",
            "to_state": "unknown",
            "outcome": "failed",
            "receipt_id": None,
            "reason_code": "NO_BRAND_RECORD",
        }

    brand_id = str(brand["id"])
    brand_status = str(brand.get("brand_status", ""))

    # Load trust profile (for receipt scope + profile_approved guard)
    try:
        trust_profile = await _load_trust_profile_by_suite(suite_id)
    except SupabaseClientError as exc:
        logger.warning(
            "a2p_state_machine trust_profile_load_failed suite_id=%s err=%s — continuing",
            suite_id, exc,
        )
        trust_profile = None

    # -- Halt states --
    if brand_status in _HALT_STATES:
        logger.info(
            "a2p_state_machine halted brand_id=%s status=%s (awaiting OTP or Twilio approval)",
            brand_id, brand_status,
        )
        return {
            "suite_id": suite_id,
            "brand_id": brand_id,
            "from_state": brand_status,
            "to_state": brand_status,
            "outcome": "halted",
            "receipt_id": None,
        }

    # -- Terminal failure states --
    if brand_status in _TERMINAL_FAILURE_STATES:
        logger.warning(
            "a2p_state_machine terminal_state brand_id=%s status=%s — "
            "orchestrator must decide retry/escalation",
            brand_id, brand_status,
        )
        return {
            "suite_id": suite_id,
            "brand_id": brand_id,
            "from_state": brand_status,
            "to_state": brand_status,
            "outcome": "failed",
            "receipt_id": None,
            "reason_code": "TERMINAL_FAILURE_STATE",
        }

    # -- Campaign path: brand approved → run steps 4-6 --
    if brand_status == "approved":
        try:
            campaign = await _load_campaign(brand_id)
        except SupabaseClientError as exc:
            logger.error(
                "a2p_state_machine load_campaign_failed brand_id=%s err=%s", brand_id, exc,
            )
            return {
                "suite_id": suite_id,
                "brand_id": brand_id,
                "from_state": brand_status,
                "to_state": "failed",
                "outcome": "failed",
                "receipt_id": None,
                "reason_code": "CAMPAIGN_LOAD_FAILED",
            }

        if not campaign:
            logger.error(
                "a2p_state_machine no_campaign_found brand_id=%s — cannot register campaign",
                brand_id,
            )
            return {
                "suite_id": suite_id,
                "brand_id": brand_id,
                "from_state": brand_status,
                "to_state": "failed",
                "outcome": "failed",
                "receipt_id": None,
                "reason_code": "NO_CAMPAIGN_RECORD",
            }

        # Campaign already pending/approved — halt.
        campaign_status = str(campaign.get("campaign_status", ""))
        if campaign_status in ("pending", "approved"):
            return {
                "suite_id": suite_id,
                "brand_id": brand_id,
                "campaign_id": str(campaign["id"]),
                "from_state": brand_status,
                "to_state": campaign_status,
                "outcome": "halted",
                "receipt_id": None,
            }

        try:
            result = await _transition_brand_approved(
                brand, campaign, trust_profile, worker_job_id=worker_job_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "a2p_state_machine unhandled_exception brand_id=%s err=%s",
                brand_id, exc, exc_info=True,
            )
            result = await _fail_brand(
                brand, trust_profile,
                from_state=brand_status,
                reason_code="UNHANDLED_EXCEPTION",
                reason_message=str(exc)[:500],
                worker_job_id=worker_job_id,
            )
        return result

    # -- Dispatch table for brand_status --
    _DISPATCH: dict[str, Any] = {
        "draft": _transition_draft,
        "otp_confirmed": _transition_otp_confirmed,
    }

    handler = _DISPATCH.get(brand_status)
    if handler is None:
        logger.error(
            "a2p_state_machine unknown_state brand_id=%s status=%r — fail-closed",
            brand_id, brand_status,
        )
        return await _fail_brand(
            brand, trust_profile,
            from_state=brand_status,
            reason_code="UNKNOWN_STATE",
            reason_message=f"Unrecognized brand_status={brand_status!r}",
            worker_job_id=worker_job_id,
        )

    try:
        result = await handler(brand, trust_profile, worker_job_id=worker_job_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "a2p_state_machine unhandled_exception brand_id=%s status=%s err=%s",
            brand_id, brand_status, exc, exc_info=True,
        )
        result = await _fail_brand(
            brand, trust_profile,
            from_state=brand_status,
            reason_code="UNHANDLED_EXCEPTION",
            reason_message=str(exc)[:500],
            worker_job_id=worker_job_id,
        )

    return result


__all__ = ["advance_a2p_registration", "submit_a2p_otp"]
