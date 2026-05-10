-- Migration 113: Add trade_id and trade_specialty to suite_profiles
-- Pass 4 of make-sure-we-have-wise-quail plan.
--
-- PURPOSE: Drives trade-pack KB resolution and {{industry}} / {{industry_specialty}}
--          dyn_var injection in the Sarah personalization webhook.
--
-- YELLOW TIER — do NOT apply to main Supabase project without founder confirmation.
-- Apply to a branch first: mcp__supabase__create_branch + apply_migration on branch.
--
-- DOWN MIGRATION (safe to run; columns are nullable so no data loss):
--   ALTER TABLE suite_profiles DROP COLUMN IF EXISTS trade_specialty;
--   ALTER TABLE suite_profiles DROP COLUMN IF EXISTS trade_id;
--   -- WARNING: Running DOWN migration removes all trade_id / trade_specialty
--   -- values. Export tenant data before running if tenants have been configured.

-- ── UP ───────────────────────────────────────────────────────────────────────

ALTER TABLE suite_profiles
  ADD COLUMN IF NOT EXISTS trade_id TEXT
    CHECK (trade_id IN ('hvac', 'electrician', 'plumber', 'specialty_remodeler'));

COMMENT ON COLUMN suite_profiles.trade_id IS
  'Aspire vertical: hvac|electrician|plumber|specialty_remodeler. '
  'Drives trade-pack KB resolution and prompt {{industry}} dyn_var.';

ALTER TABLE suite_profiles
  ADD COLUMN IF NOT EXISTS trade_specialty TEXT;

COMMENT ON COLUMN suite_profiles.trade_specialty IS
  'Free-text specialty within the trade (e.g., "data center construction" for electrician). '
  'Populates {{industry_specialty}} dyn_var. NULL is valid — prompt degrades gracefully.';

-- Index for tenant look-ups filtered by trade vertical (useful for analytics
-- and future trade-based routing logic). Partial index excludes NULL rows.
CREATE INDEX IF NOT EXISTS idx_suite_profiles_trade_id
  ON suite_profiles (trade_id)
  WHERE trade_id IS NOT NULL;
