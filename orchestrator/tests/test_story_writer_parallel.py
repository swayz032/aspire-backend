"""Tests for story_writer parallel LLM dispatch — Wave 9l.

Asserts:
  1. write_story dispatches 3 parallel LLM calls (A, B, C) and merges results.
  2. _merge_parallel_responses correctly combines A/B/C into the full schema shape.
  3. When all 3 Redis cache keys hit, no generate_json_async calls are made.
  4. Fail-closed: if any of the 3 parallel calls raises, write_story raises RuntimeError.

Law compliance:
  Law #3: failure in any call → RuntimeError (fail-closed).
  Law #2: merged response passes through _parse_llm_response without dropping receipts.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

PROJECT_ID = str(uuid.uuid4())
SUITE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CORR_ID = "test-parallel-story-001"
MODEL = "gpt-5.4-mini"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Helper responses
# ---------------------------------------------------------------------------

def _make_resp_a() -> dict[str, Any]:
    """Minimal valid Call A response (assemblies + quantities)."""
    return {
        "phases": [
            {
                "phase_number": 1,
                "phase_name": "Foundation",
                "assemblies": [
                    {"type": "footing", "quantity": 4.0, "unit": "each",
                     "truth": "observed", "confidence": 0.92, "tariff_flag": None},
                ],
                "material_lines": [
                    {"line_item": "3000 PSI concrete", "quantity": 12.0, "unit": "CY",
                     "truth": "derived", "confidence": 0.88, "tariff_flag": None},
                ],
            }
        ]
    }


def _make_resp_b() -> dict[str, Any]:
    """Minimal valid Call B response (missing_inputs + risk_flags)."""
    return {
        "missing_inputs": [
            {"description": "Foundation depth not specified", "suggested_resolution": "Confirm from geotechnical report"},
        ],
        "risk_flags": ["Unclear bearing capacity"],
    }


def _make_resp_c() -> dict[str, Any]:
    """Minimal valid Call C response (narrative + truth distribution)."""
    return {
        "phases": [
            {
                "phase_number": 1,
                "phase_name": "Foundation",
                "markdown": "## Foundation\n\nStandard spread footing system noted on sheet S1.",
            }
        ],
        "truth_distribution": {"observed": 2, "derived": 1, "assumed": 0},
        "mean_confidence": 0.90,
    }


# ---------------------------------------------------------------------------
# Test _merge_parallel_responses directly
# ---------------------------------------------------------------------------

class TestMergeParallelResponses:
    """Unit tests for the merge helper — no LLM calls needed."""

    def test_merge_combines_assemblies_and_markdown(self) -> None:
        from aspire_orchestrator.services.blueprint.story_writer import _merge_parallel_responses

        merged = _merge_parallel_responses(_make_resp_a(), _make_resp_b(), _make_resp_c())

        assert merged.get("phases"), "Merged response must contain phases"
        phase = merged["phases"][0]
        assert phase["phase_number"] == 1
        assert phase["phase_name"] == "Foundation"
        assert "Foundation" in phase["markdown"]
        assert len(phase["assemblies"]) == 1
        assert phase["assemblies"][0]["type"] == "footing"
        assert len(phase["material_lines"]) == 1
        assert phase["material_lines"][0]["line_item"] == "3000 PSI concrete"

    def test_merge_includes_missing_inputs(self) -> None:
        from aspire_orchestrator.services.blueprint.story_writer import _merge_parallel_responses

        merged = _merge_parallel_responses(_make_resp_a(), _make_resp_b(), _make_resp_c())

        assert len(merged.get("missing_inputs", [])) == 1
        assert "Foundation depth" in merged["missing_inputs"][0]["description"]

    def test_merge_preserves_truth_distribution(self) -> None:
        from aspire_orchestrator.services.blueprint.story_writer import _merge_parallel_responses

        merged = _merge_parallel_responses(_make_resp_a(), _make_resp_b(), _make_resp_c())

        assert merged.get("truth_distribution") == {"observed": 2, "derived": 1, "assumed": 0}
        assert merged.get("mean_confidence") == pytest.approx(0.90)

    def test_merge_empty_responses_produce_empty_phases(self) -> None:
        """Edge case: all 3 calls return empty — merge must return phases=[] not crash."""
        from aspire_orchestrator.services.blueprint.story_writer import _merge_parallel_responses

        merged = _merge_parallel_responses({}, {}, {})

        assert merged.get("phases") == []
        assert merged.get("missing_inputs") == []


# ---------------------------------------------------------------------------
# Test 3 parallel dispatches via write_story
# ---------------------------------------------------------------------------

class TestWriteStoryParallelDispatch:
    """Assert that write_story dispatches 3 parallel calls and merges results."""

    def _make_sheets(self) -> list[dict]:
        return [{
            "id": str(uuid.uuid4()),
            "suite_id": SUITE_ID,
            "project_id": PROJECT_ID,
            "ocr_text": "SCALE: 1/4\" = 1'-0\"\nFoundation notes here.",
            "sheet_number": "S1",
            "discipline": "S",
            "seal_detected": False,
            "supersedes_id": None,
            "revision": "0",
            "scale": "1/4\"=1'-0\"",
            "created_at": "2026-05-19T00:00:00Z",
        }]

    def test_three_parallel_calls_dispatched(self) -> None:
        """generate_json_async should be called exactly 3 times (once per parallel call)."""
        from aspire_orchestrator.services.blueprint import story_writer

        sheets = self._make_sheets()
        call_count = 0
        call_ids_seen: list[str] = []

        async def _fake_generate(*, model, messages, api_key, base_url,
                                 timeout_seconds, max_output_tokens,
                                 temperature, model_profile) -> dict:
            nonlocal call_count
            call_count += 1
            task_field = messages[-1]["content"]
            if "ASSEMBLIES_AND_QUANTITIES" in task_field:
                return {"json": _make_resp_a(), "usage": {}}
            elif "MISSING_INPUTS_AND_RISKS" in task_field:
                return {"json": _make_resp_b(), "usage": {}}
            elif "PHASED_NARRATIVE" in task_field:
                return {"json": _make_resp_c(), "usage": {}}
            return {"json": {}, "usage": {}}

        with patch.object(story_writer, "generate_json_async", new=_fake_generate), \
             patch.object(story_writer, "supabase_select", new=AsyncMock(side_effect=[
                 sheets,                   # active sheets
                 [],                       # symbols sheet 1
                 [],                       # existing missing_inputs
             ])), \
             patch.object(story_writer, "supabase_insert", new=AsyncMock(return_value={})), \
             patch(
                 "aspire_orchestrator.services.blueprint.story_writer.get_cached_llm_response",
                 new=AsyncMock(return_value=None),   # no cache hit
             ), \
             patch(
                 "aspire_orchestrator.services.blueprint.story_writer.set_cached_llm_response",
                 new=AsyncMock(return_value=None),
             ), \
             patch(
                 "aspire_orchestrator.services.blueprint.story_writer.compute_content_hash",
                 side_effect=lambda x: f"hash_{x[:8]}",
             ), \
             patch(
                 "aspire_orchestrator.services.blueprint.story_writer.retrieve_case_pack_hints",
                 new=AsyncMock(return_value=[]),
             ):
            result = _run(
                story_writer.write_story(
                    PROJECT_ID,
                    suite_id=SUITE_ID,
                    office_id=None,
                    correlation_id=CORR_ID,
                    model=MODEL,
                )
            )

        assert call_count == 3, (
            f"Expected exactly 3 parallel LLM calls (A, B, C), got {call_count}"
        )
        # StoryOutput returned successfully.
        assert result.project_id == PROJECT_ID

    def test_all_cache_hits_skip_generate_calls(self) -> None:
        """When all 3 Redis cache keys hit, generate_json_async is NOT called."""
        from aspire_orchestrator.services.blueprint import story_writer

        sheets = self._make_sheets()
        generate_called = False

        async def _should_not_call(**kwargs):
            nonlocal generate_called
            generate_called = True
            raise AssertionError("generate_json_async should not be called when all cache keys hit")

        with patch.object(story_writer, "generate_json_async", new=_should_not_call), \
             patch.object(story_writer, "supabase_select", new=AsyncMock(side_effect=[
                 sheets,
                 [],
                 [],
             ])), \
             patch.object(story_writer, "supabase_insert", new=AsyncMock(return_value={})), \
             patch(
                 "aspire_orchestrator.services.blueprint.story_writer.get_cached_llm_response",
                 # All 3 calls return cached data.
                 new=AsyncMock(side_effect=[_make_resp_a(), _make_resp_b(), _make_resp_c()]),
             ), \
             patch(
                 "aspire_orchestrator.services.blueprint.story_writer.set_cached_llm_response",
                 new=AsyncMock(return_value=None),
             ), \
             patch(
                 "aspire_orchestrator.services.blueprint.story_writer.compute_content_hash",
                 side_effect=lambda x: f"hash_{x[:8]}",
             ), \
             patch(
                 "aspire_orchestrator.services.blueprint.story_writer.retrieve_case_pack_hints",
                 new=AsyncMock(return_value=[]),
             ):
            result = _run(
                story_writer.write_story(
                    PROJECT_ID,
                    suite_id=SUITE_ID,
                    office_id=None,
                    correlation_id=CORR_ID,
                    model=MODEL,
                )
            )

        assert not generate_called, "generate_json_async must not be called when all cache keys hit"
        assert result.project_id == PROJECT_ID

    def test_parallel_call_failure_raises_runtime_error(self) -> None:
        """If any parallel call raises, write_story raises RuntimeError (Law #3 fail-closed)."""
        from aspire_orchestrator.services.blueprint import story_writer

        sheets = self._make_sheets()

        async def _call_c_fails(*, model, messages, api_key, base_url,
                                timeout_seconds, max_output_tokens,
                                temperature, model_profile) -> dict:
            task_field = messages[-1]["content"]
            if "ASSEMBLIES_AND_QUANTITIES" in task_field:
                return {"json": _make_resp_a(), "usage": {}}
            elif "MISSING_INPUTS_AND_RISKS" in task_field:
                return {"json": _make_resp_b(), "usage": {}}
            elif "PHASED_NARRATIVE" in task_field:
                raise RuntimeError("Simulated Call C failure")
            return {"json": {}, "usage": {}}

        with patch.object(story_writer, "generate_json_async", new=_call_c_fails), \
             patch.object(story_writer, "supabase_select", new=AsyncMock(side_effect=[
                 sheets,
                 [],
                 [],
             ])), \
             patch.object(story_writer, "supabase_insert", new=AsyncMock(return_value={})), \
             patch(
                 "aspire_orchestrator.services.blueprint.story_writer.get_cached_llm_response",
                 new=AsyncMock(return_value=None),
             ), \
             patch(
                 "aspire_orchestrator.services.blueprint.story_writer.set_cached_llm_response",
                 new=AsyncMock(return_value=None),
             ), \
             patch(
                 "aspire_orchestrator.services.blueprint.story_writer.compute_content_hash",
                 side_effect=lambda x: f"hash_{x[:8]}",
             ), \
             patch(
                 "aspire_orchestrator.services.blueprint.story_writer.retrieve_case_pack_hints",
                 new=AsyncMock(return_value=[]),
             ):
            with pytest.raises(RuntimeError, match="REASON"):
                _run(
                    story_writer.write_story(
                        PROJECT_ID,
                        suite_id=SUITE_ID,
                        office_id=None,
                        correlation_id=CORR_ID,
                        model=MODEL,
                    )
                )
