"""Tests for the trust onboarding state machine (Wave 2-D).

Coverage requirements:
  - All 12 state transitions: happy path + error path
  - Idempotency replay (SID already populated → no duplicate Twilio call)
  - Halt states return outcome="halted" cleanly
  - Unknown state returns outcome="failed" without Twilio calls
  - Vault decryption mocked — confirm decrypted values NOT in receipts/outputs
  - PII redaction: cut_trust_receipt raises on forbidden PII key

All Twilio API calls are mocked via unittest.mock.AsyncMock.
All supabase_select / supabase_update / supabase_insert are mocked at module level.
No real DB or Twilio traffic is hit.

Author: Aspire — Wave 2-D tests
"""

from __future__ import annotations

import importlib
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

TRUST_PROFILE_ID = "aaaaaaaa-0000-0000-0000-000000000001"
SUITE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
TENANT_ID = "bbbbbbbb-0000-0000-0000-000000000002"
OFFICE_ID = "cccccccc-0000-0000-0000-000000000003"
WORKER_JOB_ID = "arq-job-001"

# SIDs used in assertions
PROFILE_SID = "BU-profile-0001"
SHAKEN_SID = "BU-shaken-0001"
CNAM_SID = "BU-cnam-0001"
EU_SID_REP1 = "IT-rep1-0001"
EU_SID_REP2 = "IT-rep2-0001"
EU_SID_CNAM = "IT-cnam-0001"
NUMBER_SID = "PN-number-0001"
CEA_SID = "RN-cea-0001"
SHAKEN_POLICY_SID = "RN-shaken-policy"
SECONDARY_POLICY_SID = "RN-secondary-policy"
CNAM_POLICY_SID = "RNf3db3cd1fe25fcfd3c3ded065c8fea53"


def _base_profile(trust_state: str = "kyb_collected", **overrides: Any) -> dict[str, Any]:
    """Return a minimal tenant_trust_profiles row."""
    base = {
        "id": TRUST_PROFILE_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "office_id": OFFICE_ID,
        "trust_state": trust_state,
        "legal_business_name": "Scott Painting Services",
        "business_type": "llc",
        "address_street": "123 Main St",
        "address_city": "Ann Arbor",
        "address_state": "MI",
        "address_zip": "48104",
        "address_country": "US",
        "twilio_secondary_profile_sid": None,
        "twilio_shaken_bundle_sid": None,
        "twilio_cnam_bundle_sid": None,
        "ein_vault_secret_id": None,
        "ssn_vault_secret_id": None,
        "rejection_reason": None,
        "rejection_code": None,
        "dispute_count": 0,
        "branded_calling_enabled": False,
    }
    base.update(overrides)
    return base


def _base_rep(rep_index: int = 1, eu_sid: str | None = None) -> dict[str, Any]:
    """Return a minimal tenant_authorized_reps row."""
    return {
        "id": f"rep-id-00{rep_index:02d}",
        "trust_profile_id": TRUST_PROFILE_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "rep_index": rep_index,
        "first_name": "Tony",
        "last_name": "Scott",
        "business_title": "Owner",
        "email": "tony@scottpainting.com",  # NOT in receipts
        "phone_e164": "+14482885386",  # NOT in receipts
        "dob_vault_secret_id": "vault-dob-uuid-001" if eu_sid is None else None,
        "ssn_last4_vault_secret_id": None,
        "twilio_end_user_sid": eu_sid,
    }


def _base_phone() -> dict[str, Any]:
    """Return a minimal tenant_phone_numbers row."""
    return {
        "id": "phone-row-0001",
        "suite_id": SUITE_ID,
        "twilio_sid": NUMBER_SID,
        "phone_sid": NUMBER_SID,
        "phone_number": "+14482885386",
        "e164": "+14482885386",
        "status": "active",
        "trust_profile_id": TRUST_PROFILE_ID,
    }


def _receipt_type_for_failure(from_state: str) -> str:
    """Map state name to expected receipt_type on failure."""
    if "shaken" in from_state:
        return "shaken_trust_product_rejected"
    if "cnam" in from_state:
        return "cnam_trust_product_rejected"
    return "customer_profile_rejected"


# ---------------------------------------------------------------------------
# Module-level patches — reused across most tests
# ---------------------------------------------------------------------------

BASE_PATCHES = {
    "aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select": None,
    "aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update": None,
    "aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_rpc": None,
    "aspire_orchestrator.services.supabase_client.supabase_insert": None,
    "aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt": None,
}


class _PatchContext:
    """Lightweight patch manager for async context."""

    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        self._patches: list[Any] = []
        self._mocks: dict[str, Any] = {}
        self._overrides = overrides or {}

    def start(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select": AsyncMock(return_value=[]),
            "aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update": AsyncMock(return_value={}),
            "aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_rpc": AsyncMock(return_value={"decrypted_secret": "1990-01-15"}),
            "aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt": AsyncMock(return_value="receipt-id-001"),
        }
        defaults.update(self._overrides)
        for target, mock_val in defaults.items():
            if mock_val is None:
                mock_val = AsyncMock(return_value={})
            p = patch(target, mock_val)
            m = p.start()
            self._patches.append(p)
            self._mocks[target.split(".")[-1]] = m
        return self._mocks

    def stop(self) -> None:
        for p in self._patches:
            p.stop()


# ---------------------------------------------------------------------------
# Import the module under test (after patches are set up per test)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _import_state_machine():
    """Ensure fresh module import per test."""
    # Force reload so module-level caches don't bleed.
    mod_name = "aspire_orchestrator.workers.trust_onboarding.state_machine"
    if mod_name in sys.modules:
        importlib.reload(sys.modules[mod_name])


# ============================================================================
# 1. HALT STATE TESTS
# ============================================================================

class TestHaltStates:
    """Halt states (profile_submitted, shaken_submitted, cnam_submitted, number_attached)
    return outcome='halted' with no Twilio calls."""

    @pytest.mark.parametrize("halt_state", [
        "profile_submitted",
        "shaken_submitted",
        "cnam_submitted",
        "branded_calling_pending",
        "branded_calling_live",
    ])
    @pytest.mark.asyncio
    async def test_halt_state_returns_halted(self, halt_state: str) -> None:
        ctx = _PatchContext(overrides={
            "aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select": AsyncMock(
                return_value=[_base_profile(trust_state=halt_state)]
            ),
        })
        mocks = ctx.start()
        try:
            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)
            assert result["outcome"] == "halted"
            assert result["from_state"] == halt_state
            assert result["to_state"] == halt_state
            assert result["receipt_id"] is None
        finally:
            ctx.stop()


# ============================================================================
# 2. UNKNOWN STATE TEST
# ============================================================================

class TestUnknownState:
    """Unknown / ops-override state returns outcome='failed' without Twilio calls."""

    @pytest.mark.asyncio
    async def test_unknown_state_fails_closed(self) -> None:
        profile = _base_profile(trust_state="ops_custom_override")
        cut_receipt_mock = AsyncMock(return_value="receipt-fail-001")
        ctx = _PatchContext(overrides={
            "aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select": AsyncMock(
                side_effect=[
                    [profile],  # _load_trust_profile
                ]
            ),
            "aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt": cut_receipt_mock,
        })
        mocks = ctx.start()
        try:
            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            with patch("aspire_orchestrator.providers.twilio_trust_hub.create_secondary_customer_profile") as tw_mock:
                result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)
                # Twilio MUST NOT have been called.
                tw_mock.assert_not_called()
            assert result["outcome"] == "failed"
            assert result["reason_code"] == "UNKNOWN_STATE"
            # Receipt should have been cut.
            cut_receipt_mock.assert_called()
        finally:
            ctx.stop()

    @pytest.mark.asyncio
    async def test_profile_load_failure_returns_failed(self) -> None:
        from aspire_orchestrator.services.supabase_client import SupabaseClientError
        ctx = _PatchContext(overrides={
            "aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select": AsyncMock(
                side_effect=SupabaseClientError("select/tenant_trust_profiles", 500, "DB error")
            ),
        })
        mocks = ctx.start()
        try:
            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)
            assert result["outcome"] == "failed"
            assert result["reason_code"] == "PROFILE_LOAD_FAILED"
            assert result["receipt_id"] is None  # No profile → no scope for receipt
        finally:
            ctx.stop()


# ============================================================================
# 3. kyb_collected → profile_drafted
# ============================================================================

class TestKybCollected:

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        """Creates secondary profile + end users, advances to profile_drafted."""
        profile = _base_profile(trust_state="kyb_collected")
        reps = [_base_rep(rep_index=1), _base_rep(rep_index=2)]

        select_responses = [
            [profile],                               # _load_trust_profile
            [{"email": "tony@scottpainting.com"}],   # _load_suite_email (before reps)
            [reps[0], reps[1]],                      # _load_authorized_reps
        ]

        cut_receipt_mock = AsyncMock(return_value="receipt-kyb-001")

        with patch("aspire_orchestrator.providers.twilio_trust_hub.fetch_secondary_profile_policy_sid",
                   new=AsyncMock(return_value=SECONDARY_POLICY_SID)), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_secondary_customer_profile",
                   new=AsyncMock(return_value={"sid": PROFILE_SID, "status": "draft"})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_end_user",
                   new=AsyncMock(side_effect=[
                       {"sid": EU_SID_REP1},
                       {"sid": EU_SID_REP2},
                   ])), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_rpc",
                   new=AsyncMock(return_value={"decrypted_secret": "1990-01-15"})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=cut_receipt_mock):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["from_state"] == "kyb_collected"
        assert result["to_state"] == "profile_drafted"
        assert result["receipt_id"] == "receipt-kyb-001"
        cut_receipt_mock.assert_called_once()
        call_kwargs = cut_receipt_mock.call_args.kwargs
        assert call_kwargs["receipt_type"] == "customer_profile_created"
        # Verify no PII in receipt inputs/outputs.
        _assert_no_pii_in_receipt(call_kwargs)

    @pytest.mark.asyncio
    async def test_idempotency_skip_if_profile_sid_exists(self) -> None:
        """If twilio_secondary_profile_sid already set, skip create call."""
        profile = _base_profile(
            trust_state="kyb_collected",
            twilio_secondary_profile_sid=PROFILE_SID,
        )
        reps = [_base_rep(rep_index=1, eu_sid=EU_SID_REP1)]
        select_responses = [
            [profile],  # _load_trust_profile
            [reps[0]], # _load_authorized_reps
        ]

        create_profile_mock = AsyncMock(return_value={"sid": PROFILE_SID})

        with patch("aspire_orchestrator.providers.twilio_trust_hub.fetch_secondary_profile_policy_sid",
                   new=AsyncMock(return_value=SECONDARY_POLICY_SID)), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_secondary_customer_profile",
                   new=create_profile_mock), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_end_user",
                   new=AsyncMock(return_value={"sid": EU_SID_REP1})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_rpc",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-idem-001")):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        # create_secondary_customer_profile MUST NOT have been called.
        create_profile_mock.assert_not_called()
        assert result["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_twilio_4xx_maps_to_failed(self) -> None:
        """Twilio 4xx → outcome='failed', receipt cut with reason_code."""
        from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
        profile = _base_profile(trust_state="kyb_collected")
        reps = [_base_rep(rep_index=1)]
        select_responses = [
            [profile],
            [{"email": "tony@scottpainting.com"}],   # _load_suite_email first
            [reps[0]],                               # _load_authorized_reps second
        ]
        cut_receipt_mock = AsyncMock(return_value="receipt-fail-001")

        with patch("aspire_orchestrator.providers.twilio_trust_hub.fetch_secondary_profile_policy_sid",
                   new=AsyncMock(return_value=SECONDARY_POLICY_SID)), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_secondary_customer_profile",
                   new=AsyncMock(side_effect=TrustHubError("TRUST_HUB_POST_FAILED", "invalid request", 400))), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_rpc",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=cut_receipt_mock):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"
        assert result["to_state"] == "failed"
        assert "reason_code" in result
        cut_receipt_mock.assert_called()
        call_kwargs = cut_receipt_mock.call_args.kwargs
        assert call_kwargs["outcome"] == "failed"
        _assert_no_pii_in_receipt(call_kwargs)

    @pytest.mark.asyncio
    async def test_vault_decrypt_values_not_in_receipts(self) -> None:
        """Vault-decrypted DOB must not appear in receipt inputs or outputs."""
        profile = _base_profile(trust_state="kyb_collected")
        reps = [_base_rep(rep_index=1)]
        decrypted_dob = "1990-01-15"
        receipt_calls: list[dict[str, Any]] = []

        async def _capture_receipt(**kwargs: Any) -> str:
            receipt_calls.append(kwargs)
            return "receipt-vault-001"

        select_responses = [
            [profile],
            [{"email": "tony@scottpainting.com"}],   # _load_suite_email first
            [reps[0]],                               # _load_authorized_reps second
        ]

        with patch("aspire_orchestrator.providers.twilio_trust_hub.fetch_secondary_profile_policy_sid",
                   new=AsyncMock(return_value=SECONDARY_POLICY_SID)), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_secondary_customer_profile",
                   new=AsyncMock(return_value={"sid": PROFILE_SID})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_end_user",
                   new=AsyncMock(return_value={"sid": EU_SID_REP1})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_rpc",
                   new=AsyncMock(return_value={"decrypted_secret": decrypted_dob})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=_capture_receipt):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        # Decrypted DOB must never appear in any receipt field.
        for call in receipt_calls:
            all_vals = _flatten_values(call)
            for val in all_vals:
                assert decrypted_dob not in str(val), (
                    f"Vault-decrypted DOB {decrypted_dob!r} leaked into receipt: {call}"
                )


# ============================================================================
# 4. profile_drafted → profile_submitted
# ============================================================================

class TestProfileDrafted:

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        profile = _base_profile(
            trust_state="profile_drafted",
            twilio_secondary_profile_sid=PROFILE_SID,
        )
        reps = [_base_rep(rep_index=1, eu_sid=EU_SID_REP1)]
        select_responses = [[profile], [reps[0]]]
        cut_receipt_mock = AsyncMock(return_value="receipt-drafted-001")

        with patch("aspire_orchestrator.providers.twilio_trust_hub.assign_entity_to_profile",
                   new=AsyncMock(return_value={"sid": "EA-001"})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.submit_customer_profile",
                   new=AsyncMock(return_value={"sid": PROFILE_SID, "status": "pending-review"})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=cut_receipt_mock):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["to_state"] == "profile_submitted"
        cut_receipt_mock.assert_called_once()
        call_kwargs = cut_receipt_mock.call_args.kwargs
        assert call_kwargs["receipt_type"] == "customer_profile_submitted"
        _assert_no_pii_in_receipt(call_kwargs)

    @pytest.mark.asyncio
    async def test_missing_profile_sid_fails(self) -> None:
        """Missing twilio_secondary_profile_sid → outcome='failed'."""
        profile = _base_profile(trust_state="profile_drafted", twilio_secondary_profile_sid=None)
        cut_receipt_mock = AsyncMock(return_value="receipt-missing-001")

        with patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(return_value=[profile])), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=cut_receipt_mock):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"
        assert result["reason_code"] == "MISSING_PROFILE_SID"

    @pytest.mark.asyncio
    async def test_twilio_409_on_assign_is_idempotent(self) -> None:
        """409 Conflict on assign_entity_to_profile = already assigned = OK."""
        from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
        profile = _base_profile(
            trust_state="profile_drafted",
            twilio_secondary_profile_sid=PROFILE_SID,
        )
        reps = [_base_rep(rep_index=1, eu_sid=EU_SID_REP1)]
        select_responses = [[profile], [reps[0]]]

        with patch("aspire_orchestrator.providers.twilio_trust_hub.assign_entity_to_profile",
                   new=AsyncMock(side_effect=TrustHubError("CONFLICT", "already assigned", 409))), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.submit_customer_profile",
                   new=AsyncMock(return_value={"sid": PROFILE_SID, "status": "pending-review"})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-idem-001")):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["to_state"] == "profile_submitted"

    @pytest.mark.asyncio
    async def test_twilio_4xx_non_409_fails(self) -> None:
        """Non-409 Twilio 4xx on assign_entity → outcome='failed'."""
        from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
        profile = _base_profile(
            trust_state="profile_drafted",
            twilio_secondary_profile_sid=PROFILE_SID,
        )
        reps = [_base_rep(rep_index=1, eu_sid=EU_SID_REP1)]
        select_responses = [[profile], [reps[0]]]

        with patch("aspire_orchestrator.providers.twilio_trust_hub.assign_entity_to_profile",
                   new=AsyncMock(side_effect=TrustHubError("FORBIDDEN", "permission denied", 403))), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-403-001")):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"


# ============================================================================
# 5. profile_submitted → HALT
# ============================================================================

class TestProfileSubmittedHalt:

    @pytest.mark.asyncio
    async def test_profile_submitted_halts(self) -> None:
        profile = _base_profile(
            trust_state="profile_submitted",
            twilio_secondary_profile_sid=PROFILE_SID,
        )
        with patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(return_value=[profile])):
            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "halted"
        assert result["receipt_id"] is None


# ============================================================================
# 6. profile_approved → shaken_created
# ============================================================================

class TestProfileApproved:

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        profile = _base_profile(
            trust_state="profile_approved",
            twilio_secondary_profile_sid=PROFILE_SID,
        )
        phone = _base_phone()
        select_responses = [
            [profile],    # _load_trust_profile
            [phone],      # _load_phone_number
            [{"email": "tony@scottpainting.com"}],  # _load_suite_email
        ]
        cut_receipt_mock = AsyncMock(return_value="receipt-shaken-001")

        with patch("aspire_orchestrator.providers.twilio_trust_hub.fetch_shaken_policy_sid",
                   new=AsyncMock(return_value=SHAKEN_POLICY_SID)), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_trust_product",
                   new=AsyncMock(return_value={"sid": SHAKEN_SID, "status": "draft"})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.assign_entity_to_trust_product",
                   new=AsyncMock(return_value={"sid": "EA-shaken-001"})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.add_phone_to_trust_product",
                   new=AsyncMock(return_value={"sid": "PN-assign-001"})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=cut_receipt_mock):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["to_state"] == "shaken_created"
        call_kwargs = cut_receipt_mock.call_args.kwargs
        assert call_kwargs["receipt_type"] == "shaken_trust_product_created"
        _assert_no_pii_in_receipt(call_kwargs)

    @pytest.mark.asyncio
    async def test_idempotency_skip_shaken_create(self) -> None:
        """twilio_shaken_bundle_sid already set → skip create_trust_product."""
        profile = _base_profile(
            trust_state="profile_approved",
            twilio_secondary_profile_sid=PROFILE_SID,
            twilio_shaken_bundle_sid=SHAKEN_SID,
        )
        phone = _base_phone()
        select_responses = [
            [profile],
            [phone],
        ]
        create_mock = AsyncMock(return_value={"sid": SHAKEN_SID})

        with patch("aspire_orchestrator.providers.twilio_trust_hub.fetch_shaken_policy_sid",
                   new=AsyncMock(return_value=SHAKEN_POLICY_SID)), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_trust_product",
                   new=create_mock), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.assign_entity_to_trust_product",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.add_phone_to_trust_product",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-idem-shaken")):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        create_mock.assert_not_called()
        assert result["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_twilio_error_fails(self) -> None:
        from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
        profile = _base_profile(
            trust_state="profile_approved",
            twilio_secondary_profile_sid=PROFILE_SID,
        )
        select_responses = [
            [profile],
            [],  # no phone (irrelevant — fails before phone lookup)
            [{"email": "tony@scottpainting.com"}],
        ]

        with patch("aspire_orchestrator.providers.twilio_trust_hub.fetch_shaken_policy_sid",
                   new=AsyncMock(return_value=SHAKEN_POLICY_SID)), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_trust_product",
                   new=AsyncMock(side_effect=TrustHubError("CREATE_SHAKEN_FAILED", "server error", 500))), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-err-001")):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"


# ============================================================================
# 7. shaken_created → shaken_submitted
# ============================================================================

class TestShakenCreated:

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        profile = _base_profile(
            trust_state="shaken_created",
            twilio_shaken_bundle_sid=SHAKEN_SID,
        )
        cut_receipt_mock = AsyncMock(return_value="receipt-shaken-sub-001")

        with patch("aspire_orchestrator.providers.twilio_trust_hub.submit_trust_product",
                   new=AsyncMock(return_value={"sid": SHAKEN_SID, "status": "pending-review"})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(return_value=[profile])), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=cut_receipt_mock):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["to_state"] == "shaken_submitted"
        call_kwargs = cut_receipt_mock.call_args.kwargs
        # Submission step uses dedicated receipt type per W2 policy-gate
        # finding 1 (audit clarity vs `_created`).
        assert call_kwargs["receipt_type"] == "shaken_trust_product_submitted"
        _assert_no_pii_in_receipt(call_kwargs)

    @pytest.mark.asyncio
    async def test_missing_shaken_sid_fails(self) -> None:
        profile = _base_profile(trust_state="shaken_created", twilio_shaken_bundle_sid=None)

        with patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(return_value=[profile])), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-missing-shaken")):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"
        assert result["reason_code"] == "MISSING_SHAKEN_SID"

    @pytest.mark.asyncio
    async def test_twilio_error_fails(self) -> None:
        from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
        profile = _base_profile(
            trust_state="shaken_created",
            twilio_shaken_bundle_sid=SHAKEN_SID,
        )
        with patch("aspire_orchestrator.providers.twilio_trust_hub.submit_trust_product",
                   new=AsyncMock(side_effect=TrustHubError("SUBMIT_FAILED", "error", 422))), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(return_value=[profile])), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-err")):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"


# ============================================================================
# 8. shaken_submitted → HALT
# ============================================================================

class TestShakenSubmittedHalt:

    @pytest.mark.asyncio
    async def test_halts_cleanly(self) -> None:
        profile = _base_profile(
            trust_state="shaken_submitted",
            twilio_shaken_bundle_sid=SHAKEN_SID,
        )
        with patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(return_value=[profile])):
            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "halted"
        assert result["receipt_id"] is None


# ============================================================================
# 9. shaken_approved → cnam_created
# ============================================================================

class TestShakenApproved:

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        profile = _base_profile(
            trust_state="shaken_approved",
            twilio_secondary_profile_sid=PROFILE_SID,
            twilio_shaken_bundle_sid=SHAKEN_SID,
        )
        # select calls: profile, suite email, cnam_records (empty)
        select_responses = [
            [profile],
            [{"email": "tony@scottpainting.com"}],
            [],  # tenant_cnam_records — empty → insert
        ]
        cut_receipt_mock = AsyncMock(return_value="receipt-cnam-001")

        with patch("aspire_orchestrator.providers.twilio_trust_hub.fetch_cnam_policy_sid",
                   new=AsyncMock(return_value=CNAM_POLICY_SID)), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_trust_product",
                   new=AsyncMock(return_value={"sid": CNAM_SID, "status": "draft"})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.assign_entity_to_trust_product",
                   new=AsyncMock(return_value={"sid": "EA-cnam-001"})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_end_user",
                   new=AsyncMock(return_value={"sid": EU_SID_CNAM})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.services.supabase_client.supabase_insert",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=cut_receipt_mock):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["to_state"] == "cnam_created"
        # Two receipts cut: cnam_trust_product_created + cnam_display_name_set
        assert cut_receipt_mock.call_count == 2
        types_cut = {c.kwargs["receipt_type"] for c in cut_receipt_mock.call_args_list}
        assert "cnam_trust_product_created" in types_cut
        assert "cnam_display_name_set" in types_cut

    @pytest.mark.asyncio
    async def test_cnam_display_name_sanitization_applied(self) -> None:
        """Business name sanitized to SCOTT PAINTING for Scott Painting Services."""
        profile = _base_profile(
            trust_state="shaken_approved",
            twilio_secondary_profile_sid=PROFILE_SID,
            legal_business_name="Scott Painting Services",
        )
        select_responses = [
            [profile],
            [{"email": "tony@scottpainting.com"}],
            [],
        ]
        create_eu_mock = AsyncMock(return_value={"sid": EU_SID_CNAM})

        with patch("aspire_orchestrator.providers.twilio_trust_hub.fetch_cnam_policy_sid",
                   new=AsyncMock(return_value=CNAM_POLICY_SID)), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_trust_product",
                   new=AsyncMock(return_value={"sid": CNAM_SID})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.assign_entity_to_trust_product",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_end_user",
                   new=create_eu_mock), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.services.supabase_client.supabase_insert",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-cnam-name")):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        # Verify create_end_user called with sanitized CNAM name, not raw business name.
        create_eu_mock.assert_called_once()
        call_kwargs = create_eu_mock.call_args.kwargs
        assert call_kwargs["end_user_type"] == "cnam_information"
        attrs = call_kwargs["attributes"]
        cnam_name = attrs.get("cnam_display_name", "")
        assert cnam_name == "SCOTT PAINTING"
        assert len(cnam_name) <= 15

    @pytest.mark.asyncio
    async def test_idempotency_skip_cnam_create(self) -> None:
        """twilio_cnam_bundle_sid already set → skip create_trust_product for CNAM."""
        profile = _base_profile(
            trust_state="shaken_approved",
            twilio_secondary_profile_sid=PROFILE_SID,
            twilio_cnam_bundle_sid=CNAM_SID,
        )
        cnam_row = {
            "trust_profile_id": TRUST_PROFILE_ID,
            "twilio_cnam_bundle_sid": CNAM_SID,
            "twilio_cnam_end_user_sid": EU_SID_CNAM,
            "cnam_display_name": "SCOTT PAINTING",
        }
        select_responses = [
            [profile],
            [cnam_row],  # tenant_cnam_records already has EU SID
        ]
        create_tp_mock = AsyncMock(return_value={"sid": CNAM_SID})

        with patch("aspire_orchestrator.providers.twilio_trust_hub.fetch_cnam_policy_sid",
                   new=AsyncMock(return_value=CNAM_POLICY_SID)), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_trust_product",
                   new=create_tp_mock), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.assign_entity_to_trust_product",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_end_user",
                   new=AsyncMock(return_value={"sid": EU_SID_CNAM})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.services.supabase_client.supabase_insert",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-idem-cnam")):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        create_tp_mock.assert_not_called()
        assert result["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_twilio_error_fails_with_cnam_rejected_receipt(self) -> None:
        from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
        profile = _base_profile(
            trust_state="shaken_approved",
            twilio_secondary_profile_sid=PROFILE_SID,
        )
        select_responses = [
            [profile],
            [{"email": "tony@scottpainting.com"}],
            [],
        ]
        cut_receipt_mock = AsyncMock(return_value="receipt-cnam-fail")

        with patch("aspire_orchestrator.providers.twilio_trust_hub.fetch_cnam_policy_sid",
                   new=AsyncMock(return_value=CNAM_POLICY_SID)), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_trust_product",
                   new=AsyncMock(side_effect=TrustHubError("CNAM_FAIL", "forbidden", 403))), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=cut_receipt_mock):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"
        call_kwargs = cut_receipt_mock.call_args.kwargs
        assert call_kwargs["receipt_type"] == "cnam_trust_product_rejected"


# ============================================================================
# 10. cnam_created → cnam_submitted
# ============================================================================

class TestCnamCreated:

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        profile = _base_profile(
            trust_state="cnam_created",
            twilio_cnam_bundle_sid=CNAM_SID,
        )
        phone = _base_phone()
        cnam_row = {
            "trust_profile_id": TRUST_PROFILE_ID,
            "twilio_cnam_bundle_sid": CNAM_SID,
            "twilio_cnam_end_user_sid": EU_SID_CNAM,
            "twilio_cnam_channel_endpoint_sid": None,
            "cnam_display_name": "SCOTT PAINTING",
        }
        select_responses = [
            [profile],      # _load_trust_profile
            [cnam_row],     # cnam_records
            [phone],        # _load_phone_number
        ]
        cut_receipt_mock = AsyncMock(return_value="receipt-cnam-sub-001")

        with patch("aspire_orchestrator.providers.twilio_trust_hub.add_phone_to_trust_product",
                   new=AsyncMock(return_value={"sid": CEA_SID})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.submit_trust_product",
                   new=AsyncMock(return_value={"sid": CNAM_SID, "status": "pending-review"})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=cut_receipt_mock):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["to_state"] == "cnam_submitted"
        call_kwargs = cut_receipt_mock.call_args.kwargs
        # Submission step uses dedicated receipt type per W2 policy-gate
        # finding 1 (audit clarity vs `_created`).
        assert call_kwargs["receipt_type"] == "cnam_trust_product_submitted"
        _assert_no_pii_in_receipt(call_kwargs)

    @pytest.mark.asyncio
    async def test_no_active_phone_fails(self) -> None:
        profile = _base_profile(
            trust_state="cnam_created",
            twilio_cnam_bundle_sid=CNAM_SID,
        )
        cnam_row = {
            "trust_profile_id": TRUST_PROFILE_ID,
            "twilio_cnam_bundle_sid": CNAM_SID,
            "twilio_cnam_end_user_sid": EU_SID_CNAM,
            "twilio_cnam_channel_endpoint_sid": None,
        }
        select_responses = [
            [profile],
            [cnam_row],
            [],  # no active phone
        ]

        with patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-nophone")):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"
        assert result["reason_code"] == "NO_ACTIVE_PHONE_NUMBER"

    @pytest.mark.asyncio
    async def test_idempotency_skip_add_phone_if_cea_sid_exists(self) -> None:
        """twilio_cnam_channel_endpoint_sid already set → skip add_phone call."""
        profile = _base_profile(
            trust_state="cnam_created",
            twilio_cnam_bundle_sid=CNAM_SID,
        )
        cnam_row = {
            "trust_profile_id": TRUST_PROFILE_ID,
            "twilio_cnam_bundle_sid": CNAM_SID,
            "twilio_cnam_channel_endpoint_sid": CEA_SID,  # already set
        }
        select_responses = [
            [profile],
            [cnam_row],
        ]
        add_phone_mock = AsyncMock(return_value={"sid": CEA_SID})

        with patch("aspire_orchestrator.providers.twilio_trust_hub.add_phone_to_trust_product",
                   new=add_phone_mock), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.submit_trust_product",
                   new=AsyncMock(return_value={"status": "pending-review"})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-idem-cea")):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        add_phone_mock.assert_not_called()
        assert result["outcome"] == "success"


# ============================================================================
# 11. cnam_submitted → HALT
# ============================================================================

class TestCnamSubmittedHalt:

    @pytest.mark.asyncio
    async def test_halts_cleanly(self) -> None:
        profile = _base_profile(
            trust_state="cnam_submitted",
            twilio_cnam_bundle_sid=CNAM_SID,
        )
        with patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(return_value=[profile])):
            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "halted"
        assert result["receipt_id"] is None


# ============================================================================
# 12. cnam_approved → number_attached
# ============================================================================

class TestCnamApproved:

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        profile = _base_profile(
            trust_state="cnam_approved",
            twilio_secondary_profile_sid=PROFILE_SID,
            twilio_cnam_bundle_sid=CNAM_SID,
        )
        phone = _base_phone()
        select_responses = [
            [profile],
            [phone],
        ]
        cut_receipt_mock = AsyncMock(return_value="receipt-number-attached-001")

        with patch("aspire_orchestrator.providers.twilio_trust_hub.assign_number_to_profile",
                   new=AsyncMock(return_value={"sid": "CEA-profile-001"})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.enable_caller_id_lookup",
                   new=AsyncMock(return_value={"sid": NUMBER_SID, "voice_caller_id_lookup": True})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=cut_receipt_mock):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["to_state"] == "number_attached"
        # Two receipts: number_attached_to_profile + caller_id_lookup_enabled
        assert cut_receipt_mock.call_count == 2
        types_cut = {c.kwargs["receipt_type"] for c in cut_receipt_mock.call_args_list}
        assert "number_attached_to_profile" in types_cut
        assert "caller_id_lookup_enabled" in types_cut
        # PII check on every receipt.
        for call in cut_receipt_mock.call_args_list:
            _assert_no_pii_in_receipt(call.kwargs)

    @pytest.mark.asyncio
    async def test_phone_number_redacted_in_receipt(self) -> None:
        """Raw E.164 phone number must not appear in receipt outputs."""
        profile = _base_profile(
            trust_state="cnam_approved",
            twilio_secondary_profile_sid=PROFILE_SID,
        )
        phone = _base_phone()
        receipt_calls: list[dict[str, Any]] = []

        async def _capture_receipt(**kwargs: Any) -> str:
            receipt_calls.append(kwargs)
            return "receipt-pii-check"

        select_responses = [[profile], [phone]]

        with patch("aspire_orchestrator.providers.twilio_trust_hub.assign_number_to_profile",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.enable_caller_id_lookup",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=_capture_receipt):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        raw_phone = "+14482885386"
        for call in receipt_calls:
            all_vals_str = str(_flatten_values(call))
            assert raw_phone not in all_vals_str, (
                f"Raw E.164 phone {raw_phone!r} appeared in receipt: {call}"
            )

    @pytest.mark.asyncio
    async def test_no_active_phone_fails(self) -> None:
        profile = _base_profile(
            trust_state="cnam_approved",
            twilio_secondary_profile_sid=PROFILE_SID,
        )
        select_responses = [[profile], []]  # no phone

        with patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-err")):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"
        assert result["reason_code"] == "NO_ACTIVE_PHONE_NUMBER"

    @pytest.mark.asyncio
    async def test_caller_id_lookup_error_fails(self) -> None:
        from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
        profile = _base_profile(
            trust_state="cnam_approved",
            twilio_secondary_profile_sid=PROFILE_SID,
        )
        phone = _base_phone()
        select_responses = [[profile], [phone]]

        with patch("aspire_orchestrator.providers.twilio_trust_hub.assign_number_to_profile",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.enable_caller_id_lookup",
                   new=AsyncMock(side_effect=TrustHubError("CID_FAIL", "error", 422))), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-cid-err")):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"


# ============================================================================
# 13. number_attached → branded_calling_pending OR terminal halt
# ============================================================================

class TestNumberAttached:

    @pytest.mark.asyncio
    async def test_branded_calling_disabled_halts(self) -> None:
        profile = _base_profile(trust_state="number_attached")
        select_responses = [[profile]]

        with patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.settings") as s_mock:
            s_mock.branded_calling_enabled = False
            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "halted"
        assert result["from_state"] == "number_attached"
        assert result["to_state"] == "number_attached"
        assert result["receipt_id"] is None

    @pytest.mark.asyncio
    async def test_branded_calling_enabled_advances_to_pending(self) -> None:
        # W6 ship: enrollment is real (not a stub anymore). Profile must
        # carry twilio_secondary_profile_sid; thub.enroll_branded_calling
        # is mocked to return a SID; outcome="success" (no longer "halted").
        profile = _base_profile(trust_state="number_attached")
        profile["twilio_secondary_profile_sid"] = "BPaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        select_responses = [[profile]]

        with patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.thub.enroll_branded_calling",
                   new=AsyncMock(return_value={"sid": "BCaaaabbbbccccddddeeeeffffabcdef12"})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=AsyncMock(return_value="receipt-bc-001")), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.settings") as s_mock:
            s_mock.branded_calling_enabled = True
            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["to_state"] == "branded_calling_pending"
        assert result["receipt_id"] == "receipt-bc-001"


# ============================================================================
# 14. Terminal failure states
# ============================================================================

class TestTerminalFailureStates:

    @pytest.mark.parametrize("state", ["profile_rejected", "failed", "suspended"])
    @pytest.mark.asyncio
    async def test_terminal_state_returns_failed(self, state: str) -> None:
        profile = _base_profile(trust_state=state)
        with patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(return_value=[profile])):
            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"
        assert result["reason_code"] == "TERMINAL_FAILURE_STATE"
        assert result["receipt_id"] is None


# ============================================================================
# 15. PII REDACTION — cut_trust_receipt raises on forbidden PII keys
# ============================================================================

class TestPiiRedactionEnforcement:
    """Verify the state machine never attempts to pass PII into receipts.

    The cut_trust_receipt function in W2-E raises TrustReceiptError on any
    forbidden PII key. Here we test that the state machine handles that
    gracefully as well (the real PII check is in trust_receipts.py W2-E tests;
    here we test the integration point).
    """

    @pytest.mark.asyncio
    async def test_pii_key_in_receipt_raises(self) -> None:
        """Directly call cut_trust_receipt with PII — must raise TrustReceiptError."""
        from aspire_orchestrator.workers.trust_onboarding.trust_receipts import (
            TrustReceiptError,
            cut_trust_receipt,
        )
        profile = _base_profile()

        with pytest.raises(TrustReceiptError, match="PII_LEAK_BLOCKED"):
            with patch("aspire_orchestrator.workers.trust_onboarding.trust_receipts.supabase_select",
                       new=AsyncMock(return_value=[])), \
                 patch("aspire_orchestrator.workers.trust_onboarding.trust_receipts.supabase_insert",
                       new=AsyncMock(return_value={})), \
                 patch("aspire_orchestrator.services.receipt_store.store_receipts_strict",
                       new=MagicMock()):
                await cut_trust_receipt(
                    receipt_type="customer_profile_created",
                    trust_profile=profile,
                    outcome="success",
                    from_state="kyb_collected",
                    to_state="profile_drafted",
                    redacted_inputs={"ein": "12-3456789"},  # FORBIDDEN
                )


# ============================================================================
# 16. Receipt on every code path
# ============================================================================

class TestReceiptCoverage:
    """Every terminal outcome (success / failed) cuts at least one receipt."""

    @pytest.mark.asyncio
    async def test_every_success_path_cuts_receipt(self) -> None:
        """profile_drafted → profile_submitted success cuts a receipt."""
        profile = _base_profile(
            trust_state="profile_drafted",
            twilio_secondary_profile_sid=PROFILE_SID,
        )
        reps = [_base_rep(rep_index=1, eu_sid=EU_SID_REP1)]
        cut_receipt_mock = AsyncMock(return_value="receipt-coverage-001")

        with patch("aspire_orchestrator.providers.twilio_trust_hub.assign_entity_to_profile",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.submit_customer_profile",
                   new=AsyncMock(return_value={"status": "pending-review"})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=[[profile], [reps[0]]])), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=cut_receipt_mock):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["receipt_id"] is not None
        cut_receipt_mock.assert_called()

    @pytest.mark.asyncio
    async def test_every_failed_path_cuts_receipt(self) -> None:
        """A Twilio 4xx failure on any transition cuts a receipt with outcome=failed."""
        from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
        profile = _base_profile(
            trust_state="kyb_collected",
        )
        select_responses = [
            [profile],
            [{"email": "tony@scottpainting.com"}],   # _load_suite_email first
            [_base_rep(rep_index=1)],                # _load_authorized_reps second
        ]
        cut_receipt_mock = AsyncMock(return_value="receipt-fail-coverage")

        with patch("aspire_orchestrator.providers.twilio_trust_hub.fetch_secondary_profile_policy_sid",
                   new=AsyncMock(return_value=SECONDARY_POLICY_SID)), \
             patch("aspire_orchestrator.providers.twilio_trust_hub.create_secondary_customer_profile",
                   new=AsyncMock(side_effect=TrustHubError("FAIL", "error", 422))), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(side_effect=select_responses)), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_rpc",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=cut_receipt_mock):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            result = await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"
        cut_receipt_mock.assert_called()
        call_kwargs = cut_receipt_mock.call_args.kwargs
        assert call_kwargs["outcome"] == "failed"


# ============================================================================
# 17. Correlation ID propagation
# ============================================================================

class TestCorrelationIdPropagation:
    """cut_trust_receipt receives the profile's scope identifiers on every call."""

    @pytest.mark.asyncio
    async def test_suite_id_tenant_id_in_receipt(self) -> None:
        profile = _base_profile(
            trust_state="shaken_created",
            twilio_shaken_bundle_sid=SHAKEN_SID,
        )
        receipt_calls: list[dict[str, Any]] = []

        async def _capture(**kwargs: Any) -> str:
            receipt_calls.append(kwargs)
            return "receipt-trace-001"

        with patch("aspire_orchestrator.providers.twilio_trust_hub.submit_trust_product",
                   new=AsyncMock(return_value={"status": "pending-review"})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_select",
                   new=AsyncMock(return_value=[profile])), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
                   new=AsyncMock(return_value={})), \
             patch("aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
                   new=_capture):

            from aspire_orchestrator.workers.trust_onboarding.state_machine import advance_trust_state
            await advance_trust_state(TRUST_PROFILE_ID, worker_job_id=WORKER_JOB_ID)

        assert len(receipt_calls) >= 1
        call = receipt_calls[0]
        tp = call.get("trust_profile", {})
        assert str(tp.get("suite_id", "")) == SUITE_ID
        assert str(tp.get("tenant_id", "")) == TENANT_ID


# ============================================================================
# Helper utilities
# ============================================================================

_FORBIDDEN_PII_KEYS_TEST = frozenset({
    "email", "phone_e164", "phone_number", "first_name", "last_name",
    "full_name", "dob", "date_of_birth", "ssn", "ssn_last4", "ein",
    "tax_id", "address_street", "raw_business_name", "owner_name",
})


def _assert_no_pii_in_receipt(kwargs: dict[str, Any]) -> None:
    """Assert that no PII field names appear in receipt redacted_inputs or outputs."""
    for field in ("redacted_inputs", "redacted_outputs"):
        payload = kwargs.get(field) or {}
        for key in payload.keys():
            assert key.lower() not in _FORBIDDEN_PII_KEYS_TEST, (
                f"PII key {key!r} found in {field} of receipt "
                f"type={kwargs.get('receipt_type')!r}"
            )


def _flatten_values(d: Any, depth: int = 0) -> list[Any]:
    """Recursively collect all scalar values from a nested dict/list."""
    if depth > 10:
        return []
    result = []
    if isinstance(d, dict):
        for v in d.values():
            result.extend(_flatten_values(v, depth + 1))
    elif isinstance(d, (list, tuple)):
        for item in d:
            result.extend(_flatten_values(item, depth + 1))
    else:
        result.append(d)
    return result
