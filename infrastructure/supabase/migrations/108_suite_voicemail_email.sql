-- 108_suite_voicemail_email.sql
-- ----------------------------------------------------------------------------
-- Adds a dedicated voicemail_email column to suite_profiles so the Front Desk
-- Setup page can route voicemail transcripts to a specific inbox without
-- changing the owner's signup email.
--
-- Background: prior to this migration, sarah.py's _fetch_profile fell back
-- to suite_profiles.email when no dedicated voicemail address existed (a
-- soft fallback added in the personalization-wiring fix). With this column,
-- _fetch_profile now prefers an explicit voicemail address; absence still
-- falls back to suite_profiles.email so existing tenants keep working.
--
-- Contract: column is nullable. RLS already in place on suite_profiles
-- (see 051_suite_profile_onboarding.sql) — no changes required.
-- ----------------------------------------------------------------------------

ALTER TABLE suite_profiles
  ADD COLUMN IF NOT EXISTS voicemail_email TEXT;

COMMENT ON COLUMN suite_profiles.voicemail_email IS
  'Optional dedicated email address for Sarah voicemail transcripts. '
  'When NULL, _fetch_profile in routes/sarah.py falls back to suite_profiles.email.';
