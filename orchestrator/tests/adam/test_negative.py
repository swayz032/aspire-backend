"""Negative tests for Adam Research Platform — Pass 7 handoff requirements.

All tests are evil/negative: they assert that dangerous or invalid scenarios
fail in a controlled, law-compliant manner.

Laws validated:
  Law #1 (Single Brain): Adam never makes autonomous tool calls
  Law #2 (Receipt for All): No playbook executes without going through receipt infrastructure
  Law #3 (Fail Closed): Missing context → deny, never guess
  Law #6 (Tenant Isolation): Cross-tenant cache reads are impossible
  ADR-003: Conflicts are surfaced, never suppressed
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.adam.cache import (
    cache_clear_all,
    cache_get,
    cache_set,
)
from aspire_orchestrator.services.adam.router import ALL_PLAYBOOKS, route_to_playbook
from aspire_orchestrator.services.adam.verifier import verify_records
from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.normalizers.product_normalizer import (
    normalize_from_serpapi_shopping,
    normalize_from_serpapi_homedepot,
)
from aspire_orchestrator.services.adam.telemetry import (
    AdamErrorCode,
    clear_events,
    get_events,
)


ADAM_ROOT = Path(__file__).parent.parent.parent / (
    "src/aspire_orchestrator/services/adam"
)

PLAYBOOK_ROOT = ADAM_ROOT / "playbooks"

TENANT_A = "tenant-alpha-negative-001"
TENANT_B = "tenant-beta-negative-002"


@pytest.fixture(autouse=True)
def _reset_cache_and_telemetry():
    cache_clear_all()
    clear_events()
    yield
    cache_clear_all()
    clear_events()


# ---------------------------------------------------------------------------
# Negative 1: No side-effectful tool call path in Adam (Law #1)
#
# Adam is a RESEARCH agent — it NEVER sends messages, creates records, or
# triggers mutations. Playbooks must not import payment, email, invoice,
# calendar write, or SMS send modules.
# ---------------------------------------------------------------------------


FORBIDDEN_IMPORTS = [
    # Payment / financial mutations
    "stripe",
    "paypal",
    # Email send (reading is OK, sending is not)
    "sendgrid",
    "mailgun",
    "smtplib",
    # Calendar write
    "google.oauth2",
    # Direct DB writes outside the receipt infrastructure
    "asyncpg.pool.execute",
    # n8n trigger (would constitute autonomous action — Law #1)
    "n8n_client",
    "n8n_trigger",
]


def _collect_python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in str(p)]


class TestNoSideEffectfulToolCalls:
    """Law #1: Adam playbooks must not import mutation/send modules."""

    @pytest.mark.parametrize("forbidden", FORBIDDEN_IMPORTS)
    def test_playbook_files_do_not_import_forbidden_module(self, forbidden: str):
        """No playbook Python file imports a forbidden side-effectful module."""
        if not PLAYBOOK_ROOT.exists():
            pytest.skip(f"Playbook root not found: {PLAYBOOK_ROOT}")

        violations: list[str] = []
        for filepath in _collect_python_files(PLAYBOOK_ROOT):
            source = filepath.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    else:
                        names = [node.module or ""]
                    for name in names:
                        if name and forbidden in name:
                            violations.append(f"{filepath.name}: imports '{name}'")

        assert not violations, (
            f"BLOCKER — playbooks import forbidden module '{forbidden}':\n"
            + "\n".join(violations)
        )

    def test_adam_router_has_no_side_effectful_imports(self):
        """router.py must not import any forbidden modules."""
        router_file = ADAM_ROOT / "router.py"
        if not router_file.exists():
            pytest.skip("router.py not found")
        source = router_file.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_IMPORTS:
            assert forbidden not in source, (
                f"BLOCKER — router.py imports forbidden module '{forbidden}'"
            )

    def test_adam_classifiers_has_no_side_effectful_imports(self):
        """classifiers.py must not import any forbidden modules."""
        classifiers_file = ADAM_ROOT / "classifiers.py"
        if not classifiers_file.exists():
            pytest.skip("classifiers.py not found")
        source = classifiers_file.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_IMPORTS:
            assert forbidden not in source, (
                f"BLOCKER — classifiers.py imports forbidden module '{forbidden}'"
            )


# ---------------------------------------------------------------------------
# Negative 2: ATTOM-dependent playbook without normalized address fails closed
# (Law #3: Missing context → deny, never invent)
# ---------------------------------------------------------------------------


class TestAttomFailsClosedWithoutAddress:
    """Property playbooks that depend on ATTOM must fail closed with no address."""

    def test_attom_normalizer_empty_property_returns_unverified_not_invented(self):
        """normalize_from_attom_detail with empty property list returns unverified record.

        Never invents parcel facts when ATTOM returns nothing.
        """
        from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
            normalize_from_attom_detail,
        )
        record = normalize_from_attom_detail({"property": []})
        assert record.verification_status == "unverified"
        assert record.normalized_address == ""
        assert record.living_sqft is None
        assert record.year_built is None
        assert record.owner_name == ""

    def test_attom_normalizer_missing_property_key_returns_unverified(self):
        """Missing 'property' key in ATTOM response → unverified, not raised."""
        from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
            normalize_from_attom_detail,
        )
        record = normalize_from_attom_detail({})
        assert record.verification_status == "unverified"

    def test_verify_records_with_missing_address_field_fails_below_threshold(self):
        """verify_records with normalized_address missing → score below 0.80, not verified."""
        record = {"name": "Some Business", "sources": [{"provider": "attom"}]}
        report = verify_records(
            records=[record],
            sources=[SourceAttribution(provider="attom")],
            required_fields=["normalized_address", "living_sqft"],
        )
        assert report.status != "verified", (
            "BLOCKER: ATTOM record missing address should not be 'verified'"
        )
        assert "normalized_address" in report.missing_fields


# ---------------------------------------------------------------------------
# Negative 3: Ambiguous product query must NOT collapse unrelated products
# (ADR-003: Strict SKU dedup — never merge by name alone)
# ---------------------------------------------------------------------------


class TestStrictSkuDedup:
    """Different SKUs must never be merged, even if names are similar."""

    def test_different_sku_products_not_merged_by_name(self):
        """Two products with same title but different model_number must remain separate records."""
        product_a = {
            "title": "Goodman 3 Ton 14 SEER Condenser",
            "brand": "Goodman",
            "model_number": "GSX140361",  # SKU A
            "product_id": 100001,
            "price": 1150.00,
            "pickup": {"quantity": 2},
        }
        product_b = {
            "title": "Goodman 3 Ton 14 SEER Condenser",
            "brand": "Goodman",
            "model_number": "GSX140361BA",  # SKU B — different model
            "product_id": 100002,
            "price": 1100.00,
            "pickup": {"quantity": 0},
        }
        rec_a = normalize_from_serpapi_homedepot(product_a)
        rec_b = normalize_from_serpapi_homedepot(product_b)

        assert rec_a.model != rec_b.model, (
            "BLOCKER: Products with different model numbers collapsed into one record"
        )
        assert rec_a.price != rec_b.price, (
            "BLOCKER: Different SKU products must retain separate pricing"
        )

    def test_shopping_products_different_source_not_merged(self):
        """Two Google Shopping results from different retailers must remain independent records."""
        product_a = {
            "title": "3 Ton Condenser",
            "extracted_price": 1200.00,
            "source": "HVAC Direct",
        }
        product_b = {
            "title": "3 Ton Condenser",
            "extracted_price": 1350.00,
            "source": "AC Wholesalers",
        }
        rec_a = normalize_from_serpapi_shopping(product_a)
        rec_b = normalize_from_serpapi_shopping(product_b)

        assert rec_a.retailer != rec_b.retailer, (
            "Products from different retailers must have separate retailer attribution"
        )
        assert rec_a.price != rec_b.price


# ---------------------------------------------------------------------------
# Negative 4: Compliance query without official source must not return high confidence
# (Law #3: Unknown is preferable to invented)
# ---------------------------------------------------------------------------


class TestComplianceConfidenceGate:
    """Compliance research with only Tier C web sources must not exceed 0.80 confidence."""

    def test_compliance_from_tier_c_only_below_verified_threshold(self):
        """Brave/Exa/Tavily are Tier C. A compliance answer from C-tier only must be
        below the 'verified' threshold (0.80)."""
        record = {
            "content": "The quarterly estimated tax deadline is April 15, 2025.",
            "url": "https://some-blog.com/taxes",
            "sources": [{"provider": "brave"}],
        }
        report = verify_records(
            records=[record],
            sources=[SourceAttribution(provider="brave")],
            required_fields=[],
        )
        assert report.confidence_score < 0.80, (
            f"BLOCKER: Compliance query with Tier C source only should not reach "
            f"'verified' confidence ({report.confidence_score:.3f} >= 0.80)"
        )

    def test_compliance_from_single_brave_source_is_partially_verified_not_verified(self):
        """Single Brave source for compliance → status must be partially_verified or unverified."""
        record = {
            "content": "Tax compliance info from blog",
            "sources": [{"provider": "brave"}],
        }
        report = verify_records(
            records=[record],
            sources=[SourceAttribution(provider="brave")],
            required_fields=[],
        )
        assert report.status in ("partially_verified", "unverified"), (
            f"BLOCKER: Compliance from single C-tier source should not be 'verified' "
            f"(got '{report.status}')"
        )

    def test_compliance_from_tier_a_source_can_reach_verified(self):
        """Compliance with official government data (Tier A equivalent) can reach verified.
        This tests the positive counter-case: with strong sources + no missing fields, verified is achievable."""
        # Simulate government source at Tier A trust weight
        record = {
            "content": "IRS official: Q1 2025 estimated tax due April 15.",
            "sources": [{"provider": "attom"}],
        }
        report = verify_records(
            records=[record],
            sources=[SourceAttribution(provider="attom")],
            required_fields=[],
        )
        assert report.confidence_score >= 0.80


# ---------------------------------------------------------------------------
# Negative 5: Cross-tenant cache read is impossible (Law #6)
# ---------------------------------------------------------------------------


class TestCrossTenantCacheImpossible:
    """Law #6 — zero cross-tenant cache leakage at the cache module level."""

    def test_tenant_a_private_data_not_readable_by_tenant_b(self):
        """Tenant A stores secret data. Tenant B must not be able to read it."""
        PRIVATE_DATA = {"secret": "tenant_a_only_data", "balance": 99999}
        cache_set(
            tenant_id=TENANT_A,
            provider="attom",
            playbook="PROPERTY_FACTS",
            query="private query",
            value=PRIVATE_DATA,
        )
        result = cache_get(
            tenant_id=TENANT_B,
            provider="attom",
            playbook="PROPERTY_FACTS",
            query="private query",
        )
        assert result is None, (
            "BLOCKER (Law #6): Cross-tenant cache read succeeded — "
            f"Tenant B received: {result}"
        )

    def test_tenant_b_private_data_not_readable_by_tenant_a(self):
        """Tenant B stores data. Tenant A must not read it."""
        cache_set(
            tenant_id=TENANT_B,
            provider="google_places",
            playbook="SUBCONTRACTOR_SCOUT",
            query="roofers",
            value={"b_data": True},
        )
        result = cache_get(
            tenant_id=TENANT_A,
            provider="google_places",
            playbook="SUBCONTRACTOR_SCOUT",
            query="roofers",
        )
        assert result is None, (
            "BLOCKER (Law #6): Tenant A read Tenant B data from cache"
        )

    def test_cache_key_hashes_tenant_id(self):
        """Raw tenant IDs must not appear in cache keys (Law #9 privacy)."""
        from aspire_orchestrator.services.adam.cache import _cache
        cache_set(
            tenant_id=TENANT_A,
            provider="brave",
            playbook="TEST",
            query="test",
            value={"x": 1},
        )
        for key in _cache.keys():
            assert TENANT_A not in key, (
                f"BLOCKER (Law #9): Raw tenant_id '{TENANT_A}' found in cache key '{key}'"
            )


# ---------------------------------------------------------------------------
# Negative 6: Provider conflict is surfaced, not suppressed (ADR-003)
# ---------------------------------------------------------------------------


class TestConflictSurfacedNotSuppressed:
    """verify_records must surface all detected field conflicts in the report."""

    def test_conflicting_name_surfaced_in_report(self):
        """Two records with different names from different providers → conflict in report."""
        r1 = {"name": "Acme HVAC Services", "sources": [{"provider": "google_places"}]}
        r2 = {"name": "Beta Heating Corporation", "sources": [{"provider": "foursquare"}]}
        report = verify_records(
            records=[r1, r2],
            sources=[
                SourceAttribution(provider="google_places"),
                SourceAttribution(provider="foursquare"),
            ],
        )
        assert report.conflict_count >= 1, (
            "BLOCKER (ADR-003): Name conflict was not reported"
        )
        field_names = [c.field_name for c in report.conflicts]
        assert "name" in field_names, (
            f"Expected 'name' in conflict fields, got: {field_names}"
        )

    def test_conflict_report_not_empty_when_values_differ(self):
        """report.conflicts must be a non-empty list when a conflict exists."""
        r1 = {"living_sqft": 1000, "sources": [{"provider": "attom"}]}
        r2 = {"living_sqft": 2000, "sources": [{"provider": "here"}]}
        report = verify_records(
            records=[r1, r2],
            sources=[
                SourceAttribution(provider="attom"),
                SourceAttribution(provider="here"),
            ],
        )
        assert len(report.conflicts) > 0, (
            "BLOCKER (ADR-003): Conflicting living_sqft values were suppressed — "
            "report.conflicts is empty"
        )

    def test_conflict_resolution_never_silently_merges_values(self):
        """When a conflict exists, the FieldConflict contains both values, not a merged one."""
        r1 = {"name": "Alpha Corp", "sources": [{"provider": "google_places"}]}
        r2 = {"name": "Beta LLC", "sources": [{"provider": "foursquare"}]}
        report = verify_records(
            records=[r1, r2],
            sources=[
                SourceAttribution(provider="google_places"),
                SourceAttribution(provider="foursquare"),
            ],
        )
        name_conflicts = [c for c in report.conflicts if c.field_name == "name"]
        if name_conflicts:
            conflict = name_conflicts[0]
            all_values = [str(entry.get("value", "")) for entry in conflict.values]
            # Both "Alpha Corp" and "Beta LLC" must appear — neither silently merged
            value_str = " ".join(all_values).lower()
            assert "alpha" in value_str, "First conflicting value missing from report"
            assert "beta" in value_str, "Second conflicting value missing from report"

    def test_conflict_present_in_to_dict_output(self):
        """VerificationReport.to_dict() includes conflicts — not silently dropped on serialization."""
        r1 = {"name": "X Corp", "sources": [{"provider": "google_places"}]}
        r2 = {"name": "Y Corp", "sources": [{"provider": "foursquare"}]}
        report = verify_records(
            records=[r1, r2],
            sources=[
                SourceAttribution(provider="google_places"),
                SourceAttribution(provider="foursquare"),
            ],
        )
        d = report.to_dict()
        assert "conflicts" in d
        assert isinstance(d["conflicts"], list)
        if report.conflict_count > 0:
            assert len(d["conflicts"]) > 0, (
                "BLOCKER: Conflicts present in report but missing from to_dict() output"
            )


# ---------------------------------------------------------------------------
# Negative 7: Error codes cover expected failure categories
# (Law #3 — well-defined refusal taxonomy)
# ---------------------------------------------------------------------------


class TestAdamErrorCodes:
    """AdamErrorCode covers all expected Adam failure categories."""

    def test_missing_required_input_defined(self):
        assert AdamErrorCode.MISSING_REQUIRED_INPUT == "MISSING_REQUIRED_INPUT"

    def test_address_not_normalized_defined(self):
        assert AdamErrorCode.ADDRESS_NOT_NORMALIZED == "ADDRESS_NOT_NORMALIZED"

    def test_entitlement_missing_defined(self):
        assert AdamErrorCode.ENTITLEMENT_MISSING == "ENTITLEMENT_MISSING"

    def test_budget_exhausted_defined(self):
        assert AdamErrorCode.BUDGET_EXHAUSTED == "BUDGET_EXHAUSTED"

    def test_ambiguous_product_defined(self):
        assert AdamErrorCode.AMBIGUOUS_PRODUCT == "AMBIGUOUS_PRODUCT"

    def test_no_verified_results_defined(self):
        assert AdamErrorCode.NO_VERIFIED_RESULTS == "NO_VERIFIED_RESULTS"

    def test_low_confidence_defined(self):
        assert AdamErrorCode.LOW_CONFIDENCE == "LOW_CONFIDENCE"


# ---------------------------------------------------------------------------
# Negative 8: Classifier never panics — always returns a valid result
# (Law #3 safety net)
# ---------------------------------------------------------------------------


class TestClassifierNeverPanics:
    """classify_fast must handle any input without raising."""

    @pytest.mark.parametrize("query", [
        "",
        "   ",
        "\n\t",
        "a" * 10000,   # very long input
        "!@#$%^&*()",  # special characters only
        "0" * 500,     # all digits
        "SELECT * FROM users WHERE 1=1; DROP TABLE receipts;",  # SQL injection attempt
        "<script>alert('xss')</script>",  # XSS attempt in query
    ])
    def test_classify_fast_does_not_raise(self, query: str):
        """classify_fast returns a ClassificationResult for all inputs — never raises."""
        from aspire_orchestrator.services.adam.classifiers import (
            ClassificationResult,
            classify_fast,
        )
        result = classify_fast(query)
        assert isinstance(result, ClassificationResult)
        assert result.segment in (
            "trades", "landlord", "accounting_bookkeeping", "travel", "general_smb"
        )


# ---------------------------------------------------------------------------
# Negative 9: route_to_playbook never panics
# (Law #3: clean None return on no match, never exception)
# ---------------------------------------------------------------------------


class TestRouterNeverPanics:
    """route_to_playbook must return (ClassificationResult, None) on no match — never raise."""

    def test_completely_random_query_does_not_raise(self):
        """A nonsense query must return cleanly without exception."""
        classification, playbook = route_to_playbook(
            "xyzzy frob quux wibble wobble zap!@#$%"
        )
        assert classification is not None
        # playbook may or may not be None — both are valid

    def test_empty_string_query_does_not_raise(self):
        classification, playbook = route_to_playbook("")
        assert classification is not None

    def test_sql_injection_in_query_does_not_raise(self):
        classification, playbook = route_to_playbook(
            "'; DROP TABLE receipts; --"
        )
        assert classification is not None
