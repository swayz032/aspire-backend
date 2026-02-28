-- Migration 057: Add draft-first columns to approval_requests
-- Phase 3 W14: Clara Legal Production + Draft-First Authority Queue
--
-- New columns for draft-first pipeline:
--   execution_payload: Full params for resume execution (encrypted at rest in Phase 4)
--   draft_summary: Human-readable summary for Authority Queue display
--   assigned_agent: Which agent handles resume execution (e.g., "clara", "quinn")
--   execution_params_hash: SHA-256 of execution_params for binding verification

ALTER TABLE approval_requests
  ADD COLUMN IF NOT EXISTS execution_payload JSONB,
  ADD COLUMN IF NOT EXISTS draft_summary TEXT,
  ADD COLUMN IF NOT EXISTS assigned_agent TEXT,
  ADD COLUMN IF NOT EXISTS execution_params_hash TEXT;

-- Index for Authority Queue: pending approvals by tenant, ordered by creation
-- (complements existing idx_approvals_tenant_status)
CREATE INDEX IF NOT EXISTS idx_approvals_draft_queue
  ON approval_requests (tenant_id, status, created_at DESC)
  WHERE status = 'pending';
