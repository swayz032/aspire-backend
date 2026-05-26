-- Track B Pass 1.C: Extend receipts.status CHECK constraint to allow
-- SUCCEEDED_WITH_ZERO_OUTPUT for write-producing stages that legitimately
-- wrote zero rows (e.g., legend_extracted on sheets with no legend elements).
--
-- Old constraint (declared): status IN ('PENDING','SUCCEEDED','FAILED','DENIED')
-- Production reality:        SKIPPED was also in active use (17,989 rows at
--                            apply time) — bypassed the original CHECK somehow.
-- New constraint:            status IN ('PENDING','SUCCEEDED','FAILED','DENIED',
--                            'SKIPPED','SUCCEEDED_WITH_ZERO_OUTPUT')
--
-- Applied 2026-05-26.
-- Follow-up FU-TRACK-B-PRE-4: audit how SKIPPED bypassed the original CHECK.

ALTER TABLE public.receipts
  DROP CONSTRAINT IF EXISTS receipts_status_check;

ALTER TABLE public.receipts
  ADD CONSTRAINT receipts_status_check
  CHECK (status IN (
    'PENDING','SUCCEEDED','FAILED','DENIED','SKIPPED','SUCCEEDED_WITH_ZERO_OUTPUT'
  ));
