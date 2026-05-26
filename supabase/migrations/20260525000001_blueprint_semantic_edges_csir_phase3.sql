-- Wave 20.14.5L (CSIR Phase 3) — Relational Enrichment / Semantic Edge Fabric
-- Substrate for Phase 4 (constraint extraction) + Phase 5 (referential binding).
-- Architectural lesson from Wave 20.13.3 false-positive explosion:
--   embeddings = recall infrastructure
--   constraints = precision infrastructure
-- This migration installs the GRAPH layer that downstream constraint walks
-- + grounded binding both require. Append-only ledger. Polymorphic entity
-- references. Locality enforcement at schema layer.
--
-- 3 tables in this migration:
--   1. blueprint_semantic_edges          — typed edges (the graph)
--   2. blueprint_semantic_edge_integrity_reports — orphan/stale scanner output
--   3. blueprint_semantic_edge_generation_receipts — per-generator audit trail
-- ---------------------------------------------------------------------------

-- ===========================================================================
-- TABLE 1: blueprint_semantic_edges
-- ===========================================================================

CREATE TABLE IF NOT EXISTS public.blueprint_semantic_edges (
  edge_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Tenant scoping (Law #6)
  suite_id             uuid NOT NULL,
  office_id            uuid,
  project_id           uuid NOT NULL REFERENCES public.blueprint_projects(id) ON DELETE CASCADE,

  -- Polymorphic source — no FK constraint (spans 6 entity tables)
  -- Integrity validated by companion nightly scanner (TABLE 2).
  src_entity_id        uuid NOT NULL,
  src_entity_type      text NOT NULL
    CHECK (src_entity_type IN
      ('primitive','composed_symbol','legend','sheet','zone','ocr_span')),
  src_entity_version   bigint NOT NULL DEFAULT 1,
  src_snapshot_id      uuid,           -- nullable; populated when snapshot system lands

  -- Polymorphic destination — same rules
  dst_entity_id        uuid NOT NULL,
  dst_entity_type      text NOT NULL
    CHECK (dst_entity_type IN
      ('primitive','composed_symbol','legend','sheet','zone','ocr_span')),
  dst_entity_version   bigint NOT NULL DEFAULT 1,
  dst_snapshot_id      uuid,

  -- 20-type taxonomy (4 classes)
  edge_type            text NOT NULL
    CHECK (edge_type IN (
      -- Referential (4)
      'annotated_by','references_detail','calls_out','legend_entry_for',
      -- Structural (5)
      'member_of_family','belongs_to_sheet','inside_zone','adjacent_to','aligned_with',
      -- Functional (7)
      'connected_to','feeds','returns_to','protected_by','controls','serves','terminates_at',
      -- Constraint (4)
      'constrained_by','discipline_compatible_with','requires','excludes'
    )),

  -- MANDATORY locality enforcement (reviewer #4 — guards against O(n²) explosion)
  locality_scope       text NOT NULL
    CHECK (locality_scope IN (
      'same_zone',          -- both entities inside same semantic zone
      'same_sheet',         -- both entities on same sheet
      'adjacent_zone',      -- entities in neighboring zones (e.g. legend row → adjacent label)
      'cross_sheet_ref',    -- detail callouts, schedule references — limited cross-sheet
      'project_global'      -- ONLY for legend_entry_for, legend table rows — explicit project-wide
    )),
  spatial_distance     real,           -- nullable for non-spatial edges (e.g. legend_entry_for)

  -- Scoring + evidence
  confidence           real NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
  evidence             jsonb NOT NULL DEFAULT '{}'::jsonb,
  provenance           jsonb NOT NULL DEFAULT '{}'::jsonb,
  extraction_method    text NOT NULL,

  -- Audit
  created_at           timestamptz NOT NULL DEFAULT now(),
  created_by           text NOT NULL DEFAULT 'system',

  -- Idempotency (re-runs of same extractor produce same UPSERT-NOP)
  UNIQUE (project_id, src_entity_id, dst_entity_id, edge_type,
          extraction_method, src_entity_version, dst_entity_version)
);

-- Indexes for graph traversal (reviewer #5)
CREATE INDEX IF NOT EXISTS idx_bsedges_project_src_type
  ON public.blueprint_semantic_edges (project_id, src_entity_id, edge_type);

CREATE INDEX IF NOT EXISTS idx_bsedges_project_dst_type
  ON public.blueprint_semantic_edges (project_id, dst_entity_id, edge_type);

CREATE INDEX IF NOT EXISTS idx_bsedges_project_type
  ON public.blueprint_semantic_edges (project_id, edge_type);

CREATE INDEX IF NOT EXISTS idx_bsedges_suite_created
  ON public.blueprint_semantic_edges (suite_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bsedges_project_locality
  ON public.blueprint_semantic_edges (project_id, locality_scope);

CREATE INDEX IF NOT EXISTS idx_bsedges_src_type
  ON public.blueprint_semantic_edges (src_entity_type);

-- Append-only enforcement (Law #2 — receipts are immutable)
CREATE OR REPLACE FUNCTION public.bsedges_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION
    'blueprint_semantic_edges is append-only — UPDATE/DELETE forbidden (Aspire Law #2)'
    USING ERRCODE = 'feature_not_supported';
END;
$$;

CREATE TRIGGER trg_bsedges_no_update
  BEFORE UPDATE ON public.blueprint_semantic_edges
  FOR EACH ROW EXECUTE FUNCTION public.bsedges_append_only();

CREATE TRIGGER trg_bsedges_no_delete
  BEFORE DELETE ON public.blueprint_semantic_edges
  FOR EACH ROW EXECUTE FUNCTION public.bsedges_append_only();

-- RLS (Law #6 — split SELECT / INSERT, reviewer #3)
ALTER TABLE public.blueprint_semantic_edges ENABLE ROW LEVEL SECURITY;

CREATE POLICY bsedges_select ON public.blueprint_semantic_edges
  FOR SELECT
  USING (suite_id = NULLIF(current_setting('app.current_suite_id', true), '')::uuid);

CREATE POLICY bsedges_insert ON public.blueprint_semantic_edges
  FOR INSERT
  WITH CHECK (suite_id = NULLIF(current_setting('app.current_suite_id', true), '')::uuid);

-- NOTE: no UPDATE/DELETE policies declared — combined with the trigger above,
-- write attempts are doubly blocked (RLS denies, trigger raises).

GRANT SELECT, INSERT ON public.blueprint_semantic_edges TO authenticated;
GRANT ALL ON public.blueprint_semantic_edges TO service_role;

COMMENT ON TABLE public.blueprint_semantic_edges IS
  'Wave 20.14.5L CSIR Phase 3 — typed semantic edge fabric. Append-only ledger. 20-edge taxonomy. Substrate for Phase 4 (constraint extraction) + Phase 5 (referential binding). NEVER global O(n²) — locality_scope CHECK enforces bounded generation.';

COMMENT ON COLUMN public.blueprint_semantic_edges.locality_scope IS
  'Mandatory anti-explosion guard. same_zone < same_sheet < adjacent_zone < cross_sheet_ref < project_global. project_global ONLY valid for legend_entry_for + constraint edges.';

COMMENT ON COLUMN public.blueprint_semantic_edges.src_entity_version IS
  'Immutable version reference. When source entity is updated (e.g. OCR correction, motif regrouping), new edges must reference the new version. Old edges remain pointing to the old version — replayability preserved.';

COMMENT ON COLUMN public.blueprint_semantic_edges.src_snapshot_id IS
  'Future snapshot table reference. NULL until snapshot system ships. When NULL, src_entity_version is the version key.';

COMMENT ON COLUMN public.blueprint_semantic_edges.evidence IS
  'Edge-type-specific evidence payload. annotated_by: {ocr_span, distance_px, alignment, zone}. inside_zone: {zone_id, containment_method}. member_of_family: {family_id, motif_hash}. Schema validated at app layer (semantic_edges/taxonomy.py).';

COMMENT ON COLUMN public.blueprint_semantic_edges.provenance IS
  'Mandatory provenance: {extractor, model, wave, pipeline_version, sheet_id, generator_window, bbox}. Receipt-replayable.';


-- ===========================================================================
-- TABLE 2: blueprint_semantic_edge_integrity_reports
-- ===========================================================================
-- Reviewer #6 — polymorphic refs require nightly orphan scanner.
-- Reports persisted append-only for forensic + governance.

CREATE TABLE IF NOT EXISTS public.blueprint_semantic_edge_integrity_reports (
  report_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id               uuid NOT NULL,
  project_id             uuid NOT NULL REFERENCES public.blueprint_projects(id) ON DELETE CASCADE,

  run_at                 timestamptz NOT NULL DEFAULT now(),
  scanner_version        text NOT NULL,

  edges_scanned          bigint NOT NULL,
  orphan_src_count       bigint NOT NULL DEFAULT 0,   -- src_entity_id no longer exists
  orphan_dst_count       bigint NOT NULL DEFAULT 0,
  stale_version_count    bigint NOT NULL DEFAULT 0,   -- entity exists but version mismatch
  invalid_locality_count bigint NOT NULL DEFAULT 0,   -- edges where locality_scope contradicts geometry
  ttl_expired_count      bigint NOT NULL DEFAULT 0,

  details                jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bsedge_integrity_project_run
  ON public.blueprint_semantic_edge_integrity_reports (project_id, run_at DESC);

CREATE INDEX IF NOT EXISTS idx_bsedge_integrity_suite_run
  ON public.blueprint_semantic_edge_integrity_reports (suite_id, run_at DESC);

-- Append-only
CREATE TRIGGER trg_bsedge_integrity_no_update
  BEFORE UPDATE ON public.blueprint_semantic_edge_integrity_reports
  FOR EACH ROW EXECUTE FUNCTION public.bsedges_append_only();

CREATE TRIGGER trg_bsedge_integrity_no_delete
  BEFORE DELETE ON public.blueprint_semantic_edge_integrity_reports
  FOR EACH ROW EXECUTE FUNCTION public.bsedges_append_only();

ALTER TABLE public.blueprint_semantic_edge_integrity_reports ENABLE ROW LEVEL SECURITY;

CREATE POLICY bsedge_integrity_select ON public.blueprint_semantic_edge_integrity_reports
  FOR SELECT
  USING (suite_id = NULLIF(current_setting('app.current_suite_id', true), '')::uuid);

CREATE POLICY bsedge_integrity_insert ON public.blueprint_semantic_edge_integrity_reports
  FOR INSERT
  WITH CHECK (suite_id = NULLIF(current_setting('app.current_suite_id', true), '')::uuid);

GRANT SELECT, INSERT ON public.blueprint_semantic_edge_integrity_reports TO authenticated;
GRANT ALL ON public.blueprint_semantic_edge_integrity_reports TO service_role;

COMMENT ON TABLE public.blueprint_semantic_edge_integrity_reports IS
  'Wave 20.14.5L companion — nightly orphan + stale-version scanner output for blueprint_semantic_edges. Required because polymorphic entity refs (src/dst) have no FK constraint.';


-- ===========================================================================
-- TABLE 3: blueprint_semantic_edge_generation_receipts
-- ===========================================================================
-- Per-generator audit trail. Replay-able. UNKNOWN_REVIEW tracked first-class.

CREATE TABLE IF NOT EXISTS public.blueprint_semantic_edge_generation_receipts (
  receipt_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id               uuid NOT NULL,
  office_id              uuid,
  project_id             uuid NOT NULL REFERENCES public.blueprint_projects(id) ON DELETE CASCADE,
  sheet_id               uuid REFERENCES public.blueprint_sheets(id) ON DELETE SET NULL,

  generator_name         text NOT NULL,
  extraction_method      text NOT NULL,
  pipeline_version       text NOT NULL,

  -- Output stats
  edges_emitted          bigint NOT NULL,
  edges_by_type          jsonb NOT NULL DEFAULT '{}'::jsonb,
  edges_by_locality      jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- Multi-stage gating stats
  candidates_considered  bigint,
  cheap_filter_kept      bigint,
  medium_filter_kept     bigint,
  expensive_filter_kept  bigint,

  -- Locality stats (anti-explosion observability)
  avg_neighborhood_size  real,
  max_neighborhood_size  integer,
  locality_violations    bigint NOT NULL DEFAULT 0,

  -- UNKNOWN_REVIEW first-class
  unknown_review_count   bigint NOT NULL DEFAULT 0,
  unknown_review_reasons jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- Cost + runtime
  runtime_ms             real NOT NULL,
  cost_usd               real,

  -- Replay determinism
  input_evidence_hash    text,
  output_edge_hash       text,

  created_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bsedge_gen_project_method
  ON public.blueprint_semantic_edge_generation_receipts (project_id, extraction_method);

CREATE INDEX IF NOT EXISTS idx_bsedge_gen_sheet_method
  ON public.blueprint_semantic_edge_generation_receipts (sheet_id, extraction_method);

CREATE INDEX IF NOT EXISTS idx_bsedge_gen_suite_created
  ON public.blueprint_semantic_edge_generation_receipts (suite_id, created_at DESC);

-- Append-only
CREATE TRIGGER trg_bsedge_gen_no_update
  BEFORE UPDATE ON public.blueprint_semantic_edge_generation_receipts
  FOR EACH ROW EXECUTE FUNCTION public.bsedges_append_only();

CREATE TRIGGER trg_bsedge_gen_no_delete
  BEFORE DELETE ON public.blueprint_semantic_edge_generation_receipts
  FOR EACH ROW EXECUTE FUNCTION public.bsedges_append_only();

ALTER TABLE public.blueprint_semantic_edge_generation_receipts ENABLE ROW LEVEL SECURITY;

CREATE POLICY bsedge_gen_select ON public.blueprint_semantic_edge_generation_receipts
  FOR SELECT
  USING (suite_id = NULLIF(current_setting('app.current_suite_id', true), '')::uuid);

CREATE POLICY bsedge_gen_insert ON public.blueprint_semantic_edge_generation_receipts
  FOR INSERT
  WITH CHECK (suite_id = NULLIF(current_setting('app.current_suite_id', true), '')::uuid);

GRANT SELECT, INSERT ON public.blueprint_semantic_edge_generation_receipts TO authenticated;
GRANT ALL ON public.blueprint_semantic_edge_generation_receipts TO service_role;

COMMENT ON TABLE public.blueprint_semantic_edge_generation_receipts IS
  'Wave 20.14.5L companion — per-generator audit trail for edge generation. Replay-able via input_evidence_hash + output_edge_hash. UNKNOWN_REVIEW tracked first-class.';
