# Wave 11g-v2 — Rollback Plan

## Tl;dr

The Wave 11g-v2 migration is **purely additive**. Rollback never requires
schema changes — it only requires stopping the new code path. The new
columns can remain in the table permanently and cost ~80 bytes/row.

## Feature flags & kill switches

| Flag | Default | Effect when OFF |
| --- | --- | --- |
| `WAVE_11G2_PIPELINE_ENABLED` | false | Skip Track A/B entirely. Legacy SHA1 pipeline handles all ingest. |
| `WAVE_11G2_FORCE_RASTER` | false | Force every page through `RASTER_SCAN` mode. Used during topology-match latency incidents. |
| `WAVE_11G2_WRITE_NEW_COLUMNS` | true | If false, resolver still runs but does NOT write `graph_hash`, `canonical_type`, `motif_id`, `wl_rotation_orbit` to memory. Legacy `signature_hash` writes only. |
| `WAVE_11G2_YOLO_EXPORTER_ENABLED` | true | Disables the pg_cron weekly export. |

All flags are read at request time so toggling propagates within ~1 minute
(orchestrator config-poll interval).

## Rollback procedures by severity

### Yellow — slow but not broken

Symptoms: TRUE_UNKNOWN bucket creeping up; resolver hit rate dropping
gradually below 80%.

1. Open a Linear ticket; flip `WAVE_11G2_WRITE_NEW_COLUMNS=false` for the
   affected suites — pipeline still resolves but stops writing new-format
   data, preserving the option to re-run with a corrected resolver.
2. No data backout needed.

### Red — pipeline returning wrong classifications

Symptoms: Tenant complaints; high disagreement vs legacy SHA1 pipeline.

1. Flip `WAVE_11G2_PIPELINE_ENABLED=false` globally.
2. Pipeline falls back to the legacy SHA1 path automatically.
3. Identify the bad rows by `model_version` provenance tag:
   ```sql
   -- Audit only — never DELETE. New corrections come in as new INSERTs.
   SELECT id, graph_hash, canonical_type, source, model_version, created_at
     FROM blueprint_symbol_memory
    WHERE model_version LIKE 'wave_11g2.%'
      AND created_at >= '<incident_start>';
   ```
4. For each affected suite, INSERT a superseding row with `source='correction'`
   and `confirmation_count=0` — Law #2 forbids destructive overwrite. The
   query layer prefers the most recent confirmed row by default.

### Black — schema-level catastrophe (theoretical)

The additive migration has no realistic schema-level failure mode (no NOT NULL
on new columns, no FK changes, no triggers added). If a column must be
removed:

```sql
-- LAST RESORT ONLY — requires written approval (Law #4 RED tier).
ALTER TABLE public.blueprint_symbol_memory
    DROP COLUMN IF EXISTS canonical_type,
    DROP COLUMN IF EXISTS graph_hash,
    DROP COLUMN IF EXISTS primitive_signature,
    DROP COLUMN IF EXISTS motif_id,
    DROP COLUMN IF EXISTS wl_rotation_orbit,
    DROP COLUMN IF EXISTS extraction_mode,
    DROP COLUMN IF EXISTS last_confirmed_at;

DROP INDEX IF EXISTS blueprint_symbol_memory_graph_hash_idx;
DROP INDEX IF EXISTS blueprint_symbol_memory_canonical_type_idx;
DROP INDEX IF EXISTS blueprint_symbol_memory_motif_id_idx;
DROP INDEX IF EXISTS blueprint_symbol_memory_extraction_mode_idx;
```

This is **never** the expected rollback path. The expected path is "leave the
schema, flip the flag."

## Data backout

Because the migration is additive, there is **no data to back out** — the new
columns contain data, but legacy queries (filtered on `signature_hash` and
`canonical_name`) continue to function unchanged. Removing the new data is
equivalent to a column drop, which requires Black-tier sign-off above.

## Verification commands

```pwsh
# Confirm pipeline is OFF.
$env:WAVE_11G2_PIPELINE_ENABLED="false"

# Confirm legacy ingest still works against the live schema.
pytest orchestrator/tests/test_wave_11g2_acceptance.py -v -k "not 11g2"

# Confirm new columns are present (read-only safety).
# Use mcp__supabase__list_tables → blueprint_symbol_memory → verify columns.
```

## Receipt obligations

Every rollback step emits a receipt:

- `wave_11g2_flag_flip` — flag name, old, new, actor, suite_id (if scoped).
- `wave_11g2_superseding_correction` — new row id, supersedes_id reference.
- `wave_11g2_schema_dropped` — Black-tier only; requires actor + approval id.
