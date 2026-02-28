-- Migration 056: Contracts table for Clara Legal outbox
-- Persists signed/completed contract metadata from PandaDoc.
-- RLS enforced: suite_id scoping (Law #6: zero cross-tenant leakage).
-- Append-oriented: INSERT-heavy, UPDATE only for status transitions.

-- 1. Create contracts table
CREATE TABLE IF NOT EXISTS public.contracts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id TEXT NOT NULL,
    template_key TEXT NOT NULL,
    template_lane TEXT NOT NULL DEFAULT 'general',
    suite_id UUID NOT NULL REFERENCES public.suites(id),
    office_id UUID NOT NULL,
    correlation_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    risk_tier TEXT NOT NULL DEFAULT 'yellow'
        CHECK (risk_tier IN ('green', 'yellow', 'red')),
    pandadoc_status TEXT NOT NULL DEFAULT 'document.completed',
    parties JSONB NOT NULL DEFAULT '[]'::jsonb,
    terms JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    signed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Idempotency: one document per suite
    CONSTRAINT uq_contracts_suite_document UNIQUE (suite_id, document_id)
);

-- 2. Indexes
CREATE INDEX IF NOT EXISTS idx_contracts_suite_id ON public.contracts(suite_id);
CREATE INDEX IF NOT EXISTS idx_contracts_document_id ON public.contracts(document_id);
CREATE INDEX IF NOT EXISTS idx_contracts_template_key ON public.contracts(template_key);
CREATE INDEX IF NOT EXISTS idx_contracts_created_at ON public.contracts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contracts_signed_at ON public.contracts(signed_at DESC) WHERE signed_at IS NOT NULL;

-- 3. RLS (Law #6: tenant isolation)
ALTER TABLE public.contracts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.contracts FORCE ROW LEVEL SECURITY;

CREATE POLICY contracts_tenant_isolation ON public.contracts
    USING (suite_id = current_setting('app.current_suite_id')::uuid);

CREATE POLICY contracts_insert_tenant ON public.contracts
    FOR INSERT WITH CHECK (suite_id = current_setting('app.current_suite_id')::uuid);

-- 4. Updated_at trigger
CREATE OR REPLACE FUNCTION public.contracts_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_contracts_updated_at
    BEFORE UPDATE ON public.contracts
    FOR EACH ROW
    EXECUTE FUNCTION public.contracts_updated_at();

-- 5. Processed webhooks table (idempotent webhook handling)
CREATE TABLE IF NOT EXISTS public.processed_webhooks (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'pandadoc',
    document_id TEXT NOT NULL DEFAULT '',
    suite_id UUID,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_processed_webhooks_source ON public.processed_webhooks(source);
CREATE INDEX IF NOT EXISTS idx_processed_webhooks_processed_at ON public.processed_webhooks(processed_at DESC);

-- Comment
COMMENT ON TABLE public.contracts IS 'Clara Legal: signed contract records from PandaDoc. RLS enforced.';
COMMENT ON TABLE public.processed_webhooks IS 'Idempotent webhook dedup table for PandaDoc events.';
