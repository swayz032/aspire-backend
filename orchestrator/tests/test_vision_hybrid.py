"""Tests for Wave 19.2.1 hybrid Azure-layout + Vision interpretation.

Regresses the Gulfstream sheet-39 failure: PNG-only vision returned
"unclear" while the sheet literally contained ``NET TOTAL: 33`` in a
sprinkler legend table. The Azure layout pre-pass extracts that cell
and feeds it to the vision call as structured context.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCandidate,
    MissingInputCategory,
)
from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
    VisionCallResult,
    compact_azure_layout,
)
from aspire_orchestrator.services.blueprint.vision_budget import VisionBudget


_SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
_PROJECT = "5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2"


def _candidate(
    *,
    category: MissingInputCategory,
    description: str,
    sheets: list[dict[str, Any]],
    provider: Any | None = None,
    budget: Any | None = None,
) -> MissingInputCandidate:
    return MissingInputCandidate(
        missing_input_id="aaaaaaaa-0000-0000-0000-000000000001",
        project_id=_PROJECT, suite_id=_SUITE,
        office_id=None,
        description=description, suggested_resolution=None,
        category=category,
        sheets=sheets,
        symbols_by_sheet={}, ocr_by_sheet={},
        data_provider=provider,
        vision_budget=budget,
    )


def _provider_with_png(png: bytes = b"\x89PNG_FAKE") -> Any:
    p = MagicMock()
    p.get_sheet_png = AsyncMock(return_value=png)
    p.get_yolo_detections = AsyncMock(return_value=[])
    return p


def _gulfstream_sprinkler_layout() -> dict[str, Any]:
    """Approximation of the Gulfstream sheet-39 sprinkler legend Azure layout."""
    return {
        "tables": [{
            "rowCount": 3,
            "columnCount": 9,
            "cells": [
                # Header row
                {"rowIndex": 0, "columnIndex": 0, "content": "SYMBOL"},
                {"rowIndex": 0, "columnIndex": 1, "content": "MANUF."},
                {"rowIndex": 0, "columnIndex": 2, "content": "TYPE"},
                {"rowIndex": 0, "columnIndex": 8, "content": "NET TOTAL"},
                # Data row 1
                {"rowIndex": 1, "columnIndex": 0, "content": "(O)"},
                {"rowIndex": 1, "columnIndex": 1, "content": "VICTAULIC"},
                {"rowIndex": 1, "columnIndex": 2, "content": "QR-PENDANT"},
                {"rowIndex": 1, "columnIndex": 8, "content": "33"},
                # Data row 2 — the note
                {"rowIndex": 2, "columnIndex": 0,
                 "content": "REMOVED 31 UPRIGHT SPRINKLERS"},
                {"rowIndex": 2, "columnIndex": 8, "content": "+2"},
            ],
        }],
        "paragraphs": [
            {"content": "SPRINKLER LEGEND"},
            {"content": "FIRE PROTECTION PLAN"},
        ],
    }


# ===========================================================================
# compact_azure_layout — token-economical layout summary
# ===========================================================================


class TestCompactAzureLayout:
    def test_returns_empty_for_none_or_non_dict(self) -> None:
        assert compact_azure_layout(None) == {}
        assert compact_azure_layout("not a dict") == {}  # type: ignore[arg-type]
        assert compact_azure_layout([]) == {}  # type: ignore[arg-type]

    def test_strips_coordinates_keeps_cell_content(self) -> None:
        raw = {
            "tables": [{
                "rowCount": 2, "columnCount": 2,
                "cells": [
                    {"rowIndex": 0, "columnIndex": 0, "content": "Header",
                     "boundingRegions": [{"x": 100}],  # must be stripped
                     "spans": [{"offset": 0, "length": 6}],
                    },
                    {"rowIndex": 1, "columnIndex": 0, "content": "Value"},
                ],
            }],
            "paragraphs": [],
        }
        out = compact_azure_layout(raw)
        first_cell = out["tables"][0]["cells"][0]
        assert "boundingRegions" not in first_cell
        assert "spans" not in first_cell
        assert first_cell["t"] == "Header"
        assert first_cell["r"] == 0
        assert first_cell["c"] == 0

    def test_truncates_long_cell_text(self) -> None:
        raw = {
            "tables": [{
                "rowCount": 1, "columnCount": 1,
                "cells": [{"rowIndex": 0, "columnIndex": 0,
                           "content": "x" * 500}],
            }],
        }
        out = compact_azure_layout(raw)
        assert len(out["tables"][0]["cells"][0]["t"]) == 80

    def test_caps_table_count(self) -> None:
        """Sheets with 50 tables would blow the prompt budget."""
        raw = {
            "tables": [
                {"rowCount": 1, "columnCount": 1,
                 "cells": [{"rowIndex": 0, "columnIndex": 0,
                            "content": f"t{i}"}]}
                for i in range(50)
            ],
        }
        out = compact_azure_layout(raw)
        # Should be capped at the module-level limit (12).
        assert len(out["tables"]) <= 12
        # But reports total in source so receipt can flag a truncated view.
        assert out["tables_total_in_source"] == 50


# ===========================================================================
# call_vision_with_budget — extracted_layout injection
# ===========================================================================


class TestExtractedLayoutInjection:
    @pytest.mark.asyncio
    async def test_layout_appears_in_prompt_when_provided(self) -> None:
        """The Azure layout JSON is injected as an input_text BEFORE the image."""
        from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
            call_vision_with_budget,
        )

        captured_messages: list[Any] = []

        async def _fake_generate_json_async(*, messages: list[Any], **_kwargs: Any) -> Any:
            captured_messages.append(messages)
            return {
                "json": {"answer": "33 heads", "confidence": 0.9},
                "usage": {"prompt_tokens": 1200, "completion_tokens": 50},
            }

        with patch(
            "aspire_orchestrator.services.openai_client.generate_json_async",
            new=_fake_generate_json_async,
        ), patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "sk-test"},
        ):
            await call_vision_with_budget(
                png_bytes=b"\x89PNG_FAKE",
                system_prompt="sys",
                user_prompt="q",
                vision_budget=VisionBudget(cap_usd=1.0),
                extracted_layout=_gulfstream_sprinkler_layout(),
            )

        assert len(captured_messages) == 1
        user_msg = captured_messages[0][1]
        assert user_msg["role"] == "user"
        content = user_msg["content"]
        # Order: layout-text, image, user-text-question
        assert content[0]["type"] == "input_text"
        assert "Azure Document Intelligence" in content[0]["text"]
        # The sprinkler legend cell text must be in the prompt.
        assert "NET TOTAL" in content[0]["text"]
        assert "33" in content[0]["text"]
        assert content[1]["type"] == "input_image"
        assert content[2]["type"] == "input_text"

    @pytest.mark.asyncio
    async def test_no_layout_block_when_none_provided(self) -> None:
        """Back-compat: extracted_layout=None → image + text only, no layout block."""
        from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
            call_vision_with_budget,
        )

        captured: list[Any] = []

        async def _fake(*, messages: list[Any], **_kw: Any) -> Any:
            captured.append(messages)
            return {"json": {"answer": "x", "confidence": 0.5},
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10}}

        with patch(
            "aspire_orchestrator.services.openai_client.generate_json_async",
            new=_fake,
        ), patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            await call_vision_with_budget(
                png_bytes=b"PNG", system_prompt="sys", user_prompt="q",
                vision_budget=VisionBudget(cap_usd=1.0),
                # extracted_layout omitted
            )

        content = captured[0][1]["content"]
        # Two items only: image, user-text. No layout-text prepended.
        assert len(content) == 2
        assert content[0]["type"] == "input_image"
        assert content[1]["type"] == "input_text"


# ===========================================================================
# Token accounting — confirms the budget actually records non-zero usage
# ===========================================================================


class TestTokenAccounting:
    @pytest.mark.asyncio
    async def test_usage_block_flows_to_budget_record_call(self) -> None:
        """Wave 19.2.1c regression test: token counts actually populate.

        Production receipts in Wave 19.2 showed ``vision_input_tokens: 0``
        even after vision calls fired. Mock the OpenAI call to return a
        UsageDict with non-zero token counts and assert the budget
        records them.
        """
        from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
            call_vision_with_budget,
        )

        budget = VisionBudget(cap_usd=1.00)

        async def _fake(**_kw: Any) -> Any:
            return {
                "json": {"answer": "test", "confidence": 0.9},
                "usage": {
                    "prompt_tokens": 1500,
                    "completion_tokens": 200,
                    "cached_tokens": 0,
                    "cache_hit_ratio": 0.0,
                    "total_tokens": 1700,
                    "model": "gpt-5.4-mini",
                    "latency_ms": 1234.0,
                    "prompt_prefix_hash": "abc",
                },
            }

        with patch(
            "aspire_orchestrator.services.openai_client.generate_json_async",
            new=_fake,
        ), patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result = await call_vision_with_budget(
                png_bytes=b"PNG", system_prompt="sys", user_prompt="q",
                vision_budget=budget,
            )

        assert result.ok is True
        assert result.input_tokens == 1500
        assert result.output_tokens == 200
        # PROOF that the bug from Wave 19.2 is fixed: budget actually
        # increments instead of staying at zero.
        assert budget.tokens_input_used == 1500
        assert budget.tokens_output_used == 200
        assert budget.calls_made == 1
        # Cost recorded too: 1500/1000 * 0.000150 + 200/1000 * 0.000600
        # = 0.000225 + 0.000120 = 0.000345
        assert budget.cost_usd_spent == pytest.approx(0.000345)


# ===========================================================================
# run_azure_layout_pre_pass — fail-closed semantics
# ===========================================================================


class TestAzurePrePass:
    @pytest.mark.asyncio
    async def test_returns_none_for_missing_png(self) -> None:
        from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
            run_azure_layout_pre_pass,
        )
        result = await run_azure_layout_pre_pass(
            png_bytes=None, suite_id=_SUITE, office_id="",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_analyze_result_on_success(self) -> None:
        from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
            run_azure_layout_pre_pass,
        )
        layout = _gulfstream_sprinkler_layout()
        fake_client = MagicMock()
        fake_response = MagicMock()
        fake_response.success = True
        fake_response.body = {"analyzeResult": layout}
        fake_client.analyze_layout = AsyncMock(return_value=fake_response)

        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.get_azure_doc_intel_client",
            return_value=fake_client,
        ):
            result = await run_azure_layout_pre_pass(
                png_bytes=b"PNG", suite_id=_SUITE, office_id="",
            )

        assert result == layout

    @pytest.mark.asyncio
    async def test_returns_none_when_azure_unsuccessful(self) -> None:
        """Azure 429 or 500 → degrade silently. Vision call still runs without context."""
        from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
            run_azure_layout_pre_pass,
        )
        fake_client = MagicMock()
        fake_response = MagicMock()
        fake_response.success = False
        fake_response.status_code = 429
        fake_response.error_code = MagicMock(value="RATE_LIMITED")
        fake_client.analyze_layout = AsyncMock(return_value=fake_response)

        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.get_azure_doc_intel_client",
            return_value=fake_client,
        ):
            result = await run_azure_layout_pre_pass(
                png_bytes=b"PNG", suite_id=_SUITE, office_id="",
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_client_construction_raises(self) -> None:
        from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
            run_azure_layout_pre_pass,
        )
        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.get_azure_doc_intel_client",
            side_effect=RuntimeError("missing endpoint"),
        ):
            result = await run_azure_layout_pre_pass(
                png_bytes=b"PNG", suite_id=_SUITE, office_id="",
            )
        assert result is None


# ===========================================================================
# GeneralVisionResolver._method_azure_layout_only
# ===========================================================================


class TestAzureLayoutOnlyFallback:
    @pytest.mark.asyncio
    async def test_extracts_sprinkler_count_from_gulfstream_layout(self) -> None:
        """The headline regression: Gulfstream sheet 39 layout → 33 heads extracted."""
        from aspire_orchestrator.services.blueprint.resolvers.general_vision_resolver import (
            GeneralVisionResolver,
        )
        resolver = GeneralVisionResolver()
        cand = _candidate(
            category=MissingInputCategory.GENERAL_VISION,
            description=(
                "Fire protection plan: exact sprinkler head count and reroute "
                "extent are not fully legible. Sheet FP-101."
            ),
            sheets=[{"id": "fp101", "sheet_code": "FP-101"}],
            provider=_provider_with_png(),
            budget=VisionBudget(cap_usd=1.00),
        )

        result = resolver._method_azure_layout_only(
            cand, azure_layout=_gulfstream_sprinkler_layout(),
        )
        assert result.method == "azure_layout_only"
        assert result.resolved is True
        assert result.confidence == pytest.approx(0.85)
        ev = result.evidence
        assert ev["azure_table_keyword_hits"] >= 1
        assert ev["azure_tables_with_keyword_numeric_match"] == 1
        assert ev["extracted_facts_count"] >= 1
        # The fact list must surface "33" (the net total).
        facts = result.value["facts"]  # type: ignore[index]
        values = [f["value"] for f in facts]
        assert any("33" in v for v in values), (
            f"expected '33' in extracted facts but got {values}"
        )

    @pytest.mark.asyncio
    async def test_no_false_positive_when_no_keyword_match(self) -> None:
        """Tables with numbers but no question keyword → must NOT resolve."""
        from aspire_orchestrator.services.blueprint.resolvers.general_vision_resolver import (
            GeneralVisionResolver,
        )
        resolver = GeneralVisionResolver()
        # Description asks about sprinklers but layout contains only an
        # unrelated electrical-panel table.
        unrelated_layout = {
            "tables": [{
                "rowCount": 2, "columnCount": 2,
                "cells": [
                    {"rowIndex": 0, "columnIndex": 0, "content": "PANEL"},
                    {"rowIndex": 0, "columnIndex": 1, "content": "20A"},
                    {"rowIndex": 1, "columnIndex": 0, "content": "P-1"},
                    {"rowIndex": 1, "columnIndex": 1, "content": "200"},
                ],
            }],
        }
        cand = _candidate(
            category=MissingInputCategory.GENERAL_VISION,
            description="sprinkler head count",
            sheets=[{"id": "s1", "sheet_code": "FP-101"}],
            provider=_provider_with_png(),
            budget=VisionBudget(cap_usd=1.0),
        )
        result = resolver._method_azure_layout_only(cand, azure_layout=unrelated_layout)
        assert result.resolved is False
        assert result.confidence == 0.0
        assert result.evidence["skip_reason"] in (
            "no_keyword_match_in_tables", "no_keyword_numeric_match",
        )

    @pytest.mark.asyncio
    async def test_empty_layout_returns_unavailable_skip(self) -> None:
        from aspire_orchestrator.services.blueprint.resolvers.general_vision_resolver import (
            GeneralVisionResolver,
        )
        resolver = GeneralVisionResolver()
        cand = _candidate(
            category=MissingInputCategory.GENERAL_VISION,
            description="sprinkler count",
            sheets=[{"id": "s1", "sheet_code": "FP"}],
            provider=_provider_with_png(),
            budget=VisionBudget(cap_usd=1.0),
        )
        result = resolver._method_azure_layout_only(cand, azure_layout=None)
        assert result.evidence["skip_reason"] == "azure_layout_unavailable_or_empty"


# ===========================================================================
# resolve() orchestration — Azure pre-pass runs ONCE per cascade
# ===========================================================================


class TestResolveOrchestration:
    @pytest.mark.asyncio
    async def test_azure_pre_pass_called_once_then_shared(self) -> None:
        """resolve() must call run_azure_layout_pre_pass ONCE and reuse the result."""
        from aspire_orchestrator.services.blueprint.resolvers.general_vision_resolver import (
            GeneralVisionResolver,
        )
        resolver = GeneralVisionResolver()
        cand = _candidate(
            category=MissingInputCategory.GENERAL_VISION,
            description="sprinkler head count Sheet FP-101",
            sheets=[{"id": "fp101", "sheet_code": "FP-101"}],
            provider=_provider_with_png(),
            budget=VisionBudget(cap_usd=1.0),
        )

        layout = _gulfstream_sprinkler_layout()
        azure_pre_pass_mock = AsyncMock(return_value=layout)
        vision_mock = AsyncMock(return_value=VisionCallResult(
            ok=True,
            json_payload={
                "answer": "33 sprinkler heads",
                "extracted_facts": [{"fact_type": "count", "value": "33"}],
                "confidence": 0.90,
                "notes": "extracted from sprinkler legend table",
            },
            input_tokens=1500, output_tokens=200, model="gpt-5.4-mini",
            skip_reason="",
        ))

        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.vision_helper."
            "run_azure_layout_pre_pass",
            new=azure_pre_pass_mock,
        ), patch(
            "aspire_orchestrator.services.blueprint.resolvers.vision_helper."
            "call_vision_with_budget",
            new=vision_mock,
        ):
            results = await resolver.resolve(cand)

        # Azure pre-pass called exactly once for the batch (not once per method).
        assert azure_pre_pass_mock.await_count == 1
        # Two results: vision + azure-only.
        assert len(results) == 2
        methods = {r.method for r in results}
        assert "vision_unknown_fallback" in methods
        assert "azure_layout_only" in methods
        # Vision call received the layout.
        vision_kwargs = vision_mock.await_args.kwargs
        assert vision_kwargs["extracted_layout"] == layout


# ===========================================================================
# Receipt allow-list survival for new Wave 19.2.1 keys
# ===========================================================================


class TestAzureLayoutEvidenceAllowlist:
    def test_new_evidence_keys_on_allowlist(self) -> None:
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            _safe_evidence_for_receipt,
        )
        ev = {
            "azure_layout_pre_pass_used": True,
            "azure_layout_pre_passes_used": 3,
            "azure_layout_tables_count": 5,
            "azure_table_keyword_hits": 8,
            "azure_tables_with_keyword_numeric_match": 2,
        }
        safe = _safe_evidence_for_receipt(ev)
        for k in ev:
            assert k in safe, (
                f"Wave 19.2.1 evidence key {k!r} dropped by allow-list — "
                f"add it to _RECEIPT_EVIDENCE_KEY_ALLOWLIST."
            )
