"""Tests for legal_query_analyzer.py — Deterministic filter extraction.

30+ query patterns testing domain, jurisdiction, template, and chunk type extraction.
No LLM — pure keyword/regex matching.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _mock_template_resolution():
    """Mock Clara template resolution to avoid import issues.

    The analyzer imports _resolve_template_key and get_template_spec from
    clara_legal — mock them at the import source so lazy imports work.
    """
    with patch(
        "aspire_orchestrator.skillpacks.clara_legal._resolve_template_key",
        side_effect=lambda x: x,
    ), patch(
        "aspire_orchestrator.skillpacks.clara_legal.get_template_spec",
        return_value=None,
    ):
        yield


# ---------------------------------------------------------------------------
# Tests: Jurisdiction extraction
# ---------------------------------------------------------------------------


class TestJurisdictionExtraction:
    def test_full_state_name(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("NDA requirements in California")
        assert f.jurisdiction_state == "CA"

    def test_state_abbreviation(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("Contract rules in NY")
        assert f.jurisdiction_state == "NY"

    def test_texas(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("Texas business formation requirements")
        assert f.jurisdiction_state == "TX"

    def test_no_state(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("What is force majeure?")
        assert f.jurisdiction_state is None

    def test_ambiguous_abbreviation_with_context(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        # "IN" is ambiguous — should only match with location context
        f = analyze_query("Contract law in IN")
        assert f.jurisdiction_state == "IN"

    def test_ambiguous_abbreviation_without_context(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        # "OR" without context should not match Oregon
        f = analyze_query("clause OR provision")
        assert f.jurisdiction_state is None

    def test_multi_word_state(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("Requirements in New Jersey")
        assert f.jurisdiction_state == "NJ"

    def test_massachusetts(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("Non-compete rules in Massachusetts")
        assert f.jurisdiction_state == "MA"


# ---------------------------------------------------------------------------
# Tests: Domain extraction
# ---------------------------------------------------------------------------


class TestDomainExtraction:
    def test_pandadoc_api_keywords(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("PandaDoc API rate limits")
        assert f.domain == "pandadoc_api"

    def test_endpoint_keyword(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("What endpoint creates documents?")
        assert f.domain == "pandadoc_api"

    def test_webhook_keyword(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("webhook payload format")
        assert f.domain == "pandadoc_api"

    def test_contract_law_keywords(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("force majeure clause meaning")
        assert f.domain == "contract_law"

    def test_indemnification(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("indemnification best practices")
        assert f.domain == "contract_law"

    def test_compliance_keywords(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("GDPR compliance requirements")
        assert f.domain == "compliance_risk"

    def test_red_flags(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("contract red flags to watch for")
        assert f.domain == "compliance_risk"

    def test_business_context(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("net-30 payment terms explanation")
        assert f.domain == "business_context"

    def test_no_domain_for_generic_query(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("hello world")
        assert f.domain is None


# ---------------------------------------------------------------------------
# Tests: Template extraction
# ---------------------------------------------------------------------------


class TestTemplateExtraction:
    @patch("aspire_orchestrator.skillpacks.clara_legal.get_template_spec")
    @patch("aspire_orchestrator.skillpacks.clara_legal._resolve_template_key")
    def test_mutual_nda(self, mock_resolve, mock_spec):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        mock_spec.return_value = {"lane": "general"}
        mock_resolve.return_value = "general_mutual_nda"
        f = analyze_query("mutual NDA template requirements")
        assert f.template_key == "general_mutual_nda"

    @patch("aspire_orchestrator.skillpacks.clara_legal.get_template_spec")
    @patch("aspire_orchestrator.skillpacks.clara_legal._resolve_template_key")
    def test_residential_lease(self, mock_resolve, mock_spec):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        mock_spec.return_value = {"lane": "landlord"}
        mock_resolve.return_value = "landlord_residential_lease_base"
        f = analyze_query("residential lease agreement template")
        assert f.template_key == "landlord_residential_lease_base"


# ---------------------------------------------------------------------------
# Tests: Chunk type extraction
# ---------------------------------------------------------------------------


class TestChunkTypeExtraction:
    def test_clause_type(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("standard indemnification clause")
        assert f.chunk_types is not None
        assert "clause" in f.chunk_types

    def test_definition_type(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("definition of force majeure")
        assert f.chunk_types is not None
        assert "definition" in f.chunk_types

    def test_checklist_type(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("contract review checklist")
        assert f.chunk_types is not None
        assert "checklist" in f.chunk_types

    def test_no_chunk_type(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("general contract advice")
        assert f.chunk_types is None


# ---------------------------------------------------------------------------
# Tests: Reranking decision
# ---------------------------------------------------------------------------


class TestRerankDecision:
    def test_review_terms_enables_rerank(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("review this NDA", method_context="review_contract_terms")
        assert f.rerank_enabled is True

    def test_sign_enables_rerank(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("sign contract", method_context="sign_contract")
        assert f.rerank_enabled is True

    def test_browse_disables_rerank(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("browse templates", method_context="browse_templates")
        assert f.rerank_enabled is False

    def test_no_context_disables_rerank(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("review this NDA")
        assert f.rerank_enabled is False


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_query(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("")
        assert f.domain is None
        assert f.jurisdiction_state is None
        assert f.template_key is None

    def test_whitespace_query(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("   \n\t  ")
        assert f.domain is None

    def test_combined_filters(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("indemnification clause requirements in California")
        assert f.domain == "contract_law"
        assert f.jurisdiction_state == "CA"
        assert f.chunk_types is not None
        assert "clause" in f.chunk_types

    def test_sql_injection_attempt_no_filter(self):
        from aspire_orchestrator.services.legal_query_analyzer import analyze_query
        f = analyze_query("'; DROP TABLE legal_knowledge_chunks; --")
        # Should not crash, should return no meaningful filters
        assert isinstance(f.domain, (str, type(None)))
