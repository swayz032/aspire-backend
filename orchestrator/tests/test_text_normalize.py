"""Tests for the Adam TTS post-processor (Wave 2.1).

Locks the address-text contract: every Adam response that leaves the
orchestrator must read aloud correctly. Covers the actual addresses from the
production session that surfaced the bug (1575 Paul Russell Rd APT 4802,
604 Ward Pl, 4863 Price St) plus edge cases that came up during review.
"""

from __future__ import annotations

import pytest

from aspire_orchestrator.services.adam.text_normalize import (
    normalize_addresses_for_speech,
    normalize_payload_for_speech,
)


# ---------------------------------------------------------------------------
# Direct address-string normalization
# ---------------------------------------------------------------------------


class TestNormalizeAddressesForSpeech:
    """Word-boundary substitutions for street suffixes, units, cardinals."""

    # --- Real addresses from the production session ---

    def test_user_session_condo_address(self):
        """1575 Paul Russell Rd APT 4802 — the address that triggered the fix."""
        out = normalize_addresses_for_speech("1575 Paul Russell Rd APT 4802")
        assert out == "1575 Paul Russell Road Apartment 4802"

    def test_user_session_pl_address(self):
        out = normalize_addresses_for_speech("604 Ward Pl")
        assert out == "604 Ward Place"

    def test_user_session_st_address(self):
        out = normalize_addresses_for_speech("4863 Price St")
        assert out == "4863 Price Street"

    # --- Each suffix expanded ---

    def test_rd_expanded(self):
        assert normalize_addresses_for_speech("100 Maple Rd") == "100 Maple Road"

    def test_pl_expanded(self):
        assert normalize_addresses_for_speech("604 Ward Pl") == "604 Ward Place"

    def test_ave_expanded(self):
        assert normalize_addresses_for_speech("250 Oak Ave") == "250 Oak Avenue"

    def test_blvd_expanded(self):
        assert normalize_addresses_for_speech("123 Sunset Blvd") == "123 Sunset Boulevard"

    def test_dr_expanded(self):
        assert normalize_addresses_for_speech("88 Hilltop Dr") == "88 Hilltop Drive"

    def test_ln_expanded(self):
        assert normalize_addresses_for_speech("12 Quiet Ln") == "12 Quiet Lane"

    def test_ct_expanded(self):
        assert normalize_addresses_for_speech("3 Birch Ct") == "3 Birch Court"

    def test_cir_expanded(self):
        assert normalize_addresses_for_speech("44 Round Cir") == "44 Round Circle"

    def test_pkwy_expanded(self):
        assert normalize_addresses_for_speech("500 Eagle Pkwy") == "500 Eagle Parkway"

    def test_hwy_expanded(self):
        assert normalize_addresses_for_speech("US Hwy 27") == "US Highway 27"

    def test_apt_expanded(self):
        assert normalize_addresses_for_speech("100 Main St Apt 4B") == "100 Main Street Apartment 4B"

    def test_ste_expanded(self):
        assert normalize_addresses_for_speech("250 Oak Ave Ste 200") == "250 Oak Avenue Suite 200"

    # --- Trailing periods are accepted ---

    def test_period_after_suffix_consumed(self):
        assert normalize_addresses_for_speech("100 Maple Rd.") == "100 Maple Road"

    def test_period_after_apt_consumed(self):
        assert normalize_addresses_for_speech("100 Main St. Apt. 4B") == "100 Main Street Apartment 4B"

    # --- St ambiguity guard ---

    def test_st_only_expanded_after_street_number(self):
        """St as Saint must NOT be rewritten — only St as Street suffix is."""
        assert normalize_addresses_for_speech("St James Place") == "St James Place"

    def test_st_after_number_expanded(self):
        assert normalize_addresses_for_speech("100 Main St") == "100 Main Street"

    def test_st_in_middle_of_proper_noun_unchanged(self):
        """'St' inside a city/saint name with no preceding street number stays."""
        assert normalize_addresses_for_speech("Mount St Helens") == "Mount St Helens"

    # --- Cardinal direction prefixes ---

    def test_n_cardinal_expanded(self):
        assert normalize_addresses_for_speech("604 N Ward Pl") == "604 North Ward Place"

    def test_s_cardinal_expanded(self):
        assert normalize_addresses_for_speech("250 S Oak Ave") == "250 South Oak Avenue"

    def test_e_cardinal_expanded(self):
        assert normalize_addresses_for_speech("100 E Main St") == "100 East Main Street"

    def test_w_cardinal_expanded(self):
        assert normalize_addresses_for_speech("100 W. Main St") == "100 West Main Street"

    def test_cardinal_not_expanded_without_preceding_number(self):
        """Bare 'N' or 'W' in prose must NOT be rewritten to 'North' / 'West'."""
        out = normalize_addresses_for_speech("Travel N for 5 miles")
        assert out == "Travel N for 5 miles"

    def test_cardinal_not_expanded_when_capital_word_doesnt_follow(self):
        """'10 N 20' — 'N' not followed by a Capitalized word, so no rewrite."""
        out = normalize_addresses_for_speech("Block 10 N 20")
        assert out == "Block 10 N 20"

    # --- Cardinal direction suffixes (NW/NE/SE/SW after a street-type word) ---

    def test_cardinal_suffix_nw_expanded(self):
        """User-required test case from Wave 2.1 spec — Cir NW -> Circle Northwest."""
        out = normalize_addresses_for_speech("1490 Capital Cir NW")
        assert out == "1490 Capital Circle Northwest"

    def test_cardinal_suffix_ne_expanded(self):
        out = normalize_addresses_for_speech("200 Pine Ave NE")
        assert out == "200 Pine Avenue Northeast"

    def test_cardinal_suffix_se_expanded(self):
        out = normalize_addresses_for_speech("44 Park Rd SE")
        assert out == "44 Park Road Southeast"

    def test_cardinal_suffix_sw_expanded(self):
        out = normalize_addresses_for_speech("88 Mill Blvd SW")
        assert out == "88 Mill Boulevard Southwest"

    def test_cardinal_suffix_single_letter_w_expanded(self):
        out = normalize_addresses_for_speech("100 Main Street W")
        assert out == "100 Main Street West"

    def test_cardinal_suffix_only_after_street_type(self):
        """Bare 'NW' in prose without a preceding street-type word is preserved."""
        out = normalize_addresses_for_speech("The NW region had high turnout")
        assert out == "The NW region had high turnout"

    # --- Idempotency / safety ---

    def test_idempotent(self):
        once = normalize_addresses_for_speech("1575 Paul Russell Rd APT 4802")
        twice = normalize_addresses_for_speech(once)
        assert once == twice

    def test_empty_string_unchanged(self):
        assert normalize_addresses_for_speech("") == ""

    def test_none_passes_through(self):
        # Defensive: not isinstance(text, str) returns the input as-is.
        assert normalize_addresses_for_speech(None) is None  # type: ignore[arg-type]

    def test_non_address_string_unchanged(self):
        # No abbreviations -> no substitutions.
        text = "The hotel rate is 189 dollars per night."
        assert normalize_addresses_for_speech(text) == text

    def test_word_boundary_protects_inner_letters(self):
        """'Bordeaux' contains 'rd' but is not 'Rd'."""
        assert normalize_addresses_for_speech("Bordeaux Rd") == "Bordeaux Road"
        # Inner 'rd' inside 'Bordeaux' must stay.
        assert "Bordeauxoad" not in normalize_addresses_for_speech("Bordeaux Rd")

    def test_full_user_summary_sentence(self):
        """Realistic summary string like Ava would speak aloud."""
        text = (
            "Property facts for 1575 Paul Russell Rd APT 4802. Verification: "
            "verified. Living: 1500 sq ft."
        )
        out = normalize_addresses_for_speech(text)
        assert "Road" in out
        assert "Apartment" in out
        assert "Rd" not in out.replace("Road", "")
        assert "Apt" not in out.replace("Apartment", "")


# ---------------------------------------------------------------------------
# Recursive payload normalization (records, lists, nested dicts)
# ---------------------------------------------------------------------------


class TestNormalizePayloadForSpeech:
    """Walks a ResearchResponse-shaped payload, normalizing address fields."""

    def test_top_level_summary_normalized(self):
        payload = {"summary": "Found a property at 4863 Price St."}
        out = normalize_payload_for_speech(payload)
        assert out["summary"] == "Found a property at 4863 Price Street."

    def test_record_normalized_address_field(self):
        payload = {
            "summary": "x",
            "records": [{"normalized_address": "1575 Paul Russell Rd APT 4802"}],
        }
        out = normalize_payload_for_speech(payload)
        assert out["records"][0]["normalized_address"] == "1575 Paul Russell Road Apartment 4802"

    def test_record_mailing_address_field(self):
        payload = {"records": [{"mailing_address": "604 Ward Pl"}]}
        out = normalize_payload_for_speech(payload)
        assert out["records"][0]["mailing_address"] == "604 Ward Place"

    def test_nested_address_in_comp(self):
        """Comp lists nested under nearby_comps must also be normalized."""
        payload = {
            "records": [{
                "nearby_comps": [
                    {"address": "100 Main St"},
                    {"address": "250 Oak Ave"},
                ],
            }],
        }
        out = normalize_payload_for_speech(payload)
        assert out["records"][0]["nearby_comps"][0]["address"] == "100 Main Street"
        assert out["records"][0]["nearby_comps"][1]["address"] == "250 Oak Avenue"

    def test_non_address_string_fields_unchanged(self):
        """Product titles, descriptions, owner names — none should be rewritten."""
        payload = {
            "records": [{
                "product_name": "5 ft St James Brand Ladder",
                "description": "Heavy-duty ladder rated to 300 lb.",
                "owner_name": "John St James",
                "normalized_address": "100 Main St",
            }],
        }
        out = normalize_payload_for_speech(payload)
        # Address rewritten.
        assert out["records"][0]["normalized_address"] == "100 Main Street"
        # Non-address fields untouched even if they contain "St".
        assert out["records"][0]["product_name"] == "5 ft St James Brand Ladder"
        assert out["records"][0]["description"] == "Heavy-duty ladder rated to 300 lb."
        assert out["records"][0]["owner_name"] == "John St James"

    def test_empty_payload_unchanged(self):
        assert normalize_payload_for_speech({}) == {}

    def test_top_level_list_recurses(self):
        out = normalize_payload_for_speech([
            {"normalized_address": "604 Ward Pl"},
            {"normalized_address": "100 Main St"},
        ])
        assert out[0]["normalized_address"] == "604 Ward Place"
        assert out[1]["normalized_address"] == "100 Main Street"

    def test_does_not_mutate_input(self):
        original = {"records": [{"normalized_address": "100 Main St"}]}
        snapshot = {"records": [{"normalized_address": "100 Main St"}]}
        normalize_payload_for_speech(original)
        assert original == snapshot
