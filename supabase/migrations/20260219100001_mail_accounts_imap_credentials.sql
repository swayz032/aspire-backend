-- =====================================================================
-- Mail Accounts: Add encrypted IMAP credentials for PolarisM mailboxes
-- =====================================================================
-- Purpose: Store encrypted mailbox password for IMAP/SMTP access
-- The password is encrypted with AES-256-GCM using TOKEN_ENCRYPTION_KEY
-- at the application layer before INSERT.
--
-- Governance Compliance:
--   Law #2: Password storage produces a receipt (app layer)
--   Law #3: Fail closed — if TOKEN_ENCRYPTION_KEY not set, encrypt fails
--   Law #6: RLS already enforced on mail_accounts (migration 20260213)
--   Law #9: Password encrypted at rest, never logged in plaintext
--
-- Also adds imap_host/smtp_host columns for per-account server config.
-- =====================================================================

BEGIN;

-- Add encrypted password column (stores AES-256-GCM: iv:authTag:ciphertext)
ALTER TABLE app.mail_accounts
  ADD COLUMN IF NOT EXISTS encrypted_password TEXT;

-- Add IMAP/SMTP server config (defaults to PolarisM shared servers)
ALTER TABLE app.mail_accounts
  ADD COLUMN IF NOT EXISTS imap_host TEXT DEFAULT 'mail.emailarray.com',
  ADD COLUMN IF NOT EXISTS imap_port INTEGER DEFAULT 993,
  ADD COLUMN IF NOT EXISTS smtp_host TEXT DEFAULT 'mail.emailarray.com',
  ADD COLUMN IF NOT EXISTS smtp_port INTEGER DEFAULT 465;

COMMIT;
