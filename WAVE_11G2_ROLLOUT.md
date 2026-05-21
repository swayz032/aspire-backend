# Wave 11g-v2 — Phased Rollout

The migration is **additive only**. Every deployment step is independently
reversible without data loss because no existing column or row is touched.

## Phase order (matches the 12-phase plan)

### Stage 1 — Schema only (no consumers)
1. Apply `orchestrator/migrations/20260521000002_blueprint_symbol_memory_wave_11g2.sql`
   via `mcp__supabase__apply_migration` against the dev branch.
2. Verify the new columns appear with `mcp__supabase__list_tables` and that
   the existing `signature_hash` unique constraint still holds.
3. Run `mcp__supabase__get_advisors` — confirm no new advisories (RLS,
   security, performance).
4. Promote dev branch → prod via `mcp__supabase__merge_branch`.

**Receipt:** migration_applied with file hash + applied_at.

### Stage 2 — Track A (Phases 1-3)
1. Deploy `document_classifier`, `vector_extractor`, `graph_builder` BEHIND
   a feature flag (`WAVE_11G2_PIPELINE_ENABLED=false` by default).
2. Soak in shadow-mode for 48h — pipeline runs alongside the legacy SHA1
   pipeline; outputs are written to receipts but NOT to
   `blueprint_symbol_memory`.
3. Verify Prometheus counters look sane (vector_pages_total > 0,
   topology_match_latency_ms p95 < 500ms).

**Receipt:** wave_11g2_shadow_started, wave_11g2_shadow_completed.

### Stage 3 — Track B (Phases 4, 6-8)
1. Deploy `wl_motif_miner`, `legend_anchor`, `retrieval_resolver`,
   `symbol_memory.upsert` (the version that writes the new columns).
2. Keep the feature flag OFF — still shadow mode.
3. Compare resolver outputs vs legacy pipeline on a sample of 100 projects;
   require ≥ 95% agreement on resolved symbols and zero divergence on
   `source='legend'` resolutions.

**Receipt:** wave_11g2_parity_check_passed.

### Stage 4 — Activation
1. Flip `WAVE_11G2_PIPELINE_ENABLED=true` for **one** internal-test suite first.
2. 24-hour monitoring window — watch `wave_11g2_unknown_motifs_total{bucket="TRUE_UNKNOWN"}`
   and the resolver hit-rate panel.
3. Roll forward to additional tenants in waves of 10%.

**Receipt:** wave_11g2_activation per suite_id.

### Stage 5 — YOLO Exporter (Phase 9)
1. Schedule the exporter as a pg_cron weekly job:
   ```
   SELECT cron.schedule('wave_11g2_yolo_export_weekly', '0 4 * * 0', $$
       SELECT net.http_post(
         url := 'https://orchestrator/internal/wave_11g2/export_yolo',
         body := '{"correlation_id":"cron_weekly"}'::jsonb
       );
   $$);
   ```
2. Manifest hashes are recorded in receipts. Each weekly export is
   immutable and timestamped.

**Receipt:** wave_11g2_yolo_export_completed (manifest hash, sample count).

### Stage 6 — Integration into `vgse_pipeline.py`
Final step — wire the resolver outputs into the orchestrator's blueprint
pipeline so downstream consumers (Takeoff UI, Materials, Procurement) read
the new `canonical_type` field.

## Incident runbooks (referenced from alerts)

### `true_unknown_spike`
Alert: TRUE_UNKNOWN bucket growth > 5/min for 10m.
Steps:
1. Check `wave_11g2_retrieval_hit_rate{source="legend"}` — if legend hits
   dropped, a new architect with a novel legend region is likely ingesting.
2. Pull a sample motif from the latest unresolved batch via the
   `/internal/wave_11g2/sample_unknowns` endpoint.
3. If genuine novel topology: confirm in the Takeoff UI to seed the lexicon.
4. If pipeline bug: flip `WAVE_11G2_PIPELINE_ENABLED=false` for the affected
   suite_id — pipeline falls back to legacy SHA1 path automatically.

### `match_latency`
Alert: topology match p95 > 500ms for 5m.
Steps:
1. Check Supabase pgvector index usage — confirm HNSW index on `embedding`
   is being hit (not seq scan).
2. Check `wave_11g2_vector_pages_total` rate — if request volume spiked,
   consider tenant-level rate limiting.
3. If degradation persists: flip pipeline back to RASTER_SCAN-only mode by
   forcing `document_classifier → RASTER_SCAN` via the `WAVE_11G2_FORCE_RASTER`
   flag.

## Free-tier cost accounting

| Resource | Free-tier ceiling | Wave 11g-v2 impact |
| --- | --- | --- |
| Supabase DB rows | 500 MB | New columns are nullable; ~80 bytes/row growth. At 1M rows ≈ 76 MB additional — well within ceiling. |
| Supabase pgvector HNSW | included | No new index on embedding; reuses existing. |
| Prometheus series | unlimited (self-hosted) | 9 counters × low-cardinality labels = ≤ 30 series. |
| pg_cron jobs | included | +1 weekly job. |
| Grafana panels | unlimited (self-hosted) | +1 dashboard, ~5 panels. |

No paid-tier upgrade required.
