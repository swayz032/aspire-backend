-- Migration: 20260521000002_blueprint_symbol_memory_wave_11g2
-- Wave 11g-v2 — Additive columns on blueprint_symbol_memory (created Wave 11i
-- in 20260520000001_blueprint_symbol_memory.sql).
--
-- This migration is ADDITIVE ONLY (Law #2 — immutable receipts / append-only):
--   * No DROP / RENAME / DELETE / destructive UPDATE.
--   * Every new column is NULLABLE — existing rows continue to satisfy NOT NULL
--     constraints on the original columns (signature_hash, canonical_name,
--     source, suite_id) without backfill.
--   * Indexes use IF NOT EXISTS so the migration is idempotent.
--   * Existing HNSW pgvector index on `embedding` is left intact.
--   * Existing unique constraint (suite_id, signature_hash) is preserved as
--     the legacy SHA1 identity; `graph_hash` is the new WL identity and uses
--     its own index.
--
-- Track A (vector graph builder) and Track B (WL motif miner + resolver)
-- read/write these new columns. The Track C consumer (this migration) ships
-- FIRST with no callers — phase 5 of the 12-phase rollout. See
-- WAVE_11G2_ROLLOUT.md for the deployment order.
--
-- Law #2: corrections to confirmed rows (confirmation_count > 0) are never
-- UPDATEs in this schema — callers must INSERT a new row carrying the
-- existing pattern of (suite_id, signature_hash) uniqueness.
-- Law #6: no RLS change — existing suite_id isolation policy already covers
-- every new column.

-- ---------------------------------------------------------------------------
-- 1. Additive columns
-- ---------------------------------------------------------------------------

ALTER TABLE public.blueprint_symbol_memory
    ADD COLUMN IF NOT EXISTS canonical_type        text,
    ADD COLUMN IF NOT EXISTS graph_hash            text,
    ADD COLUMN IF NOT EXISTS primitive_signature   text,
    ADD COLUMN IF NOT EXISTS motif_id              text,
    ADD COLUMN IF NOT EXISTS wl_rotation_orbit     jsonb,
    ADD COLUMN IF NOT EXISTS extraction_mode       text,
    ADD COLUMN IF NOT EXISTS last_confirmed_at     timestamptz;

-- The following columns already exist on the Wave 11i base table, but we
-- declare them here defensively with IF NOT EXISTS so the migration is safe
-- to re-run against any environment whose schema drifted prior to 11g-v2:
ALTER TABLE public.blueprint_symbol_memory
    ADD COLUMN IF NOT EXISTS aliases               jsonb       DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS confirmation_count    integer     DEFAULT 0,
    ADD COLUMN IF NOT EXISTS first_seen_project_id uuid,
    ADD COLUMN IF NOT EXISTS model_version         text;

-- ---------------------------------------------------------------------------
-- 2. Indexes (additive, idempotent)
-- ---------------------------------------------------------------------------

-- Primary new lookup path: (suite_id, graph_hash) — the WL identity index.
CREATE INDEX IF NOT EXISTS blueprint_symbol_memory_graph_hash_idx
    ON public.blueprint_symbol_memory (suite_id, graph_hash)
    WHERE graph_hash IS NOT NULL;

-- Canonical taxonomy index — supports YOLO exporter class-grouping and
-- analytics ("how many GFCI receptacles across this tenant?").
CREATE INDEX IF NOT EXISTS blueprint_symbol_memory_canonical_type_idx
    ON public.blueprint_symbol_memory (suite_id, canonical_type)
    WHERE canonical_type IS NOT NULL;

-- Motif join index — connects discovered motifs (Track B) back to memory.
CREATE INDEX IF NOT EXISTS blueprint_symbol_memory_motif_id_idx
    ON public.blueprint_symbol_memory (suite_id, motif_id)
    WHERE motif_id IS NOT NULL;

-- Extraction-mode audit index — supports observability queries like
-- "what % of resolved symbols came from VECTOR_NATIVE pages this week?".
CREATE INDEX IF NOT EXISTS blueprint_symbol_memory_extraction_mode_idx
    ON public.blueprint_symbol_memory (suite_id, extraction_mode)
    WHERE extraction_mode IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. Column comments — self-documenting schema for downstream auditors.
-- ---------------------------------------------------------------------------

COMMENT ON COLUMN public.blueprint_symbol_memory.canonical_type IS
    'Wave 11g-v2: normalized taxonomy slug (e.g. electrical_receptacle_gfci). '
    'Separate from canonical_name which may be human-readable architect text. '
    'NULL when source = unresolved (TRUE_UNKNOWN bucket).';

COMMENT ON COLUMN public.blueprint_symbol_memory.graph_hash IS
    'Wave 11g-v2: Weisfeiler-Lehman graph hash from the vector graph builder. '
    'Topology-stable across surrounding context, rotation, and scale. Replaces '
    'the legacy SHA1-of-sequence signature_hash for new pipeline writes; '
    'signature_hash is retained for Wave 11i backward compatibility.';

COMMENT ON COLUMN public.blueprint_symbol_memory.primitive_signature IS
    'Wave 11g-v2: original VectorPrimitive hash from Phase 2 (vector_extractor). '
    'Provenance trail back to the raw PDF primitive that seeded this symbol.';

COMMENT ON COLUMN public.blueprint_symbol_memory.motif_id IS
    'Wave 11g-v2: short identity hash for joining to motifs discovered by the '
    'WL miner (Track B). One motif_id may correspond to many rows when the '
    'same topology has been confirmed by multiple architects/tenants.';

COMMENT ON COLUMN public.blueprint_symbol_memory.wl_rotation_orbit IS
    'Wave 11g-v2: JSONB array of 4 rotation-equivalent WL hashes (0/90/180/270). '
    'Used for rotation-invariant matching and YOLO augmentation hints.';

COMMENT ON COLUMN public.blueprint_symbol_memory.extraction_mode IS
    'Wave 11g-v2: one of VECTOR_NATIVE | HYBRID | RASTER_SCAN — describes how '
    'the symbol was extracted. Drives observability and YOLO training filters.';

COMMENT ON COLUMN public.blueprint_symbol_memory.last_confirmed_at IS
    'Wave 11g-v2: timestamp of the most recent contractor confirmation event. '
    'Distinct from updated_at which is bumped on every passive observation.';

COMMENT ON COLUMN public.blueprint_symbol_memory.aliases IS
    'JSONB array of alternative names observed for this symbol across projects. '
    'Append-only at the application layer; never destructively rewritten.';

COMMENT ON COLUMN public.blueprint_symbol_memory.confirmation_count IS
    'Number of times a contractor has explicitly confirmed this canonical_name '
    'for this graph_hash/signature_hash. Rows with > 0 confirmations are '
    'IMMUTABLE per Law #2 — corrections require INSERTing a superseding row.';

COMMENT ON COLUMN public.blueprint_symbol_memory.model_version IS
    'Wave 11g-v2: provenance tag for the model/pipeline version that produced '
    'this row. Used by the YOLO training exporter to filter on training-eligible '
    'pipeline versions.';
