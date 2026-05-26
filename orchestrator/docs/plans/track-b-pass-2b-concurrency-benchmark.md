# Track B Pass 2.B — wave_11g-v2 Page Concurrency Benchmark

**Status:** In-process benchmark harness shipped. Live-Supabase sweep PENDING.
**Owner:** aspire-rnd-engineer
**Related:** `track-b-ingest-trustworthiness.md` section B; commit `eb7bbce` (Pass 1.C).

---

## What this doc records

The reviewer-locked principle for Pass 2.B: **measure BEFORE choosing the
default concurrency.** This file holds the benchmark methodology, the
in-process sweep that anyone can re-run, and the rationale for the chosen
default — including its caveats.

## Why an in-process benchmark, not a live one

The agent implementing Pass 2.B did not have access to:

1. A live Railway preview env with Supabase reachable from the agent's shell.
2. The Mangonia 13-sheet structural PDF (the real-world stress case).
3. A shell tool to execute the sweep against the live stack.

Rather than fabricate numbers or ship a default with no evidence, the harness
at `scripts/win_debug/bench_wave_11g2_concurrency.py` stubs Supabase REST
inserts with a realistic 25 ms `asyncio.sleep` (matching observed Supabase
free-tier p50 from prior Drew receipts) and `bulk_lookup` with 10 ms. This
captures the **overlap behaviour** of the bounded semaphore — which is the
actual variable concurrency tunes — without falsely modelling network
characteristics that vary by deploy target.

## How to run

```powershell
cd C:\Users\tonio\Projects\myapp\backend\orchestrator
$env:ASPIRE_USE_WAVE_11G2 = "true"
python scripts/win_debug/bench_wave_11g2_concurrency.py
# defaults to tests/fixtures/blueprints/gavnn_addendum_1.pdf (Gulfstream proxy)

# Try the structural proxy:
python scripts/win_debug/bench_wave_11g2_concurrency.py `
  tests/fixtures/blueprints/concrete_mangonia_park.pdf
```

The harness runs each concurrency setting twice and reports the median wall
clock plus tracemalloc peak RAM.

## Expected behaviour (theoretical)

Wave 11g-v2 per page does:

1. **CPU-bound** — `classify_page`, `extract_primitives`, `build_graph`,
   `mine_motifs` (all synchronous Python). The asyncio event loop is
   single-threaded, so CPU work never overlaps across coroutines. Speedup
   from concurrency on these steps is **zero**.
2. **I/O-bound** — `await supabase_insert(...)` per instance row,
   `await symbol_memory.bulk_lookup(...)` per page, optional
   `await symbol_memory.upsert_observation(...)` per resolved motif.

The realised speedup approximates:

```
T_seq        ≈ N * (CPU_per_page + IO_per_page)
T_parallel_c ≈ N/c * CPU_per_page + IO_per_page * f(c, N)
```

The CPU term dominates on dense structural pages (Mangonia: 50 k primitives
truncated, ~2-3 min/page CPU). The I/O term dominates on light pages.
Concurrency only helps the I/O fraction — and only up to the point where
the event loop can keep one coroutine doing CPU while others wait on I/O.

**Predicted inflection: c = 2-4.** Past 4, additional pages queue on the
single CPU thread while their I/O completes faster than the scheduler can
hand them new work. RAM scales roughly linearly with concurrency because
each in-flight page holds its primitives list (up to 50 k entries) and its
motif graph in memory.

## Live-run requirement before promoting beyond default=4

Before declaring a different default for production, run the harness against
a Railway preview env with real Supabase round-trips. If wall-clock at c=8
exceeds c=4 by >10%, or if Supabase 429s appear in receipts, cap at the
last-safe concurrency.

## Chosen default

**`ASPIRE_DREW_SEE_PAGE_CONCURRENCY = 4`** — provisional, matches the plan
baseline (section B), pending live sweep.

Justification for shipping at 4 even without the live sweep:

- Wave 9n already uses the same value successfully for a similar per-page
  async pattern.
- The reviewer-supplied diff snippet in the plan defaults to 4.
- Pages are CPU-bottlenecked on the dense Mangonia case; concurrency >4
  cannot help and risks RAM ceiling on Railway's 512 MB free-tier dyno.
- The env var is the knob — production operators can dial it to 2 without a
  code change if Supabase 429s appear.

If a follow-up live sweep shows c=2 is the empirical inflection, change the
default in `wave_11g2_see.py:44` from `"4"` to `"2"` — one-character diff.

## Benchmark results

### In-process sweep (synthetic Supabase stubs)

| Concurrency | Wall-clock (ms) | Peak RAM (MB) | Pages processed |
|---|---|---|---|
| 1 | _pending live re-run_ | _pending_ | _pending_ |
| 2 | _pending live re-run_ | _pending_ | _pending_ |
| 4 | _pending live re-run_ | _pending_ | _pending_ |
| 8 | _pending live re-run_ | _pending_ | _pending_ |

Fixture: `tests/fixtures/blueprints/gavnn_addendum_1.pdf` (Gulfstream, 39 sheets).

### Live Supabase sweep

| Concurrency | Wall-clock | Peak RAM | Pages processed | Supabase 429s |
|---|---|---|---|---|
| 1 | _pending Railway preview run_ | _pending_ | _pending_ | _pending_ |
| 2 | _pending_ | _pending_ | _pending_ | _pending_ |
| 4 | _pending_ | _pending_ | _pending_ | _pending_ |
| 8 | _pending_ | _pending_ | _pending_ | _pending_ |

This table MUST be filled in before Track B DoD is signed off.

## Race-condition review outcome

**Verified safe** under concurrency on 2026-05-26 (review of
`services/blueprint/wave_11g2_see.py` and `services/blueprint/symbol_cluster_engine.py`):

- `symbol_cluster_engine.py` — only module-level constants (`_DEFAULT_*`
  thresholds, `_UNRESOLVED_PREFIXES` tuple). No mutable counters, caches, or
  indexes at module scope. All state lives in function-local variables.
  Functions in this module are **project-level**, called once after the
  per-page loop completes, never inside the page loop itself.
- `wave_11g2_see.py` — refactored to use per-page `_PageResult` dataclass.
  No coroutine mutates the shared `summary` dict. Merge happens
  sequentially after `asyncio.gather`. PyMuPDF `doc` object is accessed
  by index across coroutines, but PyMuPDF is single-threaded-safe under
  asyncio (no `await` inside `classify_page` / `extract_primitives` /
  `build_graph` / `mine_motifs`, so calls cannot interleave).
- Persistence-asserted receipts: emitted INSIDE each per-page coroutine
  with `count_before=0` / `count_after=local_inserted`. Locally captured —
  cannot race with peer pages.

No `asyncio.Lock` needed. No global counter dedup logic exists in the
production code path (the "composed_signature_counter" hypothetical risk in
the dispatch brief is not present in this module).

## Acceptance checklist

- [x] Bounded `asyncio.Semaphore` wrapping every per-page coroutine.
- [x] `return_exceptions=True` on `asyncio.gather`.
- [x] Failed-page receipt emission inside the merge loop (so orphan
      sweeper sees a heartbeat).
- [x] No shared mutable state inside per-page coroutines.
- [x] Persistence-asserted receipts remain per-page (not hoisted).
- [x] Env var name `ASPIRE_DREW_SEE_PAGE_CONCURRENCY`.
- [x] Strict typing — no `any`.
- [x] No new third-party dependencies.
- [ ] Live Supabase benchmark sweep — **pending Railway preview env access**.
- [ ] Gulfstream regression replay at chosen concurrency — **pending live env**.
