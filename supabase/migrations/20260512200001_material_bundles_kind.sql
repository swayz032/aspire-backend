-- Migration: 20260512200001_material_bundles_kind
--
-- Pass E: Add `kind` column to `material_bundles` table.
-- Discriminates between a standard product bundle (Tool mode) and a
-- supplier line-item (Supplier mode, used for "Draft RFQ" flow in Pass G).
--
-- kind = 'product'       -- default; a retail product from Home Depot
-- kind = 'supplier_line' -- a commercial/specialty line-item sourced from Yelp supplier
--
-- Existing rows are backfilled to 'product' (they were all created in Tool mode).
-- RLS policies on material_bundles are unchanged (tenant-scoped via suite_id).
--
-- Law #2: This migration is append-only. No existing rows are deleted.
-- Law #6: RLS enforcement is on the bundles table level -- not on this column.

-- 1. Create the constraint type (idempotent -- DO NOTHING on conflict)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_type
    WHERE typname = 'material_bundle_kind'
    AND typtype = 'e'
  ) THEN
    CREATE TYPE public.material_bundle_kind AS ENUM ('product', 'supplier_line');
  END IF;
END
$$;

-- 2. Add column with default 'product' (non-breaking -- existing rows get default immediately)
ALTER TABLE public.material_bundles
  ADD COLUMN IF NOT EXISTS kind public.material_bundle_kind NOT NULL DEFAULT 'product';

-- 3. Backfill any NULLs that slipped through before NOT NULL was enforced
--    (defensive -- ADD COLUMN with DEFAULT should cover this, but belt-and-suspenders)
UPDATE public.material_bundles
  SET kind = 'product'
  WHERE kind IS NULL;

-- 4. Index for filtering bundles by kind per tenant
--    (suite_id is already in the index from material_bundles base migration;
--     we add kind as a secondary filter for Push-to-Estimate queries)
CREATE INDEX IF NOT EXISTS idx_material_bundles_suite_kind
  ON public.material_bundles(suite_id, kind);

-- Verify
DO $$
DECLARE
  _col_exists boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
    AND table_name = 'material_bundles'
    AND column_name = 'kind'
  ) INTO _col_exists;

  IF NOT _col_exists THEN
    RAISE EXCEPTION 'material_bundles.kind column was not created -- migration failed';
  END IF;
END
$$;
