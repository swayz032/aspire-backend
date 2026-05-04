"""Tests for the W10 backfill state machine.

Coverage targets (≥15 tests):
  1. Verify step — tenant has no row → step inserts (bootstrap path).
  2. Verify step — tenant has is_backfill=False profile → fail ALREADY_ONBOARDED.
  3. Verify step — tenant has is_backfill=True kyb_collected → resume via reuse.
  4. Bootstrap — missing suite_profiles → fail NO_SUITE_PROFILE.
  5. Bootstrap — missing tenant_id/office_id → fail MISSING_SCOPE.
  6. Bootstrap happy path — kyb_collected receipt cut with is_backfill=True.
  7. Reuse path — delegates to W3-W5 advance_trust_state for kyb_collected →
     profile_drafted.
  8. Step 7 ordering — invoking detach in profile_drafted state → DOES NOT
     fire (negative-path invariant).
  9. Step 7 happy path — shaken_approved triggers detach + W4 reuse.
 10. Step 7 missing config — SHARED_SHAKEN_BUNDLE_SID unset → fail.
 11. Step 7 idempotency — `number_detached_from_shared_profile` already cut
     → skipped on re-entry.
 12. Step 7 RetryableError — Twilio 5xx surfaces as RetryableError.
 13. Step 7 already-clean — no CEA found → success no-op + receipt cut.
 14. Terminal — number_attached cuts backfill_complete receipt.
 15. Terminal idempotency — backfill_complete only cut once.
 16. Cross-tenant — backfill for suite A never touches suite B's data.
 17. failed/suspended — terminal failure passthrough returns outcome=failed.

Author: Aspire — Wave 10
"""

from __future__ import annotations

import importlib
import sys
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


SUITE_A = str(uuid.uuid4())
SUITE_B = str(uuid.uuid4())
TENANT_A = str(uuid.uuid4())
OFFICE_A = str(uuid.uuid4())
TRUST_PROFILE_A = str(uuid.uuid4())
TRUST_PROFILE_B = str(uuid.uuid4())
WORKER_JOB_ID = "arq-job-bf-001"

SHARED_BUNDLE_SID = "BU-shared-master-shaken-0001"
NUMBER_SID = "PN-suite-a-number-0001"
RA_SID = "RN-suite-a-cea-0001"


def _suite_profile(suite_id: str = SUITE_A) -> dict[str, Any]:
    return {
        "suite_id": suite_id,
        "tenant_id": TENANT_A,
        "office_id": OFFICE_A,
        "business_name": "Scott Painting Services",
        "entity_type": "llc",
        "business_address_street": "123 Main",
        "business_address_city": "Ann Arbor",
        "business_address_state": "MI",
        "business_address_zip": "48104",
        "business_address_country": "US",
    }


def _trust_profile(
    suite_id: str = SUITE_A,
    trust_state: str = "kyb_collected",
    is_backfill: bool = True,
    profile_id: str = TRUST_PROFILE_A,
    **overrides: Any,
) -> dict[str, Any]:
    base = {
        "id": profile_id,
        "suite_id": suite_id,
        "tenant_id": TENANT_A,
        "office_id": OFFICE_A,
        "trust_state": trust_state,
        "is_backfill": is_backfill,
        "legal_business_name": "Scott Painting Services",
        "twilio_secondary_profile_sid": "BU-secondary-0001",
        "twilio_shaken_bundle_sid": "BU-shaken-0001",
        "twilio_cnam_bundle_sid": None,
    }
    base.update(overrides)
    return base


def _phone_row(suite_id: str = SUITE_A) -> dict[str, Any]:
    return {
        "id": "phone-row-aaaa",
        "suite_id": suite_id,
        "twilio_sid": NUMBER_SID,
        "phone_sid": NUMBER_SID,
        "phone_number": "+14482885386",
        "status": "active",
    }


def _patch_settings_with_shared() -> Any:
    """Patch settings.twilio_shared_shaken_bundle_sid for step 7 tests."""
    return patch(
        "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.settings",
        MagicMock(twilio_shared_shaken_bundle_sid=SHARED_BUNDLE_SID),
    )


# ---------------------------------------------------------------------------
# 1. Verify — bootstrap path
# ---------------------------------------------------------------------------


class TestVerifyAndBootstrap:

    @pytest.mark.asyncio
    async def test_no_existing_profile_triggers_bootstrap(self) -> None:
        """No tenant_trust_profiles row → insert bootstrap + cut kyb_collected."""
        select_responses = [
            [],                       # _load_trust_profile_by_suite (none)
            [_suite_profile()],       # _load_suite_profile
        ]
        cut_mock = AsyncMock(return_value="receipt-bootstrap-001")
        insert_mock = AsyncMock(return_value={"id": TRUST_PROFILE_A, **_trust_profile()})

        with patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(side_effect=select_responses),
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_insert",
            new=insert_mock,
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.cut_trust_receipt",
            new=cut_mock,
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            result = await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["from_state"] == "bootstrap"
        assert result["to_state"] == "kyb_collected"
        assert result["step"] == "bootstrap"
        # Receipt was cut with is_backfill=true.
        assert cut_mock.called
        kwargs = cut_mock.call_args.kwargs
        assert kwargs["receipt_type"] == "kyb_collected"
        assert kwargs["redacted_inputs"]["is_backfill"] is True

    @pytest.mark.asyncio
    async def test_existing_profile_is_backfill_false_refused(self) -> None:
        """Tenant already onboarded normally → fail ALREADY_ONBOARDED."""
        existing = _trust_profile(is_backfill=False, trust_state="number_attached")
        with patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(return_value=[existing]),
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            result = await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"
        assert result["reason_code"] == "ALREADY_ONBOARDED"
        assert result["step"] == "verify"

    @pytest.mark.asyncio
    async def test_bootstrap_missing_suite_profile_fails(self) -> None:
        select_responses = [
            [],   # _load_trust_profile_by_suite (none)
            [],   # _load_suite_profile (missing)
        ]
        with patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(side_effect=select_responses),
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            result = await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"
        assert result["reason_code"] == "NO_SUITE_PROFILE"

    @pytest.mark.asyncio
    async def test_bootstrap_missing_scope_fails(self) -> None:
        suite_profile_no_scope = {"suite_id": SUITE_A, "business_name": "X"}
        select_responses = [
            [],
            [suite_profile_no_scope],
        ]
        with patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(side_effect=select_responses),
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            result = await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"
        assert result["reason_code"] == "MISSING_SCOPE"


# ---------------------------------------------------------------------------
# 2. Reuse path — delegates to W3-W5 state machine
# ---------------------------------------------------------------------------


class TestReusePath:

    @pytest.mark.asyncio
    async def test_kyb_collected_delegates_to_w3(self) -> None:
        """When existing profile is kyb_collected, reuse path advances via W3."""
        existing = _trust_profile(trust_state="kyb_collected")
        inner_mock = AsyncMock(return_value={
            "trust_profile_id": TRUST_PROFILE_A,
            "from_state": "kyb_collected",
            "to_state": "profile_drafted",
            "outcome": "success",
            "receipt_id": "inner-receipt-001",
        })
        with patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(return_value=[existing]),
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.state_machine.advance_trust_state",
            new=inner_mock,
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            result = await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["to_state"] == "profile_drafted"
        assert result["step"] == "reuse"
        inner_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_state_returns_terminal_failure(self) -> None:
        existing = _trust_profile(trust_state="failed")
        with patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(return_value=[existing]),
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            result = await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"
        assert result["reason_code"] == "TERMINAL_FAILURE_STATE"


# ---------------------------------------------------------------------------
# 3. Step 7 ordering — HARD INVARIANT TESTS
# ---------------------------------------------------------------------------


class TestStep7Ordering:

    @pytest.mark.asyncio
    async def test_detach_does_NOT_fire_before_shaken_approved(self) -> None:
        """Critical: invoking on profile_drafted MUST NOT touch shared bundle.

        This is the W10 negative-path invariant. The reuse path is taken,
        and `delete_channel_endpoint_assignment` must never be called.
        """
        existing = _trust_profile(trust_state="profile_drafted")
        inner_mock = AsyncMock(return_value={
            "trust_profile_id": TRUST_PROFILE_A,
            "from_state": "profile_drafted",
            "to_state": "profile_submitted",
            "outcome": "success",
            "receipt_id": "inner-receipt-002",
        })
        delete_cea_mock = AsyncMock()
        with patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(return_value=[existing]),
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.state_machine.advance_trust_state",
            new=inner_mock,
        ), patch(
            "aspire_orchestrator.providers.twilio_trust_hub.delete_channel_endpoint_assignment",
            new=delete_cea_mock,
        ), patch(
            "aspire_orchestrator.providers.twilio_trust_hub.list_channel_endpoint_assignments",
            new=AsyncMock(return_value=[]),
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        # Detach must NOT have been called for any state before shaken_approved.
        delete_cea_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_shaken_approved_triggers_detach_then_w4_advance(self) -> None:
        """shaken_approved → detach from shared + W4 advance fire in same tick."""
        existing = _trust_profile(trust_state="shaken_approved")
        select_responses = [
            [existing],                # _load_trust_profile_by_suite
            [_phone_row()],            # _load_active_phone_number
            [],                        # idempotency check (no prior detach)
        ]
        list_cea_mock = AsyncMock(return_value=[
            {"sid": RA_SID, "channel_endpoint_sid": NUMBER_SID},
        ])
        delete_cea_mock = AsyncMock()
        cut_mock = AsyncMock(return_value="receipt-detach-001")
        inner_mock = AsyncMock(return_value={
            "trust_profile_id": TRUST_PROFILE_A,
            "from_state": "shaken_approved",
            "to_state": "cnam_created",
            "outcome": "success",
            "receipt_id": "inner-receipt-003",
        })

        with _patch_settings_with_shared(), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(side_effect=select_responses),
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.cut_trust_receipt",
            new=cut_mock,
        ), patch(
            "aspire_orchestrator.providers.twilio_trust_hub.list_channel_endpoint_assignments",
            new=list_cea_mock,
        ), patch(
            "aspire_orchestrator.providers.twilio_trust_hub.delete_channel_endpoint_assignment",
            new=delete_cea_mock,
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.state_machine.advance_trust_state",
            new=inner_mock,
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            result = await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["step"] == "detach_and_advance"
        delete_cea_mock.assert_called_once_with(
            SHARED_BUNDLE_SID, RA_SID, kind="trust_product"
        )
        # Receipt cut for the detach step
        cut_mock.assert_called()
        called_types = [c.kwargs.get("receipt_type") for c in cut_mock.call_args_list]
        assert "number_detached_from_shared_profile" in called_types

    @pytest.mark.asyncio
    async def test_detach_missing_shared_config_fails_closed(self) -> None:
        """ASPIRE_TWILIO_SHARED_SHAKEN_BUNDLE_SID unset → fail."""
        existing = _trust_profile(trust_state="shaken_approved")
        select_responses = [
            [existing],
            [_phone_row()],
            [],
        ]
        with patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.settings",
            MagicMock(twilio_shared_shaken_bundle_sid=""),  # unset
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(side_effect=select_responses),
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            result = await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "failed"
        assert result["reason_code"] == "SHARED_SHAKEN_NOT_CONFIGURED"
        assert result["step"] == "detach_from_shared"

    @pytest.mark.asyncio
    async def test_detach_idempotency_skips_when_receipt_exists(self) -> None:
        """Existing number_detached_from_shared_profile receipt → skip Twilio DELETE."""
        existing = _trust_profile(trust_state="shaken_approved")
        select_responses = [
            [existing],
            [_phone_row()],
            [{"id": "prior-tx", "receipt_id": "trust_number_detached_from_shared_profile_001"}],
        ]
        delete_cea_mock = AsyncMock()
        list_cea_mock = AsyncMock(return_value=[])
        cut_mock = AsyncMock(return_value="ignored")
        inner_mock = AsyncMock(return_value={
            "trust_profile_id": TRUST_PROFILE_A,
            "from_state": "shaken_approved",
            "to_state": "cnam_created",
            "outcome": "success",
            "receipt_id": "inner-r",
        })

        with _patch_settings_with_shared(), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(side_effect=select_responses),
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.cut_trust_receipt",
            new=cut_mock,
        ), patch(
            "aspire_orchestrator.providers.twilio_trust_hub.delete_channel_endpoint_assignment",
            new=delete_cea_mock,
        ), patch(
            "aspire_orchestrator.providers.twilio_trust_hub.list_channel_endpoint_assignments",
            new=list_cea_mock,
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.state_machine.advance_trust_state",
            new=inner_mock,
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        delete_cea_mock.assert_not_called()
        list_cea_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_detach_5xx_raises_retryable_error(self) -> None:
        """Twilio 5xx during list-CEA must surface as RetryableError for ARQ retry."""
        from aspire_orchestrator.providers.twilio_trust_hub import TrustHubError
        from aspire_orchestrator.services.resilience import RetryableError

        existing = _trust_profile(trust_state="shaken_approved")
        select_responses = [
            [existing],
            [_phone_row()],
            [],
        ]
        list_cea_mock = AsyncMock(side_effect=TrustHubError(
            "TWILIO_5XX", "Twilio 503 maintenance", 503,
        ))

        with _patch_settings_with_shared(), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(side_effect=select_responses),
        ), patch(
            "aspire_orchestrator.providers.twilio_trust_hub.list_channel_endpoint_assignments",
            new=list_cea_mock,
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            with pytest.raises(RetryableError):
                await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

    @pytest.mark.asyncio
    async def test_detach_no_cea_found_treats_as_clean(self) -> None:
        """list_channel_endpoint_assignments returns no match → success no-op."""
        existing = _trust_profile(trust_state="shaken_approved")
        select_responses = [
            [existing],
            [_phone_row()],
            [],
        ]
        # Returns assignments for OTHER numbers but not our target number
        list_cea_mock = AsyncMock(return_value=[
            {"sid": "RN-other-1", "channel_endpoint_sid": "PN-someone-else"},
        ])
        delete_cea_mock = AsyncMock()
        cut_mock = AsyncMock(return_value="receipt-noop-001")
        inner_mock = AsyncMock(return_value={
            "trust_profile_id": TRUST_PROFILE_A,
            "from_state": "shaken_approved",
            "to_state": "cnam_created",
            "outcome": "success",
            "receipt_id": "inner-r",
        })

        with _patch_settings_with_shared(), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(side_effect=select_responses),
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.cut_trust_receipt",
            new=cut_mock,
        ), patch(
            "aspire_orchestrator.providers.twilio_trust_hub.list_channel_endpoint_assignments",
            new=list_cea_mock,
        ), patch(
            "aspire_orchestrator.providers.twilio_trust_hub.delete_channel_endpoint_assignment",
            new=delete_cea_mock,
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.state_machine.advance_trust_state",
            new=inner_mock,
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            result = await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        # No actual DELETE issued, but receipt was still cut for audit.
        delete_cea_mock.assert_not_called()
        assert result["outcome"] == "success"
        types = [c.kwargs.get("receipt_type") for c in cut_mock.call_args_list]
        assert "number_detached_from_shared_profile" in types


# ---------------------------------------------------------------------------
# 4. Terminal — backfill_complete
# ---------------------------------------------------------------------------


class TestTerminalState:

    @pytest.mark.asyncio
    async def test_number_attached_cuts_backfill_complete(self) -> None:
        existing = _trust_profile(trust_state="number_attached")
        select_responses = [
            [existing],   # _load_trust_profile_by_suite
            [],           # idempotency check (no prior backfill_complete)
        ]
        cut_mock = AsyncMock(return_value="receipt-complete-001")
        with patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(side_effect=select_responses),
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.cut_trust_receipt",
            new=cut_mock,
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            result = await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        assert result["step"] == "complete"
        assert result["to_state"] == "number_attached"
        cut_mock.assert_called()
        kwargs = cut_mock.call_args.kwargs
        assert kwargs["receipt_type"] == "backfill_complete"
        # Law #2: redacted_inputs flags is_backfill=True
        assert kwargs["redacted_inputs"]["is_backfill"] is True

    @pytest.mark.asyncio
    async def test_backfill_complete_idempotent(self) -> None:
        """Re-entering on number_attached when receipt already cut → skip + success."""
        existing = _trust_profile(trust_state="number_attached")
        select_responses = [
            [existing],
            [{
                "id": "tx-prior",
                "receipt_id": "trust_backfill_complete_abc",
            }],
        ]
        cut_mock = AsyncMock()
        with patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(side_effect=select_responses),
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.cut_trust_receipt",
            new=cut_mock,
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            result = await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "success"
        # Receipt NOT re-cut (idempotency).
        cut_mock.assert_not_called()
        # Returns the prior receipt id.
        assert "backfill_complete" in (result["receipt_id"] or "")


# ---------------------------------------------------------------------------
# 5. Cross-tenant isolation (Law #6)
# ---------------------------------------------------------------------------


class TestCrossTenantIsolation:

    @pytest.mark.asyncio
    async def test_advance_for_suite_a_never_touches_suite_b(self) -> None:
        """Verify backfill SUITE_A never reads/writes SUITE_B-scoped data.

        Pattern: spy on supabase_select; assert every call's filter
        contains SUITE_A and never SUITE_B. Same for the active-phone
        load (suite_id filter).
        """
        existing_a = _trust_profile(suite_id=SUITE_A, trust_state="shaken_approved")
        select_calls: list[str] = []

        async def _fake_select(table: str, filters: str, **kwargs: Any) -> list[dict[str, Any]]:
            select_calls.append(filters)
            if "suite_id=eq." + SUITE_A in filters and table == "tenant_trust_profiles":
                return [existing_a]
            if "suite_id=eq." + SUITE_A in filters and table == "tenant_phone_numbers":
                return [_phone_row(SUITE_A)]
            return []

        list_cea_mock = AsyncMock(return_value=[])
        cut_mock = AsyncMock(return_value="r-001")
        inner_mock = AsyncMock(return_value={
            "trust_profile_id": TRUST_PROFILE_A,
            "from_state": "shaken_approved",
            "to_state": "cnam_created",
            "outcome": "success",
            "receipt_id": "r-002",
        })

        with _patch_settings_with_shared(), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(side_effect=_fake_select),
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.cut_trust_receipt",
            new=cut_mock,
        ), patch(
            "aspire_orchestrator.providers.twilio_trust_hub.list_channel_endpoint_assignments",
            new=list_cea_mock,
        ), patch(
            "aspire_orchestrator.workers.trust_onboarding.state_machine.advance_trust_state",
            new=inner_mock,
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        # Every call must reference SUITE_A; SUITE_B must never appear.
        for filters in select_calls:
            assert SUITE_B not in filters, (
                f"Cross-tenant leak detected: filter contains SUITE_B: {filters}"
            )
        # And at least one call MUST reference SUITE_A.
        assert any(SUITE_A in f for f in select_calls), (
            "No SUITE_A scope filter found — was state machine actually scoped?"
        )


# ---------------------------------------------------------------------------
# 6. Halt states — branded_calling_pending = halted
# ---------------------------------------------------------------------------


class TestPostTerminalHalt:

    @pytest.mark.asyncio
    async def test_branded_calling_pending_halts_cleanly(self) -> None:
        existing = _trust_profile(trust_state="branded_calling_pending")
        with patch(
            "aspire_orchestrator.workers.trust_onboarding.backfill_state_machine.supabase_select",
            new=AsyncMock(return_value=[existing]),
        ):
            from aspire_orchestrator.workers.trust_onboarding.backfill_state_machine import (
                advance_backfill,
            )
            result = await advance_backfill(SUITE_A, worker_job_id=WORKER_JOB_ID)

        assert result["outcome"] == "halted"
        assert result["step"] == "post_terminal"
        assert result["receipt_id"] is None
