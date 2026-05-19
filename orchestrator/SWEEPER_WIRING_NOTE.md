# Sweeper wiring (P0-5 / P1-8) - applied locally; push pending

The local copy of `orchestrator/src/aspire_orchestrator/server.py` on the
`fix/blueprint-sweeper-streaming` branch has the following changes applied
but they were not pushed via the MCP tool because the file (~160 KB)
exceeds practical tool-call input limits.

## Required changes to apply

### 1. lifespan startup block (after `start_sre_triage()` near line 199)

Replace the existing 4-line `_orphan_sweeper_task = asyncio.create_task(...)` comment+task
with:

```python
    # Fix 4 (2026-05-19): Orphan blueprint project sweeper.
    # Catches projects where the worker died (Railway redeploy, OOM, signal)
    # at ANY of the 5 pipeline stages.  Each stage has its own staleness
    # threshold from _STAGE_TIMEOUTS in blueprint_dispatch.py.
    # See _sweep_stale_blueprint_projects() for per-stage thresholds.
    _orphan_sweeper_task: asyncio.Task[None] = asyncio.create_task(
        _sweep_stale_blueprint_projects_loop(),
        name="blueprint-orphan-sweeper",
    )

    # P1-8 (2026-05-19): Storage object orphan sweep - heavier scan that
    # catches blobs where /upload-init minted a signed URL but /upload-complete
    # was never called (no DB row exists at all). Runs every 30 min.
    _storage_orphan_sweeper_task: asyncio.Task[None] = asyncio.create_task(
        _sweep_storage_orphans_loop(),
        name="blueprint-storage-orphan-sweeper",
    )
```

And in the corresponding `finally:` block also cancel `_storage_orphan_sweeper_task`.

### 2. Replace `_sweep_stale_blueprint_projects` body

The new body queries all 5 stages via PostgREST `or=(...)` syntax, picks the
earliest stalled stage based on `_SWEEP_STAGE_THRESHOLDS_MINUTES`, marks only
that stage failed, emits `blueprint.pipeline.timed_out` with the correct stage,
and calls `delete_orphaned_storage_object` + emits `blueprint.storage.orphan_deleted`.

### 3. Add `_sweep_storage_orphans_loop` and `_sweep_storage_orphans`

New async functions that run every 30 min, call `list_storage_orphans(60)`,
delete each, and emit a receipt per deletion.

The full diff is on disk at
`C:\Users\tonio\Projects\myapp\backend\orchestrator\src\aspire_orchestrator\server.py`
lines 200-237 (lifespan wiring) and lines 254-521 (new threshold map +
rewritten sweeper + new storage sweep).

The storage_lifecycle.py helpers and the two test files were pushed in
earlier commits on this branch.
