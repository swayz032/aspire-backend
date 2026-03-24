"""Unit tests for Clara Intelligence Waves 2-4 — Quality, Compliance, Narration.

Tests _assess_document_quality(), _intelligent_compliance_assessment(),
and specialist narration integration.

Law coverage:
  - Law #2: Quality assessment tracks fill coverage (audit readiness)
  - Law #3: Fail closed on LLM errors (compliance falls back to Layer 1)
  - Law #4: Risk tier awareness in compliance urgency levels
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.providers.pandadoc_client import _assess_document_quality
from aspire_orchestrator.services.narration import compose_narration


# ---------------------------------------------------------------------------
# _assess_document_quality tests
# ---------------------------------------------------------------------------


class TestAssessDocumentQuality:
    """Tests for post-creation document quality assessment."""

    def test_perfect_fill_grade_a(self) -> None:
        """100% fill rate = grade A, confidence 98."""
        tokens = [
            {"name": "Sender.Company", "value": "Skytech"},
            {"name": "Client.Company", "value": "BlueWave"},
        ]
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent={}, missing_tokens=[], template_type="trades_hvac_proposal",
        )
        assert result["confidence_score"] == 98
        assert result["quality_grade"] == "A"
        assert result["tokens_filled"] == 2
        assert result["tokens_total"] == 2
        assert result["ready_for_review"] is True
        assert len(result["proactive_warnings"]) == 0

    def test_high_fill_grade_a(self) -> None:
        """90%+ fill rate = grade A, confidence 92."""
        tokens = [{"name": f"Token.{i}", "value": f"val{i}"} for i in range(10)]
        tokens.append({"name": "Sender.Website", "value": ""})  # 1 missing
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent={}, missing_tokens=["Sender.Website"],
        )
        assert result["confidence_score"] == 92
        assert result["quality_grade"] == "A"

    def test_medium_fill_grade_b(self) -> None:
        """70-89% fill rate = grade B."""
        tokens = [{"name": f"Token.{i}", "value": f"val{i}"} for i in range(8)]
        tokens.extend([{"name": f"Missing.{i}", "value": ""} for i in range(3)])
        missing = [f"Missing.{i}" for i in range(3)]
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent={}, missing_tokens=missing,
        )
        assert result["quality_grade"] == "B"
        assert result["confidence_score"] == 80

    def test_low_fill_grade_c(self) -> None:
        """50-69% fill rate = grade C."""
        tokens = [{"name": f"Token.{i}", "value": f"val{i}"} for i in range(6)]
        tokens.extend([{"name": f"Missing.{i}", "value": ""} for i in range(4)])
        missing = [f"Missing.{i}" for i in range(4)]
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent={}, missing_tokens=missing,
        )
        assert result["quality_grade"] == "C"
        assert result["confidence_score"] == 60
        assert result["ready_for_review"] is False

    def test_poor_fill_grade_d(self) -> None:
        """<50% fill rate = grade D."""
        tokens = [{"name": "Token.0", "value": "val"}]
        tokens.extend([{"name": f"Missing.{i}", "value": ""} for i in range(3)])
        missing = [f"Missing.{i}" for i in range(3)]
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent={}, missing_tokens=missing,
        )
        assert result["quality_grade"] == "D"
        assert result["confidence_score"] == 40

    def test_address_chain_specialist_note(self) -> None:
        """When full address chains are present, specialist notes mention it."""
        addr_tokens = [
            "Sender.Address", "Sender.City", "Sender.State", "Sender.Zip",
            "Client.Address", "Client.City", "Client.State", "Client.Zip",
        ]
        tokens = [{"name": n, "value": "filled"} for n in addr_tokens]
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent={}, missing_tokens=[],
        )
        assert any("address chains" in n.lower() for n in result["specialist_notes"])

    def test_party_identification_note(self) -> None:
        """When all party ID tokens filled, specialist notes confirm."""
        id_tokens = [
            "Sender.Company", "Sender.FirstName",
            "Client.Company", "Client.FirstName",
        ]
        tokens = [{"name": n, "value": "filled"} for n in id_tokens]
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent={}, missing_tokens=[],
        )
        assert any("party identification" in n.lower() for n in result["specialist_notes"])

    def test_fields_prefilled_note(self) -> None:
        """When auto fields are prefilled, note mentions it."""
        tokens = [{"name": "Sender.Company", "value": "Test"}]
        fields = {"Date1": {"value": "02/22/2026"}, "Date2": {"value": "02/22/2026"}}
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent=fields, missing_tokens=[],
        )
        assert any("auto-prefilled" in n.lower() for n in result["specialist_notes"])

    def test_proactive_warning_critical_missing(self) -> None:
        """Critical missing tokens produce proactive warnings."""
        tokens = [{"name": "Sender.Company", "value": ""}]
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent={},
            missing_tokens=["Sender.Company"],
        )
        assert len(result["proactive_warnings"]) > 0
        assert "important" in result["proactive_warnings"][0].lower()

    def test_proactive_warning_optional_missing(self) -> None:
        """Optional missing tokens produce softer warnings."""
        tokens = [
            {"name": "Sender.Company", "value": "Test"},
            {"name": "Sender.Website", "value": ""},
        ]
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent={},
            missing_tokens=["Sender.Website"],
        )
        assert len(result["proactive_warnings"]) > 0
        assert "optional" in result["proactive_warnings"][0].lower()

    def test_empty_tokens_list(self) -> None:
        """Edge case: no tokens at all = 100% fill rate."""
        result = _assess_document_quality(
            tokens_sent=[], fields_sent={}, missing_tokens=[],
        )
        assert result["confidence_score"] == 98
        assert result["quality_grade"] == "A"


# ---------------------------------------------------------------------------
# _intelligent_compliance_assessment tests
# ---------------------------------------------------------------------------


class TestIntelligentComplianceAssessment:
    """Tests for the LLM-enhanced compliance assessment."""

    @pytest.mark.asyncio
    async def test_fallback_without_api_key(self) -> None:
        """Without API key, returns deterministic Layer 1 output."""
        from aspire_orchestrator.skillpacks.clara_legal import _intelligent_compliance_assessment
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.openai_api_key = ""

        with patch("aspire_orchestrator.config.settings.settings", mock_settings):
            result = await _intelligent_compliance_assessment(
                {"status": "document.draft", "name": "Test NDA"},
                "test-id-123",
                suite_id="STE-0001",
            )

        assert result["compliance_status"] == "pending"
        assert result["contract_id"] == "test-id-123"
        assert "specialist_assessment" not in result  # LLM didn't run

    @pytest.mark.asyncio
    async def test_layer1_voided_contract(self) -> None:
        """Voided contract = terminated status (deterministic)."""
        from aspire_orchestrator.skillpacks.clara_legal import _intelligent_compliance_assessment
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.openai_api_key = ""

        with patch("aspire_orchestrator.config.settings.settings", mock_settings):
            result = await _intelligent_compliance_assessment(
                {"status": "voided", "name": "Expired NDA"},
                "void-id",
            )

        assert result["compliance_status"] == "terminated"

    @pytest.mark.asyncio
    async def test_llm_enrichment_succeeds(self) -> None:
        """When LLM works, compliance data is enriched with specialist assessment."""
        from aspire_orchestrator.skillpacks.clara_legal import _intelligent_compliance_assessment

        mock_content = json.dumps({
            "specialist_assessment": "Contract is active with no compliance issues.",
            "recommended_actions": ["Schedule 60-day renewal review"],
            "risk_score": 15,
        })

        with patch("aspire_orchestrator.config.settings.settings") as mock_settings:
            mock_settings.openai_api_key = "test-key"
            mock_settings.openai_base_url = None
            mock_settings.router_model_reasoner = "gpt-5.2"
            with patch(
                "aspire_orchestrator.skillpacks.clara_legal.generate_text_async",
                AsyncMock(return_value=mock_content),
            ):
                result = await _intelligent_compliance_assessment(
                    {"status": "document.completed", "name": "Active MSA"},
                    "active-id",
                    suite_id="STE-0001",
                )

        assert result["compliance_status"] == "active"
        assert result["specialist_assessment"] == "Contract is active with no compliance issues."
        assert result["risk_score"] == 15
        assert "Schedule 60-day renewal review" in result["recommended_actions"]

    @pytest.mark.asyncio
    async def test_llm_failure_graceful_degradation(self) -> None:
        """LLM failure returns Layer 1 output with no enrichment."""
        from aspire_orchestrator.skillpacks.clara_legal import _intelligent_compliance_assessment

        with patch("aspire_orchestrator.config.settings.settings") as mock_settings:
            mock_settings.openai_api_key = "test-key"
            mock_settings.openai_base_url = None
            mock_settings.router_model_reasoner = "gpt-5.2"
            with patch(
                "aspire_orchestrator.skillpacks.clara_legal.generate_text_async",
                AsyncMock(side_effect=Exception("boom")),
            ):
                result = await _intelligent_compliance_assessment(
                    {"status": "document.sent", "name": "Sent Contract"},
                    "sent-id",
                )

        assert result["compliance_status"] == "awaiting_signature"
        assert "specialist_assessment" not in result  # LLM didn't enrich


# ---------------------------------------------------------------------------
# Specialist narration tests
# ---------------------------------------------------------------------------


class TestSpecialistNarration:
    """Tests for specialist-aware contract narration."""

    def test_contract_narration_with_quality_data(self) -> None:
        """When quality data is present with high confidence, use specialist narration."""
        result = compose_narration(
            outcome="pending",
            task_type="contract",
            tool_used="pandadoc.contract.generate",
            execution_params={
                "template_type": "trades_hvac_proposal",
                "authority_queue": True,
            },
            execution_result={
                "document_quality": {
                    "confidence_score": 95,
                    "tokens_filled": 16,
                    "tokens_total": 16,
                    "proactive_warnings": [],
                },
            },
            draft_id="doc-123",
            risk_tier="yellow",
            owner_name="Antonio",
            subject_name="GreenBuild Properties",
        )
        assert "16" in result and "fields" in result
        assert "95%" in result
        assert "Authority Queue" in result

    def test_contract_narration_with_warnings(self) -> None:
        """Specialist narration includes proactive warnings."""
        result = compose_narration(
            outcome="pending",
            task_type="contract",
            tool_used="pandadoc.contract.generate",
            execution_params={
                "template_type": "trades_hvac_proposal",
                "authority_queue": True,
            },
            execution_result={
                "document_quality": {
                    "confidence_score": 92,
                    "tokens_filled": 15,
                    "tokens_total": 16,
                    "proactive_warnings": [
                        "1 optional field(s) left blank (Sender.Website) -- you can add them in PandaDoc"
                    ],
                },
            },
            draft_id="doc-123",
            risk_tier="yellow",
            owner_name="Antonio",
            subject_name="GreenBuild Properties",
        )
        assert "optional" in result.lower()

    def test_contract_narration_without_quality_data(self) -> None:
        """Without quality data, falls back to standard narration."""
        result = compose_narration(
            outcome="pending",
            task_type="contract",
            tool_used="pandadoc.contract.generate",
            execution_params={
                "template_type": "trades_hvac_proposal",
                "authority_queue": True,
            },
            execution_result={},
            draft_id="doc-123",
            risk_tier="yellow",
            owner_name="Antonio",
            subject_name="GreenBuild Properties",
        )
        assert "Authority Queue" in result
        assert "hvac proposal" in result.lower()

    def test_new_template_labels_in_narration(self) -> None:
        """Verify new template types have proper human-readable labels."""
        for tpl_type, expected_fragment in [
            ("trades_hvac_proposal", "hvac"),
            ("trades_roofing_proposal", "roofing"),
            ("trades_painting_proposal", "painting"),
            ("landlord_commercial_sublease", "sublease"),
            ("acct_tax_filing", "tax filing"),
            ("general_w9", "w-9"),
        ]:
            result = compose_narration(
                outcome="pending",
                task_type="contract",
                tool_used="pandadoc.contract.generate",
                execution_params={"template_type": tpl_type, "authority_queue": True},
                execution_result={},
                draft_id="doc-123",
                risk_tier="yellow",
                subject_name="Test Client",
            )
            assert expected_fragment in result.lower(), (
                f"Expected '{expected_fragment}' in narration for {tpl_type}: {result}"
            )


# ---------------------------------------------------------------------------
# Enhanced quality assessment tests (pricing + content)
# ---------------------------------------------------------------------------


class TestQualityWithContentIntelligence:
    """Tests for quality assessment with pricing tables and content placeholders."""

    def test_pricing_table_boosts_notes(self) -> None:
        """When pricing tables are populated, specialist notes include it."""
        tokens = [{"name": "Sender.Company", "value": "Test"}]
        pricing = [{
            "name": "Pricing Table 1",
            "sections": [{"rows": [
                {"data": {"name": "HVAC", "price": "15000.00", "qty": "1"}},
            ]}],
        }]
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent={}, missing_tokens=[],
            pricing_tables_sent=pricing,
        )
        assert result["pricing_table_populated"] is True
        assert any("pricing table" in n.lower() for n in result["specialist_notes"])
        # Should mention the amount
        assert any("$15,000.00" in n for n in result["specialist_notes"])

    def test_content_placeholders_in_notes(self) -> None:
        """When content placeholders are generated, specialist notes mention it."""
        tokens = [{"name": "Sender.Company", "value": "Test"}]
        content = [{"uuid": "ph-1", "blocks": [{"type": "paragraph"}]}]
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent={}, missing_tokens=[],
            content_placeholders_sent=content,
        )
        assert result["content_placeholders_populated"] is True
        assert any("content section" in n.lower() for n in result["specialist_notes"])

    def test_no_pricing_no_content_default(self) -> None:
        """Without pricing or content, defaults remain unchanged."""
        tokens = [{"name": "Sender.Company", "value": "Test"}]
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent={}, missing_tokens=[],
        )
        assert result["pricing_table_populated"] is False
        assert result["content_placeholders_populated"] is False

    def test_pricing_with_multiple_rows(self) -> None:
        """Multiple pricing rows correctly counted."""
        tokens = [{"name": "Sender.Company", "value": "Test"}]
        pricing = [{
            "name": "Pricing Table 1",
            "sections": [{"rows": [
                {"data": {"name": "Labor", "price": "5000.00", "qty": "1"}},
                {"data": {"name": "Materials", "price": "3000.00", "qty": "2"}},
            ]}],
        }]
        result = _assess_document_quality(
            tokens_sent=tokens, fields_sent={}, missing_tokens=[],
            pricing_tables_sent=pricing,
        )
        assert any("2 line items" in n for n in result["specialist_notes"])


class TestNarrationWithContentIntelligence:
    """Tests for narration with pricing + content data."""

    def test_narration_mentions_pricing(self) -> None:
        """When quality data includes pricing, narration mentions it."""
        result = compose_narration(
            outcome="pending",
            task_type="contract",
            tool_used="pandadoc.contract.generate",
            execution_params={
                "template_type": "trades_hvac_proposal",
                "authority_queue": True,
            },
            execution_result={
                "document_quality": {
                    "confidence_score": 95,
                    "tokens_filled": 16,
                    "tokens_total": 16,
                    "proactive_warnings": [],
                    "pricing_table_populated": True,
                    "content_placeholders_populated": False,
                    "specialist_notes": [
                        "All party identification tokens filled correctly",
                        "Pricing table populated (1 line item, total $15,000.00)",
                    ],
                },
            },
            draft_id="doc-123",
            risk_tier="yellow",
            owner_name="Antonio",
            subject_name="GreenBuild Properties",
        )
        assert "pricing table" in result.lower()

    def test_narration_mentions_content(self) -> None:
        """When quality data includes content placeholders, narration mentions it."""
        result = compose_narration(
            outcome="pending",
            task_type="contract",
            tool_used="pandadoc.contract.generate",
            execution_params={
                "template_type": "trades_hvac_proposal",
                "authority_queue": True,
            },
            execution_result={
                "document_quality": {
                    "confidence_score": 95,
                    "tokens_filled": 16,
                    "tokens_total": 16,
                    "proactive_warnings": [],
                    "pricing_table_populated": False,
                    "content_placeholders_populated": True,
                    "specialist_notes": [
                        "All party identification tokens filled correctly",
                        "2 content sections generated",
                    ],
                },
            },
            draft_id="doc-123",
            risk_tier="yellow",
            owner_name="Antonio",
            subject_name="GreenBuild Properties",
        )
        assert "content section" in result.lower()
