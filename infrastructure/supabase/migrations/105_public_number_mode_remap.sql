-- =============================================================================
-- Migration 105: PublicNumberMode data remap (Pass 19 — Lane B §3.1)
-- =============================================================================
-- Idempotent data migration: remap existing front_desk_configs rows from the
-- old 2-mode enum values to the new honest 3-mode values (§3.1 design decision).
--
-- Remap table:
--   KEEP_CURRENT_NUMBER  → FORWARD_EXISTING
--   ASPIRE_NUMBER        → ASPIRE_NEW_NUMBER
--   ASPIRE_NEW_NUMBER    → (already correct — idempotent, no-op)
--   FORWARD_EXISTING     → (already correct — idempotent, no-op)
--   PORT_IN              → (already correct — idempotent, no-op)
--
-- Aspire Laws:
--   Law #2: No UPDATE on receipts — this table is config, not a receipt table.
--           front_desk_configs uses append-only versioning (is_current flag)
--           but historical version rows must also be remapped so old versions
--           retain consistent enum semantics.
--   Law #3: Fail closed — only remap known stale values; never touch values
--           that are already on the new enum.
-- =============================================================================

-- Remap KEEP_CURRENT_NUMBER → FORWARD_EXISTING
UPDATE public.front_desk_configs
SET    public_number_mode = 'FORWARD_EXISTING'
WHERE  public_number_mode = 'KEEP_CURRENT_NUMBER';

-- Remap ASPIRE_NUMBER → ASPIRE_NEW_NUMBER
UPDATE public.front_desk_configs
SET    public_number_mode = 'ASPIRE_NEW_NUMBER'
WHERE  public_number_mode = 'ASPIRE_NUMBER';

-- Verify: no stale values remain (advisory check only — does not fail the migration)
DO $$
DECLARE
  stale_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO stale_count
  FROM public.front_desk_configs
  WHERE public_number_mode NOT IN (
    'ASPIRE_NEW_NUMBER',
    'FORWARD_EXISTING',
    'PORT_IN'
  );

  IF stale_count > 0 THEN
    RAISE WARNING '105_public_number_mode_remap: % row(s) have unexpected public_number_mode values after remap', stale_count;
  ELSE
    RAISE NOTICE '105_public_number_mode_remap: remap complete — all rows on canonical enum values';
  END IF;
END $$;
