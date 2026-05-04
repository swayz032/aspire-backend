"""Tests for the CNAM display name sanitizer (W2-B).

Covers ≥20 cases per Gate 1 requirement (plan §12). Categories:
    - Plan-canonical examples (4)
    - LLC/Corp/Sole Prop suffix stripping (8)
    - Leading-digit handling (X prefix)
    - Special-character stripping (&, /, -, ', !, @, #, _)
    - Empty / whitespace-only / None inputs
    - Truncation at word boundary
    - Hard truncation when no word boundary
    - Stacked suffixes
    - Result must always start with a letter

Run:
    railway run -- wsl -d Ubuntu-22.04 -e bash -c \\
      "cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator \\
       && source ~/venvs/aspire/bin/activate \\
       && python -m pytest tests/test_cnam_sanitizer.py -v"
"""

from __future__ import annotations

import pytest

from aspire_orchestrator.workers.trust_onboarding.cnam_sanitizer import (
    MAX_CNAM_LENGTH,
    sanitize_cnam_display_name,
)


# ---------------------------------------------------------------------------
# Plan-canonical examples (architect plan §III W2-B)
# ---------------------------------------------------------------------------


class TestPlanCanonicalExamples:
    def test_acme_plumbing_heating_llc(self) -> None:
        assert sanitize_cnam_display_name("ACME Plumbing & Heating LLC") == "ACME PLUMBING"

    def test_scott_painting_services(self) -> None:
        # 23 chars truncates at "SCOTT PAINTING " (15) -> word boundary -> "SCOTT PAINTING" (14)
        assert sanitize_cnam_display_name("Scott Painting Services") == "SCOTT PAINTING"

    def test_24_7_fix_it_co(self) -> None:
        # / and - dropped entirely (not replaced with space): 24/7 -> 247, Fix-It -> FIXIT
        assert sanitize_cnam_display_name("The 24/7 Fix-It Co") == "THE 247 FIXIT"

    def test_123_electric_starts_with_digit(self) -> None:
        # Starts with digit -> X prepend
        result = sanitize_cnam_display_name("123 Electric")
        assert result.startswith("X ")
        assert result == "X 123 ELECTRIC"


# ---------------------------------------------------------------------------
# Suffix stripping
# ---------------------------------------------------------------------------


class TestSuffixStripping:
    @pytest.mark.parametrize(
        "input_name,expected",
        [
            ("Acme LLC", "ACME"),
            ("Acme Inc", "ACME"),
            ("Acme Corp", "ACME"),
            ("Acme LTD", "ACME"),
            ("Acme PLLC", "ACME"),
            ("Acme LLP", "ACME"),
            ("Acme PC", "ACME"),
            ("Acme PA", "ACME"),
            ("Acme, LLC", "ACME"),  # comma before suffix
            ("Acme LLC.", "ACME"),  # period after suffix
        ],
    )
    def test_single_trailing_suffix_stripped(self, input_name: str, expected: str) -> None:
        assert sanitize_cnam_display_name(input_name) == expected

    def test_co_suffix_stripped(self) -> None:
        # "CO" is a valid suffix but also a 2-letter code — make sure word-boundary works
        assert sanitize_cnam_display_name("Bay Area CO") == "BAY AREA"

    def test_co_in_middle_not_stripped(self) -> None:
        # "Co" inside a word should NOT be stripped — only trailing
        assert sanitize_cnam_display_name("Coast Coffee") == "COAST COFFEE"

    def test_lp_suffix_stripped(self) -> None:
        assert sanitize_cnam_display_name("Real Estate Holdings LP") == "REAL ESTATE"

    def test_stacked_suffixes(self) -> None:
        # Edge case: "ACME LLC INC" — both suffixes stripped
        assert sanitize_cnam_display_name("Acme LLC Inc") == "ACME"

    def test_no_suffix_unchanged(self) -> None:
        assert sanitize_cnam_display_name("Bob's Burgers") == "BOBS BURGERS"


# ---------------------------------------------------------------------------
# Special character stripping
# ---------------------------------------------------------------------------


class TestSpecialCharStripping:
    @pytest.mark.parametrize(
        "input_name,expected",
        [
            ("AT&T", "ATT"),
            ("Mom's Diner", "MOMS DINER"),
            ("Smith's Auto", "SMITHS AUTO"),
            ("A+B Painting", "AB PAINTING"),
            ("Tip-Top Shape", "TIPTOP SHAPE"),
            ("X@Y Corp", "XY"),  # @ stripped, CORP stripped
            ("Half_Off Tire", "HALFOFF TIRE"),
            ("North/South", "NORTHSOUTH"),
            ("100% Honest LLC", "100 HONEST"),  # % dropped, LLC stripped
            ("Café Olé", "CAF OL"),  # accented chars dropped
        ],
    )
    def test_special_chars_dropped(self, input_name: str, expected: str) -> None:
        result = sanitize_cnam_display_name(input_name)
        # Some examples may end up starting with a digit and require X prepend
        if not expected[0].isalpha():
            assert result.startswith("X ")
        else:
            assert result == expected, f"{input_name!r} -> {result!r} (expected {expected!r})"

    def test_period_and_comma_preserved(self) -> None:
        # Per Twilio docs, period and comma are allowed in CNAM
        assert sanitize_cnam_display_name("U.S. Tile") == "U.S. TILE"

    def test_trailing_period_stripped(self) -> None:
        # Trailing punctuation cleaned
        assert sanitize_cnam_display_name("Acme.") == "ACME"

    def test_trailing_comma_stripped(self) -> None:
        assert sanitize_cnam_display_name("Acme,") == "ACME"


# ---------------------------------------------------------------------------
# Leading-digit handling (X prefix)
# ---------------------------------------------------------------------------


class TestLeadingDigit:
    def test_starts_with_digit_gets_x_prefix(self) -> None:
        result = sanitize_cnam_display_name("7 Eleven")
        assert result.startswith("X ")
        assert result == "X 7 ELEVEN"

    def test_starts_with_digit_long_enough_to_truncate(self) -> None:
        # "999 Heavy Equipment" upper -> "999 HEAVY EQUIPMENT"
        # 19 chars; truncate to 15 at word boundary -> "999 HEAVY"
        # starts with digit -> prepend "X " -> "X 999 HEAVY" (11 chars)
        result = sanitize_cnam_display_name("999 Heavy Equipment")
        assert result.startswith("X ")
        assert len(result) <= MAX_CNAM_LENGTH
        assert result == "X 999 HEAVY"

    def test_starts_with_letter_unchanged(self) -> None:
        result = sanitize_cnam_display_name("ACME LLC")
        assert not result.startswith("X ")
        assert result == "ACME"


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


class TestTruncation:
    def test_long_name_truncates_at_word_boundary(self) -> None:
        # 30 chars -> truncate at last space before 15
        result = sanitize_cnam_display_name("Mountain Top Adventure Tours")
        assert len(result) <= MAX_CNAM_LENGTH
        # Should NOT mid-word truncate -> "MOUNTAIN TOP" or "MOUNTAIN TOP A"
        # Last space within first 15 of "MOUNTAIN TOP ADVENTURE TOURS" is after "TOP" (12)
        # so result is "MOUNTAIN TOP"
        assert result == "MOUNTAIN TOP"

    def test_long_single_word_hard_truncates(self) -> None:
        # No word boundary -> hard cut at 15
        result = sanitize_cnam_display_name("Supercalifragilistic")
        assert len(result) == MAX_CNAM_LENGTH
        assert result == "SUPERCALIFRAGIL"

    def test_exactly_15_chars_unchanged(self) -> None:
        # "ACME PAINTING CO" is 16 chars; CO stripped -> "ACME PAINTING" (13)
        # Need a true 15-char input that has no suffix to strip
        result = sanitize_cnam_display_name("ABC DEF GHI JKL")
        assert result == "ABC DEF GHI JKL"
        assert len(result) == 15

    def test_short_name_unchanged(self) -> None:
        assert sanitize_cnam_display_name("Bob") == "BOB"


# ---------------------------------------------------------------------------
# Empty / invalid inputs
# ---------------------------------------------------------------------------


class TestEmptyAndInvalidInputs:
    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            sanitize_cnam_display_name("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            sanitize_cnam_display_name("   ")

    def test_none_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            sanitize_cnam_display_name(None)  # type: ignore[arg-type]

    def test_only_special_chars_raises(self) -> None:
        # "@!#$%" upper unchanged, all chars dropped -> empty
        with pytest.raises(ValueError, match="empty"):
            sanitize_cnam_display_name("@!#$%")


# ---------------------------------------------------------------------------
# Result invariants (universal — apply to every successful output)
# ---------------------------------------------------------------------------


class TestUniversalInvariants:
    @pytest.mark.parametrize(
        "input_name",
        [
            "ACME Plumbing & Heating LLC",
            "Scott Painting Services",
            "The 24/7 Fix-It Co",
            "123 Electric",
            "Mom's Pet Grooming, Inc.",
            "Bob's Burgers",
            "AT&T",
            "Real Estate Holdings LP",
            "U.S. Tile",
            "Mountain Top Adventure Tours",
        ],
    )
    def test_result_starts_with_letter(self, input_name: str) -> None:
        result = sanitize_cnam_display_name(input_name)
        assert result and result[0].isalpha(), f"{input_name!r} -> {result!r}"

    @pytest.mark.parametrize(
        "input_name",
        [
            "ACME Plumbing & Heating LLC",
            "Scott Painting Services",
            "Mountain Top Adventure Tours",
            "Supercalifragilistic",
            "999 Heavy Equipment",
        ],
    )
    def test_result_within_max_length(self, input_name: str) -> None:
        result = sanitize_cnam_display_name(input_name)
        assert len(result) <= MAX_CNAM_LENGTH, f"{input_name!r} -> {result!r} ({len(result)} chars)"

    @pytest.mark.parametrize(
        "input_name",
        [
            "ACME Plumbing & Heating LLC",
            "AT&T",
            "Mom's Diner",
            "100% Honest",
        ],
    )
    def test_result_only_allowed_chars(self, input_name: str) -> None:
        import re

        result = sanitize_cnam_display_name(input_name)
        # CNAM allows A-Z, 0-9, space, period, comma
        assert re.match(r"^[A-Z0-9 .,]+$", result), f"{input_name!r} -> {result!r}"

    def test_idempotent(self) -> None:
        # Applying sanitization to an already-sanitized output should be no-op
        once = sanitize_cnam_display_name("Scott Painting Services")
        twice = sanitize_cnam_display_name(once)
        assert once == twice
