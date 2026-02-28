-- ============================================================================
-- ASPIRE RECEIPTS TABLE - IMMUTABLE AUDIT TRAIL
-- ============================================================================
--
-- Purpose: Append-only ledger for ALL Aspire actions (Aspire Law #2)
-- Gate: Gate 6 - Receipts Immutable (CRITICAL)
-- Phase Introduced: Phase 1 (Core Orchestrator)
--
-- Key Requirements:
-- - NO UPDATE/DELETE privileges (immutable by design)
-- - Hash-chained for integrity verification
-- - PII redacted before insertion (Presidio DLP integration)
-- - 100% action coverage (no exceptions)
-- - Row-Level Security (RLS) for multi-tenant isolation
--
-- Related Files:
-- - plan/gates/gate-06-receipts-immutable.md (full specification)
-- - plan/gates/gate-07-rls-isolation.md (RLS policies)
-- - plan/gates/gate-08-replay-demo.md (deterministic replay)
--
-- ============================================================================

-- Enable UUID extension (required for receipt_id generation)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Enable pgcrypto for hash generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================================
-- MAIN RECEIPTS TABLE
-- ============================================================================

CREATE TABLE receipts (
    -- Identity
    receipt_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    correlation_id UUID NOT NULL,  -- Request tracing (groups related receipts)

    -- Tenant Isolation (Suite = Organization, Office = Individual Human)
    suite_id UUID NOT NULL,   -- Multi-tenant isolation (CRITICAL for RLS)
    office_id UUID NOT NULL,  -- Individual human within suite

    -- Action Details
    action_type TEXT NOT NULL,  -- e.g., "stripe.invoice.send", "email.draft", "calendar.create"
    risk_tier TEXT NOT NULL CHECK (risk_tier IN ('green', 'yellow', 'red')),

    -- Tool & Authorization
    tool_used TEXT NOT NULL,  -- e.g., "stripe_api", "gmail_api", "calendar_api"
    capability_token_id UUID NOT NULL,  -- Authorization proof (short-lived token)

    -- Timestamps (all required)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at TIMESTAMPTZ,  -- NULL if autonomous (green tier), populated if yellow/red
    executed_at TIMESTAMPTZ NOT NULL,

    -- Approval Evidence (who/when/what approved)
    approval_evidence JSONB,  -- {approver_id, approval_method: "voice"|"video"|"text", approval_timestamp, video_proof_url}

    -- Outcome
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'denied', 'failed')),
    reason_code TEXT,  -- Why denied/failed (e.g., "user_denied", "api_timeout", "insufficient_permission")

    -- Data (PII REDACTED before insertion via Presidio DLP)
    redacted_inputs JSONB NOT NULL,   -- Action parameters (PII removed)
    redacted_outputs JSONB,            -- Action results (PII removed)

    -- Hash Chain (integrity verification)
    previous_receipt_hash TEXT,  -- SHA-256 hash of previous receipt (NULL for first receipt in suite)
    receipt_hash TEXT NOT NULL   -- SHA-256(receipt_id || action_type || outcome || executed_at || redacted_inputs || previous_receipt_hash)
);

-- ============================================================================
-- INDEXES (Performance Optimization)
-- ============================================================================

-- Primary lookup: Find receipts for specific suite/office
CREATE INDEX idx_receipts_suite_office ON receipts (suite_id, office_id, created_at DESC);

-- Correlation: Group related receipts
CREATE INDEX idx_receipts_correlation ON receipts (correlation_id);

-- Action type analysis
CREATE INDEX idx_receipts_action_type ON receipts (action_type, created_at DESC);

-- Outcome filtering (failed/denied receipts)
CREATE INDEX idx_receipts_outcome ON receipts (outcome, created_at DESC);

-- Approval tracking
CREATE INDEX idx_receipts_approval ON receipts (approved_at) WHERE approved_at IS NOT NULL;

-- Hash chain verification (for replay/audit)
CREATE INDEX idx_receipts_hash_chain ON receipts (suite_id, created_at ASC);

-- ============================================================================
-- ROW-LEVEL SECURITY (RLS) - CRITICAL FOR MULTI-TENANT ISOLATION
-- ============================================================================
-- Gate 7: RLS Isolation - Zero cross-tenant data leakage
-- ============================================================================

-- Enable RLS on receipts table
ALTER TABLE receipts ENABLE ROW LEVEL SECURITY;

-- Policy: Users can only see receipts from their own suite
CREATE POLICY tenant_isolation_receipts ON receipts
  FOR ALL  -- Applies to SELECT, INSERT (UPDATE/DELETE revoked below)
  USING (suite_id = current_setting('app.current_suite_id', true)::uuid);

-- ============================================================================
-- IMMUTABILITY ENFORCEMENT (Aspire Law #2: No Action Without Receipt)
-- ============================================================================
-- Gate 6: Receipts Immutable - NO UPDATE/DELETE privileges
-- ============================================================================

-- Revoke UPDATE, DELETE, TRUNCATE privileges (append-only)
-- NOTE: Replace 'app_role' with your application database role
REVOKE UPDATE, DELETE, TRUNCATE ON receipts FROM app_role;

-- Grant only SELECT and INSERT
GRANT SELECT, INSERT ON receipts TO app_role;

-- ============================================================================
-- HASH CHAIN FUNCTION (Deterministic Integrity Verification)
-- ============================================================================
-- Gate 8: Replay Demo - Deterministic state reconstruction
-- ============================================================================

-- Function: Calculate receipt hash (SHA-256)
CREATE OR REPLACE FUNCTION calculate_receipt_hash(
    p_receipt_id UUID,
    p_action_type TEXT,
    p_outcome TEXT,
    p_executed_at TIMESTAMPTZ,
    p_redacted_inputs JSONB,
    p_previous_hash TEXT
) RETURNS TEXT AS $$
BEGIN
    RETURN encode(
        digest(
            p_receipt_id::text ||
            p_action_type ||
            p_outcome ||
            p_executed_at::text ||
            p_redacted_inputs::text ||
            COALESCE(p_previous_hash, ''),
            'sha256'
        ),
        'hex'
    );
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Function: Get previous receipt hash for suite (for hash chaining)
CREATE OR REPLACE FUNCTION get_previous_receipt_hash(p_suite_id UUID)
RETURNS TEXT AS $$
DECLARE
    v_previous_hash TEXT;
BEGIN
    SELECT receipt_hash INTO v_previous_hash
    FROM receipts
    WHERE suite_id = p_suite_id
    ORDER BY created_at DESC
    LIMIT 1;

    RETURN v_previous_hash;
END;
$$ LANGUAGE plpgsql STABLE;

-- ============================================================================
-- TRIGGER: Auto-calculate receipt hash on INSERT
-- ============================================================================

CREATE OR REPLACE FUNCTION trigger_calculate_receipt_hash()
RETURNS TRIGGER AS $$
BEGIN
    -- Get previous receipt hash for this suite
    NEW.previous_receipt_hash := get_previous_receipt_hash(NEW.suite_id);

    -- Calculate hash for this receipt
    NEW.receipt_hash := calculate_receipt_hash(
        NEW.receipt_id,
        NEW.action_type,
        NEW.outcome,
        NEW.executed_at,
        NEW.redacted_inputs,
        NEW.previous_receipt_hash
    );

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER receipts_calculate_hash
    BEFORE INSERT ON receipts
    FOR EACH ROW
    EXECUTE FUNCTION trigger_calculate_receipt_hash();

-- ============================================================================
-- VERIFICATION FUNCTIONS (Testing & Auditing)
-- ============================================================================

-- Function: Verify hash chain integrity for entire suite
CREATE OR REPLACE FUNCTION verify_hash_chain(p_suite_id UUID)
RETURNS TABLE (
    is_valid BOOLEAN,
    total_receipts INTEGER,
    invalid_receipts INTEGER,
    first_invalid_receipt_id UUID
) AS $$
DECLARE
    v_total INTEGER := 0;
    v_invalid INTEGER := 0;
    v_first_invalid UUID := NULL;
    v_receipt RECORD;
    v_calculated_hash TEXT;
BEGIN
    FOR v_receipt IN
        SELECT * FROM receipts
        WHERE suite_id = p_suite_id
        ORDER BY created_at ASC
    LOOP
        v_total := v_total + 1;

        -- Calculate expected hash
        v_calculated_hash := calculate_receipt_hash(
            v_receipt.receipt_id,
            v_receipt.action_type,
            v_receipt.outcome,
            v_receipt.executed_at,
            v_receipt.redacted_inputs,
            v_receipt.previous_receipt_hash
        );

        -- Compare with stored hash
        IF v_calculated_hash != v_receipt.receipt_hash THEN
            v_invalid := v_invalid + 1;
            IF v_first_invalid IS NULL THEN
                v_first_invalid := v_receipt.receipt_id;
            END IF;
        END IF;
    END LOOP;

    RETURN QUERY SELECT
        (v_invalid = 0) AS is_valid,
        v_total AS total_receipts,
        v_invalid AS invalid_receipts,
        v_first_invalid AS first_invalid_receipt_id;
END;
$$ LANGUAGE plpgsql STABLE;

-- ============================================================================
-- EXAMPLE USAGE
-- ============================================================================

-- Insert receipt example (hash auto-calculated via trigger):
/*
INSERT INTO receipts (
    correlation_id,
    suite_id,
    office_id,
    action_type,
    risk_tier,
    tool_used,
    capability_token_id,
    executed_at,
    outcome,
    redacted_inputs,
    redacted_outputs
) VALUES (
    uuid_generate_v4(),
    'suite_abc123'::uuid,
    'office_user1'::uuid,
    'stripe.invoice.send',
    'yellow',
    'stripe_api',
    'token_xyz789'::uuid,
    NOW(),
    'success',
    '{"amount": 5000, "customer_email": "<EMAIL_REDACTED>", "invoice_id": "inv_123"}'::jsonb,
    '{"stripe_invoice_id": "in_abc123def456", "status": "sent"}'::jsonb
);
*/

-- Verify hash chain integrity:
/*
SELECT * FROM verify_hash_chain('suite_abc123'::uuid);
*/

-- Query receipts for specific action type:
/*
SELECT
    receipt_id,
    action_type,
    outcome,
    created_at
FROM receipts
WHERE action_type = 'stripe.invoice.send'
ORDER BY created_at DESC
LIMIT 10;
*/

-- ============================================================================
-- DEPLOYMENT CHECKLIST
-- ============================================================================
-- [ ] Database role created (replace 'app_role' above)
-- [ ] Current suite_id context set before EVERY query:
--     SET LOCAL app.current_suite_id = '<suite_id_from_jwt>';
-- [ ] RLS policies tested (evil tests pass - zero cross-tenant leakage)
-- [ ] Hash chain verification tested (100% valid)
-- [ ] PII redaction integrated (Presidio DLP active before INSERT)
-- [ ] Receipt coverage verified (100% of actions generate receipts)
-- ============================================================================
