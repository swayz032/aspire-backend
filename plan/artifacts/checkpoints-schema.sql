-- ============================================================================
-- ASPIRE CHECKPOINTS TABLE - LANGGRAPH STATE PERSISTENCE
-- ============================================================================
--
-- Purpose: Store LangGraph state machine checkpoints for resumable workflows
-- Phase Introduced: Phase 1 (Core Orchestrator)
--
-- Key Requirements:
-- - Persistent state storage for LangGraph (event sourcing)
-- - Support for workflow pause/resume (approval gates, async operations)
-- - Correlation with receipts (same correlation_id)
-- - Multi-tenant isolation (RLS enabled)
-- - Cleanup policy (expire old checkpoints after workflow completion)
--
-- Related Files:
-- - plan/Aspire-Production-Roadmap.md (Phase 1 implementation)
-- - plan/gates/gate-07-rls-isolation.md (RLS policies)
-- - backend/orchestrator/brain.py (LangGraph implementation)
--
-- ============================================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- MAIN CHECKPOINTS TABLE
-- ============================================================================

CREATE TABLE checkpoints (
    -- Identity
    checkpoint_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    thread_id UUID NOT NULL,  -- LangGraph thread identifier (groups workflow execution)
    correlation_id UUID NOT NULL,  -- Correlates with receipts table

    -- Tenant Isolation
    suite_id UUID NOT NULL,   -- Multi-tenant isolation (CRITICAL for RLS)
    office_id UUID NOT NULL,  -- Individual human within suite

    -- State Machine Details
    checkpoint_namespace TEXT NOT NULL DEFAULT 'default',  -- LangGraph namespace
    parent_checkpoint_id UUID,  -- Previous checkpoint (for state history)

    -- State Snapshot (JSONB for flexibility)
    state_snapshot JSONB NOT NULL,  -- Full LangGraph state at this checkpoint
    metadata JSONB,  -- Additional metadata (node name, branch info, etc.)

    -- Workflow Context
    current_node TEXT NOT NULL,  -- Which node in state machine (e.g., "wait_approval", "execute")
    workflow_status TEXT NOT NULL CHECK (workflow_status IN ('pending', 'in_progress', 'paused', 'completed', 'failed')),

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,  -- Auto-cleanup after expiration (7 days default)

    -- Execution Context
    pending_action TEXT,  -- What action is waiting (e.g., "user_approval", "api_call")
    pending_inputs JSONB,  -- Input parameters for pending action

    -- Foreign Key Constraints
    CONSTRAINT fk_parent_checkpoint FOREIGN KEY (parent_checkpoint_id) REFERENCES checkpoints(checkpoint_id) ON DELETE SET NULL
);

-- ============================================================================
-- INDEXES (Performance Optimization)
-- ============================================================================

-- Primary lookup: Find checkpoints for specific thread
CREATE INDEX idx_checkpoints_thread ON checkpoints (thread_id, created_at DESC);

-- Correlation: Link checkpoints to receipts
CREATE INDEX idx_checkpoints_correlation ON checkpoints (correlation_id);

-- Suite/Office lookup
CREATE INDEX idx_checkpoints_suite_office ON checkpoints (suite_id, office_id, created_at DESC);

-- Workflow status filtering (find paused workflows)
CREATE INDEX idx_checkpoints_status ON checkpoints (workflow_status, created_at DESC);

-- Expiration cleanup (for background jobs)
CREATE INDEX idx_checkpoints_expires ON checkpoints (expires_at) WHERE expires_at IS NOT NULL;

-- Current node analysis (which nodes are slowest?)
CREATE INDEX idx_checkpoints_current_node ON checkpoints (current_node, created_at DESC);

-- ============================================================================
-- ROW-LEVEL SECURITY (RLS) - MULTI-TENANT ISOLATION
-- ============================================================================

-- Enable RLS on checkpoints table
ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY;

-- Policy: Users can only see checkpoints from their own suite
CREATE POLICY tenant_isolation_checkpoints ON checkpoints
  FOR ALL
  USING (suite_id = current_setting('app.current_suite_id', true)::uuid);

-- ============================================================================
-- AUTO-UPDATE TIMESTAMP TRIGGER
-- ============================================================================

CREATE OR REPLACE FUNCTION trigger_update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER checkpoints_update_timestamp
    BEFORE UPDATE ON checkpoints
    FOR EACH ROW
    EXECUTE FUNCTION trigger_update_timestamp();

-- ============================================================================
-- AUTO-SET EXPIRATION TRIGGER (7 days default)
-- ============================================================================

CREATE OR REPLACE FUNCTION trigger_set_expiration()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.expires_at IS NULL THEN
        -- Default: checkpoints expire 7 days after creation
        NEW.expires_at := NEW.created_at + INTERVAL '7 days';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER checkpoints_set_expiration
    BEFORE INSERT ON checkpoints
    FOR EACH ROW
    EXECUTE FUNCTION trigger_set_expiration();

-- ============================================================================
-- CLEANUP FUNCTIONS
-- ============================================================================

-- Function: Delete expired checkpoints (run daily via cron/background job)
CREATE OR REPLACE FUNCTION cleanup_expired_checkpoints()
RETURNS TABLE (
    deleted_count INTEGER
) AS $$
DECLARE
    v_deleted INTEGER;
BEGIN
    DELETE FROM checkpoints
    WHERE expires_at < NOW()
      AND workflow_status IN ('completed', 'failed');

    GET DIAGNOSTICS v_deleted = ROW_COUNT;

    RETURN QUERY SELECT v_deleted;
END;
$$ LANGUAGE plpgsql;

-- Function: Force expire checkpoints for completed workflows
CREATE OR REPLACE FUNCTION expire_completed_workflows(p_thread_id UUID)
RETURNS VOID AS $$
BEGIN
    UPDATE checkpoints
    SET expires_at = NOW()
    WHERE thread_id = p_thread_id
      AND workflow_status = 'completed';
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- QUERY HELPER FUNCTIONS
-- ============================================================================

-- Function: Get latest checkpoint for thread
CREATE OR REPLACE FUNCTION get_latest_checkpoint(p_thread_id UUID)
RETURNS TABLE (
    checkpoint_id UUID,
    current_node TEXT,
    workflow_status TEXT,
    state_snapshot JSONB,
    created_at TIMESTAMPTZ
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        c.checkpoint_id,
        c.current_node,
        c.workflow_status,
        c.state_snapshot,
        c.created_at
    FROM checkpoints c
    WHERE c.thread_id = p_thread_id
    ORDER BY c.created_at DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE;

-- Function: Get all paused workflows (for resumption)
CREATE OR REPLACE FUNCTION get_paused_workflows(p_suite_id UUID)
RETURNS TABLE (
    thread_id UUID,
    correlation_id UUID,
    current_node TEXT,
    pending_action TEXT,
    created_at TIMESTAMPTZ
) AS $$
BEGIN
    RETURN QUERY
    SELECT DISTINCT ON (c.thread_id)
        c.thread_id,
        c.correlation_id,
        c.current_node,
        c.pending_action,
        c.created_at
    FROM checkpoints c
    WHERE c.suite_id = p_suite_id
      AND c.workflow_status = 'paused'
    ORDER BY c.thread_id, c.created_at DESC;
END;
$$ LANGUAGE plpgsql STABLE;

-- Function: Get checkpoint history for thread (debugging)
CREATE OR REPLACE FUNCTION get_checkpoint_history(p_thread_id UUID)
RETURNS TABLE (
    checkpoint_id UUID,
    current_node TEXT,
    workflow_status TEXT,
    created_at TIMESTAMPTZ,
    parent_checkpoint_id UUID
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        c.checkpoint_id,
        c.current_node,
        c.workflow_status,
        c.created_at,
        c.parent_checkpoint_id
    FROM checkpoints c
    WHERE c.thread_id = p_thread_id
    ORDER BY c.created_at ASC;
END;
$$ LANGUAGE plpgsql STABLE;

-- ============================================================================
-- EXAMPLE USAGE
-- ============================================================================

-- Insert checkpoint example:
/*
INSERT INTO checkpoints (
    thread_id,
    correlation_id,
    suite_id,
    office_id,
    current_node,
    workflow_status,
    state_snapshot,
    pending_action,
    pending_inputs
) VALUES (
    uuid_generate_v4(),
    'correlation_abc123'::uuid,
    'suite_xyz789'::uuid,
    'office_user1'::uuid,
    'wait_approval',
    'paused',
    '{"intent": "Send invoice to customer", "invoice_amount": 5000, "customer_id": "cust_abc"}'::jsonb,
    'user_approval',
    '{"invoice_id": "inv_123", "recipient": "<EMAIL_REDACTED>"}'::jsonb
);
*/

-- Get latest checkpoint for thread:
/*
SELECT * FROM get_latest_checkpoint('thread_abc123'::uuid);
*/

-- Get all paused workflows for suite:
/*
SELECT * FROM get_paused_workflows('suite_xyz789'::uuid);
*/

-- Clean up expired checkpoints (run daily):
/*
SELECT * FROM cleanup_expired_checkpoints();
*/

-- Get checkpoint history for debugging:
/*
SELECT * FROM get_checkpoint_history('thread_abc123'::uuid);
*/

-- ============================================================================
-- DEPLOYMENT CHECKLIST
-- ============================================================================
-- [ ] Database role has permissions (SELECT, INSERT, UPDATE, DELETE on checkpoints)
-- [ ] Current suite_id context set before EVERY query:
--     SET LOCAL app.current_suite_id = '<suite_id_from_jwt>';
-- [ ] RLS policies tested (zero cross-tenant leakage)
-- [ ] Cleanup cron job scheduled (daily cleanup_expired_checkpoints())
-- [ ] LangGraph integration tested (checkpoint save/restore)
-- [ ] Paused workflow resumption tested
-- ============================================================================

-- ============================================================================
-- RELATED TABLES
-- ============================================================================
-- receipts: Links checkpoints to audit trail (same correlation_id)
-- suites: References suite_id for multi-tenant isolation
-- offices: References office_id for individual human tracking
-- ============================================================================
