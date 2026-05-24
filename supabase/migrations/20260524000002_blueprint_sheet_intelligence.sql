-- Wave 20a (plan v7 §4.2): blueprint_sheet_intelligence
-- Caches Azure prebuilt-layout output per sheet at INGEST time so downstream
-- stages (SEE / REASON / cascade resolvers / Wave 20.6 semantic compiler) read
-- structured layout from the cache instead of re-running Azure per consumer.

CREATE TABLE IF NOT EXISTS public.blueprint_sheet_intelligence (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id        uuid NOT NULL,
  office_id       uuid,
  project_id      uuid NOT NULL REFERENCES public.blueprint_projects(id) ON DELETE CASCADE,
  sheet_id        uuid NOT NULL REFERENCES public.blueprint_sheets(id) ON DELETE CASCADE,
  tables          jsonb NOT NULL DEFAULT '[]'::jsonb,     -- list of {bbox, headers, rows, region_type}
  paragraphs      jsonb NOT NULL DEFAULT '[]'::jsonb,     -- list of {bbox, text, role}
  key_value_pairs jsonb NOT NULL DEFAULT '[]'::jsonb,     -- list of {key, value, confidence}
  page_count      int,
  regions         jsonb NOT NULL DEFAULT '{}'::jsonb,     -- {legend: [...], schedule: [...], notes: [...], plan: [...], detail: [...]}
  extracted_at    timestamptz NOT NULL DEFAULT now(),
  azure_model     text NOT NULL DEFAULT 'prebuilt-layout',
  azure_call_ms   int,
  UNIQUE (project_id, sheet_id)
);

CREATE INDEX IF NOT EXISTS idx_bp_sheet_intel_project ON public.blueprint_sheet_intelligence (project_id);
CREATE INDEX IF NOT EXISTS idx_bp_sheet_intel_suite ON public.blueprint_sheet_intelligence (suite_id);
CREATE INDEX IF NOT EXISTS idx_bp_sheet_intel_regions ON public.blueprint_sheet_intelligence USING gin (regions);
CREATE INDEX IF NOT EXISTS idx_bp_sheet_intel_tables ON public.blueprint_sheet_intelligence USING gin (tables);

ALTER TABLE public.blueprint_sheet_intelligence ENABLE ROW LEVEL SECURITY;

CREATE POLICY bp_sheet_intel_tenant_iso ON public.blueprint_sheet_intelligence
  FOR ALL USING (suite_id = current_setting('app.current_suite_id', true)::uuid);

COMMENT ON TABLE public.blueprint_sheet_intelligence IS
  'Wave 20: per-sheet Azure layout cache. Feeds Wave 20.6 Construction Semantic Compiler.';
COMMENT ON COLUMN public.blueprint_sheet_intelligence.regions IS
  'Wave 20: region classification — {legend, schedule, notes, plan, detail} → list of bbox+sheet_context. Critical for Wave 20.6 legend extraction.';
