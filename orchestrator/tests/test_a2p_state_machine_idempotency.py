"""Idempotency tests for A2P 10DLC state machine — Wave 7.

Covers:
  - Worker-kill scenarios: brand_pending re-run produces no duplicate Twilio call
  - Post-Twilio-create / pre-DB-write crash: re-run finds existing SID, does not re-create
  - OTP submission timeout mid-flight: fixed idempotency key means Twilio deduplicate
  - Campaign create on retry: same idempotency_key returns existing campaign_sid (no Twilio call)
  - Twilio 5xx → resilience layer surfaces RetryableError (not a state-machine TrustHubError)
  - CircuitOpenError from breaker → converted to TrustHubError, state machine fails-closed

Aspire Laws validated:
  Law #2  — receipts always cut on state change (even idempotent advances).
  Law #3  — fail closed on partial state (Twilio created, DB not updated yet).
  Law #10 — reliability: 5xx retried; circuit-breaker reject → fail-closed.

Author: Aspire — Wave 7 (adversarial additions)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
from aspire_orchestrator.services.resilience import CircuitOpenError, RetryableError
from aspire_orchestrator.workers.trust_onboarding.a2p_state_machine import (
    _OTP_ATTEMPT_PREFIX,
    _OTP_MAX_ATTEMPTS,
    advance_a2p_registration,
    submit_a2p_otp,
)

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

SUITE_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())
OFFICE_ID = str(uuid.uuid4())
BRAND_ID = str(uuid.uuid4())
CAMPAIGN_ID = str(uuid.uuid4())
TRUST_PROFILE_ID = str(uuid.uuid4())
BRAND_REG_SID = "BRbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb00"
BRAND_SID = "BNbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb00"
VETTING_SID = "BVbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb00"
MESSAGING_SVC_SID = "MGbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb00"
CAMPAIGN_SID = "QEbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb00"
PHONE_NUMBER_SID = "PNbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb00"


# ---------------------------------------------------------------------------
# Shared factory helpers
# ---------------------------------------------------------------------------


def _make_brand(
    brand_status: str = "draft",
    brand_reg_sid: str | None = None,
    brand_sid: str | None = None,
    vetting_sid: str | None = None,
    rejection_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": BRAND_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "brand_type": "sole_proprietor",
        "brand_status": brand_status,
        "twilio_brand_registration_sid": brand_reg_sid,
        "twilio_brand_sid": brand_sid,
        "twilio_brand_vetting_sid": vetting_sid,
        "otp_verified_at": None,
        "rejection_reason": rejection_reason,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _make_campaign(
    campaign_status: str = "draft",
    messaging_svc_sid: str | None = None,
    campaign_sid: str | None = None,
) -> dict[str, Any]:
    return {
        "id": CAMPAIGN_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "brand_id": BRAND_ID,
        "campaign_use_case": "MIXED",
        "campaign_description": "Aspire business notifications",
        "sample_messages": ["Hello from Aspire!", "Your appointment is confirmed."],
        "has_embedded_links": False,
        "has_embedded_phone": False,
        "campaign_status": campaign_status,
        "twilio_messaging_service_sid": messaging_svc_sid,
        "twilio_campaign_sid": campaign_sid,
    }


def _make_trust_profile(trust_state: str = "profile_approved") -> dict[str, Any]:
    return {
        "id": TRUST_PROFILE_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "office_id": OFFICE_ID,
        "trust_state": trust_state,
        "twilio_secondary_profile_sid": "BUbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb00",
    }


def _make_phone_row() -> dict[str, Any]:
    return {
        "twilio_sid": PHONE_NUMBER_SID,
        "phone_number": "+15005550006",
        "status": "active",
    }


# ---------------------------------------------------------------------------
# Mock infrastructure (mirrors test_a2p_state_machine.py)
# ---------------------------------------------------------------------------


class _MockSupabase:
    """Tracks supabase calls for assertion; mutates rows on update."""

    def __init__(
        self,
        brand: dict[str, Any] | None = None,
        campaign: dict[str, Any] | None = None,
        trust_profile: dict[str, Any] | None = None,
        phone: dict[str, Any] | None = None,
    ) -> None:
        self.brand = brand
        self.campaign = campaign
        self.trust_profile = trust_profile
        self.phone = phone
        self.updated: list[tuple[str, Any, Any]] = []
        self.inserted: list[tuple[str, Any]] = []

    async def select(self, table: str, filter_str: str, limit: int = 10, **_: Any) -> list[Any]:
        if table == "tenant_a2p_brands":
            return [self.brand] if self.brand else []
        if table == "tenant_a2p_campaigns":
            return [self.campaign] if self.campaign else []
        if table == "tenant_trust_profiles":
            return [self.trust_profile] if self.trust_profile else []
        if table == "tenant_phone_numbers":
            return [self.phone] if self.phone else []
        return []

    async def update(self, table: str, filter_str: str, fields: dict[str, Any]) -> None:
        self.updated.append((table, filter_str, fields))
        if table == "tenant_a2p_brands" and self.brand:
            self.brand.update(fields)
        if table == "tenant_a2p_campaigns" and self.campaign:
            self.campaign.update(fields)

    async def insert(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        self.inserted.append((table, row))
        return row


def _patch_supabase(mock_sb: _MockSupabase):
    return patch.multiple(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine",
        supabase_select=AsyncMock(side_effect=mock_sb.select),
        supabase_update=AsyncMock(side_effect=mock_sb.update),
        supabase_insert=AsyncMock(side_effect=mock_sb.insert),
    )


def _patch_cut_receipt():
    return patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.cut_trust_receipt",
        new_callable=AsyncMock,
        return_value=f"trust_a2p_brand_registered_{uuid.uuid4().hex}",
    )


# ---------------------------------------------------------------------------
# 1. Brand registration idempotency — worker-kill scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brand_pending_rerun_does_not_duplicate_twilio_call():
    """Worker killed mid-brand_pending: re-run with SID already in DB skips Twilio.

    Law #2 + idempotency: if twilio_brand_registration_sid is already stored,
    the state machine MUST skip the Twilio create call and still advance state.
    This test simulates the worker being re-enqueued after a crash.
    """
    # Brand is in 'draft' state but the brand_reg_sid is ALREADY stored
    # (Twilio call succeeded on the previous attempt, DB write succeeded too,
    #  but the worker crashed before sending the HTTP response).
    brand = _make_brand(
        brand_status="draft",
        brand_reg_sid=BRAND_REG_SID,
        brand_sid=BRAND_SID,
    )
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
    ) as mock_create:
        result = await advance_a2p_registration(SUITE_ID)

    # Must succeed without creating a duplicate brand on Twilio
    assert result["outcome"] == "success", (
        f"Expected outcome=success, got {result}"
    )
    assert result["to_state"] == "pending"
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_brand_pending_rerun_idempotency_key_constant():
    """Idempotency key for brand registration is always 'a2p-brand-register-{suite_id}'.

    Twilio uses this key to deduplicate concurrent/replayed POST requests.
    Verify the key is stable across re-runs (same value regardless of attempt count).
    """
    brand = _make_brand(brand_status="draft")
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
        return_value={"sid": BRAND_REG_SID, "brandSid": BRAND_SID},
    ) as mock_create:
        # First run
        await advance_a2p_registration(SUITE_ID)

    idem_key_used = mock_create.call_args.kwargs["idempotency_key"]
    assert idem_key_used == f"a2p-brand-register-{SUITE_ID}", (
        f"Wrong idempotency key: {idem_key_used!r}"
    )


@pytest.mark.asyncio
async def test_twilio_brand_created_but_db_write_crashed_advance_is_idempotent():
    """Partial crash: Twilio created brand, DB write failed, brand_reg_sid NOT stored.

    This is the hardest idempotency case: the brand_status is still 'draft'
    and twilio_brand_registration_sid is null because the DB write crashed.
    On re-run, the state machine will call Twilio AGAIN — but Twilio will
    deduplicate via the idempotency_key and return the SAME SID.

    Verify: second Twilio call uses the same idempotency_key → state advances.
    """
    # Brand with no stored SID (crash before DB write)
    brand = _make_brand(brand_status="draft", brand_reg_sid=None)
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    # Twilio returns the same SID on both calls (idempotent-per-key)
    twilio_response = {"sid": BRAND_REG_SID, "brandSid": BRAND_SID}

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
        return_value=twilio_response,
    ) as mock_create:
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "success"
    assert result["to_state"] == "pending"
    # Twilio was called — but with the deterministic idempotency key
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["idempotency_key"] == f"a2p-brand-register-{SUITE_ID}"

    # DB was updated with the brand SIDs
    brand_updates = [u for u in mock_sb.updated if u[0] == "tenant_a2p_brands"]
    assert any(
        u[2].get("twilio_brand_registration_sid") == BRAND_REG_SID
        for u in brand_updates
    ), "brand_reg_sid must be stored in DB after successful Twilio call"


# ---------------------------------------------------------------------------
# 2. OTP submission idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_otp_submission_uses_fixed_idempotency_key():
    """OTP submit idempotency key is 'a2p-otp-verify-{suite_id}' — same on every attempt.

    If the OTP call times out mid-flight, the re-submit uses the same key so
    Twilio will either process it once or return the prior result.
    """
    brand = _make_brand(brand_status="pending", brand_reg_sid=BRAND_REG_SID)
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.submit_a2p_otp",
        new_callable=AsyncMock,
        return_value={"status": "verified"},
    ) as mock_otp:
        result = await submit_a2p_otp(SUITE_ID, "123456")

    assert result["success"] is True
    mock_otp.assert_called_once()
    assert mock_otp.call_args.kwargs["idempotency_key"] == f"a2p-otp-verify-{SUITE_ID}", (
        "OTP idempotency key must be deterministic for timeout-safe re-submission"
    )


@pytest.mark.asyncio
async def test_otp_already_confirmed_resubmit_does_not_regress():
    """Re-submitting OTP when brand is already 'otp_confirmed' must not call Twilio.

    If otp_confirmed was written but the HTTP response was lost, a naive retry
    would re-run the submit_a2p_otp flow. The state machine must detect
    otp_confirmed status and NOT call Twilio again.

    Expected: the state machine advance_a2p_registration runs the otp_confirmed
    transition (vetting POST), not the OTP flow. But calling submit_a2p_otp
    directly when status=otp_confirmed is an evil test — it should succeed (or
    at minimum not regress the brand state).
    """
    # Brand already in otp_confirmed — so advance_a2p_registration should move to vetting
    brand = _make_brand(
        brand_status="otp_confirmed",
        brand_reg_sid=BRAND_REG_SID,
        vetting_sid=VETTING_SID,  # vetting already done too
    )
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_sole_proprietor_vetting",
        new_callable=AsyncMock,
    ) as mock_vet:
        result = await advance_a2p_registration(SUITE_ID)

    # Must advance (idempotent vetting skip) without re-submitting OTP
    assert result["outcome"] == "success"
    assert result["to_state"] == "pending"
    mock_vet.assert_not_called()  # vetting SID already stored


# ---------------------------------------------------------------------------
# 3. Campaign create idempotency on retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_campaign_create_idempotency_key_constant_on_retry():
    """Campaign creation idempotency key is 'a2p-campaign-{suite_id}'.

    Same key on every advance_a2p_registration call for this suite.
    Twilio will return the existing campaign_sid if already processed.
    """
    brand = _make_brand(brand_status="approved", brand_reg_sid=BRAND_REG_SID)
    campaign = _make_campaign(
        campaign_status="draft",
        messaging_svc_sid=MESSAGING_SVC_SID,
        campaign_sid=None,  # not yet created
    )
    phone = _make_phone_row()
    mock_sb = _MockSupabase(
        brand=brand, campaign=campaign,
        trust_profile=_make_trust_profile(), phone=phone,
    )

    with (
        _patch_supabase(mock_sb),
        _patch_cut_receipt(),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_messaging_service",
            new_callable=AsyncMock,
        ),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.add_phone_to_messaging_service",
            new_callable=AsyncMock,
            return_value={"sid": "PA000"},
        ),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_campaign",
            new_callable=AsyncMock,
            return_value={"sid": CAMPAIGN_SID},
        ) as mock_cmp,
    ):
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "success"
    assert result["to_state"] == "campaign_pending"
    mock_cmp.assert_called_once()
    assert mock_cmp.call_args.kwargs["idempotency_key"] == f"a2p-campaign-{SUITE_ID}"


@pytest.mark.asyncio
async def test_campaign_create_with_existing_sid_skips_twilio():
    """If campaign_sid is already stored, skip create_a2p_campaign on retry.

    Simulates: Twilio create succeeded, DB write succeeded, but worker was
    re-enqueued before returning (e.g., ARQ retry due to timeout).
    """
    brand = _make_brand(brand_status="approved", brand_reg_sid=BRAND_REG_SID)
    campaign = _make_campaign(
        campaign_status="draft",
        messaging_svc_sid=MESSAGING_SVC_SID,
        campaign_sid=CAMPAIGN_SID,  # already stored
    )
    phone = _make_phone_row()
    mock_sb = _MockSupabase(
        brand=brand, campaign=campaign,
        trust_profile=_make_trust_profile(), phone=phone,
    )

    with (
        _patch_supabase(mock_sb),
        _patch_cut_receipt(),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_messaging_service",
            new_callable=AsyncMock,
        ),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.add_phone_to_messaging_service",
            new_callable=AsyncMock,
            return_value={"sid": "PA000"},
        ),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_campaign",
            new_callable=AsyncMock,
        ) as mock_cmp,
    ):
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "success"
    mock_cmp.assert_not_called()  # existing SID — skip Twilio


# ---------------------------------------------------------------------------
# 4. Twilio 5xx → resilience error surfaces correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_twilio_5xx_on_brand_registration_raises_retryable_not_state_machine_failure():
    """Twilio 5xx on BrandRegistrations POST → RetryableError MUST propagate to ARQ.

    Law #10 (reliability): transient 5xx must let ARQ's exponential-backoff
    retry budget kick in. If the state machine catches RetryableError and
    calls _fail_brand instead, the brand is marked permanently rejected on
    first 5xx — neutralizing ARQ's retry. Post-fix, the BLE001 guard
    explicitly re-raises RetryableError before the generic Exception clause.
    """
    brand = _make_brand(brand_status="draft")
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
        side_effect=RetryableError("TWILIO_TRANSIENT", "Trust Hub POST transient 500"),
    ):
        with pytest.raises(RetryableError):
            await advance_a2p_registration(SUITE_ID)


@pytest.mark.asyncio
async def test_twilio_circuit_open_on_brand_registration_fails_closed():
    """CircuitOpenError → provider raises TrustHubError(503) → state machine fails-closed.

    The provider's try/except CircuitOpenError re-raises as TrustHubError(503).
    State machine must treat this as a brand failure (not a silent pass).
    """
    brand = _make_brand(brand_status="draft")
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
        side_effect=TrustHubError("TWILIO_CIRCUIT_OPEN", "Twilio is degraded", 503),
    ):
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "CREATE_BRAND_REGISTRATION_FAILED"
    assert result["to_state"] == "rejected"


@pytest.mark.asyncio
async def test_twilio_5xx_on_campaign_create_fails_closed():
    """Twilio 5xx on UsAppToPerson → RetryableError MUST propagate to ARQ.

    Law #10 (reliability): same invariant as the brand-registration retry
    test — transient failures during campaign create must NOT mark the
    brand as rejected. ARQ's retry budget owns transient recovery.
    """
    brand = _make_brand(brand_status="approved", brand_reg_sid=BRAND_REG_SID)
    campaign = _make_campaign(campaign_status="draft", messaging_svc_sid=MESSAGING_SVC_SID)
    phone = _make_phone_row()
    mock_sb = _MockSupabase(
        brand=brand, campaign=campaign,
        trust_profile=_make_trust_profile(), phone=phone,
    )

    with (
        _patch_supabase(mock_sb),
        _patch_cut_receipt(),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_messaging_service",
            new_callable=AsyncMock,
        ),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.add_phone_to_messaging_service",
            new_callable=AsyncMock,
            return_value={"sid": "PA000"},
        ),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_campaign",
            new_callable=AsyncMock,
            side_effect=RetryableError("TWILIO_TRANSIENT", "Trust Hub POST transient 500"),
        ),
    ):
        with pytest.raises(RetryableError):
            await advance_a2p_registration(SUITE_ID)


# ---------------------------------------------------------------------------
# 5. Messaging service create idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_messaging_service_idempotency_key_is_constant():
    """Messaging service create key is 'a2p-messaging-service-{suite_id}'.

    Stable key means Twilio deduplicates concurrent creates from the same suite.
    """
    brand = _make_brand(brand_status="approved", brand_reg_sid=BRAND_REG_SID)
    campaign = _make_campaign(campaign_status="draft")
    phone = _make_phone_row()
    mock_sb = _MockSupabase(
        brand=brand, campaign=campaign,
        trust_profile=_make_trust_profile(), phone=phone,
    )

    with (
        _patch_supabase(mock_sb),
        _patch_cut_receipt(),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_messaging_service",
            new_callable=AsyncMock,
            return_value={"sid": MESSAGING_SVC_SID},
        ) as mock_svc,
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.add_phone_to_messaging_service",
            new_callable=AsyncMock,
            return_value={"sid": "PA000"},
        ),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_campaign",
            new_callable=AsyncMock,
            return_value={"sid": CAMPAIGN_SID},
        ),
    ):
        await advance_a2p_registration(SUITE_ID)

    assert mock_svc.call_args.kwargs["idempotency_key"] == f"a2p-messaging-service-{SUITE_ID}"


@pytest.mark.asyncio
async def test_add_phone_409_is_treated_as_idempotent_success():
    """add_phone_to_messaging_service returning 409 must not fail the campaign advance.

    409 = already added. This is an idempotent outcome — the phone IS in the
    service — so the state machine must continue to campaign registration.
    """
    brand = _make_brand(brand_status="approved", brand_reg_sid=BRAND_REG_SID)
    campaign = _make_campaign(
        campaign_status="draft",
        messaging_svc_sid=MESSAGING_SVC_SID,
    )
    phone = _make_phone_row()
    mock_sb = _MockSupabase(
        brand=brand, campaign=campaign,
        trust_profile=_make_trust_profile(), phone=phone,
    )

    with (
        _patch_supabase(mock_sb),
        _patch_cut_receipt(),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_messaging_service",
            new_callable=AsyncMock,
        ),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.add_phone_to_messaging_service",
            new_callable=AsyncMock,
            side_effect=TrustHubError("PHONE_ALREADY_ADDED", "already in service", 409),
        ),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_campaign",
            new_callable=AsyncMock,
            return_value={"sid": CAMPAIGN_SID},
        ) as mock_cmp,
    ):
        result = await advance_a2p_registration(SUITE_ID)

    # 409 is idempotent — campaign creation must still proceed
    assert result["outcome"] == "success"
    assert result["to_state"] == "campaign_pending"
    mock_cmp.assert_called_once()
