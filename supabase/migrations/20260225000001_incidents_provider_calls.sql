-- Wave 2C: Incidents + Provider Call Log tables for Admin Ops
-- Part of Backend Sync & Reliability enterprise plan

-- =============================================================================
-- incidents — stores system incidents from exception handler + monitors
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.incidents (
    incident_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id TEXT NOT NULL DEFAULT 'system',
    state TEXT NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'investigating', 'resolved', 'closed')),
    severity TEXT NOT NULL DEFAULT 'medium'
        CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    title TEXT NOT NULL,
    correlation_id TEXT,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    timeline JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_pack JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for admin queries
CREATE INDEX IF NOT EXISTS idx_incidents_state ON public.incidents (state);
CREATE INDEX IF NOT EXISTS idx_incidents_severity ON public.incidents (severity);
CREATE INDEX IF NOT EXISTS idx_incidents_first_seen ON public.incidents (first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_correlation_id ON public.incidents (correlation_id);

-- RLS: service_role can insert/read all, authenticated users read own suite
ALTER TABLE public.incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.incidents FORCE ROW LEVEL SECURITY;

-- Service role (orchestrator) can do everything
CREATE POLICY incidents_service_all ON public.incidents
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Authenticated users can only read incidents for their own suite
CREATE POLICY incidents_auth_select ON public.incidents
    FOR SELECT TO authenticated
    USING (suite_id = current_setting('app.current_suite_id', true));


-- =============================================================================
-- provider_call_log — durable log of every external API call
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.provider_call_log (
    call_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id TEXT,
    provider TEXT NOT NULL,
    action TEXT NOT NULL,
    suite_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'error'
        CHECK (status IN ('success', 'error')),
    http_status INTEGER NOT NULL DEFAULT 0,
    error_code TEXT DEFAULT '',
    error_message TEXT DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    latency_ms NUMERIC(10,1) NOT NULL DEFAULT 0.0,
    redacted_payload_preview TEXT DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for admin queries
CREATE INDEX IF NOT EXISTS idx_pcl_provider ON public.provider_call_log (provider);
CREATE INDEX IF NOT EXISTS idx_pcl_correlation_id ON public.provider_call_log (correlation_id);
CREATE INDEX IF NOT EXISTS idx_pcl_status ON public.provider_call_log (status);
CREATE INDEX IF NOT EXISTS idx_pcl_started_at ON public.provider_call_log (started_at DESC);

-- RLS: service_role can insert/read all
ALTER TABLE public.provider_call_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.provider_call_log FORCE ROW LEVEL SECURITY;

CREATE POLICY pcl_service_all ON public.provider_call_log
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Authenticated admin users can read (no write)
CREATE POLICY pcl_auth_select ON public.provider_call_log
    FOR SELECT TO authenticated USING (true);


-- =============================================================================
-- client_events — frontend error reports (Wave 4I preparation)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.client_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type TEXT NOT NULL,
    suite_id TEXT NOT NULL,
    correlation_id TEXT,
    severity TEXT NOT NULL DEFAULT 'info'
        CHECK (severity IN ('debug', 'info', 'warning', 'error', 'critical')),
    message TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    user_agent TEXT DEFAULT '',
    url TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_ce_suite_id ON public.client_events (suite_id);
CREATE INDEX IF NOT EXISTS idx_ce_event_type ON public.client_events (event_type);
CREATE INDEX IF NOT EXISTS idx_ce_created_at ON public.client_events (created_at DESC);

-- RLS
ALTER TABLE public.client_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.client_events FORCE ROW LEVEL SECURITY;

CREATE POLICY ce_service_all ON public.client_events
    FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE POLICY ce_auth_insert ON public.client_events
    FOR INSERT TO authenticated
    WITH CHECK (suite_id = current_setting('app.current_suite_id', true));

CREATE POLICY ce_auth_select ON public.client_events
    FOR SELECT TO authenticated
    USING (suite_id = current_setting('app.current_suite_id', true));
