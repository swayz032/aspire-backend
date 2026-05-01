"""Tests for forwarding_instructions service (Pass 19 Lane B).

Covers:
- AT&T: all 4 codes (always / no-answer / busy / unreachable)
- Verizon: 2 codes (always / busy-no-answer)
- T-Mobile: 2 codes (always / conditional)
- Generic fallback: all 4 patterns returned
- Carrier matching is case-insensitive
- aspire_forward_target interpolated correctly in all codes
- No PII in returned dict (phone number is input, not injected)
"""

from __future__ import annotations

import pytest

from aspire_orchestrator.services.forwarding_instructions import (
    resolve_forwarding_instructions,
)

_ASPIRE_FWD = "+18005550190"


class TestATTForwardingInstructions:
    """AT&T carrier codes — CCF protocol."""

    def test_att_always_forward_code(self) -> None:
        result = resolve_forwarding_instructions("AT&T Wireless", _ASPIRE_FWD)
        assert "always" in result
        assert result["always"] == f"**21*{_ASPIRE_FWD}#"

    def test_att_no_answer_forward_code(self) -> None:
        result = resolve_forwarding_instructions("AT&T Wireless", _ASPIRE_FWD)
        assert "no_answer" in result
        assert result["no_answer"] == f"**61*{_ASPIRE_FWD}#"

    def test_att_busy_forward_code(self) -> None:
        result = resolve_forwarding_instructions("AT&T Wireless", _ASPIRE_FWD)
        assert "busy" in result
        assert result["busy"] == f"**67*{_ASPIRE_FWD}#"

    def test_att_unreachable_forward_code(self) -> None:
        result = resolve_forwarding_instructions("AT&T Wireless", _ASPIRE_FWD)
        assert "unreachable" in result
        assert result["unreachable"] == f"**62*{_ASPIRE_FWD}#"

    def test_att_name_variants_case_insensitive(self) -> None:
        """'att', 'AT&T', 'at&t mobility' all match AT&T codes."""
        for name in ("att", "AT&T", "at&t mobility", "AT&T Services", "AT&T Inc"):
            result = resolve_forwarding_instructions(name, _ASPIRE_FWD)
            assert result["always"] == f"**21*{_ASPIRE_FWD}#", f"Failed for carrier name: {name}"


class TestVerizonForwardingInstructions:
    """Verizon carrier codes."""

    def test_verizon_always_forward_code(self) -> None:
        result = resolve_forwarding_instructions("Verizon", _ASPIRE_FWD)
        assert "always" in result
        assert result["always"] == f"*72{_ASPIRE_FWD}"

    def test_verizon_busy_no_answer_code(self) -> None:
        result = resolve_forwarding_instructions("Verizon", _ASPIRE_FWD)
        assert "busy_no_answer" in result
        assert result["busy_no_answer"] == f"*71{_ASPIRE_FWD}"

    def test_verizon_name_variants(self) -> None:
        for name in ("verizon", "Verizon Wireless", "VERIZON", "cellco partnership dba verizon"):
            result = resolve_forwarding_instructions(name, _ASPIRE_FWD)
            assert result["always"] == f"*72{_ASPIRE_FWD}", f"Failed for: {name}"


class TestTMobileForwardingInstructions:
    """T-Mobile carrier codes."""

    def test_tmobile_always_forward_code(self) -> None:
        result = resolve_forwarding_instructions("T-Mobile", _ASPIRE_FWD)
        assert "always" in result
        assert result["always"] == f"**21*{_ASPIRE_FWD}#"

    def test_tmobile_conditional_forward_code(self) -> None:
        result = resolve_forwarding_instructions("T-Mobile", _ASPIRE_FWD)
        assert "conditional" in result
        assert result["conditional"] == f"**61*{_ASPIRE_FWD}#"

    def test_tmobile_name_variants(self) -> None:
        for name in ("t-mobile", "T-Mobile USA", "TMOBILE", "t mobile", "T-Mobile US"):
            result = resolve_forwarding_instructions(name, _ASPIRE_FWD)
            assert result["always"] == f"**21*{_ASPIRE_FWD}#", f"Failed for: {name}"


class TestGenericFallbackForwardingInstructions:
    """Unknown carrier → generic fallback presents all 4 patterns."""

    def test_unknown_carrier_has_all_four_patterns(self) -> None:
        result = resolve_forwarding_instructions("US Cellular", _ASPIRE_FWD)
        assert "always" in result
        assert "no_answer" in result
        assert "busy" in result
        assert "unreachable" in result

    def test_empty_carrier_name_returns_generic(self) -> None:
        result = resolve_forwarding_instructions("", _ASPIRE_FWD)
        assert len(result) >= 4

    def test_none_like_carrier_returns_generic(self) -> None:
        result = resolve_forwarding_instructions("Unknown", _ASPIRE_FWD)
        assert len(result) >= 4

    def test_generic_codes_contain_forward_target(self) -> None:
        """All generic codes must interpolate the forward target."""
        target = "+18005550190"
        result = resolve_forwarding_instructions("Sprint", target)
        for code in result.values():
            assert target in code or code == "", f"Code does not contain target: {code}"


class TestForwardingInstructionsContract:
    """Contract: return type is dict[str, str], all values are non-empty strings."""

    def test_return_type_is_dict_str_str(self) -> None:
        result = resolve_forwarding_instructions("AT&T Wireless", _ASPIRE_FWD)
        assert isinstance(result, dict)
        for k, v in result.items():
            assert isinstance(k, str)
            assert isinstance(v, str)

    def test_all_values_are_non_empty(self) -> None:
        for carrier in ("AT&T Wireless", "Verizon", "T-Mobile", "Sprint"):
            result = resolve_forwarding_instructions(carrier, _ASPIRE_FWD)
            for k, v in result.items():
                assert v, f"Empty value for key={k} carrier={carrier}"

    def test_result_is_immutable_between_calls(self) -> None:
        """Calls with same args return equivalent but independent dicts."""
        r1 = resolve_forwarding_instructions("Verizon", _ASPIRE_FWD)
        r2 = resolve_forwarding_instructions("Verizon", _ASPIRE_FWD)
        assert r1 == r2
        r1["always"] = "MUTATED"
        assert r2["always"] != "MUTATED"
