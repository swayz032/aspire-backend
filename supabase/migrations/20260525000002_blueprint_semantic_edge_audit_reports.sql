-- Wave 20.14.5L L-7 — Audit receipts table for the L-6 audit harness.
-- Separate from blueprint_semantic_edge_integrity_reports (which is the
-- nightly orphan-scanner output). This table holds per-audit precision
-- + sampled-edge verdicts so Phase 3 acceptance + Phase 5 binding
-- recall can be re-measured later.
--
-- Reuses the bsedges_append_only() trigger function from migration
-- 20260525000001 (Law #2: receipts are immutable).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.blueprint_semantic_edge_audit_reports (
  audit_id            text PRIMARY KEY,
                       -- 24-char SHA-1 prefix from L-6 audit.audit_edges();
                       -- deterministic over (generator + project + sample + seed)

  -- Tenant scoping (Law #6)
  suite_id            uuid NOT NULL,
  office_id           uuid,
  project_id          uuid NOT NULL REFERENCES public.blueprint_projects(id) ON DELETE CASCADE,

  -- Provenance: which generator was audited
  generator_name      text NOT NULL,
  generator_version   text NOT NULL,
  taxonomy_version    text,
  pipeline_version    text,

  -- Verifier identity
  verifier_name       text NOT NULL,                    -- 'rule_based_v1' | 'oracle' | 'vlm_v1' (future)
  verifier_version    text NOT NULL,

  -- Sample shape
  sample_size         integer NOT NULL,
  total_edges         integer NOT NULL,                 -- universe before sampling
  sample_seed         integer NOT NULL DEFAULT 0,

  -- Threshold + outcome
  threshold           real NOT NULL,
  passes_threshold    boolean NOT NULL,
  failing_types       text[] NOT NULL DEFAULT '{}',

  -- Aggregated metrics
  verdict_counts      jsonb NOT NULL DEFAULT '{}'::jsonb,
                       -- {'true_positive': N, 'false_positive': N, ...}
  per_type_precision  jsonb NOT NULL DEFAULT '{}'::jsonb,
                       -- {'annotated_by': 0.91, 'adjacent_to': 0.88, ...}

  -- Sampled audited edges (compact projection — not the full edge row;
  -- the full edges live in blueprint_semantic_edges already)
  audited_edges       jsonb NOT NULL DEFAULT '[]'::jsonb,

  -- Governance authority: who is allowed to write this row
  writer_authority    text NOT NULL CHECK (writer_authority IN (
                        'drew_pipeline', 'manual_cli', 'runtime_reconciler', 'l6_smoke_test'
                      )),
  created_by          text NOT NULL DEFAULT 'system',

  created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bsedge_audit_project_created
  ON public.blueprint_semantic_edge_audit_reports (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bsedge_audit_suite_created
  ON public.blueprint_semantic_edge_audit_reports (suite_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bsedge_audit_generator
  ON public.blueprint_semantic_edge_audit_reports (project_id, generator_name);

CREATE INDEX IF NOT EXISTS idx_bsedge_audit_passing
  ON public.blueprint_semantic_edge_audit_reports (project_id, passes_threshold);

-- Append-only enforcement (Law #2) — reuses the shared trigger fn
CREATE TRIGGER trg_bsedge_audit_no_update
  BEFORE UPDATE ON public.blueprint_semantic_edge_audit_reports
  FOR EACH ROW EXECUTE FUNCTION public.bsedges_append_only();

CREATE TRIGGER trg_bsedge_audit_no_delete
  BEFORE DELETE ON public.blueprint_semantic_edge_audit_reports
  FOR EACH ROW EXECUTE FUNCTION public.bsedges_append_only();

-- RLS (Law #6) — split SELECT / INSERT policies per reviewer L-1 pattern
ALTER TABLE public.blueprint_semantic_edge_audit_reports ENABLE ROW LEVEL SECURITY;

CREATE POLICY bsedge_audit_select ON public.blueprint_semantic_edge_audit_reports
  FOR SELECT
  USING (suite_id = NULLIF(current_setting('app.current_suite_id', true), '')::uuid);

CREATE POLICY bsedge_audit_insert ON public.blueprint_semantic_edge_audit_reports
  FOR INSERT
  WITH CHECK (suite_id = NULLIF(current_setting('app.current_suite_id', true), '')::uuid);

-- Note: no UPDATE/DELETE policies declared — combined with the trigger,
-- writes are doubly blocked (RLS denies + trigger raises).

GRANT SELECT, INSERT ON public.blueprint_semantic_edge_audit_reports TO authenticated;
GRANT ALL ON public.blueprint_semantic_edge_audit_reports TO service_role;

COMMENT ON TABLE public.blueprint_semantic_edge_audit_reports IS
  'Wave 20.14.5L L-7 — append-only audit receipts. Output of L-6 audit_edges() runs persisted for Phase 3 acceptance + Phase 5 binding-recall re-measurement. Append-only; writer_authority gates which agent identities may insert.';

COMMENT ON COLUMN public.blueprint_semantic_edge_audit_reports.writer_authority IS
  'Phase 3 lightweight capability gate. Phase 4/5 will replace with real capability_tokens FK (Law #5).';

COMMENT ON COLUMN public.blueprint_semantic_edge_audit_reports.audited_edges IS
  'Compact projection of sampled edges: [{edge_type, src, dst, replay_hash, confidence, verdict, rationale}]. Full edges live in blueprint_semantic_edges.';
