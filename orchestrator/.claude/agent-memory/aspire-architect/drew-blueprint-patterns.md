---
name: drew-blueprint-patterns
description: Drew Blueprint Engine architecture patterns, known gaps, and audit findings from Wave 9e verification
metadata:
  type: project
---

## Wave 9f fixes applied (2026-05-19)

### P0 — CancelledError handling
- `_run_full_pipeline` in `blueprint_dispatch.py`: added `except asyncio.CancelledError:` before `except Exception:`. Marks current stage failed + emits `blueprint.pipeline.cancelled` receipt + re-raises.
- `_async_classify_pipeline`: CancelledError added to sheets-load try block (marks classify=failed + re-raises).
- `_async_reason_pipeline`: CancelledError added before `except Exception:` in write_story try block.
- `_async_ingest_pipeline`: CancelledError added to extract_sheet_corpus try block.
- `_async_see_pipeline`: CancelledError added to sheets-load try block (also releases _see_lock).
- `_async_procure_pipeline`: CancelledError added to materials-load try block.
- Helper: `_mark_stage_failed_best_effort(project_id, stage, suite_id)` module-level async function.

### P1 — no-materials early return in `_async_procure_pipeline`
- Added `set_stage_progress(procure, done)` before the `return {"status":"ok", ...}` when `not materials`.
- Was: only set done at end of happy path (line 2356). Early return at line 2241 was missing it.

### P1 — Concurrent SEE lock
- Module-level `_stage_locks: dict[str, asyncio.Lock] = {}` and `_get_stage_lock(stage, project_id)` helper.
- `_async_see_pipeline`: `.locked()` check at entry; if locked → emit `blueprint.see.concurrent_skip` receipt + return `{"status":"skipped","reason":"concurrent_run"}`.
- If not locked → `await _see_lock.acquire()` + explicit `_see_lock.release()` at every exit point (5 total).
- Limitation: single-replica only. Multi-replica needs `pg_try_advisory_xact_lock` migration.

### MEDIUM — DLP fail-closed
- `_dlp_redact`: when `_dlp is None`, now returns `("[DLP_UNAVAILABLE — content withheld]", -2)` instead of `(text, -1)`. Caller must store placeholder, NOT raw text.
- Call site in `_async_ingest_pipeline`: `if redaction_count == -2:` emits `blueprint.ingest.dlp_unavailable` receipt. `else:` always emits `blueprint.ingest.dlp_scanned` (fires for zero-hit scans too).

### LOW — Legacy receipt format migration
- `blueprint.upload.dedup_invalid_stale` inline receipt (was at ~line 1126): migrated from `{receipt_id,ts,event_type,status}` to `{id,outcome,action_type,receipt_type,created_at,risk_tier,...}`.
- `blueprint.ingest.thumbnail_upload_failed` inline receipt (was at ~line 1293): same migration.

### LOW — DLP zero-hit receipt
- Previous: emitted `blueprint.ingest.dlp_redacted` only when `redaction_count > 0`.
- Now: emits `blueprint.ingest.dlp_scanned` on every DLP scan (including zero-hit), with `pii_detected: bool`.

## Pipeline structure
- 5 async pipeline functions: `_async_ingest_pipeline`, `_async_classify_pipeline`, `_async_see_pipeline`, `_async_reason_pipeline`, `_async_procure_pipeline` — all in `src/aspire_orchestrator/skillpacks/drew_blueprint.py`
- Dispatched via `_run_full_pipeline` in `routes/blueprint_dispatch.py`
- Stage progress tracked via `services/blueprint/stage_progress.py` (two-step SELECT+PATCH, best-effort)

## Confirmed architectural gaps (Wave 9e audit)

### CancelledError not caught (P0 severity)
`_run_full_pipeline` catches `asyncio.TimeoutError` and `Exception` but NOT `asyncio.CancelledError` (which is BaseException in Python 3.8+). FastAPI background task cancellation on server shutdown leaves stage_progress stuck as `in_progress`. Same gap in all 5 `_async_*_pipeline` functions — none have `except asyncio.CancelledError` handlers before re-raising.

### Orphan sweeper only covers INGEST (P0-5 was FAIL)
`server.py:_sweep_stale_blueprint_projects()` queries only `stage_progress->>ingest=eq.in_progress`. Classify/SEE/REASON/PROCURE orphans are never swept. The PR claimed per-stage thresholds (15/5/8/5/3 min) — these do NOT exist.

### stream_pdf_for_hash still used in production (P1-5 was PARTIAL)
`stream_pdf_to_temp_file` was written and is correct. But `routes/blueprint_dispatch.py:851` still imports and calls `stream_pdf_for_hash` (the deprecated accumulator). The true-streaming function is dead code.

### No project-level lock on concurrent SEE runs
The SELECT→DELETE→INSERT pattern in `_async_see_pipeline` has no advisory lock. Two concurrent SEE dispatches against the same project_id both DELETE then INSERT, potentially doubling symbol rows.

### Ingest returns status:"ok" even with zero sheets stored
`_async_ingest_pipeline` returns `{"status": "ok", ...}` unconditionally even when `sheet_ids` is empty (only sets `stage_progress="failed"`). The dispatch caller checks `ingest_result.get("status") not in ("ok", "dedup")` so it will NOT short-circuit on zero-sheet ingest — it proceeds to CLASSIFY which then correctly returns `status:"failed"`.

## What passed correctly
- P0-3: set_stage_progress in_progress/done/failed present in all 5 pipelines (try/except pattern)
- P0-7: SEE DELETE scoped by suite_id+project_id; REASON deletes 3 tables scoped same way; PROCURE uses UPDATE
- P0-8: poll hook reads `freshProject.stage_progress` not stale closure
- P0-9: SheetThumbnailGrid imports STAGE_TERMINAL and uses it correctly
- P1-1: watchdog timer in useBlueprintUpload with proper cleanup on useEffect deps
- P1-2: consecutive error ceiling with allSettled — increments only when ALL 3 fail
- P1-3: _assertShape at every JSON boundary in blueprintsApi.ts
- P1-4: Promise.allSettled in poll loop
- P1-10: classify returns failed (not ok) on zero sheets
- P2-1: legacy ingest queries `content_hash` column (line 1081)
- P2-5: LlamaParse hoisted out of per-page loop in ocr_coordinator.py
- P2-8: stageCompletedAt exported from poll hook, stamped on first 'ok' observation

**Why:** Reference for future waves building on Drew pipeline. These gaps need Wave 9f fixes.
**How to apply:** When proposing any Drew pipeline change, check CancelledError handling, sweeper scope, and the stream_pdf_for_hash migration.
