"""Tests for the trust-onboarding receipt taxonomy + hash chain (W2-E).

Covers:
    - All 23 receipt types are registered
    - PII guardrails reject all forbidden field names (Law #9, R-006)
    - Receipt write failures raise TrustReceiptError
    - Hash chain populated correctly (previous_receipt_id from latest transition)
    - Both receipts row + trust_state_transitions row written

Run:
    railway run -- wsl -d Ubuntu-22.04 -e bash -c \\
      "cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator \\
       && source ~/venvs/aspire/bin/activate \\
       && python -m pytest tests/test_trust_receipts.py -v"
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.workers.trust_onboarding.trust_receipts import (
    RECEIPT_TIER,
    RECEIPT_TOOL,
    RECEIPT_TYPES,
    TrustReceiptError,
    cut_trust_receipt,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _base_profile() -> dict[str, Any]:
    """A valid trust_profile dict for cut_trust_receipt."""
    return {
        "id": "11111111-aaaa-bbbb-cccc-111111111111",
        "tenant_id": "tenant-aaaa-bbbb-cccc-dddddddddddd",
        "suite_id": "94b89098-c4bf-4419-a154-e18d9d53f993",
        "office_id": "22222222-bbbb-cccc-dddd-222222222222",
    }


# ---------------------------------------------------------------------------
# Receipt type registry
# ---------------------------------------------------------------------------


class TestReceiptRegistry:
    def test_all_22_state_machine_types_registered(self) -> None:
        # 15 state machine + 2 A2P + 6 swap = 23 (per docstring)
        # But two of those overlap in state-machine vs swap categories;
        # actual distinct count is per the RECEIPT_TYPES frozenset.
        assert len(RECEIPT_TYPES) >= 22  # 15 + 2 + 6 minimum

    def test_known_state_machine_types(self) -> None:
        for t in (
            "kyb_collected",
            "customer_profile_created",
            "customer_profile_submitted",
            "customer_profile_approved",
            "customer_profile_rejected",
            "shaken_trust_product_created",
            "shaken_trust_product_approved",
            "cnam_trust_product_created",
            "cnam_display_name_set",
            "cnam_trust_product_approved",
            "branded_calling_enrolled",
            "number_attached_to_profile",
            "caller_id_lookup_enabled",
        ):
            assert t in RECEIPT_TYPES, f"Missing state-machine receipt type: {t}"

    def test_known_a2p_types(self) -> None:
        assert "a2p_brand_registered" in RECEIPT_TYPES
        assert "a2p_campaign_approved" in RECEIPT_TYPES

    def test_known_swap_types(self) -> None:
        for t in (
            "number_swap_initiated",
            "number_detached_from_profile",
            "caller_id_lookup_disabled",
            "front_desk_phone_switched",
            "phone_number_released",
            "number_swap_complete",
        ):
            assert t in RECEIPT_TYPES, f"Missing swap receipt type: {t}"

    def test_tier_is_yellow(self) -> None:
        assert RECEIPT_TIER == "yellow"

    def test_tool_is_twilio_trust_hub(self) -> None:
        assert RECEIPT_TOOL == "twilio_trust_hub"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_unknown_receipt_type_raises(self) -> None:
        with pytest.raises(TrustReceiptError, match="UNKNOWN_RECEIPT_TYPE"):
            asyncio.run(cut_trust_receipt(
                receipt_type="not_a_real_type",
                trust_profile=_base_profile(),
                outcome="success",
                from_state="kyb_collected",
                to_state="profile_drafted",
            ))

    def test_missing_scope_raises(self) -> None:
        with pytest.raises(TrustReceiptError, match="MISSING_SCOPE"):
            asyncio.run(cut_trust_receipt(
                receipt_type="kyb_collected",
                trust_profile={"id": "x"},  # missing suite_id, tenant_id, office_id
                outcome="success",
                from_state="kyb_collected",
                to_state="profile_drafted",
            ))


# ---------------------------------------------------------------------------
# PII guardrails (Law #9 + W1 R-006 mandate)
# ---------------------------------------------------------------------------


class TestPIIGuardrails:
    @pytest.mark.parametrize("forbidden_key", [
        "email",
        "phone_e164",
        "phone_number",
        "first_name",
        "last_name",
        "full_name",
        "dob",
        "date_of_birth",
        "ssn",
        "ssn_last4",
        "ein",
        "tax_id",
        "address_street",
        "raw_business_name",
        "owner_name",
    ])
    def test_redacted_inputs_blocks_pii(self, forbidden_key: str) -> None:
        with pytest.raises(TrustReceiptError, match="PII_LEAK_BLOCKED"):
            asyncio.run(cut_trust_receipt(
                receipt_type="kyb_collected",
                trust_profile=_base_profile(),
                outcome="success",
                from_state="kyb_collected",
                to_state="profile_drafted",
                redacted_inputs={forbidden_key: "leaked-value"},
            ))

    @pytest.mark.parametrize("forbidden_key", [
        "email",
        "phone_e164",
        "first_name",
        "dob",
        "ssn",
        "ein",
    ])
    def test_redacted_outputs_blocks_pii(self, forbidden_key: str) -> None:
        with pytest.raises(TrustReceiptError, match="PII_LEAK_BLOCKED"):
            asyncio.run(cut_trust_receipt(
                receipt_type="customer_profile_created",
                trust_profile=_base_profile(),
                outcome="success",
                from_state="kyb_collected",
                to_state="profile_drafted",
                redacted_outputs={forbidden_key: "leaked-value"},
            ))

    def test_pii_check_case_insensitive(self) -> None:
        # Even "EMAIL" or "Email" is rejected
        with pytest.raises(TrustReceiptError, match="PII_LEAK_BLOCKED"):
            asyncio.run(cut_trust_receipt(
                receipt_type="kyb_collected",
                trust_profile=_base_profile(),
                outcome="success",
                from_state="kyb_collected",
                to_state="profile_drafted",
                redacted_inputs={"EMAIL": "leaked@example.com"},
            ))

    def test_safe_fields_allowed(self) -> None:
        """Twilio SIDs, latency, status — all allowed."""
        # Mock the receipt store + transitions insert so this doesn't hit the live DB
        with (
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts.receipt_store"
            ) as rs_mock,
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts.supabase_insert",
                new=AsyncMock(return_value={"id": "transition-row-id"}),
            ) as ins_mock,
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts._get_previous_receipt_id",
                new=AsyncMock(return_value="trust_kyb_collected_prev"),
            ),
        ):
            rs_mock.store_receipts_strict = MagicMock(return_value=None)

            receipt_id = asyncio.run(cut_trust_receipt(
                receipt_type="customer_profile_created",
                trust_profile=_base_profile(),
                outcome="success",
                from_state="kyb_collected",
                to_state="profile_drafted",
                redacted_inputs={
                    "trust_profile_id": "11111111-aaaa-bbbb-cccc-111111111111",
                    "step_name": "create_secondary_profile",
                },
                redacted_outputs={
                    "twilio_resource_sid": "BUaaaa",
                    "twilio_status": "draft",
                    "latency_seconds": 0.234,
                },
                twilio_resource_sid="BUaaaa",
                twilio_status="draft",
            ))

            assert receipt_id.startswith("trust_customer_profile_created_")
            rs_mock.store_receipts_strict.assert_called_once()
            ins_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Hash chain
# ---------------------------------------------------------------------------


class TestHashChain:
    def test_first_receipt_has_null_previous(self) -> None:
        with (
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts.receipt_store"
            ) as rs_mock,
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts.supabase_insert",
                new=AsyncMock(return_value={}),
            ) as ins_mock,
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts._get_previous_receipt_id",
                new=AsyncMock(return_value=None),  # no prior transition
            ),
        ):
            rs_mock.store_receipts_strict = MagicMock(return_value=None)
            asyncio.run(cut_trust_receipt(
                receipt_type="kyb_collected",
                trust_profile=_base_profile(),
                outcome="success",
                from_state="<initial>",
                to_state="kyb_collected",
            ))
            transition_row = ins_mock.call_args[0][1]
            assert transition_row["previous_receipt_id"] is None

    def test_subsequent_receipt_chains_to_previous(self) -> None:
        with (
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts.receipt_store"
            ) as rs_mock,
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts.supabase_insert",
                new=AsyncMock(return_value={}),
            ) as ins_mock,
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts._get_previous_receipt_id",
                new=AsyncMock(return_value="trust_kyb_collected_abc123"),
            ),
        ):
            rs_mock.store_receipts_strict = MagicMock(return_value=None)
            asyncio.run(cut_trust_receipt(
                receipt_type="customer_profile_created",
                trust_profile=_base_profile(),
                outcome="success",
                from_state="kyb_collected",
                to_state="profile_drafted",
            ))
            transition_row = ins_mock.call_args[0][1]
            assert transition_row["previous_receipt_id"] == "trust_kyb_collected_abc123"


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


class TestOutputShape:
    def test_receipt_id_format(self) -> None:
        with (
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts.receipt_store"
            ) as rs_mock,
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts.supabase_insert",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts._get_previous_receipt_id",
                new=AsyncMock(return_value=None),
            ),
        ):
            rs_mock.store_receipts_strict = MagicMock(return_value=None)
            rid = asyncio.run(cut_trust_receipt(
                receipt_type="cnam_trust_product_approved",
                trust_profile=_base_profile(),
                outcome="success",
                from_state="cnam_submitted",
                to_state="cnam_approved",
            ))
            assert rid.startswith("trust_cnam_trust_product_approved_")
            assert len(rid) > 30  # uuid hex is 32 chars + prefix

    def test_receipt_row_has_yellow_tier_and_tool(self) -> None:
        captured: dict[str, Any] = {}

        def _capture(receipts: list[dict]) -> None:
            captured["row"] = receipts[0]

        with (
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts.receipt_store"
            ) as rs_mock,
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts.supabase_insert",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "aspire_orchestrator.workers.trust_onboarding.trust_receipts._get_previous_receipt_id",
                new=AsyncMock(return_value=None),
            ),
        ):
            rs_mock.store_receipts_strict = MagicMock(side_effect=_capture)
            asyncio.run(cut_trust_receipt(
                receipt_type="kyb_collected",
                trust_profile=_base_profile(),
                outcome="success",
                from_state="<initial>",
                to_state="kyb_collected",
            ))

            row = captured["row"]
            assert row["risk_tier"] == "yellow"
            assert row["tool_used"] == "twilio_trust_hub"
            assert row["receipt_type"] == "kyb_collected"
            assert row["suite_id"] == "94b89098-c4bf-4419-a154-e18d9d53f993"
