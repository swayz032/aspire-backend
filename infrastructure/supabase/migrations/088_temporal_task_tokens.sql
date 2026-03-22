-- Migration 088: temporal_task_tokens — Enhancement #8 (Async Activity Completion)
-- Stores Temporal task tokens for webhook-based async activity completion.
-- When a provider webhook fires, the handler looks up the task token here
-- and calls temporal_client.get_async_activity_handle(task_token).complete().

CREATE TABLE IF NOT EXISTS public.temporal_task_tokens (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    provider TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    task_token TEXT NOT NULL,
    suite_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    expires_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '72 hours') NOT NULL,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_task_token_ref UNIQUE (provider, ref_id)
);

-- Index for webhook lookup (provider + ref_id)
CREATE INDEX IF NOT EXISTS idx_task_tokens_lookup
    ON public.temporal_task_tokens(provider, ref_id)
    WHERE completed_at IS NULL;

-- Index for cleanup of expired tokens
CREATE INDEX IF NOT EXISTS idx_task_tokens_expires
    ON public.temporal_task_tokens(expires_at)
    WHERE completed_at IS NULL;

-- RLS: tenant isolation
ALTER TABLE public.temporal_task_tokens ENABLE ROW LEVEL SECURITY;

-- Service role can do everything (used by webhook handlers)
CREATE POLICY temporal_task_tokens_service_all
    ON public.temporal_task_tokens
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Authenticated users can only see their own suite's tokens
CREATE POLICY temporal_task_tokens_tenant_select
    ON public.temporal_task_tokens
    FOR SELECT
    TO authenticated
    USING (suite_id = (current_setting('app.current_suite_id', true))::TEXT);

-- Cleanup function for expired tokens (called by pg_cron or scheduled workflow)
CREATE OR REPLACE FUNCTION public.cleanup_expired_task_tokens()
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM public.temporal_task_tokens
    WHERE expires_at < NOW()
    RETURNING 1 INTO deleted_count;

    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$;

-- Comment for documentation
COMMENT ON TABLE public.temporal_task_tokens IS
    'Temporal async activity completion tokens. Enhancement #8: webhook providers save task_token here; webhook handler completes activity externally.';
COMMENT ON FUNCTION public.cleanup_expired_task_tokens IS
    'Removes expired task tokens. Schedule via pg_cron: SELECT cron.schedule(''cleanup-task-tokens'', ''0 3 * * *'', $$SELECT public.cleanup_expired_task_tokens()$$);';
