-- Track B Pass 1.C: Extend receipts.status CHECK constraint to allow
-- SUCCEEDED_WITH_ZERO_OUTPUT for write-producing stages that legitimately
-- wrote zero rows (e.g., legend_extracted on sheets with no legend elements).
--
-- Old constraint: status IN ('PENDING','SUCCEEDED','FAILED','DENIED')
-- New constraint: status IN ('PENDING','SUCCEEDED','FAILED','DENIED','SUCCEEDED_WITH_ZERO_OUTPUT')
--
-- Applied 2026-05-26.

-- Drop the old check constraint (name from 20260210000001_trust_spine_bundle.sql).
-- PostgREST / Supabase names CHECK constraints as <table>_<column>_check by default
-- when defined inline; we use ALTER TABLE to be safe regardless of generated name.
ALTER TABLE public.receipts
  DROP CONSTRAINT IF EXISTS receipts_status_check;

ALTER TABLE public.receipts
  ADD CONSTRAINT receipts_status_check
  CHECK (status IN ('PENDING','SUCCEEDED','FAILED','DENIED','SUCCEEDED_WITH_ZERO_OUTPUT'));
