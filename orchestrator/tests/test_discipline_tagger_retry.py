"""Tests for the LLM batch-drop retry layer (Wave 17b.1).

Mocks the OpenAI client to simulate the omission pattern observed on GAVNN
project 5b0adf45 where the model occasionally drops a sheet from its batched
JSON response. The retry layer must:
  1. Detect the omission.
  2. Issue ONE retry containing only the missing sheets.
  3. If retry STILL omits, write the missing_input row exactly once.
  4. Never loop more than twice (initial + 1 retry).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.services.blueprint.discipline_tagger import (
    tag_disciplines,
)


def _sheet(sheet_id: str, sheet_number: str, ocr: str = "") -> dict[str, Any]:
    # Provide enough opaque OCR that the atlas Tier-3 density check does NOT
    # fire. We want every test sheet to fall through to the LLM.
    return {
        "id": sheet_id,
        "sheet_number": sheet_number,
        "sheet_code": None,
        "sheet_title": None,
        "ocr_text": ocr or ("filler text " * 100),
    }


def _llm_result(text: str) -> dict[str, Any]:
    return {
        "text": text,
        "usage": {
            "prompt_tokens": 100,
            "cached_tokens": 0,
            "completion_tokens": 50,
            "prompt_prefix_hash": "deadbeef",
        },
    }


@pytest.fixture(autouse=True)
def _disable_cache(monkeypatch):
    """Force the Redis/LRU cache to miss so the LLM mock is always called."""
    async def _miss(*a, **k):
        return None
    async def _set(*a, **k):
        return None
    monkeypatch.setattr(
        "aspire_orchestrator.services.blueprint.discipline_tagger.get_cached_llm_response",
        _miss,
    )
    monkeypatch.setattr(
        "aspire_orchestrator.services.blueprint.discipline_tagger.set_cached_llm_response",
        _set,
    )


@pytest.fixture(autouse=True)
def _stub_openai_key(monkeypatch):
    monkeypatch.setattr(
        "aspire_orchestrator.services.blueprint.discipline_tagger.resolve_openai_api_key",
        lambda: "sk-test",
    )


class TestRetryFiresOnce:
    def test_omission_first_call_retry_succeeds(self):
        """LLM omits sheet B on first call; retry returns it; final tag is good."""
        sheets = [
            _sheet("aaa", "1"),
            _sheet("bbb", "2"),
            _sheet("ccc", "3"),
        ]

        # First call: response omits "bbb"
        first_text = json.dumps([
            {"sheet_id": "aaa", "discipline": "E", "confidence": 0.9, "reasoning": "test"},
            {"sheet_id": "ccc", "discipline": "P", "confidence": 0.9, "reasoning": "test"},
        ])
        # Retry call: returns the missing sheet
        retry_text = json.dumps([
            {"sheet_id": "bbb", "discipline": "A", "confidence": 0.85, "reasoning": "retry"},
        ])

        mock_llm = AsyncMock(side_effect=[_llm_result(first_text), _llm_result(retry_text)])
        with patch(
            "aspire_orchestrator.services.openai_client.generate_text_async",
            mock_llm,
        ):
            tags = asyncio.new_event_loop().run_until_complete(
                tag_disciplines(sheets, model="gpt-test", correlation_id="corr-1234")
            )

        by_id = {t.sheet_id: t for t in tags}
        assert by_id["aaa"].discipline == "E"
        assert by_id["bbb"].discipline == "A"
        assert by_id["bbb"].source == "llm_retry"
        assert by_id["bbb"].needs_review is False
        assert by_id["ccc"].discipline == "P"

        # Exactly two LLM calls (initial + 1 retry)
        assert mock_llm.await_count == 2

    def test_omission_both_calls_marks_dropped(self):
        """LLM omits sheet B on initial AND retry calls — single missing_input."""
        sheets = [
            _sheet("aaa", "1"),
            _sheet("bbb", "2"),
        ]

        first_text = json.dumps([
            {"sheet_id": "aaa", "discipline": "E", "confidence": 0.9, "reasoning": "ok"},
        ])
        # Retry also omits bbb
        retry_text = json.dumps([])

        mock_llm = AsyncMock(side_effect=[_llm_result(first_text), _llm_result(retry_text)])
        with patch(
            "aspire_orchestrator.services.openai_client.generate_text_async",
            mock_llm,
        ):
            tags = asyncio.new_event_loop().run_until_complete(
                tag_disciplines(sheets, model="gpt-test", correlation_id="corr-2222")
            )

        by_id = {t.sheet_id: t for t in tags}
        assert by_id["aaa"].discipline == "E"
        assert by_id["bbb"].discipline is None
        assert by_id["bbb"].confidence == 0.0
        assert by_id["bbb"].source == "llm_dropped"
        assert by_id["bbb"].needs_review is True
        assert "retry" in by_id["bbb"].reasoning.lower()

        # Exactly one retry, no more.
        assert mock_llm.await_count == 2

    def test_no_omission_no_retry(self):
        """When LLM returns all sheets cleanly, no retry fires."""
        sheets = [
            _sheet("aaa", "1"),
            _sheet("bbb", "2"),
        ]

        text = json.dumps([
            {"sheet_id": "aaa", "discipline": "E", "confidence": 0.9, "reasoning": "ok"},
            {"sheet_id": "bbb", "discipline": "P", "confidence": 0.9, "reasoning": "ok"},
        ])
        mock_llm = AsyncMock(side_effect=[_llm_result(text)])
        with patch(
            "aspire_orchestrator.services.openai_client.generate_text_async",
            mock_llm,
        ):
            tags = asyncio.new_event_loop().run_until_complete(
                tag_disciplines(sheets, model="gpt-test", correlation_id="corr-3333")
            )

        assert all(t.discipline is not None for t in tags)
        assert mock_llm.await_count == 1


class TestAtlasShortCircuitsLLM:
    def test_atlas_matched_sheets_skip_llm(self):
        """Sheets the atlas can classify are NEVER sent to the LLM."""
        sheets = [
            {
                "id": "atlas-1",
                "sheet_number": "1",
                "sheet_code": "E-1",
                "sheet_title": None,
                "ocr_text": "x" * 400,
            },
            {
                "id": "atlas-2",
                "sheet_number": "2",
                "sheet_code": "P-1",
                "sheet_title": None,
                "ocr_text": "x" * 400,
            },
            # LLM-required (no sheet_code, no title-block phrase)
            _sheet("llm-1", "3"),
        ]

        llm_text = json.dumps([
            {"sheet_id": "llm-1", "discipline": "A", "confidence": 0.85, "reasoning": "ok"},
        ])
        mock_llm = AsyncMock(side_effect=[_llm_result(llm_text)])
        with patch(
            "aspire_orchestrator.services.openai_client.generate_text_async",
            mock_llm,
        ):
            tags = asyncio.new_event_loop().run_until_complete(
                tag_disciplines(sheets, model="gpt-test", correlation_id="corr-atlas")
            )

        by_id = {t.sheet_id: t for t in tags}
        assert by_id["atlas-1"].source == "atlas"
        assert by_id["atlas-1"].discipline == "E"
        assert by_id["atlas-2"].source == "atlas"
        assert by_id["atlas-2"].discipline == "P"
        assert by_id["llm-1"].source in ("llm", "llm_retry")
        # LLM was called exactly once, with only the one Tier-4 sheet
        assert mock_llm.await_count == 1

    def test_all_atlas_no_llm_call(self):
        """If atlas covers every sheet, LLM is never invoked."""
        sheets = [
            {"id": f"sid-{i}", "sheet_number": str(i), "sheet_code": f"E-{i}",
             "sheet_title": None, "ocr_text": "x" * 200}
            for i in range(1, 6)
        ]
        mock_llm = AsyncMock()
        with patch(
            "aspire_orchestrator.services.openai_client.generate_text_async",
            mock_llm,
        ):
            tags = asyncio.new_event_loop().run_until_complete(
                tag_disciplines(sheets, model="gpt-test", correlation_id="corr-all-atlas")
            )

        assert len(tags) == 5
        assert all(t.source == "atlas" for t in tags)
        mock_llm.assert_not_awaited()
