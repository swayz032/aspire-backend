-- Pass D: material_bundles table
-- Tenant-scoped per (project_id, suite_id). Full Product snapshot stored for
-- receipt-grade audit (§8.4 — timestamps must survive downstream changes).
-- All writes go through the backend route using service_role; authenticated
-- users get SELECT only via RLS so the frontend can hydrate via Supabase.

CREATE TABLE IF NOT EXISTS public.material_bundles (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id          TEXT NOT NULL,
  -- Normalized lowercase project address (or real project_id in future).
  suite_id            UUID NOT NULL,
  office_id           UUID NOT NULL,
  product_payload     JSONB NOT NULL,
  -- Full Product snapshot: id, title, brand, price, sku, imageUrl, store, etc.
  store_id            TEXT,
  category_hint       TEXT,
  quantity            NUMERIC NOT NULL DEFAULT 1 CHECK (quantity > 0),
  unit_price          NUMERIC,
  fetched_at          TIMESTAMPTZ NOT NULL,
  pushed_to_estimate  BOOLEAN NOT NULL DEFAULT false,
  estimate_draft_id   UUID,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by          UUID
);

CREATE INDEX IF NOT EXISTS idx_bundles_project
  ON public.material_bundles (project_id, suite_id, pushed_to_estimate);

CREATE INDEX IF NOT EXISTS idx_bundles_suite
  ON public.material_bundles (suite_id);

ALTER TABLE public.material_bundles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.material_bundles FORCE ROW LEVEL SECURITY;

-- Service role full access (backend writes via service_role key)
CREATE POLICY material_bundles_service_all ON public.material_bundles
  FOR ALL TO service_role
  USING (true)
  WITH CHECK (true);

-- Authenticated users see only their own suite's bundles (read-only;
-- writes go through the backend route which uses service_role)
CREATE POLICY material_bundles_authenticated_select ON public.material_bundles
  FOR SELECT TO authenticated
  USING (suite_id = (auth.jwt() ->> 'suite_id')::uuid);
