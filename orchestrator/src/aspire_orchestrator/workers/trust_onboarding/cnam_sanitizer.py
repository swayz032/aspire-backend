"""CNAM display name sanitizer.

Converts a tenant's free-form business name (e.g., "ACME Plumbing & Heating LLC")
into a CNAM-compliant display string (max 15 chars, alpha + digits + space +
period + comma; must start with a letter).

Twilio CNAM rules (from the Twilio CNAM docs the user pasted 2026-05-04):
    - Maximum 15 characters
    - Must be a UNIQUE name, not generic ("City/State" rejected)
    - Must START with a letter
    - Allowed character class: A-Z, 0-9, space, period, comma

The sanitizer is purely deterministic — same input always yields same output.
The state machine calls this on the tenant's `suite_profiles.business_name`
when creating the CNAM EndUser (Wave 5, Step 5 of the 8-step recipe).

Example transformations:

    "ACME Plumbing & Heating LLC"       -> "ACME PLUMBING"     (13 chars; & dropped, LLC stripped)
    "Scott Painting Services"           -> "SCOTT PAINTING"    (14 chars; truncated at word boundary)
    "The 24/7 Fix-It Co"                -> "THE 247 FIXIT"     (13 chars; / and - dropped, CO stripped)
    "123 Electric"                      -> "X 123 ELECTRIC"    (14 chars; X prefix because starts with digit)
    "Mom's Pet Grooming, Inc."          -> "MOMS PET GROOMI"   (15 chars; ' dropped, INC stripped, truncated)

Author: Aspire — Wave 2-B (per docs/plans/per-tenant-trust-hub-cnam.md §III)
"""

from __future__ import annotations

import re

__all__ = ["sanitize_cnam_display_name", "MAX_CNAM_LENGTH"]


MAX_CNAM_LENGTH: int = 15

# Common business-entity suffixes to strip from the END of the sanitized
# string. Order matters only for "PLLC" / "LLC" / "LP" / "LLP" — longer
# variants must be tried first so we don't strip "LL" off "PLLC".
_TRAILING_SUFFIXES: tuple[str, ...] = (
    "PLLC",
    "LLP",
    "LLC",
    "INC",
    "CORP",
    "LTD",
    "CO",
    "LP",
    "PC",
    "PA",
)

# Regex that matches anything OUTSIDE the CNAM-allowed character class.
# Allowed: uppercase letters, digits, whitespace, period, comma.
# Anything else (e.g. & ' / - ! @ #) is dropped entirely (NOT replaced
# with a space) so "24/7" -> "247" and "Fix-It" -> "FIXIT" per Twilio
# docs intent.
_DISALLOWED_CHARS = re.compile(r"[^A-Z0-9\s.,]")
_MULTI_WHITESPACE = re.compile(r"\s+")


def sanitize_cnam_display_name(raw_name: str) -> str:
    """Convert a free-form business name to a 15-char CNAM display string.

    Args:
        raw_name: The tenant's `suite_profiles.business_name` (or equivalent).

    Returns:
        A CNAM-compliant string (1–15 chars, starts with a letter, contains
        only A-Z 0-9 space period comma).

    Raises:
        ValueError: When `raw_name` is empty or sanitization yields an empty
            result (e.g., input was only special characters). The state
            machine treats this as a fatal config error and surfaces it as
            a `cnam_display_name_invalid` receipt.
    """
    if raw_name is None or not str(raw_name).strip():
        raise ValueError(
            f"Cannot derive CNAM display name from empty input: {raw_name!r}"
        )

    s = str(raw_name).strip().upper()
    s = _DISALLOWED_CHARS.sub("", s)
    s = _MULTI_WHITESPACE.sub(" ", s).strip()
    s = _strip_trailing_suffixes(s)
    s = s.rstrip(".,").strip()
    s = _truncate_at_word_boundary(s, MAX_CNAM_LENGTH)
    s = _ensure_starts_with_letter(s, MAX_CNAM_LENGTH)

    if not s.strip():
        raise ValueError(
            f"Sanitization of {raw_name!r} produced empty CNAM display name"
        )

    return s


def _strip_trailing_suffixes(s: str) -> str:
    """Strip business-entity suffixes (LLC/INC/CORP/etc.) from the end.

    Repeats up to 3x to handle stacked suffixes like "ACME LLC INC" or
    legitimately rare cases. Each pass tries longest suffixes first.
    """
    for _ in range(3):
        previous = s
        for suffix in _TRAILING_SUFFIXES:
            # \b for word boundary; allow trailing comma/period/space
            pattern = rf"\b{suffix}\b\s*[,.]?\s*$"
            new_s = re.sub(pattern, "", s).strip()
            if new_s != s:
                s = new_s
                break
        if s == previous:
            break
    return s


def _truncate_at_word_boundary(s: str, max_len: int) -> str:
    """Truncate to `max_len` chars, preferring word boundaries when possible.

    If the truncation point falls inside a word AND there's a space late
    enough in the string (>= 8 chars in), back up to that space. Otherwise
    truncate hard at `max_len`.
    """
    if len(s) <= max_len:
        return s
    truncated = s[:max_len]
    last_space = truncated.rfind(" ")
    if last_space >= 8:  # don't truncate too aggressively
        truncated = truncated[:last_space]
    return truncated.rstrip()


def _ensure_starts_with_letter(s: str, max_len: int) -> str:
    """If `s` doesn't start with a letter, prepend "X " and re-truncate.

    Twilio CNAM requires display names to start with a letter. Common
    failure mode: "123 Electric" or ",Acme" after sanitization.
    """
    if not s:
        return s
    if s[0].isalpha():
        return s
    candidate = ("X " + s).strip()
    return candidate[:max_len].rstrip()
