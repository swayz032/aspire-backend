-- Migration 083: workflow_executions table for Temporal durable execution tracking
-- Phase 6 of observability plan
--
-- Stores workflow execution state synced from Temporal.
-- Admin portal reads this table (never opens Temporal Web UI).

-- =============================================================================
-- 1. Create workflow_executions table
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.workflow_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id TEXT NOT NULL,
    run_id TEXT,
    tenant_id TEXT NOT NULL REFERENCES public.tenants(tenant_id),
    correlation_id TEXT,

    -- Workflow metadata
    workflow_type TEXT NOT NULL DEFAULT 'intent',  -- intent, scheduled, retry
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'cancelled', 'timed_out')),

    -- Timing
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    duration_ms NUMERIC,

    -- Error info (populated on failure)
    error_type TEXT,
    error_message TEXT,

    -- Context
    input_summary JSONB DEFAULT '{}'::JSONB,
    output_summary JSONB DEFAULT '{}'::JSONB,
    metadata JSONB DEFAULT '{}'::JSONB,

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- 2. Indexes
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_workflow_executions_tenant
    ON public.workflow_executions(tenant_id);

CREATE INDEX IF NOT EXISTS idx_workflow_executions_status
    ON public.workflow_executions(status)
    WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_workflow_executions_correlation
    ON public.workflow_executions(correlation_id)
    WHERE correlation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_workflow_executions_workflow_id
    ON public.workflow_executions(workflow_id);

CREATE INDEX IF NOT EXISTS idx_workflow_executions_started
    ON public.workflow_executions(started_at DESC);

-- =============================================================================
-- 3. RLS Policies
-- =============================================================================

ALTER TABLE public.workflow_executions ENABLE ROW LEVEL SECURITY;

-- Admin/authenticated users can read their tenant's workflows
CREATE POLICY workflow_executions_select_member
    ON public.workflow_executions FOR SELECT
    TO authenticated
    USING (app.is_member(tenant_id));

-- Service role can do everything (backend writes)
CREATE POLICY workflow_executions_all_service
    ON public.workflow_executions FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- =============================================================================
-- 4. Updated_at trigger
-- =============================================================================

CREATE OR REPLACE FUNCTION update_workflow_executions_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_workflow_executions_updated_at
    BEFORE UPDATE ON public.workflow_executions
    FOR EACH ROW
    EXECUTE FUNCTION update_workflow_executions_updated_at();

-- =============================================================================
-- 5. Enable Realtime (admin portal live updates)
-- =============================================================================

ALTER PUBLICATION supabase_realtime ADD TABLE public.workflow_executions;
