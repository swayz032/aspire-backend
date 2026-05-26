"""Track B Pass 2.B — wave_11g-v2 page concurrency benchmark sweep.

Runs ``run_wave_11g2_see_for_project`` at concurrency = 1, 2, 4, 8 over a
fixture PDF, with all Supabase + receipt + tenant_memory I/O stubbed in
memory so the timing reflects per-page CPU + sleep-overlap behaviour, not
network latency to a live Supabase project.

Why an in-process benchmark:
    The reviewer-locked instruction is to pick the default concurrency
    empirically. We do not have a live Supabase or the Mangonia PDF in CI;
    this harness gives a deterministic, reproducible signal that anyone
    can re-run. Live timings against a real Supabase will be a follow-up
    on the Railway preview env.

Usage:
    python scripts/win_debug/bench_wave_11g2_concurrency.py [pdf_path]

Defaults to ``tests/fixtures/blueprints/gavnn_addendum_1.pdf`` (Gulfstream,
39 sheets) — the closest in-repo proxy for a dense multi-sheet workload.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

# Pre-set test-friendly env BEFORE importing orchestrator modules.
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "bench-only-not-a-real-key")


CONCURRENCY_LEVELS = (1, 2, 4, 8)
DEFAULT_PDF = ROOT / "tests" / "fixtures" / "blueprints" / "gavnn_addendum_1.pdf"


async def _stub_insert(_table: str, _row: dict[str, Any]) -> dict[str, Any]:
    # Simulate realistic Supabase REST round-trip (~25ms).
    await asyncio.sleep(0.025)
    return {"id": _row.get("id")}


async def _stub_bulk_lookup(_suite_id: str, _hashes: list[str]) -> dict[str, Any]:
    await asyncio.sleep(0.010)
    return {}


async def _stub_upsert_observation(**_kwargs: Any) -> None:
    await asyncio.sleep(0.005)


def _stub_emit_receipt(*_args: Any, **_kwargs: Any) -> None:
    return None


def _stub_emit_pa_receipt(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _run_once(pdf_bytes: bytes, sheet_ids: dict[int, str], concurrency: int) -> dict[str, Any]:
    os.environ["ASPIRE_DREW_SEE_PAGE_CONCURRENCY"] = str(concurrency)
    # Re-import lazily so the env var is picked up by the runtime read.
    from aspire_orchestrator.services.blueprint import wave_11g2_see as mod

    started = time.perf_counter()
    tracemalloc.start()

    with patch(
        "aspire_orchestrator.services.supabase_client.supabase_insert",
        side_effect=_stub_insert,
    ), patch(
        "aspire_orchestrator.services.blueprint.symbol_memory.bulk_lookup",
        side_effect=_stub_bulk_lookup,
    ), patch(
        "aspire_orchestrator.services.blueprint.symbol_memory.upsert_observation",
        side_effect=_stub_upsert_observation,
    ), patch.object(mod, "_emit_receipt", side_effect=_stub_emit_receipt), patch(
        "aspire_orchestrator.services.receipt_store.emit_persistence_asserted_receipt",
        side_effect=_stub_emit_pa_receipt,
    ):
        result = await mod.run_wave_11g2_see_for_project(
            project_id="00000000-0000-0000-0000-000000000bench",
            suite_id="00000000-0000-0000-0000-0000000bench01",
            office_id=None,
            pdf_bytes=pdf_bytes,
            sheet_ids_by_page=sheet_ids,
            correlation_id=f"bench-{concurrency}",
            discipline_by_page=None,
        )

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    wall_ms = int((time.perf_counter() - started) * 1000)
    return {
        "concurrency": concurrency,
        "wall_ms": wall_ms,
        "peak_mb": round(peak / (1024 * 1024), 1),
        "pages_processed": result.get("pages_processed", 0),
        "pages_classified": result.get("pages_classified", 0),
        "rows_inserted": result.get("rows_inserted", 0),
    }


async def _main(pdf_path: Path) -> int:
    if not pdf_path.exists():
        print(f"ERROR: fixture not found: {pdf_path}", file=sys.stderr)
        return 2

    pdf_bytes = pdf_path.read_bytes()
    print(f"benchmark fixture: {pdf_path.name} ({len(pdf_bytes) / 1024:.0f} KB)")

    # Force WAVE_11G2 on.
    os.environ["ASPIRE_USE_WAVE_11G2"] = "true"

    # Count pages once so we can build a synthetic sheet_id map.
    import fitz

    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        page_count = len(doc)
    sheet_ids = {i + 1: f"sheet-{i + 1:04d}" for i in range(page_count)}
    print(f"pages: {page_count}")

    rows = []
    for c in CONCURRENCY_LEVELS:
        # Run twice and take median to dampen warm-up noise.
        trials = []
        for _ in range(2):
            r = await _run_once(pdf_bytes, sheet_ids, c)
            trials.append(r)
        merged = dict(trials[0])
        merged["wall_ms"] = int(statistics.median(t["wall_ms"] for t in trials))
        merged["peak_mb"] = max(t["peak_mb"] for t in trials)
        rows.append(merged)
        print(
            f"  concurrency={c:>2}  wall_ms={merged['wall_ms']:>7d}  "
            f"peak_mb={merged['peak_mb']:>5.1f}  pages_processed={merged['pages_processed']}"
        )

    # Markdown table for the doc.
    print()
    print("| Concurrency | Wall-clock (ms) | Peak RAM (MB) | Pages processed |")
    print("|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['concurrency']} | {r['wall_ms']:,} | {r['peak_mb']} | {r['pages_processed']} |"
        )
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    raise SystemExit(asyncio.run(_main(target)))
