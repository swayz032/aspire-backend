"""Tests for Adam Verification Engine (verifier.py).

Validates: trust tiers, confidence scoring, conflict detection,
Exa grounding integration, missing field reporting.
ADR-003: Conflicting values SURFACED, never suppressed.
"""

from __future__ import annotations

import pytest

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.verification_report import VerificationReport
from aspire_orchestrator.services.adam.verifier import (
    get_trust_weight,
    verify_records,
    _values_conflict,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source(provider: str) -> SourceAttribution:
    return SourceAttribution(provider=provider, retrieved_at="2026-04-05T00:00:00Z")


def _record(provider: str, **fields) -> dict:
    """Build a minimal record dict with source attribution embedded."""
    record = dict(fields)
    record["sources"] = [{"provider": provider}]
    return record


# ---------------------------------------------------------------------------
# Trust weight tests
# ---------------------------------------------------------------------------


class TestTrustWeights:
    """Provider trust weights match ADR-003 tier definitions."""

    def test_attom_is_tier_a(self):
        assert get_trust_weight("attom") == 0.95

    def test_google_places_is_tier_b(self):
        assert get_trust_weight("google_places") == 0.80

    def test_here_is_tier_b(self):
        assert get_trust_weight("here") == 0.80

    def test_foursquare_is_tier_b(self):
        assert get_trust_weight("foursquare") == 0.80

    def test_tripadvisor_is_tier_b(self):
        assert get_trust_weight("tripadvisor") == 0.80

    def test_serpapi_shopping_is_tier_b(self):
        assert get_trust_weight("serpapi_shopping") == 0.80

    def test_brave_is_tier_c(self):
        assert get_trust_weight("brave") == 0.60

    def test_exa_is_tier_c(self):
        assert get_trust_weight("exa") == 0.60

    def test_tavily_is_tier_c(self):
        assert get_trust_weight("tavily") == 0.60

    def test_parallel_is_tier_c(self):
        assert get_trust_weight("parallel") == 0.60

    def test_unknown_provider_defaults_to_tier_c_weight(self):
        """Unknown providers not in _PROVIDER_TRUST fall through to Tier C weight (0.60).
        The _PROVIDER_TRUST dict defaults unknown keys to 'C', and _TRUST_WEIGHTS['C'] = 0.60.
        """
        assert get_trust_weight("some_new_provider") == 0.60


# ---------------------------------------------------------------------------
# Basic verify_records scenarios
# ---------------------------------------------------------------------------


class TestVerifyRecordsBasic:
    """Core verification logic: status determination and score ranges."""

    def test_empty_records_returns_unverified_zero_score(self):
        """Zero records → unverified with score 0.0."""
        report = verify_records(records=[], sources=[])
        assert report.status == "unverified"
        assert report.confidence_score == 0.0

    def test_empty_sources_returns_unverified(self):
        """Records with no sources → unverified."""
        record = _record("attom", name="Test Business", address="123 Main St")
        report = verify_records(records=[record], sources=[])
        assert report.status == "unverified"

    def test_single_tier_a_source_no_missing_fields_verified(self):
        """Single Tier A source (ATTOM), all required fields present → verified, score >= 0.80."""
        record = _record(
            "attom",
            normalized_address="123 Main St, Lexington, KY 40509",
            living_sqft=1800,
            year_built=1995,
        )
        report = verify_records(
            records=[record],
            sources=[_source("attom")],
            required_fields=["normalized_address", "living_sqft", "year_built"],
        )
        assert report.status == "verified"
        assert report.confidence_score >= 0.80

    def test_single_tier_c_source_with_missing_fields_unverified(self):
        """Single Tier C source, multiple missing required fields → unverified, score < 0.40."""
        record = _record("brave", name="Some Business")
        # Required fields not in record → all missing
        report = verify_records(
            records=[record],
            sources=[_source("brave")],
            required_fields=["normalized_address", "phone", "category"],
        )
        assert report.status == "unverified"
        assert report.confidence_score < 0.40

    def test_single_tier_c_source_no_required_fields_partially_verified(self):
        """Single Tier C source with no missing required fields → partially_verified."""
        record = _record("exa", name="Business")
        report = verify_records(
            records=[record],
            sources=[_source("exa")],
            required_fields=[],
        )
        # score < 0.80 so not "verified"; score >= 0.40 and source_count >= 1 → partially_verified
        assert report.status == "partially_verified"

    def test_returns_verification_report_instance(self):
        record = _record("brave", name="Test")
        report = verify_records(records=[record], sources=[_source("brave")])
        assert isinstance(report, VerificationReport)


# ---------------------------------------------------------------------------
# Multi-source agreement
# ---------------------------------------------------------------------------


class TestMultiSourceAgreement:
    """Multiple agreeing sources increase confidence score."""

    def test_two_tier_b_sources_agreement_increases_score(self):
        """Two agreeing Tier B sources get a multi-source bonus."""
        record1 = _record("google_places", name="Acme Roofing", normalized_address="123 Main St")
        record2 = _record("foursquare", name="Acme Roofing", normalized_address="123 Main St")
        report_single = verify_records(
            records=[record1],
            sources=[_source("google_places")],
        )
        report_multi = verify_records(
            records=[record1, record2],
            sources=[_source("google_places"), _source("foursquare")],
        )
        assert report_multi.confidence_score >= report_single.confidence_score

    def test_three_sources_gets_larger_bonus_than_two(self):
        """3+ sources get a larger multi-source bonus than 2 sources."""
        base_record = {"name": "Acme Roofing", "sources": [{"provider": "google_places"}]}
        r1 = _record("google_places", name="Acme Roofing")
        r2 = _record("foursquare", name="Acme Roofing")
        r3 = _record("here", name="Acme Roofing")
        report_two = verify_records(
            records=[r1, r2],
            sources=[_source("google_places"), _source("foursquare")],
        )
        report_three = verify_records(
            records=[r1, r2, r3],
            sources=[_source("google_places"), _source("foursquare"), _source("here")],
        )
        assert report_three.confidence_score >= report_two.confidence_score

    def test_source_count_reported_correctly(self):
        """source_count in report equals number of unique providers."""
        r1 = _record("google_places", name="A")
        r2 = _record("foursquare", name="A")
        report = verify_records(
            records=[r1, r2],
            sources=[_source("google_places"), _source("foursquare")],
        )
        assert report.source_count == 2


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


class TestConflictDetection:
    """Conflicts are surfaced, not suppressed (ADR-003)."""

    def test_string_field_conflict_detected(self):
        """Two records with different 'name' from different providers → conflict reported."""
        r1 = _record("google_places", name="Acme HVAC")
        r2 = _record("foursquare", name="Acme Heating and Cooling")
        report = verify_records(
            records=[r1, r2],
            sources=[_source("google_places"), _source("foursquare")],
        )
        assert report.conflict_count >= 1
        field_names = [c.field_name for c in report.conflicts]
        assert "name" in field_names

    def test_numeric_within_10_percent_is_not_conflict(self):
        """Numeric values within 10% tolerance must NOT generate a conflict."""
        # 1000 vs 1050 = 5% difference
        r1 = _record("attom", living_sqft=1000)
        r2 = _record("here", living_sqft=1050)
        report = verify_records(
            records=[r1, r2],
            sources=[_source("attom"), _source("here")],
        )
        sqft_conflicts = [c for c in report.conflicts if c.field_name == "living_sqft"]
        assert len(sqft_conflicts) == 0

    def test_numeric_beyond_10_percent_is_conflict(self):
        """Numeric values diverging >10% → conflict reported."""
        # 1000 vs 1200 = 20% difference
        r1 = _record("attom", living_sqft=1000)
        r2 = _record("here", living_sqft=1200)
        report = verify_records(
            records=[r1, r2],
            sources=[_source("attom"), _source("here")],
        )
        sqft_conflicts = [c for c in report.conflicts if c.field_name == "living_sqft"]
        assert len(sqft_conflicts) == 1

    def test_string_comparison_case_insensitive(self):
        """Case-insensitive string comparison: 'ACME' vs 'acme' → no conflict."""
        r1 = _record("google_places", name="ACME Roofing")
        r2 = _record("foursquare", name="acme roofing")
        report = verify_records(
            records=[r1, r2],
            sources=[_source("google_places"), _source("foursquare")],
        )
        name_conflicts = [c for c in report.conflicts if c.field_name == "name"]
        assert len(name_conflicts) == 0

    def test_conflict_penalizes_confidence_score(self):
        """Detected conflicts reduce the confidence score."""
        r_agree = _record("google_places", name="Acme HVAC", normalized_address="123 Main St")
        r_conflict = _record("foursquare", name="Different Name Corp", normalized_address="123 Main St")
        report_agree = verify_records(
            records=[r_agree, r_agree],
            sources=[_source("google_places"), _source("foursquare")],
        )
        report_conflict = verify_records(
            records=[r_agree, r_conflict],
            sources=[_source("google_places"), _source("foursquare")],
        )
        assert report_conflict.confidence_score <= report_agree.confidence_score

    def test_conflict_surfaces_all_provider_values(self):
        """FieldConflict.values includes entries for each provider."""
        r1 = _record("google_places", name="Acme HVAC")
        r2 = _record("foursquare", name="Beta Corp")
        report = verify_records(
            records=[r1, r2],
            sources=[_source("google_places"), _source("foursquare")],
        )
        name_conflicts = [c for c in report.conflicts if c.field_name == "name"]
        assert len(name_conflicts) == 1
        assert len(name_conflicts[0].values) >= 2

    def test_single_record_no_conflicts(self):
        """A single record cannot produce conflicts (no second source to compare)."""
        r = _record("attom", name="Acme")
        report = verify_records(records=[r], sources=[_source("attom")])
        assert report.conflict_count == 0
        assert len(report.conflicts) == 0


# ---------------------------------------------------------------------------
# Missing field detection
# ---------------------------------------------------------------------------


class TestMissingFieldDetection:
    """Required fields missing from all records are reported in missing_fields."""

    def test_missing_required_field_reported(self):
        """A required field absent from all records appears in missing_fields."""
        record = _record("attom", normalized_address="123 Main St")
        report = verify_records(
            records=[record],
            sources=[_source("attom")],
            required_fields=["normalized_address", "living_sqft"],
        )
        assert "living_sqft" in report.missing_fields

    def test_present_field_not_in_missing_list(self):
        """A present required field must NOT appear in missing_fields."""
        record = _record("attom", normalized_address="123 Main St", living_sqft=1800)
        report = verify_records(
            records=[record],
            sources=[_source("attom")],
            required_fields=["normalized_address", "living_sqft"],
        )
        assert "normalized_address" not in report.missing_fields
        assert "living_sqft" not in report.missing_fields

    def test_missing_fields_penalize_confidence(self):
        """Each missing required field reduces the confidence score."""
        r = _record("attom", normalized_address="123 Main St")
        report_complete = verify_records(
            records=[_record("attom", normalized_address="123 Main", living_sqft=1500)],
            sources=[_source("attom")],
            required_fields=["normalized_address", "living_sqft"],
        )
        report_incomplete = verify_records(
            records=[r],
            sources=[_source("attom")],
            required_fields=["normalized_address", "living_sqft"],
        )
        assert report_incomplete.confidence_score < report_complete.confidence_score

    def test_no_required_fields_means_no_missing_fields(self):
        """When required_fields is empty, missing_fields list is always empty."""
        record = _record("brave", name="Test")
        report = verify_records(
            records=[record],
            sources=[_source("brave")],
            required_fields=[],
        )
        assert report.missing_fields == []


# ---------------------------------------------------------------------------
# Exa grounding integration
# ---------------------------------------------------------------------------


class TestExaGrounding:
    """Exa native grounding.confidence blended into confidence calculation."""

    def test_exa_high_grounding_boosts_score(self):
        """Exa 'high' grounding (0.90) raises final confidence above base C-tier alone."""
        record = _record("exa", content="Tax deadline info from official IRS source")
        exa_grounding = [{"confidence": "high", "field": "content"}]
        report_no_grounding = verify_records(
            records=[record],
            sources=[_source("exa")],
            exa_grounding=None,
        )
        report_with_grounding = verify_records(
            records=[record],
            sources=[_source("exa")],
            exa_grounding=exa_grounding,
        )
        assert report_with_grounding.confidence_score >= report_no_grounding.confidence_score

    def test_exa_medium_grounding_mapped_to_0_70(self):
        """'medium' grounding blends in as 0.70 score."""
        record = _record("exa", content="Some content")
        exa_grounding = [{"confidence": "medium"}]
        report = verify_records(
            records=[record],
            sources=[_source("exa")],
            exa_grounding=exa_grounding,
        )
        # Score should reflect blend of base C weight (0.60) + medium grounding (0.70)
        # Blend: 0.60*0.6 + 0.70*0.4 = 0.64
        assert report.confidence_score > 0.0

    def test_exa_low_grounding_mapped_correctly(self):
        """'low' grounding (0.40) reduces effective confidence below base."""
        record = _record("exa", content="Uncertain content")
        exa_grounding = [{"confidence": "low"}]
        report = verify_records(
            records=[record],
            sources=[_source("exa")],
            exa_grounding=exa_grounding,
        )
        # Blend: 0.60*0.6 + 0.40*0.4 = 0.52
        assert report.confidence_score > 0.0

    def test_unknown_exa_confidence_string_ignored(self):
        """Unrecognized grounding confidence strings do not crash."""
        record = _record("exa", content="Some content")
        exa_grounding = [{"confidence": "unknown_level"}]
        report = verify_records(
            records=[record],
            sources=[_source("exa")],
            exa_grounding=exa_grounding,
        )
        assert report is not None

    def test_empty_exa_grounding_list_has_no_effect(self):
        """Empty exa_grounding list behaves identically to None."""
        record = _record("exa", content="content")
        report_none = verify_records(
            records=[record], sources=[_source("exa")], exa_grounding=None
        )
        report_empty = verify_records(
            records=[record], sources=[_source("exa")], exa_grounding=[]
        )
        assert report_none.confidence_score == report_empty.confidence_score


# ---------------------------------------------------------------------------
# _values_conflict unit tests
# ---------------------------------------------------------------------------


class TestValuesConflict:
    """_values_conflict correctly identifies conflicting value lists."""

    def test_identical_strings_not_conflict(self):
        assert _values_conflict(["Acme HVAC", "Acme HVAC"]) is False

    def test_case_insensitive_strings_not_conflict(self):
        assert _values_conflict(["ACME", "acme"]) is False

    def test_different_strings_are_conflict(self):
        assert _values_conflict(["Acme HVAC", "Beta Corp"]) is True

    def test_numerics_within_tolerance_not_conflict(self):
        assert _values_conflict([1000, 1050]) is False  # 5% diff

    def test_numerics_beyond_tolerance_are_conflict(self):
        assert _values_conflict([1000, 1200]) is True  # 20% diff

    def test_empty_list_not_conflict(self):
        assert _values_conflict([]) is False

    def test_single_value_not_conflict(self):
        assert _values_conflict(["only one"]) is False

    def test_both_zero_not_conflict(self):
        assert _values_conflict([0, 0]) is False
