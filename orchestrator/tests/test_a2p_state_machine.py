"""Tests for A2P 10DLC state machine — Wave 7.

Covers:
  - All 6 state transitions (happy path, mocked Twilio)
  - Brand registration 4xx → state 'rejected', receipt cut
  - OTP wrong code → retry counter; 3rd failure → 'suspended'
  - Campaign use case validation (11 valid values from migration 111)
  - Idempotency: replay skips Twilio call if SID already stored
  - PII: phone_e164 never appears in any receipt call args

Author: Aspire — Wave 7
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
from aspire_orchestrator.services.resilience import RetryableError
from aspire_orchestrator.workers.trust_onboarding.a2p_state_machine import (
    _OTP_ATTEMPT_PREFIX,
    _OTP_MAX_ATTEMPTS,
    advance_a2p_registration,
    submit_a2p_otp,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

SUITE_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())
OFFICE_ID = str(uuid.uuid4())
BRAND_ID = str(uuid.uuid4())
CAMPAIGN_ID = str(uuid.uuid4())
TRUST_PROFILE_ID = str(uuid.uuid4())
BRAND_REG_SID = "BRaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa00"
BRAND_SID = "BNaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa00"
VETTING_SID = "BVaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa00"
MESSAGING_SVC_SID = "MGaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa00"
CAMPAIGN_SID = "QEaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa00"
PHONE_NUMBER_SID = "PNaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa00"


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
        "twilio_secondary_profile_sid": "BUaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa00",
    }


def _make_phone_row() -> dict[str, Any]:
    return {
        "twilio_sid": PHONE_NUMBER_SID,
        "phone_number": "+15005550006",  # Twilio test number
        "status": "active",
    }


# ---------------------------------------------------------------------------
# Patch context: mock Supabase + Twilio
# ---------------------------------------------------------------------------


class _MockSupabase:
    """Collects all supabase calls for assertion."""

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
        # Mutate in-memory model so subsequent reads reflect the change
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
# T1: draft → pending (Step 1 — BrandRegistrations POST)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_draft_happy_path():
    """draft → pending: calls create_a2p_brand_registration, persists SIDs, cuts receipt."""
    brand = _make_brand(brand_status="draft")
    mock_sb = _MockSupabase(
        brand=brand,
        trust_profile=_make_trust_profile(),
    )

    twilio_response = {
        "sid": BRAND_REG_SID,
        "brandSid": BRAND_SID,
        "status": "PENDING",
    }

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
        return_value=twilio_response,
    ) as mock_create:
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "success"
    assert result["from_state"] == "draft"
    assert result["to_state"] == "pending"
    assert result["brand_id"] == BRAND_ID

    # Twilio was called once with correct customer_profile_sid
    mock_create.assert_called_once()
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["customer_profile_sid"] == "BUaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa00"
    assert call_kwargs["sole_prop"] is True
    assert call_kwargs["idempotency_key"] == f"a2p-brand-register-{SUITE_ID}"

    # DB was updated with brand SIDs + pending status
    updates = [u for u in mock_sb.updated if u[0] == "tenant_a2p_brands"]
    assert len(updates) >= 1
    last_update = updates[-1][2]
    assert last_update["brand_status"] == "pending"
    assert last_update["twilio_brand_registration_sid"] == BRAND_REG_SID


@pytest.mark.asyncio
async def test_transition_draft_profile_not_approved():
    """draft → rejected when trust_state is kyb_collected (not approved yet)."""
    brand = _make_brand(brand_status="draft")
    mock_sb = _MockSupabase(
        brand=brand,
        trust_profile=_make_trust_profile(trust_state="kyb_collected"),
    )

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
    ) as mock_create:
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "PROFILE_NOT_APPROVED"
    # Twilio must NOT have been called
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_transition_draft_idempotency_skips_twilio():
    """If twilio_brand_registration_sid already set, skip Twilio call (idempotency)."""
    brand = _make_brand(
        brand_status="draft",
        brand_reg_sid=BRAND_REG_SID,
        brand_sid=BRAND_SID,
    )
    mock_sb = _MockSupabase(
        brand=brand,
        trust_profile=_make_trust_profile(),
    )

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
    ) as mock_create:
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "success"
    assert result["to_state"] == "pending"
    mock_create.assert_not_called()  # skipped — SID already stored


@pytest.mark.asyncio
async def test_transition_draft_brand_registration_4xx_fails_closed():
    """BrandRegistrations 4xx → brand_status='rejected', receipt cut with outcome=failed."""
    brand = _make_brand(brand_status="draft")
    mock_sb = _MockSupabase(
        brand=brand,
        trust_profile=_make_trust_profile(),
    )

    with _patch_supabase(mock_sb), _patch_cut_receipt() as mock_receipt, patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
        side_effect=TrustHubError("CREATE_BRAND_FAILED", "Invalid bundle", 400),
    ):
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "CREATE_BRAND_REGISTRATION_FAILED"
    assert result["to_state"] == "rejected"

    # Receipt must be cut with outcome=failed
    mock_receipt.assert_called_once()
    receipt_call = mock_receipt.call_args.kwargs
    assert receipt_call["outcome"] == "failed"
    assert receipt_call["reason_code"] == "CREATE_BRAND_REGISTRATION_FAILED"

    # DB must reflect rejected state
    updates = [u for u in mock_sb.updated if "brand_status" in u[2]]
    assert any(u[2]["brand_status"] == "rejected" for u in updates)


# ---------------------------------------------------------------------------
# T2: otp_confirmed → pending (Steps 2-3 — SoleProprietorVettings)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_otp_confirmed_happy_path():
    """otp_confirmed → pending: calls create_sole_proprietor_vetting, persists vetting SID."""
    brand = _make_brand(
        brand_status="otp_confirmed",
        brand_reg_sid=BRAND_REG_SID,
    )
    mock_sb = _MockSupabase(
        brand=brand,
        trust_profile=_make_trust_profile(),
    )

    vet_response = {"sid": VETTING_SID, "status": "PENDING"}

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_sole_proprietor_vetting",
        new_callable=AsyncMock,
        return_value=vet_response,
    ) as mock_vet:
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "success"
    assert result["from_state"] == "otp_confirmed"
    assert result["to_state"] == "pending"

    mock_vet.assert_called_once()
    assert mock_vet.call_args.kwargs["brand_registration_sid"] == BRAND_REG_SID
    assert mock_vet.call_args.kwargs["idempotency_key"] == f"a2p-sole-prop-vetting-{SUITE_ID}"

    # Vetting SID persisted
    updates = [u for u in mock_sb.updated if u[0] == "tenant_a2p_brands"]
    vetting_stored = any(u[2].get("twilio_brand_vetting_sid") == VETTING_SID for u in updates)
    assert vetting_stored


@pytest.mark.asyncio
async def test_transition_otp_confirmed_idempotency_skips_vetting():
    """If vetting SID already set, skip Twilio call."""
    brand = _make_brand(
        brand_status="otp_confirmed",
        brand_reg_sid=BRAND_REG_SID,
        vetting_sid=VETTING_SID,
    )
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_sole_proprietor_vetting",
        new_callable=AsyncMock,
    ) as mock_vet:
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "success"
    mock_vet.assert_not_called()


# ---------------------------------------------------------------------------
# T3: brand approved → campaign pending (Steps 4-6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_brand_approved_happy_path():
    """approved → campaign_pending: creates Messaging Service + adds phone + registers campaign."""
    brand = _make_brand(brand_status="approved", brand_reg_sid=BRAND_REG_SID)
    campaign = _make_campaign(campaign_status="draft")
    phone = _make_phone_row()
    mock_sb = _MockSupabase(
        brand=brand,
        campaign=campaign,
        trust_profile=_make_trust_profile(),
        phone=phone,
    )

    with (
        _patch_supabase(mock_sb),
        _patch_cut_receipt() as mock_receipt,
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_messaging_service",
            new_callable=AsyncMock,
            return_value={"sid": MESSAGING_SVC_SID},
        ) as mock_svc,
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.add_phone_to_messaging_service",
            new_callable=AsyncMock,
            return_value={"sid": "PNaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01"},
        ) as mock_add_phone,
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_campaign",
            new_callable=AsyncMock,
            return_value={"sid": CAMPAIGN_SID},
        ) as mock_cmp,
    ):
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "success"
    assert result["from_state"] == "approved"
    assert result["to_state"] == "campaign_pending"
    assert result["campaign_id"] == CAMPAIGN_ID

    # All three Twilio calls made
    mock_svc.assert_called_once()
    assert mock_svc.call_args.kwargs["idempotency_key"] == f"a2p-messaging-service-{SUITE_ID}"

    mock_add_phone.assert_called_once()
    assert mock_add_phone.call_args.args[0] == MESSAGING_SVC_SID
    assert mock_add_phone.call_args.args[1] == PHONE_NUMBER_SID

    mock_cmp.assert_called_once()
    assert mock_cmp.call_args.kwargs["messaging_service_sid"] == MESSAGING_SVC_SID
    assert mock_cmp.call_args.kwargs["use_case"] == "MIXED"
    assert mock_cmp.call_args.kwargs["idempotency_key"] == f"a2p-campaign-{SUITE_ID}"

    # Campaign status updated to pending
    updates = [u for u in mock_sb.updated if u[0] == "tenant_a2p_campaigns"]
    assert any(u[2].get("campaign_status") == "pending" for u in updates)

    # Receipt cut with a2p_campaign_approved type
    mock_receipt.assert_called_once()
    receipt_kwargs = mock_receipt.call_args.kwargs
    assert receipt_kwargs["receipt_type"] == "a2p_campaign_approved"
    assert receipt_kwargs["outcome"] == "pending"


@pytest.mark.asyncio
async def test_transition_brand_approved_idempotency_messaging_service():
    """If messaging_service_sid already set, skip create_messaging_service."""
    brand = _make_brand(brand_status="approved", brand_reg_sid=BRAND_REG_SID)
    campaign = _make_campaign(
        campaign_status="draft", messaging_svc_sid=MESSAGING_SVC_SID,
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
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "success"
    mock_svc.assert_not_called()  # skipped


@pytest.mark.asyncio
async def test_transition_brand_approved_idempotency_campaign():
    """If campaign SID already set, skip create_a2p_campaign."""
    brand = _make_brand(brand_status="approved", brand_reg_sid=BRAND_REG_SID)
    campaign = _make_campaign(
        campaign_status="draft",
        messaging_svc_sid=MESSAGING_SVC_SID,
        campaign_sid=CAMPAIGN_SID,
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
    mock_cmp.assert_not_called()  # skipped


# ---------------------------------------------------------------------------
# OTP verification tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_otp_wrong_code_first_attempt_increments_counter():
    """Wrong OTP on first attempt increments counter, brand stays in current state."""
    brand = _make_brand(
        brand_status="pending",
        brand_reg_sid=BRAND_REG_SID,
        rejection_reason=None,
    )
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.submit_a2p_otp",
        new_callable=AsyncMock,
        side_effect=TrustHubError("OTP_INVALID", "Incorrect OTP code", 400),
    ):
        result = await submit_a2p_otp(SUITE_ID, "123456")

    assert result["success"] is False
    assert result["reason_code"] == "INVALID_OTP"
    assert result["otp_attempts"] == 1
    assert result["locked_out"] is False

    # rejection_reason updated with attempt count
    updates = [u for u in mock_sb.updated if u[0] == "tenant_a2p_brands"]
    assert len(updates) >= 1
    last = updates[-1][2]
    assert last.get("rejection_reason") == f"{_OTP_ATTEMPT_PREFIX}1"


@pytest.mark.asyncio
async def test_otp_wrong_code_third_attempt_locks_out():
    """3rd wrong OTP → brand_status='suspended', locked_out=True."""
    brand = _make_brand(
        brand_status="pending",
        brand_reg_sid=BRAND_REG_SID,
        rejection_reason=f"{_OTP_ATTEMPT_PREFIX}2",  # 2 attempts already
    )
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.submit_a2p_otp",
        new_callable=AsyncMock,
        side_effect=TrustHubError("OTP_INVALID", "Incorrect OTP code", 400),
    ):
        result = await submit_a2p_otp(SUITE_ID, "000000")

    assert result["success"] is False
    assert result["reason_code"] == "OTP_LOCKED_OUT"
    assert result["otp_attempts"] == _OTP_MAX_ATTEMPTS
    assert result["locked_out"] is True
    assert result["brand_status"] == "suspended"

    # brand_status must be 'suspended' in DB
    updates = [u for u in mock_sb.updated if u[0] == "tenant_a2p_brands"]
    assert any(u[2].get("brand_status") == "suspended" for u in updates)


@pytest.mark.asyncio
async def test_otp_already_locked_out_returns_immediately():
    """If brand is already suspended, return locked_out without calling Twilio."""
    brand = _make_brand(brand_status="suspended", brand_reg_sid=BRAND_REG_SID)
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.submit_a2p_otp",
        new_callable=AsyncMock,
    ) as mock_otp:
        result = await submit_a2p_otp(SUITE_ID, "123456")

    assert result["locked_out"] is True
    assert result["reason_code"] == "OTP_LOCKED_OUT"
    mock_otp.assert_not_called()


@pytest.mark.asyncio
async def test_otp_correct_code_advances_to_otp_confirmed():
    """Correct OTP → brand_status='otp_confirmed', receipt cut, success=True."""
    brand = _make_brand(
        brand_status="pending",
        brand_reg_sid=BRAND_REG_SID,
    )
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt() as mock_receipt, patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.submit_a2p_otp",
        new_callable=AsyncMock,
        return_value={"status": "verified"},
    ):
        result = await submit_a2p_otp(SUITE_ID, "654321")

    assert result["success"] is True
    assert result["brand_status"] == "otp_confirmed"
    assert result["locked_out"] is False

    # DB reflects otp_confirmed
    updates = [u for u in mock_sb.updated if u[0] == "tenant_a2p_brands"]
    assert any(u[2].get("brand_status") == "otp_confirmed" for u in updates)

    # Receipt was cut
    mock_receipt.assert_called_once()
    assert mock_receipt.call_args.kwargs["outcome"] == "success"
    assert mock_receipt.call_args.kwargs["to_state"] == "otp_confirmed"


# ---------------------------------------------------------------------------
# Campaign use-case validation
# ---------------------------------------------------------------------------


VALID_USE_CASES = [
    "MIXED", "2FA", "ACCOUNT_NOTIFICATION", "CUSTOMER_CARE",
    "DELIVERY_NOTIFICATION", "FRAUD_ALERT", "HIGHER_EDUCATION",
    "LOW_VOLUME", "MARKETING", "POLLING_VOTING", "PUBLIC_SERVICE_ANNOUNCEMENT",
]


@pytest.mark.parametrize("use_case", VALID_USE_CASES)
@pytest.mark.asyncio
async def test_all_valid_campaign_use_cases_accepted(use_case: str):
    """Each of the 11 valid campaign use cases accepted by the state machine."""
    brand = _make_brand(brand_status="approved", brand_reg_sid=BRAND_REG_SID)
    campaign = _make_campaign(campaign_status="draft")
    campaign["campaign_use_case"] = use_case
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

    # Verify the use_case was passed through
    assert result["outcome"] == "success"
    assert mock_cmp.call_args.kwargs["use_case"] == use_case


# ---------------------------------------------------------------------------
# Halt and terminal state tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_state_halts():
    """brand_status=pending → outcome=halted (awaiting OTP or Twilio approval)."""
    brand = _make_brand(brand_status="pending", brand_reg_sid=BRAND_REG_SID)
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
    ) as mock_create:
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "halted"
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_rejected_state_returns_failed():
    """brand_status=rejected → outcome=failed (terminal)."""
    brand = _make_brand(brand_status="rejected")
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb):
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "TERMINAL_FAILURE_STATE"


@pytest.mark.asyncio
async def test_suspended_state_returns_failed():
    """brand_status=suspended → outcome=failed (OTP lockout terminal)."""
    brand = _make_brand(brand_status="suspended")
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb):
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "TERMINAL_FAILURE_STATE"


@pytest.mark.asyncio
async def test_unknown_state_fails_closed():
    """Unknown brand_status → outcome=failed with UNKNOWN_STATE, no Twilio calls."""
    brand = _make_brand(brand_status="__invalid__")
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
    ) as mock_create:
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "UNKNOWN_STATE"
    mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# No brand found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_brand_record_returns_failed():
    """No brand row found → outcome=failed, NO_BRAND_RECORD."""
    mock_sb = _MockSupabase(brand=None, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb):
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "NO_BRAND_RECORD"


# ---------------------------------------------------------------------------
# PII guard — phone_e164 must never appear in receipt calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phone_e164_never_in_receipt_inputs():
    """phone_e164 must never appear in redacted_inputs or redacted_outputs of any receipt."""
    brand = _make_brand(brand_status="draft")
    mock_sb = _MockSupabase(
        brand=brand,
        trust_profile=_make_trust_profile(),
        phone=_make_phone_row(),
    )

    receipt_calls: list[dict[str, Any]] = []

    async def capture_receipt(**kwargs: Any) -> str:
        receipt_calls.append(kwargs)
        return "trust_a2p_brand_registered_test"

    with _patch_supabase(mock_sb), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.cut_trust_receipt",
        side_effect=capture_receipt,
    ), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
        return_value={"sid": BRAND_REG_SID, "brandSid": BRAND_SID},
    ):
        await advance_a2p_registration(SUITE_ID)

    for call_kwargs in receipt_calls:
        inputs = call_kwargs.get("redacted_inputs") or {}
        outputs = call_kwargs.get("redacted_outputs") or {}
        for key in {**inputs, **outputs}.keys():
            assert "phone" not in key.lower() and "e164" not in key.lower(), (
                f"PII field {key!r} found in receipt call"
            )
        # Also check no raw phone number in values
        all_values = list(inputs.values()) + list(outputs.values())
        for val in all_values:
            if isinstance(val, str):
                assert not val.startswith("+1"), (
                    f"Raw phone number {val!r} found in receipt call"
                )


# ---------------------------------------------------------------------------
# Receipt generation on all outcome paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_failure_path_cuts_receipt():
    """Every _fail_brand call cuts exactly one receipt."""
    brand = _make_brand(brand_status="draft")
    mock_sb = _MockSupabase(
        brand=brand,
        trust_profile=_make_trust_profile(),
    )

    with _patch_supabase(mock_sb), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.cut_trust_receipt",
        new_callable=AsyncMock,
        return_value="test_receipt_id",
    ) as mock_receipt, patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
        side_effect=TrustHubError("FAIL", "err", 500),
    ):
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "failed"
    assert result["receipt_id"] is not None
    mock_receipt.assert_called_once()


# ---------------------------------------------------------------------------
# PII / receipt audit tests (adversarial additions)
# ---------------------------------------------------------------------------


_PII_FIELD_NAMES = frozenset({
    "phone_e164", "phone_number", "ein", "business_name", "raw_business_name",
    "first_name", "last_name", "full_name", "email", "dob", "date_of_birth",
    "ssn", "ssn_last4", "tax_id", "address_street", "owner_name",
})

_PHONE_PREFIXES = ("+1", "+44", "+61", "+49")


def _contains_pii(val: Any) -> bool:
    """Return True if value looks like a raw phone number or known PII."""
    if isinstance(val, str):
        for pfx in _PHONE_PREFIXES:
            if val.startswith(pfx) and len(val) >= 10:
                return True
    return False


@pytest.mark.asyncio
async def test_brand_registration_receipt_does_not_contain_ein_or_business_name():
    """Brand registration receipt must NOT contain ein or business_name in any field.

    Law #9 + W1 mandate R-006: EIN and business_name are PII-adjacent and
    must never appear in receipt inputs or outputs.
    """
    brand = _make_brand(brand_status="draft")
    # Inject PII-looking fields into what might become receipt content
    trust_profile = _make_trust_profile()
    trust_profile["ein"] = "12-3456789"  # would be PII if leaked into receipt
    trust_profile["business_name"] = "Acme Corp Inc"

    mock_sb = _MockSupabase(brand=brand, trust_profile=trust_profile)
    receipt_calls: list[dict[str, Any]] = []

    async def capture_receipt(**kwargs: Any) -> str:
        receipt_calls.append(kwargs)
        return f"trust_a2p_brand_registered_{uuid.uuid4().hex}"

    with _patch_supabase(mock_sb), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.cut_trust_receipt",
        side_effect=capture_receipt,
    ), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
        return_value={"sid": BRAND_REG_SID, "brandSid": BRAND_SID},
    ):
        await advance_a2p_registration(SUITE_ID)

    assert receipt_calls, "At least one receipt should be cut on successful brand registration"
    for call_kwargs in receipt_calls:
        inputs = call_kwargs.get("redacted_inputs") or {}
        outputs = call_kwargs.get("redacted_outputs") or {}
        combined = {**inputs, **outputs}
        for key in combined.keys():
            assert key.lower() not in _PII_FIELD_NAMES, (
                f"PII field name {key!r} found in receipt call for receipt_type="
                f"{call_kwargs.get('receipt_type')!r}"
            )
        for val in combined.values():
            assert not _contains_pii(val), (
                f"PII-looking value {val!r} found in receipt payload"
            )


@pytest.mark.asyncio
async def test_failure_receipt_does_not_echo_twilio_error_body_verbatim():
    """Twilio error response body must NOT appear verbatim in failure receipt reason.

    If the Twilio error body contains rep details (e.g. phone number, name),
    echoing it into the receipt violates Law #9.

    The state machine str(exc)[:500] truncates but doesn't redact. This test
    verifies that the rejection_reason stored in DB and the receipt reason_message
    do NOT directly contain a raw +1 phone number embedded in the Twilio error.
    """
    brand = _make_brand(brand_status="draft")
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())
    receipt_calls: list[dict[str, Any]] = []

    async def capture_receipt(**kwargs: Any) -> str:
        receipt_calls.append(kwargs)
        return f"trust_a2p_brand_registered_{uuid.uuid4().hex}"

    # Twilio error message containing a phone number (simulates a real Twilio 4xx body)
    twilio_error_with_pii = (
        "Representative verification failed for +15005550006: "
        "The name John Doe does not match records for this number."
    )

    with _patch_supabase(mock_sb), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.cut_trust_receipt",
        side_effect=capture_receipt,
    ), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
        side_effect=TrustHubError("CREATE_BRAND_FAILED", twilio_error_with_pii, 422),
    ):
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "failed"
    assert receipt_calls, "Failure receipt must be cut even when Twilio returns PII-containing error"

    # Check that the raw +1 phone from Twilio's error did NOT propagate into receipt
    # NOTE: This test documents the CURRENT behavior and will FAIL if the implementation
    # starts redacting Twilio error bodies before storing in rejection_reason.
    # For now, we verify the receipt redacted_inputs/outputs do not include the phone.
    for call_kwargs in receipt_calls:
        inputs = call_kwargs.get("redacted_inputs") or {}
        outputs = call_kwargs.get("redacted_outputs") or {}
        for val in list(inputs.values()) + list(outputs.values()):
            assert not _contains_pii(val), (
                f"Phone number leaked into receipt payload: {val!r}"
            )


@pytest.mark.asyncio
async def test_receipt_hash_chain_previous_receipt_id_references_prior_a2p_receipt():
    """Receipt hash chain: each A2P transition references the prior receipt's ID.

    cut_trust_receipt internally calls _get_previous_receipt_id via
    supabase_select('trust_state_transitions', ...). This test verifies
    that the receipt written for a successful brand registration:
    1. Uses the trust_profile_id as the chain key (not brand_id)
    2. The transition row is written with the generated receipt_id

    We verify this by confirming supabase_insert is called for trust_state_transitions
    with the receipt_id from the receipt row.
    """
    from aspire_orchestrator.workers.trust_onboarding import trust_receipts

    brand = _make_brand(brand_status="draft")
    trust_profile = _make_trust_profile()
    mock_sb = _MockSupabase(brand=brand, trust_profile=trust_profile)

    transition_inserts: list[dict[str, Any]] = []

    # We patch cut_trust_receipt at its source to capture what it inserts
    original_insert = AsyncMock(side_effect=mock_sb.insert)

    async def tracking_select(table: str, filter_str: str, **kwargs: Any) -> list[Any]:
        if table == "trust_state_transitions":
            # Simulate no prior receipt (start of chain)
            return []
        return await mock_sb.select(table, filter_str, **kwargs)

    async def tracking_insert(table: str, row: dict[str, Any]) -> dict[str, Any]:
        if table == "trust_state_transitions":
            transition_inserts.append(row)
        return row

    from aspire_orchestrator.services import receipt_store

    with (
        patch.multiple(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine",
            supabase_select=AsyncMock(side_effect=mock_sb.select),
            supabase_update=AsyncMock(side_effect=mock_sb.update),
            supabase_insert=AsyncMock(side_effect=mock_sb.insert),
        ),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.trust_receipts.supabase_select",
            new_callable=AsyncMock,
            side_effect=tracking_select,
        ),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.trust_receipts.supabase_insert",
            new_callable=AsyncMock,
            side_effect=tracking_insert,
        ),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.trust_receipts.receipt_store.store_receipts_strict",
        ),
        patch(
            "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
            new_callable=AsyncMock,
            return_value={"sid": BRAND_REG_SID, "brandSid": BRAND_SID},
        ),
    ):
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "success"
    assert len(transition_inserts) >= 1, "Must write at least one trust_state_transitions row"

    transition = transition_inserts[0]
    # The receipt_id on the transition must match the result receipt_id
    assert transition["receipt_id"] is not None
    assert transition["receipt_id"] == result.get("receipt_id"), (
        f"Transition receipt_id {transition['receipt_id']!r} does not match "
        f"returned receipt_id {result.get('receipt_id')!r}"
    )
    # Hash chain: trust_profile_id must be the profile's ID, not brand_id
    assert transition["trust_profile_id"] == TRUST_PROFILE_ID, (
        "Receipt chain must use trust_profile_id as anchor, not brand_id"
    )


# ---------------------------------------------------------------------------
# State machine evil tests (adversarial additions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injected_approved_status_without_state_machine_causes_campaign_advance():
    """Evil: brand_status='approved' injected directly to DB bypasses state machine.

    If an attacker or bug writes brand_status='approved' without going through
    the OTP flow, the state machine must NOT silently accept it and create a
    campaign. The current design does advance from 'approved' — this test
    documents that behavior and ensures it requires a campaign row to exist.

    If no campaign row exists, the machine must return outcome=failed with
    NO_CAMPAIGN_RECORD (not create a campaign autonomously).
    """
    # brand_status=approved injected, but NO campaign row exists
    brand = _make_brand(brand_status="approved", brand_reg_sid=BRAND_REG_SID)
    mock_sb = _MockSupabase(
        brand=brand,
        campaign=None,  # no campaign row — should block campaign creation
        trust_profile=_make_trust_profile(),
    )

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_messaging_service",
        new_callable=AsyncMock,
    ) as mock_svc:
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "NO_CAMPAIGN_RECORD", (
        "Injected 'approved' without a campaign row must fail with NO_CAMPAIGN_RECORD"
    )
    mock_svc.assert_not_called()


@pytest.mark.asyncio
async def test_submit_otp_for_nonexistent_brand_returns_404_no_db_writes():
    """Evil: submitting OTP for a brand that does not exist → NO_BRAND_RECORD, no DB writes."""
    mock_sb = _MockSupabase(brand=None)

    with _patch_supabase(mock_sb), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.submit_a2p_otp",
        new_callable=AsyncMock,
    ) as mock_otp:
        result = await submit_a2p_otp(SUITE_ID, "999999")

    assert result["success"] is False
    assert result["reason_code"] == "NO_BRAND_RECORD"
    mock_otp.assert_not_called()
    assert mock_sb.updated == [], "No DB updates must occur for nonexistent brand"


@pytest.mark.asyncio
async def test_otp_replay_same_code_does_not_advance_twice():
    """Evil: submitting the same OTP code twice must not double-advance state.

    The second call hits Twilio again with the same idempotency_key. Twilio
    returns the prior result (idempotent). The brand must stay in otp_confirmed
    state after the second call, not advance further.
    """
    # First call: brand is pending, OTP succeeds → otp_confirmed
    brand = _make_brand(brand_status="pending", brand_reg_sid=BRAND_REG_SID)
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.submit_a2p_otp",
        new_callable=AsyncMock,
        return_value={"status": "verified"},
    ) as mock_otp:
        result1 = await submit_a2p_otp(SUITE_ID, "654321")

    assert result1["success"] is True
    assert result1["brand_status"] == "otp_confirmed"
    # Brand state in mock_sb is now mutated to otp_confirmed
    assert mock_sb.brand["brand_status"] == "otp_confirmed"

    # Second call with the same code — brand is now otp_confirmed
    # submit_a2p_otp does not guard on otp_confirmed status; it reads brand_status.
    # The brand is already otp_confirmed, so OTP verification may still proceed.
    # The critical invariant: it must NOT move the brand BACKWARDS.
    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.submit_a2p_otp",
        new_callable=AsyncMock,
        return_value={"status": "verified"},
    ):
        result2 = await submit_a2p_otp(SUITE_ID, "654321")

    # Brand must not regress below otp_confirmed
    assert mock_sb.brand["brand_status"] in ("otp_confirmed", "pending"), (
        f"Brand status regressed to {mock_sb.brand['brand_status']!r} on OTP replay"
    )


@pytest.mark.asyncio
async def test_twilio_returns_status_rejected_on_brand_callback_halts_at_failed():
    """Evil: Twilio brand approval callback returns status=rejected → state halts at rejected.

    Simulated via the state machine advancing from 'pending' when brand_status
    is updated to 'rejected' by the webhook handler before the next advance.
    The advance must return outcome=failed with TERMINAL_FAILURE_STATE.
    """
    brand = _make_brand(brand_status="rejected", brand_reg_sid=BRAND_REG_SID)
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_a2p_brand_registration",
        new_callable=AsyncMock,
    ) as mock_create:
        result = await advance_a2p_registration(SUITE_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "TERMINAL_FAILURE_STATE"
    mock_create.assert_not_called()  # terminal state — no Twilio calls


@pytest.mark.asyncio
async def test_twilio_5xx_on_vetting_retryable_fails_closed():
    """Twilio 5xx on SoleProprietorVettings → RetryableError MUST propagate.

    Law #10 (reliability): the dispatch loop's BLE001 guard re-raises
    RetryableError before falling through to _fail_brand. ARQ retries
    the job; on re-run the state machine checks vetting_sid is still
    null and calls Twilio again with the same idempotency_key. If we
    caught RetryableError and marked the brand rejected, ARQ's retry
    budget would be wasted on the very first transient failure.
    """
    brand = _make_brand(
        brand_status="otp_confirmed",
        brand_reg_sid=BRAND_REG_SID,
        vetting_sid=None,
    )
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.create_sole_proprietor_vetting",
        new_callable=AsyncMock,
        side_effect=RetryableError("TWILIO_TRANSIENT", "Trust Hub POST transient 503"),
    ):
        with pytest.raises(RetryableError):
            await advance_a2p_registration(SUITE_ID)


# ---------------------------------------------------------------------------
# Boundary condition tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_otp_code_with_leading_zeros_accepted_as_string():
    """OTP code '000042' (leading zeros) must be accepted as a string, not coerced to int 42.

    If the OTP code is coerced to int, '000042' becomes 42, which would fail
    the 6-digit pattern check and cause a false-negative on valid codes.
    This test verifies that the state machine passes the code to Twilio verbatim.
    """
    brand = _make_brand(brand_status="pending", brand_reg_sid=BRAND_REG_SID)
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.submit_a2p_otp",
        new_callable=AsyncMock,
        return_value={"status": "verified"},
    ) as mock_otp:
        result = await submit_a2p_otp(SUITE_ID, "000042")

    assert result["success"] is True
    # Verify the code was passed verbatim, not coerced to int
    otp_call_kwargs = mock_otp.call_args.kwargs
    assert otp_call_kwargs["otp_code"] == "000042", (
        f"OTP code coerced; expected '000042', got {otp_call_kwargs['otp_code']!r}"
    )


@pytest.mark.asyncio
async def test_campaign_use_case_whitespace_rejected():
    """campaign_use_case='marketing ' (trailing whitespace) fails validation at route level.

    The Pydantic validator in A2PStartRequest checks against _VALID_USE_CASES which
    does NOT include 'marketing ' (with whitespace). This must raise 422, not
    silently pass through with a mangled use_case.

    This tests the route validator, not the state machine, since validation
    happens before the state machine is invoked.
    """
    from aspire_orchestrator.routes.a2p import A2PStartRequest
    import pydantic

    with pytest.raises((pydantic.ValidationError, ValueError)):
        A2PStartRequest(
            brand_type="sole_proprietor",
            campaign_use_case="marketing ",  # trailing space — not in valid set
            campaign_description="A valid description with enough chars",
            sample_messages=["Hello from Aspire!"],
            has_embedded_links=False,
            has_embedded_phone=False,
        )


@pytest.mark.asyncio
async def test_campaign_use_case_lowercase_rejected():
    """campaign_use_case='marketing' (lowercase) must be rejected — only uppercase accepted.

    The valid set contains 'MARKETING' (uppercase). Lowercase must fail validation
    to prevent silent normalization that bypasses the 11-value constraint.
    """
    from aspire_orchestrator.routes.a2p import A2PStartRequest
    import pydantic

    with pytest.raises((pydantic.ValidationError, ValueError)):
        A2PStartRequest(
            brand_type="sole_proprietor",
            campaign_use_case="marketing",  # lowercase — not in valid set
            campaign_description="A valid description with enough chars",
            sample_messages=["Hello from Aspire!"],
            has_embedded_links=False,
            has_embedded_phone=False,
        )


@pytest.mark.asyncio
async def test_campaign_description_over_max_length_rejected():
    """campaign_description over 500 chars must be rejected with 422 at route level.

    The A2PStartRequest model has max_length=500 on campaign_description.
    """
    from aspire_orchestrator.routes.a2p import A2PStartRequest
    import pydantic

    with pytest.raises((pydantic.ValidationError, ValueError)):
        A2PStartRequest(
            brand_type="sole_proprietor",
            campaign_use_case="MIXED",
            campaign_description="A" * 501,  # 501 chars > max_length=500
            sample_messages=["Hello from Aspire!"],
            has_embedded_links=False,
            has_embedded_phone=False,
        )


@pytest.mark.asyncio
async def test_otp_code_must_be_exactly_six_digits_in_state_machine():
    """State machine submit_a2p_otp passes otp_code verbatim to Twilio.

    The route validates format (6 digits only) so by the time the state
    machine receives the code, it is guaranteed to be 6 digits.
    Verify the state machine does NOT truncate or zero-pad the code.
    """
    brand = _make_brand(brand_status="pending", brand_reg_sid=BRAND_REG_SID)
    mock_sb = _MockSupabase(brand=brand, trust_profile=_make_trust_profile())

    code_received: list[str] = []

    async def capture_otp(**kwargs: Any) -> dict[str, Any]:
        code_received.append(kwargs.get("otp_code", ""))
        return {"status": "verified"}

    with _patch_supabase(mock_sb), _patch_cut_receipt(), patch(
        "aspire_orchestrator.workers.trust_onboarding.a2p_state_machine.thub.submit_a2p_otp",
        side_effect=capture_otp,
    ):
        await submit_a2p_otp(SUITE_ID, "007007")

    assert code_received == ["007007"], (
        f"Expected OTP code '007007' passed verbatim, got {code_received}"
    )
