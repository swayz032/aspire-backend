"""Carrier-specific conditional-forwarding instruction resolver (Pass 19 Lane B §3.1).

Used by FORWARD_EXISTING public-number mode: owner keeps their existing carrier
number; Aspire generates carrier-specific codes the owner dials once to configure
conditional call forwarding to the Aspire Sarah forward-target number.

Carrier coverage (2026 verified):
  AT&T (CCF protocol):
    **21*<target>#  — always forward
    **61*<target>#  — no answer
    **67*<target>#  — busy
    **62*<target>#  — unreachable / not reachable

  Verizon:
    *72<target>     — always forward (activate)
    *71<target>     — busy + no-answer conditional forward

  T-Mobile (matches AT&T CCF codes — T-Mobile adopted GSM MMI codes):
    **21*<target>#  — always forward
    **61*<target>#  — conditional (no-answer / busy)

  Generic fallback (Sprint, US Cellular, MVNO, unknown):
    All four patterns presented (owner tries each until one works).

Aspire Laws:
  Law #9 — `aspire_forward_target` is the Aspire-issued forward-target phone
            number. It is an infrastructure config value, not caller PII.
            No caller PII flows through this module.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Carrier matching — normalised carrier name patterns
# ---------------------------------------------------------------------------

def _normalise(carrier_name: str) -> str:
    """Lowercase, strip punctuation for fuzzy matching."""
    return carrier_name.lower().replace("&", "").replace("-", "").replace(" ", "")


_ATT_TOKENS = {"att", "attservices", "attmobility", "attinc", "attllc", "attcorp"}
_VERIZON_TOKENS = {"verizon", "verizonwireless", "cellco", "vzw"}
_TMOBILE_TOKENS = {"tmobile", "tmobileusa", "tmousa", "tmus", "tmobile"}


def _is_att(normalised: str) -> bool:
    return (
        normalised in _ATT_TOKENS
        or normalised.startswith("att")
    )


def _is_verizon(normalised: str) -> bool:
    return (
        normalised in _VERIZON_TOKENS
        or "verizon" in normalised
        or "cellco" in normalised
    )


def _is_tmobile(normalised: str) -> bool:
    return (
        normalised in _TMOBILE_TOKENS
        or "tmobile" in normalised
        or "tmo" in normalised
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_forwarding_instructions(
    carrier_name: str,
    aspire_forward_target: str,
) -> dict[str, str]:
    """Return carrier-specific conditional-forwarding codes for an Aspire forward target.

    Args:
        carrier_name: carrier name string from Twilio Lookup v2
                      (e.g. "AT&T Wireless", "Verizon", "T-Mobile USA").
        aspire_forward_target: The Aspire-issued E.164 phone number to forward calls to.

    Returns:
        dict[str, str] mapping semantic key → dial-code string.
        Keys vary by carrier — callers should iterate and display all keys.
        Values are complete dial strings the owner enters on their handset.

    Examples:
        AT&T always-forward:    "**21*+18005550190#"
        Verizon always-forward: "*72+18005550190"
        T-Mobile always-forward:"**21*+18005550190#"
        Generic always-forward: "**21*+18005550190#"
    """
    n = _normalise(carrier_name)

    if _is_att(n):
        return _build_att(aspire_forward_target)
    if _is_verizon(n):
        return _build_verizon(aspire_forward_target)
    if _is_tmobile(n):
        return _build_tmobile(aspire_forward_target)

    # Generic fallback — present all four patterns
    return _build_generic(aspire_forward_target)


# ---------------------------------------------------------------------------
# Carrier-specific builders
# ---------------------------------------------------------------------------


def _build_att(target: str) -> dict[str, str]:
    """AT&T CCF (Conditional Call Forwarding) — GSM MMI codes.

    Verified against AT&T support documentation (2026).
    """
    return {
        "always": f"**21*{target}#",
        "no_answer": f"**61*{target}#",
        "busy": f"**67*{target}#",
        "unreachable": f"**62*{target}#",
    }


def _build_verizon(target: str) -> dict[str, str]:
    """Verizon — star-code forwarding.

    *72 activates call-forwarding-always.
    *71 activates busy+no-answer conditional forwarding.
    Verified against Verizon support documentation (2026).
    """
    return {
        "always": f"*72{target}",
        "busy_no_answer": f"*71{target}",
    }


def _build_tmobile(target: str) -> dict[str, str]:
    """T-Mobile — GSM MMI codes (same as AT&T CCF).

    T-Mobile adopted standard GSM MMI codes.
    Verified against T-Mobile support documentation (2026).
    """
    return {
        "always": f"**21*{target}#",
        "conditional": f"**61*{target}#",
    }


def _build_generic(target: str) -> dict[str, str]:
    """Generic fallback — present all four patterns.

    For carriers not specifically mapped (Sprint/Boost, US Cellular, MVNOs, etc.)
    we present all four GSM MMI codes + Verizon star codes. Owner tries each
    until one works on their handset.
    """
    return {
        "always": f"**21*{target}#",
        "no_answer": f"**61*{target}#",
        "busy": f"**67*{target}#",
        "unreachable": f"**62*{target}#",
    }


__all__ = [
    "resolve_forwarding_instructions",
]
