-- Wave 20.6.1b (plan v7 §2.7.1): blueprint_project_legends
-- Per-project symbol dictionary extracted from sheet legends.
-- Stages 2-6 of Construction Semantic Compiler consume this as ground truth.

CREATE TABLE IF NOT EXISTS public.blueprint_project_legends (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id              uuid NOT NULL,
  office_id             uuid,
  project_id            uuid NOT NULL REFERENCES public.blueprint_projects(id) ON DELETE CASCADE,
  discipline            text NOT NULL,
  symbol_id             text NOT NULL,
  label                 text,
  specification         text,
  vector_signature      text,
  representation_bbox   jsonb,
  source_sheet_id       uuid REFERENCES public.blueprint_sheets(id),
  source_region_index   int,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, discipline, symbol_id)
);

CREATE INDEX IF NOT EXISTS idx_bp_project_legends_project ON public.blueprint_project_legends (project_id);
CREATE INDEX IF NOT EXISTS idx_bp_project_legends_discipline ON public.blueprint_project_legends (project_id, discipline);
CREATE INDEX IF NOT EXISTS idx_bp_project_legends_signature ON public.blueprint_project_legends (vector_signature) WHERE vector_signature IS NOT NULL;

ALTER TABLE public.blueprint_project_legends ENABLE ROW LEVEL SECURITY;

CREATE POLICY bp_project_legends_tenant_iso ON public.blueprint_project_legends
  FOR ALL USING (suite_id = current_setting('app.current_suite_id', true)::uuid);

COMMENT ON TABLE public.blueprint_project_legends IS
  'Wave 20.6: per-project symbol dictionary. Source of truth for Wave 20.6 Stages 2-6 (canonical symbolization, embeddings, clustering, context binding). Replaces global symbol_memory as resolution source.';
COMMENT ON COLUMN public.blueprint_project_legends.vector_signature IS
  'Wave 20.6.3: populated by Symbol Family Embeddings (topology graph hash). NULL until Stage 3 runs.';
