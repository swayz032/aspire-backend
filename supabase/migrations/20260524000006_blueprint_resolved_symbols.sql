-- Wave 20.6 Stage 5c: blueprint_resolved_symbols
-- Truth surface for material aggregation + estimating.
-- Replaces 99%-noise blueprint_symbols for downstream consumers.
-- Populated by symbol_cluster_engine.propagate_legend_bindings_to_clusters().

CREATE TABLE IF NOT EXISTS public.blueprint_resolved_symbols (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id            uuid NOT NULL,
  project_id          uuid NOT NULL REFERENCES public.blueprint_projects(id) ON DELETE CASCADE,
  sheet_id            uuid NOT NULL REFERENCES public.blueprint_sheets(id) ON DELETE CASCADE,
  legend_id           uuid REFERENCES public.blueprint_project_legends(id),
  bbox                jsonb NOT NULL,
  symbol_name         text,
  confidence          numeric,
  resolution_source   text NOT NULL,
  cluster_id          text,
  motif_hash          text,
  created_at          timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_resolved_symbols_project
  ON public.blueprint_resolved_symbols (project_id);

CREATE INDEX IF NOT EXISTS idx_resolved_symbols_legend
  ON public.blueprint_resolved_symbols (legend_id);

CREATE INDEX IF NOT EXISTS idx_resolved_symbols_cluster
  ON public.blueprint_resolved_symbols (cluster_id);

CREATE INDEX IF NOT EXISTS idx_resolved_symbols_suite_resolution
  ON public.blueprint_resolved_symbols (suite_id, resolution_source);

ALTER TABLE public.blueprint_resolved_symbols ENABLE ROW LEVEL SECURITY;

CREATE POLICY resolved_symbols_tenant_iso ON public.blueprint_resolved_symbols
  FOR ALL USING (suite_id = current_setting('app.current_suite_id', true)::uuid);

COMMENT ON TABLE public.blueprint_resolved_symbols IS
  'Wave 20.6: legend-bound symbol instances. Replaces 99%-noise blueprint_symbols as the truth surface for material aggregation + estimating.';

COMMENT ON COLUMN public.blueprint_resolved_symbols.resolution_source IS
  'Wave 20.6: topology_match | cluster_propagation | vlm_verified | pending_vlm_verify | unknown';

COMMENT ON COLUMN public.blueprint_resolved_symbols.cluster_id IS
  'Wave 20.6: cluster_id from symbol_cluster_engine — all rows sharing a cluster_id have the same geometric motif identity.';

COMMENT ON COLUMN public.blueprint_resolved_symbols.legend_id IS
  'Wave 20.6: FK to blueprint_project_legends. NULL when resolution_source is pending_vlm_verify or unknown.';
