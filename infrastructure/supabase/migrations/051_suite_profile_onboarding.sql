-- Phase 3 W3: Onboarding columns for suite_profiles
-- Supports the 3-step founder onboarding wizard

ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS owner_name TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS owner_title TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS industry TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS team_size TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS services_needed TEXT[] DEFAULT '{}';
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS current_tools TEXT[] DEFAULT '{}';
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS pain_point TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMPTZ;
