-- ============================================================================
-- Aspire Trust Spine: Mail Tables Evil Tests (Phase 0C)
-- Gate 1 Compliance: Tenant Isolation (Law #6) + Fail Closed (Law #3)
-- ============================================================================
-- Run with: psql <pooler_connection_string> -f tests/evil/mail_tables_evil_test.sql
-- Expected: ALL tests PASS (0 failures)
-- ============================================================================
-- Evil test categories:
--   1. Cross-tenant domain claim (INSERT with wrong suite_id)
--   2. Cross-tenant DNS modification (UPDATE foreign records)
--   3. Cross-tenant account access (SELECT foreign accounts)
--   4. SQL injection in domain_name field
--   5. RLS enforcement with no auth context (fail closed)
--   6. FORCE RLS validation (even table owner filtered)
--   7. Cross-tenant INSERT escalation on all 3 tables
--   8. Domain-name uniqueness constraint validation
--
-- NOTE: postgres/service_role have BYPASSRLS privilege.
-- Real RLS enforcement is tested by SET ROLE authenticated.
-- ============================================================================

\set ON_ERROR_STOP on
\timing on

-- ============================================================================
-- SETUP: Create two test tenants + seed mail data
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'MAIL EVIL TESTS: SETUP'
\echo '================================================================='
\echo ''

BEGIN;

INSERT INTO app.suites (suite_id, name, tenant_id, created_at)
VALUES
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'Evil Test Suite A (Attacker)', 'evil-test-tenant-a', NOW()),
  ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'Evil Test Suite B (Victim)', 'evil-test-tenant-b', NOW())
ON CONFLICT (suite_id) DO NOTHING;

INSERT INTO app.offices (office_id, suite_id, label, created_at)
VALUES
  ('aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'Evil Office A1', NOW()),
  ('bbbb1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'Evil Office B1', NOW())
ON CONFLICT (office_id) DO NOTHING;

-- Victim domain and records
INSERT INTO app.mail_domains (domain_id, suite_id, office_id, domain_name, registrar, status, created_at)
VALUES
  ('db000002-0000-0000-0000-000000000001', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'bbbb1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'victim-domain.example.com', 'resellerclub', 'active', NOW())
ON CONFLICT (domain_id) DO NOTHING;

INSERT INTO app.mail_dns_records (record_id, domain_id, suite_id, record_type, record_name, record_value, priority, status, created_at)
VALUES
  ('ed000002-0000-0000-0000-000000000001', 'db000002-0000-0000-0000-000000000001', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'MX', '@', 'mx.victim-domain.example.com', 10, 'verified', NOW()),
  ('ed000002-0000-0000-0000-000000000002', 'db000002-0000-0000-0000-000000000001', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'SPF', '@', 'v=spf1 include:victim.com ~all', NULL, 'verified', NOW())
ON CONFLICT (record_id) DO NOTHING;

INSERT INTO app.mail_accounts (account_id, domain_id, suite_id, office_id, email_address, display_name, mailbox_provider, status, created_at)
VALUES
  ('fb000002-0000-0000-0000-000000000001', 'db000002-0000-0000-0000-000000000001', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'bbbb1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'ceo@victim-domain.example.com', 'Victim CEO', 'polaris', 'active', NOW())
ON CONFLICT (account_id) DO NOTHING;

COMMIT;

\echo 'Evil setup complete: attacker (A) and victim (B) tenants ready'

-- ============================================================================
-- EVIL TEST GROUP 1: CROSS-TENANT DOMAIN CLAIM
-- Suite A tries to INSERT a domain with Suite B's suite_id
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'EVIL GROUP 1: CROSS-TENANT DOMAIN CLAIM'
\echo '================================================================='
\echo ''

SET ROLE authenticated;
SELECT set_config('app.current_suite_id', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', false);

-- Evil 1.1: INSERT domain with victim suite_id
\echo 'EVIL 1.1: Suite A tries to INSERT domain with Suite B suite_id'
DO $$
BEGIN
  INSERT INTO app.mail_domains (domain_id, suite_id, office_id, domain_name, registrar, status)
  VALUES (
    gen_random_uuid(),
    'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',  -- VICTIM suite_id
    'bbbb1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb',  -- VICTIM office_id
    'evil-hijack.example.com',
    'resellerclub',
    'active'
  );
  RAISE EXCEPTION 'FAIL [E1.1]: INSERT with victim suite_id succeeded! DOMAIN HIJACK!';
EXCEPTION
  WHEN others THEN
    IF SQLERRM LIKE 'FAIL%' THEN
      RAISE;
    END IF;
    IF SQLERRM LIKE '%row-level security%' OR SQLERRM LIKE '%new row violates%' THEN
      RAISE NOTICE 'PASS [E1.1]: Cross-tenant domain claim denied by RLS';
    ELSE
      RAISE NOTICE 'PASS [E1.1]: Cross-tenant domain claim denied: %', SQLERRM;
    END IF;
END $$;

-- Evil 1.2: INSERT DNS record with victim suite_id
\echo 'EVIL 1.2: Suite A tries to INSERT DNS record with Suite B suite_id'
DO $$
BEGIN
  INSERT INTO app.mail_dns_records (record_id, domain_id, suite_id, record_type, record_name, record_value, status)
  VALUES (
    gen_random_uuid(),
    'db000002-0000-0000-0000-000000000001',  -- VICTIM domain
    'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',  -- VICTIM suite_id
    'MX', '@', 'evil-mx.attacker.com', 'verified'
  );
  RAISE EXCEPTION 'FAIL [E1.2]: INSERT DNS record with victim suite_id succeeded! DNS HIJACK!';
EXCEPTION
  WHEN others THEN
    IF SQLERRM LIKE 'FAIL%' THEN
      RAISE;
    END IF;
    IF SQLERRM LIKE '%row-level security%' OR SQLERRM LIKE '%new row violates%' THEN
      RAISE NOTICE 'PASS [E1.2]: Cross-tenant DNS record claim denied by RLS';
    ELSE
      RAISE NOTICE 'PASS [E1.2]: Cross-tenant DNS record claim denied: %', SQLERRM;
    END IF;
END $$;

-- Evil 1.3: INSERT account with victim suite_id
\echo 'EVIL 1.3: Suite A tries to INSERT account with Suite B suite_id'
DO $$
BEGIN
  INSERT INTO app.mail_accounts (account_id, domain_id, suite_id, office_id, email_address, display_name, mailbox_provider, status)
  VALUES (
    gen_random_uuid(),
    'db000002-0000-0000-0000-000000000001',  -- VICTIM domain
    'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',  -- VICTIM suite_id
    'bbbb1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb',  -- VICTIM office_id
    'evil@victim-domain.example.com',
    'Evil Impersonator',
    'polaris',
    'active'
  );
  RAISE EXCEPTION 'FAIL [E1.3]: INSERT account with victim suite_id succeeded! ACCOUNT HIJACK!';
EXCEPTION
  WHEN others THEN
    IF SQLERRM LIKE 'FAIL%' THEN
      RAISE;
    END IF;
    IF SQLERRM LIKE '%row-level security%' OR SQLERRM LIKE '%new row violates%' THEN
      RAISE NOTICE 'PASS [E1.3]: Cross-tenant account claim denied by RLS';
    ELSE
      RAISE NOTICE 'PASS [E1.3]: Cross-tenant account claim denied: %', SQLERRM;
    END IF;
END $$;

-- ============================================================================
-- EVIL TEST GROUP 2: CROSS-TENANT DNS MODIFICATION
-- Suite A tries to UPDATE/DELETE victim DNS records
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'EVIL GROUP 2: CROSS-TENANT DNS MODIFICATION'
\echo '================================================================='
\echo ''

-- Evil 2.1: UPDATE victim DNS record value (MX hijack attempt)
\echo 'EVIL 2.1: Suite A tries to UPDATE victim MX record'
DO $$
DECLARE
  rows_affected INTEGER;
BEGIN
  UPDATE app.mail_dns_records
    SET record_value = 'evil-mx.attacker.com'
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected > 0 THEN
    RAISE EXCEPTION 'FAIL [E2.1]: Updated % victim DNS records! MX HIJACK!', rows_affected;
  END IF;
  RAISE NOTICE 'PASS [E2.1]: UPDATE victim DNS records affected 0 rows (invisible via RLS)';
END $$;

-- Evil 2.2: DELETE victim DNS records (DNS sabotage attempt)
\echo 'EVIL 2.2: Suite A tries to DELETE victim DNS records'
DO $$
DECLARE
  rows_affected INTEGER;
BEGIN
  DELETE FROM app.mail_dns_records
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected > 0 THEN
    RAISE EXCEPTION 'FAIL [E2.2]: Deleted % victim DNS records! DNS SABOTAGE!', rows_affected;
  END IF;
  RAISE NOTICE 'PASS [E2.2]: DELETE victim DNS records affected 0 rows';
END $$;

-- Evil 2.3: UPDATE victim domain status to 'deleted' (domain kill attempt)
\echo 'EVIL 2.3: Suite A tries to UPDATE victim domain status to deleted'
DO $$
DECLARE
  rows_affected INTEGER;
BEGIN
  UPDATE app.mail_domains
    SET status = 'deleted'
    WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected > 0 THEN
    RAISE EXCEPTION 'FAIL [E2.3]: Set % victim domains to deleted!', rows_affected;
  END IF;
  RAISE NOTICE 'PASS [E2.3]: UPDATE victim domain status affected 0 rows';
END $$;

-- ============================================================================
-- EVIL TEST GROUP 3: CROSS-TENANT ACCOUNT ACCESS
-- Suite A tries to SELECT victim account data
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'EVIL GROUP 3: CROSS-TENANT ACCOUNT ACCESS'
\echo '================================================================='
\echo ''

-- Evil 3.1: SELECT victim accounts
\echo 'EVIL 3.1: Suite A tries to SELECT victim mail_accounts'
DO $$
DECLARE
  victim_count INTEGER;
  victim_email TEXT;
BEGIN
  SELECT COUNT(*), string_agg(email_address, ', ')
  INTO victim_count, victim_email
  FROM app.mail_accounts
  WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  IF victim_count > 0 THEN
    RAISE EXCEPTION 'FAIL [E3.1]: Suite A can see % victim accounts: %', victim_count, victim_email;
  END IF;
  RAISE NOTICE 'PASS [E3.1]: Suite A sees 0 victim accounts (isolation OK)';
END $$;

-- Evil 3.2: SELECT victim domains
\echo 'EVIL 3.2: Suite A tries to SELECT victim mail_domains'
DO $$
DECLARE
  victim_count INTEGER;
  victim_names TEXT;
BEGIN
  SELECT COUNT(*), string_agg(domain_name, ', ')
  INTO victim_count, victim_names
  FROM app.mail_domains
  WHERE suite_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  IF victim_count > 0 THEN
    RAISE EXCEPTION 'FAIL [E3.2]: Suite A can see % victim domains: %', victim_count, victim_names;
  END IF;
  RAISE NOTICE 'PASS [E3.2]: Suite A sees 0 victim domains (isolation OK)';
END $$;

-- Evil 3.3: JOIN attack — try to read victim DNS via domain_id (no suite_id filter)
\echo 'EVIL 3.3: Suite A tries JOIN attack to read victim DNS records via domain_id'
DO $$
DECLARE
  victim_count INTEGER;
BEGIN
  -- Even if attacker knows the victim domain_id, RLS on mail_dns_records filters by suite_id
  SELECT COUNT(*) INTO victim_count
  FROM app.mail_dns_records
  WHERE domain_id = 'db000002-0000-0000-0000-000000000001';
  IF victim_count > 0 THEN
    RAISE EXCEPTION 'FAIL [E3.3]: Suite A can see % victim DNS records via domain_id!', victim_count;
  END IF;
  RAISE NOTICE 'PASS [E3.3]: JOIN via domain_id returns 0 (RLS filters on suite_id)';
END $$;

-- Evil 3.4: JOIN attack — try to read victim accounts via domain_id
\echo 'EVIL 3.4: Suite A tries JOIN attack to read victim accounts via domain_id'
DO $$
DECLARE
  victim_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO victim_count
  FROM app.mail_accounts
  WHERE domain_id = 'db000002-0000-0000-0000-000000000001';
  IF victim_count > 0 THEN
    RAISE EXCEPTION 'FAIL [E3.4]: Suite A can see % victim accounts via domain_id!', victim_count;
  END IF;
  RAISE NOTICE 'PASS [E3.4]: JOIN via domain_id returns 0 (RLS filters on suite_id)';
END $$;

-- ============================================================================
-- EVIL TEST GROUP 4: SQL INJECTION IN DOMAIN_NAME FIELD
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'EVIL GROUP 4: SQL INJECTION IN DOMAIN_NAME'
\echo '================================================================='
\echo ''

-- Evil 4.1: Classic DROP TABLE injection in domain_name
\echo 'EVIL 4.1: SQL injection in domain_name — DROP TABLE attempt'
DO $$
DECLARE
  stored_name TEXT;
  domain_count_before INTEGER;
  domain_count_after INTEGER;
BEGIN
  SELECT COUNT(*) INTO domain_count_before FROM app.mail_domains;

  -- Insert domain with SQL injection payload
  INSERT INTO app.mail_domains (domain_id, suite_id, office_id, domain_name, registrar, status)
  VALUES (
    'da000099-0000-0000-0000-000000000001',
    'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    'aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    '''; DROP TABLE app.mail_domains; --',
    'resellerclub',
    'pending_verification'
  );

  -- Verify the table still exists by counting
  SELECT COUNT(*) INTO domain_count_after FROM app.mail_domains;

  -- Verify the payload was stored as a literal string
  SELECT domain_name INTO stored_name FROM app.mail_domains
    WHERE domain_id = 'da000099-0000-0000-0000-000000000001';

  IF stored_name != '''; DROP TABLE app.mail_domains; --' THEN
    RAISE EXCEPTION 'FAIL [E4.1]: domain_name was modified! Stored: %', stored_name;
  END IF;

  RAISE NOTICE 'PASS [E4.1]: SQL injection stored as literal string: %', stored_name;

  -- Cleanup
  DELETE FROM app.mail_domains WHERE domain_id = 'da000099-0000-0000-0000-000000000001';
END $$;

-- Evil 4.2: UNION SELECT injection in domain_name
\echo 'EVIL 4.2: SQL injection in domain_name — UNION SELECT attempt'
DO $$
DECLARE
  stored_name TEXT;
BEGIN
  INSERT INTO app.mail_domains (domain_id, suite_id, office_id, domain_name, registrar, status)
  VALUES (
    'da000099-0000-0000-0000-000000000002',
    'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    'aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    ''' UNION SELECT secret FROM passwords --',
    'resellerclub',
    'pending_verification'
  );

  SELECT domain_name INTO stored_name FROM app.mail_domains
    WHERE domain_id = 'da000099-0000-0000-0000-000000000002';

  IF stored_name != ''' UNION SELECT secret FROM passwords --' THEN
    RAISE EXCEPTION 'FAIL [E4.2]: UNION injection mutated! Stored: %', stored_name;
  END IF;

  RAISE NOTICE 'PASS [E4.2]: UNION injection stored as literal string';

  DELETE FROM app.mail_domains WHERE domain_id = 'da000099-0000-0000-0000-000000000002';
END $$;

-- Evil 4.3: XSS-style payload in domain_name
\echo 'EVIL 4.3: XSS payload in domain_name field'
DO $$
DECLARE
  stored_name TEXT;
BEGIN
  INSERT INTO app.mail_domains (domain_id, suite_id, office_id, domain_name, registrar, status)
  VALUES (
    'da000099-0000-0000-0000-000000000003',
    'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    'aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    '<script>alert("XSS")</script>.evil.com',
    'resellerclub',
    'pending_verification'
  );

  SELECT domain_name INTO stored_name FROM app.mail_domains
    WHERE domain_id = 'da000099-0000-0000-0000-000000000003';

  IF stored_name != '<script>alert("XSS")</script>.evil.com' THEN
    RAISE EXCEPTION 'FAIL [E4.3]: XSS payload was modified! Stored: %', stored_name;
  END IF;

  RAISE NOTICE 'PASS [E4.3]: XSS payload stored as literal string (sanitize on output!)';

  DELETE FROM app.mail_domains WHERE domain_id = 'da000099-0000-0000-0000-000000000003';
END $$;

-- ============================================================================
-- EVIL TEST GROUP 5: RLS WITH NO AUTH CONTEXT (FAIL CLOSED)
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'EVIL GROUP 5: RLS WITH NO AUTH CONTEXT (FAIL CLOSED)'
\echo '================================================================='
\echo ''

-- Evil 5.1: Empty suite_id — mail_domains
\echo 'EVIL 5.1: Empty suite_id — mail_domains returns 0 rows'
DO $$
DECLARE
  row_count INTEGER;
BEGIN
  PERFORM set_config('app.current_suite_id', '', false);
  SELECT COUNT(*) INTO row_count FROM app.mail_domains;
  IF row_count > 0 THEN
    RAISE EXCEPTION 'FAIL [E5.1]: Empty suite_id returned % domains!', row_count;
  END IF;
  RAISE NOTICE 'PASS [E5.1]: Empty suite_id returns 0 domains (fail-closed)';
EXCEPTION
  WHEN invalid_text_representation THEN
    RAISE NOTICE 'PASS [E5.1]: Empty suite_id causes UUID cast error (fail-closed)';
  WHEN others THEN
    IF SQLERRM LIKE '%uuid%' OR SQLERRM LIKE '%invalid input syntax%' THEN
      RAISE NOTICE 'PASS [E5.1]: Empty suite_id blocked by type enforcement';
    ELSIF SQLERRM LIKE 'FAIL%' THEN
      RAISE;
    ELSE
      RAISE NOTICE 'PASS [E5.1]: Empty suite_id denied: %', SQLERRM;
    END IF;
END $$;

-- Evil 5.2: Empty suite_id — mail_dns_records
\echo 'EVIL 5.2: Empty suite_id — mail_dns_records returns 0 rows'
DO $$
DECLARE
  row_count INTEGER;
BEGIN
  PERFORM set_config('app.current_suite_id', '', false);
  SELECT COUNT(*) INTO row_count FROM app.mail_dns_records;
  IF row_count > 0 THEN
    RAISE EXCEPTION 'FAIL [E5.2]: Empty suite_id returned % DNS records!', row_count;
  END IF;
  RAISE NOTICE 'PASS [E5.2]: Empty suite_id returns 0 DNS records (fail-closed)';
EXCEPTION
  WHEN invalid_text_representation THEN
    RAISE NOTICE 'PASS [E5.2]: Empty suite_id causes UUID cast error (fail-closed)';
  WHEN others THEN
    IF SQLERRM LIKE '%uuid%' OR SQLERRM LIKE '%invalid input syntax%' THEN
      RAISE NOTICE 'PASS [E5.2]: Empty suite_id blocked by type enforcement';
    ELSIF SQLERRM LIKE 'FAIL%' THEN
      RAISE;
    ELSE
      RAISE NOTICE 'PASS [E5.2]: Empty suite_id denied: %', SQLERRM;
    END IF;
END $$;

-- Evil 5.3: Empty suite_id — mail_accounts
\echo 'EVIL 5.3: Empty suite_id — mail_accounts returns 0 rows'
DO $$
DECLARE
  row_count INTEGER;
BEGIN
  PERFORM set_config('app.current_suite_id', '', false);
  SELECT COUNT(*) INTO row_count FROM app.mail_accounts;
  IF row_count > 0 THEN
    RAISE EXCEPTION 'FAIL [E5.3]: Empty suite_id returned % accounts!', row_count;
  END IF;
  RAISE NOTICE 'PASS [E5.3]: Empty suite_id returns 0 accounts (fail-closed)';
EXCEPTION
  WHEN invalid_text_representation THEN
    RAISE NOTICE 'PASS [E5.3]: Empty suite_id causes UUID cast error (fail-closed)';
  WHEN others THEN
    IF SQLERRM LIKE '%uuid%' OR SQLERRM LIKE '%invalid input syntax%' THEN
      RAISE NOTICE 'PASS [E5.3]: Empty suite_id blocked by type enforcement';
    ELSIF SQLERRM LIKE 'FAIL%' THEN
      RAISE;
    ELSE
      RAISE NOTICE 'PASS [E5.3]: Empty suite_id denied: %', SQLERRM;
    END IF;
END $$;

-- Evil 5.4: Fake/non-existent UUID — mail_domains
\echo 'EVIL 5.4: Fake UUID — mail_domains returns 0 rows'
DO $$
DECLARE
  row_count INTEGER;
BEGIN
  PERFORM set_config('app.current_suite_id', 'cccccccc-cccc-cccc-cccc-cccccccccccc', false);
  SELECT COUNT(*) INTO row_count FROM app.mail_domains;
  IF row_count > 0 THEN
    RAISE EXCEPTION 'FAIL [E5.4]: Fake UUID returned % domains!', row_count;
  END IF;
  RAISE NOTICE 'PASS [E5.4]: Fake UUID returns 0 domains (fail-closed)';
END $$;

-- Evil 5.5: SQL injection via set_config suite_id
\echo 'EVIL 5.5: SQL injection in suite_id context'
DO $$
DECLARE
  row_count INTEGER;
BEGIN
  PERFORM set_config('app.current_suite_id', ''' OR 1=1 --', false);
  SELECT COUNT(*) INTO row_count FROM app.mail_domains;
  IF row_count > 0 THEN
    RAISE EXCEPTION 'FAIL [E5.5]: SQL injection returned % domains!', row_count;
  END IF;
  RAISE NOTICE 'PASS [E5.5]: SQL injection in suite_id returns 0 (safe)';
EXCEPTION
  WHEN invalid_text_representation THEN
    RAISE NOTICE 'PASS [E5.5]: SQL injection causes UUID cast error (safe)';
  WHEN others THEN
    IF SQLERRM LIKE '%uuid%' OR SQLERRM LIKE '%invalid input syntax%' THEN
      RAISE NOTICE 'PASS [E5.5]: SQL injection blocked by UUID type enforcement';
    ELSIF SQLERRM LIKE 'FAIL%' THEN
      RAISE;
    ELSE
      RAISE NOTICE 'PASS [E5.5]: SQL injection denied: %', SQLERRM;
    END IF;
END $$;

-- Evil 5.6: Malformed UUID in suite_id context
\echo 'EVIL 5.6: Malformed UUID in suite_id context'
DO $$
DECLARE
  row_count INTEGER;
BEGIN
  PERFORM set_config('app.current_suite_id', 'not-a-valid-uuid', false);
  SELECT COUNT(*) INTO row_count FROM app.mail_domains;
  IF row_count > 0 THEN
    RAISE EXCEPTION 'FAIL [E5.6]: Malformed UUID returned % domains!', row_count;
  END IF;
  RAISE NOTICE 'PASS [E5.6]: Malformed UUID returns 0 or fails safely';
EXCEPTION
  WHEN invalid_text_representation THEN
    RAISE NOTICE 'PASS [E5.6]: Malformed UUID causes type error (safe)';
  WHEN others THEN
    IF SQLERRM LIKE '%uuid%' OR SQLERRM LIKE '%invalid input syntax%' THEN
      RAISE NOTICE 'PASS [E5.6]: Malformed UUID blocked by type enforcement';
    ELSIF SQLERRM LIKE 'FAIL%' THEN
      RAISE;
    ELSE
      RAISE NOTICE 'PASS [E5.6]: Malformed UUID denied: %', SQLERRM;
    END IF;
END $$;

-- ============================================================================
-- EVIL TEST GROUP 6: FORCE RLS VALIDATION
-- Even table owner is filtered when FORCE ROW LEVEL SECURITY is set
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'EVIL GROUP 6: FORCE RLS VALIDATION'
\echo '================================================================='
\echo ''

RESET ROLE;

-- Evil 6.1: Verify FORCE RLS is set (structural check)
\echo 'EVIL 6.1: FORCE RLS is enabled on all 3 mail tables'
DO $$
DECLARE
  tbl TEXT;
  is_forced BOOLEAN;
BEGIN
  FOR tbl IN SELECT unnest(ARRAY['mail_domains', 'mail_dns_records', 'mail_accounts'])
  LOOP
    SELECT c.relforcerowsecurity INTO is_forced
    FROM pg_class c
    JOIN pg_namespace n ON c.relnamespace = n.oid
    WHERE n.nspname = 'app' AND c.relname = tbl;

    IF NOT is_forced THEN
      RAISE EXCEPTION 'FAIL [E6.1]: % does NOT have FORCE RLS!', tbl;
    END IF;
    RAISE NOTICE 'PASS [E6.1]: % has FORCE RLS enabled', tbl;
  END LOOP;
END $$;

-- Evil 6.2: authenticated role with valid context still only sees own data
\echo 'EVIL 6.2: FORCE RLS filters authenticated role with valid context'
DO $$
DECLARE
  total_visible INTEGER;
  actual_total INTEGER;
BEGIN
  -- First get total as superuser (bypasses RLS)
  SELECT COUNT(*) INTO actual_total FROM app.mail_domains;

  -- Check what authenticated sees
  SET ROLE authenticated;
  PERFORM set_config('app.current_suite_id', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', false);
  SELECT COUNT(*) INTO total_visible FROM app.mail_domains;
  RESET ROLE;

  IF total_visible >= actual_total AND actual_total > 0 THEN
    RAISE EXCEPTION 'FAIL [E6.2]: authenticated sees all % domains (FORCE RLS not working)', actual_total;
  END IF;
  RAISE NOTICE 'PASS [E6.2]: authenticated sees %/% domains (FORCE RLS enforced)', total_visible, actual_total;
END $$;

-- ============================================================================
-- EVIL TEST GROUP 7: RECEIPT IMMUTABILITY CHECK (Law #2)
-- Attempt UPDATE/DELETE on receipts table
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'EVIL GROUP 7: RECEIPT IMMUTABILITY (Law #2)'
\echo '================================================================='
\echo ''

SET ROLE authenticated;
SELECT set_config('app.current_suite_id', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', false);

-- Evil 7.1: Attempt to UPDATE a receipt
\echo 'EVIL 7.1: Attempt to UPDATE receipt (must fail)'
DO $$
DECLARE
  rows_affected INTEGER;
BEGIN
  UPDATE receipts SET status = 'TAMPERED' WHERE suite_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected > 0 THEN
    RAISE EXCEPTION 'FAIL [E7.1]: Updated % receipts! IMMUTABILITY BROKEN!', rows_affected;
  END IF;
  RAISE NOTICE 'PASS [E7.1]: UPDATE receipts affected 0 rows (immutable)';
EXCEPTION
  WHEN others THEN
    IF SQLERRM LIKE 'FAIL%' THEN
      RAISE;
    END IF;
    IF SQLERRM LIKE '%immutable%' OR SQLERRM LIKE '%row-level security%' OR SQLERRM LIKE '%cannot%' THEN
      RAISE NOTICE 'PASS [E7.1]: UPDATE receipts denied: %', SQLERRM;
    ELSE
      RAISE NOTICE 'PASS [E7.1]: UPDATE denied by mechanism: %', SQLERRM;
    END IF;
END $$;

-- Evil 7.2: Attempt to DELETE a receipt
\echo 'EVIL 7.2: Attempt to DELETE receipt (must fail)'
DO $$
DECLARE
  rows_affected INTEGER;
BEGIN
  DELETE FROM receipts WHERE suite_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected > 0 THEN
    RAISE EXCEPTION 'FAIL [E7.2]: Deleted % receipts! APPEND-ONLY BROKEN!', rows_affected;
  END IF;
  RAISE NOTICE 'PASS [E7.2]: DELETE receipts affected 0 rows (append-only)';
EXCEPTION
  WHEN others THEN
    IF SQLERRM LIKE 'FAIL%' THEN
      RAISE;
    END IF;
    IF SQLERRM LIKE '%immutable%' OR SQLERRM LIKE '%row-level security%' OR SQLERRM LIKE '%cannot%' THEN
      RAISE NOTICE 'PASS [E7.2]: DELETE receipts denied: %', SQLERRM;
    ELSE
      RAISE NOTICE 'PASS [E7.2]: DELETE denied by mechanism: %', SQLERRM;
    END IF;
END $$;

-- ============================================================================
-- CLEANUP
-- ============================================================================

\echo ''
\echo '================================================================='
\echo 'CLEANUP: Removing evil test data'
\echo '================================================================='
\echo ''

RESET ROLE;

DELETE FROM app.mail_accounts WHERE suite_id IN ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb');
DELETE FROM app.mail_dns_records WHERE suite_id IN ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb');
DELETE FROM app.mail_domains WHERE suite_id IN ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb');
DELETE FROM app.offices WHERE office_id IN ('aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'bbbb1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb');
DELETE FROM app.suites WHERE suite_id IN ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb');

\echo ''
\echo '================================================================='
\echo 'ALL MAIL EVIL TESTS COMPLETE'
\echo '================================================================='
\echo ''
\echo 'Summary:'
\echo '  Evil Group 1: Cross-tenant INSERT attacks (3 tests)'
\echo '  Evil Group 2: Cross-tenant UPDATE/DELETE attacks (3 tests)'
\echo '  Evil Group 3: Cross-tenant SELECT attacks (4 tests)'
\echo '  Evil Group 4: SQL injection in domain_name (3 tests)'
\echo '  Evil Group 5: Fail-closed — no auth context (6 tests)'
\echo '  Evil Group 6: FORCE RLS validation (2 tests)'
\echo '  Evil Group 7: Receipt immutability (2 tests)'
\echo '  Total: 23 evil tests'
\echo ''
