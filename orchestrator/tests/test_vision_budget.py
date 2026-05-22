"""Tests for Wave 19.2 / Commit 4 VisionBudget cost meter.

Pure unit tests — no OpenAI, no env mutation between tests.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from aspire_orchestrator.services.blueprint.vision_budget import (
    VisionBudget,
    estimate_cost_usd,
)


class TestEstimateCostUsd:
    def test_known_model_uses_published_rate(self) -> None:
        # gpt-5.4-mini: $0.000150 / 1K in, $0.000600 / 1K out
        cost = estimate_cost_usd(
            model="gpt-5.4-mini", input_tokens=1000, output_tokens=1000,
        )
        assert cost == pytest.approx(0.000150 + 0.000600)

    def test_unknown_model_uses_safe_high_fallback(self) -> None:
        """An unknown model name must default to the gpt-5 tier rate, NOT zero.

        Failing closed prevents an accidentally-misnamed model from spending
        unbounded dollars before the cap kicks in.
        """
        cost = estimate_cost_usd(
            model="fictional-model-x", input_tokens=1000, output_tokens=1000,
        )
        # gpt-5 fallback: $0.0025 + $0.010 = $0.0125
        assert cost == pytest.approx(0.0125)


class TestVisionBudgetCapResolution:
    def test_default_cap_is_30_cents(self) -> None:
        # With no env var set, default is 0.30 USD.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ASPIRE_DREW_VISION_CAP_USD", None)
            b = VisionBudget()
        assert b.cap_usd == 0.30

    def test_env_var_overrides_cap(self) -> None:
        with patch.dict(os.environ, {"ASPIRE_DREW_VISION_CAP_USD": "1.50"}):
            b = VisionBudget()
        assert b.cap_usd == 1.50

    def test_invalid_env_falls_back_to_default(self) -> None:
        with patch.dict(os.environ, {"ASPIRE_DREW_VISION_CAP_USD": "not_a_number"}):
            b = VisionBudget()
        assert b.cap_usd == 0.30

    def test_zero_or_negative_env_falls_back_to_default(self) -> None:
        """Zero would disable the meter — fail closed, use default."""
        for raw in ("0", "0.0", "-1.5"):
            with patch.dict(os.environ, {"ASPIRE_DREW_VISION_CAP_USD": raw}):
                b = VisionBudget()
            assert b.cap_usd == 0.30, f"value {raw!r} should fall back to default"


class TestVisionBudgetCanSpend:
    def test_admits_first_call_unconditionally(self) -> None:
        b = VisionBudget(cap_usd=0.30)
        assert b.can_spend() is True

    def test_admits_until_cap_then_refuses(self) -> None:
        b = VisionBudget(cap_usd=0.10)
        # Spend $0.06 — still under cap.
        b.record_call(model="gpt-5", input_tokens=20_000, output_tokens=1_000)
        # 20000/1000 * $0.0025 + 1000/1000 * $0.010 = $0.05 + $0.010 = $0.06
        assert b.can_spend() is True
        # Spend another $0.06 — total $0.12 — over cap.
        b.record_call(model="gpt-5", input_tokens=20_000, output_tokens=1_000)
        assert b.can_spend() is False


class TestVisionBudgetRecordCall:
    def test_accumulates_tokens_and_cost(self) -> None:
        b = VisionBudget(cap_usd=1.00)
        b.record_call(model="gpt-5.4-mini", input_tokens=2000, output_tokens=500)
        b.record_call(model="gpt-5.4-mini", input_tokens=1000, output_tokens=250)
        assert b.tokens_input_used == 3000
        assert b.tokens_output_used == 750
        assert b.calls_made == 2
        # 3000/1000 * 0.000150 + 750/1000 * 0.000600 = 0.00045 + 0.00045 = 0.0009
        assert b.cost_usd_spent == pytest.approx(0.0009)

    def test_negative_token_counts_clamped_to_zero(self) -> None:
        """Defensive: a buggy upstream that reports negative tokens must not flip the counter."""
        b = VisionBudget(cap_usd=1.00)
        b.record_call(model="gpt-5.4-mini", input_tokens=-500, output_tokens=-100)
        assert b.tokens_input_used == 0
        assert b.tokens_output_used == 0


class TestVisionBudgetReceiptEvidence:
    def test_evidence_shape_matches_allowlist_keys(self) -> None:
        """The keys emitted MUST appear in _RECEIPT_EVIDENCE_KEY_ALLOWLIST."""
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            _RECEIPT_EVIDENCE_KEY_ALLOWLIST,
        )
        b = VisionBudget(cap_usd=0.30)
        ev = b.receipt_evidence()
        # Every key in evidence must be allow-listed; otherwise the cascade
        # receipt would silently drop them.
        for key in ev:
            assert key in _RECEIPT_EVIDENCE_KEY_ALLOWLIST, (
                f"vision budget evidence key {key!r} is not on the cascade "
                f"receipt allow-list — it would be silently dropped."
            )

    def test_abort_counter_records(self) -> None:
        b = VisionBudget(cap_usd=0.30)
        b.record_abort()
        b.record_abort()
        ev = b.receipt_evidence()
        assert ev["vision_calls_aborted_by_budget"] == 2
