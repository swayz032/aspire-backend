-- Migration: serpapi_budget_and_materials_cache
-- Pass A: SerpApi dual-account budget plumbing + Supabase persistent counter
-- Applied: 2026-05-12

-- serpapi_budget: persistent dual-account monthly counter
CREATE TABLE IF NOT EXISTS public.serpapi_budget (
    month        TEXT    NOT NULL,
    account_id   TEXT    NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0),
    cap          INTEGER NOT NULL DEFAULT 240,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (month, account_id)
);
CREATE INDEX IF NOT EXISTS idx_serpapi_budget_month ON public.serpapi_budget (month);
ALTER TABLE public.serpapi_budget ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.serpapi_budget FORCE ROW LEVEL SECURITY;
CREATE POLICY serpapi_budget_service_all ON public.serpapi_budget
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- materials_search_cache: persistent SerpApi response cache (Pass C will wire write path)
-- NOTE (Pass C): strip raw thumbnails[]/specifications[] arrays before writing payload
-- to keep JSONB sizes manageable (these can be 50-200 rows * multi-KB each).
CREATE TABLE IF NOT EXISTS public.materials_search_cache (
    cache_key        TEXT        NOT NULL PRIMARY KEY,
    query_normalized TEXT        NOT NULL,
    store_id         TEXT        NOT NULL DEFAULT '',
    engine           TEXT        NOT NULL CHECK (engine IN ('home_depot','shopping','home_depot_product')),
    payload          JSONB       NOT NULL,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msc_expires_at ON public.materials_search_cache (expires_at);
CREATE INDEX IF NOT EXISTS idx_msc_engine_store ON public.materials_search_cache (engine, store_id);
ALTER TABLE public.materials_search_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.materials_search_cache FORCE ROW LEVEL SECURITY;
CREATE POLICY msc_service_all ON public.materials_search_cache
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- increment_serpapi_budget: atomic increment with cap enforcement
-- Returns new count, or NULL if account was already at cap (no increment)
CREATE OR REPLACE FUNCTION public.increment_serpapi_budget(
    p_month      TEXT,
    p_account_id TEXT,
    p_cap        INTEGER DEFAULT 240
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $$
DECLARE
    v_new_count INTEGER;
BEGIN
    INSERT INTO public.serpapi_budget (month, account_id, count, cap)
    VALUES (p_month, p_account_id, 0, p_cap)
    ON CONFLICT (month, account_id) DO NOTHING;

    UPDATE public.serpapi_budget
    SET    count = count + 1, updated_at = now()
    WHERE  month = p_month AND account_id = p_account_id AND count < p_cap
    RETURNING count INTO v_new_count;

    RETURN v_new_count;
END;
$$;

GRANT EXECUTE ON FUNCTION public.increment_serpapi_budget(TEXT, TEXT, INTEGER) TO service_role;

-- pg_cron monthly reset (graceful degradation if pg_cron not installed)
DO $$
BEGIN
  PERFORM cron.schedule(
    'serpapi-budget-monthly-reset',
    '0 0 1 * *',
    $cron$
      UPDATE public.serpapi_budget
      SET    count = 0, updated_at = now()
      WHERE  month = to_char((now() AT TIME ZONE 'UTC') - interval '1 month', 'YYYY-MM');
    $cron$
  );
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'pg_cron not available — manual monthly reset required';
END $$;
