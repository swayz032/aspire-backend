-- 064: Marketing Fields + Premium Display IDs
-- Phase 3 W10.1: income_range, referral_source, industry_specialty for suite_profiles
-- Premium display ID seeding: first user gets STE-1001, OFF-0001

BEGIN;

-- ============================================================
-- 1. Add marketing/intake columns to suite_profiles
-- ============================================================
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS income_range TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS referral_source TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS industry_specialty TEXT;

-- CHECK: income_range (bucketed, non-PII)
DO $$ BEGIN
  ALTER TABLE suite_profiles ADD CONSTRAINT chk_sp_income_range
    CHECK (income_range IN (
      'under_25k','25k_50k','50k_75k','75k_100k',
      '100k_150k','150k_250k','250k_500k','500k_plus'
    ) OR income_range IS NULL);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- CHECK: referral_source
DO $$ BEGIN
  ALTER TABLE suite_profiles ADD CONSTRAINT chk_sp_referral_source
    CHECK (referral_source IN (
      'google_search','social_media','friend_referral','podcast',
      'blog_article','conference_event','advertisement','app_store','other'
    ) OR referral_source IS NULL);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- industry_specialty: TEXT, no CHECK (frontend enforces per-category options)

-- ============================================================
-- 2. Premium Display ID Seeding
-- ============================================================
-- Seed suite sequence at 1000 so first new user gets STE-1001 (not STE-001).
-- Makes Aspire feel established, not like a beta.
-- GREATEST() ensures we never go backward if sequence already advanced.

INSERT INTO public.display_id_sequences (entity_type, scope_id, year_scope, current_seq)
VALUES ('suite', NULL, NULL, 1000)
ON CONFLICT (entity_type, COALESCE(scope_id, '00000000-0000-0000-0000-000000000000'::uuid), COALESCE(year_scope, 0))
DO UPDATE SET current_seq = GREATEST(display_id_sequences.current_seq, 1000);

-- ============================================================
-- 3. Update suite trigger to 4-digit padding
-- ============================================================
CREATE OR REPLACE FUNCTION public.trg_suite_display_id()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE v_seq BIGINT;
BEGIN
  IF NEW.display_id IS NULL THEN
    v_seq := public.next_display_id('suite', NULL, NULL);
    NEW.display_id := 'STE-' || LPAD(v_seq::text, 4, '0');
  END IF;
  RETURN NEW;
END;
$$;

-- ============================================================
-- 4. Update office trigger to 4-digit padding
-- ============================================================
CREATE OR REPLACE FUNCTION public.trg_office_display_id()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE v_seq BIGINT;
BEGIN
  IF NEW.display_id IS NULL THEN
    v_seq := public.next_display_id('office', NEW.suite_id, NULL);
    NEW.display_id := 'OFF-' || LPAD(v_seq::text, 4, '0');
  END IF;
  RETURN NEW;
END;
$$;

COMMIT;
