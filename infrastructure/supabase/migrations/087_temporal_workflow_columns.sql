-- Migration 087: Add Temporal correlation fields to workflow_executions
-- Part of Temporal + LangGraph integration (Phase 2)
-- Backwards-compatible — all new columns are nullable

-- Add Temporal-specific columns
ALTER TABLE public.workflow_executions
    ADD COLUMN IF NOT EXISTS temporal_namespace TEXT,
    ADD COLUMN IF NOT EXISTS temporal_workflow_id TEXT,
    ADD COLUMN IF NOT EXISTS temporal_run_id TEXT,
    ADD COLUMN IF NOT EXISTS parent_workflow_id TEXT,
    ADD COLUMN IF NOT EXISTS workflow_kind TEXT DEFAULT 'intent',
    ADD COLUMN IF NOT EXISTS current_wait_type TEXT,
    ADD COLUMN IF NOT EXISTS current_agent TEXT,
    ADD COLUMN IF NOT EXISTS thread_id TEXT,
    ADD COLUMN IF NOT EXISTS approval_id TEXT,
    ADD COLUMN IF NOT EXISTS outbox_job_id TEXT,
    ADD COLUMN IF NOT EXISTS a2a_task_id TEXT,
    ADD COLUMN IF NOT EXISTS latest_response JSONB DEFAULT '{}'::JSONB,
    ADD COLUMN IF NOT EXISTS search_labels JSONB DEFAULT '{}'::JSONB;

-- Index for Temporal ID lookups
CREATE INDEX IF NOT EXISTS idx_we_temporal_workflow_id
    ON public.workflow_executions(temporal_workflow_id)
    WHERE temporal_workflow_id IS NOT NULL;

-- Index for parent/child joins
CREATE INDEX IF NOT EXISTS idx_we_parent_workflow_id
    ON public.workflow_executions(parent_workflow_id)
    WHERE parent_workflow_id IS NOT NULL;

-- Index for thread correlation
CREATE INDEX IF NOT EXISTS idx_we_thread_id
    ON public.workflow_executions(thread_id)
    WHERE thread_id IS NOT NULL;

-- Index for approval correlation
CREATE INDEX IF NOT EXISTS idx_we_approval_id
    ON public.workflow_executions(approval_id)
    WHERE approval_id IS NOT NULL;

-- workflow_kind check constraint
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_workflow_kind'
    ) THEN
        ALTER TABLE public.workflow_executions
            ADD CONSTRAINT chk_workflow_kind
            CHECK (workflow_kind IN (
                'intent', 'approval', 'outbox_execution', 'callback',
                'agent_fanout', 'scheduled', 'retry'
            ));
    END IF;
END $$;

-- current_wait_type check constraint
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_current_wait_type'
    ) THEN
        ALTER TABLE public.workflow_executions
            ADD CONSTRAINT chk_current_wait_type
            CHECK (current_wait_type IS NULL OR current_wait_type IN (
                'approval', 'presence', 'callback', 'timer',
                'activity_retry', 'child_workflow'
            ));
    END IF;
END $$;

-- Ensure RLS is enabled (Law #6: tenant isolation)
-- workflow_executions should already have RLS; this is a safety net
ALTER TABLE public.workflow_executions ENABLE ROW LEVEL SECURITY;

-- RPC function for upsert (used by sync_workflow_execution activity)
CREATE OR REPLACE FUNCTION public.upsert_workflow_execution(
    p_workflow_id TEXT,
    p_temporal_run_id TEXT DEFAULT NULL,
    p_suite_id TEXT DEFAULT NULL,
    p_office_id TEXT DEFAULT NULL,
    p_correlation_id TEXT DEFAULT NULL,
    p_status TEXT DEFAULT NULL,
    p_workflow_kind TEXT DEFAULT 'intent',
    p_current_wait_type TEXT DEFAULT NULL,
    p_current_agent TEXT DEFAULT NULL,
    p_thread_id TEXT DEFAULT NULL,
    p_approval_id TEXT DEFAULT NULL,
    p_outbox_job_id TEXT DEFAULT NULL,
    p_parent_workflow_id TEXT DEFAULT NULL,
    p_latest_response JSONB DEFAULT '{}'::JSONB,
    p_updated_at TEXT DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    UPDATE public.workflow_executions
    SET
        temporal_run_id = COALESCE(p_temporal_run_id, temporal_run_id),
        status = COALESCE(p_status, status),
        workflow_kind = COALESCE(p_workflow_kind, workflow_kind),
        current_wait_type = p_current_wait_type,
        current_agent = COALESCE(p_current_agent, current_agent),
        thread_id = COALESCE(p_thread_id, thread_id),
        approval_id = COALESCE(p_approval_id, approval_id),
        outbox_job_id = COALESCE(p_outbox_job_id, outbox_job_id),
        parent_workflow_id = COALESCE(p_parent_workflow_id, parent_workflow_id),
        latest_response = COALESCE(p_latest_response, latest_response),
        updated_at = NOW()
    WHERE temporal_workflow_id = p_workflow_id
        AND suite_id = p_suite_id;

    IF NOT FOUND THEN
        INSERT INTO public.workflow_executions (
            temporal_workflow_id, temporal_run_id, suite_id, office_id,
            correlation_id, status, workflow_kind, current_wait_type,
            current_agent, thread_id, approval_id, outbox_job_id,
            parent_workflow_id, latest_response
        ) VALUES (
            p_workflow_id, p_temporal_run_id, p_suite_id, p_office_id,
            p_correlation_id, p_status, p_workflow_kind, p_current_wait_type,
            p_current_agent, p_thread_id, p_approval_id, p_outbox_job_id,
            p_parent_workflow_id, p_latest_response
        );
    END IF;
END;
$$;
