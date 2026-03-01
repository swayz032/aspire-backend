-- Migration 064 (applied): Add marketing/intake columns to suite_profiles
-- Fixes onboarding bootstrap failure: income_range, referral_source, industry_specialty columns missing

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
