---
name: drew-pipeline-patterns
description: Drew blueprint pipeline architecture patterns — concurrency, caching, receipts. Wave 9l+.
metadata:
  type: project
---

## INGEST concurrent processing (Wave 9l)
- Use `asyncio.Semaphore(int(os.getenv("ASPIRE_DREW_INGEST_CONCURRENCY", "4")))` in `_async_ingest_pipeline`
- Pre-assign sheet UUIDs via `_assigned_ids = [str(uuid.uuid4()) for _ in corpus.sheets]` before gather
- `asyncio.gather(return_exceptions=True)` preserves order and allows partial failures
- Full-res PNG uploaded to `blueprint-sheet-images` bucket after thumbnail; stored in `blueprint_sheets.image_storage_path`

## SEE cache bypass (Wave 9l)
- Root cause of 520+s timeout: `_async_see_pipeline` called `split_pdf_to_sheets(pdf_bytes)` even though INGEST already rasterized every sheet
- Fix: check if ALL sheet rows have `image_storage_path` set → download concurrently → skip pdf_splitter
- Cache miss fallback: any sheet missing path → fall back to `split_pdf_to_sheets`
- Emit `blueprint.see.cache_hit_ratio` receipt (Law #2)
- `sheet_image_storage.download_sheet_image` is patched via `aspire_orchestrator.services.blueprint.sheet_image_storage.download_sheet_image` in tests (lazy import inside `_async_see_pipeline`)

## REASON parallel dispatch (Wave 9l)
- Split single LLM call into 3 parallel `asyncio.gather` calls (A: assemblies, B: missing_inputs, C: narrative)
- Each call uses separate Redis cache key: `reason_a`, `reason_b`, `reason_c`
- `_merge_parallel_responses(resp_a, resp_b, resp_c)` merges into shape `_parse_llm_response` expects
- `asyncio.gather(return_exceptions=False)` — fail-closed on any call failure (Law #3)

## OCR parallel dispatch (Wave 9n)
- `extract_sheet_corpus` now uses `asyncio.gather` + `Semaphore(ASPIRE_DREW_OCR_CONCURRENCY, default=8)` instead of sequential for-loop
- Azure client is instantiated per-sheet inside the semaphore: `_az_client = get_azure_doc_intel_client()` — stateless, safe to call N times
- `_ocr_one_sheet` is a nested async function that captures outer variables via closure (`_primary_ocr`, `_lp_response_cache`, `_ocr_sem`, `corpus`, `correlation_id`, `suite_id`, `office_id`)
- `corpus.record_provider()` is called inside the nested function — safe because no `await` between `.get()` and `=` assignment (event loop atomic)
- `gather(return_exceptions=True)` → degraded sheets fall back to PyMuPDF in the aggregation loop after gather completes
- Performance: 39 sheets ÷ 8 concurrency = 5 batches × ~8s Azure = ~40s INGEST (was ~6 min sequential)
- Test pattern: patch `get_azure_doc_intel_client` at `aspire_orchestrator.services.blueprint.ocr_coordinator.get_azure_doc_intel_client` and return a MagicMock with `analyze_layout` async method

## INGEST image upload retry (Wave 9n)
- Full-res PNG upload now retries once (0.5s delay) before emitting `blueprint.ingest.image_cache_failed` receipt
- Old behavior: silent `_log.warning` with no receipt on failure — SEE still re-rendered but ops had no audit trail
- New behavior: 2 attempts → receipt with `sheet_id`, `project_id`, `page_number`, `reason=upload_failed_after_retry`
- Thumbnail uploads remain best-effort (already emitted `blueprint.ingest.thumbnail_upload_failed`)

## SEE Supabase error observability (Wave 9n)
- Symbol insert failure log now includes `str(exc)[:200]` (not just `type(exc).__name__`)
- Same improvement applied to scale update and seal_detected update log lines
- Symbol insert failure emits `blueprint.see.symbol_insert_failed` receipt with `error_type`, `error_message`, `sheet_id`, `project_id`
- Most likely causes of SupabaseClientError in SEE: RLS `app.current_suite_id` not set in worker context; column mismatch; concurrent PK conflict
- Scale and seal errors do NOT emit receipts (low-signal, non-critical); symbol insert failure does because it directly impacts story quality

## Progressive pipeline (Wave 9o)
- `_run_full_pipeline` fan-out after CLASSIFY: `asyncio.gather(see_branch(), reason_procure_branch(), return_exceptions=True)`
- `see_branch` = `_run_stage_with_timeout("see", ...)` standalone
- `reason_procure_branch` = await REASON then PROCURE in sequence
- `_current_stage` tracking requires special handling in the fan-out: each branch owns its own stage tracking; outer `_current_stage` only matters for pre-CLASSIFY failure paths
- New receipt type: `blueprint.pipeline.progressive_dispatch` — emitted BEFORE `asyncio.gather` with `{see_branch_started_at, reason_branch_started_at}`
- Outer `except asyncio.TimeoutError` cannot catch these anymore — each branch's `_run_stage_with_timeout` already handles timeout receipts; the outer handler only needs to handle CancelledError and generic Exception
- `write_story` already handles empty symbols list safely — `symbols_by_sheet` initialized to `{sid: []}` for all sheets; `if sheet_symbols:` guards prevent crashes
- `_isStageRunning` in poll hook checks `p[k] === 'running'` — with parallel branches, see + reason + procure can ALL be 'running' simultaneously; hook cadence stays 2s during any running stage (correct behavior, no change needed)
- Frontend `StageProgress` type maps backend `not_started/in_progress/done/failed` to `pending/running/ok/error` via `_normalizeStageStatus`
- Tab gating uses `stageProgress.reason`, `stageProgress.see`, `stageProgress.procure` from `useBlueprintProjectPoll` — tests mock `useBlueprintStory`, `useTakeoffSymbols`, `useTakeoffMaterials` etc. for isolation

## Receipt types (canonical)
- `blueprint.ingest.dlp_unavailable` — DLP service down
- `blueprint.ingest.dlp_scanned` — DLP ran (even 0 redactions)
- `blueprint.ingest.thumbnail_upload_failed` — thumbnail failed (soft)
- `blueprint.ingest.image_cache_failed` — full-res image upload failed after retry (Wave 9n)
- `blueprint.ingest.azure_doc_intel_used` — Azure OCR used per-sheet (emitted from ocr_coordinator.py)
- `blueprint.see.concurrent_skip` — second concurrent SEE for same project
- `blueprint.see.idempotency_clear` — symbols deleted before re-SEE
- `blueprint.see.skipped_disciplines` — wave 9k skip filter
- `blueprint.see.cache_hit_ratio` — wave 9l: ratio of sheets loaded from storage vs pdf_splitter
- `blueprint.see.symbol_insert_failed` — Supabase insert failed for a symbol row (Wave 9n)
- `blueprint.llm.cache_hit` — Redis cache hit for LLM call
- `blueprint.pipeline.progressive_dispatch` — Wave 9o: fan-out of SEE + REASON+PROCURE branches (new)

## RLS pattern (blueprint tables)
- All blueprint tables use `suite_id = current_setting('app.current_suite_id')::uuid`
- Storage buckets use service_role for writes; application enforces path prefix = suite_id

## Test patterns
- `test_drew_see.py` has duplicate `TestSeeDisciplineSkipFilter` + `TestSeeConcurrency` classes — existing issue, not introduced by this wave
- Patch `aspire_orchestrator.skillpacks.drew_blueprint.split_pdf_to_sheets` for SEE stage tests
- `_query_receipts_by_corr` helper in test_drew_see.py bypasses suite_id filter
- `asyncio.get_event_loop().run_until_complete(coro)` pattern used in test_sheet_image_storage.py
