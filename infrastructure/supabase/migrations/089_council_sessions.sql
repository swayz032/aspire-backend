-- Council sessions and proposals — persistent Meeting of Minds state
-- Replaces in-memory _sessions dict in council_service.py

CREATE TABLE IF NOT EXISTS public.council_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id text NOT NULL,
    trigger text NOT NULL DEFAULT 'manual',
    evidence_pack jsonb NOT NULL DEFAULT '{}',
    members text[] NOT NULL DEFAULT ARRAY['gpt', 'gemini', 'claude'],
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'collecting', 'deliberating', 'decided', 'error')),
    decision jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    created_by text NOT NULL DEFAULT 'ava_admin'
);

CREATE TABLE IF NOT EXISTS public.council_proposals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid NOT NULL REFERENCES public.council_sessions(id) ON DELETE CASCADE,
    member text NOT NULL,
    root_cause text NOT NULL,
    fix_plan text NOT NULL,
    tests text[] NOT NULL DEFAULT '{}',
    risk_tier text NOT NULL DEFAULT 'green',
    evidence_links text[] NOT NULL DEFAULT '{}',
    confidence float NOT NULL DEFAULT 0.5 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status text NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted', 'accepted', 'rejected')),
    raw_response jsonb,
    model_used text,
    tokens_used integer DEFAULT 0,
    latency_ms integer DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_council_sessions_status ON public.council_sessions(status);
CREATE INDEX IF NOT EXISTS idx_council_sessions_incident ON public.council_sessions(incident_id);
CREATE INDEX IF NOT EXISTS idx_council_proposals_session ON public.council_proposals(session_id);

-- RLS: service_role full access
ALTER TABLE public.council_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.council_proposals ENABLE ROW LEVEL SECURITY;

CREATE POLICY "council_sessions_service_role_all"
    ON public.council_sessions FOR ALL
    USING (auth.role() = 'service_role');

CREATE POLICY "council_proposals_service_role_all"
    ON public.council_proposals FOR ALL
    USING (auth.role() = 'service_role');

-- Admin read access
CREATE POLICY "council_sessions_admin_select"
    ON public.council_sessions FOR SELECT
    USING (
        auth.role() = 'authenticated'
        AND (auth.jwt() ->> 'email') IN (
            SELECT unnest(string_to_array(
                current_setting('app.admin_emails', true),
                ','
            ))
        )
    );

CREATE POLICY "council_proposals_admin_select"
    ON public.council_proposals FOR SELECT
    USING (
        auth.role() = 'authenticated'
        AND (auth.jwt() ->> 'email') IN (
            SELECT unnest(string_to_array(
                current_setting('app.admin_emails', true),
                ','
            ))
        )
    );
