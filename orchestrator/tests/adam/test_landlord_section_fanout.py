"""W0 — card-per-section fanout for the landlord property_facts playbook.

`fan_out_property_sections()` takes a single normalized property record
and emits one record per UI section so the desktop carousel can render a
card per category of data ATTOM returned. Each emitted record carries the
same data plus a `_cardSection` tag that PropertyCard.tsx:344 uses to
pick a renderer.

These tests lock in:
- Predicate-driven conditional emission (no empty cards)
- Section order matches the report-like read order
- Safety-net fallback when every predicate fails
- Records are independent dict copies, not aliased
- _section_order matches the index of the section in the canonical plan
"""

from __future__ import annotations

import pytest

from aspire_orchestrator.services.adam.playbooks.landlord import (
    _SECTION_PLAN,
    fan_out_property_sections,
)


def _full_property_dict() -> dict:
    """A property record that satisfies every predicate so we can verify
    full-fanout behavior."""
    return {
        "normalized_address": "4863 PRICE Street, FOREST PARK, GA 30297",
        "owner_name": "TONY LEWIS SCOTT",
        "previous_owner_name": "CEDRIC S HORTON",
        "mailing_address": "4863 PRICE Street, FOREST PARK, GA 30297-1959",
        "tax_market_value": 316800,
        "estimated_value": 295330,
        "annual_tax_amount": 4508.14,
        "mortgage_amount": 192449,
        "mortgage_lender": "UNITED WHOLESALE",
        "current_loan_balance": 166170,
        "last_sale_amount": 196000,
        "last_sale_date": "2019-09-30",
        "transaction_history": [{"date": "2019-09-30", "type": "Sale", "amount": 196000}],
        "prior_foreclosure": True,
        "foreclosure_stage": "none",
        "foreclosure_records": [{"recording_date": "2018-05-12"}],
        "foreclosure_filing": {"filing_date": "2024-03-15"},
        "permit_signals": [{"date": "2020-08-12", "description": "Re-roof"}],
        "major_improvements_year": 2013,
        "nearby_schools": [{"name": "Forest Park HS", "rating": 6}],
        "school_district_name": "Clayton County Schools",
        "estimated_rent": 1850.0,
    }


class TestFanOutFullProperty:
    """A maximal property record fans out into all 9 section cards."""

    def test_emits_nine_cards_when_every_section_qualifies(self):
        records = fan_out_property_sections(_full_property_dict())
        assert len(records) == 9

    def test_section_order_is_locked(self):
        records = fan_out_property_sections(_full_property_dict())
        sections = [r["_cardSection"] for r in records]
        assert sections == [
            "overview",
            "ownership",
            "valuation",
            "mortgage",
            "sale_history",
            "foreclosure",
            "permits",
            "schools",
            "rental",
        ]

    def test_each_record_carries_card_section_tag(self):
        records = fan_out_property_sections(_full_property_dict())
        for r in records:
            assert "_cardSection" in r and r["_cardSection"]

    def test_each_record_carries_section_label(self):
        records = fan_out_property_sections(_full_property_dict())
        for r in records:
            assert "_sectionLabel" in r and r["_sectionLabel"]

    def test_section_order_matches_index(self):
        records = fan_out_property_sections(_full_property_dict())
        for i, r in enumerate(records):
            assert r["_section_order"] == i

    def test_records_are_independent_copies(self):
        """Mutating one record's metadata must not bleed across to siblings —
        otherwise the carousel would render duplicate-looking cards."""
        records = fan_out_property_sections(_full_property_dict())
        records[0]["_cardSection"] = "MUTATED"
        assert records[1]["_cardSection"] != "MUTATED"

    def test_full_property_data_preserved_on_each_card(self):
        """Each section card should carry the full property dict so its
        renderer can pull whatever fields it needs (e.g. SchoolsSection
        still shows latitude in addition to schools)."""
        records = fan_out_property_sections(_full_property_dict())
        for r in records:
            assert r["normalized_address"] == "4863 PRICE Street, FOREST PARK, GA 30297"
            assert r["owner_name"] == "TONY LEWIS SCOTT"


class TestFanOutConditional:
    """Sections without backing data are skipped."""

    def test_minimal_property_emits_only_overview(self):
        """A bare property with only an address yields one card, not nine."""
        records = fan_out_property_sections({
            "normalized_address": "1 Test Lane",
        })
        assert len(records) == 1
        assert records[0]["_cardSection"] == "overview"

    def test_no_mortgage_skips_mortgage_card(self):
        prop = _full_property_dict()
        prop["mortgage_amount"] = None
        prop["mortgage_lender"] = ""
        prop["current_loan_balance"] = None
        records = fan_out_property_sections(prop)
        sections = [r["_cardSection"] for r in records]
        assert "mortgage" not in sections

    def test_no_rental_avm_skips_rental_card(self):
        prop = _full_property_dict()
        prop["estimated_rent"] = None
        records = fan_out_property_sections(prop)
        sections = [r["_cardSection"] for r in records]
        assert "rental" not in sections

    def test_no_foreclosure_signal_skips_foreclosure_card(self):
        prop = _full_property_dict()
        prop["prior_foreclosure"] = False
        prop["foreclosure_records"] = []
        prop["foreclosure_filing"] = {}
        prop["foreclosure_stage"] = "none"
        prop["reo_flag"] = False
        records = fan_out_property_sections(prop)
        sections = [r["_cardSection"] for r in records]
        assert "foreclosure" not in sections

    def test_clean_foreclosure_stage_alone_does_not_emit(self):
        """foreclosure_stage='none' must NOT trigger the card."""
        prop = {"normalized_address": "1 Test Ln", "foreclosure_stage": "none", "owner_name": "x"}
        records = fan_out_property_sections(prop)
        sections = [r["_cardSection"] for r in records]
        assert "foreclosure" not in sections

    def test_active_foreclosure_stage_emits_card(self):
        prop = {"normalized_address": "1 Test Ln", "foreclosure_stage": "auction", "owner_name": "x"}
        records = fan_out_property_sections(prop)
        sections = [r["_cardSection"] for r in records]
        assert "foreclosure" in sections

    def test_no_schools_skips_schools_card(self):
        prop = _full_property_dict()
        prop["nearby_schools"] = []
        prop["school_district_name"] = ""
        records = fan_out_property_sections(prop)
        sections = [r["_cardSection"] for r in records]
        assert "schools" not in sections

    def test_no_permits_skips_permits_card(self):
        prop = _full_property_dict()
        prop["permit_signals"] = []
        prop["major_improvements_year"] = None
        records = fan_out_property_sections(prop)
        sections = [r["_cardSection"] for r in records]
        assert "permits" not in sections


class TestFanOutSafetyNet:
    """When every predicate fails the fanout still emits an overview card."""

    def test_truly_empty_dict_still_emits_overview(self):
        records = fan_out_property_sections({})
        assert len(records) == 1
        assert records[0]["_cardSection"] == "overview"

    def test_predicate_exception_is_swallowed_not_propagated(self):
        """If a predicate raises (corrupted record), the playbook must not
        crash — the section is just skipped."""
        # Construct a record where predicates would raise on .get() — none of
        # the standard dict predicates do, but we exercise the try/except by
        # passing a value that raises on truthiness check.
        class _Nasty:
            def __bool__(self):
                raise RuntimeError("predicate explosion")

        prop = {"owner_name": _Nasty()}
        # Must not raise
        records = fan_out_property_sections(prop)
        # Overview always passes (lambda r: True), so we still get 1 card
        assert len(records) >= 1
        assert records[0]["_cardSection"] == "overview"


class TestSectionPlanIntegrity:
    """Lock the canonical plan against accidental edits — these are the
    section keys PropertyCard.tsx is wired to render."""

    def test_section_keys_match_renderer_contract(self):
        """The renderer map at PropertyCard.tsx:344 expects these exact keys.
        Renaming any of them silently breaks the UI without a test failure."""
        expected = {
            "overview", "ownership", "mortgage", "valuation", "sale_history",
            "rental", "permits", "schools", "foreclosure",
        }
        actual = {section for section, _, _ in _SECTION_PLAN}
        assert actual == expected, f"Section keys drifted from PropertyCard.tsx contract: {actual ^ expected}"

    def test_section_plan_has_no_duplicate_keys(self):
        keys = [section for section, _, _ in _SECTION_PLAN]
        assert len(keys) == len(set(keys))
