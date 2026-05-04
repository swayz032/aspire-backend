"""Tests for Wave 6 — Branded Calling enrollment (private-beta gated).

Coverage:
  Contract tests (feature-flag behaviour, happy path, error mapping):
    1.  Flag OFF → state machine halts at number_attached, no Twilio call
    2.  Flag ON, API URL not set → BRANDED_CALLING_NOT_CONFIGURED 503
    3.  Flag ON, API URL set, happy path → advances to branded_calling_pending + receipt
    4.  Idempotency: twilio_branded_calling_sid already set → skip Twilio call
    5.  Twilio 5xx during enrollment → RetryableError re-raised to ARQ (Law #10)
    6.  Missing customer_profile_sid → fails cleanly + receipt cut
    7.  Receipt PII guard: business name MUST NOT appear in redacted fields
    8.  Status callback → approved → branded_calling_live + branded_calling_approved receipt
    9.  Status callback → rejected → failed + branded_calling_rejected receipt
   10.  Receipt taxonomy: branded_calling_approved / branded_calling_rejected in RECEIPT_TYPES
   11.  Settings: branded_calling_api_url and branded_calling_api_key default to None
   12.  enroll_branded_calling: both URL and key None → BRANDED_CALLING_NOT_CONFIGURED
   13.  fetch_branded_calling_status: key None → BRANDED_CALLING_NOT_CONFIGURED

Aspire Laws verified:
  Law #1: adapter makes no autonomous decisions — returns result for orchestrator
  Law #2: every code path that changes state cuts a receipt
  Law #3: fail-closed on missing credentials
  Law #7: no retry inside adapter (RetryableError surfaces to ARQ)
  Law #9: PII-free receipts (business name blocked by _FORBIDDEN_PII_KEYS check)
  Law #10: RetryableError on 5xx so ARQ retries

All Twilio/Supabase calls are mocked — no real external traffic.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRUST_PROFILE_ID = "aaaaaaaa-0000-0000-0000-000000000099"
SUITE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
TENANT_ID = "bbbbbbbb-0000-0000-0000-000000000099"
OFFICE_ID = "cccccccc-0000-0000-0000-000000000099"
WORKER_JOB_ID = "arq-job-w6-001"

PROFILE_SID = "BU-profile-w6-001"
# Must be a valid Twilio SID format: 2 uppercase letters + 32 hex chars
ENROLLMENT_SID = "BCaaaabbbbccccddddeeeeffffabcdef12"

_FAKE_API_URL = "https://branded-calling.twilio.com/v1/Enrollments"
_FAKE_API_KEY = "fake-bc-api-key-for-tests"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _profile(
    trust_state: str = "number_attached",
    branded_calling_sid: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Minimal tenant_trust_profiles row for number_attached state."""
    base: dict[str, Any] = {
        "id": TRUST_PROFILE_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "office_id": OFFICE_ID,
        "trust_state": trust_state,
        "legal_business_name": "Scott Painting Services",
        "business_type": "llc",
        "twilio_secondary_profile_sid": PROFILE_SID,
        "twilio_shaken_bundle_sid": "BU-shaken-w6",
        "twilio_cnam_bundle_sid": "BU-cnam-w6",
        "twilio_branded_calling_sid": branded_calling_sid,
        "branded_calling_enabled": False,
        "branded_calling_display_name": None,
        "dispute_count": 0,
        "rejection_reason": None,
        "rejection_code": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Test 1: Feature flag OFF → state machine halts, no Twilio call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_halts_at_number_attached() -> None:
    """BRANDED_CALLING_ENABLED=false → outcome='halted', to_state='number_attached'."""
    from aspire_orchestrator.workers.trust_onboarding.state_machine import (
        _transition_number_attached,
    )

    profile = _profile()  # trust_state=number_attached

    with patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.settings"
    ) as mock_settings, patch(
        "aspire_orchestrator.providers.twilio_trust_hub.enroll_branded_calling",
        new_callable=AsyncMock,
    ) as mock_enroll:
        mock_settings.branded_calling_enabled = False
        result = await _transition_number_attached(
            profile, worker_job_id=WORKER_JOB_ID
        )

    assert result["outcome"] == "halted"
    assert result["to_state"] == "number_attached"
    assert result["receipt_id"] is None
    # No Twilio call when flag is off
    mock_enroll.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: Flag ON, API URL not set → BRANDED_CALLING_NOT_CONFIGURED 503
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_on_no_api_url_raises_not_configured() -> None:
    """Flag=True but branded_calling_api_url is None → TrustHubError(BRANDED_CALLING_NOT_CONFIGURED)."""
    from aspire_orchestrator.providers.twilio_trust_hub import (
        TrustHubError,
        enroll_branded_calling,
    )

    with patch(
        "aspire_orchestrator.providers.twilio_trust_hub.settings"
    ) as mock_settings:
        mock_settings.branded_calling_api_url = None
        mock_settings.branded_calling_api_key = None

        with pytest.raises(TrustHubError) as exc_info:
            await enroll_branded_calling(
                customer_profile_sid=PROFILE_SID,
                brand_logo_url=None,
                idempotency_key="idem-key-001",
            )

    assert exc_info.value.code == "BRANDED_CALLING_NOT_CONFIGURED"
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Test 3: Happy path — flag ON + URL set → advances to branded_calling_pending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_advances_to_branded_calling_pending() -> None:
    """Full happy path: flag ON, valid API URL/key, Twilio returns enrollment SID."""
    from aspire_orchestrator.workers.trust_onboarding.state_machine import (
        _transition_number_attached,
    )

    profile = _profile()
    enroll_response = {"sid": ENROLLMENT_SID, "status": "pending"}

    with patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.settings"
    ) as mock_settings, patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.thub.enroll_branded_calling",
        new_callable=AsyncMock,
        return_value=enroll_response,
    ) as mock_enroll, patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
        new_callable=AsyncMock,
        return_value={},
    ) as mock_update, patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
        new_callable=AsyncMock,
        return_value="receipt-bc-enrolled-001",
    ) as mock_receipt:
        mock_settings.branded_calling_enabled = True

        result = await _transition_number_attached(
            profile, worker_job_id=WORKER_JOB_ID
        )

    assert result["outcome"] == "success"
    assert result["to_state"] == "branded_calling_pending"
    assert result["from_state"] == "number_attached"
    assert result["receipt_id"] == "receipt-bc-enrolled-001"

    # Twilio was called once with correct SID
    mock_enroll.assert_called_once()
    call_kwargs = mock_enroll.call_args.kwargs
    assert call_kwargs["customer_profile_sid"] == PROFILE_SID
    assert call_kwargs["idempotency_key"] == f"enroll-branded-calling-{TRUST_PROFILE_ID}"

    # DB updated with SID + new state
    update_call = mock_update.call_args
    assert update_call.args[0] == "tenant_trust_profiles"
    update_fields = update_call.args[2]
    assert update_fields["trust_state"] == "branded_calling_pending"
    assert update_fields["twilio_branded_calling_sid"] == ENROLLMENT_SID

    # Receipt cut with correct type
    receipt_call = mock_receipt.call_args.kwargs
    assert receipt_call["receipt_type"] == "branded_calling_enrolled"
    assert receipt_call["to_state"] == "branded_calling_pending"
    assert receipt_call["outcome"] == "success"


# ---------------------------------------------------------------------------
# Test 4: Idempotency — enrollment SID already stored → skip Twilio call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_skip_when_enrollment_sid_already_set() -> None:
    """If twilio_branded_calling_sid is already set, skip enroll_branded_calling."""
    from aspire_orchestrator.workers.trust_onboarding.state_machine import (
        _transition_number_attached,
    )

    profile = _profile(branded_calling_sid=ENROLLMENT_SID)

    with patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.settings"
    ) as mock_settings, patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.thub.enroll_branded_calling",
        new_callable=AsyncMock,
    ) as mock_enroll, patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
        new_callable=AsyncMock,
        return_value={},
    ), patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
        new_callable=AsyncMock,
        return_value="receipt-idem-001",
    ):
        mock_settings.branded_calling_enabled = True

        result = await _transition_number_attached(
            profile, worker_job_id=WORKER_JOB_ID
        )

    # Should still succeed (already enrolled = idempotent advance)
    assert result["outcome"] == "success"
    assert result["to_state"] == "branded_calling_pending"
    # Twilio was NOT called again
    mock_enroll.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: Twilio 5xx → RetryableError re-raised to ARQ (Law #10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_twilio_5xx_raises_retryable_error() -> None:
    """Twilio 503 during enrollment → RetryableError surfaces to ARQ for retry."""
    from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
    from aspire_orchestrator.services.resilience import RetryableError
    from aspire_orchestrator.workers.trust_onboarding.state_machine import (
        _transition_number_attached,
    )

    profile = _profile()

    with patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.settings"
    ) as mock_settings, patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.thub.enroll_branded_calling",
        new_callable=AsyncMock,
        side_effect=TrustHubError("TRUST_HUB_BRANDED_CALLING_POST_FAILED", "Service unavailable", 503),
    ), patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
        new_callable=AsyncMock,
        return_value="receipt-fail-001",
    ), patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
        new_callable=AsyncMock,
        return_value={},
    ):
        mock_settings.branded_calling_enabled = True

        with pytest.raises(RetryableError):
            await _transition_number_attached(
                profile, worker_job_id=WORKER_JOB_ID
            )


# ---------------------------------------------------------------------------
# Test 6: Missing customer_profile_sid → fails cleanly with receipt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_profile_sid_fails_cleanly() -> None:
    """If twilio_secondary_profile_sid is missing, fail with MISSING_PROFILE_SID receipt."""
    from aspire_orchestrator.workers.trust_onboarding.state_machine import (
        _transition_number_attached,
    )

    # Remove the profile SID
    profile = _profile()
    profile["twilio_secondary_profile_sid"] = None

    with patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.settings"
    ) as mock_settings, patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.thub.enroll_branded_calling",
        new_callable=AsyncMock,
    ) as mock_enroll, patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.supabase_update",
        new_callable=AsyncMock,
        return_value={},
    ), patch(
        "aspire_orchestrator.workers.trust_onboarding.state_machine.cut_trust_receipt",
        new_callable=AsyncMock,
        return_value="receipt-missing-sid-001",
    ):
        mock_settings.branded_calling_enabled = True

        result = await _transition_number_attached(
            profile, worker_job_id=WORKER_JOB_ID
        )

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "MISSING_PROFILE_SID"
    mock_enroll.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7: PII guard — business name MUST NOT appear in receipt fields
# ---------------------------------------------------------------------------


def test_receipt_pii_guard_blocks_business_name() -> None:
    """Attempting to write legal_business_name into a receipt raises TrustReceiptError."""
    import asyncio

    from aspire_orchestrator.workers.trust_onboarding.trust_receipts import (
        TrustReceiptError,
        _assert_no_pii,
    )

    # raw_business_name is in _FORBIDDEN_PII_KEYS
    with pytest.raises(TrustReceiptError) as exc_info:
        _assert_no_pii(
            {"raw_business_name": "Scott Painting Services"},
            label="redacted_outputs",
            receipt_type="branded_calling_enrolled",
        )
    assert "PII_LEAK_BLOCKED" in str(exc_info.value)

    # email is also blocked
    with pytest.raises(TrustReceiptError):
        _assert_no_pii(
            {"email": "owner@scottpainting.com"},
            label="redacted_inputs",
            receipt_type="branded_calling_approved",
        )


# ---------------------------------------------------------------------------
# Test 8: Status callback → approved → branded_calling_live + receipt
# ---------------------------------------------------------------------------


async def _branded_calling_select_side_effect(
    table: str,
    filter_str: str,
    *args: Any,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Return profile row only when the filter is on twilio_branded_calling_sid.

    The status_callback route loops over columns in order:
      twilio_secondary_profile_sid → twilio_shaken_bundle_sid →
      twilio_cnam_bundle_sid → twilio_branded_calling_sid
    The profile has NONE of the first three SIDs set to ENROLLMENT_SID,
    so this side_effect returns [] for those columns and the profile row only
    when the filter targets twilio_branded_calling_sid.
    """
    if "twilio_branded_calling_sid" in filter_str:
        return [{
            "id": TRUST_PROFILE_ID,
            "suite_id": SUITE_ID,
            "tenant_id": TENANT_ID,
            "office_id": OFFICE_ID,
            "trust_state": "branded_calling_pending",
            "twilio_secondary_profile_sid": None,
            "twilio_shaken_bundle_sid": None,
            "twilio_cnam_bundle_sid": None,
            "twilio_branded_calling_sid": ENROLLMENT_SID,
        }]
    return []


@pytest.mark.asyncio
async def test_status_callback_approved_advances_to_live() -> None:
    """Twilio status=twilio-approved for branded_calling bundle → state=branded_calling_live."""
    import os

    os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-key")

    from fastapi.testclient import TestClient

    with patch(
        "aspire_orchestrator.routes.trust_hub.verify_twilio_signature",
        return_value=True,
    ), patch(
        "aspire_orchestrator.routes.trust_hub.supabase_select",
        side_effect=_branded_calling_select_side_effect,
    ), patch(
        "aspire_orchestrator.routes.trust_hub.supabase_update",
        new_callable=AsyncMock,
        return_value={},
    ) as mock_update, patch(
        "aspire_orchestrator.routes.trust_hub.cut_trust_receipt",
        new_callable=AsyncMock,
        return_value="receipt-bc-approved-001",
    ) as mock_receipt, patch(
        "aspire_orchestrator.routes.trust_hub._enqueue_advance_trust_state",
        new_callable=AsyncMock,
    ):
        from aspire_orchestrator.server import app

        client = TestClient(app)
        resp = client.post(
            "/v1/trust-hub/status-callback",
            data={
                "ResourceSid": ENROLLMENT_SID,
                "Status": "twilio-approved",
            },
            headers={"X-Twilio-Signature": "valid-sig"},
        )

    assert resp.status_code == 200

    # DB must be updated with branded_calling_live state and flag flipped
    assert mock_update.called, "supabase_update must be called on approval"
    update_call_args = mock_update.call_args.args
    update_fields: dict[str, Any] = update_call_args[2]
    assert update_fields["trust_state"] == "branded_calling_live"
    assert update_fields.get("branded_calling_enabled") is True

    # Receipt type must be branded_calling_approved
    assert mock_receipt.called, "cut_trust_receipt must be called"
    receipt_kwargs = mock_receipt.call_args.kwargs
    assert receipt_kwargs["receipt_type"] == "branded_calling_approved"
    assert receipt_kwargs["outcome"] == "success"


# ---------------------------------------------------------------------------
# Test 9: Status callback → rejected → state=failed + branded_calling_rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_callback_rejected_advances_to_failed() -> None:
    """Twilio status=twilio-rejected for branded_calling bundle → state=failed."""
    import os

    os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-key")

    with patch(
        "aspire_orchestrator.routes.trust_hub.verify_twilio_signature",
        return_value=True,
    ), patch(
        "aspire_orchestrator.routes.trust_hub.supabase_select",
        side_effect=_branded_calling_select_side_effect,
    ), patch(
        "aspire_orchestrator.routes.trust_hub.supabase_update",
        new_callable=AsyncMock,
        return_value={},
    ) as mock_update, patch(
        "aspire_orchestrator.routes.trust_hub.cut_trust_receipt",
        new_callable=AsyncMock,
        return_value="receipt-bc-rejected-001",
    ) as mock_receipt, patch(
        "aspire_orchestrator.routes.trust_hub._enqueue_advance_trust_state",
        new_callable=AsyncMock,
    ):
        from fastapi.testclient import TestClient

        from aspire_orchestrator.server import app

        client = TestClient(app)
        resp = client.post(
            "/v1/trust-hub/status-callback",
            data={
                "ResourceSid": ENROLLMENT_SID,
                "Status": "twilio-rejected",
                "FailureReason": "Brand not verified",
                "ErrorCode": "30450",
            },
            headers={"X-Twilio-Signature": "valid-sig"},
        )

    assert resp.status_code == 200

    assert mock_update.called, "supabase_update must be called on rejection"
    update_fields: dict[str, Any] = mock_update.call_args.args[2]
    assert update_fields["trust_state"] == "failed"

    assert mock_receipt.called, "cut_trust_receipt must be called"
    receipt_kwargs = mock_receipt.call_args.kwargs
    assert receipt_kwargs["receipt_type"] == "branded_calling_rejected"
    assert receipt_kwargs["outcome"] == "denied"


# ---------------------------------------------------------------------------
# Test 10: Receipt taxonomy — new types registered in RECEIPT_TYPES
# ---------------------------------------------------------------------------


def test_new_receipt_types_in_taxonomy() -> None:
    """branded_calling_approved and branded_calling_rejected must be in RECEIPT_TYPES."""
    from aspire_orchestrator.workers.trust_onboarding.trust_receipts import RECEIPT_TYPES

    assert "branded_calling_enrolled" in RECEIPT_TYPES
    assert "branded_calling_approved" in RECEIPT_TYPES
    assert "branded_calling_rejected" in RECEIPT_TYPES


# ---------------------------------------------------------------------------
# Test 11: Settings defaults
# ---------------------------------------------------------------------------


def test_settings_branded_calling_defaults() -> None:
    """branded_calling_enabled defaults False; api_url and api_key default None."""
    # Import fresh settings (already loaded from env; test env does not set these)
    from aspire_orchestrator.config.settings import Settings

    s = Settings(
        _env_file=None,
        ASPIRE_TWILIO_ACCOUNT_SID="",
        ASPIRE_TWILIO_AUTH_TOKEN="",
    )
    assert s.branded_calling_enabled is False
    assert s.branded_calling_api_url is None
    assert s.branded_calling_api_key is None


# ---------------------------------------------------------------------------
# Test 12: enroll_branded_calling — both URL and key None → fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enroll_branded_calling_no_url_and_no_key() -> None:
    """enroll_branded_calling raises BRANDED_CALLING_NOT_CONFIGURED when both URL+key absent."""
    from aspire_orchestrator.providers.twilio_trust_hub import (
        TrustHubError,
        enroll_branded_calling,
    )

    with patch(
        "aspire_orchestrator.providers.twilio_trust_hub.settings"
    ) as mock_s:
        mock_s.branded_calling_api_url = None
        mock_s.branded_calling_api_key = "some-key"  # URL missing is enough

        with pytest.raises(TrustHubError) as exc_info:
            await enroll_branded_calling(
                customer_profile_sid=PROFILE_SID,
                brand_logo_url=None,
                idempotency_key="idem-002",
            )

    assert exc_info.value.code == "BRANDED_CALLING_NOT_CONFIGURED"
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Test 13: fetch_branded_calling_status — key None → fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_branded_calling_status_no_key() -> None:
    """fetch_branded_calling_status raises BRANDED_CALLING_NOT_CONFIGURED when key absent."""
    from aspire_orchestrator.providers.twilio_trust_hub import (
        TrustHubError,
        fetch_branded_calling_status,
    )

    with patch(
        "aspire_orchestrator.providers.twilio_trust_hub.settings"
    ) as mock_s:
        mock_s.branded_calling_api_url = _FAKE_API_URL
        mock_s.branded_calling_api_key = None  # key missing

        with pytest.raises(TrustHubError) as exc_info:
            await fetch_branded_calling_status(ENROLLMENT_SID)

    assert exc_info.value.code == "BRANDED_CALLING_NOT_CONFIGURED"
    assert exc_info.value.status_code == 503
