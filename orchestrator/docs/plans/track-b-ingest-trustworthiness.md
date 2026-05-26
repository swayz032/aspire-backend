# Track B — Ingest Trustworthiness (Phase 5 Generalization Unblocker)

**Status:** Approved 2026-05-26
**Owner:** Aspire backend
**Repo:** `swayz032/aspire-backend`
**Phase 5 dependency:** Mangonia cross-project audit blocked on this entire track.
**Definition of Done:** Mangonia full bundle ingests cleanly with sheets > 0, primitives > 0, intelligence > 0, motifs > 0, legends > 0, AND all receipt SUCCEEDED counts agree with DB row deltas.

## Why this track exists

Phase 5 (`Phase5_v1_validated` at `9ad438e`) proved the OCR × Geometry × Topology
architecture works on Gulfstream MEP. Attempting the same audit on Mangonia
structural surfaced **three pipeline trustworthiness bugs that block any cross-firm
generalization test**:

| # | Bug | User-visible symptom |
|---|---|---|
| 1 | Orphan sweeper kills actively-progressing SEE | Drew shows "vision model error" on dense PDFs |
| 2 | wave_11g-v2 page loop sequential, not parallel | 30+ min wall-clock on dense sheets |
| 3 | Receipt SUCCEEDED while DB rows = 0 (function-call path) | Silent persistence loss |

Reviewer-locked principle (2026-05-26): **never tell users to upload a smaller PDF.**
Production-ready = handles whatever GCs actually have. See
`memory/feedback_no_smaller_pdfs.md`.

## Dependency-ordered sequence

Reviewer-locked: implement **C → B → A**, not severity order.

### Why this order

- **C first** — you can't measure A or B impact without trustworthy telemetry.
  Persistence assertions make every subsequent change verifiable.
- **B second** — page parallelism may eliminate ~80% of apparent orphan conditions
  by itself, narrowing what A actually needs to handle.
- **A last** — tune orphan detection against the new, stable, fast timings —
  not against today's broken timings.

---

## C. Persistence Assertions (architectural integrity)

### Spec

A stage that writes rows must FAIL its receipt if zero rows were written.
A stage that writes rows but legitimately wrote zero (rare edge case) must
emit a distinct status: `SUCCEEDED_WITH_ZERO_OUTPUT`.

### State machine change

Before:

```
SUCCEEDED        — emitted unconditionally if no exception raised
FAILED           — explicit error
PENDING          — in-progress
```

After (for write-producing stages):

```
SUCCEEDED                    — wrote >= expected_min rows
SUCCEEDED_WITH_ZERO_OUTPUT   — wrote 0 rows but no exception (rare; flagged)
FAILED                       — error OR wrote 0 rows when >0 expected
PENDING                      — in-progress
```

### Write-producing stage taxonomy

**CORRECTED 2026-05-26 per receipt-ledger-auditor findings.** The original plan
had three table-name errors. Production Drew writes to `blueprint_symbols`, NOT
`blueprint_primitives`. `blueprint_primitives` is a separate CSIR research table
populated only by standalone scripts (extract_primitives_vtpl.py, sdpe_run.py,
etc.) — not by the production pipeline.

| Stage receipt | Expected rows table | expected_min | Notes |
|---|---|---|---|
| `provider.azure.sheet_layout` (RENAMED from `blueprint.ingest.azure_doc_intel_used`) | `blueprint_sheet_intelligence` | 1 | Per-sheet. `blueprint.ingest.azure_doc_intel_used` is OCR-text-extraction only; it doesn't write to `blueprint_sheet_intelligence`. The sheet-layout receipt is the correct anchor. |
| `drew.see.legend_extracted` | `blueprint_project_legends` | 0 | Per-project. Projects without legend pages legitimately write 0. Gulfstream wrote 288. |
| `drew.see.11g2.page` | `blueprint_symbols` (NOT blueprint_primitives) | 1 if `metadata.skipped != "raster_scan"`, else 0 | Per-page. RASTER_SCAN pages emit a receipt with 0 rows by design — need expected_min=0 exemption. Also, B-PRE-1/B-PRE-2 follow-ups: zero-primitive and zero-motif vector pages currently emit NO receipt — needs separate fix. |
| `drew.see.11g2.completed` | `blueprint_symbols` | 0 | Per-project summary. Legitimate zero if all pages raster or no motifs meet min_frequency. |
| `blueprint.see.completed` | `blueprint_symbols` (NOT blueprint_primitives) | 100 | Per-project total. Gulfstream = 1.28M symbols, trivially passes. |
| `drew.see.spatial_binding` | `blueprint_symbol_memory` (advisory) | 0 | Per-project. Write count not currently captured in receipt metadata (B-PRE-3 follow-up). Assertion is advisory only. |

### Follow-up bugs filed (NOT blocking Pass 1.C — file as separate tasks)

- **B-PRE-1:** `wave_11g2_see.py:368` silently skips receipt emission for zero-primitive vector pages. Will create heartbeat gaps for Pass 3.A sweeper.
- **B-PRE-2:** `wave_11g2_see.py:398` silently skips receipt emission for zero-motif pages. Same heartbeat impact.
- **B-PRE-3:** `drew.see.spatial_binding` lacks `write_count` field. Advisory-only assertion until added.

### What MUST happen before wire-in

The implementing agent MUST use the corrected table names above. Wiring against
the original `blueprint_primitives` references would cause every healthy
SUCCEEDED receipt to flip to FAILED (delta=0 on a table the pipeline doesn't
write to). Gulfstream's 1.28M `blueprint_symbols` rows are the actual write
target — `blueprint_primitives` is empty for production projects.

`expected_min = 0` stages emit `SUCCEEDED_WITH_ZERO_OUTPUT` instead of `SUCCEEDED`
when they write zero rows — so the observability signal is preserved without
forcing a false-positive failure.

### Implementation

**New helper:** `services/receipt_store.py` — `emit_persistence_asserted_receipt(stage, expected_table, expected_min, count_before, count_after, ...)`.

Each call signature:
```python
async def emit_persistence_asserted_receipt(
    stage: str,
    expected_table: str,
    expected_min: int,
    count_before: int,           # row count of (project_id, suite_id) before stage
    count_after: int,            # row count after stage
    correlation_id: str,
    suite_id: str,
    redacted_outputs: dict,
) -> ReceiptStatus:
    delta = count_after - count_before
    if delta >= expected_min and expected_min > 0:
        return SUCCEEDED
    if delta == 0 and expected_min == 0:
        return SUCCEEDED_WITH_ZERO_OUTPUT
    return FAILED(reason=f"persistence_assertion: {expected_table} delta={delta} < expected_min={expected_min}")
```

**Wire-in:** Find every `_cut_receipt(...status="SUCCEEDED", action_type="<stage>")` call
in `routes/blueprint_dispatch.py` and `services/blueprint/*` that touches a
write-producing stage. Wrap it with the asserted helper.

**Migration:** Add `SUCCEEDED_WITH_ZERO_OUTPUT` to the `receipts.status` CHECK
constraint (if any). Verify with `mcp__supabase__list_tables` first.

### Files touched (estimated)

- `src/aspire_orchestrator/services/receipt_store.py` (+helper)
- `src/aspire_orchestrator/routes/blueprint_dispatch.py` (+5-8 call site wraps)
- `src/aspire_orchestrator/services/blueprint/wave_11g2_see.py` (+page assertion)
- `src/aspire_orchestrator/services/blueprint/universal_sheet_extractor.py` (+intel assertion)
- `src/aspire_orchestrator/services/blueprint/legend_extractor.py` (+legend assertion — `expected_min=0`)
- `supabase/migrations/<date>_receipts_status_enum_extension.sql` (if CHECK exists)

### Tests

- **Unit:** mock count_before / count_after, assert correct status emitted across all 3 branches
- **Integration (Gulfstream replay):** re-run Gulfstream's ingest; all stages still emit `SUCCEEDED` (regression — counts haven't changed)
- **Synthetic failure:** force a stage to write zero rows (e.g., disconnect Supabase mid-write); assert receipt is `FAILED`, not `SUCCEEDED`
- **Negative:** Mangonia replay (after B + A) should show legend_extracted with `SUCCEEDED_WITH_ZERO_OUTPUT` on sheets that legitimately have no legend, not `SUCCEEDED` masking a write failure

### Verification

- `receipt-ledger-auditor` agent — re-audit 100% receipt coverage holds on Gulfstream
- `aspire-policy-gate` agent — verify governance: zero rows on write-stage = FAILED unless explicitly excepted

---

## B. wave_11g-v2 Page Parallelism (performance fix)

### Spec

The `drew.see.11g2.page` loop currently processes pages sequentially. Pages
are independent extractions (no cross-page state). Parallelize with
`asyncio.gather` bounded by a semaphore.

### Knobs

```python
ASPIRE_DREW_SEE_PAGE_CONCURRENCY   default 4   (env var, mirror Wave 9n)
```

### Benchmark first, choose default empirically

Per reviewer: do not assume 4 is optimal until measured. Run benchmark on
Mangonia (13 dense pages) and Gulfstream subset (5 sheets) at concurrency
{1, 2, 4, 8} and record:

| Concurrency | Gulfstream-5sheet wall-clock | Mangonia-13sheet wall-clock | Peak RAM | Notes |
|---|---|---|---|---|
| 1 | baseline | baseline | baseline | sequential ref |
| 2 |  |  |  |  |
| 4 |  |  |  |  |
| 8 |  |  |  |  |

Choose default based on the inflection point — diminishing returns AND/OR RAM blow-up boundary.

### Implementation

**Find the sequential loop** — likely in `services/blueprint/wave_11g2_see.py`
inside a function like `extract_all_pages` or `process_sheets`.

Replace:
```python
for sheet in sheets:
    await _process_page(sheet)
```

With:
```python
sem = asyncio.Semaphore(int(os.getenv("ASPIRE_DREW_SEE_PAGE_CONCURRENCY", "4")))

async def _bounded(sheet):
    async with sem:
        return await _process_page(sheet)

results = await asyncio.gather(*[_bounded(s) for s in sheets], return_exceptions=True)
# Handle per-page exceptions individually — one failed page must not kill the batch
for sheet, result in zip(sheets, results):
    if isinstance(result, Exception):
        logger.error("wave_11g2: page failed sheet=%s err=%s", sheet.id, result)
        # Emit failure receipt for THIS sheet, continue with others
```

### Concurrency correctness

- Each page does its own Supabase writes — Supabase REST handles concurrent
  inserts fine (the orchestrator already uses an `httpx.AsyncClient` pool)
- Verify with `aspire-policy-gate` — no shared mutable state across page handlers
  (e.g., a global `composed_signature_counter` would be a bug)
- Per-page write failures must not roll back peer pages (`return_exceptions=True`)

### Files touched

- `src/aspire_orchestrator/services/blueprint/wave_11g2_see.py` (the loop)
- `src/aspire_orchestrator/services/blueprint/symbol_cluster_engine.py` (verify no global state)
- `tests/blueprint/test_wave_11g2_concurrency.py` (NEW — synthetic 10-page parallel test)

### Tests

- **Unit:** mock 10 pages with deterministic sleep, assert wall-clock at concurrency=4 is <= 35% of sequential
- **Integration:** Gulfstream re-ingest under each concurrency setting; assert primitive counts identical
- **Stress:** Mangonia ingest at concurrency=4, assert <15 min wall-clock and no OOM

### Verification

- `release-sre` agent — benchmark table populated, default chosen empirically
- `aspire-test-engineer` — concurrency-safety tests at 1/2/4/8 all pass
- `output-critic` — Framer-grade UI test: Mangonia upload, user sees per-page progress, NOT a 30-min stall

---

## A. Sweeper Heartbeat (orphan detection correctness)

### Spec

The orphan sweeper currently kills SEE stages stuck `in_progress` for >8 min.
This false-positives on dense sheets where SEE is healthy but slow.

New rule: a stage is orphaned ONLY if:
1. `stage_progress.<stage>` = `in_progress`, AND
2. NO `<stage>` progress receipts have arrived in the orphan_window
3. AND wall-clock since stage_started > absolute_max_window

### State machine

```
in_progress + recent page/progress receipts within heartbeat_window  -> HEALTHY
in_progress + no receipts within heartbeat_window                     -> ORPHAN
in_progress + total_elapsed > absolute_max                            -> ORPHAN (hard ceiling)
completed                                                              -> SUCCESS
failed                                                                 -> FAILED
```

### Knobs (env var, per-stage)

```
ASPIRE_SWEEPER_SEE_HEARTBEAT_MIN          default 5   (no page receipt in 5 min => orphan)
ASPIRE_SWEEPER_SEE_ABSOLUTE_MAX_MIN       default 60  (hard ceiling regardless of heartbeat)
ASPIRE_SWEEPER_REASON_HEARTBEAT_MIN       default 3
ASPIRE_SWEEPER_REASON_ABSOLUTE_MAX_MIN    default 15
```

### Implementation

**Locate sweeper:** likely `services/blueprint/sweeper.py` or a background task in
`services/job_orchestrator.py`. Find the rule that emits `blueprint.pipeline.timed_out`
with `error_class=OrphanTimeout`.

Replace `if elapsed_minutes > threshold:` with a heartbeat-aware check:

```python
async def is_orphaned(project_id: str, stage: str, suite_id: str) -> tuple[bool, str]:
    stage_started_at = await _get_stage_started(project_id, stage)
    total_elapsed = now() - stage_started_at

    absolute_max = int(os.getenv(f"ASPIRE_SWEEPER_{stage.upper()}_ABSOLUTE_MAX_MIN", "60"))
    if total_elapsed > timedelta(minutes=absolute_max):
        return True, f"absolute_max_exceeded ({absolute_max} min)"

    # Heartbeat check: any progress receipts in the last heartbeat_window?
    heartbeat = int(os.getenv(f"ASPIRE_SWEEPER_{stage.upper()}_HEARTBEAT_MIN", "5"))
    progress_stages = STAGE_PROGRESS_RECEIPT_PATTERNS[stage]  # e.g., ['drew.see.11g2.page']
    latest_progress = await _latest_progress_receipt(project_id, progress_stages)

    if latest_progress is None:
        # Never received any progress — use stage_started_at as floor
        if total_elapsed > timedelta(minutes=heartbeat):
            return True, f"no_heartbeat_since_start ({heartbeat} min)"
        return False, "in_progress_no_heartbeat_yet"

    silence = now() - latest_progress
    if silence > timedelta(minutes=heartbeat):
        return True, f"silent_{int(silence.total_seconds())}s (heartbeat={heartbeat} min)"

    return False, f"healthy_last_heartbeat_{int(silence.total_seconds())}s_ago"
```

### Stage → progress receipt mapping

```python
STAGE_PROGRESS_RECEIPT_PATTERNS = {
    "see":      ["drew.see.11g2.page", "drew.see.spatial_binding", "blueprint.see.vgse.completed"],
    "reason":   ["reason.sheet_intelligence_context", "drew.reason.assembly_typed"],
    "ingest":   ["blueprint.ingest.azure_doc_intel_used", "blueprint.ingest.dlp_scanned"],
    "classify": ["drew.classify.atlas.matched", "drew.classify.verified"],
    "procure":  ["drew.procure.supplier_lookup"],
}
```

### Files touched

- `src/aspire_orchestrator/services/blueprint/sweeper.py` (rewrite is_orphaned logic)
- `src/aspire_orchestrator/config/settings.py` (+5 env vars with defaults)
- `tests/blueprint/test_sweeper_heartbeat.py` (NEW)

### Tests

- **Unit:**
  - Stage in_progress, fresh page receipt 30s ago → NOT orphaned
  - Stage in_progress, no receipt in 10 min → ORPHANED (heartbeat exceeded)
  - Stage in_progress, total 65 min elapsed but recent heartbeats → ORPHANED (absolute_max)
  - Stage in_progress, no receipts ever, started 2 min ago → NOT orphaned (within initial grace)
- **Integration replay:** Run sweeper against Mangonia's ca3a21b7 historical data — assert it would NOT have killed at 11 min given that drew.see.11g2.page was at 13:09:15

### Verification

- `aspire-policy-gate` — orphan detection still fail-closed: when in doubt, kill the job
- `receipt-ledger-auditor` — sweeper-kill receipts include heartbeat diagnostic in `error_message`
- Live test: re-upload Mangonia, watch sweeper allow it to complete

---

## Cross-cutting: Definition of Done

Track B is DONE when:

1. **Mangonia full bundle ingests cleanly** via Desktop HTTP path (no smaller PDF workarounds)
2. All counts > 0:
   ```
   blueprint_sheets               > 0
   blueprint_primitives           > 0
   blueprint_sheet_intelligence   > 0
   blueprint_composed_symbols     > 0
   blueprint_project_legends      > 0  (or SUCCEEDED_WITH_ZERO_OUTPUT honestly)
   ```
3. **Receipts agree with DB counts.** No `SUCCEEDED` for write-producing stage with zero rows.
4. **Wall-clock < 15 min** for Mangonia at chosen concurrency default.
5. **Gulfstream regression clean** — re-ingest produces identical primitive count (1.28M ± 5%).
6. All Track-B test suites green.

Once DoD passes → **N-4.10 cross-project audit unblocks immediately.**

## Sequencing

```
C (persistence assertions)
    ↓
    [verify Gulfstream regression clean]
    ↓
B (page parallelism)
    ↓
    [benchmark concurrency 1/2/4/8, pick default]
    ↓
    [verify Gulfstream regression clean at chosen concurrency]
    ↓
A (sweeper heartbeat)
    ↓
    [replay Mangonia historical against new sweeper]
    ↓
    [live Mangonia retry — must succeed end-to-end]
    ↓
DoD signoff -> N-4.10 unblocks
```

## Risk register

| Risk | Mitigation |
|---|---|
| C breaks Gulfstream regression because some stage legitimately writes 0 in normal operation | Audit Gulfstream's 1.28M-primitive ingest receipts first. Identify stages with legit zero-output paths. Use `expected_min=0` for those. |
| B causes Supabase rate-limiting under concurrency=8 | Stop the benchmark sweep if 429s appear. Cap concurrency at last-safe value. |
| B reveals shared mutable state (e.g., counter, cache) that breaks at concurrency>1 | `aspire-policy-gate` reviews symbol_cluster_engine.py before B ships. Race-condition test at concurrency=8. |
| A's heartbeat receipts table query is slow → sweeper itself becomes the bottleneck | Index receipts on (project_id, created_at desc, action_type). Verify p95 < 100ms under load. |
| C + B + A interact in unexpected ways | Strict sequence. Each phase has its own Gulfstream regression gate. Don't merge B until C is green. Don't merge A until B is green. |

## Team-Mode dispatch

```
TeamCreate "Track B — Ingest Trustworthiness" — 3 teammates serial:

  Pass 1 (C):
    aspire-architect → persistence assertions helper + wire-in
    aspire-test-engineer → unit + integration + synthetic failure tests
    receipt-ledger-auditor → regression audit on Gulfstream

  Pass 2 (B):
    aspire-rnd-engineer → page parallelism refactor + semaphore
    aspire-test-engineer → concurrency-safety tests + benchmark sweep
    aspire-policy-gate → race-condition review on cluster engine
    release-sre → benchmark sign-off, pick default concurrency

  Pass 3 (A):
    aspire-architect → sweeper heartbeat state machine
    aspire-test-engineer → unit + Mangonia historical replay
    aspire-policy-gate → fail-closed posture verified

Verification close (parallel):
  output-critic → end-to-end Framer-grade test (Mangonia HTTP upload UX)
  release-sre → final Ship/No-Ship + DoD signoff
```

## Phase 5 resumption

Once Track B DoD is signed off, immediately fire **N-4.10 Mangonia cross-project
audit** with no code changes to the Phase 5 stack:
- Pull motifs / legends / primitives for the new Mangonia project_id
- Generate motif histograms via bbox-containment SQL
- Generate legend histograms via SheetCoordinateTransform + α dilation
- Run `binding_live_audit.py` with the existing OCR + Geometry + Topology + Connectivity scorers
- Fill out the scorecard

That experiment answers: **Gulfstream-bot or universal blueprint reader?**

## Why this plan is honest

Phase 5 (`Phase5_v1_validated` at `9ad438e`) is technically frozen. None of these
fixes change a single scorer band, coordinate transform, or weighted product.
What changes is the **operational ground** Phase 5 stands on — making the
cross-project experiment actually runnable. The plan respects that boundary:
no scorer tuning, no premature optimization, no opportunistic refactors.

Three production bugs. Sequence locked. Definition of done explicit. Risk
register documented. Verification subagents named. Ready to dispatch.
