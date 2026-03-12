-- Migration 080: Token Usage Log (Phase 5E)
-- Tracks per-tenant, per-agent LLM token consumption for cost attribution.
-- Used by the admin cost report endpoint and Prometheus metrics.

CREATE TABLE IF NOT EXISTS public.token_usage_log (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    suite_id uuid NOT NULL,
    agent_id text NOT NULL,
    model text NOT NULL,
    profile text NOT NULL DEFAULT 'unknown',
    prompt_tokens int NOT NULL DEFAULT 0,
    completion_tokens int NOT NULL DEFAULT 0,
    total_tokens int NOT NULL DEFAULT 0,
    cache_hit boolean DEFAULT false,
    created_at timestamptz DEFAULT now()
);

-- Query by tenant + time range (admin cost report)
CREATE INDEX idx_token_usage_suite_created
    ON public.token_usage_log (suite_id, created_at DESC);

-- Query by model for aggregate cost analysis
CREATE INDEX idx_token_usage_model
    ON public.token_usage_log (model, created_at DESC);

-- RLS: Tenant isolation (Law #6)
ALTER TABLE public.token_usage_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.token_usage_log FORCE ROW LEVEL SECURITY;

-- Tenants see only their own usage
CREATE POLICY token_usage_tenant_select ON public.token_usage_log
    FOR SELECT USING (
        suite_id IN (
            SELECT tm.suite_id FROM tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
    );

-- Service role bypass for backend writes
CREATE POLICY token_usage_service_role ON public.token_usage_log
    FOR ALL USING (
        current_setting('role', true) = 'service_role'
    );

-- Tenant insert (scoped to own suites)
CREATE POLICY token_usage_insert ON public.token_usage_log
    FOR INSERT WITH CHECK (
        suite_id IN (
            SELECT tm.suite_id FROM tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
    );
