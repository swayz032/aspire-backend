---
name: drew-pipeline-hardening
description: Drew Blueprint pipeline production hardening — 6 fixes for stale dedup, orphan projects, timeouts, retries, and receipt quality. Commit da0a3ce on 2026-05-19.
metadata:
  type: project
---

## Drew Blueprint Pipeline — Production Hardening (2026-05-19)

Commit: `da0a3ce` on `main` (swayz032/aspire-backend)

**Why:** Two live failure modes: (1) stale dedup returned FAILED project to user; (2) ProviderError in INGEST caused background task to die silently with no retry, no timeout, orphan project stuck in_progress forever.

### Fix 1 — Stale dedup validation in dispatch route
File: `routes/blueprint_dispatch.py`

Dedup block now does a two-step validation before returning `status=dedup`:
1. Check `stage_progress.ingest == "done"` from the existing project row.
2. Query `blueprint_sheets` for `project_id + suite_id` — must have >= 1 row.

If either fails → emit `blueprint.upload.dedup_invalid_stale` receipt, NULL-out `content_hash` on the stale project (so future hash lookups can't find it), fall through to fresh project creation.

CRITICAL: The same validation already existed in `_async_ingest_pipeline` for the non-dispatch path (`address` field dedup). The dispatch route used `content_hash` field and lacked the validation — that was the gap.

**Pattern for all future dedup checks:** validate state before returning cached results. Never trust a hash match alone.

### Fix 2 — Per-stage hard timeouts
File: `routes/blueprint_dispatch.py`

```python
_STAGE_TIMEOUTS = {"ingest": 480, "classify": 120, "see": 180, "reason": 120, "procure": 60}
```

`_run_stage_with_timeout(stage, coro, ...)` wraps `asyncio.wait_for(coro, timeout=N)`.
On `asyncio.TimeoutError`: emit `blueprint.<stage>.timeout` receipt with `timeout_ms` + `elapsed_ms`, set `stage_progress[stage] = "failed"`, re-raise to outer handler.

Outer handler catches `asyncio.TimeoutError` separately from `Exception` — emits `blueprint.pipeline.failed` with `error_class=TimeoutError`. Pipeline stops immediately.

**Pattern:** Always separate `asyncio.TimeoutError` from `Exception` in the outer handler because `TimeoutError` is already handled in the stage wrapper — don't double-emit.

### Fix 3 — Single transient retry in ocr_coordinator
File: `services/blueprint/ocr_coordinator.py`

Added `_is_transient_provider_error(exc)` helper that uses `exc.code.retryable` property on `InternalErrorCode` (already defined — avoids duplicating category logic). Non-`ProviderError` exceptions are treated as transient.

Retry loop: `while _lp_attempts < 2` (2 = 1 original + 1 retry). On transient error at attempt 0: `await asyncio.sleep(1.0)`, increment, continue. At attempt 1 or non-transient: log warning, break to fallback.

**Pattern for retry in OCR:** Use `InternalErrorCode.retryable` — it already encodes NETWORK/RATE/SERVER vs AUTH/INPUT/DOMAIN. Don't re-implement the category check.

**Gotcha:** `AUTH_MISSING_CREDENTIALS` does not exist in `InternalErrorCode`. Use `AUTH_INVALID_KEY` for 401 tests.

### Fix 4 — Orphan project sweeper
File: `src/aspire_orchestrator/server.py`

Added `_sweep_stale_blueprint_projects_loop()` as an `asyncio.Task` started in `_app_lifespan`. Runs every 5 minutes (30s startup grace). `_sweep_stale_blueprint_projects()` queries for `stage_progress->>ingest=eq.in_progress` + `created_at < NOW() - 15min`.

PostgREST jsonb text-path filter syntax: `stage_progress->>ingest=eq.in_progress` — note `->>` not `->`. Works with `supabase_select(filters=...)`.

Task cancellation in finally block: cancel + `await` + swallow `CancelledError`.

**Pattern:** Startup sweepers go in lifespan as `asyncio.create_task`, with `task.cancel()` + await in `finally`. Don't use `asyncio.ensure_future` or `loop.create_task` — `asyncio.create_task` is correct in async context.

### Fix 5 — Structured error fields in pipeline.failed receipt
`redacted_outputs` now includes: `error_class`, `error_message` (capped 500 chars), `stage`, `provider` (from `exc.provider_id` if present), `retry_count`.

Detect provider: `if hasattr(exc, "provider_id"): provider_hint = str(exc.provider_id)`.

### Fix 6 — receipt_id in dispatch response
`BlueprintDispatchResponse` gets `receipt_id: str | None = None`. `_cut_receipt()` already returns the UUID — capture it and pass to the response model. Works for both `queued` and `dedup` paths.

### Test patterns
- Existing dedup test (`test_dispatch_dedup_returns_existing`) needed updating: now uses `side_effect=_select_fn` returning valid stage_progress + sheets on second call.
- Timeout test patches `_STAGE_TIMEOUTS` to `0.05` via `patch.dict(...)` — test runs in <1s.
- Retry test patches `asyncio.sleep` with `AsyncMock` to avoid actual waiting.

### Key files changed
- `orchestrator/src/aspire_orchestrator/routes/blueprint_dispatch.py`
- `orchestrator/src/aspire_orchestrator/server.py`
- `orchestrator/src/aspire_orchestrator/services/blueprint/ocr_coordinator.py`
- `orchestrator/tests/test_drew_dispatch.py`
