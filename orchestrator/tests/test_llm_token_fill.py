"""Unit tests for Clara Intelligence Wave 1 — LLM Token Fill + Validation.

Tests _validate_token_value() and _llm_fill_missing_tokens() in isolation.
All LLM calls are mocked — no external API needed.

Law coverage:
  - Law #2: Token fill produces audit trail via logging
  - Law #3: Fail closed on missing API key, LLM errors
  - Law #7: Token fill is bounded execution (returns values, never decides)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.providers.pandadoc_client import (
    _validate_token_value,
    _llm_fill_missing_tokens,
    _assess_document_quality,
    _build_pricing_tables,
    _build_content_placeholders,
    _llm_parse_pricing,
)


# ---------------------------------------------------------------------------
# _validate_token_value tests
# ---------------------------------------------------------------------------


class TestValidateTokenValue:
    """Tests for format validation of LLM-proposed token values."""

    def test_email_valid(self) -> None:
        assert _validate_token_value("Client.Email", "alice@example.com") is True

    def test_email_invalid(self) -> None:
        assert _validate_token_value("Client.Email", "not-an-email") is False

    def test_phone_valid(self) -> None:
        assert _validate_token_value("Sender.Phone", "(512) 555-0199") is True

    def test_phone_invalid(self) -> None:
        assert _validate_token_value("Sender.Phone", "abc") is False

    def test_zip_valid(self) -> None:
        assert _validate_token_value("Client.Zip", "78701") is True

    def test_zip_invalid(self) -> None:
        assert _validate_token_value("Client.Zip", "no-digits-here") is False

    def test_state_valid(self) -> None:
        assert _validate_token_value("Sender.State", "TX") is True

    def test_state_too_long(self) -> None:
        assert _validate_token_value("Sender.State", "X" * 40) is False

    def test_value_with_dollar(self) -> None:
        assert _validate_token_value("Document.Value", "$2,500/month") is True

    def test_value_with_digits(self) -> None:
        assert _validate_token_value("Document.Fee", "150000") is True

    def test_value_no_digits(self) -> None:
        assert _validate_token_value("Document.Value", "some text") is False

    def test_empty_string_rejected(self) -> None:
        assert _validate_token_value("Sender.Company", "") is False

    def test_whitespace_only_rejected(self) -> None:
        assert _validate_token_value("Client.Company", "   ") is False

    def test_generic_token_accepted(self) -> None:
        """Non-specific tokens accept any non-empty string."""
        assert _validate_token_value("Sender.Website", "https://skytechdev.com") is True


# ---------------------------------------------------------------------------
# _llm_fill_missing_tokens tests
# ---------------------------------------------------------------------------


class TestLLMFillMissingTokens:
    """Tests for the GPT-5.2 powered token fill layer."""

    @pytest.mark.asyncio
    async def test_no_missing_tokens_returns_empty(self) -> None:
        """When there are no missing tokens, skip LLM entirely."""
        filled, still_missing = await _llm_fill_missing_tokens(
            missing_tokens=[],
            filled_tokens={"Sender.Company": "Test"},
            sender_data={},
            client_data={},
            terms={},
        )
        assert filled == {}
        assert still_missing == []

    @pytest.mark.asyncio
    async def test_no_api_key_returns_graceful(self) -> None:
        """Without API key, return empty fill + all tokens still missing."""
        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value=""):
            filled, still_missing = await _llm_fill_missing_tokens(
                missing_tokens=["Sender.Website"],
                filled_tokens={},
                sender_data={"website": "https://test.com"},
                client_data={},
                terms={},
            )
        assert filled == {}
        assert still_missing == ["Sender.Website"]

    @pytest.mark.asyncio
    async def test_successful_llm_fill(self) -> None:
        """LLM returns valid values for missing tokens."""
        llm_json = json.dumps({
            "Sublessee.Company": "BlueWave Marketing",
            "Document.Value": "$2,500",
        })

        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value="test-key"):
            with patch("aspire_orchestrator.providers.pandadoc_client.generate_text_async",
                       new_callable=AsyncMock, return_value=llm_json):
                filled, still_missing = await _llm_fill_missing_tokens(
                    missing_tokens=["Sublessee.Company", "Document.Value"],
                    filled_tokens={"Sender.Company": "Skytech"},
                    sender_data={"company": "Skytech"},
                    client_data={"company": "BlueWave Marketing"},
                    terms={"fee": "$2,500"},
                )

        assert filled == {
            "Sublessee.Company": "BlueWave Marketing",
            "Document.Value": "$2,500",
        }
        assert still_missing == []

    @pytest.mark.asyncio
    async def test_llm_returns_invalid_values_filtered(self) -> None:
        """LLM returns values that fail validation — they become still_missing."""
        llm_json = json.dumps({
            "Client.Email": "not-an-email",  # fails validation
            "Client.Company": "ValidCo LLC",  # passes
        })

        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value="test-key"):
            with patch("aspire_orchestrator.providers.pandadoc_client.generate_text_async",
                       new_callable=AsyncMock, return_value=llm_json):
                filled, still_missing = await _llm_fill_missing_tokens(
                    missing_tokens=["Client.Email", "Client.Company"],
                    filled_tokens={},
                    sender_data={},
                    client_data={},
                    terms={},
                )

        assert filled == {"Client.Company": "ValidCo LLC"}
        assert still_missing == ["Client.Email"]

    @pytest.mark.asyncio
    async def test_llm_exception_graceful_degradation(self) -> None:
        """LLM call throws exception — graceful degradation."""
        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value="test-key"):
            with patch("aspire_orchestrator.providers.pandadoc_client.generate_text_async",
                       new_callable=AsyncMock, side_effect=Exception("API down")):
                filled, still_missing = await _llm_fill_missing_tokens(
                    missing_tokens=["Sender.Website"],
                    filled_tokens={},
                    sender_data={},
                    client_data={},
                    terms={},
                )

        assert filled == {}
        assert still_missing == ["Sender.Website"]

    @pytest.mark.asyncio
    async def test_llm_returns_non_json_graceful(self) -> None:
        """LLM returns non-JSON response — graceful degradation."""
        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value="test-key"):
            with patch("aspire_orchestrator.providers.pandadoc_client.generate_text_async",
                       new_callable=AsyncMock, return_value="I cannot help with that."):
                filled, still_missing = await _llm_fill_missing_tokens(
                    missing_tokens=["Sender.Website"],
                    filled_tokens={},
                    sender_data={},
                    client_data={},
                    terms={},
                )

        assert filled == {}
        assert still_missing == ["Sender.Website"]


# ---------------------------------------------------------------------------
# _build_pricing_tables tests
# ---------------------------------------------------------------------------


class TestBuildPricingTables:
    """Tests for pricing table intelligence — Layer 3a."""

    @pytest.mark.asyncio
    async def test_empty_terms_returns_empty(self) -> None:
        """No terms → no pricing tables."""
        result = await _build_pricing_tables(terms={})
        assert result == []

    @pytest.mark.asyncio
    async def test_no_pricing_data_returns_empty(self) -> None:
        """Terms without pricing keys → no pricing tables."""
        result = await _build_pricing_tables(terms={"scope": "Full replacement"})
        assert result == []

    @pytest.mark.asyncio
    async def test_structured_line_items(self) -> None:
        """Pre-structured line_items flow through directly."""
        terms = {
            "line_items": [
                {"name": "HVAC Unit", "price": "8000", "qty": "1", "description": "Main unit"},
                {"name": "Installation Labor", "price": "4000.00", "qty": "1", "description": "8 hrs"},
            ]
        }
        result = await _build_pricing_tables(
            terms=terms, template_type="trades_hvac_proposal",
            template_spec={"pricing_table_name": "Pricing Table 1"},
        )
        assert len(result) == 1
        table = result[0]
        assert table["name"] == "Pricing Table 1"
        assert table["data_merge"] is False
        rows = table["sections"][0]["rows"]
        assert len(rows) == 2
        assert rows[0]["data"]["name"] == "HVAC Unit"
        assert rows[0]["data"]["price"] == "8000.00"
        assert rows[1]["data"]["price"] == "4000.00"

    @pytest.mark.asyncio
    async def test_budget_fallback_single_item(self) -> None:
        """Simple budget string → single line item (no LLM needed)."""
        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value=""):
            result = await _build_pricing_tables(
                terms={"budget": "$15,000", "scope": "Full HVAC replacement"},
                template_type="trades_hvac_proposal",
                template_spec={"pricing_table_name": "Pricing Table 1"},
            )
        assert len(result) == 1
        rows = result[0]["sections"][0]["rows"]
        assert len(rows) == 1
        assert rows[0]["data"]["name"] == "HVAC Services"
        assert rows[0]["data"]["price"] == "15000.00"
        assert rows[0]["data"]["description"] == "Full HVAC replacement"

    @pytest.mark.asyncio
    async def test_budget_numeric(self) -> None:
        """Numeric budget → single line item."""
        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value=""):
            result = await _build_pricing_tables(
                terms={"budget": 25000},
                template_type="trades_roofing_proposal",
                template_spec={"pricing_table_name": "Pricing Table 1"},
            )
        assert len(result) == 1
        rows = result[0]["sections"][0]["rows"]
        assert rows[0]["data"]["price"] == "25000.00"
        assert rows[0]["data"]["name"] == "Roofing Services"

    @pytest.mark.asyncio
    async def test_custom_pricing_table_name(self) -> None:
        """Template spec with custom pricing_table_name."""
        result = await _build_pricing_tables(
            terms={"line_items": [{"name": "Item", "price": "100"}]},
            template_spec={"pricing_table_name": "Bill of Materials"},
        )
        assert result[0]["name"] == "Bill of Materials"

    @pytest.mark.asyncio
    async def test_line_items_with_dollar_signs(self) -> None:
        """Price values with $ and commas are cleaned."""
        result = await _build_pricing_tables(
            terms={"line_items": [{"name": "Work", "price": "$1,500.50"}]},
            template_spec={"pricing_table_name": "Pricing Table 1"},
        )
        assert result[0]["sections"][0]["rows"][0]["data"]["price"] == "1500.50"


# ---------------------------------------------------------------------------
# _llm_parse_pricing tests
# ---------------------------------------------------------------------------


class TestLLMParsePricing:
    """Tests for LLM-powered pricing text parsing."""

    @pytest.mark.asyncio
    async def test_no_api_key_returns_empty(self) -> None:
        """Without API key, return empty list."""
        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value=""):
            result = await _llm_parse_pricing("$15,000 fixed fee")
        assert result == []

    @pytest.mark.asyncio
    async def test_successful_llm_parse(self) -> None:
        """LLM successfully parses free-text pricing."""
        llm_json = json.dumps([
            {"name": "HVAC System", "description": "Full replacement", "price": "12000.00", "qty": "1"},
            {"name": "Ductwork", "description": "New duct installation", "price": "3000.00", "qty": "1"},
        ])

        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value="test-key"):
            with patch("aspire_orchestrator.providers.pandadoc_client.generate_text_async",
                       new_callable=AsyncMock, return_value=llm_json):
                result = await _llm_parse_pricing("$15,000 total: HVAC system $12k, ductwork $3k")

        assert len(result) == 2
        assert result[0]["data"]["name"] == "HVAC System"
        assert result[0]["data"]["price"] == "12000.00"

    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty(self) -> None:
        """LLM failure → graceful degradation to empty list."""
        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value="test-key"):
            with patch("aspire_orchestrator.providers.pandadoc_client.generate_text_async",
                       new_callable=AsyncMock, side_effect=Exception("API down")):
                result = await _llm_parse_pricing("$15,000")
        assert result == []


# ---------------------------------------------------------------------------
# _build_content_placeholders tests
# ---------------------------------------------------------------------------


class TestBuildContentPlaceholders:
    """Tests for content placeholder intelligence — Layer 3b."""

    @pytest.mark.asyncio
    async def test_empty_placeholders_returns_empty(self) -> None:
        """No template placeholders → skip entirely."""
        result = await _build_content_placeholders(
            template_placeholders=[],
            terms={"scope": "Full replacement"},
            parties=[],
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_no_uuid_placeholders_filtered(self) -> None:
        """Placeholders without uuid are filtered out."""
        result = await _build_content_placeholders(
            template_placeholders=[{"name": "scope"}],  # missing uuid
            terms={"scope": "Full replacement"},
            parties=[],
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_no_api_key_returns_empty(self) -> None:
        """Without API key, return empty list."""
        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value=""):
            result = await _build_content_placeholders(
                template_placeholders=[{"uuid": "ph-1", "name": "scope"}],
                terms={"scope": "Full HVAC replacement"},
                parties=[],
            )
        assert result == []

    @pytest.mark.asyncio
    async def test_successful_content_generation(self) -> None:
        """LLM generates content blocks for placeholders."""
        llm_json = json.dumps([{
            "uuid": "ph-scope-1",
            "blocks": [
                {"type": "paragraph", "data": {"text": "This project covers full HVAC replacement."}},
            ],
        }])

        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value="test-key"):
            with patch("aspire_orchestrator.providers.pandadoc_client.generate_text_async",
                       new_callable=AsyncMock, return_value=llm_json):
                result = await _build_content_placeholders(
                    template_placeholders=[{"uuid": "ph-scope-1", "name": "scope_of_work"}],
                    terms={"scope": "Full HVAC replacement"},
                    parties=[{"role": "client", "name": "GreenBuild"}],
                    template_type="trades_hvac_proposal",
                )

        assert len(result) == 1
        assert result[0]["uuid"] == "ph-scope-1"
        assert len(result[0]["blocks"]) == 1

    @pytest.mark.asyncio
    async def test_invalid_uuid_filtered(self) -> None:
        """LLM returns content for unknown uuid → filtered out."""
        llm_json = json.dumps([{
            "uuid": "unknown-uuid",
            "blocks": [{"type": "paragraph", "data": {"text": "Injected content"}}],
        }])

        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value="test-key"):
            with patch("aspire_orchestrator.providers.pandadoc_client.generate_text_async",
                       new_callable=AsyncMock, return_value=llm_json):
                result = await _build_content_placeholders(
                    template_placeholders=[{"uuid": "ph-real-1", "name": "scope"}],
                    terms={"scope": "test"},
                    parties=[],
                )

        assert result == []  # unknown uuid rejected

    @pytest.mark.asyncio
    async def test_llm_failure_graceful(self) -> None:
        """LLM exception → return empty list."""
        with patch("aspire_orchestrator.providers.pandadoc_client.resolve_openai_api_key", return_value="test-key"):
            with patch("aspire_orchestrator.providers.pandadoc_client.generate_text_async",
                       new_callable=AsyncMock, side_effect=Exception("API down")):
                result = await _build_content_placeholders(
                    template_placeholders=[{"uuid": "ph-1", "name": "scope"}],
                    terms={"scope": "test"},
                    parties=[],
                )
        assert result == []
