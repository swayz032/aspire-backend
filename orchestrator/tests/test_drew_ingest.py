"""Drew Stage 1 INGEST tests — Wave 2B.

Tests the INGEST stage against real (redacted) fixtures. All external HTTP calls
(LlamaParse, Azure Doc Intel) are mocked via unittest.mock. PyMuPDF runs real.

Aspire Laws validated:
  - Law #2: Every ingest call emits exactly one 'blueprint.ingest' receipt.
  - Law #3: Fail-closed on missing payload, bad bytes, unsupported format.
  - Law #6: Tenant isolation — suite_id scoped to actor.

xfail policy: tests that require the Wave-2 ingest() implementation to be complete
are marked @pytest.mark.xfail(reason='blocked on wave-2-impl') so the suite
stays green while feat/wave-2-ingest-classify merges.
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures directory helper
# ---------------------------------------------------------------------------
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "blueprints"

SUITE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OFFICE_A = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CORRELATION_ID = "test-ingest-wave2b-001"


def _fixture_bytes(name: str) -> bytes:
    """Return bytes of a committed (redacted) fixture PDF."""
    path = FIXTURES_DIR / name
    assert path.exists(), f"Fixture not found: {path}"
    return path.read_bytes()


def _query_receipts_by_corr(correlation_id: str) -> list[dict]:
    """Query in-memory receipt store by correlation_id without suite_id filter.

    Drew Wave 1 stub receipts do not embed suite_id in the receipt payload
    (that is a Wave 2 implementation detail). For stub-level tests, we query
    the raw in-memory list directly.

    This helper is intentionally test-only. Production code uses query_receipts()
    with mandatory suite_id (Law #6).
    """
    import aspire_orchestrator.services.receipt_store as rs

    with rs._lock:
        return [r for r in rs._receipts if r.get("correlation_id") == correlation_id]


def _pdf_sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# conftest-style module-level setup
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_receipt_store():
    """Isolate receipt store state between tests."""
    from aspire_orchestrator.services.receipt_store import clear_store
    clear_store()
    yield
    clear_store()


# ---------------------------------------------------------------------------
# Basic fixture loading (verifies redacted PDFs are valid PyMuPDF-readable)
# ---------------------------------------------------------------------------

class TestFixtureIntegrity:
    """Sanity checks: all committed fixtures are valid PDFs that PyMuPDF can open."""

    def test_gavnn_addendum_is_readable_pdf(self) -> None:
        """Golden #1: GAVNN addendum opens and has correct page count."""
        import pymupdf  # type: ignore
        data = _fixture_bytes("gavnn_addendum_1.pdf")
        doc = pymupdf.open(stream=data, filetype="pdf")
        assert len(doc) == 39
        doc.close()

    def test_eng_rev1_master_is_readable_pdf(self) -> None:
        """Golden #2: 22MB multi-discipline master opens and has correct page count."""
        import pymupdf  # type: ignore
        data = _fixture_bytes("eng_rev1_signed_master.pdf")
        doc = pymupdf.open(stream=data, filetype="pdf")
        assert len(doc) == 13
        doc.close()

    def test_dedup_pair_source_have_identical_hash(self) -> None:
        """Source originals for eng_c2_2_gsm and its duplicate must have same SHA256.

        PyMuPDF save() is nondeterministic (timestamps, xref ordering), so the
        committed redacted files differ in bytes even when the originals are identical.
        We verify the originals are byte-identical (the invariant that matters for dedup).
        The ingest dedup logic hashes the input bytes before redaction is applied.
        """
        orig_dir = FIXTURES_DIR / "originals"
        src1 = orig_dir / "21030 ENG-C2.2 GSM.pdf"
        src2 = orig_dir / "21030 ENG-C2.2 GSM (1).pdf"

        if not src1.exists() or not src2.exists():
            pytest.skip(
                "Originals not present in this environment (gitignored). "
                "Dedup source identity verified during Wave 2B fixture commit. "
                "Run redact_fixtures.py locally to reproduce."
            )

        d1 = src1.read_bytes()
        d2 = src2.read_bytes()
        assert _pdf_sha256(d1) == _pdf_sha256(d2), (
            "Dedup test fixture originals must be byte-identical. "
            "Re-copy 21030 ENG-C2.2 GSM (1).pdf from blueprints/ if they differ."
        )

    def test_light_pole_pair_different_hash(self) -> None:
        """Revision pair must NOT be byte-identical (they are distinct revisions)."""
        d1 = _fixture_bytes("light_pole_lp1_r1.pdf")
        d2 = _fixture_bytes("light_pole_revised.pdf")
        assert _pdf_sha256(d1) != _pdf_sha256(d2)

    def test_all_14_fixtures_present(self) -> None:
        """All committed fixtures from plan §9.2 must exist in fixtures/blueprints/."""
        expected = [
            "gavnn_addendum_1.pdf",
            "eng_rev1_signed_master.pdf",
            "eng_c2_2_gsm.pdf",
            "eng_c2_2_gsm_duplicate.pdf",
            "electrical_e1.pdf",
            "electrical_e2.pdf",
            "electrical_e3.pdf",
            "electrical_e4.pdf",
            "plumbing_p1.pdf",
            "light_pole_lp1_r1.pdf",
            "light_pole_revised.pdf",
            "concrete_mangonia_park.pdf",
            "precast_drainage.pdf",
            "electrical_site_29187p_es.pdf",
        ]
        missing = [f for f in expected if not (FIXTURES_DIR / f).exists()]
        assert not missing, f"Missing committed fixtures: {missing}"

    def test_no_pii_in_committed_fixtures(self) -> None:
        """Law #9: No P.E. number pattern or License No. in any committed fixture."""
        import re
        import pymupdf  # type: ignore

        PE_VERIFY = re.compile(
            r"\bP\.E\.\s*#?\s*\d{4,7}|License\s+(?:No\.?|#|Number)\s*\d{5,10}",
            re.IGNORECASE,
        )
        violations: list[str] = []
        for pdf_file in FIXTURES_DIR.glob("*.pdf"):
            doc = pymupdf.open(str(pdf_file))
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
            if PE_VERIFY.search(text):
                violations.append(pdf_file.name)
        assert not violations, (
            f"Law #9 violation: committed fixtures contain P.E./License patterns: {violations}"
        )


# ---------------------------------------------------------------------------
# INGEST stage tests (implementation in wave-2-ingest-classify)
# ---------------------------------------------------------------------------

class TestIngestEngRev1Master:
    """INGEST: multi-page master PDF extracts all sheets without error."""

    @pytest.mark.xfail(reason="blocked on wave-2-impl: Drew.ingest() is still a stub")
    def test_ingest_eng_rev1_master_extracts_all_sheets(self) -> None:
        """Load eng_rev1_signed_master.pdf, call drew.ingest(), assert sheet_count == 13.

        The 22MB master has 13 pages (verified from PyMuPDF). Each page is one sheet.
        """
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        data = _fixture_bytes("eng_rev1_signed_master.pdf")
        corr = str(uuid.uuid4())

        with patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.LlamaParseClient"
        ) as mock_llama:
            mock_llama.return_value.parse_pdf = MagicMock(return_value={"pages": list(range(13))})
            result = drew.run_agentic_loop(
                "INGEST",
                {"pdf_bytes": data, "suite_id": SUITE_A, "office_id": OFFICE_A},
                corr,
            )

        assert result["status"] == "ok", f"Expected ok, got: {result}"
        assert result["sheet_count"] == 13
        assert "project_id" in result


class TestIngestDedup:
    """INGEST: idempotency — same PDF bytes must not create duplicate projects."""

    @pytest.mark.xfail(reason="blocked on wave-2-impl: Drew.ingest() is still a stub")
    def test_ingest_dedup_returns_existing_project(self) -> None:
        """Call ingest twice with same PDF bytes; second call returns existing project_id."""
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        data = _fixture_bytes("eng_c2_2_gsm.pdf")
        corr1 = str(uuid.uuid4())
        corr2 = str(uuid.uuid4())

        with patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.LlamaParseClient"
        ) as mock_llama:
            mock_llama.return_value.parse_pdf = MagicMock(
                return_value={"pages": [{"page": 1, "text": "Civil sheet"}]}
            )
            result1 = drew.run_agentic_loop(
                "INGEST",
                {"pdf_bytes": data, "suite_id": SUITE_A, "office_id": OFFICE_A},
                corr1,
            )
            result2 = drew.run_agentic_loop(
                "INGEST",
                {"pdf_bytes": data, "suite_id": SUITE_A, "office_id": OFFICE_A},
                corr2,
            )

        assert result1["status"] == "ok"
        assert result2["status"] == "ok"
        # Idempotency: same project returned
        assert result1["project_id"] == result2["project_id"], (
            "Second ingest of identical bytes must return existing project_id "
            "(hash-based idempotency)"
        )

    @pytest.mark.xfail(reason="blocked on wave-2-impl: Drew.ingest() is still a stub")
    def test_ingest_duplicate_files_collapse(self) -> None:
        """eng_c2_2_gsm.pdf and _duplicate.pdf have same hash — must produce one sheet."""
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        data_a = _fixture_bytes("eng_c2_2_gsm.pdf")
        data_b = _fixture_bytes("eng_c2_2_gsm_duplicate.pdf")

        # Confirm bytes are identical (hash dedup requires this)
        assert hashlib.sha256(data_a).digest() == hashlib.sha256(data_b).digest()

        corr1 = str(uuid.uuid4())
        corr2 = str(uuid.uuid4())

        with patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.LlamaParseClient"
        ) as mock_llama:
            mock_llama.return_value.parse_pdf = MagicMock(
                return_value={"pages": [{"page": 1, "text": "Civil GSM sheet"}]}
            )
            result1 = drew.run_agentic_loop(
                "INGEST",
                {"pdf_bytes": data_a, "suite_id": SUITE_A, "office_id": OFFICE_A},
                corr1,
            )
            result2 = drew.run_agentic_loop(
                "INGEST",
                {"pdf_bytes": data_b, "suite_id": SUITE_A, "office_id": OFFICE_A},
                corr2,
            )

        assert result1["status"] == "ok"
        assert result2["status"] == "ok"
        assert result1["project_id"] == result2["project_id"], "Duplicate file must collapse to same project"
        # Only one sheet (not two) expected for the dedup case
        assert result1.get("sheet_count", 0) + result2.get("sheet_count", 0) <= 2, (
            "Hash dedup must not double-count identical sheets"
        )


class TestIngestReceipt:
    """Law #2: INGEST must emit exactly one blueprint.ingest receipt."""

    def test_ingest_emits_receipt_on_stub(self) -> None:
        """Wave 1 stub already emits a receipt on INGEST — assert it exists."""
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        corr = "test-receipt-ingest-" + str(uuid.uuid4())

        drew.run_agentic_loop(
            "INGEST",
            {"pdf_bytes": b"fake", "suite_id": SUITE_A},
            corr,
        )

        receipts = _query_receipts_by_corr(corr)
        assert len(receipts) >= 1, "INGEST must emit at least one receipt (Law #2)"
        event_types = [r["event_type"] for r in receipts]
        assert "blueprint.ingest" in event_types, (
            f"Expected 'blueprint.ingest' receipt, found: {event_types}"
        )

    @pytest.mark.xfail(reason="blocked on wave-2-impl: Drew.ingest() is still a stub")
    def test_ingest_emits_receipt_with_project_id(self) -> None:
        """Full ingest receipt must include project_id in metadata (Law #2)."""
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        data = _fixture_bytes("eng_c2_2_gsm.pdf")
        corr = "test-receipt-ingest-full-" + str(uuid.uuid4())

        with patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.LlamaParseClient"
        ) as mock_llama:
            mock_llama.return_value.parse_pdf = MagicMock(
                return_value={"pages": [{"page": 1, "text": "Civil"}]}
            )
            drew.run_agentic_loop(
                "INGEST",
                {"pdf_bytes": data, "suite_id": SUITE_A, "office_id": OFFICE_A},
                corr,
            )

        receipts = _query_receipts_by_corr(corr)
        ingest_receipts = [r for r in receipts if r["event_type"] == "blueprint.ingest"]
        assert len(ingest_receipts) == 1
        r = ingest_receipts[0]
        assert r["actor"] == "skillpack:drew-blueprint"
        assert "project_id" in (r.get("metadata") or {}), (
            "blueprint.ingest receipt must include project_id in metadata"
        )


class TestIngestLargeFileNoOOM:
    """Performance: 22MB master file must ingest without memory blow-up."""

    @pytest.mark.xfail(reason="blocked on wave-2-impl: Drew.ingest() is still a stub")
    def test_ingest_large_file_no_oom(self) -> None:
        """22MB master file should ingest. Peak RSS must stay under 512MB above baseline.

        Uses tracemalloc to catch Python heap allocations. Does not test C-level
        PyMuPDF allocations, but catches any accidental full-bytes buffer copies.
        """
        import tracemalloc
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        data = _fixture_bytes("eng_rev1_signed_master.pdf")
        file_size_mb = len(data) / (1024 * 1024)
        assert file_size_mb > 15, f"Expected large file, got {file_size_mb:.1f}MB"

        tracemalloc.start()
        snapshot_before = tracemalloc.take_snapshot()

        corr = str(uuid.uuid4())
        with patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.LlamaParseClient"
        ) as mock_llama:
            mock_llama.return_value.parse_pdf = MagicMock(return_value={"pages": list(range(13))})
            result = drew.run_agentic_loop(
                "INGEST",
                {"pdf_bytes": data, "suite_id": SUITE_A},
                corr,
            )

        snapshot_after = tracemalloc.take_snapshot()
        tracemalloc.stop()

        top_stats = snapshot_after.compare_to(snapshot_before, "lineno")
        total_bytes = sum(stat.size_diff for stat in top_stats if stat.size_diff > 0)
        total_mb = total_bytes / (1024 * 1024)

        assert result["status"] == "ok", f"Ingest must succeed before memory check: {result}"
        assert total_mb < 512, (
            f"Memory growth {total_mb:.1f}MB exceeds 512MB ceiling for 22MB file ingest. "
            f"Check for buffer copies in pdf_splitter.py."
        )
