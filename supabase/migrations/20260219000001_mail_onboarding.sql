-- =====================================================================
-- Mail Onboarding Migration: Inbox & Mail Setup Production
-- =====================================================================
-- Purpose:
--   1. Create app.mail_onboarding_jobs — state machine for mail setup wizard
--   2. ALTER oauth_tokens — add email, scopes, token_type for Google OAuth
--
-- Depends on: 20260210000002_desktop_tables.sql (oauth_tokens, app.check_suite_access)
--             20260213000001_mail_tables.sql (app.mail_domains, app.mail_accounts)
--
-- Governance:
--   Law #2: Receipts at application layer (onboarding state transitions)
--   Law #3: Fail closed — RLS denies by default
--   Law #6: Tenant isolation — suite_id FK, ENABLE + FORCE RLS
-- =====================================================================

BEGIN;

-- =====================================================================
-- TABLE: MAIL ONBOARDING JOBS
-- State machine for mail setup wizard (Google OAuth + PolarisM BYOD paths)
-- =====================================================================

CREATE TABLE IF NOT EXISTS app.mail_onboarding_jobs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id        UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  office_id       UUID NOT NULL REFERENCES app.offices(office_id) ON DELETE CASCADE,
  correlation_id  TEXT NOT NULL DEFAULT ('corr_' || gen_random_uuid()::text),
  provider        TEXT NOT NULL CHECK (provider IN ('POLARIS', 'GOOGLE')),
  domain          TEXT,
  domain_mode     TEXT CHECK (domain_mode IN ('byod', 'buy_domain') OR domain_mode IS NULL),
  state           TEXT NOT NULL DEFAULT 'INIT' CHECK (state IN (
    'INIT',
    'DOMAIN_SELECTED',
    'POLARIS_DOMAIN_ADDED',
    'DNS_PLAN_READY',
    'VERIFYING_DOMAIN',
    'DOMAIN_VERIFIED',
    'DKIM_ENABLED',
    'VERIFYING_DNS',
    'DNS_HEALTHY',
    'MAILBOX_PROVISIONED',
    'GOOGLE_OAUTH_PENDING',
    'GOOGLE_OAUTH_COMPLETE',
    'CHECKS_RUNNING',
    'ACTIVE',
    'FAILED'
  )),
  state_updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  mailbox_email     TEXT,
  display_name      TEXT,
  verification_txt  TEXT,
  dkim_host         TEXT,
  dkim_value        TEXT,
  last_health       JSONB DEFAULT '{}',
  last_error        TEXT,
  eli_config        JSONB DEFAULT '{}',
  domain_purchase   JSONB DEFAULT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_onboarding_suite
  ON app.mail_onboarding_jobs(suite_id, state);
CREATE INDEX IF NOT EXISTS idx_onboarding_correlation
  ON app.mail_onboarding_jobs(correlation_id);

-- RLS
ALTER TABLE app.mail_onboarding_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.mail_onboarding_jobs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS mail_onboarding_jobs_select ON app.mail_onboarding_jobs;
CREATE POLICY mail_onboarding_jobs_select ON app.mail_onboarding_jobs
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_onboarding_jobs_insert ON app.mail_onboarding_jobs;
CREATE POLICY mail_onboarding_jobs_insert ON app.mail_onboarding_jobs
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_onboarding_jobs_update ON app.mail_onboarding_jobs;
CREATE POLICY mail_onboarding_jobs_update ON app.mail_onboarding_jobs
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_onboarding_jobs_delete ON app.mail_onboarding_jobs;
CREATE POLICY mail_onboarding_jobs_delete ON app.mail_onboarding_jobs
  FOR DELETE TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_onboarding_jobs_service_role ON app.mail_onboarding_jobs;
CREATE POLICY mail_onboarding_jobs_service_role ON app.mail_onboarding_jobs
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON app.mail_onboarding_jobs TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.mail_onboarding_jobs TO service_role;

-- Updated_at trigger
DROP TRIGGER IF EXISTS set_updated_at ON app.mail_onboarding_jobs;
CREATE TRIGGER set_updated_at
  BEFORE UPDATE ON app.mail_onboarding_jobs
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- =====================================================================
-- ALTER: OAUTH_TOKENS — Add Google OAuth columns
-- oauth_tokens is in public schema (from desktop_tables migration)
-- =====================================================================

ALTER TABLE oauth_tokens
  ADD COLUMN IF NOT EXISTS email TEXT,
  ADD COLUMN IF NOT EXISTS scopes TEXT[],
  ADD COLUMN IF NOT EXISTS token_type TEXT DEFAULT 'Bearer';

-- =====================================================================
-- COMPLETION
-- =====================================================================

COMMIT;

-- Migration complete:
--   1. app.mail_onboarding_jobs: 15-state machine, dual-path RLS, FORCE RLS
--   2. oauth_tokens: +email, +scopes, +token_type columns
