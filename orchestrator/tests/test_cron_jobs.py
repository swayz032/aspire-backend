"""Tests for Wave 9 — trust-onboarding cron jobs.

Covers:
  - poll_trust_status_for_tenants:
      * stuck-state detection (24h+ in *_submitted)
      * idempotency (re-run is safe)
      * _MAX_TENANTS_PER_RUN cap respected
      * RetryableError propagates to ARQ
      * No advance when Twilio still pending
      * Receipt cut on mismatch detection (reason_code='cron_reconcile')
      * Empty candidate list returns examined=0

  - poll_carrier_reputation:
      * Feature flag off → no-op (skipped_feature_flag_off=True)
      * Reputation change cuts a receipt
      * No change → no receipt
      * Twilio error on one tenant doesn't break the loop

  - enqueue_cnam_display_name_changes:
      * Cooldown enforcement (last_cnam_change_at < 30d ago → cooldown_pending)
      * Valid request (NULL last_cnam_change_at) → ARQ enqueue
      * Expired/old request with cooldown met → enqueue
      * Missing trust profile → marked failed

Author: Aspire — Wave 9
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
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
CNAM_END_USER_SID = "ITaaaaaaaaaaaaaaaaaaaaaaaaaaaaCNAMEU"
CNAM_TRUST_PRODUCT_SID = "BUaaaaaaaaaaaaaaaaaaaaaaaaaaaaCNAMTP"
CUSTOMER_PROFILE_SID = "BUaaaaaaaaaaaaaaaaaaaaaaaaaaaaaCPSID"
SHAKEN_TRUST_PRODUCT_SID = "BUaaaaaaaaaaaaaaaaaaaaaaaaaaaaSHAKEN"


def _make_profile(
    trust_state: str = "profile_submitted",
    cp_sid: str = CUSTOMER_PROFILE_SID,
    shaken_sid: str = SHAKEN_TRUST_PRODUCT_SID,
    cnam_sid: str = CNAM_TRUST_PRODUCT_SID,
    cnam_eu_sid: str = CNAM_END_USER_SID,
    last_reputation_check: str | None = None,
    last_reputation_status: dict[str, Any] | None = None,
    last_cnam_change_at: str | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": profile_id or TRUST_PROFILE_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "office_id": OFFICE_ID,
        "trust_state": trust_state,
        "customer_profile_sid": cp_sid,
        "shaken_trust_product_sid": shaken_sid,
        "cnam_trust_product_sid": cnam_sid,
        "cnam_end_user_sid": cnam_eu_sid,
        "last_reputation_check": last_reputation_check,
        "last_reputation_status": last_reputation_status,
        "last_cnam_change_at": last_cnam_change_at,
    }


# ---------------------------------------------------------------------------
# Section 1 — poll_trust_status_for_tenants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_trust_status_empty_candidates_returns_zero() -> None:
    """No stuck tenants → examined=0, reconciled=0, no Twilio calls."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        poll_trust_status_for_tenants,
    )

    with patch(f"{_CRON}.supabase_select", new_callable=AsyncMock, return_value=[]):
        result = await poll_trust_status_for_tenants()

    assert result["examined"] == 0
    assert result["reconciled"] == 0
    assert result["twilio_errors"] == 0
    assert result["max_tenants_capped"] is False


@pytest.mark.asyncio
async def test_poll_trust_status_detects_mismatch_and_enqueues() -> None:
    """profile_submitted in DB + twilio-approved at Twilio → enqueue advance."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        poll_trust_status_for_tenants,
    )

    profile = _make_profile(trust_state="profile_submitted")

    with (
        patch(f"{_CRON}.supabase_select", new_callable=AsyncMock, return_value=[profile]),
        patch(
            f"{_CRON}.thub.fetch_customer_profile_status",
            new_callable=AsyncMock,
            return_value="twilio-approved",
        ),
        patch(f"{_CRON}.cut_trust_receipt", new_callable=AsyncMock, return_value="trust_receipt_01"),
        patch(
            f"{_CRON}._enqueue_advance_trust_state",
            new_callable=AsyncMock,
            return_value=True,
        ) as enqueue_mock,
    ):
        result = await poll_trust_status_for_tenants()

    assert result["examined"] == 1
    assert result["reconciled"] == 1
    assert result["twilio_errors"] == 0
    enqueue_mock.assert_awaited_once_with(TRUST_PROFILE_ID, job_suffix="cron_reconcile")


@pytest.mark.asyncio
async def test_poll_trust_status_no_advance_when_twilio_still_pending() -> None:
    """Twilio still in 'pending-review' → no enqueue, no receipt."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        poll_trust_status_for_tenants,
    )

    profile = _make_profile(trust_state="cnam_submitted")

    with (
        patch(f"{_CRON}.supabase_select", new_callable=AsyncMock, return_value=[profile]),
        patch(
            f"{_CRON}.thub.fetch_trust_product_status",
            new_callable=AsyncMock,
            return_value="pending-review",
        ),
        patch(f"{_CRON}.cut_trust_receipt", new_callable=AsyncMock) as cut_mock,
        patch(f"{_CRON}._enqueue_advance_trust_state", new_callable=AsyncMock) as enqueue_mock,
    ):
        result = await poll_trust_status_for_tenants()

    assert result["examined"] == 1
    assert result["reconciled"] == 0
    cut_mock.assert_not_awaited()
    enqueue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_trust_status_max_tenants_per_run_cap() -> None:
    """When more than _MAX_TENANTS_PER_RUN candidates returned, cap at limit."""
    from aspire_orchestrator.workers.trust_onboarding import cron_jobs as _cj
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        poll_trust_status_for_tenants,
    )

    cap = _cj._MAX_TENANTS_PER_RUN
    # Return cap+5 candidates so the cron knows we're capped
    candidates = [
        _make_profile(profile_id=str(uuid.uuid4()), trust_state="profile_submitted")
        for _ in range(cap + 5)
    ]

    with (
        patch(f"{_CRON}.supabase_select", new_callable=AsyncMock, return_value=candidates),
        patch(
            f"{_CRON}.thub.fetch_customer_profile_status",
            new_callable=AsyncMock,
            return_value="pending-review",  # no advances; just count examined
        ),
    ):
        result = await poll_trust_status_for_tenants()

    assert result["examined"] == cap
    assert result["max_tenants_capped"] is True


@pytest.mark.asyncio
async def test_poll_trust_status_retryable_error_propagates() -> None:
    """RetryableError from Twilio must propagate so ARQ retries the cron run."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        poll_trust_status_for_tenants,
    )

    profile = _make_profile(trust_state="shaken_submitted")

    with (
        patch(f"{_CRON}.supabase_select", new_callable=AsyncMock, return_value=[profile]),
        patch(
            f"{_CRON}.thub.fetch_trust_product_status",
            new_callable=AsyncMock,
            side_effect=RetryableError("TWILIO_TRANSIENT", "503"),
        ),
        pytest.raises(RetryableError),
    ):
        await poll_trust_status_for_tenants()


@pytest.mark.asyncio
async def test_poll_trust_status_twilio_error_isolates_to_one_tenant() -> None:
    """A non-retryable TrustHubError on tenant A must NOT block tenant B."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        poll_trust_status_for_tenants,
    )

    profile_a = _make_profile(profile_id=str(uuid.uuid4()), trust_state="profile_submitted")
    profile_b = _make_profile(profile_id=str(uuid.uuid4()), trust_state="profile_submitted")

    with (
        patch(
            f"{_CRON}.supabase_select",
            new_callable=AsyncMock,
            return_value=[profile_a, profile_b],
        ),
        patch(
            f"{_CRON}.thub.fetch_customer_profile_status",
            new_callable=AsyncMock,
            side_effect=[
                TrustHubError("TRUST_HUB_GET_FAILED", "404 not found", 404),
                "twilio-approved",
            ],
        ),
        patch(f"{_CRON}.cut_trust_receipt", new_callable=AsyncMock, return_value="r1"),
        patch(
            f"{_CRON}._enqueue_advance_trust_state",
            new_callable=AsyncMock,
            return_value=True,
        ) as enqueue_mock,
    ):
        result = await poll_trust_status_for_tenants()

    assert result["examined"] == 2
    assert result["reconciled"] == 1   # only B
    assert result["twilio_errors"] == 1  # only A
    enqueue_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_trust_status_skips_when_sid_missing() -> None:
    """profile_submitted without a customer_profile_sid → not reconciled (defensive)."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        poll_trust_status_for_tenants,
    )

    profile = _make_profile(trust_state="profile_submitted", cp_sid="")

    with (
        patch(f"{_CRON}.supabase_select", new_callable=AsyncMock, return_value=[profile]),
        patch(f"{_CRON}.thub.fetch_customer_profile_status", new_callable=AsyncMock) as fetch_mock,
    ):
        result = await poll_trust_status_for_tenants()

    assert result["examined"] == 1
    assert result["reconciled"] == 0
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_trust_status_idempotent_rerun_is_safe() -> None:
    """Re-running the cron with the same approved status enqueues again — but the
    ARQ _job_id de-dupes upstream. We just verify our side issues a deterministic
    enqueue and a single receipt per call."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        poll_trust_status_for_tenants,
    )

    profile = _make_profile(trust_state="cnam_submitted")

    enqueue_count = {"n": 0}

    async def _enqueue_count(*args: Any, **kwargs: Any) -> bool:
        enqueue_count["n"] += 1
        return True

    with (
        patch(f"{_CRON}.supabase_select", new_callable=AsyncMock, return_value=[profile]),
        patch(
            f"{_CRON}.thub.fetch_trust_product_status",
            new_callable=AsyncMock,
            return_value="twilio-approved",
        ),
        patch(f"{_CRON}.cut_trust_receipt", new_callable=AsyncMock, return_value="r1"),
        patch(
            f"{_CRON}._enqueue_advance_trust_state",
            new_callable=AsyncMock,
            side_effect=_enqueue_count,
        ),
    ):
        # Run twice
        result_1 = await poll_trust_status_for_tenants()
        result_2 = await poll_trust_status_for_tenants()

    assert result_1["reconciled"] == 1
    assert result_2["reconciled"] == 1
    assert enqueue_count["n"] == 2  # both runs enqueue; ARQ dedup is upstream


# ---------------------------------------------------------------------------
# Section 2 — poll_carrier_reputation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_carrier_reputation_skipped_when_feature_off() -> None:
    """branded_calling_enabled=False → no-op."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        poll_carrier_reputation,
    )

    with patch(f"{_CRON}.settings.branded_calling_enabled", False):
        result = await poll_carrier_reputation()

    assert result["skipped_feature_flag_off"] is True
    assert result["examined"] == 0


@pytest.mark.asyncio
async def test_poll_carrier_reputation_change_cuts_receipt() -> None:
    """Reputation change → DB update + receipt cut."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        poll_carrier_reputation,
    )

    profile = _make_profile(
        trust_state="number_attached",
        last_reputation_status={"overall": {"score": 90, "label": "trusted"}},
    )

    new_reputation = {
        "overall": {"score": 30, "label": "spam_likely"},
        "t_mobile": {"score": 30, "label": "spam_likely"},
        "att": {"score": 75, "label": "trusted"},
        "verizon": {"score": 60, "label": "neutral"},
    }

    with (
        patch(f"{_CRON}.settings.branded_calling_enabled", True),
        patch(f"{_CRON}.supabase_select", new_callable=AsyncMock, return_value=[profile]),
        patch(
            f"{_CRON}._fetch_carrier_reputation",
            new_callable=AsyncMock,
            return_value=new_reputation,
        ),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, return_value={}),
        patch(
            f"{_CRON}.cut_trust_receipt",
            new_callable=AsyncMock,
            return_value="trust_receipt_rep1",
        ) as cut_mock,
    ):
        result = await poll_carrier_reputation()

    assert result["examined"] == 1
    assert result["updated"] == 1
    cut_mock.assert_awaited_once()
    # Inspect the receipt arguments to verify Law #9 — no PII keys leak.
    call_kwargs = cut_mock.await_args.kwargs
    assert call_kwargs["receipt_type"] == "carrier_reputation_updated"
    redacted_outputs = call_kwargs["redacted_outputs"]
    forbidden_keys = {"phone_number", "phone_e164", "email", "first_name"}
    assert not (set(redacted_outputs.keys()) & forbidden_keys)


@pytest.mark.asyncio
async def test_poll_carrier_reputation_no_change_no_receipt() -> None:
    """Same reputation as last poll → no receipt cut."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        poll_carrier_reputation,
    )

    same_reputation = {"overall": {"score": 80, "label": "trusted"}}
    profile = _make_profile(
        trust_state="number_attached",
        last_reputation_status=same_reputation,
    )

    with (
        patch(f"{_CRON}.settings.branded_calling_enabled", True),
        patch(f"{_CRON}.supabase_select", new_callable=AsyncMock, return_value=[profile]),
        patch(
            f"{_CRON}._fetch_carrier_reputation",
            new_callable=AsyncMock,
            return_value=same_reputation,
        ),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, return_value={}),
        patch(f"{_CRON}.cut_trust_receipt", new_callable=AsyncMock) as cut_mock,
    ):
        result = await poll_carrier_reputation()

    assert result["examined"] == 1
    assert result["updated"] == 0
    cut_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Section 3 — enqueue_cnam_display_name_changes
# ---------------------------------------------------------------------------


def _make_change_request(
    request_id: str | None = None,
    status_value: str = "pending",
    trust_profile_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": request_id or str(uuid.uuid4()),
        "tenant_id": TENANT_ID,
        "suite_id": SUITE_ID,
        "trust_profile_id": trust_profile_id or TRUST_PROFILE_ID,
        "requested_display_name": "Scott Painting Pro",
        "sanitized_display_name": "SCOTT PAINTING",
        "status": status_value,
        "capability_token_id": "test-cap",
    }


@pytest.mark.asyncio
async def test_enqueue_cnam_changes_cooldown_pending_when_recent_change() -> None:
    """last_cnam_change_at < 30d ago → row flipped to cooldown_pending, not enqueued."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        enqueue_cnam_display_name_changes,
    )

    request = _make_change_request()
    recent_change = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    profile = _make_profile(
        trust_state="number_attached",
        last_cnam_change_at=recent_change,
    )

    select_mock = AsyncMock(side_effect=[[request], [profile]])
    with (
        patch(f"{_CRON}.supabase_select", select_mock),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, return_value={}) as update_mock,
        patch(f"{_CRON}._enqueue_apply_cnam_change", new_callable=AsyncMock) as enqueue_mock,
    ):
        result = await enqueue_cnam_display_name_changes()

    assert result["examined"] == 1
    assert result["enqueued"] == 0
    assert result["cooldown_pending"] == 1
    enqueue_mock.assert_not_awaited()
    # Update was called to flip status to cooldown_pending
    assert update_mock.await_count >= 1
    update_call = update_mock.await_args_list[0]
    assert update_call.args[2]["status"] == "cooldown_pending"


@pytest.mark.asyncio
async def test_enqueue_cnam_changes_null_last_change_enqueues() -> None:
    """last_cnam_change_at IS NULL → cooldown trivially met → ARQ enqueue."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        enqueue_cnam_display_name_changes,
    )

    request = _make_change_request()
    profile = _make_profile(
        trust_state="number_attached",
        last_cnam_change_at=None,
    )

    select_mock = AsyncMock(side_effect=[[request], [profile]])
    with (
        patch(f"{_CRON}.supabase_select", select_mock),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, return_value={}),
        patch(
            f"{_CRON}._enqueue_apply_cnam_change",
            new_callable=AsyncMock,
            return_value=True,
        ) as enqueue_mock,
    ):
        result = await enqueue_cnam_display_name_changes()

    assert result["examined"] == 1
    assert result["enqueued"] == 1
    enqueue_mock.assert_awaited_once_with(request["id"])


@pytest.mark.asyncio
async def test_enqueue_cnam_changes_old_change_satisfies_cooldown() -> None:
    """last_cnam_change_at > 30 days ago → cooldown met → enqueue."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        enqueue_cnam_display_name_changes,
    )

    request = _make_change_request()
    old_change = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    profile = _make_profile(
        trust_state="number_attached",
        last_cnam_change_at=old_change,
    )

    select_mock = AsyncMock(side_effect=[[request], [profile]])
    with (
        patch(f"{_CRON}.supabase_select", select_mock),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, return_value={}),
        patch(
            f"{_CRON}._enqueue_apply_cnam_change",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        result = await enqueue_cnam_display_name_changes()

    assert result["enqueued"] == 1
    assert result["cooldown_pending"] == 0


@pytest.mark.asyncio
async def test_enqueue_cnam_changes_no_trust_profile_marks_failed() -> None:
    """Missing trust profile (FK gone or race) → request marked failed."""
    from aspire_orchestrator.workers.trust_onboarding.cron_jobs import (
        enqueue_cnam_display_name_changes,
    )

    request = _make_change_request()

    select_mock = AsyncMock(side_effect=[[request], []])  # request exists; profile gone
    update_calls: list[dict[str, Any]] = []

    async def _update(table: str, _filter: str, payload: dict[str, Any]) -> dict[str, Any]:
        update_calls.append(payload)
        return {}

    with (
        patch(f"{_CRON}.supabase_select", select_mock),
        patch(f"{_CRON}.supabase_update", new_callable=AsyncMock, side_effect=_update),
        patch(f"{_CRON}._enqueue_apply_cnam_change", new_callable=AsyncMock) as enqueue_mock,
    ):
        result = await enqueue_cnam_display_name_changes()

    assert result["enqueued"] == 0
    enqueue_mock.assert_not_awaited()
    # _mark_cnam_request_failed should have written status=failed
    statuses = [c.get("status") for c in update_calls]
    assert "failed" in statuses
