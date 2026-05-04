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
