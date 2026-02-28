-- =====================================================================
-- Mail Tables Migration: Phase 0C Domain Rail Foundation
-- =====================================================================
-- Purpose: Create 3 mail/domain tables with dual-path RLS
-- Depends on: Trust Spine core migrations (app.suites, app.offices,
--             app.check_suite_access), desktop_tables migration
--
-- Tables:
--   1. app.mail_domains   — domain records per suite
--   2. app.mail_dns_records — DNS verification records (SPF/DKIM/DMARC/MX)
--   3. app.mail_accounts   — email accounts per office
--
-- RLS Strategy (Dual-Path, replicated from desktop_tables):
--   Path A: PostgREST/Supabase Auth → app.check_suite_access(suite_id)
--   Path B: Express server (raw pg) → current_setting('app.current_suite_id')
--   service_role gets bypass policy for backend admin operations.
--
-- Governance Compliance:
--   Law #2: All state changes produce receipts (enforced at application layer)
--   Law #3: Fail closed — RLS denies by default
--   Law #6: Tenant isolation — UUID suite_id FK, ENABLE + FORCE RLS
--
-- Idempotency: All operations use IF NOT EXISTS or DROP...IF EXISTS
-- =====================================================================

BEGIN;

-- =====================================================================
-- TABLE 1: MAIL DOMAINS
-- Domain records per suite (one suite can own multiple domains)
-- =====================================================================

CREATE TABLE IF NOT EXISTS app.mail_domains (
  domain_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id      UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  office_id     UUID NOT NULL REFERENCES app.offices(office_id) ON DELETE CASCADE,
  domain_name   TEXT NOT NULL,
  registrar     TEXT NOT NULL DEFAULT 'resellerclub',
  status        TEXT NOT NULL DEFAULT 'pending_verification'
                CHECK (status IN (
                  'pending_verification', 'dns_propagating', 'active',
                  'suspended', 'expired', 'transfer_in', 'transfer_out',
                  'pending_purchase', 'purchase_failed', 'deleting', 'deleted'
                )),
  owner_email   TEXT,
  provider_ref  TEXT,          -- External registrar order/domain ID
  expires_at    TIMESTAMPTZ,   -- Domain expiry date from registrar
  verified_at   TIMESTAMPTZ,   -- When DNS verification completed
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(suite_id, domain_name)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_mail_domains_suite_id
  ON app.mail_domains(suite_id);
CREATE INDEX IF NOT EXISTS idx_mail_domains_suite_office
  ON app.mail_domains(suite_id, office_id);
CREATE INDEX IF NOT EXISTS idx_mail_domains_domain_name
  ON app.mail_domains(domain_name);
CREATE INDEX IF NOT EXISTS idx_mail_domains_status
  ON app.mail_domains(status);

-- RLS
ALTER TABLE app.mail_domains ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.mail_domains FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS mail_domains_select ON app.mail_domains;
CREATE POLICY mail_domains_select ON app.mail_domains
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_domains_insert ON app.mail_domains;
CREATE POLICY mail_domains_insert ON app.mail_domains
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_domains_update ON app.mail_domains;
CREATE POLICY mail_domains_update ON app.mail_domains
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_domains_delete ON app.mail_domains;
CREATE POLICY mail_domains_delete ON app.mail_domains
  FOR DELETE TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_domains_service_role ON app.mail_domains;
CREATE POLICY mail_domains_service_role ON app.mail_domains
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON app.mail_domains TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.mail_domains TO service_role;

-- =====================================================================
-- TABLE 2: MAIL DNS RECORDS
-- DNS verification records for domain setup (SPF/DKIM/DMARC/MX)
-- Linked to mail_domains via domain_id
-- =====================================================================

CREATE TABLE IF NOT EXISTS app.mail_dns_records (
  record_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  domain_id     UUID NOT NULL REFERENCES app.mail_domains(domain_id) ON DELETE CASCADE,
  suite_id      UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  record_type   TEXT NOT NULL
                CHECK (record_type IN ('MX', 'SPF', 'DKIM', 'DMARC', 'TXT', 'CNAME', 'A', 'AAAA')),
  record_name   TEXT NOT NULL DEFAULT '@',   -- e.g. '@', 'mail', '_dmarc'
  record_value  TEXT NOT NULL,               -- The actual DNS record value
  priority      INTEGER,                     -- MX priority (null for non-MX)
  ttl           INTEGER NOT NULL DEFAULT 3600,
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'propagating', 'verified', 'failed', 'stale')),
  verified_at   TIMESTAMPTZ,
  last_check_at TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_mail_dns_records_domain_id
  ON app.mail_dns_records(domain_id);
CREATE INDEX IF NOT EXISTS idx_mail_dns_records_suite_id
  ON app.mail_dns_records(suite_id);
CREATE INDEX IF NOT EXISTS idx_mail_dns_records_type
  ON app.mail_dns_records(record_type);
CREATE INDEX IF NOT EXISTS idx_mail_dns_records_status
  ON app.mail_dns_records(status);

-- RLS
ALTER TABLE app.mail_dns_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.mail_dns_records FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS mail_dns_records_select ON app.mail_dns_records;
CREATE POLICY mail_dns_records_select ON app.mail_dns_records
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_dns_records_insert ON app.mail_dns_records;
CREATE POLICY mail_dns_records_insert ON app.mail_dns_records
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_dns_records_update ON app.mail_dns_records;
CREATE POLICY mail_dns_records_update ON app.mail_dns_records
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_dns_records_delete ON app.mail_dns_records;
CREATE POLICY mail_dns_records_delete ON app.mail_dns_records
  FOR DELETE TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_dns_records_service_role ON app.mail_dns_records;
CREATE POLICY mail_dns_records_service_role ON app.mail_dns_records
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON app.mail_dns_records TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.mail_dns_records TO service_role;

-- =====================================================================
-- TABLE 3: MAIL ACCOUNTS
-- Email accounts (mailboxes) per domain per office
-- =====================================================================

CREATE TABLE IF NOT EXISTS app.mail_accounts (
  account_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  domain_id        UUID NOT NULL REFERENCES app.mail_domains(domain_id) ON DELETE CASCADE,
  suite_id         UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  office_id        UUID NOT NULL REFERENCES app.offices(office_id) ON DELETE CASCADE,
  email_address    TEXT NOT NULL,
  display_name     TEXT,
  mailbox_provider TEXT NOT NULL DEFAULT 'polaris'
                   CHECK (mailbox_provider IN ('polaris', 'gmail', 'outlook', 'imap')),
  status           TEXT NOT NULL DEFAULT 'provisioning'
                   CHECK (status IN ('provisioning', 'active', 'suspended', 'deprovisioning', 'deleted')),
  quota_mb         INTEGER NOT NULL DEFAULT 5120,    -- 5GB default
  provider_ref     TEXT,                              -- External mailbox provider ID
  last_sync_at     TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(suite_id, email_address)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_mail_accounts_domain_id
  ON app.mail_accounts(domain_id);
CREATE INDEX IF NOT EXISTS idx_mail_accounts_suite_id
  ON app.mail_accounts(suite_id);
CREATE INDEX IF NOT EXISTS idx_mail_accounts_suite_office
  ON app.mail_accounts(suite_id, office_id);
CREATE INDEX IF NOT EXISTS idx_mail_accounts_email
  ON app.mail_accounts(email_address);
CREATE INDEX IF NOT EXISTS idx_mail_accounts_status
  ON app.mail_accounts(status);

-- RLS
ALTER TABLE app.mail_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.mail_accounts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS mail_accounts_select ON app.mail_accounts;
CREATE POLICY mail_accounts_select ON app.mail_accounts
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_accounts_insert ON app.mail_accounts;
CREATE POLICY mail_accounts_insert ON app.mail_accounts
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_accounts_update ON app.mail_accounts;
CREATE POLICY mail_accounts_update ON app.mail_accounts
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_accounts_delete ON app.mail_accounts;
CREATE POLICY mail_accounts_delete ON app.mail_accounts
  FOR DELETE TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS mail_accounts_service_role ON app.mail_accounts;
CREATE POLICY mail_accounts_service_role ON app.mail_accounts
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON app.mail_accounts TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.mail_accounts TO service_role;

-- =====================================================================
-- UPDATED_AT TRIGGERS
-- Auto-update updated_at on row modification
-- =====================================================================

DO $$
DECLARE
  tbl TEXT;
BEGIN
  FOR tbl IN SELECT unnest(ARRAY[
    'app.mail_domains', 'app.mail_dns_records', 'app.mail_accounts'
  ])
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS set_updated_at ON %s; CREATE TRIGGER set_updated_at BEFORE UPDATE ON %s FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();',
      tbl, tbl
    );
  END LOOP;
END;
$$;

-- =====================================================================
-- COMPLETION
-- =====================================================================

COMMIT;

-- Migration complete: 3 mail tables in app schema
-- Dual-path RLS: auth.uid() for PostgREST + current_setting for Express server
-- All tables: ENABLE + FORCE RLS, service_role bypass, full CRUD policies
-- Depends on: app.check_suite_access() from desktop_tables migration
-- Depends on: app.set_updated_at() from desktop_tables migration
