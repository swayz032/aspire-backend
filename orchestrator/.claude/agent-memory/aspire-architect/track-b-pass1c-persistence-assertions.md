---
name: track-b-pass1c-persistence-assertions
description: Table name corrections, helper locations, call site patterns for Track B Pass 1.C persistence assertions
metadata:
  type: project
---

## Key discoveries (2026-05-26)

### Table name corrections in original plan
The plan used wrong table names. Corrections:
- `blueprint_primitives` → does NOT exist in production. Production table is `blueprint_symbols`.
- `blueprint_composed_symbols` → exists but is NOT written by `drew.see.11g2.completed`. That stage writes to `blueprint_symbols`.
- `drew.see.legend_extracted` receipt is emitted in `vgse_pipeline.py` but `blueprint_project_legends` is populated by `extract_project_legends()` which is NEVER called from the active pipeline.
- The correct stage name for sheet intelligence assertion is `provider.azure.sheet_layout` (NOT `blueprint.ingest.azure_doc_intel_used`).

### `blueprint_symbols` has no project_id column
Only has `sheet_id` and `suite_id`. `count_table_rows()` with `project_id` filter won't work.
**Solution:** Use local insert counters (e.g., `summary["rows_inserted"]`) as the delta directly, with `count_before=0`.

### `blueprint_symbol_memory` has no direct `project_id` column
Has `first_seen_project_id` and `last_seen_project_id`. Can't use simple `project_id=eq.X` filter.
**Solution:** Use `summary["legend_memory_upserts"]` as proxy delta.

### Helper locations
- `count_table_rows` → `services/receipt_store.py` (NOT supabase_client.py — tests patch `receipt_store._get_supabase_client`)
- `emit_persistence_asserted_receipt` → `services/receipt_store.py`
- Both are SYNC functions (not async) — tests call them directly without `await`

### Wire-in summary (final corrected)
| Stage receipt | Expected table | expected_min | Source file | Count method |
|---|---|---|---|---|
| `provider.azure.sheet_layout` | `blueprint_sheet_intelligence` | 1 | `drew_blueprint.py:_async_universal_sheet_intel_pipeline` | `count_table_rows()` (has project_id) |
| `drew.see.legend_extracted` | `blueprint_project_legends` | 0 | `vgse_pipeline.py:run_vgse_for_project` | `count_table_rows()` (has project_id) |
| `drew.see.11g2.page` | `blueprint_symbols` | 1 (0 if RASTER_SCAN) | `wave_11g2_see.py` | local `inserted` counter |
| `drew.see.11g2.completed` | `blueprint_symbols` | 0 | `wave_11g2_see.py` | local `summary["rows_inserted"]` |
| `blueprint.see.completed` | `blueprint_symbols` | 100 | `drew_blueprint.py:_async_see_pipeline` | local `total_symbols` |
| `drew.see.spatial_binding` | `blueprint_symbol_memory` | 0 | `vgse_pipeline.py:run_vgse_for_project` | local `summary["legend_memory_upserts"]` |

### Migration
`migrations/20260526000001_receipts_status_extension.sql` — adds `SUCCEEDED_WITH_ZERO_OUTPUT` to receipts.status CHECK constraint.
Old: `('PENDING','SUCCEEDED','FAILED','DENIED')`
New: `('PENDING','SUCCEEDED','FAILED','DENIED','SUCCEEDED_WITH_ZERO_OUTPUT')`

### Critical: receipt_store.py status_map
Added `"succeeded_with_zero_output": "SUCCEEDED_WITH_ZERO_OUTPUT"` to `_map_receipt_to_row` status_map so the new status flows through to Supabase correctly.

**Why:** Track B Pass 1.C generalization blocker — receipts were emitting SUCCEEDED when 0 rows were written.
**How to apply:** When working on Drew pipeline stages that write to DB, always use `emit_persistence_asserted_receipt` instead of bare `store_receipts` for the completion receipt.
