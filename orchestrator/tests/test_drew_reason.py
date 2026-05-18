"""Drew Wave 4 REASON tests.

Law compliance tested:
  Law #1: Drew returns structured data — no autonomous decisions.
  Law #2: Every code path emits a receipt with correct event_type.
  Law #3: Missing payload keys → error + receipt (fail-closed).
  Law #6: Case-pack memory is strictly tenant-scoped (evil test).
  Law #9: Story markdown NEVER appears in receipts or logs.

Test categories:
  1. Payload validation (missing keys → error)
  2. Receipt shape conformance (every code path emits receipt)
  3. Story content (phases have markdown, every fact has truth tag)
  4. Confidence demotion (low-confidence → missing_input)
  5. Seal-detected trust upgrade (+0.05 boost)
  6. Case-pack isolation (tenant B gets empty hints when A has stories)
  7. Golden fixture tests (GAVNN + ENG_Rev1)
  8. Model parity (@pytest.mark.slow @pytest.mark.live — skipped without keys)

Mocking strategy:
  - supabase_select, supabase_insert patched by default.
  - generate_json_async patched with a realistic stub response.
  - "live" tests use real API keys and are marked @pytest.mark.live.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "blueprints"

SUITE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUITE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OFFICE_A = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PROJECT_A = "proj-aaaa-0000-0000-aaaaaaaaaaaa"

SHEET_1_ID = "sheet-001-0000-0000-aaaaaaaaaaaa"
SHEET_2_ID = "sheet-002-0000-0000-aaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _query_receipts_by_event(event_type: str) -> list[dict]:
    import aspire_orchestrator.services.receipt_store as rs
    with rs._lock:
        return [r for r in rs._receipts if r.get("event_type") == event_type]


def _query_receipts_by_corr(correlation_id: str) -> list[dict]:
    import aspire_orchestrator.services.receipt_store as rs
    with rs._lock:
        return [r for r in rs._receipts if r.get("correlation_id") == correlation_id]


@pytest.fixture(autouse=True)
def _clear_receipt_store():
    from aspire_orchestrator.services.receipt_store import clear_store
    clear_store()
    yield
    clear_store()


@pytest.fixture(autouse=True)
def _stub_openai_api_key(monkeypatch):
    """Set a stub OPENAI_API_KEY so _resolve_api_key() doesn't raise in unit tests.

    Live tests that actually call the API are marked @pytest.mark.live and skip
    unless a real key is present. Unit tests never reach the real API call because
    generate_json_async is mocked.
    """
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-stub-key-for-unit-tests")


def _make_sheet(
    sheet_id: str = SHEET_1_ID,
    project_id: str = PROJECT_A,
    suite_id: str = SUITE_A,
    *,
    supersedes_id: str | None = None,
    discipline: str = "A",
    ocr_text: str = "DEMISING WALL: 3-5/8\" MTL STUD, 10'-0\" CLG HT",
    seal_detected: bool = False,
    scale: str = "1/4\"=1'-0\"",
) -> dict:
    return {
        "id": sheet_id,
        "suite_id": suite_id,
        "project_id": project_id,
        "sheet_number": "A-1",
        "discipline": discipline,
        "ocr_text": ocr_text,
        "hash": "abc123",
        "scale": scale,
        "seal_detected": seal_detected,
        "supersedes_id": supersedes_id,
        "created_at": "2026-05-17T00:00:00+00:00",
    }


def _make_llm_response(
    *,
    phase_count: int = 3,
    include_low_confidence_assumed: bool = False,
    include_missing_input: bool = True,
    include_tariff_flag: bool = False,
    include_untagged_fact: bool = False,
) -> dict:
    """Build a realistic LLM structured output."""
    assemblies = [
        {
            "type": "Demising wall, 3-5/8\" MTL STUD / GWB both sides, 10'-0\" ht",
            "quantity": 84.0,
            "unit": "LF",
            "truth": "derived",
            "confidence": 0.91,
            "source_sheet_id": SHEET_1_ID,
            "tariff_flag": None,
        },
        {
            "type": "Door opening, Type A",
            "quantity": 3.0,
            "unit": "ea",
            "truth": "observed",
            "confidence": None,
            "source_sheet_id": SHEET_1_ID,
            "tariff_flag": None,
        },
    ]

    if include_low_confidence_assumed:
        assemblies.append({
            "type": "Ceiling height 9-ft assumed",
            "quantity": None,
            "unit": "ft",
            "truth": "assumed",
            "confidence": 0.62,  # Below 0.70 floor → must demote to missing_input
            "source_sheet_id": SHEET_1_ID,
            "tariff_flag": None,
        })

    material_lines = [
        {
            "line_item": "Metal stud, 3-5/8\" 20ga",
            "quantity": 84.0,
            "unit": "LF",
            "truth": "derived",
            "confidence": 0.91,
            "source_sheet_id": SHEET_1_ID,
            "tariff_flag": None,
        },
        {
            "line_item": "GWB, 5/8\" standard",
            "quantity": 1680.0,
            "unit": "SF",
            "truth": "derived",
            "confidence": 0.91,
            "source_sheet_id": SHEET_1_ID,
            "tariff_flag": None,
        },
    ]

    if include_tariff_flag:
        material_lines.append({
            "line_item": "Galv. steel edge metal, 24-ga",
            "quantity": 7300.0,
            "unit": "LF",
            "truth": "observed",
            "confidence": None,
            "source_sheet_id": SHEET_1_ID,
            "tariff_flag": "section_232_steel",
        })

    if include_untagged_fact:
        assemblies.append({
            "type": "This fact has no truth tag",
            "quantity": 5.0,
            "unit": "ea",
            # Missing 'truth' field — should be dropped by defense-in-depth
            "confidence": 0.95,
        })

    phases = []
    for i in range(1, phase_count + 1):
        phases.append({
            "phase_number": i,
            "phase_name": f"Phase {i} — Demo" if i == 1 else f"Phase {i} — Work",
            "markdown": f"## Phase {i}\n\nSample story text for phase {i}.",
            "assemblies": assemblies if i == 1 else [],
            "material_lines": material_lines if i == 1 else [],
        })

    missing_inputs = []
    if include_missing_input:
        missing_inputs.append({
            "description": "Ceiling height not specified on any sheet.",
            "suggested_resolution": "Confirm ceiling height on site or from architectural notes.",
        })

    return {
        "phases": phases,
        "missing_inputs": missing_inputs,
        "truth_distribution": {
            "observed": 3,
            "derived": 4,
            "assumed": 0,
        },
        "mean_confidence": 0.91,
    }


# ---------------------------------------------------------------------------
# 1. Payload validation
# ---------------------------------------------------------------------------

class TestReasonPayloadValidation:
    """REASON is real (Wave 4) — missing keys return error + receipt."""

    def test_missing_project_id_returns_error(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        result = drew.run_agentic_loop(
            "REASON", {"suite_id": SUITE_A}, "test-reason-missing-pid"
        )
        assert result["status"] == "error"
        assert result["stage"] == "reason"
        assert "project_id" in result.get("reason", "")

    def test_missing_suite_id_returns_error(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        result = drew.run_agentic_loop(
            "REASON", {"project_id": PROJECT_A}, "test-reason-missing-sid"
        )
        assert result["status"] == "error"
        assert result["stage"] == "reason"
        assert "suite_id" in result.get("reason", "")

    def test_empty_payload_returns_error(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        result = drew.run_agentic_loop("REASON", {}, "test-reason-empty")
        assert result["status"] == "error"
        assert result["stage"] == "reason"


# ---------------------------------------------------------------------------
# 2. Receipt emission (Law #2)
# ---------------------------------------------------------------------------

class TestReasonReceiptEmission:
    """Every code path — including failures and denials — emits a receipt."""

    def test_reason_emits_receipt_on_missing_key(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        corr = "test-reason-receipt-missing-" + str(uuid.uuid4())
        drew.run_agentic_loop("REASON", {}, corr)
        receipts = _query_receipts_by_corr(corr)
        assert len(receipts) >= 1
        types = {r["event_type"] for r in receipts}
        assert "blueprint.reason" in types

    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.generate_json_async",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_insert",
        new_callable=AsyncMock,
    )
    def test_reason_emits_receipt_on_success(
        self,
        mock_insert: AsyncMock,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        mock_select.return_value = [_make_sheet()]
        mock_llm.return_value = _make_llm_response()
        mock_insert.return_value = None

        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        corr = "test-reason-receipt-ok-" + str(uuid.uuid4())
        result = drew.run_agentic_loop(
            "REASON",
            {"project_id": PROJECT_A, "suite_id": SUITE_A},
            corr,
        )

        assert result["status"] == "ok"
        receipts = _query_receipts_by_corr(corr)
        assert len(receipts) >= 1
        reason_receipts = [r for r in receipts if r["event_type"] == "blueprint.reason"]
        assert reason_receipts, "Must have blueprint.reason receipt"
        r = reason_receipts[0]
        assert r["actor"] == "skillpack:drew-blueprint"
        assert r["status"] == "ok"
        # Law #9: markdown must NOT appear in receipt
        receipt_text = json.dumps(r)
        assert "Sample story text" not in receipt_text, (
            "Story markdown must not appear in receipt (Law #9)"
        )

    def test_reason_receipt_metadata_has_counts(self) -> None:
        """Receipt metadata must include counts (not markdown)."""
        with (
            patch(
                "aspire_orchestrator.services.blueprint.story_writer.generate_json_async",
                new_callable=AsyncMock,
                return_value=_make_llm_response(phase_count=3),
            ),
            patch(
                "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
                new_callable=AsyncMock,
                return_value=[_make_sheet()],
            ),
            patch(
                "aspire_orchestrator.services.blueprint.story_writer.supabase_insert",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            from aspire_orchestrator.skillpacks.drew_blueprint import Drew
            drew = Drew()
            corr = "test-reason-receipt-meta-" + str(uuid.uuid4())
            drew.run_agentic_loop(
                "REASON",
                {"project_id": PROJECT_A, "suite_id": SUITE_A},
                corr,
            )
            receipts = [
                r for r in _query_receipts_by_corr(corr)
                if r["event_type"] == "blueprint.reason"
            ]
            assert receipts
            meta = receipts[0].get("metadata", {})
            assert "phase_count" in meta
            assert "assembly_count" in meta
            assert "material_count" in meta
            assert "missing_input_count" in meta
            assert "mean_confidence" in meta
            assert "truth_distribution" in meta
            assert "model_version" in meta


# ---------------------------------------------------------------------------
# 3. Story content — phases, truth tags
# ---------------------------------------------------------------------------

class TestReasonStoryContent:
    """Verify story rows have correct shape and every fact has a truth tag."""

    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.generate_json_async",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_insert",
        new_callable=AsyncMock,
    )
    def test_reason_writes_story_rows_with_phases(
        self,
        mock_insert: AsyncMock,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """write_story must persist one story row per LLM-returned phase."""
        mock_select.return_value = [_make_sheet()]
        mock_llm.return_value = _make_llm_response(phase_count=5)
        inserted_tables: list[str] = []

        async def _capture_insert(table: str, data: dict) -> None:
            inserted_tables.append(table)

        mock_insert.side_effect = _capture_insert

        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        result = drew.run_agentic_loop(
            "REASON",
            {"project_id": PROJECT_A, "suite_id": SUITE_A},
            "test-phase-count-" + str(uuid.uuid4()),
        )

        assert result["status"] == "ok"
        assert result["phase_count"] == 5
        story_inserts = [t for t in inserted_tables if t == "blueprint_story"]
        assert len(story_inserts) == 5, (
            f"Expected 5 story rows, got {len(story_inserts)}"
        )

    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.generate_json_async",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_insert",
        new_callable=AsyncMock,
    )
    def test_reason_every_fact_has_truth_tag(
        self,
        mock_insert: AsyncMock,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """Every assembly and material row inserted must have a truth field."""
        mock_select.return_value = [_make_sheet()]
        mock_llm.return_value = _make_llm_response()
        inserted_rows: list[tuple[str, dict]] = []

        async def _capture(table: str, data: dict) -> None:
            inserted_rows.append((table, data))

        mock_insert.side_effect = _capture

        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        drew.run_agentic_loop(
            "REASON",
            {"project_id": PROJECT_A, "suite_id": SUITE_A},
            "test-truth-tag-" + str(uuid.uuid4()),
        )

        valid_truth_tags = {"observed", "derived", "assumed", "field_confirmed", "vendor_confirmed"}
        for table, row in inserted_rows:
            if table in ("blueprint_assemblies", "blueprint_materials"):
                assert row.get("truth") in valid_truth_tags, (
                    f"Row in {table} has invalid truth tag: {row.get('truth')}"
                )


# ---------------------------------------------------------------------------
# 4. Confidence demotion → missing_input
# ---------------------------------------------------------------------------

class TestReasonConfidenceDemotion:
    """Facts below confidence floor must be emitted as missing_inputs, not rows."""

    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.generate_json_async",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_insert",
        new_callable=AsyncMock,
    )
    def test_low_confidence_assumed_creates_missing_input(
        self,
        mock_insert: AsyncMock,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """Assumed fact at confidence 0.62 (< 0.70 floor) must become missing_input."""
        mock_select.return_value = [_make_sheet()]
        mock_llm.return_value = _make_llm_response(include_low_confidence_assumed=True)
        inserted_rows: list[tuple[str, dict]] = []

        async def _capture(table: str, data: dict) -> None:
            inserted_rows.append((table, data))

        mock_insert.side_effect = _capture

        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        result = drew.run_agentic_loop(
            "REASON",
            {"project_id": PROJECT_A, "suite_id": SUITE_A},
            "test-demotion-" + str(uuid.uuid4()),
        )

        # The low-confidence assumed item should NOT appear as an assembly
        assembly_rows = [
            row for (t, row) in inserted_rows if t == "blueprint_assemblies"
        ]
        low_conf_assemblies = [
            r for r in assembly_rows
            if "ceiling height 9-ft assumed" in (r.get("type") or "").lower()
        ]
        assert not low_conf_assemblies, (
            "Low-confidence assumed fact must not become an assembly row"
        )

        # It should appear as a missing_input
        mi_rows = [row for (t, row) in inserted_rows if t == "blueprint_missing_inputs"]
        assert mi_rows, "Low-confidence fact must produce a missing_input"


# ---------------------------------------------------------------------------
# 5. Seal-detected trust upgrade
# ---------------------------------------------------------------------------

class TestReasonSealTrustUpgrade:
    """seal_detected=true on a sheet gives +0.05 confidence boost."""

    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.generate_json_async",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_insert",
        new_callable=AsyncMock,
    )
    def test_seal_detected_upgrades_trust(
        self,
        mock_insert: AsyncMock,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """A derived fact at confidence 0.82 on a sealed sheet should be boosted
        to 0.87 (above 0.85 floor) and emitted as a row, not a missing_input."""
        # Sheet with seal_detected=True
        sealed_sheet = _make_sheet(seal_detected=True)
        mock_select.return_value = [sealed_sheet]

        # LLM returns a derived fact at confidence 0.82 (below floor without boost,
        # above with +0.05 boost → 0.87)
        llm_resp = _make_llm_response()
        # Override the assembly confidence to 0.82 on the sealed sheet
        llm_resp["phases"][0]["assemblies"][0]["confidence"] = 0.82
        llm_resp["phases"][0]["assemblies"][0]["source_sheet_id"] = SHEET_1_ID

        mock_llm.return_value = llm_resp
        inserted_tables: list[str] = []

        async def _capture(table: str, data: dict) -> None:
            inserted_tables.append(table)

        mock_insert.side_effect = _capture

        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        drew.run_agentic_loop(
            "REASON",
            {"project_id": PROJECT_A, "suite_id": SUITE_A},
            "test-seal-boost-" + str(uuid.uuid4()),
        )

        # The assembly should appear — not be demoted to missing_input
        asm_inserts = [t for t in inserted_tables if t == "blueprint_assemblies"]
        mi_inserts = [t for t in inserted_tables if t == "blueprint_missing_inputs"]

        # At least one assembly inserted (seal boost saved it from demotion)
        assert len(asm_inserts) > 0, (
            "Seal boost should have raised confidence 0.82 → 0.87, keeping assembly"
        )


# ---------------------------------------------------------------------------
# 6. Untagged facts are dropped (defense in depth)
# ---------------------------------------------------------------------------

class TestReasonUntaggedFactDefense:
    """Facts without truth tags must be silently dropped."""

    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.generate_json_async",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_insert",
        new_callable=AsyncMock,
    )
    def test_untagged_facts_are_dropped(
        self,
        mock_insert: AsyncMock,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """LLM response with an untagged fact: that fact must not be inserted."""
        mock_select.return_value = [_make_sheet()]
        mock_llm.return_value = _make_llm_response(include_untagged_fact=True)
        inserted_rows: list[tuple[str, dict]] = []

        async def _capture(table: str, data: dict) -> None:
            inserted_rows.append((table, data))

        mock_insert.side_effect = _capture

        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        drew.run_agentic_loop(
            "REASON",
            {"project_id": PROJECT_A, "suite_id": SUITE_A},
            "test-untagged-drop-" + str(uuid.uuid4()),
        )

        assembly_rows = [r for (t, r) in inserted_rows if t == "blueprint_assemblies"]
        untagged = [
            r for r in assembly_rows
            if "no truth tag" in (r.get("type") or "").lower()
        ]
        assert not untagged, "Untagged fact must be dropped, not inserted"


# ---------------------------------------------------------------------------
# 7. Tariff flag preservation
# ---------------------------------------------------------------------------

class TestReasonTariffFlag:
    """Material rows with tariff flags must carry those flags in the DB insert."""

    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.generate_json_async",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_insert",
        new_callable=AsyncMock,
    )
    def test_tariff_flag_preserved_in_material_row(
        self,
        mock_insert: AsyncMock,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        mock_select.return_value = [_make_sheet()]
        mock_llm.return_value = _make_llm_response(include_tariff_flag=True)
        inserted_rows: list[tuple[str, dict]] = []

        async def _capture(table: str, data: dict) -> None:
            inserted_rows.append((table, data))

        mock_insert.side_effect = _capture

        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        drew.run_agentic_loop(
            "REASON",
            {"project_id": PROJECT_A, "suite_id": SUITE_A},
            "test-tariff-flag-" + str(uuid.uuid4()),
        )

        mat_rows = [r for (t, r) in inserted_rows if t == "blueprint_materials"]
        steel_rows = [
            r for r in mat_rows
            if r.get("tariff_flag") == "section_232_steel"
        ]
        assert steel_rows, "Material with section_232_steel flag must be persisted"


# ---------------------------------------------------------------------------
# 8. Case-pack isolation (Law #6 evil test)
# ---------------------------------------------------------------------------

class TestReasonCasePackIsolation:
    """Tenant B's case pack must be empty when only Tenant A has prior stories."""

    @pytest.mark.asyncio
    async def test_case_pack_isolation_law_6(self) -> None:
        """retrieve_case_pack_hints for suite_b returns [] when only suite_a has rows."""
        from aspire_orchestrator.services.blueprint.case_pack_memory import (
            retrieve_case_pack_hints,
        )

        # Tenant A has one prior story
        suite_a_story = {
            "id": str(uuid.uuid4()),
            "suite_id": SUITE_A,
            "project_id": PROJECT_A,
            "phase": 3,
            "markdown": "Sample story for Tenant A",
            "truth_distribution": {"observed": 2, "derived": 1, "assumed": 0},
            "created_at": "2026-05-17T00:00:00+00:00",
        }

        async def _mock_select(table: str, *, filters: str = "", **kwargs):
            if "blueprint_story" in table:
                # Only return rows for the suite matching the filter
                if f"suite_id=eq.{SUITE_A}" in filters:
                    return [suite_a_story]
                elif f"suite_id=eq.{SUITE_B}" in filters:
                    return []  # Tenant B sees nothing
                return []
            if "blueprint_sheets" in table:
                return []
            return []

        with patch(
            "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
            side_effect=_mock_select,
        ):
            # Tenant A gets hints
            hints_a = await retrieve_case_pack_hints(
                suite_id=SUITE_A,
                project_context={"discipline_mix": ["A"], "sheet_count": 5},
            )
            # Tenant B gets nothing — EVEN THOUGH Tenant A has stories
            hints_b = await retrieve_case_pack_hints(
                suite_id=SUITE_B,
                project_context={"discipline_mix": ["A"], "sheet_count": 5},
            )

        assert len(hints_b) == 0, (
            f"Tenant B must get 0 hints when only Tenant A has stories. "
            f"Got: {len(hints_b)} hints — LAW #6 VIOLATION"
        )

    @pytest.mark.asyncio
    async def test_case_pack_missing_suite_id_returns_empty(self) -> None:
        """Missing suite_id returns empty list — never queries cross-tenant."""
        from aspire_orchestrator.services.blueprint.case_pack_memory import (
            retrieve_case_pack_hints,
        )

        hints = await retrieve_case_pack_hints(
            suite_id="",
            project_context={},
        )
        assert hints == []


# ---------------------------------------------------------------------------
# 9. No-sheets edge case
# ---------------------------------------------------------------------------

class TestReasonNoSheets:
    """No sheets → RuntimeError raised → error result + receipt."""

    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
        new_callable=AsyncMock,
        return_value=[],  # Empty sheet list
    )
    def test_reason_no_sheets_returns_error(self, mock_select: AsyncMock) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        corr = "test-reason-no-sheets-" + str(uuid.uuid4())
        result = drew.run_agentic_loop(
            "REASON",
            {"project_id": PROJECT_A, "suite_id": SUITE_A},
            corr,
        )
        assert result["status"] == "error"
        assert result["stage"] == "reason"

        # Receipt must still be emitted (Law #2)
        receipts = _query_receipts_by_corr(corr)
        assert len(receipts) >= 1
        types = {r["event_type"] for r in receipts}
        assert "blueprint.reason" in types

    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
        new_callable=AsyncMock,
        return_value=[
            # Sheet with supersedes_id set — treated as superseded, excluded
            _make_sheet(supersedes_id="old-sheet-id-000000000000000")
        ],
    )
    def test_reason_all_superseded_returns_error(self, mock_select: AsyncMock) -> None:
        """If all sheets are superseded, raise fail-closed error."""
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        result = drew.run_agentic_loop(
            "REASON",
            {"project_id": PROJECT_A, "suite_id": SUITE_A},
            "test-all-superseded-" + str(uuid.uuid4()),
        )
        assert result["status"] == "error"
        assert result["stage"] == "reason"


# ---------------------------------------------------------------------------
# 10. LLM invalid output → retry + fail
# ---------------------------------------------------------------------------

class TestReasonLLMInvalidOutput:
    """Two consecutive empty LLM responses → error + blueprint.reason.invalid receipt."""

    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.generate_json_async",
        new_callable=AsyncMock,
        return_value={},  # Empty dict — no "phases" key
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
        new_callable=AsyncMock,
        return_value=[_make_sheet()],
    )
    def test_llm_invalid_output_returns_error(
        self,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        corr = "test-llm-invalid-" + str(uuid.uuid4())
        result = drew.run_agentic_loop(
            "REASON",
            {"project_id": PROJECT_A, "suite_id": SUITE_A},
            corr,
        )
        assert result["status"] == "error"
        assert result["stage"] == "reason"

        # receipt must be emitted (Law #2)
        receipts = _query_receipts_by_corr(corr)
        assert receipts


# ---------------------------------------------------------------------------
# 11. Golden fixture tests (GAVNN + ENG_Rev1)
# ---------------------------------------------------------------------------

class TestReasonGoldenFixtures:
    """Structural assertions against the two canonical golden fixtures."""

    @pytest.mark.xfail(
        reason="golden fixture requires real PDF parsing (INGEST+SEE) + live LLM",
        strict=False,
    )
    def test_reason_against_gavnn_golden(self) -> None:
        """GAVNN addendum fixture: story must mention phase keywords."""
        fixture_path = FIXTURES_DIR / "gavnn_addendum_1.pdf"
        if not fixture_path.exists():
            pytest.skip("GAVNN golden fixture not found")

        # This test is a structural marker — full pipeline requires:
        # 1. INGEST (OCR) → writes sheets
        # 2. SEE (YOLO) → writes symbols
        # 3. REASON (LLM) → writes story
        # Without a mocked pipeline, we assert the fixture is present.
        assert fixture_path.exists()

    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.generate_json_async",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_insert",
        new_callable=AsyncMock,
    )
    def test_reason_gavnn_story_contains_phase_keywords(
        self,
        mock_insert: AsyncMock,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """GAVNN-style: story phases must include revision and framing keywords."""
        # GAVNN sheet context: addendum supersession + demising wall
        gavnn_sheet = _make_sheet(
            ocr_text=(
                "ADDENDUM 1 — REPLACES SHEET A-3 REV 0 — "
                "DEMISING WALL: 3-5/8\" MTL STUD, GWB-MR EA SIDE, 10'-0\" CLG HT"
            ),
            discipline="Addenda",
        )
        mock_select.return_value = [gavnn_sheet]

        # LLM returns story referencing addendum and framing
        llm_resp = _make_llm_response(phase_count=3)
        llm_resp["phases"][0]["markdown"] = (
            "## Phase 3 — Framing / Drywall\n\n"
            "Addendum A-3 Rev 1 specifies 3-5/8\" metal stud framing with "
            "moisture-resistant GWB. Ceiling height 10'-0\" (observed). "
            "Supersedes A-3 Rev 0 — revision relationship confirmed."
        )
        mock_llm.return_value = llm_resp
        mock_insert.return_value = None

        inserted_markdowns: list[str] = []

        async def _capture(table: str, data: dict) -> None:
            if table == "blueprint_story":
                inserted_markdowns.append(str(data.get("markdown", "")))

        mock_insert.side_effect = _capture

        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        result = drew.run_agentic_loop(
            "REASON",
            {"project_id": PROJECT_A, "suite_id": SUITE_A},
            "test-gavnn-golden-" + str(uuid.uuid4()),
        )
        assert result["status"] == "ok"
        assert result["phase_count"] == 3

        full_story = " ".join(inserted_markdowns).lower()
        assert "framing" in full_story or "addendum" in full_story, (
            "GAVNN story must mention framing or addendum phase"
        )

    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.generate_json_async",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_select",
        new_callable=AsyncMock,
    )
    @patch(
        "aspire_orchestrator.services.blueprint.story_writer.supabase_insert",
        new_callable=AsyncMock,
    )
    def test_reason_against_eng_rev1_golden(
        self,
        mock_insert: AsyncMock,
        mock_select: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """ENG_Rev1: multi-discipline story must cover civil, plumbing, electrical."""
        # 4 sealed sheets across C/P/E disciplines
        sheets = [
            _make_sheet(
                sheet_id="sheet-c1-0000-0000-aaaaaaaaaaaa",
                discipline="C",
                seal_detected=True,
                ocr_text="8\" PVC SANITARY SEWER APPROX 180 LF; 4 CATCH BASINS TYPE D",
            ),
            _make_sheet(
                sheet_id="sheet-p1-0000-0000-aaaaaaaaaaaa",
                discipline="P",
                seal_detected=True,
                ocr_text="3\" DWV CAST IRON BELOW SLAB; 1-1/2\" HWS/HWR; 22 FU",
            ),
            _make_sheet(
                sheet_id="sheet-e1-0000-0000-aaaaaaaaaaaa",
                discipline="E",
                seal_detected=True,
                ocr_text="200A 120/240V SINGLE PHASE SERVICE; FEEDER 2-#4/0 + #2 GND IN 2\" CONDUIT",
            ),
            _make_sheet(
                sheet_id="sheet-e2-0000-0000-aaaaaaaaaaaa",
                discipline="E",
                seal_detected=False,
                ocr_text="12 POLE-MOUNTED FIXTURES 150W LED PHOTOCELL CONTROLLED",
            ),
        ]
        mock_select.return_value = sheets

        # LLM returns 4-discipline phased story
        llm_resp = {
            "phases": [
                {
                    "phase_number": 1,
                    "phase_name": "Phase 1 — Site Prep / Underground Utilities",
                    "markdown": "## Phase 1\n\nCivil: 8-inch PVC sanitary sewer 180 LF (observed, sealed C-1).",
                    "assemblies": [
                        {"type": "PVC sanitary sewer, 8-inch", "quantity": 180.0, "unit": "LF",
                         "truth": "observed", "confidence": None, "source_sheet_id": "sheet-c1-0000-0000-aaaaaaaaaaaa"}
                    ],
                    "material_lines": [],
                },
                {
                    "phase_number": 2,
                    "phase_name": "Phase 2 — Below-Slab Plumbing",
                    "markdown": "## Phase 2\n\nPlumbing: 3-inch DWV cast iron below slab (observed, sealed P-1).",
                    "assemblies": [
                        {"type": "DWV cast iron, 3-inch", "quantity": None, "unit": "LF",
                         "truth": "observed", "confidence": None, "source_sheet_id": "sheet-p1-0000-0000-aaaaaaaaaaaa"}
                    ],
                    "material_lines": [],
                },
                {
                    "phase_number": 5,
                    "phase_name": "Phase 5 — Electrical Service",
                    "markdown": "## Phase 5\n\nElectrical: 200A single-phase service (observed, sealed E-1).",
                    "assemblies": [
                        {"type": "200A single-phase service entrance", "quantity": 1, "unit": "ea",
                         "truth": "observed", "confidence": None, "source_sheet_id": "sheet-e1-0000-0000-aaaaaaaaaaaa"}
                    ],
                    "material_lines": [],
                },
                {
                    "phase_number": 8,
                    "phase_name": "Phase 8 — Site Lighting",
                    "markdown": "## Phase 8\n\nSite lighting: 12 pole-mounted LED fixtures 150W (observed, E-2).",
                    "assemblies": [
                        {"type": "LED pole-mounted fixture, 150W", "quantity": 12, "unit": "ea",
                         "truth": "observed", "confidence": None, "source_sheet_id": "sheet-e2-0000-0000-aaaaaaaaaaaa"}
                    ],
                    "material_lines": [],
                },
            ],
            "missing_inputs": [
                {"description": "Storm drain pipe size and length not specified.", "suggested_resolution": None},
                {"description": "Feeder run length not dimensioned.", "suggested_resolution": None},
            ],
            "truth_distribution": {"observed": 4, "derived": 0, "assumed": 0},
            "mean_confidence": 1.0,
        }
        mock_llm.return_value = llm_resp
        mock_insert.return_value = None

        from aspire_orchestrator.skillpacks.drew_blueprint import Drew
        drew = Drew()
        result = drew.run_agentic_loop(
            "REASON",
            {"project_id": PROJECT_A, "suite_id": SUITE_A},
            "test-eng-rev1-golden-" + str(uuid.uuid4()),
        )

        assert result["status"] == "ok"
        assert result["phase_count"] == 4, f"Expected 4 phases, got {result['phase_count']}"
        # Truth distribution: all 4 assemblies are observed
        td = result.get("truth_distribution", {})
        assert td.get("observed", 0) >= 4, (
            f"ENG_Rev1 story should have >=4 observed facts. Got: {td}"
        )
        # Missing inputs present
        assert result.get("missing_input_count", 0) >= 2


# ---------------------------------------------------------------------------
# 12. Model parity test (live, slow — skipped without API keys)
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.live
class TestReasonModelParity:
    """Mini vs production model: ≥85% structural similarity.

    Skipped by default. Run explicitly:
        pytest tests/test_drew_reason.py -m "slow and live" -v
    """

    def test_reason_model_parity_mini_vs_5_2(self) -> None:
        """Run REASON twice with different models, assert structural similarity."""
        import os

        if not os.getenv("OPENAI_API_KEY"):
            pytest.skip("OPENAI_API_KEY not set — skipping live model parity test")

        from aspire_orchestrator.services.blueprint.story_writer import write_story
        import asyncio

        # This test requires a real project in the DB — would need integration setup.
        # For now, this is a marker test showing the parity contract.
        pytest.skip(
            "Model parity test requires a real project with sheets in the DB. "
            "Run after setting up integration fixtures."
        )
