-- Fix provider_call_log NOT NULL constraints that block logger writes
-- and create missing token_usage_log table

-- 1. Make run_id and params_hash nullable (observability, not required)
ALTER TABLE public.provider_call_log ALTER COLUMN run_id DROP NOT NULL;
ALTER TABLE public.provider_call_log ALTER COLUMN params_hash DROP NOT NULL;
ALTER TABLE public.provider_call_log ALTER COLUMN params_hash SET DEFAULT '';

-- 2. Create token_usage_log (referenced by openai_client.py but never created)
CREATE TABLE IF NOT EXISTS public.token_usage_log (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id text,
    agent_id text NOT NULL DEFAULT 'orchestrator',
    model text NOT NULL,
    profile text NOT NULL DEFAULT 'unknown',
    prompt_tokens integer NOT NULL DEFAULT 0,
    completion_tokens integer NOT NULL DEFAULT 0,
    total_tokens integer NOT NULL DEFAULT 0,
    cache_hit boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_token_usage_log_created ON public.token_usage_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_token_usage_log_model ON public.token_usage_log(model);

ALTER TABLE public.token_usage_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "token_usage_log_service_role_all"
    ON public.token_usage_log FOR ALL
    USING (auth.role() = 'service_role');
