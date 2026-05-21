"""Wave 17b.5 — REASON assembly-typing strictness.

Goal: prove that the LLM-emitted `type` field on each assembly is normalized
against the canonical taxonomy and that anything not matching falls through to
blueprint_missing_inputs rather than becoming a `type='general'` placeholder.

Law compliance:
    Law #2: typed and missing rows must end up in receipts (assembly_typed stage receipt).
    Law #3: fail-closed — unknown types do NOT silently default; they emit missing_inputs.
    Law #6: rows include suite_id.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


SUITE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OFFICE_A = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PROJECT_A = "proj-aaaa-0000-0000-aaaaaaaaaaaa"
SHEET_1_ID = "sheet-001-0000-0000-aaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _clear_receipt_store() -> Any:
    from aspire_orchestrator.services.receipt_store import clear_store
    clear_store()
    yield
    clear_store()


@pytest.fixture(autouse=True)
def _stub_openai_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-stub-key")


def _make_sheet() -> dict[str, Any]:
    return {
        "id": SHEET_1_ID,
        "suite_id": SUITE_A,
        "project_id": PROJECT_A,
        "sheet_number": "E-1",
        "discipline": "E",
        "ocr_text": "POWER PLAN — TYPICAL TENANT SPACE",
        "hash": "abc",
        "scale": "1/4\"=1'-0\"",
        "seal_detected": False,
        "supersedes_id": None,
        "created_at": "2026-05-21T00:00:00+00:00",
    }


def _llm_response_with_types(types_with_truth: list[tuple[str, str, float | None]]) -> dict[str, Any]:
    """Build a minimal LLM response whose phase-1 assemblies have the given (type, truth, conf)."""
    assemblies = [
        {
            "type": t,
            "quantity": 1.0,
            "unit": "ea",
            "truth": truth,
            "confidence": conf,
            "source_sheet_id": SHEET_1_ID,
            "tariff_flag": None,
        }
        for (t, truth, conf) in types_with_truth
    ]
    phases = [{
        "phase_number": 1,
        "phase_name": "Phase 1 — Demo",
        "markdown": "## Phase 1\nStub.",
        "assemblies": assemblies,
        "material_lines": [],
    }]
    return {
        "json": {
            "phases": phases,
            "missing_inputs": [],
            "truth_distribution": {"observed": 0, "derived": len(assemblies), "assumed": 0},
            "mean_confidence": 0.9,
        },
        "usage": {
            "prompt_tokens": 100, "cached_tokens": 0, "cache_hit_ratio": 0.0,
            "completion_tokens": 100, "total_tokens": 200, "model": "gpt-5.4-mini",
            "latency_ms": 100.0, "prompt_prefix_hash": "abc",
        },
    }


def _run_reason() -> dict[str, Any]:
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew
    drew = Drew()
    corr = "test-typing-" + str(uuid.uuid4())
    return drew.run_agentic_loop(
        "REASON", {"project_id": PROJECT_A, "suite_id": SUITE_A}, corr
    )


class TestReasonAssemblyTyping:
    """Verify canonical-type strictness on REASON."""

    @patch("aspire_orchestrator.services.blueprint.story_writer.generate_json_async", new_callable=AsyncMock)
    @patch("aspire_orchestrator.services.blueprint.story_writer.supabase_select", new_callable=AsyncMock)
    @patch("aspire_orchestrator.services.blueprint.story_writer.supabase_insert", new_callable=AsyncMock)
    def test_three_canonical_types_produce_three_assemblies_zero_missing(
        self,
        mock_insert: AsyncMock,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        mock_select.return_value = [_make_sheet()]
        mock_llm.return_value = _llm_response_with_types([
            ("wall_demising_gwb_mr_both_sides", "derived", 0.91),
            ("door_hm_3070", "observed", None),
            ("electrical_receptacle_gfci", "derived", 0.88),
        ])
        inserts: list[tuple[str, dict[str, Any]]] = []

        async def _cap(table: str, data: dict[str, Any]) -> None:
            inserts.append((table, data))
        mock_insert.side_effect = _cap

        result = _run_reason()

        assert result["status"] == "ok"
        assemblies = [r for t, r in inserts if t == "blueprint_assemblies"]
        missing = [r for t, r in inserts if t == "blueprint_missing_inputs"]
        assert len(assemblies) == 3, f"expected 3 canonical assemblies, got {len(assemblies)}"
        for asm in assemblies:
            assert asm["type"] != ""
            assert asm["type"] != "general"
        # No missing_inputs from typing (the LLM-emitted missing_inputs list is empty).
        missing_from_typing = [
            m for m in missing if "non-canonical type" in m.get("description", "")
        ]
        assert len(missing_from_typing) == 0

    @patch("aspire_orchestrator.services.blueprint.story_writer.generate_json_async", new_callable=AsyncMock)
    @patch("aspire_orchestrator.services.blueprint.story_writer.supabase_select", new_callable=AsyncMock)
    @patch("aspire_orchestrator.services.blueprint.story_writer.supabase_insert", new_callable=AsyncMock)
    def test_two_canonical_one_unknown_produces_two_assemblies_one_missing(
        self,
        mock_insert: AsyncMock,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        mock_select.return_value = [_make_sheet()]
        mock_llm.return_value = _llm_response_with_types([
            ("wall_demising_gwb_mr_both_sides", "derived", 0.91),
            ("door_hm_3070", "observed", None),
            ("totally_made_up_assembly_type_xyz", "derived", 0.90),
        ])
        inserts: list[tuple[str, dict[str, Any]]] = []

        async def _cap(table: str, data: dict[str, Any]) -> None:
            inserts.append((table, data))
        mock_insert.side_effect = _cap

        result = _run_reason()

        assert result["status"] == "ok"
        assemblies = [r for t, r in inserts if t == "blueprint_assemblies"]
        missing_typing = [
            r for t, r in inserts
            if t == "blueprint_missing_inputs"
            and "non-canonical type" in r.get("description", "")
        ]
        assert len(assemblies) == 2
        assert len(missing_typing) == 1
        # And no 'general' placeholder survived.
        for asm in assemblies:
            assert asm["type"] != "general"
            assert asm["type"] != ""

    @patch("aspire_orchestrator.services.blueprint.story_writer.generate_json_async", new_callable=AsyncMock)
    @patch("aspire_orchestrator.services.blueprint.story_writer.supabase_select", new_callable=AsyncMock)
    @patch("aspire_orchestrator.services.blueprint.story_writer.supabase_insert", new_callable=AsyncMock)
    def test_human_readable_type_does_not_fuzzy_match(
        self,
        mock_insert: AsyncMock,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """`Wall - Demising` does NOT silently map to wall_demising_gwb_mr_both_sides.

        Strict matching: no fuzzy. The row drops to missing_input so the LLM
        prompt or the taxonomy can be tightened up.
        """
        mock_select.return_value = [_make_sheet()]
        mock_llm.return_value = _llm_response_with_types([
            ("Wall - Demising", "derived", 0.91),
        ])
        inserts: list[tuple[str, dict[str, Any]]] = []

        async def _cap(table: str, data: dict[str, Any]) -> None:
            inserts.append((table, data))
        mock_insert.side_effect = _cap

        _run_reason()
        assemblies = [r for t, r in inserts if t == "blueprint_assemblies"]
        missing_typing = [
            r for t, r in inserts
            if t == "blueprint_missing_inputs"
            and "non-canonical type" in r.get("description", "")
        ]
        assert len(assemblies) == 0
        assert len(missing_typing) == 1


class TestAssemblyTypedReceipt:
    """drew.reason.assembly_typed receipt is emitted on every REASON success."""

    @patch("aspire_orchestrator.services.blueprint.story_writer.generate_json_async", new_callable=AsyncMock)
    @patch("aspire_orchestrator.services.blueprint.story_writer.supabase_select", new_callable=AsyncMock)
    @patch("aspire_orchestrator.services.blueprint.story_writer.supabase_insert", new_callable=AsyncMock)
    def test_receipt_includes_typed_and_missing_counts(
        self,
        mock_insert: AsyncMock,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        mock_select.return_value = [_make_sheet()]
        mock_llm.return_value = _llm_response_with_types([
            ("wall_demising_gwb_mr_both_sides", "derived", 0.91),
            ("door_hm_3070", "observed", None),
            ("not_canonical", "derived", 0.91),
        ])
        mock_insert.return_value = None

        _run_reason()

        import aspire_orchestrator.services.receipt_store as rs
        with rs._lock:
            typed_receipts = [
                r for r in rs._receipts
                if r.get("receipt_type") == "drew.reason.assembly_typed"
                or r.get("action_type") == "drew.reason.assembly_typed"
            ]
        assert typed_receipts, "expected drew.reason.assembly_typed stage receipt"
        meta = typed_receipts[0].get("metadata", {})
        assert meta.get("typed_count") == 2
        assert meta.get("missing_count") == 1
        assert meta.get("distinct_types") == 2
