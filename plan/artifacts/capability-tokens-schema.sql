-- ============================================================================
-- ASPIRE CAPABILITY TOKENS SCHEMA - SHORT-LIVED AUTHORIZATION
-- ============================================================================
--
-- Purpose: Capability-based access control (least privilege, short-lived tokens)
-- Gate: Gate 5 - Capability Tokens (Aspire Law #5)
-- Phase Introduced: Phase 1 (Core Orchestrator)
--
-- Key Requirements:
-- - Short-lived tokens (<60s expiry, NO exceptions)
-- - Scoped permissions (tool + action + tenant)
-- - Revocable (support immediate revocation)
-- - Server-side validation ONLY (no client-side trust)
-- - Cryptographic signatures (HMAC-SHA256)
--
-- Related Files:
-- - plan/gates/gate-07-rls-isolation.md (RLS policies)
-- - plan/Aspire-Production-Roadmap.md (Phase 1 implementation)
-- - CLAUDE.md (Aspire Law #5: Capability Tokens)
--
-- ============================================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Enable pgcrypto for HMAC signatures
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================================
-- CAPABILITY TOKENS TABLE
-- ============================================================================

CREATE TABLE capability_tokens (
    -- Identity
    token_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Tenant Isolation (CRITICAL for RLS)
    suite_id UUID NOT NULL,
    office_id UUID NOT NULL,

    -- Authorization Scope
    tool_name TEXT NOT NULL,  -- e.g., "stripe_api", "gmail_api", "calendar_api"
    action_type TEXT NOT NULL,  -- e.g., "invoice.create", "email.send", "calendar.update"
    scopes TEXT[] NOT NULL,  -- e.g., ["invoice.write", "payment.read"]

    -- Token Lifecycle
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,  -- MUST be <60s from issued_at (enforced by trigger)
    revoked_at TIMESTAMPTZ,  -- NULL if active, populated if revoked

    -- Security
    signature TEXT NOT NULL,  -- HMAC-SHA256(token_id || suite_id || tool_name || expires_at, secret_key)
    secret_key_version INTEGER NOT NULL DEFAULT 1,  -- Support key rotation

    -- Usage Tracking
    used_at TIMESTAMPTZ,  -- NULL if unused, populated on first use
    used_count INTEGER NOT NULL DEFAULT 0,  -- Number of times token was used
    max_uses INTEGER DEFAULT 1,  -- Single-use tokens by default (prevents replay attacks)

    -- Metadata
    correlation_id UUID,  -- Links to receipts/checkpoints
    request_context JSONB,  -- {ip_address, user_agent, request_id}

    -- Status
    is_active BOOLEAN NOT NULL DEFAULT true,

    -- Constraints
    CONSTRAINT check_expiry_under_60s CHECK (expires_at <= issued_at + INTERVAL '60 seconds'),
    CONSTRAINT check_max_uses_positive CHECK (max_uses > 0)
);

-- ============================================================================
-- INDEXES (Performance Optimization)
-- ============================================================================

-- Primary lookup: Validate token by token_id
CREATE INDEX idx_tokens_id_active ON capability_tokens (token_id, is_active, expires_at);

-- Suite/Office isolation
CREATE INDEX idx_tokens_suite_office ON capability_tokens (suite_id, office_id, issued_at DESC);

-- Tool/Action lookup (analytics)
CREATE INDEX idx_tokens_tool_action ON capability_tokens (tool_name, action_type, issued_at DESC);

-- Expiration cleanup
CREATE INDEX idx_tokens_expires ON capability_tokens (expires_at) WHERE is_active = true;

-- Revocation tracking
CREATE INDEX idx_tokens_revoked ON capability_tokens (revoked_at) WHERE revoked_at IS NOT NULL;

-- Correlation tracking (link to receipts)
CREATE INDEX idx_tokens_correlation ON capability_tokens (correlation_id);

-- ============================================================================
-- ROW-LEVEL SECURITY (RLS) - MULTI-TENANT ISOLATION
-- ============================================================================

-- Enable RLS on capability_tokens table
ALTER TABLE capability_tokens ENABLE ROW LEVEL SECURITY;

-- Policy: Tokens can only be accessed by their owning suite
CREATE POLICY tenant_isolation_tokens ON capability_tokens
  FOR ALL
  USING (suite_id = current_setting('app.current_suite_id', true)::uuid);

-- ============================================================================
-- TOKEN GENERATION FUNCTIONS
-- ============================================================================

-- Function: Generate HMAC signature for token
CREATE OR REPLACE FUNCTION generate_token_signature(
    p_token_id UUID,
    p_suite_id UUID,
    p_tool_name TEXT,
    p_expires_at TIMESTAMPTZ,
    p_secret_key TEXT  -- Retrieved from environment/vault (NEVER store in DB)
) RETURNS TEXT AS $$
BEGIN
    RETURN encode(
        hmac(
            p_token_id::text || '|' ||
            p_suite_id::text || '|' ||
            p_tool_name || '|' ||
            extract(epoch from p_expires_at)::text,
            p_secret_key,
            'sha256'
        ),
        'hex'
    );
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Function: Mint new capability token (server-side only)
CREATE OR REPLACE FUNCTION mint_capability_token(
    p_suite_id UUID,
    p_office_id UUID,
    p_tool_name TEXT,
    p_action_type TEXT,
    p_scopes TEXT[],
    p_secret_key TEXT,
    p_ttl_seconds INTEGER DEFAULT 60,  -- Default 60s, max 60s enforced by constraint
    p_correlation_id UUID DEFAULT NULL,
    p_request_context JSONB DEFAULT NULL
) RETURNS TABLE (
    token_id UUID,
    signature TEXT,
    expires_at TIMESTAMPTZ
) AS $$
DECLARE
    v_token_id UUID;
    v_expires_at TIMESTAMPTZ;
    v_signature TEXT;
BEGIN
    -- Generate token ID
    v_token_id := uuid_generate_v4();

    -- Calculate expiry (<60s enforced by constraint)
    v_expires_at := NOW() + (p_ttl_seconds || ' seconds')::INTERVAL;

    -- Generate signature
    v_signature := generate_token_signature(
        v_token_id,
        p_suite_id,
        p_tool_name,
        v_expires_at,
        p_secret_key
    );

    -- Insert token
    INSERT INTO capability_tokens (
        token_id,
        suite_id,
        office_id,
        tool_name,
        action_type,
        scopes,
        expires_at,
        signature,
        correlation_id,
        request_context
    ) VALUES (
        v_token_id,
        p_suite_id,
        p_office_id,
        p_tool_name,
        p_action_type,
        p_scopes,
        v_expires_at,
        v_signature,
        p_correlation_id,
        p_request_context
    );

    -- Return token details
    RETURN QUERY SELECT
        v_token_id,
        v_signature,
        v_expires_at;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- TOKEN VALIDATION FUNCTIONS
-- ============================================================================

-- Function: Validate capability token (server-side)
CREATE OR REPLACE FUNCTION validate_capability_token(
    p_token_id UUID,
    p_signature TEXT,
    p_secret_key TEXT
) RETURNS TABLE (
    is_valid BOOLEAN,
    reason_code TEXT,
    suite_id UUID,
    office_id UUID,
    tool_name TEXT,
    action_type TEXT,
    scopes TEXT[]
) AS $$
DECLARE
    v_token RECORD;
    v_expected_signature TEXT;
BEGIN
    -- Retrieve token
    SELECT * INTO v_token
    FROM capability_tokens
    WHERE token_id = p_token_id;

    -- Check if token exists
    IF NOT FOUND THEN
        RETURN QUERY SELECT
            false AS is_valid,
            'token_not_found'::TEXT AS reason_code,
            NULL::UUID, NULL::UUID, NULL::TEXT, NULL::TEXT, NULL::TEXT[];
        RETURN;
    END IF;

    -- Check if token is active
    IF NOT v_token.is_active THEN
        RETURN QUERY SELECT
            false AS is_valid,
            'token_inactive'::TEXT AS reason_code,
            NULL::UUID, NULL::UUID, NULL::TEXT, NULL::TEXT, NULL::TEXT[];
        RETURN;
    END IF;

    -- Check if token is revoked
    IF v_token.revoked_at IS NOT NULL THEN
        RETURN QUERY SELECT
            false AS is_valid,
            'token_revoked'::TEXT AS reason_code,
            NULL::UUID, NULL::UUID, NULL::TEXT, NULL::TEXT, NULL::TEXT[];
        RETURN;
    END IF;

    -- Check if token is expired
    IF v_token.expires_at < NOW() THEN
        RETURN QUERY SELECT
            false AS is_valid,
            'token_expired'::TEXT AS reason_code,
            NULL::UUID, NULL::UUID, NULL::TEXT, NULL::TEXT, NULL::TEXT[];
        RETURN;
    END IF;

    -- Verify signature
    v_expected_signature := generate_token_signature(
        v_token.token_id,
        v_token.suite_id,
        v_token.tool_name,
        v_token.expires_at,
        p_secret_key
    );

    IF v_expected_signature != p_signature THEN
        RETURN QUERY SELECT
            false AS is_valid,
            'signature_invalid'::TEXT AS reason_code,
            NULL::UUID, NULL::UUID, NULL::TEXT, NULL::TEXT, NULL::TEXT[];
        RETURN;
    END IF;

    -- Check max uses (prevent replay attacks)
    IF v_token.used_count >= v_token.max_uses THEN
        RETURN QUERY SELECT
            false AS is_valid,
            'max_uses_exceeded'::TEXT AS reason_code,
            NULL::UUID, NULL::UUID, NULL::TEXT, NULL::TEXT, NULL::TEXT[];
        RETURN;
    END IF;

    -- Token is valid - increment use count
    UPDATE capability_tokens
    SET used_count = used_count + 1,
        used_at = COALESCE(used_at, NOW())
    WHERE token_id = p_token_id;

    -- Return valid token
    RETURN QUERY SELECT
        true AS is_valid,
        'valid'::TEXT AS reason_code,
        v_token.suite_id,
        v_token.office_id,
        v_token.tool_name,
        v_token.action_type,
        v_token.scopes;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- TOKEN REVOCATION FUNCTIONS
-- ============================================================================

-- Function: Revoke specific token
CREATE OR REPLACE FUNCTION revoke_token(p_token_id UUID)
RETURNS BOOLEAN AS $$
BEGIN
    UPDATE capability_tokens
    SET revoked_at = NOW(),
        is_active = false
    WHERE token_id = p_token_id
      AND revoked_at IS NULL;

    RETURN FOUND;
END;
$$ LANGUAGE plpgsql;

-- Function: Revoke all tokens for suite (emergency kill switch)
CREATE OR REPLACE FUNCTION revoke_all_suite_tokens(p_suite_id UUID)
RETURNS INTEGER AS $$
DECLARE
    v_revoked_count INTEGER;
BEGIN
    UPDATE capability_tokens
    SET revoked_at = NOW(),
        is_active = false
    WHERE suite_id = p_suite_id
      AND revoked_at IS NULL;

    GET DIAGNOSTICS v_revoked_count = ROW_COUNT;
    RETURN v_revoked_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- CLEANUP FUNCTIONS
-- ============================================================================

-- Function: Delete expired tokens (run hourly via cron)
CREATE OR REPLACE FUNCTION cleanup_expired_tokens()
RETURNS INTEGER AS $$
DECLARE
    v_deleted_count INTEGER;
BEGIN
    DELETE FROM capability_tokens
    WHERE expires_at < NOW() - INTERVAL '1 hour';  -- Keep expired tokens for 1hr for debugging

    GET DIAGNOSTICS v_deleted_count = ROW_COUNT;
    RETURN v_deleted_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- ANALYTICS FUNCTIONS
-- ============================================================================

-- Function: Get token usage stats for suite
CREATE OR REPLACE FUNCTION get_token_usage_stats(p_suite_id UUID, p_hours INTEGER DEFAULT 24)
RETURNS TABLE (
    tool_name TEXT,
    action_type TEXT,
    total_issued INTEGER,
    total_used INTEGER,
    total_expired INTEGER,
    total_revoked INTEGER
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        t.tool_name,
        t.action_type,
        COUNT(*)::INTEGER AS total_issued,
        COUNT(t.used_at)::INTEGER AS total_used,
        COUNT(*) FILTER (WHERE t.expires_at < NOW())::INTEGER AS total_expired,
        COUNT(t.revoked_at)::INTEGER AS total_revoked
    FROM capability_tokens t
    WHERE t.suite_id = p_suite_id
      AND t.issued_at > NOW() - (p_hours || ' hours')::INTERVAL
    GROUP BY t.tool_name, t.action_type
    ORDER BY total_issued DESC;
END;
$$ LANGUAGE plpgsql STABLE;

-- ============================================================================
-- EXAMPLE USAGE
-- ============================================================================

-- Mint new capability token:
/*
SELECT * FROM mint_capability_token(
    p_suite_id := 'suite_abc123'::uuid,
    p_office_id := 'office_user1'::uuid,
    p_tool_name := 'stripe_api',
    p_action_type := 'invoice.send',
    p_scopes := ARRAY['invoice.write', 'email.send'],
    p_secret_key := 'your_secret_key_from_env_or_vault',
    p_ttl_seconds := 60,
    p_correlation_id := 'correlation_xyz789'::uuid
);
*/

-- Validate capability token:
/*
SELECT * FROM validate_capability_token(
    p_token_id := 'token_abc123'::uuid,
    p_signature := 'sha256_hmac_signature',
    p_secret_key := 'your_secret_key_from_env_or_vault'
);
*/

-- Revoke specific token:
/*
SELECT revoke_token('token_abc123'::uuid);
*/

-- Emergency: Revoke all tokens for suite:
/*
SELECT revoke_all_suite_tokens('suite_abc123'::uuid);
*/

-- Cleanup expired tokens (run hourly):
/*
SELECT cleanup_expired_tokens();
*/

-- Get token usage stats:
/*
SELECT * FROM get_token_usage_stats('suite_abc123'::uuid, 24);
*/

-- ============================================================================
-- DEPLOYMENT CHECKLIST
-- ============================================================================
-- [ ] Secret key stored in environment variable (NEVER in database)
-- [ ] RLS policies enabled (zero cross-tenant token access)
-- [ ] Cleanup cron job scheduled (hourly cleanup_expired_tokens())
-- [ ] Token validation integrated in ALL MCP tool calls
-- [ ] <60s expiry enforced (constraint check_expiry_under_60s)
-- [ ] Signature validation tested (reject forged tokens)
-- [ ] Revocation tested (revoked tokens rejected)
-- [ ] Max uses enforced (single-use tokens prevent replay attacks)
-- [ ] Server-side validation ONLY (never trust client-provided tokens)
-- ============================================================================

-- ============================================================================
-- SECURITY NOTES
-- ============================================================================
-- 1. SECRET KEY MANAGEMENT:
--    - Store secret key in environment variable or vault (e.g., AWS Secrets Manager)
--    - NEVER commit secret key to git
--    - Rotate secret key quarterly (update secret_key_version)
--
-- 2. TOKEN LIFETIME:
--    - MAXIMUM 60 seconds (enforced by constraint)
--    - Shorter is better (reduces replay attack window)
--
-- 3. SINGLE-USE TOKENS:
--    - Default max_uses = 1 (prevents replay attacks)
--    - Only increase for idempotent operations
--
-- 4. REVOCATION:
--    - Immediate revocation via revoke_token()
--    - Emergency kill switch via revoke_all_suite_tokens()
--
-- 5. VALIDATION:
--    - Server-side ONLY (never trust client)
--    - Verify signature, expiry, revocation, max uses
--    - Log all validation failures (security monitoring)
-- ============================================================================
