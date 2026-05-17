"""Drew Stage 2 CLASSIFY tests — Wave 2B.

Tests the CLASSIFY stage against real (redacted) fixtures. LLM calls are mocked.
PyMuPDF runs real.

Aspire Laws validated:
  - Law #2: Every classify call emits exactly one 'blueprint.classify' receipt
            with discipline_counts + revisions metadata.
  - Law #3: Fail-closed on low-confidence classification → blueprint_missing_inputs.
  - Law #6: suite_id scoped (verified via RLS evil test in test_drew_rls.py).

xfail policy: tests requiring the Wave-2 classify() implementation are marked
@pytest.mark.xfail(reason='blocked on wave-2-impl').
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "blueprints"

SUITE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OFFICE_A = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _fixture_bytes(name: str) -> bytes:
    path = FIXTURES_DIR / name
    assert path.exists(), f"Fixture not found: {path}"
    return path.read_bytes()


def _query_receipts_by_corr(correlation_id: str) -> list[dict]:
    """Query in-memory receipt store by correlation_id without suite_id filter.

    Drew Wave 1 stub receipts do not embed suite_id. This helper is test-only.
    Production code uses query_receipts() with mandatory suite_id (Law #6).
    """
    import aspire_orchestrator.services.receipt_store as rs
    with rs._lock:
        return [r for r in rs._receipts if r.get("correlation_id") == correlation_id]


@pytest.fixture(autouse=True)
def _clear_receipt_store():
    from aspire_orchestrator.services.receipt_store import clear_store
    clear_store()
    yield
    clear_store()


# ---------------------------------------------------------------------------
# CLASSIFY: discipline tags
# ---------------------------------------------------------------------------

class TestClassifyDisciplines:
    """CLASSIFY: multi-discipline master produces correct discipline distribution."""

    @pytest.mark.xfail(reason="blocked on wave-2-impl: Drew.classify() is still a stub")
    def test_classify_disciplines_match_golden(self) -> None:
        """eng_rev1_signed_master has C, E, P, LP disciplines — all must be tagged.

        Golden labels (from plan §9.3 and fixture inspection):
          - Civil (C): at least 1 sheet
          - Electrical (E): at least 1 sheet
          - Plumbing (P): at least 1 sheet
          - Lighting/Light Pole (LP): at least 1 sheet

        The classify stage must identify all four disciplines present in the 13-page master.
        """
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        data = _fixture_bytes("eng_rev1_signed_master.pdf")
        corr = str(uuid.uuid4())

        # Mock the LLM discipline tagger response for each sheet
        # Sheet 0 = Civil, 1-4 = Electrical, 5 = Plumbing, 6-7 = LightPole, 8-12 = General
        def mock_classify_sheet(sheet_text: str, sheet_num: int) -> str:
            if sheet_num == 0:
                return "C"
            elif 1 <= sheet_num <= 4:
                return "E"
            elif sheet_num == 5:
                return "P"
            elif 6 <= sheet_num <= 7:
                return "LP"
            return "G"

        with patch(
            "aspire_orchestrator.services.blueprint.discipline_tagger.classify_sheet",
            side_effect=mock_classify_sheet,
        ):
            result = drew.run_agentic_loop(
                "CLASSIFY",
                {"pdf_bytes": data, "suite_id": SUITE_A, "office_id": OFFICE_A},
                corr,
            )

        assert result["status"] == "ok", f"CLASSIFY must succeed: {result}"
        disciplines = result.get("discipline_counts", {})
        for required_discipline in ["C", "E", "P", "LP"]:
            assert required_discipline in disciplines, (
                f"Expected discipline '{required_discipline}' in classification result. "
                f"Got: {disciplines}"
            )
            assert disciplines[required_discipline] >= 1


class TestClassifyRevisionPair:
    """CLASSIFY: revision pair — older sheet must be marked superseded."""

    @pytest.mark.xfail(reason="blocked on wave-2-impl: Drew.classify() is still a stub")
    def test_classify_revision_pair_supersedes(self) -> None:
        """light_pole_lp1_r1.pdf + light_pole_revised.pdf in same project.

        After classify, light_pole_lp1_r1 sheet must have supersedes_id pointing
        from the revised sheet (i.e., the revised sheet supersedes the original).
        """
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        corr = str(uuid.uuid4())

        data_r1 = _fixture_bytes("light_pole_lp1_r1.pdf")
        data_revised = _fixture_bytes("light_pole_revised.pdf")

        # First ingest both into the same project
        with patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.LlamaParseClient"
        ) as mock_llama:
            mock_llama.return_value.parse_pdf = MagicMock(
                return_value={"pages": [{"page": 1, "text": "Light Pole R1"}]}
            )
            result_r1 = drew.run_agentic_loop(
                "INGEST",
                {"pdf_bytes": data_r1, "suite_id": SUITE_A, "project_id": "lp-test-project"},
                corr,
            )
            result_rev = drew.run_agentic_loop(
                "INGEST",
                {"pdf_bytes": data_revised, "suite_id": SUITE_A, "project_id": "lp-test-project"},
                corr,
            )

        # Now classify the project
        classify_corr = str(uuid.uuid4())
        with patch(
            "aspire_orchestrator.services.blueprint.revision_detector.detect_supersessions",
        ) as mock_detect:
            mock_detect.return_value = [
                {
                    "original_sheet_id": result_r1.get("sheet_ids", ["r1"])[0],
                    "revised_sheet_id": result_rev.get("sheet_ids", ["rev"])[0],
                    "supersedes_id": result_r1.get("sheet_ids", ["r1"])[0],
                }
            ]
            classify_result = drew.run_agentic_loop(
                "CLASSIFY",
                {"suite_id": SUITE_A, "project_id": "lp-test-project"},
                classify_corr,
            )

        assert classify_result["status"] == "ok"
        revisions = classify_result.get("revisions", [])
        assert len(revisions) >= 1, "Expected at least one supersession pair for light pole revision"
        # The supersession chain must link original → revised
        superseded_ids = [r.get("supersedes_id") for r in revisions]
        assert any(sid is not None for sid in superseded_ids), (
            "At least one sheet must have supersedes_id set (revision chain)"
        )


class TestClassifyAddendum:
    """CLASSIFY: GAVNN addendum sheets must be marked as superseding base sheets."""

    @pytest.mark.xfail(reason="blocked on wave-2-impl: Drew.classify() is still a stub")
    def test_classify_addendum_supersedes_base(self) -> None:
        """GAVNN Addendum 1 — after classify, addendum sheets must have supersedes_id.

        The GAVNN fixture is a 39-page addendum document. At least some sheets
        must be flagged as superseding a base document sheet (addendum_flag=True).
        """
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        data = _fixture_bytes("gavnn_addendum_1.pdf")
        corr = str(uuid.uuid4())

        with patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.LlamaParseClient"
        ) as mock_llama:
            mock_llama.return_value.parse_pdf = MagicMock(
                return_value={
                    "pages": [
                        {"page": i, "text": "ADDENDUM 1", "is_addendum": True}
                        for i in range(39)
                    ]
                }
            )
            ingest_result = drew.run_agentic_loop(
                "INGEST",
                {"pdf_bytes": data, "suite_id": SUITE_A},
                corr,
            )

        classify_corr = str(uuid.uuid4())
        classify_result = drew.run_agentic_loop(
            "CLASSIFY",
            {"suite_id": SUITE_A, "project_id": ingest_result.get("project_id")},
            classify_corr,
        )

        assert classify_result["status"] == "ok"
        # Addendum detection: addendum_flag or supersedes_id must be set on at least one sheet
        revisions = classify_result.get("revisions", [])
        addendum_sheets = classify_result.get("addendum_sheets", [])
        assert len(revisions) + len(addendum_sheets) > 0, (
            "GAVNN Addendum 1 must be classified with at least one addendum/supersession flag"
        )


class TestClassifyLowConfidence:
    """CLASSIFY: low-confidence sheet must create a blueprint_missing_inputs row."""

    @pytest.mark.xfail(reason="blocked on wave-2-impl: Drew.classify() is still a stub")
    def test_classify_low_confidence_creates_missing_input(self) -> None:
        """Sheet with weak title block signal → blueprint_missing_inputs entry.

        Uses electrical_e2.pdf which has minimal extractable text (0 redactions,
        likely a raster sheet). Low-confidence classification must not silently
        default; it must surface a missing_input row.
        """
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        data = _fixture_bytes("electrical_e2.pdf")
        corr = str(uuid.uuid4())

        with patch(
            "aspire_orchestrator.services.blueprint.discipline_tagger.classify_sheet",
        ) as mock_classify:
            # Return low confidence (below 0.85 threshold per drew-truth-class-policy.md)
            mock_classify.return_value = {"discipline": "E", "confidence": 0.45}

            with patch(
                "aspire_orchestrator.skillpacks.drew_blueprint.LlamaParseClient"
            ) as mock_llama:
                mock_llama.return_value.parse_pdf = MagicMock(
                    return_value={"pages": [{"page": 1, "text": ""}]}
                )
                ingest_result = drew.run_agentic_loop(
                    "INGEST",
                    {"pdf_bytes": data, "suite_id": SUITE_A},
                    corr,
                )

            classify_result = drew.run_agentic_loop(
                "CLASSIFY",
                {"suite_id": SUITE_A, "project_id": ingest_result.get("project_id")},
                str(uuid.uuid4()),
            )

        assert classify_result["status"] == "ok"
        missing_inputs = classify_result.get("missing_inputs", [])
        assert len(missing_inputs) >= 1, (
            "Low-confidence classification must emit a blueprint_missing_inputs entry. "
            "Confidence 0.45 < 0.85 threshold — must not silently default to guessed discipline."
        )
        # Verify the missing input is about discipline identification
        discipline_gaps = [
            m for m in missing_inputs
            if "discipline" in m.get("field", "").lower() or "classify" in m.get("field", "").lower()
        ]
        assert len(discipline_gaps) >= 1, (
            f"Missing input must reference discipline classification gap. Got: {missing_inputs}"
        )


# ---------------------------------------------------------------------------
# CLASSIFY: Receipt emission
# ---------------------------------------------------------------------------

class TestClassifyReceipt:
    """Law #2: CLASSIFY must emit 'blueprint.classify' receipt with metadata."""

    def test_classify_emits_receipt_on_stub(self) -> None:
        """Wave 1 stub emits receipt on CLASSIFY — assert it exists now."""
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        corr = "test-classify-receipt-" + str(uuid.uuid4())

        drew.run_agentic_loop(
            "CLASSIFY",
            {"suite_id": SUITE_A},
            corr,
        )

        receipts = _query_receipts_by_corr(corr)
        assert len(receipts) >= 1, "CLASSIFY must emit at least one receipt"
        event_types = [r["event_type"] for r in receipts]
        assert "blueprint.classify" in event_types, (
            f"Expected 'blueprint.classify' receipt, found: {event_types}"
        )

    @pytest.mark.xfail(reason="blocked on wave-2-impl: Drew.classify() is still a stub")
    def test_classify_receipt_has_discipline_counts(self) -> None:
        """Full classify receipt must include discipline_counts in metadata (Law #2)."""
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        data = _fixture_bytes("eng_c2_2_gsm.pdf")
        corr = "test-classify-receipt-full-" + str(uuid.uuid4())

        with patch(
            "aspire_orchestrator.services.blueprint.discipline_tagger.classify_sheet",
            return_value={"discipline": "C", "confidence": 0.97},
        ):
            with patch(
                "aspire_orchestrator.skillpacks.drew_blueprint.LlamaParseClient"
            ) as mock_llama:
                mock_llama.return_value.parse_pdf = MagicMock(
                    return_value={"pages": [{"page": 1, "text": "Civil Site Plan"}]}
                )
                ingest_corr = str(uuid.uuid4())
                ingest_result = drew.run_agentic_loop(
                    "INGEST",
                    {"pdf_bytes": data, "suite_id": SUITE_A},
                    ingest_corr,
                )
                drew.run_agentic_loop(
                    "CLASSIFY",
                    {"suite_id": SUITE_A, "project_id": ingest_result.get("project_id")},
                    corr,
                )

        receipts = _query_receipts_by_corr(corr)
        classify_receipts = [r for r in receipts if r["event_type"] == "blueprint.classify"]
        assert len(classify_receipts) == 1, "Expected exactly one blueprint.classify receipt"
        r = classify_receipts[0]
        meta = r.get("metadata") or {}
        assert "discipline_counts" in meta, (
            "blueprint.classify receipt must include discipline_counts in metadata"
        )
        assert "revisions" in meta, (
            "blueprint.classify receipt must include revisions list in metadata"
        )
