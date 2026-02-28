-- ============================================================================
-- RLS Isolation Tests: founder_hub_notes
-- Phase 3 W10 — Founder Hub Notes table tenant isolation (Law #6)
--
-- Tests: cross-tenant SELECT/INSERT/UPDATE/DELETE, missing context,
--        service role bypass, structural verification
-- Pattern: mail_tables_rls_test.sql
-- ============================================================================

-- ============================================================================
-- SETUP: Create two test suites with seed notes
-- ============================================================================

-- Clean up any previous test data
DELETE FROM founder_hub_notes WHERE suite_id IN (
  '11111111-1111-1111-1111-111111111111'::uuid,
  '22222222-2222-2222-2222-222222222222'::uuid
);

-- Ensure test suites exist (safe upserts)
INSERT INTO suites (id, name)
VALUES ('11111111-1111-1111-1111-111111111111', 'RLS Test Suite Alpha')
ON CONFLICT (id) DO NOTHING;

INSERT INTO suites (id, name)
VALUES ('22222222-2222-2222-2222-222222222222', 'RLS Test Suite Bravo')
ON CONFLICT (id) DO NOTHING;

-- Seed notes for Suite Alpha
INSERT INTO founder_hub_notes (id, suite_id, title, content, pinned)
VALUES
  ('aaaa0001-0001-0001-0001-000000000001', '11111111-1111-1111-1111-111111111111', 'Alpha Note 1', 'Content from Alpha suite', true),
  ('aaaa0001-0001-0001-0001-000000000002', '11111111-1111-1111-1111-111111111111', 'Alpha Note 2', 'Private Alpha data', false);

-- Seed notes for Suite Bravo
INSERT INTO founder_hub_notes (id, suite_id, title, content, pinned)
VALUES
  ('bbbb0002-0002-0002-0002-000000000001', '22222222-2222-2222-2222-222222222222', 'Bravo Note 1', 'Content from Bravo suite', false),
  ('bbbb0002-0002-0002-0002-000000000002', '22222222-2222-2222-2222-222222222222', 'Bravo Note 2', 'Private Bravo data', true);

-- ============================================================================
-- GROUP 1: Cross-Tenant SELECT Isolation
-- Suite Alpha MUST NOT see Suite Bravo notes (and vice versa)
-- ============================================================================

SET ROLE authenticated;

-- Test 1.1: Suite Alpha can only see its own notes
SELECT set_config('app.current_suite_id', '11111111-1111-1111-1111-111111111111', true);
DO $$
DECLARE
  row_count INT;
  bravo_leak INT;
BEGIN
  SELECT count(*) INTO row_count FROM founder_hub_notes;
  IF row_count != 2 THEN
    RAISE EXCEPTION '[FAIL] Test 1.1: Suite Alpha sees % notes (expected 2)', row_count;
  END IF;

  SELECT count(*) INTO bravo_leak FROM founder_hub_notes
    WHERE suite_id = '22222222-2222-2222-2222-222222222222';
  IF bravo_leak != 0 THEN
    RAISE EXCEPTION '[FAIL] Test 1.1: Suite Alpha can see % Bravo notes (CROSS-TENANT LEAK!)', bravo_leak;
  END IF;

  RAISE NOTICE '[PASS] Test 1.1: Suite Alpha sees only its own 2 notes';
END $$;

-- Test 1.2: Suite Bravo can only see its own notes
SELECT set_config('app.current_suite_id', '22222222-2222-2222-2222-222222222222', true);
DO $$
DECLARE
  row_count INT;
  alpha_leak INT;
BEGIN
  SELECT count(*) INTO row_count FROM founder_hub_notes;
  IF row_count != 2 THEN
    RAISE EXCEPTION '[FAIL] Test 1.2: Suite Bravo sees % notes (expected 2)', row_count;
  END IF;

  SELECT count(*) INTO alpha_leak FROM founder_hub_notes
    WHERE suite_id = '11111111-1111-1111-1111-111111111111';
  IF alpha_leak != 0 THEN
    RAISE EXCEPTION '[FAIL] Test 1.2: Suite Bravo can see % Alpha notes (CROSS-TENANT LEAK!)', alpha_leak;
  END IF;

  RAISE NOTICE '[PASS] Test 1.2: Suite Bravo sees only its own 2 notes';
END $$;

-- Test 1.3: Direct ID lookup across tenants fails silently
SELECT set_config('app.current_suite_id', '11111111-1111-1111-1111-111111111111', true);
DO $$
DECLARE
  found_count INT;
BEGIN
  SELECT count(*) INTO found_count FROM founder_hub_notes
    WHERE id = 'bbbb0002-0002-0002-0002-000000000001';
  IF found_count != 0 THEN
    RAISE EXCEPTION '[FAIL] Test 1.3: Alpha can read Bravo note by direct ID (CROSS-TENANT LEAK!)';
  END IF;

  RAISE NOTICE '[PASS] Test 1.3: Direct ID lookup across tenants returns zero rows';
END $$;

-- ============================================================================
-- GROUP 2: Cross-Tenant INSERT Isolation
-- Suite Alpha MUST NOT insert notes into Suite Bravo
-- ============================================================================

-- Test 2.1: Suite Alpha cannot insert a note with Bravo's suite_id
SELECT set_config('app.current_suite_id', '11111111-1111-1111-1111-111111111111', true);
DO $$
BEGIN
  INSERT INTO founder_hub_notes (id, suite_id, title, content)
  VALUES ('cccc0003-0003-0003-0003-000000000001', '22222222-2222-2222-2222-222222222222', 'Injected by Alpha', 'Cross-tenant injection');

  RAISE EXCEPTION '[FAIL] Test 2.1: Alpha inserted note into Bravo suite (RLS INSERT policy BYPASSED!)';
EXCEPTION
  WHEN others THEN
    IF SQLERRM LIKE '%new row violates%' OR SQLERRM LIKE '%policy%' THEN
      RAISE NOTICE '[PASS] Test 2.1: Cross-tenant INSERT blocked by RLS';
    ELSIF SQLERRM LIKE '%FAIL%' THEN
      RAISE EXCEPTION '%', SQLERRM;
    ELSE
      RAISE NOTICE '[PASS] Test 2.1: Cross-tenant INSERT blocked (error: %)', SQLERRM;
    END IF;
END $$;

-- Test 2.2: Suite Alpha CAN insert note with its own suite_id
SELECT set_config('app.current_suite_id', '11111111-1111-1111-1111-111111111111', true);
DO $$
DECLARE
  inserted_count INT;
BEGIN
  INSERT INTO founder_hub_notes (id, suite_id, title, content)
  VALUES ('cccc0003-0003-0003-0003-000000000002', '11111111-1111-1111-1111-111111111111', 'Valid Alpha Note', 'Legitimate insert');

  SELECT count(*) INTO inserted_count FROM founder_hub_notes
    WHERE id = 'cccc0003-0003-0003-0003-000000000002';
  IF inserted_count != 1 THEN
    RAISE EXCEPTION '[FAIL] Test 2.2: Own-tenant INSERT did not persist';
  END IF;

  RAISE NOTICE '[PASS] Test 2.2: Own-tenant INSERT succeeds';
END $$;

-- ============================================================================
-- GROUP 3: Cross-Tenant UPDATE Isolation
-- Suite Alpha MUST NOT update Suite Bravo notes
-- ============================================================================

-- Test 3.1: Suite Alpha cannot update Bravo notes (silently affects 0 rows)
SELECT set_config('app.current_suite_id', '11111111-1111-1111-1111-111111111111', true);
DO $$
DECLARE
  affected INT;
  bravo_title TEXT;
BEGIN
  UPDATE founder_hub_notes SET title = 'HACKED BY ALPHA'
    WHERE id = 'bbbb0002-0002-0002-0002-000000000001';
  GET DIAGNOSTICS affected = ROW_COUNT;

  IF affected != 0 THEN
    RAISE EXCEPTION '[FAIL] Test 3.1: Alpha updated % Bravo notes (CROSS-TENANT LEAK!)', affected;
  END IF;

  -- Verify Bravo note is untouched (switch context to Bravo)
  PERFORM set_config('app.current_suite_id', '22222222-2222-2222-2222-222222222222', true);
  SELECT title INTO bravo_title FROM founder_hub_notes
    WHERE id = 'bbbb0002-0002-0002-0002-000000000001';
  IF bravo_title != 'Bravo Note 1' THEN
    RAISE EXCEPTION '[FAIL] Test 3.1: Bravo note title changed to "%"', bravo_title;
  END IF;

  RAISE NOTICE '[PASS] Test 3.1: Cross-tenant UPDATE silently affects 0 rows';
END $$;

-- Test 3.2: Suite Alpha CAN update its own notes
SELECT set_config('app.current_suite_id', '11111111-1111-1111-1111-111111111111', true);
DO $$
DECLARE
  affected INT;
BEGIN
  UPDATE founder_hub_notes SET title = 'Alpha Note 1 Updated'
    WHERE id = 'aaaa0001-0001-0001-0001-000000000001';
  GET DIAGNOSTICS affected = ROW_COUNT;

  IF affected != 1 THEN
    RAISE EXCEPTION '[FAIL] Test 3.2: Own-tenant UPDATE affected % rows (expected 1)', affected;
  END IF;

  -- Revert for subsequent tests
  UPDATE founder_hub_notes SET title = 'Alpha Note 1'
    WHERE id = 'aaaa0001-0001-0001-0001-000000000001';

  RAISE NOTICE '[PASS] Test 3.2: Own-tenant UPDATE succeeds';
END $$;

-- ============================================================================
-- GROUP 4: Cross-Tenant DELETE Isolation
-- Suite Alpha MUST NOT delete Suite Bravo notes
-- ============================================================================

-- Test 4.1: Suite Alpha cannot delete Bravo notes (silently affects 0 rows)
SELECT set_config('app.current_suite_id', '11111111-1111-1111-1111-111111111111', true);
DO $$
DECLARE
  affected INT;
  bravo_count INT;
BEGIN
  DELETE FROM founder_hub_notes
    WHERE id = 'bbbb0002-0002-0002-0002-000000000001';
  GET DIAGNOSTICS affected = ROW_COUNT;

  IF affected != 0 THEN
    RAISE EXCEPTION '[FAIL] Test 4.1: Alpha deleted % Bravo notes (CROSS-TENANT LEAK!)', affected;
  END IF;

  -- Verify Bravo note still exists (switch context)
  PERFORM set_config('app.current_suite_id', '22222222-2222-2222-2222-222222222222', true);
  SELECT count(*) INTO bravo_count FROM founder_hub_notes
    WHERE id = 'bbbb0002-0002-0002-0002-000000000001';
  IF bravo_count != 1 THEN
    RAISE EXCEPTION '[FAIL] Test 4.1: Bravo note was deleted despite RLS';
  END IF;

  RAISE NOTICE '[PASS] Test 4.1: Cross-tenant DELETE silently affects 0 rows';
END $$;

-- Test 4.2: Suite Alpha CAN delete its own notes
SELECT set_config('app.current_suite_id', '11111111-1111-1111-1111-111111111111', true);
DO $$
DECLARE
  affected INT;
BEGIN
  -- Delete the note we inserted in Test 2.2
  DELETE FROM founder_hub_notes
    WHERE id = 'cccc0003-0003-0003-0003-000000000002';
  GET DIAGNOSTICS affected = ROW_COUNT;

  IF affected != 1 THEN
    RAISE EXCEPTION '[FAIL] Test 4.2: Own-tenant DELETE affected % rows (expected 1)', affected;
  END IF;

  RAISE NOTICE '[PASS] Test 4.2: Own-tenant DELETE succeeds';
END $$;

-- ============================================================================
-- GROUP 5: Missing Context (No suite_id set)
-- Operations MUST fail or return empty when no tenant context
-- ============================================================================

-- Test 5.1: SELECT with no context returns zero rows (fail-closed)
SELECT set_config('app.current_suite_id', '', true);
DO $$
DECLARE
  row_count INT;
BEGIN
  SELECT count(*) INTO row_count FROM founder_hub_notes;
  IF row_count != 0 THEN
    RAISE EXCEPTION '[FAIL] Test 5.1: No-context SELECT returned % rows (expected 0 — fail-closed violation!)', row_count;
  END IF;

  RAISE NOTICE '[PASS] Test 5.1: No-context SELECT returns 0 rows (fail-closed)';
EXCEPTION
  WHEN others THEN
    -- Cast error is also acceptable (fail-closed)
    IF SQLERRM LIKE '%invalid input syntax%' OR SQLERRM LIKE '%cannot be cast%' THEN
      RAISE NOTICE '[PASS] Test 5.1: No-context SELECT throws cast error (fail-closed)';
    ELSIF SQLERRM LIKE '%FAIL%' THEN
      RAISE EXCEPTION '%', SQLERRM;
    ELSE
      RAISE NOTICE '[PASS] Test 5.1: No-context SELECT blocked (error: %)', SQLERRM;
    END IF;
END $$;

-- Test 5.2: INSERT with no context is denied
SELECT set_config('app.current_suite_id', '', true);
DO $$
BEGIN
  INSERT INTO founder_hub_notes (id, suite_id, title, content)
  VALUES ('dddd0004-0004-0004-0004-000000000001', '11111111-1111-1111-1111-111111111111', 'No Context Insert', 'Should fail');

  RAISE EXCEPTION '[FAIL] Test 5.2: INSERT with no suite context succeeded (fail-closed violation!)';
EXCEPTION
  WHEN others THEN
    IF SQLERRM LIKE '%FAIL%' THEN
      RAISE EXCEPTION '%', SQLERRM;
    ELSE
      RAISE NOTICE '[PASS] Test 5.2: INSERT with no context denied (%)' , SQLERRM;
    END IF;
END $$;

-- ============================================================================
-- GROUP 6: Service Role Bypass (Admin Operations)
-- Service role MUST be able to read/write across tenants (admin use)
-- ============================================================================

RESET ROLE;

-- Test 6.1: Service role (postgres) can see ALL notes across tenants
DO $$
DECLARE
  total INT;
BEGIN
  SELECT count(*) INTO total FROM founder_hub_notes
    WHERE suite_id IN (
      '11111111-1111-1111-1111-111111111111',
      '22222222-2222-2222-2222-222222222222'
    );
  IF total < 4 THEN
    RAISE EXCEPTION '[FAIL] Test 6.1: Service role sees only % notes (expected >= 4)', total;
  END IF;

  RAISE NOTICE '[PASS] Test 6.1: Service role sees all % notes across tenants', total;
END $$;

-- Test 6.2: Service role can insert into any suite
DO $$
DECLARE
  inserted_count INT;
BEGIN
  INSERT INTO founder_hub_notes (id, suite_id, title, content)
  VALUES ('eeee0005-0005-0005-0005-000000000001', '22222222-2222-2222-2222-222222222222', 'Service Role Insert', 'Admin created note');

  SELECT count(*) INTO inserted_count FROM founder_hub_notes
    WHERE id = 'eeee0005-0005-0005-0005-000000000001';
  IF inserted_count != 1 THEN
    RAISE EXCEPTION '[FAIL] Test 6.2: Service role INSERT did not persist';
  END IF;

  -- Clean up
  DELETE FROM founder_hub_notes WHERE id = 'eeee0005-0005-0005-0005-000000000001';

  RAISE NOTICE '[PASS] Test 6.2: Service role can insert into any suite';
END $$;

-- ============================================================================
-- GROUP 7: Structural Verification
-- Verify RLS is enabled, policies exist, FORCE RLS is set
-- ============================================================================

-- Test 7.1: RLS is enabled on founder_hub_notes
DO $$
DECLARE
  rls_enabled BOOLEAN;
BEGIN
  SELECT relrowsecurity INTO rls_enabled
    FROM pg_class WHERE relname = 'founder_hub_notes';
  IF NOT rls_enabled THEN
    RAISE EXCEPTION '[FAIL] Test 7.1: RLS is NOT enabled on founder_hub_notes';
  END IF;

  RAISE NOTICE '[PASS] Test 7.1: RLS is enabled on founder_hub_notes';
END $$;

-- Test 7.2: FORCE RLS is intentionally NOT set
-- DESIGN DECISION: supabaseAdmin (service role) needs cross-tenant access for:
--   - Admin operations (profile updates via PATCH /api/onboarding/profile)
--   - N8N workflow receipts (system-level writes)
--   - Ops telemetry and audit queries
-- Tenant isolation is enforced for the 'authenticated' role via RLS policies (Groups 1-5).
-- Service role bypass is verified in Group 6.
DO $$
DECLARE
  force_rls BOOLEAN;
BEGIN
  SELECT relforcerowsecurity INTO force_rls
    FROM pg_class WHERE relname = 'founder_hub_notes';
  IF force_rls THEN
    RAISE EXCEPTION '[FAIL] Test 7.2: FORCE RLS is set — this breaks service-role admin access (supabaseAdmin). If FORCE RLS was intentionally added, update Group 6 tests accordingly.';
  END IF;

  RAISE NOTICE '[PASS] Test 7.2: FORCE RLS is not set (service-role bypass by design — see comment)';
END $$;

-- Test 7.3: Exactly 4 RLS policies exist (SELECT, INSERT, UPDATE, DELETE)
DO $$
DECLARE
  policy_count INT;
BEGIN
  SELECT count(*) INTO policy_count
    FROM pg_policies WHERE tablename = 'founder_hub_notes';
  IF policy_count != 4 THEN
    RAISE EXCEPTION '[FAIL] Test 7.3: Found % policies (expected 4: SELECT, INSERT, UPDATE, DELETE)', policy_count;
  END IF;

  RAISE NOTICE '[PASS] Test 7.3: Exactly 4 RLS policies on founder_hub_notes';
END $$;

-- Test 7.4: All 4 policy types are covered
DO $$
DECLARE
  sel_count INT;
  ins_count INT;
  upd_count INT;
  del_count INT;
BEGIN
  SELECT count(*) INTO sel_count FROM pg_policies
    WHERE tablename = 'founder_hub_notes' AND cmd = 'r';
  SELECT count(*) INTO ins_count FROM pg_policies
    WHERE tablename = 'founder_hub_notes' AND cmd = 'a';
  SELECT count(*) INTO upd_count FROM pg_policies
    WHERE tablename = 'founder_hub_notes' AND cmd = 'w';
  SELECT count(*) INTO del_count FROM pg_policies
    WHERE tablename = 'founder_hub_notes' AND cmd = 'd';

  IF sel_count < 1 OR ins_count < 1 OR upd_count < 1 OR del_count < 1 THEN
    RAISE EXCEPTION '[FAIL] Test 7.4: Missing policy type (S:%, I:%, U:%, D:%)',
      sel_count, ins_count, upd_count, del_count;
  END IF;

  RAISE NOTICE '[PASS] Test 7.4: All 4 policy types covered (S:%, I:%, U:%, D:%)',
    sel_count, ins_count, upd_count, del_count;
END $$;

-- Test 7.5: Foreign key to suites table exists
DO $$
DECLARE
  fk_count INT;
BEGIN
  SELECT count(*) INTO fk_count
    FROM information_schema.table_constraints tc
    JOIN information_schema.constraint_column_usage ccu
      ON tc.constraint_name = ccu.constraint_name
    WHERE tc.table_name = 'founder_hub_notes'
      AND tc.constraint_type = 'FOREIGN KEY'
      AND ccu.table_name = 'suites'
      AND ccu.column_name = 'id';
  IF fk_count < 1 THEN
    RAISE EXCEPTION '[FAIL] Test 7.5: No foreign key from founder_hub_notes.suite_id to suites.id';
  END IF;

  RAISE NOTICE '[PASS] Test 7.5: FK constraint to suites(id) exists';
END $$;

-- Test 7.6: Indexes exist for performance
DO $$
DECLARE
  idx_suite INT;
  idx_updated INT;
BEGIN
  SELECT count(*) INTO idx_suite FROM pg_indexes
    WHERE tablename = 'founder_hub_notes' AND indexname = 'idx_fh_notes_suite_id';
  SELECT count(*) INTO idx_updated FROM pg_indexes
    WHERE tablename = 'founder_hub_notes' AND indexname = 'idx_fh_notes_updated';

  IF idx_suite < 1 THEN
    RAISE EXCEPTION '[FAIL] Test 7.6: Missing index idx_fh_notes_suite_id';
  END IF;
  IF idx_updated < 1 THEN
    RAISE EXCEPTION '[FAIL] Test 7.6: Missing index idx_fh_notes_updated';
  END IF;

  RAISE NOTICE '[PASS] Test 7.6: Both performance indexes exist';
END $$;

-- ============================================================================
-- CLEANUP: Remove all test data
-- ============================================================================

RESET ROLE;

DELETE FROM founder_hub_notes WHERE suite_id IN (
  '11111111-1111-1111-1111-111111111111'::uuid,
  '22222222-2222-2222-2222-222222222222'::uuid
);

DELETE FROM suites WHERE id IN (
  '11111111-1111-1111-1111-111111111111'::uuid,
  '22222222-2222-2222-2222-222222222222'::uuid
);

-- ============================================================================
-- SUMMARY
-- ============================================================================
-- Group 1: Cross-tenant SELECT isolation (3 tests)
-- Group 2: Cross-tenant INSERT isolation (2 tests)
-- Group 3: Cross-tenant UPDATE isolation (2 tests)
-- Group 4: Cross-tenant DELETE isolation (2 tests)
-- Group 5: Missing context / fail-closed (2 tests)
-- Group 6: Service role bypass (2 tests)
-- Group 7: Structural verification (6 tests)
-- TOTAL: 19 tests
-- ============================================================================
