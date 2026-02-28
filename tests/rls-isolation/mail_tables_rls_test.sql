-- ============================================================================
-- Aspire Trust Spine: Mail Tables RLS Isolation Tests (Phase 0C)
-- Gate 1 Compliance: Tenant Isolation (Law #6)
-- ============================================================================
-- Run with: psql <pooler_connection_string> -f tests/rls-isolation/mail_tables_rls_test.sql
-- Expected: ALL tests PASS (0 failures)
-- ============================================================================
-- Tables under test:
--   app.mail_domains      (suite_id FK, dual-path RLS)
--   app.mail_dns_records  (suite_id FK, dual-path RLS)
--   app.mail_accounts     (suite_id FK + office_id FK, dual-path RLS)
--
-- RLS enforced via app.check_suite_access(suite_id):
--   Path A: PostgREST auth.uid() join
--   Path B: Express server current_setting('app.current_suite_id')
--
-- NOTE: postgres/service_role have BYPASSRLS privilege.
-- Real RLS enforcement is tested by SET ROLE authenticated.
-- ============================================================================

\set ON_ERROR_STOP on
\timing on

-- ============================================================================
-- SETUP: Create two test tenants + offices + seed mail data
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'MAIL RLS TESTS: SETUP — Creating test tenants and seed data'
\echo '================================================================='
\echo ''

BEGIN;

-- Test suites (same UUIDs as desktop RLS tests for consistency)
INSERT INTO app.suites (suite_id, name, tenant_id, created_at)
VALUES
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'Mail Test Suite A (Attacker)', 'mail-test-tenant-a', NOW()),
  ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'Mail Test Suite B (Victim)', 'mail-test-tenant-b', NOW())
ON CONFLICT (suite_id) DO NOTHING;

-- Test offices (two per suite to test intra-suite visibility)
INSERT INTO app.offices (office_id, suite_id, label, created_at)
VALUES
  ('aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'Mail Office A1', NOW()),
  ('aaaa2222-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'Mail Office A2', NOW()),
  ('bbbb1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'Mail Office B1', NOW()),
  ('bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'Mail Office B2', NOW())
ON CONFLICT (office_id) DO NOTHING;

-- Seed mail_domains
INSERT INTO app.mail_domains (domain_id, suite_id, office_id, domain_name, registrar, status, created_at)
VALUES
  ('da000001-0000-0000-0000-000000000001', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'suite-a.example.com', 'resellerclub', 'active', NOW()),
  ('da000001-0000-0000-0000-000000000002', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'aaaa2222-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'suite-a-office2.example.com', 'resellerclub', 'pending_verification', NOW()),
  ('db000001-0000-0000-0000-000000000001', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'bbbb1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'suite-b-secret.example.com', 'resellerclub', 'active', NOW())
ON CONFLICT (domain_id) DO NOTHING;

-- Seed mail_dns_records
INSERT INTO app.mail_dns_records (record_id, domain_id, suite_id, record_type, record_name, record_value, priority, status, created_at)
VALUES
  ('ec000001-0000-0000-0000-000000000001', 'da000001-0000-0000-0000-000000000001', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'MX', '@', 'mail.suite-a.example.com', 10, 'verified', NOW()),
  ('ec000001-0000-0000-0000-000000000002', 'da000001-0000-0000-0000-000000000001', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'SPF', '@', 'v=spf1 include:_spf.polaris.mail ~all', NULL, 'verified', NOW()),
  ('ec000001-0000-0000-0000-000000000003', 'da000001-0000-0000-0000-000000000001', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'DKIM', 'default._domainkey', 'v=DKIM1; k=rsa; p=MIGfMA0GCS...', NULL, 'pending', NOW()),
  ('ed000001-0000-0000-0000-000000000001', 'db000001-0000-0000-0000-000000000001', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'MX', '@', 'mail.suite-b-secret.example.com', 10, 'verified', NOW()),
  ('ed000001-0000-0000-0000-000000000002', 'db000001-0000-0000-0000-000000000001', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'DMARC', '_dmarc', 'v=DMARC1; p=reject; rua=mailto:dmarc@suite-b.com', NULL, 'verified', NOW())
ON CONFLICT (record_id) DO NOTHING;

-- Seed mail_accounts
INSERT INTO app.mail_accounts (account_id, domain_id, suite_id, office_id, email_address, display_name, mailbox_provider, status, created_at)
VALUES
  ('fa000001-0000-0000-0000-000000000001', 'da000001-0000-0000-0000-000000000001', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'admin@suite-a.example.com', 'Suite A Admin', 'polaris', 'active', NOW()),
  ('fa000001-0000-0000-0000-000000000002', 'da000001-0000-0000-0000-000000000001', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'aaaa2222-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'sales@suite-a.example.com', 'Suite A Sales', 'polaris', 'active', NOW()),
  ('fb000001-0000-0000-0000-000000000001', 'db000001-0000-0000-0000-000000000001', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'bbbb1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'secret@suite-b-secret.example.com', 'Secret B Admin', 'polaris', 'active', NOW())
ON CONFLICT (account_id) DO NOTHING;

COMMIT;

\echo 'Setup complete: 2 suites, 4 offices, 3 domains, 5 DNS records, 3 mail accounts'

-- ============================================================================
-- TEST GROUP 1: MAIL_DOMAINS — Suite Isolation (Tenant A perspective)
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'TEST GROUP 1: MAIL_DOMAINS — Suite Isolation (Tenant A)'
\echo '(SET ROLE authenticated + app.current_suite_id)'
\echo '================================================================='
\echo ''

SET ROLE authenticated;
SELECT set_config('app.current_suite_id', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', false);

-- Test 1.1: Suite A can SELECT own domains
\echo 'TEST 1.1: Suite A can see own mail_domains'
DO $$
DECLARE
  own_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO own_count FROM app.mail_domains
    WHERE suite_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  IF own_count != 2 THEN
    RAISE EXCEPTION 'FAIL [1.1]: Suite A expected 2 own domains, got %', own_count;
  END IF;
  RAISE NOTICE 'PASS [1.1]: Suite A sees 2 own domains';
END $$;

-- Test 1.2: Suite A CANNOT SELECT Suite B domains
\echo 'TEST 1.2: Suite A cannot see Suite B mail_domains'
DO $$
DECLARE
  victim_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO victim_count FROM app.mail_domains
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  IF victim_count > 0 THEN
    RAISE EXCEPTION 'FAIL [1.2]: Suite A sees % Suite B domains (LEAKAGE!)', victim_count;
  END IF;
  RAISE NOTICE 'PASS [1.2]: Suite A sees 0 Suite B domains (isolation OK)';
END $$;

-- Test 1.3: SELECT * returns ONLY Suite A domains
\echo 'TEST 1.3: SELECT * from mail_domains returns only own data'
DO $$
DECLARE
  total_count INTEGER;
  own_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO total_count FROM app.mail_domains;
  SELECT COUNT(*) INTO own_count FROM app.mail_domains
    WHERE suite_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  IF total_count != own_count THEN
    RAISE EXCEPTION 'FAIL [1.3]: SELECT * returns % but only % are own (LEAKAGE)', total_count, own_count;
  END IF;
  RAISE NOTICE 'PASS [1.3]: SELECT * returns only own data (% rows)', total_count;
END $$;

-- Test 1.4: Suite A CANNOT UPDATE Suite B domain (invisible via RLS)
\echo 'TEST 1.4: Suite A cannot UPDATE Suite B mail_domains'
DO $$
DECLARE
  rows_affected INTEGER;
BEGIN
  UPDATE app.mail_domains SET status = 'PWNED'
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected > 0 THEN
    RAISE EXCEPTION 'FAIL [1.4]: Updated % Suite B domains!', rows_affected;
  END IF;
  RAISE NOTICE 'PASS [1.4]: UPDATE Suite B domains affected 0 rows (invisible via RLS)';
END $$;

-- Test 1.5: Suite A CANNOT DELETE Suite B domain (invisible via RLS)
\echo 'TEST 1.5: Suite A cannot DELETE Suite B mail_domains'
DO $$
DECLARE
  rows_affected INTEGER;
BEGIN
  DELETE FROM app.mail_domains
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected > 0 THEN
    RAISE EXCEPTION 'FAIL [1.5]: Deleted % Suite B domains!', rows_affected;
  END IF;
  RAISE NOTICE 'PASS [1.5]: DELETE Suite B domains affected 0 rows (invisible via RLS)';
END $$;

-- ============================================================================
-- TEST GROUP 2: MAIL_DNS_RECORDS — Suite Isolation
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'TEST GROUP 2: MAIL_DNS_RECORDS — Suite Isolation'
\echo '================================================================='
\echo ''

-- Test 2.1: Suite A can SELECT own DNS records
\echo 'TEST 2.1: Suite A can see own mail_dns_records'
DO $$
DECLARE
  own_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO own_count FROM app.mail_dns_records
    WHERE suite_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  IF own_count != 3 THEN
    RAISE EXCEPTION 'FAIL [2.1]: Suite A expected 3 own DNS records, got %', own_count;
  END IF;
  RAISE NOTICE 'PASS [2.1]: Suite A sees 3 own DNS records';
END $$;

-- Test 2.2: Suite A CANNOT SELECT Suite B DNS records
\echo 'TEST 2.2: Suite A cannot see Suite B mail_dns_records'
DO $$
DECLARE
  victim_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO victim_count FROM app.mail_dns_records
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  IF victim_count > 0 THEN
    RAISE EXCEPTION 'FAIL [2.2]: Suite A sees % Suite B DNS records (LEAKAGE!)', victim_count;
  END IF;
  RAISE NOTICE 'PASS [2.2]: Suite A sees 0 Suite B DNS records (isolation OK)';
END $$;

-- Test 2.3: SELECT * returns ONLY Suite A DNS records
\echo 'TEST 2.3: SELECT * from mail_dns_records returns only own data'
DO $$
DECLARE
  total_count INTEGER;
  own_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO total_count FROM app.mail_dns_records;
  SELECT COUNT(*) INTO own_count FROM app.mail_dns_records
    WHERE suite_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  IF total_count != own_count THEN
    RAISE EXCEPTION 'FAIL [2.3]: SELECT * returns % but only % are own (LEAKAGE)', total_count, own_count;
  END IF;
  RAISE NOTICE 'PASS [2.3]: SELECT * returns only own data (% rows)', total_count;
END $$;

-- Test 2.4: Suite A CANNOT UPDATE Suite B DNS records
\echo 'TEST 2.4: Suite A cannot UPDATE Suite B mail_dns_records'
DO $$
DECLARE
  rows_affected INTEGER;
BEGIN
  UPDATE app.mail_dns_records SET record_value = 'PWNED'
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected > 0 THEN
    RAISE EXCEPTION 'FAIL [2.4]: Updated % Suite B DNS records!', rows_affected;
  END IF;
  RAISE NOTICE 'PASS [2.4]: UPDATE Suite B DNS records affected 0 rows';
END $$;

-- Test 2.5: Suite A CANNOT DELETE Suite B DNS records
\echo 'TEST 2.5: Suite A cannot DELETE Suite B mail_dns_records'
DO $$
DECLARE
  rows_affected INTEGER;
BEGIN
  DELETE FROM app.mail_dns_records
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected > 0 THEN
    RAISE EXCEPTION 'FAIL [2.5]: Deleted % Suite B DNS records!', rows_affected;
  END IF;
  RAISE NOTICE 'PASS [2.5]: DELETE Suite B DNS records affected 0 rows';
END $$;

-- ============================================================================
-- TEST GROUP 3: MAIL_ACCOUNTS — Suite Isolation + Office Visibility
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'TEST GROUP 3: MAIL_ACCOUNTS — Suite Isolation + Office Visibility'
\echo '================================================================='
\echo ''

-- Test 3.1: Suite A can SELECT own accounts
\echo 'TEST 3.1: Suite A can see own mail_accounts'
DO $$
DECLARE
  own_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO own_count FROM app.mail_accounts
    WHERE suite_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  IF own_count != 2 THEN
    RAISE EXCEPTION 'FAIL [3.1]: Suite A expected 2 own accounts, got %', own_count;
  END IF;
  RAISE NOTICE 'PASS [3.1]: Suite A sees 2 own accounts';
END $$;

-- Test 3.2: Suite A CANNOT SELECT Suite B accounts
\echo 'TEST 3.2: Suite A cannot see Suite B mail_accounts'
DO $$
DECLARE
  victim_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO victim_count FROM app.mail_accounts
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  IF victim_count > 0 THEN
    RAISE EXCEPTION 'FAIL [3.2]: Suite A sees % Suite B accounts (LEAKAGE!)', victim_count;
  END IF;
  RAISE NOTICE 'PASS [3.2]: Suite A sees 0 Suite B accounts (isolation OK)';
END $$;

-- Test 3.3: Office A1 can see Office A2 accounts (suite-level isolation, not office-level)
\echo 'TEST 3.3: Office A1 can see Office A2 accounts (suite-level RLS)'
DO $$
DECLARE
  office_a2_count INTEGER;
BEGIN
  -- RLS is at suite level, so Office A1 context should still see Office A2 accounts
  SELECT COUNT(*) INTO office_a2_count FROM app.mail_accounts
    WHERE office_id = 'aaaa2222-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  IF office_a2_count != 1 THEN
    RAISE EXCEPTION 'FAIL [3.3]: Expected 1 Office A2 account visible, got %', office_a2_count;
  END IF;
  RAISE NOTICE 'PASS [3.3]: Office A2 account visible to Suite A context (suite-level isolation)';
END $$;

-- Test 3.4: SELECT * returns ONLY Suite A accounts
\echo 'TEST 3.4: SELECT * from mail_accounts returns only own data'
DO $$
DECLARE
  total_count INTEGER;
  own_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO total_count FROM app.mail_accounts;
  SELECT COUNT(*) INTO own_count FROM app.mail_accounts
    WHERE suite_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  IF total_count != own_count THEN
    RAISE EXCEPTION 'FAIL [3.4]: SELECT * returns % but only % are own (LEAKAGE)', total_count, own_count;
  END IF;
  RAISE NOTICE 'PASS [3.4]: SELECT * returns only own data (% rows)', total_count;
END $$;

-- Test 3.5: Suite A CANNOT UPDATE Suite B accounts
\echo 'TEST 3.5: Suite A cannot UPDATE Suite B mail_accounts'
DO $$
DECLARE
  rows_affected INTEGER;
BEGIN
  UPDATE app.mail_accounts SET display_name = 'PWNED'
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected > 0 THEN
    RAISE EXCEPTION 'FAIL [3.5]: Updated % Suite B accounts!', rows_affected;
  END IF;
  RAISE NOTICE 'PASS [3.5]: UPDATE Suite B accounts affected 0 rows';
END $$;

-- Test 3.6: Suite A CANNOT DELETE Suite B accounts
\echo 'TEST 3.6: Suite A cannot DELETE Suite B mail_accounts'
DO $$
DECLARE
  rows_affected INTEGER;
BEGIN
  DELETE FROM app.mail_accounts
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected > 0 THEN
    RAISE EXCEPTION 'FAIL [3.6]: Deleted % Suite B accounts!', rows_affected;
  END IF;
  RAISE NOTICE 'PASS [3.6]: DELETE Suite B accounts affected 0 rows';
END $$;

-- ============================================================================
-- TEST GROUP 4: REVERSE ISOLATION (Tenant B perspective)
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'TEST GROUP 4: REVERSE ISOLATION (Suite B perspective)'
\echo '================================================================='
\echo ''

SELECT set_config('app.current_suite_id', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', false);

-- Test 4.1: Suite B cannot see Suite A domains
\echo 'TEST 4.1: Suite B cannot see Suite A mail_domains'
DO $$
DECLARE
  attacker_count INTEGER;
  own_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO attacker_count FROM app.mail_domains
    WHERE suite_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  SELECT COUNT(*) INTO own_count FROM app.mail_domains
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  IF attacker_count > 0 THEN
    RAISE EXCEPTION 'FAIL [4.1]: Suite B sees % Suite A domains', attacker_count;
  END IF;
  IF own_count != 1 THEN
    RAISE EXCEPTION 'FAIL [4.1]: Suite B expected 1 own domain, got %', own_count;
  END IF;
  RAISE NOTICE 'PASS [4.1]: Reverse isolation OK (own=%, other=0)', own_count;
END $$;

-- Test 4.2: Suite B cannot see Suite A DNS records
\echo 'TEST 4.2: Suite B cannot see Suite A mail_dns_records'
DO $$
DECLARE
  attacker_count INTEGER;
  own_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO attacker_count FROM app.mail_dns_records
    WHERE suite_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  SELECT COUNT(*) INTO own_count FROM app.mail_dns_records
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  IF attacker_count > 0 THEN
    RAISE EXCEPTION 'FAIL [4.2]: Suite B sees % Suite A DNS records', attacker_count;
  END IF;
  IF own_count != 2 THEN
    RAISE EXCEPTION 'FAIL [4.2]: Suite B expected 2 own DNS records, got %', own_count;
  END IF;
  RAISE NOTICE 'PASS [4.2]: DNS reverse isolation OK (own=%, other=0)', own_count;
END $$;

-- Test 4.3: Suite B cannot see Suite A accounts
\echo 'TEST 4.3: Suite B cannot see Suite A mail_accounts'
DO $$
DECLARE
  attacker_count INTEGER;
  own_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO attacker_count FROM app.mail_accounts
    WHERE suite_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  SELECT COUNT(*) INTO own_count FROM app.mail_accounts
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  IF attacker_count > 0 THEN
    RAISE EXCEPTION 'FAIL [4.3]: Suite B sees % Suite A accounts', attacker_count;
  END IF;
  IF own_count != 1 THEN
    RAISE EXCEPTION 'FAIL [4.3]: Suite B expected 1 own account, got %', own_count;
  END IF;
  RAISE NOTICE 'PASS [4.3]: Accounts reverse isolation OK (own=%, other=0)', own_count;
END $$;

-- ============================================================================
-- TEST GROUP 5: SERVICE ROLE BYPASS (Admin operations)
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'TEST GROUP 5: SERVICE ROLE BYPASS (Admin operations)'
\echo '================================================================='
\echo ''

RESET ROLE;

-- Test 5.1: service_role can see ALL domains across suites
\echo 'TEST 5.1: service_role sees all mail_domains'
DO $$
DECLARE
  total_count INTEGER;
  suite_a INTEGER;
  suite_b INTEGER;
BEGIN
  -- As postgres (BYPASSRLS), verify total data
  SELECT COUNT(*) INTO total_count FROM app.mail_domains;
  SELECT COUNT(*) INTO suite_a FROM app.mail_domains WHERE suite_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  SELECT COUNT(*) INTO suite_b FROM app.mail_domains WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  IF total_count < 3 THEN
    RAISE EXCEPTION 'FAIL [5.1]: service_role sees only % domains (expected 3)', total_count;
  END IF;
  IF suite_a < 2 OR suite_b < 1 THEN
    RAISE EXCEPTION 'FAIL [5.1]: service_role cross-suite incomplete (A=%, B=%)', suite_a, suite_b;
  END IF;
  RAISE NOTICE 'PASS [5.1]: service_role sees all % domains (A=%, B=%)', total_count, suite_a, suite_b;
END $$;

-- Test 5.2: service_role can see ALL DNS records across suites
\echo 'TEST 5.2: service_role sees all mail_dns_records'
DO $$
DECLARE
  total_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO total_count FROM app.mail_dns_records;
  IF total_count < 5 THEN
    RAISE EXCEPTION 'FAIL [5.2]: service_role sees only % DNS records (expected 5)', total_count;
  END IF;
  RAISE NOTICE 'PASS [5.2]: service_role sees all % DNS records', total_count;
END $$;

-- Test 5.3: service_role can see ALL accounts across suites
\echo 'TEST 5.3: service_role sees all mail_accounts'
DO $$
DECLARE
  total_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO total_count FROM app.mail_accounts;
  IF total_count < 3 THEN
    RAISE EXCEPTION 'FAIL [5.3]: service_role sees only % accounts (expected 3)', total_count;
  END IF;
  RAISE NOTICE 'PASS [5.3]: service_role sees all % accounts', total_count;
END $$;

-- ============================================================================
-- TEST GROUP 6: STRUCTURAL VERIFICATION
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'TEST GROUP 6: STRUCTURAL VERIFICATION'
\echo '================================================================='
\echo ''

-- Test 6.1: All 3 mail tables have RLS ENABLED
\echo 'TEST 6.1: All mail tables have RLS ENABLED'
DO $$
DECLARE
  rls_count INTEGER;
  missing TEXT;
BEGIN
  SELECT COUNT(*) INTO rls_count
  FROM pg_class c
  JOIN pg_namespace n ON c.relnamespace = n.oid
  WHERE n.nspname = 'app'
    AND c.relkind = 'r'
    AND c.relrowsecurity = true
    AND c.relname IN ('mail_domains', 'mail_dns_records', 'mail_accounts');

  IF rls_count != 3 THEN
    SELECT string_agg(c.relname, ', ') INTO missing
    FROM pg_class c
    JOIN pg_namespace n ON c.relnamespace = n.oid
    WHERE n.nspname = 'app'
      AND c.relkind = 'r'
      AND c.relrowsecurity = false
      AND c.relname IN ('mail_domains', 'mail_dns_records', 'mail_accounts');
    RAISE EXCEPTION 'FAIL [6.1]: Only %/3 mail tables have RLS enabled. Missing: %', rls_count, missing;
  END IF;
  RAISE NOTICE 'PASS [6.1]: All 3 mail tables have RLS ENABLED';
END $$;

-- Test 6.2: All 3 mail tables have FORCE RLS
\echo 'TEST 6.2: All mail tables have FORCE RLS'
DO $$
DECLARE
  force_count INTEGER;
  missing TEXT;
BEGIN
  SELECT COUNT(*) INTO force_count
  FROM pg_class c
  JOIN pg_namespace n ON c.relnamespace = n.oid
  WHERE n.nspname = 'app'
    AND c.relkind = 'r'
    AND c.relforcerowsecurity = true
    AND c.relname IN ('mail_domains', 'mail_dns_records', 'mail_accounts');

  IF force_count != 3 THEN
    SELECT string_agg(c.relname, ', ') INTO missing
    FROM pg_class c
    JOIN pg_namespace n ON c.relnamespace = n.oid
    WHERE n.nspname = 'app'
      AND c.relkind = 'r'
      AND c.relforcerowsecurity = false
      AND c.relname IN ('mail_domains', 'mail_dns_records', 'mail_accounts');
    RAISE EXCEPTION 'FAIL [6.2]: Only %/3 mail tables have FORCE RLS. Missing: %', force_count, missing;
  END IF;
  RAISE NOTICE 'PASS [6.2]: All 3 mail tables have FORCE RLS';
END $$;

-- Test 6.3: All 3 mail tables have SELECT/INSERT/UPDATE/DELETE + service_role policies
\echo 'TEST 6.3: All mail tables have complete RLS policy set'
DO $$
DECLARE
  tbl TEXT;
  policy_count INTEGER;
  policies TEXT;
BEGIN
  FOR tbl IN SELECT unnest(ARRAY['mail_domains', 'mail_dns_records', 'mail_accounts'])
  LOOP
    SELECT COUNT(*), string_agg(pol.polname, ', ')
    INTO policy_count, policies
    FROM pg_policy pol
    JOIN pg_class c ON pol.polrelid = c.oid
    JOIN pg_namespace n ON c.relnamespace = n.oid
    WHERE n.nspname = 'app' AND c.relname = tbl;

    -- Expect 5 policies: select, insert, update, delete, service_role
    IF policy_count < 5 THEN
      RAISE EXCEPTION 'FAIL [6.3]: % has only %/5 policies: %', tbl, policy_count, policies;
    END IF;
    RAISE NOTICE 'PASS [6.3]: % has % policies: %', tbl, policy_count, policies;
  END LOOP;
END $$;

-- Test 6.4: suite_id is UUID type on all 3 mail tables
\echo 'TEST 6.4: suite_id is UUID type on all mail tables'
DO $$
DECLARE
  tbl TEXT;
  col_type TEXT;
BEGIN
  FOR tbl IN SELECT unnest(ARRAY['mail_domains', 'mail_dns_records', 'mail_accounts'])
  LOOP
    SELECT data_type INTO col_type
    FROM information_schema.columns
    WHERE table_schema = 'app' AND table_name = tbl AND column_name = 'suite_id';

    IF col_type IS NULL THEN
      RAISE EXCEPTION 'FAIL [6.4]: % missing suite_id column', tbl;
    END IF;
    IF col_type != 'uuid' THEN
      RAISE EXCEPTION 'FAIL [6.4]: %.suite_id is % (expected uuid)', tbl, col_type;
    END IF;
    RAISE NOTICE 'PASS [6.4]: %.suite_id is uuid', tbl;
  END LOOP;
END $$;

-- Test 6.5: Foreign keys to app.suites(suite_id) exist
\echo 'TEST 6.5: Foreign keys to app.suites(suite_id) exist'
DO $$
DECLARE
  tbl TEXT;
  fk_count INTEGER;
BEGIN
  FOR tbl IN SELECT unnest(ARRAY['mail_domains', 'mail_dns_records', 'mail_accounts'])
  LOOP
    SELECT COUNT(*) INTO fk_count
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
      AND tc.table_schema = kcu.table_schema
    JOIN information_schema.constraint_column_usage ccu
      ON tc.constraint_name = ccu.constraint_name
      AND tc.table_schema = ccu.table_schema
    WHERE tc.table_schema = 'app'
      AND tc.table_name = tbl
      AND tc.constraint_type = 'FOREIGN KEY'
      AND kcu.column_name = 'suite_id'
      AND ccu.table_name = 'suites';

    IF fk_count = 0 THEN
      RAISE EXCEPTION 'FAIL [6.5]: % missing FK to app.suites(suite_id)', tbl;
    END IF;
    RAISE NOTICE 'PASS [6.5]: %.suite_id has FK to app.suites', tbl;
  END LOOP;
END $$;

-- Test 6.6: mail_dns_records has FK to mail_domains(domain_id)
\echo 'TEST 6.6: mail_dns_records has FK to mail_domains(domain_id)'
DO $$
DECLARE
  fk_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO fk_count
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
    AND tc.table_schema = kcu.table_schema
  JOIN information_schema.constraint_column_usage ccu
    ON tc.constraint_name = ccu.constraint_name
    AND tc.table_schema = ccu.table_schema
  WHERE tc.table_schema = 'app'
    AND tc.table_name = 'mail_dns_records'
    AND tc.constraint_type = 'FOREIGN KEY'
    AND kcu.column_name = 'domain_id'
    AND ccu.table_name = 'mail_domains';

  IF fk_count = 0 THEN
    RAISE EXCEPTION 'FAIL [6.6]: mail_dns_records missing FK to mail_domains(domain_id)';
  END IF;
  RAISE NOTICE 'PASS [6.6]: mail_dns_records.domain_id has FK to mail_domains';
END $$;

-- Test 6.7: mail_accounts has FK to mail_domains(domain_id)
\echo 'TEST 6.7: mail_accounts has FK to mail_domains(domain_id)'
DO $$
DECLARE
  fk_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO fk_count
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
    AND tc.table_schema = kcu.table_schema
  JOIN information_schema.constraint_column_usage ccu
    ON tc.constraint_name = ccu.constraint_name
    AND tc.table_schema = ccu.table_schema
  WHERE tc.table_schema = 'app'
    AND tc.table_name = 'mail_accounts'
    AND tc.constraint_type = 'FOREIGN KEY'
    AND kcu.column_name = 'domain_id'
    AND ccu.table_name = 'mail_domains';

  IF fk_count = 0 THEN
    RAISE EXCEPTION 'FAIL [6.7]: mail_accounts missing FK to mail_domains(domain_id)';
  END IF;
  RAISE NOTICE 'PASS [6.7]: mail_accounts.domain_id has FK to mail_domains';
END $$;

-- ============================================================================
-- CLEANUP
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'CLEANUP: Removing mail test data'
\echo '================================================================='
\echo ''

RESET ROLE;

-- Delete in dependency order (child tables first)
DELETE FROM app.mail_accounts WHERE suite_id IN ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb');
DELETE FROM app.mail_dns_records WHERE suite_id IN ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb');
DELETE FROM app.mail_domains WHERE suite_id IN ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb');
DELETE FROM app.offices WHERE office_id IN (
  'aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'aaaa2222-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  'bbbb1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
);
DELETE FROM app.suites WHERE suite_id IN ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb');

\echo ''
\echo '================================================================='
\echo 'ALL MAIL RLS TESTS COMPLETE'
\echo '================================================================='
\echo ''
\echo 'Summary:'
\echo '  Group 1: mail_domains suite isolation (5 tests)'
\echo '  Group 2: mail_dns_records suite isolation (5 tests)'
\echo '  Group 3: mail_accounts suite + office isolation (6 tests)'
\echo '  Group 4: Reverse isolation from Suite B (3 tests)'
\echo '  Group 5: Service role bypass (3 tests)'
\echo '  Group 6: Structural verification (7 tests)'
\echo '  Total: 29 tests'
\echo ''
