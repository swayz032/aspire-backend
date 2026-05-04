"""Tests for Wave 9 — apply_cnam_display_name_change ARQ job.

Covers:
  - Happy path: request → sanitize → update_end_user → submit → DB update → receipt
  - Invalid display name (only special chars) → fail-closed, no Twilio call
  - Twilio 5xx → RetryableError re-raised (Law #10)
  - Twilio 4xx → request marked failed, non-retryable
  - Receipt cut with PII-free redacted_outputs (no business_name)
  - Idempotency: re-run after partial failure does NOT cut duplicate receipts
                  (idempotency keys carry through to Twilio update_end_user
                  + submit_trust_product calls)
  - Missing trust profile → terminal failure
  - Missing CNAM EndUser SID on profile → terminal failure
  - DB write failure post-Twilio → re-raised so ARQ retries
  - Sanitization output is exactly the value passed to Twilio attributes

Author: Aspire — Wave 9
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
from aspire_orchestrator.services.resilience import RetryableError

# ---------------------------------------------------------------------------
# Test fixtures / constants
# ---------------------------------------------------------------------------

_CRON = "aspire_orchestrator.workers.trust_onboarding.cron_jobs"

SUITE_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())
OFFICE_ID = str(uuid.uuid4())
TRUST_PROFILE_ID = str(uuid.uuid4())
REQUEST_ID = str(uuid.uuid4())
CNAM_END_USER_SID = "ITaaaaaaaaaaaaaaaaaaaaaaaaaaaaCNAMEU"
CNAM_TRUST_PRODUCT_SID = "BUaaaaaaaaaaaaaaaaaaaaaaaaaaaaCNAMTP"


def _make_request(
    requested_name: str = "Scott Painting Services",
) -> dict[str, Any]:
    return {
        "id": REQUEST_ID,
        "tenant_id": TENANT_ID,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "trust_profile_id": TRUST_PROFILE_ID,
        "requested_display_name": requested_name,
        "sanitized_display_name": "",  # filled in by job
        "status": "in_progress",
        "capability_token_id": "test-cap-token",
    }


def _make_profile() -> dict[str, Any]:
    return {
        "id": TRUST_PROFILE_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "office_id": OFFICE_ID,
        "trust_state": "number_attached",
        "cnam_end_user_sid": CNAM_END_USER_SID,
        "cnam_trust_product_sid": CNAM_TRUST_PRODUCT_SID,
    }


# ---------------------------------------------------------------------------
# Test 1: Happy path — sanitize + Twilio update + submit + DB + receipt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cnam_change_happy_path() -> None:
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        apply_cnam_display_name_change,
    )

    select_mock = AsyncMock(side_effect=[[_make_request()], [_make_profile()]])
    update_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def _update(table: str, filt: str, payload: dict[str, Any]) -> dict[str, Any]:
        update_calls.append((table, filt, payload))
        return {}

    with (
        patch(f"{_CRON}.supabase_select", select_mock),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, side_effect=_update),
        patch(
            f"{_CRON}.thub.update_end_user",
            new_callable=AsyncMock,
            return_value={"sid": CNAM_END_USER_SID},
        ) as update_eu_mock,
        patch(
            f"{_CRON}.thub.submit_trust_product",
            new_callable=AsyncMock,
            return_value={"sid": CNAM_TRUST_PRODUCT_SID, "status": "pending-review"},
        ) as submit_mock,
        patch(
            f"{_CRON}.cut_trust_receipt",
            new_callable=AsyncMock,
            return_value="trust_receipt_cnam_change_01",
        ) as cut_mock,
    ):
        result = await apply_cnam_display_name_change(REQUEST_ID)

    assert result["outcome"] == "success"
    assert result["request_id"] == REQUEST_ID
    assert result["sanitized_display_name"] == "SCOTT PAINTING"
    assert result["receipt_id"] == "trust_receipt_cnam_change_01"
    update_eu_mock.assert_awaited_once()
    submit_mock.assert_awaited_once()
    cut_mock.assert_awaited_once()
    # Idempotency keys must be deterministic
    eu_kwargs = update_eu_mock.await_args.kwargs
    assert eu_kwargs["idempotency_key"] == f"cnam_change_update_eu:{REQUEST_ID}"
    assert eu_kwargs["attributes"] == {"cnam_display_name": "SCOTT PAINTING"}
    submit_kwargs = submit_mock.await_args.kwargs
    assert submit_kwargs["idempotency_key"] == f"cnam_change_resubmit:{REQUEST_ID}"


# ---------------------------------------------------------------------------
# Test 2: Invalid display name — fail closed, no Twilio call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cnam_change_invalid_name_fails_closed() -> None:
    """Display name that sanitizes to empty (only special chars) → request failed."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        apply_cnam_display_name_change,
    )

    bad_request = _make_request(requested_name="!@#$%")
    select_mock = AsyncMock(return_value=[bad_request])

    with (
        patch(f"{_CRON}.supabase_select", select_mock),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, return_value={}),
        patch(f"{_CRON}.thub.update_end_user", new_callable=AsyncMock) as update_eu_mock,
        patch(f"{_CRON}.thub.submit_trust_product", new_callable=AsyncMock) as submit_mock,
        patch(f"{_CRON}.cut_trust_receipt", new_callable=AsyncMock) as cut_mock,
    ):
        result = await apply_cnam_display_name_change(REQUEST_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "INVALID_DISPLAY_NAME"
    update_eu_mock.assert_not_awaited()
    submit_mock.assert_not_awaited()
    cut_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 3: Twilio 5xx → RetryableError re-raised (Law #10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cnam_change_5xx_propagates_retryable_error() -> None:
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        apply_cnam_display_name_change,
    )

    select_mock = AsyncMock(side_effect=[[_make_request()], [_make_profile()]])

    with (
        patch(f"{_CRON}.supabase_select", select_mock),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, return_value={}),
        patch(
            f"{_CRON}.thub.update_end_user",
            new_callable=AsyncMock,
            side_effect=RetryableError("TWILIO_TRANSIENT", "503"),
        ),
        pytest.raises(RetryableError),
    ):
        await apply_cnam_display_name_change(REQUEST_ID)


# ---------------------------------------------------------------------------
# Test 4: Twilio 4xx → request marked failed, non-retryable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cnam_change_4xx_marks_failed() -> None:
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        apply_cnam_display_name_change,
    )

    select_mock = AsyncMock(side_effect=[[_make_request()], [_make_profile()]])
    update_calls: list[dict[str, Any]] = []

    async def _update(table: str, _filter: str, payload: dict[str, Any]) -> dict[str, Any]:
        update_calls.append(payload)
        return {}

    with (
        patch(f"{_CRON}.supabase_select", select_mock),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, side_effect=_update),
        patch(
            f"{_CRON}.thub.update_end_user",
            new_callable=AsyncMock,
            side_effect=TrustHubError("TRUST_HUB_POST_FAILED", "400 invalid attr", 400),
        ),
        patch(f"{_CRON}.thub.submit_trust_product", new_callable=AsyncMock) as submit_mock,
        patch(f"{_CRON}.cut_trust_receipt", new_callable=AsyncMock) as cut_mock,
    ):
        result = await apply_cnam_display_name_change(REQUEST_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "TWILIO_400"
    submit_mock.assert_not_awaited()
    cut_mock.assert_not_awaited()
    # Status should have been set to failed
    statuses = [c.get("status") for c in update_calls]
    assert "failed" in statuses


# ---------------------------------------------------------------------------
# Test 5: Receipt PII-free
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cnam_change_receipt_has_no_pii() -> None:
    """Receipt must NOT contain raw business_name / phone / email keys (Law #9)."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        apply_cnam_display_name_change,
    )

    select_mock = AsyncMock(side_effect=[[_make_request()], [_make_profile()]])

    cut_kwargs: dict[str, Any] = {}

    async def _capture_cut(**kwargs: Any) -> str:
        cut_kwargs.update(kwargs)
        return "trust_receipt_pii_check"

    with (
        patch(f"{_CRON}.supabase_select", select_mock),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, return_value={}),
        patch(f"{_CRON}.thub.update_end_user", new_callable=AsyncMock, return_value={}),
        patch(f"{_CRON}.thub.submit_trust_product", new_callable=AsyncMock, return_value={}),
        patch(f"{_CRON}.cut_trust_receipt", new_callable=AsyncMock, side_effect=_capture_cut),
    ):
        await apply_cnam_display_name_change(REQUEST_ID)

    forbidden = {
        "raw_business_name", "business_name", "phone_number", "phone_e164",
        "email", "first_name", "last_name", "dob", "ssn", "ssn_last4", "ein",
    }
    inputs_keys = set((cut_kwargs.get("redacted_inputs") or {}).keys())
    outputs_keys = set((cut_kwargs.get("redacted_outputs") or {}).keys())
    assert not (inputs_keys & forbidden), f"Forbidden PII in inputs: {inputs_keys & forbidden}"
    assert not (outputs_keys & forbidden), f"Forbidden PII in outputs: {outputs_keys & forbidden}"
    # cnam_display_name IS allowed (already public)
    assert (cut_kwargs.get("redacted_outputs") or {}).get("cnam_display_name") == "SCOTT PAINTING"


# ---------------------------------------------------------------------------
# Test 6: Missing trust profile → terminal failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cnam_change_missing_profile_fails() -> None:
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        apply_cnam_display_name_change,
    )

    # request exists, profile lookup returns []
    select_mock = AsyncMock(side_effect=[[_make_request()], []])

    with (
        patch(f"{_CRON}.supabase_select", select_mock),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, return_value={}),
        patch(f"{_CRON}.thub.update_end_user", new_callable=AsyncMock) as update_eu_mock,
    ):
        result = await apply_cnam_display_name_change(REQUEST_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "NO_TRUST_PROFILE"
    update_eu_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 7: Missing CNAM EndUser SID → terminal failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cnam_change_missing_cnam_resources() -> None:
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        apply_cnam_display_name_change,
    )

    profile_no_sid = _make_profile()
    profile_no_sid["cnam_end_user_sid"] = ""

    select_mock = AsyncMock(side_effect=[[_make_request()], [profile_no_sid]])

    with (
        patch(f"{_CRON}.supabase_select", select_mock),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, return_value={}),
        patch(f"{_CRON}.thub.update_end_user", new_callable=AsyncMock) as update_eu_mock,
    ):
        result = await apply_cnam_display_name_change(REQUEST_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "MISSING_CNAM_RESOURCES"
    update_eu_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 8: Request not found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cnam_change_request_not_found() -> None:
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        apply_cnam_display_name_change,
    )

    select_mock = AsyncMock(return_value=[])

    with (
        patch(f"{_CRON}.supabase_select", select_mock),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, return_value={}),
        patch(f"{_CRON}.thub.update_end_user", new_callable=AsyncMock) as update_eu_mock,
    ):
        result = await apply_cnam_display_name_change(REQUEST_ID)

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "REQUEST_NOT_FOUND"
    update_eu_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 9: Idempotency — re-run uses same idempotency keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cnam_change_idempotency_keys_deterministic() -> None:
    """Re-run with same request_id produces same Twilio idempotency keys."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        apply_cnam_display_name_change,
    )

    select_mock_1 = AsyncMock(side_effect=[[_make_request()], [_make_profile()]])
    select_mock_2 = AsyncMock(side_effect=[[_make_request()], [_make_profile()]])

    captured_keys: list[str] = []

    async def _update_eu(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured_keys.append(kwargs.get("idempotency_key", ""))
        return {}

    async def _submit_tp(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured_keys.append(kwargs.get("idempotency_key", ""))
        return {}

    common_patches = [
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, return_value={}),
        patch(f"{_CRON}.thub.update_end_user", new_callable=AsyncMock, side_effect=_update_eu),
        patch(f"{_CRON}.thub.submit_trust_product", new_callable=AsyncMock, side_effect=_submit_tp),
        patch(f"{_CRON}.cut_trust_receipt", new_callable=AsyncMock, return_value="r"),
    ]

    with patch(f"{_CRON}.supabase_select", select_mock_1):
        for p in common_patches:
            p.start()
        try:
            await apply_cnam_display_name_change(REQUEST_ID)
        finally:
            for p in common_patches:
                p.stop()

    common_patches_2 = [
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, return_value={}),
        patch(f"{_CRON}.thub.update_end_user", new_callable=AsyncMock, side_effect=_update_eu),
        patch(f"{_CRON}.thub.submit_trust_product", new_callable=AsyncMock, side_effect=_submit_tp),
        patch(f"{_CRON}.cut_trust_receipt", new_callable=AsyncMock, return_value="r"),
    ]
    with patch(f"{_CRON}.supabase_select", select_mock_2):
        for p in common_patches_2:
            p.start()
        try:
            await apply_cnam_display_name_change(REQUEST_ID)
        finally:
            for p in common_patches_2:
                p.stop()

    # First run: 1 update_eu + 1 submit; second run: same.
    # All update_eu keys identical, all submit_trust_product keys identical.
    assert captured_keys[0] == captured_keys[2]  # update_eu key stable
    assert captured_keys[1] == captured_keys[3]  # submit_trust_product key stable
    assert captured_keys[0] == f"cnam_change_update_eu:{REQUEST_ID}"
    assert captured_keys[1] == f"cnam_change_resubmit:{REQUEST_ID}"


# ---------------------------------------------------------------------------
# Test 10: DB update failure post-Twilio re-raises (so ARQ retries)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cnam_change_db_update_failure_reraises() -> None:
    """Twilio commits, then trust profile UPDATE fails → re-raise so ARQ retries."""
    from aspire_orchestrator.services.supabase_client import SupabaseClientError
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        apply_cnam_display_name_change,
    )

    select_mock = AsyncMock(side_effect=[[_make_request()], [_make_profile()]])

    with (
        patch(f"{_CRON}.supabase_select", select_mock),
        patch(
            f"{_CRON}.supabase_update",
            new_callable=AsyncMock,
            side_effect=SupabaseClientError("update/tenant_trust_profiles", 500, "boom"),
        ),
        patch(f"{_CRON}.thub.update_end_user", new_callable=AsyncMock, return_value={}),
        patch(f"{_CRON}.thub.submit_trust_product", new_callable=AsyncMock, return_value={}),
        pytest.raises(SupabaseClientError),
    ):
        await apply_cnam_display_name_change(REQUEST_ID)
