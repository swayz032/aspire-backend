-- Add execution payload storage for draft-first pattern
ALTER TABLE approval_requests ADD COLUMN IF NOT EXISTS execution_payload JSONB;
ALTER TABLE approval_requests ADD COLUMN IF NOT EXISTS draft_summary TEXT;
ALTER TABLE approval_requests ADD COLUMN IF NOT EXISTS assigned_agent TEXT;
ALTER TABLE approval_requests ADD COLUMN IF NOT EXISTS execution_params_hash TEXT;

-- Index for resume lookups
CREATE INDEX IF NOT EXISTS idx_approval_requests_status_tenant
  ON approval_requests (status, tenant_id) WHERE status = 'pending';

COMMENT ON COLUMN approval_requests.execution_payload IS 'Full tool params for resume execution after approval';
COMMENT ON COLUMN approval_requests.draft_summary IS 'Human-readable summary for Authority Queue display';
COMMENT ON COLUMN approval_requests.assigned_agent IS 'Agent that will execute on approval (e.g. quinn, finn)';
COMMENT ON COLUMN approval_requests.execution_params_hash IS 'SHA-256 of canonical JSON payload for approve-then-swap defense';
