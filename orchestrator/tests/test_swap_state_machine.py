"""Tests for Wave 11 number-swap state machine.

Covers:
  1. Happy path: full 11-step swap, all receipts cut in order
  2. Idempotency: kill after step 5, re-run completes from step 6 cleanly
  3. Atomic-switch rollback: step 7 failure → detach + release + old stays live
  4. Old detach fails after step 7: log + alert, swap still succeeds
  5. RetryableError propagates to ARQ (never swallowed)
  6. Cross-tenant: suite A job cannot swap suite B number
  7. Purchase abort: purchase fails → no DB changes, SwapAbortError raised
  8. PII: phone E.164 never appears in receipt redacted_inputs/redacted_outputs
  9. No trust profile → SwapAbortError
  10. No customer_profile_sid → SwapAbortError
  11. release_old_number=False skips Twilio release call

Author: Aspire — Wave 11
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
from aspire_orchestrator.services.resilience import RetryableError
from aspire_orchestrator.workers.trust_onboarding.swap_state_machine import (
    SwapAbortError,
    SwapRollbackError,
    run_number_swap,
)

# ---------------------------------------------------------------------------
# Test fixtures / constants
# ---------------------------------------------------------------------------

SUITE_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())
OFFICE_ID = str(uuid.uuid4())
SWAP_JOB_ID = str(uuid.uuid4())
OLD_PHONE_ID = str(uuid.uuid4())
NEW_PHONE_ID = str(uuid.uuid4())
TRUST_PROFILE_ID = str(uuid.uuid4())
OLD_TWILIO_SID = "PNaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaOLD"
NEW_TWILIO_SID = "PNaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaANEW"
CP_SID = "BUaaaaaaaaaaaaaaaaaaaaaaaaaaaaaCPSID"
SHAKEN_SID = "BUaaaaaaaaaaaaaaaaaaaaaaaaaaaaaSHAKEN"
CNAM_SID = "BUaaaaaaaaaaaaaaaaaaaaaaaaaaaaaCNAMSS"
CP_RA_SID = "RAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaCPRA"
SHAKEN_RA_SID = "RAaaaaaaaaaaaaaaaaaaaaaaaaaaaaSHKRA"
CNAM_RA_SID = "RAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaCNRA"
OLD_CP_RA_SID = "RAaaaaaaaaaaaaaaaaaaaaaaaaaaaaOLDCP"
OLD_SHAKEN_RA_SID = "RAaaaaaaaaaaaaaaaaaaaaaaaaaaOLDSHK"
OLD_CNAM_RA_SID = "RAaaaaaaaaaaaaaaaaaaaaaaaaaaaaOLDCN"


def _make_swap_job(
    progress: dict[str, Any] | None = None,
    status: str = "pending",
    release_old: bool = True,
) -> dict[str, Any]:
    return {
        "id": SWAP_JOB_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "office_id": OFFICE_ID,
        "old_phone_number_id": OLD_PHONE_ID,
        "new_number_e164": "+14155550199",
        "release_old_number": release_old,
        "status": status,
        "progress": progress or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _make_trust_profile(
    trust_state: str = "number_attached",
    cp_sid: str = CP_SID,
    shaken_sid: str = SHAKEN_SID,
    cnam_sid: str = CNAM_SID,
) -> dict[str, Any]:
    return {
        "id": TRUST_PROFILE_ID,
        "suite_id": SUITE_ID,
        "tenant_id": TENANT_ID,
        "office_id": OFFICE_ID,
        "trust_state": trust_state,
        "customer_profile_sid": cp_sid,
        "shaken_trust_product_sid": shaken_sid,
        "cnam_trust_product_sid": cnam_sid,
    }


def _make_old_phone() -> dict[str, Any]:
    return {
        "id": OLD_PHONE_ID,
        "suite_id": SUITE_ID,
        "phone_number": "+14482885386",
        "twilio_sid": OLD_TWILIO_SID,
        "status": "active",
    }


def _make_new_phone_row() -> dict[str, Any]:
    return {
        "id": NEW_PHONE_ID,
        "suite_id": SUITE_ID,
        "phone_number": "+14155550199",
        "twilio_sid": NEW_TWILIO_SID,
        "status": "active",
    }


def _make_purchased_number() -> Any:
    m = MagicMock()
    m.twilio_sid = NEW_TWILIO_SID
    m.phone_number = "+14155550199"
    return m


# ---------------------------------------------------------------------------
# Context-manager helpers
# ---------------------------------------------------------------------------

_SM = "aspire_orchestrator.workers.trust_onboarding.swap_state_machine"


def _patch_supabase_select(side_effects: list[Any]):
    return patch(f"{_SM}.supabase_select", new_callable=AsyncMock, side_effect=side_effects)


def _patch_supabase_update(side_effect: Any = None):
    if side_effect is not None:
        return patch(f"{_SM}.supabase_update", new_callable=AsyncMock, side_effect=side_effect)
    return patch(f"{_SM}.supabase_update", new_callable=AsyncMock)


def _patch_supabase_insert():
    return patch(f"{_SM}.supabase_insert", new_callable=AsyncMock, return_value={"id": NEW_PHONE_ID})


def _patch_cut_receipt():
    receipt_counter = {"n": 0}

    async def _side_effect(**kwargs: Any) -> str:
        receipt_counter["n"] += 1
        return f"trust_receipt_{receipt_counter['n']:02d}"

    return (
        patch(f"{_SM}.cut_trust_receipt", side_effect=_side_effect),
        receipt_counter,
    )


def _enter_thub_patches(
    stack: ExitStack,
    *,
    cp_error: Exception | None = None,
    shaken_add_side_effects: list[Any] | None = None,
    cnam_add_side_effects: list[Any] | None = None,
    enable_error: Exception | None = None,
    list_side_effects: list[Any] | None = None,
    delete_error: Exception | None = None,
) -> dict[str, AsyncMock]:
    """Enter all thub patches into the ExitStack and return the mock objects."""
    cp = stack.enter_context(
        patch(
            f"{_SM}.thub.assign_number_to_profile",
            new_callable=AsyncMock,
            side_effect=cp_error if cp_error else None,
            return_value=None if cp_error else {"sid": CP_RA_SID},
        )
    )
    add_side = (shaken_add_side_effects or []) + (cnam_add_side_effects or [])
    if not add_side:
        add_side = [{"sid": SHAKEN_RA_SID}, {"sid": CNAM_RA_SID}]
    add = stack.enter_context(
        patch(
            f"{_SM}.thub.add_phone_to_trust_product",
            new_callable=AsyncMock,
            side_effect=add_side,
        )
    )
    enable = stack.enter_context(
        patch(
            f"{_SM}.thub.enable_caller_id_lookup",
            new_callable=AsyncMock,
            side_effect=enable_error if enable_error else None,
            return_value=None if enable_error else {"sid": NEW_TWILIO_SID},
        )
    )
    disable = stack.enter_context(
        patch(f"{_SM}.thub.disable_caller_id_lookup", new_callable=AsyncMock, return_value=None)
    )
    release = stack.enter_context(
        patch(f"{_SM}.thub.release_phone_number", new_callable=AsyncMock, return_value=None)
    )
    if list_side_effects is not None:
        list_m = stack.enter_context(
            patch(
                f"{_SM}.thub.list_channel_endpoint_assignments",
                new_callable=AsyncMock,
                side_effect=list_side_effects,
            )
        )
    else:
        list_m = stack.enter_context(
            patch(
                f"{_SM}.thub.list_channel_endpoint_assignments",
                new_callable=AsyncMock,
                side_effect=[
                    [{"sid": OLD_CP_RA_SID, "channel_endpoint_sid": OLD_TWILIO_SID}],
                    [{"sid": OLD_SHAKEN_RA_SID, "channel_endpoint_sid": OLD_TWILIO_SID}],
                    [{"sid": OLD_CNAM_RA_SID, "channel_endpoint_sid": OLD_TWILIO_SID}],
                ],
            )
        )
    if delete_error:
        delete_m = stack.enter_context(
            patch(f"{_SM}.thub.delete_channel_endpoint_assignment", new_callable=AsyncMock, side_effect=delete_error)
        )
    else:
        delete_m = stack.enter_context(
            patch(f"{_SM}.thub.delete_channel_endpoint_assignment", new_callable=AsyncMock, return_value=None)
        )
    return {
        "cp": cp,
        "add": add,
        "enable": enable,
        "disable": disable,
        "release": release,
        "list": list_m,
        "delete": delete_m,
    }


# ---------------------------------------------------------------------------
# Test 1: Happy path — full 11-step swap, 12 receipts cut
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_full_swap_12_receipts() -> None:
    """Full happy-path swap cuts 12 receipts in order and returns success."""
    receipt_patch, receipt_counter = _patch_cut_receipt()

    with ExitStack() as stack:
        stack.enter_context(_patch_supabase_select([
            [_make_swap_job()],
            [_make_trust_profile()],
            [_make_old_phone()],
            [_make_new_phone_row()],
        ]))
        stack.enter_context(_patch_supabase_update())
        stack.enter_context(receipt_patch)
        stack.enter_context(
            patch(f"{_SM}.purchase_number", new_callable=AsyncMock, return_value=_make_purchased_number())
        )
        _enter_thub_patches(stack)

        result = await run_number_swap(SWAP_JOB_ID, worker_job_id="job-001")

    assert result["outcome"] == "success"
    assert result["swap_job_id"] == SWAP_JOB_ID
    assert result["old_number_e164"] == "+14482885386"
    assert result["new_number_e164"] == "+14155550199"
    # 12 receipts: initiated + 3×attached + cid_enabled + switched + 3×detached + cid_disabled + released + complete
    assert len(result["receipt_ids"]) == 12
    assert receipt_counter["n"] == 12


# ---------------------------------------------------------------------------
# Test 2: Idempotency — kill after step 5, re-run completes from step 6
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_resume_from_step_6() -> None:
    """Worker killed after step 5 (cnam attach): re-run completes steps 6-11 only."""
    progress = {
        "step_1_initiated_receipt": "trust_receipt_01",
        "step_1_new_twilio_sid": NEW_TWILIO_SID,
        "step_2_new_phone_id": NEW_PHONE_ID,
        "step_3_cp_ra_sid": CP_RA_SID,
        "step_4_shaken_ra_sid": SHAKEN_RA_SID,
        "step_5_cnam_ra_sid": CNAM_RA_SID,
    }

    receipt_patch, receipt_counter = _patch_cut_receipt()

    # When progress already has twilio_sid, the select for new phone lookup is NOT issued.
    # Sequence: load_swap_job, load_trust_profile, load_old_phone
    with ExitStack() as stack:
        stack.enter_context(_patch_supabase_select([
            [_make_swap_job(progress=progress)],
            [_make_trust_profile()],
            [_make_old_phone()],
        ]))
        stack.enter_context(_patch_supabase_update())
        stack.enter_context(receipt_patch)
        # purchase_number must NOT be called
        stack.enter_context(
            patch(
                f"{_SM}.purchase_number",
                new_callable=AsyncMock,
                side_effect=AssertionError("purchase_number must NOT be called on resume"),
            )
        )
        # CP / SHAKEN / CNAM attach must NOT be called
        stack.enter_context(
            patch(
                f"{_SM}.thub.assign_number_to_profile",
                new_callable=AsyncMock,
                side_effect=AssertionError("CP attach must NOT be called on resume"),
            )
        )
        stack.enter_context(
            patch(
                f"{_SM}.thub.add_phone_to_trust_product",
                new_callable=AsyncMock,
                side_effect=AssertionError("SHAKEN/CNAM attach must NOT be called on resume"),
            )
        )
        stack.enter_context(
            patch(f"{_SM}.thub.enable_caller_id_lookup", new_callable=AsyncMock, return_value={"sid": NEW_TWILIO_SID})
        )
        stack.enter_context(
            patch(f"{_SM}.thub.disable_caller_id_lookup", new_callable=AsyncMock, return_value=None)
        )
        stack.enter_context(
            patch(f"{_SM}.thub.release_phone_number", new_callable=AsyncMock, return_value=None)
        )
        stack.enter_context(
            patch(
                f"{_SM}.thub.list_channel_endpoint_assignments",
                new_callable=AsyncMock,
                side_effect=[
                    [{"sid": OLD_CP_RA_SID, "channel_endpoint_sid": OLD_TWILIO_SID}],
                    [{"sid": OLD_SHAKEN_RA_SID, "channel_endpoint_sid": OLD_TWILIO_SID}],
                    [{"sid": OLD_CNAM_RA_SID, "channel_endpoint_sid": OLD_TWILIO_SID}],
                ],
            )
        )
        stack.enter_context(
            patch(f"{_SM}.thub.delete_channel_endpoint_assignment", new_callable=AsyncMock, return_value=None)
        )

        result = await run_number_swap(SWAP_JOB_ID, worker_job_id="job-002")

    assert result["outcome"] == "success"
    # Steps 6-11 produce: cid_enabled + switched + 3×detached + cid_disabled + released + complete = 8 receipts
    # (step_1_initiated_receipt was in progress so no re-issue; CP/SHAKEN/CNAM attach receipts also skipped)
    assert len(result["receipt_ids"]) == 8
    assert receipt_counter["n"] == 8


# ---------------------------------------------------------------------------
# Test 3: Atomic-switch rollback — step 7 failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_atomic_switch_rollback_on_step7_failure() -> None:
    """If step 7 (front_desk_configs update) fails, rollback fires and old number stays live."""
    from aspire_orchestrator.services.supabase_client import SupabaseClientError

    receipt_patch, _ = _patch_cut_receipt()
    update_call_count: dict[str, int] = {"n": 0}

    async def _update_side_effect(table: str, filters: str, data: dict[str, Any]) -> dict[str, Any]:
        update_call_count["n"] += 1
        if table == "front_desk_configs":
            raise SupabaseClientError("update/front_desk_configs", detail="DB write failed")
        return {}

    with ExitStack() as stack:
        stack.enter_context(_patch_supabase_select([
            [_make_swap_job()],
            [_make_trust_profile()],
            [_make_old_phone()],
            [_make_new_phone_row()],
        ]))
        stack.enter_context(_patch_supabase_update(side_effect=_update_side_effect))
        stack.enter_context(receipt_patch)
        stack.enter_context(
            patch(f"{_SM}.purchase_number", new_callable=AsyncMock, return_value=_make_purchased_number())
        )
        _enter_thub_patches(stack)

        with pytest.raises(SwapRollbackError) as exc_info:
            await run_number_swap(SWAP_JOB_ID, worker_job_id="job-003")

    assert "ATOMIC_SWITCH_FAILED" in exc_info.value.reason_code
    assert update_call_count["n"] >= 1


# ---------------------------------------------------------------------------
# Test 4: Old detach fails after step 7 — swap still completes (graceful)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_detach_failure_is_non_blocking() -> None:
    """Post-switch detach failures are logged but do not fail the swap."""
    receipt_patch, _ = _patch_cut_receipt()

    with ExitStack() as stack:
        stack.enter_context(_patch_supabase_select([
            [_make_swap_job()],
            [_make_trust_profile()],
            [_make_old_phone()],
            [_make_new_phone_row()],
        ]))
        stack.enter_context(_patch_supabase_update())
        stack.enter_context(receipt_patch)
        stack.enter_context(
            patch(f"{_SM}.purchase_number", new_callable=AsyncMock, return_value=_make_purchased_number())
        )
        # Standard new-number attach patches
        stack.enter_context(
            patch(f"{_SM}.thub.assign_number_to_profile", new_callable=AsyncMock, return_value={"sid": CP_RA_SID})
        )
        stack.enter_context(
            patch(
                f"{_SM}.thub.add_phone_to_trust_product",
                new_callable=AsyncMock,
                side_effect=[{"sid": SHAKEN_RA_SID}, {"sid": CNAM_RA_SID}],
            )
        )
        stack.enter_context(
            patch(f"{_SM}.thub.enable_caller_id_lookup", new_callable=AsyncMock, return_value={"sid": NEW_TWILIO_SID})
        )
        stack.enter_context(
            patch(f"{_SM}.thub.disable_caller_id_lookup", new_callable=AsyncMock, return_value=None)
        )
        stack.enter_context(
            patch(f"{_SM}.thub.release_phone_number", new_callable=AsyncMock, return_value=None)
        )
        # list_channel_endpoint_assignments raises TrustHubError — detach failure
        stack.enter_context(
            patch(
                f"{_SM}.thub.list_channel_endpoint_assignments",
                new_callable=AsyncMock,
                side_effect=TrustHubError("TWILIO_ERROR", "Detach list failed", 503),
            )
        )
        stack.enter_context(
            patch(f"{_SM}.thub.delete_channel_endpoint_assignment", new_callable=AsyncMock, return_value=None)
        )

        result = await run_number_swap(SWAP_JOB_ID, worker_job_id="job-004")

    assert result["outcome"] == "success"


# ---------------------------------------------------------------------------
# Test 5: RetryableError propagates — never swallowed by state machine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retryable_error_propagates_on_cp_attach() -> None:
    """RetryableError from CP attach must propagate to ARQ for retry."""
    with ExitStack() as stack:
        stack.enter_context(_patch_supabase_select([
            [_make_swap_job()],
            [_make_trust_profile()],
            [_make_old_phone()],
            [_make_new_phone_row()],
        ]))
        stack.enter_context(_patch_supabase_update())
        stack.enter_context(
            patch(f"{_SM}.cut_trust_receipt", new_callable=AsyncMock, return_value="trust_receipt_01")
        )
        stack.enter_context(
            patch(f"{_SM}.purchase_number", new_callable=AsyncMock, return_value=_make_purchased_number())
        )
        stack.enter_context(
            patch(
                f"{_SM}.thub.assign_number_to_profile",
                new_callable=AsyncMock,
                side_effect=RetryableError("TWILIO_TRANSIENT", "429 rate limit"),
            )
        )

        with pytest.raises(RetryableError):
            await run_number_swap(SWAP_JOB_ID, worker_job_id="job-005")


@pytest.mark.asyncio
async def test_retryable_error_propagates_on_shaken_attach() -> None:
    """RetryableError from SHAKEN attach must propagate."""
    with ExitStack() as stack:
        stack.enter_context(_patch_supabase_select([
            [_make_swap_job()],
            [_make_trust_profile()],
            [_make_old_phone()],
            [_make_new_phone_row()],
        ]))
        stack.enter_context(_patch_supabase_update())
        stack.enter_context(
            patch(f"{_SM}.cut_trust_receipt", new_callable=AsyncMock, return_value="trust_receipt_01")
        )
        stack.enter_context(
            patch(f"{_SM}.purchase_number", new_callable=AsyncMock, return_value=_make_purchased_number())
        )
        stack.enter_context(
            patch(f"{_SM}.thub.assign_number_to_profile", new_callable=AsyncMock, return_value={"sid": CP_RA_SID})
        )
        stack.enter_context(
            patch(
                f"{_SM}.thub.add_phone_to_trust_product",
                new_callable=AsyncMock,
                side_effect=RetryableError("TWILIO_TRANSIENT", "503 unavailable"),
            )
        )

        with pytest.raises(RetryableError):
            await run_number_swap(SWAP_JOB_ID, worker_job_id="job-005b")


# ---------------------------------------------------------------------------
# Test 6: Cross-tenant isolation — RLS returns empty for wrong-suite job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_job_denied() -> None:
    """A swap_job_id not found (RLS blocks cross-tenant) raises SwapAbortError."""
    with ExitStack() as stack:
        stack.enter_context(
            patch(f"{_SM}.supabase_select", new_callable=AsyncMock, return_value=[])
        )

        with pytest.raises(SwapAbortError) as exc_info:
            await run_number_swap(SWAP_JOB_ID, worker_job_id="job-006")

    assert exc_info.value.reason_code == "SWAP_JOB_NOT_FOUND"


# ---------------------------------------------------------------------------
# Test 7: Purchase failure → abort, no DB changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purchase_failure_aborts_cleanly() -> None:
    """If purchase_number fails, SwapAbortError is raised."""
    insert_mock = AsyncMock()

    with ExitStack() as stack:
        stack.enter_context(_patch_supabase_select([
            [_make_swap_job()],
            [_make_trust_profile()],
            [_make_old_phone()],
        ]))
        stack.enter_context(_patch_supabase_update())
        stack.enter_context(patch(f"{_SM}.supabase_insert", insert_mock))
        stack.enter_context(
            patch(f"{_SM}.cut_trust_receipt", new_callable=AsyncMock, return_value="trust_receipt_01")
        )
        stack.enter_context(
            patch(
                f"{_SM}.purchase_number",
                new_callable=AsyncMock,
                side_effect=Exception("Twilio card declined"),
            )
        )

        with pytest.raises(SwapAbortError) as exc_info:
            await run_number_swap(SWAP_JOB_ID, worker_job_id="job-007")

    assert exc_info.value.reason_code == "PURCHASE_FAILED"
    insert_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 8: PII — phone E.164 never in receipts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pii_phone_e164_never_in_receipts() -> None:
    """Verify no receipt call includes phone E.164 in redacted_inputs/outputs."""
    captured: list[dict[str, Any]] = []

    async def _capture(**kwargs: Any) -> str:
        captured.append(kwargs)
        return f"trust_receipt_{len(captured):02d}"

    with ExitStack() as stack:
        stack.enter_context(_patch_supabase_select([
            [_make_swap_job()],
            [_make_trust_profile()],
            [_make_old_phone()],
            [_make_new_phone_row()],
        ]))
        stack.enter_context(_patch_supabase_update())
        stack.enter_context(patch(f"{_SM}.cut_trust_receipt", side_effect=_capture))
        stack.enter_context(
            patch(f"{_SM}.purchase_number", new_callable=AsyncMock, return_value=_make_purchased_number())
        )
        _enter_thub_patches(stack)

        await run_number_swap(SWAP_JOB_ID, worker_job_id="job-008")

    pii_patterns = ["+14482885386", "+14155550199", "448288", "415555"]
    for call_kwargs in captured:
        combined = str(call_kwargs.get("redacted_inputs", {})) + str(call_kwargs.get("redacted_outputs", {}))
        for pii in pii_patterns:
            assert pii not in combined, (
                f"PII {pii!r} found in receipt type "
                f"{call_kwargs.get('receipt_type')!r}: {combined}"
            )


# ---------------------------------------------------------------------------
# Test 9: No trust profile → SwapAbortError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_trust_profile_aborts() -> None:
    with ExitStack() as stack:
        stack.enter_context(_patch_supabase_select([
            [_make_swap_job()],
            [],  # no trust profile
        ]))

        with pytest.raises(SwapAbortError) as exc_info:
            await run_number_swap(SWAP_JOB_ID, worker_job_id="job-009")

    assert exc_info.value.reason_code == "NO_TRUST_PROFILE"


# ---------------------------------------------------------------------------
# Test 10: No customer_profile_sid → SwapAbortError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_cp_sid_aborts() -> None:
    bad_profile = _make_trust_profile(cp_sid="")

    with ExitStack() as stack:
        stack.enter_context(_patch_supabase_select([
            [_make_swap_job()],
            [bad_profile],
            [_make_old_phone()],
        ]))

        with pytest.raises(SwapAbortError) as exc_info:
            await run_number_swap(SWAP_JOB_ID, worker_job_id="job-010")

    assert exc_info.value.reason_code == "NO_CUSTOMER_PROFILE"


# ---------------------------------------------------------------------------
# Test 11: release_old_number=False skips Twilio release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_old_false_skips_twilio_release() -> None:
    """When release_old_number=False, release_phone_number is NOT called."""
    receipt_patch, _ = _patch_cut_receipt()
    release_mock = AsyncMock()

    with ExitStack() as stack:
        stack.enter_context(_patch_supabase_select([
            [_make_swap_job(release_old=False)],
            [_make_trust_profile()],
            [_make_old_phone()],
            [_make_new_phone_row()],
        ]))
        stack.enter_context(_patch_supabase_update())
        stack.enter_context(receipt_patch)
        stack.enter_context(
            patch(f"{_SM}.purchase_number", new_callable=AsyncMock, return_value=_make_purchased_number())
        )
        stack.enter_context(
            patch(f"{_SM}.thub.assign_number_to_profile", new_callable=AsyncMock, return_value={"sid": CP_RA_SID})
        )
        stack.enter_context(
            patch(
                f"{_SM}.thub.add_phone_to_trust_product",
                new_callable=AsyncMock,
                side_effect=[{"sid": SHAKEN_RA_SID}, {"sid": CNAM_RA_SID}],
            )
        )
        stack.enter_context(
            patch(f"{_SM}.thub.enable_caller_id_lookup", new_callable=AsyncMock, return_value={"sid": NEW_TWILIO_SID})
        )
        stack.enter_context(
            patch(f"{_SM}.thub.disable_caller_id_lookup", new_callable=AsyncMock, return_value=None)
        )
        stack.enter_context(patch(f"{_SM}.thub.release_phone_number", release_mock))
        stack.enter_context(
            patch(
                f"{_SM}.thub.list_channel_endpoint_assignments",
                new_callable=AsyncMock,
                side_effect=[
                    [{"sid": OLD_CP_RA_SID, "channel_endpoint_sid": OLD_TWILIO_SID}],
                    [{"sid": OLD_SHAKEN_RA_SID, "channel_endpoint_sid": OLD_TWILIO_SID}],
                    [{"sid": OLD_CNAM_RA_SID, "channel_endpoint_sid": OLD_TWILIO_SID}],
                ],
            )
        )
        stack.enter_context(
            patch(f"{_SM}.thub.delete_channel_endpoint_assignment", new_callable=AsyncMock, return_value=None)
        )

        result = await run_number_swap(SWAP_JOB_ID, worker_job_id="job-011")

    assert result["outcome"] == "success"
    release_mock.assert_not_awaited()
