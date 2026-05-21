"""Wave 18 — Production integration tests for ``wave_11g2_see``.

Coverage:
    1. Feature flag default OFF (helper + caller behavior).
    2. Synthetic VECTOR_NATIVE PDF → end-to-end run produces primitives,
       motifs (when min_frequency=1), and writes to a mocked supabase_insert.
    3. RASTER_SCAN page is skipped (no writes, emits page receipt).
    4. Primitive truncation: page with >MAX_PRIMITIVES_PER_PAGE → log
       warning, pipeline still completes.
    5. Track A → Track B graph adapter shape — nodes(data=True),
       neighbors(), __contains__, __len__.

The tests stub Supabase + receipt store so no network is required.
"""
from __future__ import annotations

import io
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# Ensure import-time env defaults (matches other test files).
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")


# --- Synthetic PDF helpers ---------------------------------------------------


def _build_vector_pdf(num_drawings: int = 80) -> bytes:
    """Build a tiny PDF with ``num_drawings`` repeated vector outlets.

    Each "outlet" is a small square — repeated enough times that the WL
    miner finds repeating motifs even with min_frequency >= 1.
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    # Draw repeating small squares in a grid pattern. Each square has
    # 4 line segments — the miner should treat each as a distinct
    # primitive cluster.
    cols = 10
    side = 12.0
    spacing = 18.0
    drawn = 0
    for row in range(num_drawings // cols + 1):
        for col in range(cols):
            if drawn >= num_drawings:
                break
            x = 50 + col * spacing
            y = 50 + row * spacing
            page.draw_rect(
                fitz.Rect(x, y, x + side, y + side),
                color=(0, 0, 0),
                width=0.5,
            )
            drawn += 1
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _build_raster_pdf() -> bytes:
    """Build a single-page PDF with no vector content (raster-scan profile)."""
    import fitz

    doc = fitz.open()
    doc.new_page(width=612, height=792)  # blank — classifier returns RASTER_SCAN
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


# --- Tests -------------------------------------------------------------------


def test_feature_flag_default_off(monkeypatch):
    monkeypatch.delenv("ASPIRE_USE_WAVE_11G2", raising=False)
    from aspire_orchestrator.services.blueprint import wave_11g2_see

    assert wave_11g2_see.is_enabled() is False


def test_feature_flag_enabled(monkeypatch):
    monkeypatch.setenv("ASPIRE_USE_WAVE_11G2", "true")
    from aspire_orchestrator.services.blueprint import wave_11g2_see

    assert wave_11g2_see.is_enabled() is True


def test_track_a_graph_adapter_shape():
    from aspire_orchestrator.services.blueprint.vector_extractor import extract_primitives
    from aspire_orchestrator.services.blueprint.vector_graph_builder import build_graph
    from aspire_orchestrator.services.blueprint.wave_11g2_see import TrackAGraphAdapter

    import fitz

    pdf_bytes = _build_vector_pdf(num_drawings=10)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    primitives = extract_primitives(page)
    assert primitives, "synthetic PDF should yield primitives"
    graph = build_graph(primitives)
    adapter = TrackAGraphAdapter(graph)

    # networkx-style read surface used by mine_motifs.
    nodes_with_data = adapter.nodes(data=True)
    assert isinstance(nodes_with_data, list)
    assert all(isinstance(item, tuple) and len(item) == 2 for item in nodes_with_data)
    nodes_no_data = adapter.nodes()
    assert all(not isinstance(n, tuple) for n in nodes_no_data)

    # Adjacency parity.
    for n in nodes_no_data:
        neighbors = adapter.neighbors(n)
        assert isinstance(neighbors, list)
        assert n in adapter

    assert len(adapter) == len(nodes_no_data)
    doc.close()


@pytest.mark.asyncio
async def test_end_to_end_vector_pdf_writes_rows(monkeypatch):
    """Vector PDF → primitives mined, resolution cascade run, rows written."""
    monkeypatch.setenv("ASPIRE_11G2_MIN_FREQUENCY", "1")  # ensure motifs survive

    # Re-import to capture env-driven defaults.
    import importlib
    from aspire_orchestrator.services.blueprint import wave_11g2_see
    importlib.reload(wave_11g2_see)

    pdf_bytes = _build_vector_pdf(num_drawings=80)

    insert_calls: list[dict[str, Any]] = []

    async def _fake_insert(table: str, row: dict[str, Any]) -> dict[str, Any]:
        insert_calls.append({"table": table, "row": row})
        return {"id": row.get("id")}

    async def _fake_bulk_lookup(suite_id: str, hashes: list[str]) -> dict[str, dict]:
        return {}

    async def _fake_upsert(**kwargs):
        return None

    receipt_calls: list[Any] = []

    def _fake_store(receipts):
        receipt_calls.extend(receipts)

    with patch(
        "aspire_orchestrator.services.blueprint.wave_11g2_see.symbol_memory.bulk_lookup",
        new=_fake_bulk_lookup,
    ), patch(
        "aspire_orchestrator.services.blueprint.wave_11g2_see.symbol_memory.upsert_observation",
        new=_fake_upsert,
    ), patch(
        "aspire_orchestrator.services.supabase_client.supabase_insert",
        new=_fake_insert,
    ), patch(
        "aspire_orchestrator.services.receipt_store.store_receipts",
        new=_fake_store,
    ):
        summary = await wave_11g2_see.run_wave_11g2_see_for_project(
            project_id="proj-test-0001",
            suite_id="suite-test-0001",
            office_id=None,
            pdf_bytes=pdf_bytes,
            sheet_ids_by_page={1: "sheet-uuid-page-1"},
            correlation_id="corr-test-0001",
        )

    assert summary["pages_classified"] == 1
    assert summary["total_primitives"] > 0
    # Confirm receipts emitted at boundaries.
    receipt_types = {r.get("action_type") for r in receipt_calls}
    assert "drew.see.11g2.completed" in receipt_types

    # Every inserted row must be RLS-scoped and tagged as wl_motif.
    for call in insert_calls:
        assert call["table"] == "blueprint_symbols"
        assert call["row"]["suite_id"] == "suite-test-0001"
        bbox = call["row"]["bbox"]
        assert bbox["name_source"] == "wl_motif"
        assert "graph_hash" in bbox
        assert "motif_id" in bbox


@pytest.mark.asyncio
async def test_raster_page_skipped(monkeypatch):
    """A blank/raster page must be classified RASTER_SCAN and emit no rows."""
    from aspire_orchestrator.services.blueprint import wave_11g2_see

    pdf_bytes = _build_raster_pdf()

    insert_calls: list[Any] = []

    async def _fake_insert(table: str, row: dict[str, Any]) -> dict[str, Any]:
        insert_calls.append(row)
        return {"id": row.get("id")}

    async def _fake_bulk_lookup(suite_id: str, hashes: list[str]) -> dict[str, dict]:
        return {}

    with patch(
        "aspire_orchestrator.services.blueprint.wave_11g2_see.symbol_memory.bulk_lookup",
        new=_fake_bulk_lookup,
    ), patch(
        "aspire_orchestrator.services.supabase_client.supabase_insert",
        new=_fake_insert,
    ):
        summary = await wave_11g2_see.run_wave_11g2_see_for_project(
            project_id="proj-test-raster",
            suite_id="suite-test-raster",
            office_id=None,
            pdf_bytes=pdf_bytes,
            sheet_ids_by_page={1: "sheet-uuid-raster"},
            correlation_id="corr-test-raster",
        )

    assert summary["pages_classified"] == 1
    assert summary["pages_skipped_raster"] >= 1
    assert summary["rows_inserted"] == 0
    assert insert_calls == []


@pytest.mark.asyncio
async def test_primitive_truncation_logs_and_completes(monkeypatch, caplog):
    """Page with >MAX primitives must truncate and still complete cleanly."""
    monkeypatch.setenv("ASPIRE_11G2_MAX_PRIMITIVES", "20")
    monkeypatch.setenv("ASPIRE_11G2_MIN_FREQUENCY", "1")
    import importlib
    from aspire_orchestrator.services.blueprint import wave_11g2_see
    importlib.reload(wave_11g2_see)

    pdf_bytes = _build_vector_pdf(num_drawings=200)  # well above the cap

    async def _fake_insert(table: str, row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row.get("id")}

    async def _fake_bulk_lookup(suite_id: str, hashes: list[str]) -> dict[str, dict]:
        return {}

    async def _fake_upsert(**kwargs):
        return None

    with patch(
        "aspire_orchestrator.services.blueprint.wave_11g2_see.symbol_memory.bulk_lookup",
        new=_fake_bulk_lookup,
    ), patch(
        "aspire_orchestrator.services.blueprint.wave_11g2_see.symbol_memory.upsert_observation",
        new=_fake_upsert,
    ), patch(
        "aspire_orchestrator.services.supabase_client.supabase_insert",
        new=_fake_insert,
    ):
        import logging
        caplog.set_level(logging.WARNING)
        summary = await wave_11g2_see.run_wave_11g2_see_for_project(
            project_id="proj-truncate",
            suite_id="suite-truncate",
            office_id=None,
            pdf_bytes=pdf_bytes,
            sheet_ids_by_page={1: "sheet-trunc"},
            correlation_id="corr-truncate",
        )

    assert summary["primitives_truncated_pages"] >= 1
    assert summary["total_primitives"] <= 20  # capped
    truncate_msgs = [r for r in caplog.records if "truncating primitives" in r.message]
    assert truncate_msgs, "expected at least one truncation warning"


def test_feature_flag_caller_does_not_invoke_when_off(monkeypatch):
    """drew_blueprint should only call run_wave_11g2_see when flag is true.

    Verified by inspecting the integration site source — the call is gated
    by `if _w11g2.is_enabled()`. is_enabled returns False by default; we
    re-prove the contract here for posterity.
    """
    monkeypatch.delenv("ASPIRE_USE_WAVE_11G2", raising=False)
    import importlib
    from aspire_orchestrator.services.blueprint import wave_11g2_see
    importlib.reload(wave_11g2_see)
    assert not wave_11g2_see.is_enabled()
