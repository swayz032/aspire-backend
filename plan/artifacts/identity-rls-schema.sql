-- ============================================================================
-- ASPIRE IDENTITY & RLS SCHEMA - SUITE/OFFICE MULTI-TENANT ISOLATION
-- ============================================================================
--
-- Purpose: Multi-tenant identity system (Suite = Organization, Office = Individual)
-- Gate: Gate 7 - RLS Isolation (CRITICAL)
-- Phase Introduced: Phase 1 (Core Orchestrator)
--
-- Key Requirements:
-- - Zero cross-tenant data leakage (CRITICAL - Aspire Law #6)
-- - Suite/Office separation (organizational + individual isolation)
-- - Row-Level Security (RLS) on ALL tenant-scoped tables
-- - Evil test suite: 100% pass rate (zero cross-tenant SELECT)
--
-- Related Files:
-- - plan/gates/gate-07-rls-isolation.md (full specification + evil tests)
-- - plan/Aspire-Production-Roadmap.md (Phase 1 implementation)
-- - CLAUDE.md (Aspire Law #6: Tenant Isolation)
--
-- ============================================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- SUITES TABLE (Organizations / Tenants)
-- ============================================================================

CREATE TABLE suites (
    -- Identity
    suite_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    suite_name TEXT NOT NULL,  -- Organization name (e.g., "Acme Corp")

    -- Authentication & Authorization
    owner_email TEXT NOT NULL UNIQUE,  -- Primary suite owner (for login)
    auth_provider TEXT NOT NULL DEFAULT 'email_password',  -- 'email_password' | 'google_oauth' | 'microsoft_saml'
    auth_provider_id TEXT,  -- External auth provider ID (if using OAuth/SAML)

    -- Subscription & Billing
    subscription_tier TEXT NOT NULL DEFAULT 'founder_quarter' CHECK (subscription_tier IN ('founder_quarter', 'team_expansion', 'enterprise')),
    subscription_status TEXT NOT NULL DEFAULT 'active' CHECK (subscription_status IN ('active', 'trial', 'suspended', 'cancelled')),
    billing_email TEXT,

    -- Metadata
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ,

    -- Feature Flags (per-suite configuration)
    feature_flags JSONB DEFAULT '{}'::jsonb,  -- {"video_mode_enabled": true, "advanced_rag": false}

    -- Status
    is_active BOOLEAN NOT NULL DEFAULT true,
    is_deleted BOOLEAN NOT NULL DEFAULT false  -- Soft delete (preserve receipts)
);

-- ============================================================================
-- OFFICES TABLE (Individuals within Suites)
-- ============================================================================

CREATE TABLE offices (
    -- Identity
    office_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    suite_id UUID NOT NULL REFERENCES suites(suite_id) ON DELETE CASCADE,

    -- Human Identity
    office_name TEXT NOT NULL,  -- Individual's name (e.g., "John Doe")
    office_email TEXT NOT NULL,  -- Individual's work email
    office_role TEXT NOT NULL DEFAULT 'member' CHECK (office_role IN ('owner', 'admin', 'member', 'guest')),

    -- Authentication
    auth_provider TEXT NOT NULL DEFAULT 'email_password',
    auth_provider_id TEXT,

    -- Preferences
    preferences JSONB DEFAULT '{}'::jsonb,  -- {"default_call_mode": "warm", "notification_settings": {...}}

    -- Metadata
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ,

    -- Status
    is_active BOOLEAN NOT NULL DEFAULT true,
    is_deleted BOOLEAN NOT NULL DEFAULT false,  -- Soft delete

    -- Unique constraint: One email per suite
    CONSTRAINT unique_office_email_per_suite UNIQUE (suite_id, office_email)
);

-- ============================================================================
-- INDEXES (Performance Optimization)
-- ============================================================================

-- Suites
CREATE INDEX idx_suites_owner_email ON suites (owner_email);
CREATE INDEX idx_suites_subscription ON suites (subscription_tier, subscription_status);
CREATE INDEX idx_suites_active ON suites (is_active, is_deleted);

-- Offices
CREATE INDEX idx_offices_suite ON offices (suite_id, is_active);
CREATE INDEX idx_offices_email ON offices (office_email);
CREATE INDEX idx_offices_role ON offices (suite_id, office_role);
CREATE INDEX idx_offices_active ON offices (is_active, is_deleted);

-- ============================================================================
-- ROW-LEVEL SECURITY (RLS) - ZERO CROSS-TENANT LEAKAGE
-- ============================================================================
-- Gate 7: RLS Isolation - CRITICAL
-- ============================================================================

-- Enable RLS on suites table
ALTER TABLE suites ENABLE ROW LEVEL SECURITY;

-- Policy: Suite can only see itself
CREATE POLICY suite_self_access ON suites
  FOR ALL
  USING (suite_id = current_setting('app.current_suite_id', true)::uuid);

-- Enable RLS on offices table
ALTER TABLE offices ENABLE ROW LEVEL SECURITY;

-- Policy: Office can only see offices within same suite
CREATE POLICY office_suite_isolation ON offices
  FOR ALL
  USING (suite_id = current_setting('app.current_suite_id', true)::uuid);

-- ============================================================================
-- AUTO-UPDATE TIMESTAMP TRIGGERS
-- ============================================================================

CREATE OR REPLACE FUNCTION trigger_update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER suites_update_timestamp
    BEFORE UPDATE ON suites
    FOR EACH ROW
    EXECUTE FUNCTION trigger_update_timestamp();

CREATE TRIGGER offices_update_timestamp
    BEFORE UPDATE ON offices
    FOR EACH ROW
    EXECUTE FUNCTION trigger_update_timestamp();

-- ============================================================================
-- SESSION CONTEXT HELPER FUNCTIONS
-- ============================================================================

-- Function: Set current suite context (MUST be called before EVERY query)
CREATE OR REPLACE FUNCTION set_suite_context(p_suite_id UUID)
RETURNS VOID AS $$
BEGIN
    PERFORM set_config('app.current_suite_id', p_suite_id::text, false);
END;
$$ LANGUAGE plpgsql;

-- Function: Get current suite context
CREATE OR REPLACE FUNCTION get_suite_context()
RETURNS UUID AS $$
BEGIN
    RETURN current_setting('app.current_suite_id', true)::uuid;
EXCEPTION
    WHEN OTHERS THEN
        RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;

-- Function: Clear suite context (for testing)
CREATE OR REPLACE FUNCTION clear_suite_context()
RETURNS VOID AS $$
BEGIN
    PERFORM set_config('app.current_suite_id', '', false);
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- QUERY HELPER FUNCTIONS
-- ============================================================================

-- Function: Get all offices in current suite
CREATE OR REPLACE FUNCTION get_suite_offices(p_suite_id UUID)
RETURNS TABLE (
    office_id UUID,
    office_name TEXT,
    office_email TEXT,
    office_role TEXT,
    is_active BOOLEAN,
    created_at TIMESTAMPTZ
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        o.office_id,
        o.office_name,
        o.office_email,
        o.office_role,
        o.is_active,
        o.created_at
    FROM offices o
    WHERE o.suite_id = p_suite_id
      AND o.is_deleted = false
    ORDER BY o.created_at ASC;
END;
$$ LANGUAGE plpgsql STABLE;

-- Function: Get suite info
CREATE OR REPLACE FUNCTION get_suite_info(p_suite_id UUID)
RETURNS TABLE (
    suite_id UUID,
    suite_name TEXT,
    owner_email TEXT,
    subscription_tier TEXT,
    subscription_status TEXT,
    created_at TIMESTAMPTZ
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        s.suite_id,
        s.suite_name,
        s.owner_email,
        s.subscription_tier,
        s.subscription_status,
        s.created_at
    FROM suites s
    WHERE s.suite_id = p_suite_id
      AND s.is_deleted = false;
END;
$$ LANGUAGE plpgsql STABLE;

-- Function: Soft delete suite (preserve receipts)
CREATE OR REPLACE FUNCTION soft_delete_suite(p_suite_id UUID)
RETURNS VOID AS $$
BEGIN
    -- Mark suite as deleted (receipts remain for audit)
    UPDATE suites
    SET is_deleted = true,
        is_active = false,
        updated_at = NOW()
    WHERE suite_id = p_suite_id;

    -- Mark all offices as deleted
    UPDATE offices
    SET is_deleted = true,
        is_active = false,
        updated_at = NOW()
    WHERE suite_id = p_suite_id;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- EVIL TEST SUITE (Zero Cross-Tenant Leakage Verification)
-- ============================================================================
-- Gate 7: RLS Isolation - CRITICAL
-- These tests MUST pass before production deployment
-- ============================================================================

-- Test 1: Cross-Tenant SELECT (MUST RETURN ZERO ROWS)
CREATE OR REPLACE FUNCTION test_cross_tenant_select()
RETURNS TABLE (
    test_name TEXT,
    pass BOOLEAN,
    details TEXT
) AS $$
DECLARE
    v_suite_a UUID;
    v_suite_b UUID;
    v_office_a UUID;
    v_cross_tenant_count INTEGER;
BEGIN
    -- Create Suite A
    INSERT INTO suites (suite_name, owner_email)
    VALUES ('Test Suite A', 'owner_a@test.com')
    RETURNING suite_id INTO v_suite_a;

    -- Create Suite B
    INSERT INTO suites (suite_name, owner_email)
    VALUES ('Test Suite B', 'owner_b@test.com')
    RETURNING suite_id INTO v_suite_b;

    -- Create Office in Suite A
    INSERT INTO offices (suite_id, office_name, office_email, office_role)
    VALUES (v_suite_a, 'User A', 'user_a@test.com', 'member')
    RETURNING office_id INTO v_office_a;

    -- Set context to Suite B
    PERFORM set_suite_context(v_suite_b);

    -- Attempt to read Suite A's offices (MUST FAIL - RLS blocks)
    SELECT COUNT(*) INTO v_cross_tenant_count
    FROM offices
    WHERE suite_id = v_suite_a;

    -- Cleanup
    PERFORM clear_suite_context();
    DELETE FROM suites WHERE suite_id IN (v_suite_a, v_suite_b);

    -- Return test result
    RETURN QUERY SELECT
        'Cross-Tenant SELECT Test'::TEXT,
        (v_cross_tenant_count = 0) AS pass,
        format('Suite B attempted to read Suite A offices. Found: %s (expected: 0)', v_cross_tenant_count)::TEXT;
END;
$$ LANGUAGE plpgsql;

-- Test 2: Session Context Bypass Attempt (MUST FAIL)
CREATE OR REPLACE FUNCTION test_session_context_bypass()
RETURNS TABLE (
    test_name TEXT,
    pass BOOLEAN,
    details TEXT
) AS $$
DECLARE
    v_suite_a UUID;
    v_suite_b UUID;
    v_bypass_count INTEGER;
BEGIN
    -- Create Suite A
    INSERT INTO suites (suite_name, owner_email)
    VALUES ('Test Suite A', 'owner_a@test.com')
    RETURNING suite_id INTO v_suite_a;

    -- Create Suite B
    INSERT INTO suites (suite_name, owner_email)
    VALUES ('Test Suite B', 'owner_b@test.com')
    RETURNING suite_id INTO v_suite_b;

    -- Set context to Suite A
    PERFORM set_suite_context(v_suite_a);

    -- Attempt to override context within session (MUST BE BLOCKED by RLS)
    BEGIN
        PERFORM set_config('app.current_suite_id', v_suite_b::text, true);
    EXCEPTION
        WHEN OTHERS THEN
            NULL;  -- Expected to fail
    END;

    -- Try to read Suite B (should still be blocked by original context)
    SELECT COUNT(*) INTO v_bypass_count
    FROM suites
    WHERE suite_id = v_suite_b;

    -- Cleanup
    PERFORM clear_suite_context();
    DELETE FROM suites WHERE suite_id IN (v_suite_a, v_suite_b);

    -- Return test result
    RETURN QUERY SELECT
        'Session Context Bypass Test'::TEXT,
        (v_bypass_count = 0) AS pass,
        format('Attempted to bypass session context. Found Suite B: %s (expected: 0)', v_bypass_count)::TEXT;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- EXAMPLE USAGE
-- ============================================================================

-- Create suite and office:
/*
-- Create suite
INSERT INTO suites (suite_name, owner_email, subscription_tier)
VALUES ('Acme Corp', 'founder@acme.com', 'founder_quarter')
RETURNING suite_id;

-- Create office
INSERT INTO offices (suite_id, office_name, office_email, office_role)
VALUES ('<suite_id>', 'Jane Founder', 'jane@acme.com', 'owner');

-- Set session context (REQUIRED before EVERY query)
SELECT set_suite_context('<suite_id>');

-- Query offices in current suite
SELECT * FROM get_suite_offices('<suite_id>');

-- Get suite info
SELECT * FROM get_suite_info('<suite_id>');
*/

-- Run evil tests (MUST pass before production):
/*
SELECT * FROM test_cross_tenant_select();
SELECT * FROM test_session_context_bypass();
*/

-- ============================================================================
-- DEPLOYMENT CHECKLIST
-- ============================================================================
-- [ ] RLS enabled on suites and offices tables
-- [ ] Session context set before EVERY database query:
--     SELECT set_suite_context('<suite_id_from_jwt>');
-- [ ] Evil test suite passes 100% (zero cross-tenant leakage):
--     SELECT * FROM test_cross_tenant_select();  -- MUST PASS
--     SELECT * FROM test_session_context_bypass();  -- MUST PASS
-- [ ] Application code NEVER trusts client-provided suite_id
--     (extract from server-side JWT validation only)
-- [ ] All tenant-scoped tables have RLS enabled:
--     - receipts (CRITICAL)
--     - checkpoints (CRITICAL)
--     - capability_tokens (CRITICAL)
--     - Any future tables with suite_id column
-- ============================================================================

-- ============================================================================
-- RELATED TABLES (Must also have RLS policies)
-- ============================================================================
-- receipts: RLS policy using suite_id (Gate 6 + Gate 7)
-- checkpoints: RLS policy using suite_id
-- capability_tokens: RLS policy using suite_id
-- All future tenant-scoped tables MUST enable RLS
-- ============================================================================
