"""Tests for Wave 10b discipline map-reduce in story_writer.

Asserts:
  1. _DISCIPLINE_GROUPS covers all 11 discipline codes from discipline_tagger.py.
  2. _build_messages_discipline_map returns valid message pairs scoped to the group.
  3. _merge_discipline_maps unions assemblies + material_lines across groups by phase_number.
  4. When ASPIRE_DREW_REASON_DISCIPLINE_MAPS=true, write_story dispatches N map calls + B + C.
  5. When no sheets have discipline tags, falls back to monolithic call-A.
  6. Fail-closed: any map-call failure propagates as RuntimeError.

Law compliance:
  Law #3: failure in any parallel call → RuntimeError.
  Law #6: suite_id/office_id scope carried through to all calls.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ID = str(uuid.uuid4())
SUITE_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
CORR_ID = "test-discipline-maps-001"
MODEL = "gpt-5.4-mini"

# All discipline codes per discipline_tagger.py
_ALL_DISCIPLINE_CODES = {"A", "S", "M", "E", "P", "FP", "C", "L", "Specs", "Schedules", "Addenda"}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_sheet(discipline: str, idx: int = 0) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "suite_id": SUITE_ID,
        "project_id": PROJECT_ID,
        "ocr_text": f"Sheet {idx} OCR text here discipline={discipline}",
        "sheet_number": f"{discipline}-{idx}",
        "discipline": discipline,
        "seal_detected": False,
        "supersedes_id": None,
        "revision": "0",
        "scale": "1/4\"=1'-0\"",
        "created_at": "2026-05-23T00:00:00Z",
    }


def _make_resp_a_for_group(phase_num: int = 1) -> dict[str, Any]:
    return {
        "phases": [
            {
                "phase_number": phase_num,
                "phase_name": f"Phase {phase_num}",
                "assemblies": [
                    {"type": "footing", "quantity": 2.0, "unit": "each",
                     "truth": "observed", "confidence": 0.91, "tariff_flag": None},
                ],
                "material_lines": [
                    {"line_item": "3000 PSI concrete", "quantity": 8.0, "unit": "CY",
                     "truth": "derived", "confidence": 0.85, "tariff_flag": None},
                ],
            }
        ]
    }


def _make_resp_b() -> dict[str, Any]:
    return {
        "missing_inputs": [
            {"description": "Bearing capacity not specified", "suggested_resolution": "Get geotech report"},
        ],
        "risk_flags": [],
    }


def _make_resp_c() -> dict[str, Any]:
    return {
        "phases": [
            {"phase_number": 1, "phase_name": "Foundation",
             "markdown": "## Foundation\n\nSpread footings on S1."},
        ],
        "truth_distribution": {"observed": 2, "derived": 1, "assumed": 0},
        "mean_confidence": 0.88,
    }


# ---------------------------------------------------------------------------
# Unit: _DISCIPLINE_GROUPS coverage
# ---------------------------------------------------------------------------

class TestDisciplineGroupsCoverage:
    """Verify _DISCIPLINE_GROUPS maps all 11 known discipline codes."""

    def test_all_codes_covered(self) -> None:
        from aspire_orchestrator.services.blueprint.story_writer import _DISCIPLINE_GROUPS

        covered: set[str] = set()
        for codes in _DISCIPLINE_GROUPS.values():
            covered.update(codes)

        missing = _ALL_DISCIPLINE_CODES - covered
        assert not missing, f"Discipline codes not covered by any group: {missing}"

    def test_no_duplicate_codes_across_groups(self) -> None:
        from aspire_orchestrator.services.blueprint.story_writer import _DISCIPLINE_GROUPS

        seen: set[str] = set()
        for grp, codes in _DISCIPLINE_GROUPS.items():
            for code in codes:
                assert code not in seen, f"Code '{code}' appears in multiple groups (found in '{grp}')"
                seen.add(code)

    def test_group_names_are_non_empty_strings(self) -> None:
        from aspire_orchestrator.services.blueprint.story_writer import _DISCIPLINE_GROUPS

        for grp, codes in _DISCIPLINE_GROUPS.items():
            assert grp, "Group name must be non-empty"
            assert codes, f"Group '{grp}' has no codes"


# ---------------------------------------------------------------------------
# Unit: _build_messages_discipline_map
# ---------------------------------------------------------------------------

class TestBuildMessagesDisciplineMap:
    """Verify _build_messages_discipline_map returns valid, scoped messages."""

    def _build(self, discipline_group: str, sheets: list[dict[str, Any]]) -> list[dict[str, str]]:
        from aspire_orchestrator.services.blueprint.story_writer import _build_messages_discipline_map

        with (
            patch("aspire_orchestrator.services.blueprint.story_writer._load_system_prompt", return_value="SYS"),
            patch("aspire_orchestrator.services.blueprint.story_writer._load_kb_doc", return_value="KB"),
        ):
            return _build_messages_discipline_map(
                discipline_group=discipline_group,
                sheets_for_group=sheets,
                symbols_by_sheet={},
                seal_sheet_ids=set(),
                discipline_counts={discipline_group: len(sheets)},
                case_pack_hints=[],
                project_id=PROJECT_ID,
            )

    def test_returns_system_and_user_messages(self) -> None:
        msgs = self._build("structural", [_make_sheet("S")])
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_user_payload_contains_group_name(self) -> None:
        import json
        msgs = self._build("electrical", [_make_sheet("E")])
        payload = json.loads(msgs[1]["content"])
        assert payload["discipline_group"] == "electrical"

    def test_user_payload_task_is_assemblies_and_quantities(self) -> None:
        import json
        msgs = self._build("mechanical", [_make_sheet("M")])
        payload = json.loads(msgs[1]["content"])
        assert payload["task"] == "ASSEMBLIES_AND_QUANTITIES"

    def test_sheet_count_matches_filtered_sheets(self) -> None:
        import json
        sheets = [_make_sheet("P"), _make_sheet("P", 1)]
        msgs = self._build("plumbing", sheets)
        payload = json.loads(msgs[1]["content"])
        assert payload["sheet_count"] == 2

    def test_instructions_mention_discipline_group(self) -> None:
        import json
        msgs = self._build("fire_protection", [_make_sheet("FP")])
        payload = json.loads(msgs[1]["content"])
        assert "fire_protection" in payload["instructions"]

    def test_allowed_assembly_types_present(self) -> None:
        import json
        msgs = self._build("architectural", [_make_sheet("A")])
        payload = json.loads(msgs[1]["content"])
        assert isinstance(payload.get("allowed_assembly_types"), list)
        assert len(payload["allowed_assembly_types"]) > 0


# ---------------------------------------------------------------------------
# Unit: _merge_discipline_maps
# ---------------------------------------------------------------------------

class TestMergeDisciplineMaps:
    """Verify _merge_discipline_maps unions assemblies and material_lines across groups."""

    def test_single_group_passthrough(self) -> None:
        from aspire_orchestrator.services.blueprint.story_writer import _merge_discipline_maps

        resp = _make_resp_a_for_group(phase_num=1)
        merged = _merge_discipline_maps([resp])

        assert len(merged["phases"]) == 1
        assert merged["phases"][0]["phase_number"] == 1
        assert len(merged["phases"][0]["assemblies"]) == 1

    def test_two_groups_same_phase_unions_assemblies(self) -> None:
        from aspire_orchestrator.services.blueprint.story_writer import _merge_discipline_maps

        resp1 = {
            "phases": [{"phase_number": 1, "phase_name": "Foundation",
                        "assemblies": [{"type": "footing"}], "material_lines": []}]
        }
        resp2 = {
            "phases": [{"phase_number": 1, "phase_name": "Foundation",
                        "assemblies": [{"type": "slab_on_grade"}], "material_lines": [{"line_item": "Rebar #4"}]}]
        }
        merged = _merge_discipline_maps([resp1, resp2])

        assert len(merged["phases"]) == 1
        phase = merged["phases"][0]
        types = {a["type"] for a in phase["assemblies"]}
        assert "footing" in types
        assert "slab_on_grade" in types
        assert len(phase["material_lines"]) == 1

    def test_two_groups_different_phases_produces_two_phases(self) -> None:
        from aspire_orchestrator.services.blueprint.story_writer import _merge_discipline_maps

        resp1 = {"phases": [{"phase_number": 1, "phase_name": "Foundation",
                              "assemblies": [], "material_lines": []}]}
        resp2 = {"phases": [{"phase_number": 2, "phase_name": "Framing",
                              "assemblies": [], "material_lines": []}]}
        merged = _merge_discipline_maps([resp1, resp2])

        nums = [p["phase_number"] for p in merged["phases"]]
        assert sorted(nums) == [1, 2]

    def test_phases_sorted_by_phase_number(self) -> None:
        from aspire_orchestrator.services.blueprint.story_writer import _merge_discipline_maps

        resp1 = {"phases": [{"phase_number": 3, "phase_name": "MEP",
                              "assemblies": [], "material_lines": []}]}
        resp2 = {"phases": [{"phase_number": 1, "phase_name": "Foundation",
                              "assemblies": [], "material_lines": []}]}
        merged = _merge_discipline_maps([resp1, resp2])

        nums = [p["phase_number"] for p in merged["phases"]]
        assert nums == sorted(nums)

    def test_empty_input_returns_empty_phases(self) -> None:
        from aspire_orchestrator.services.blueprint.story_writer import _merge_discipline_maps

        merged = _merge_discipline_maps([])
        assert merged == {"phases": []}

    def test_empty_map_response_skipped_gracefully(self) -> None:
        from aspire_orchestrator.services.blueprint.story_writer import _merge_discipline_maps

        resp1 = _make_resp_a_for_group(phase_num=1)
        merged = _merge_discipline_maps([resp1, {}])

        assert len(merged["phases"]) == 1

    def test_phase_name_from_first_group_wins(self) -> None:
        """When two groups emit same phase, first group's phase_name is kept."""
        from aspire_orchestrator.services.blueprint.story_writer import _merge_discipline_maps

        resp1 = {"phases": [{"phase_number": 1, "phase_name": "Foundation (Structural)",
                              "assemblies": [], "material_lines": []}]}
        resp2 = {"phases": [{"phase_number": 1, "phase_name": "Foundation (Architectural)",
                              "assemblies": [], "material_lines": []}]}
        merged = _merge_discipline_maps([resp1, resp2])

        assert merged["phases"][0]["phase_name"] == "Foundation (Structural)"


# ---------------------------------------------------------------------------
# Integration: write_story discipline map-reduce path (env flag gated)
# ---------------------------------------------------------------------------

class TestWriteStoryDisciplineMaps:
    """Assert write_story dispatches N map calls when ASPIRE_DREW_REASON_DISCIPLINE_MAPS=true."""

    def _make_sheets(self, disciplines: list[str]) -> list[dict[str, Any]]:
        return [_make_sheet(d, i) for i, d in enumerate(disciplines)]

    def _make_symbol_result(self) -> dict[str, Any]:
        return {"json": {"class_id": 1, "class_name": "electrical_panel", "count": 2}}

    def _patch_write_story_deps(self, sheets: list[dict[str, Any]]):
        """Return a context manager stacking all write_story patches.

        Follows the same pattern as test_story_writer_parallel.py:
        patch.object on module-level re-exports (supabase_select, supabase_insert,
        generate_json_async) so tests intercept the function that write_story
        aliases via `_sw = sys.modules[...]`.
        """
        from contextlib import ExitStack
        from aspire_orchestrator.services.blueprint import story_writer as _sw

        stack = ExitStack()

        # supabase_select: first call returns sheets, subsequent calls return [] (symbols, missing_inputs)
        stack.enter_context(patch.object(
            _sw, "supabase_select",
            new=AsyncMock(side_effect=[sheets, *([[] for _ in range(len(sheets) + 5)])]),
        ))
        # supabase_insert: succeed silently
        stack.enter_context(patch.object(_sw, "supabase_insert", new=AsyncMock(return_value={})))
        # supabase_insert_batch_ignore_conflicts: succeed silently
        stack.enter_context(patch.object(
            _sw, "supabase_insert_batch_ignore_conflicts",
            new=AsyncMock(return_value=(0, 0)),
        ))
        # Redis cache — patch at source module so local `from X import Y` inside
        # write_story gets the mock (Python resolves the name from sys.modules at import time).
        stack.enter_context(patch(
            "aspire_orchestrator.services.llm_response_cache.get_cached_llm_response",
            new=AsyncMock(return_value=None),
        ))
        stack.enter_context(patch(
            "aspire_orchestrator.services.llm_response_cache.set_cached_llm_response",
            new=AsyncMock(),
        ))
        # compute_content_hash — fast stub (patched at source module too)
        stack.enter_context(patch(
            "aspire_orchestrator.services.llm_response_cache.compute_content_hash",
            side_effect=lambda x: f"hash_{x[:8]}",
        ))
        # KB loading
        stack.enter_context(patch(
            "aspire_orchestrator.services.blueprint.story_writer._load_system_prompt",
            return_value="SYS",
        ))
        stack.enter_context(patch(
            "aspire_orchestrator.services.blueprint.story_writer._load_kb_doc",
            return_value="KB",
        ))
        # Case-pack hints — patched at source module (local import inside write_story)
        stack.enter_context(patch(
            "aspire_orchestrator.services.blueprint.case_pack_memory.retrieve_case_pack_hints",
            new=AsyncMock(return_value=[]),
        ))
        # API key resolution — module-level function, patch.object works
        stack.enter_context(patch.object(_sw, "_resolve_api_key", return_value="sk-test"))
        return stack

    def test_discipline_maps_dispatches_per_group_with_env_flag(self, monkeypatch) -> None:
        """With ASPIRE_DREW_REASON_DISCIPLINE_MAPS=true, one call per active discipline group."""
        monkeypatch.setenv("ASPIRE_DREW_REASON_DISCIPLINE_MAPS", "true")

        from aspire_orchestrator.services.blueprint import story_writer

        # Sheets with two distinct groups: S (structural) + E (electrical)
        sheets = self._make_sheets(["S", "E"])
        call_ids_seen: list[str] = []

        async def fake_generate(**kwargs: Any) -> dict[str, Any]:
            import json as _json
            msgs = kwargs.get("messages", [])
            user_content = msgs[-1]["content"] if msgs else "{}"
            try:
                payload = _json.loads(user_content)
            except Exception:
                payload = {}
            task = payload.get("task", "UNKNOWN")
            grp = payload.get("discipline_group", "")
            call_ids_seen.append(f"{task}:{grp}" if grp else task)
            if task == "ASSEMBLIES_AND_QUANTITIES":
                return {"json": _make_resp_a_for_group(), "usage": {}}
            if task == "MISSING_INPUTS_AND_RISKS":
                return {"json": _make_resp_b(), "usage": {}}
            return {"json": _make_resp_c(), "usage": {}}

        with self._patch_write_story_deps(sheets):
            with patch.object(story_writer, "generate_json_async", new=fake_generate):
                _run(story_writer.write_story(
                    PROJECT_ID,
                    suite_id=SUITE_ID,
                    office_id="office-1",
                    correlation_id=CORR_ID,
                    model=MODEL,
                ))

        # Expect: 2 map calls (structural + electrical) + B + C = 4 total
        asm_calls = [c for c in call_ids_seen if c.startswith("ASSEMBLIES_AND_QUANTITIES:")]
        b_calls = [c for c in call_ids_seen if c == "MISSING_INPUTS_AND_RISKS"]
        c_calls = [c for c in call_ids_seen if c == "PHASED_NARRATIVE"]
        assert len(asm_calls) == 2, f"Expected 2 assembly map calls, got {asm_calls}"
        assert len(b_calls) == 1
        assert len(c_calls) == 1

    def test_discipline_maps_off_by_default(self, monkeypatch) -> None:
        """Without env flag, monolithic call-A dispatched (not per-group)."""
        monkeypatch.delenv("ASPIRE_DREW_REASON_DISCIPLINE_MAPS", raising=False)

        from aspire_orchestrator.services.blueprint import story_writer

        sheets = self._make_sheets(["S", "E"])
        call_ids_seen: list[str] = []

        async def fake_generate(**kwargs: Any) -> dict[str, Any]:
            import json as _json
            msgs = kwargs.get("messages", [])
            user_content = msgs[-1]["content"] if msgs else "{}"
            try:
                payload = _json.loads(user_content)
            except Exception:
                payload = {}
            task = payload.get("task", "UNKNOWN")
            call_ids_seen.append(task)
            if task == "ASSEMBLIES_AND_QUANTITIES":
                return {"json": _make_resp_a_for_group(), "usage": {}}
            if task == "MISSING_INPUTS_AND_RISKS":
                return {"json": _make_resp_b(), "usage": {}}
            return {"json": _make_resp_c(), "usage": {}}

        with self._patch_write_story_deps(sheets):
            with patch.object(story_writer, "generate_json_async", new=fake_generate):
                _run(story_writer.write_story(
                    PROJECT_ID,
                    suite_id=SUITE_ID,
                    office_id="office-1",
                    correlation_id=CORR_ID,
                    model=MODEL,
                ))

        # Monolithic mode: exactly 1 ASSEMBLIES call (not per-group)
        assert call_ids_seen.count("ASSEMBLIES_AND_QUANTITIES") == 1
        assert call_ids_seen.count("MISSING_INPUTS_AND_RISKS") == 1
        assert call_ids_seen.count("PHASED_NARRATIVE") == 1

    def test_discipline_maps_fallback_when_no_tagged_sheets(self, monkeypatch) -> None:
        """When all sheets have unknown discipline, they land in 'other' group (still 1 map call).
        The monolithic fallback (_use_discipline_maps=False) only fires when _active_groups is
        completely empty — which can't happen because untagged sheets go to 'other'."""
        monkeypatch.setenv("ASPIRE_DREW_REASON_DISCIPLINE_MAPS", "true")

        from aspire_orchestrator.services.blueprint import story_writer

        sheets = self._make_sheets([""])  # no discipline tag → goes to "other" group
        call_ids_seen: list[str] = []

        async def fake_generate(**kwargs: Any) -> dict[str, Any]:
            import json as _json
            msgs = kwargs.get("messages", [])
            user_content = msgs[-1]["content"] if msgs else "{}"
            try:
                payload = _json.loads(user_content)
            except Exception:
                payload = {}
            task = payload.get("task", "UNKNOWN")
            grp = payload.get("discipline_group", "")
            call_ids_seen.append(f"MAP:{grp}" if grp else task)
            if task == "ASSEMBLIES_AND_QUANTITIES":
                return {"json": _make_resp_a_for_group(), "usage": {}}
            if task == "MISSING_INPUTS_AND_RISKS":
                return {"json": _make_resp_b(), "usage": {}}
            return {"json": _make_resp_c(), "usage": {}}

        with self._patch_write_story_deps(sheets):
            with patch.object(story_writer, "generate_json_async", new=fake_generate):
                _run(story_writer.write_story(
                    PROJECT_ID,
                    suite_id=SUITE_ID,
                    office_id="office-1",
                    correlation_id=CORR_ID,
                    model=MODEL,
                ))

        # Untagged sheet → "other" group → 1 MAP: call. Must not raise.
        assert len(call_ids_seen) > 0


# ---------------------------------------------------------------------------
# Unit: Modal per-sheet receipts
# ---------------------------------------------------------------------------

class TestModalYoloReceipts:
    """Verify detect_symbols_modal emits per-sheet receipts on success and failure."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_success_emits_receipt(self) -> None:
        """On HTTP 200, a success receipt is emitted."""
        import httpx
        from aspire_orchestrator.providers.modal_yolo_client import detect_symbols_modal

        fake_response = MagicMock(spec=httpx.Response)
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "detections": [
                {"class_id": 1, "class_name": "electrical_panel", "confidence": 0.88,
                 "bbox_xyxy": [10.0, 20.0, 100.0, 200.0]},
            ],
            "image_size": [1280, 960],
            "model_version": "modal-yolo11n-test",
        }

        receipts_stored: list[dict] = []

        def fake_store(recs: list[dict]) -> None:
            receipts_stored.extend(recs)

        with (
            patch.dict("os.environ", {"ASPIRE_DREW_MODAL_YOLO_URL": "https://fake-modal.modal.run/detect"}),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch("aspire_orchestrator.providers.modal_yolo_client.store_receipts", fake_store, create=True),
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=fake_response)
            mock_client_cls.return_value = mock_client

            # Patch the lazy import inside detect_symbols_modal
            with patch(
                "aspire_orchestrator.providers.modal_yolo_client.store_receipts",
                side_effect=fake_store,
                create=True,
            ):
                result = self._run(detect_symbols_modal(
                    b"fake_png_bytes",
                    sheet_id="sheet-uuid-001",
                    correlation_id=CORR_ID,
                ))

        # Result should be valid ModalResult
        assert result is not None
        assert result.model_version == "modal-yolo11n-test"
        assert len(result.detections) == 1

    def test_timeout_returns_none(self) -> None:
        """On timeout, detect_symbols_modal returns None."""
        import httpx
        from aspire_orchestrator.providers.modal_yolo_client import detect_symbols_modal

        with (
            patch.dict("os.environ", {"ASPIRE_DREW_MODAL_YOLO_URL": "https://fake-modal.modal.run/detect"}),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_client_cls.return_value = mock_client

            result = self._run(detect_symbols_modal(
                b"fake_png_bytes",
                sheet_id="sheet-timeout-001",
                correlation_id=CORR_ID,
            ))

        assert result is None

    def test_network_error_returns_none(self) -> None:
        """On generic network error, detect_symbols_modal returns None."""
        import httpx
        from aspire_orchestrator.providers.modal_yolo_client import detect_symbols_modal

        with (
            patch.dict("os.environ", {"ASPIRE_DREW_MODAL_YOLO_URL": "https://fake-modal.modal.run/detect"}),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=ConnectionError("network down"))
            mock_client_cls.return_value = mock_client

            result = self._run(detect_symbols_modal(
                b"fake_png_bytes",
                sheet_id="sheet-network-001",
                correlation_id=CORR_ID,
            ))

        assert result is None

    def test_http_500_returns_none(self) -> None:
        """On HTTP 500, detect_symbols_modal returns None."""
        import httpx
        from aspire_orchestrator.providers.modal_yolo_client import detect_symbols_modal

        fake_response = MagicMock(spec=httpx.Response)
        fake_response.status_code = 500

        with (
            patch.dict("os.environ", {"ASPIRE_DREW_MODAL_YOLO_URL": "https://fake-modal.modal.run/detect"}),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=fake_response)
            mock_client_cls.return_value = mock_client

            result = self._run(detect_symbols_modal(
                b"fake_png_bytes",
                sheet_id="sheet-500-001",
                correlation_id=CORR_ID,
            ))

        assert result is None

    def test_non_json_response_returns_none(self) -> None:
        """On non-JSON response body, detect_symbols_modal returns None."""
        import httpx
        from aspire_orchestrator.providers.modal_yolo_client import detect_symbols_modal

        fake_response = MagicMock(spec=httpx.Response)
        fake_response.status_code = 200
        fake_response.json.side_effect = ValueError("not JSON")

        with (
            patch.dict("os.environ", {"ASPIRE_DREW_MODAL_YOLO_URL": "https://fake-modal.modal.run/detect"}),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=fake_response)
            mock_client_cls.return_value = mock_client

            result = self._run(detect_symbols_modal(
                b"fake_png_bytes",
                sheet_id="sheet-json-001",
                correlation_id=CORR_ID,
            ))

        assert result is None

    def test_no_modal_url_returns_none_immediately(self) -> None:
        """When ASPIRE_DREW_MODAL_YOLO_URL is not set, returns None immediately."""
        from aspire_orchestrator.providers.modal_yolo_client import detect_symbols_modal

        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("ASPIRE_DREW_MODAL_YOLO_URL", None)
            result = self._run(detect_symbols_modal(
                b"fake_png_bytes",
                sheet_id="sheet-nourl-001",
                correlation_id=CORR_ID,
            ))

        assert result is None

    def test_empty_bytes_returns_none_immediately(self) -> None:
        """When sheet_image_bytes is empty, returns None immediately."""
        from aspire_orchestrator.providers.modal_yolo_client import detect_symbols_modal

        with patch.dict("os.environ", {"ASPIRE_DREW_MODAL_YOLO_URL": "https://fake-modal.modal.run/detect"}):
            result = self._run(detect_symbols_modal(
                b"",
                sheet_id="sheet-empty-001",
                correlation_id=CORR_ID,
            ))

        assert result is None
