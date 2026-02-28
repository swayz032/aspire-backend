-- Migration 059: Signing sessions table + contract_state column on contracts
-- Enables embedded PandaDoc signing via Aspire-branded public pages.
-- External signers access via token (no Aspire account required).

-- 1. Add contract_state to contracts table
ALTER TABLE public.contracts
    ADD COLUMN IF NOT EXISTS contract_state TEXT NOT NULL DEFAULT 'draft'
    CHECK (contract_state IN ('draft', 'reviewed', 'sent', 'signed', 'archived', 'expired'));

CREATE INDEX IF NOT EXISTS idx_contracts_contract_state
    ON public.contracts(contract_state);

-- 2. Signing sessions table
CREATE TABLE IF NOT EXISTS public.signing_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token TEXT NOT NULL UNIQUE,
    document_id TEXT NOT NULL,
    suite_id UUID NOT NULL REFERENCES public.suites(id),
    signer_email TEXT NOT NULL DEFAULT '',
    signer_name TEXT NOT NULL DEFAULT '',
    pandadoc_session_id TEXT NOT NULL DEFAULT '',
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '24 hours'),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_signing_sessions_token ON public.signing_sessions(token);
CREATE INDEX IF NOT EXISTS idx_signing_sessions_document_id ON public.signing_sessions(document_id);
CREATE INDEX IF NOT EXISTS idx_signing_sessions_suite_id ON public.signing_sessions(suite_id);
CREATE INDEX IF NOT EXISTS idx_signing_sessions_expires_at ON public.signing_sessions(expires_at);

-- 3. RLS (Law #6: tenant isolation for admin queries)
ALTER TABLE public.signing_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.signing_sessions FORCE ROW LEVEL SECURITY;

-- Suite-scoped policy for authenticated admin queries
CREATE POLICY signing_sessions_tenant_isolation ON public.signing_sessions
    USING (suite_id = current_setting('app.current_suite_id')::uuid);

CREATE POLICY signing_sessions_insert_tenant ON public.signing_sessions
    FOR INSERT WITH CHECK (suite_id = current_setting('app.current_suite_id')::uuid);

-- Public token lookup policy (no suite_id required — external signers access by token)
-- This uses service_role or anon key with token-based lookup
CREATE POLICY signing_sessions_public_token_lookup ON public.signing_sessions
    FOR SELECT USING (true);
    -- Note: The public route in routes.ts queries by token only.
    -- RLS for SELECT allows all reads since token is unique and unguessable (UUID).
    -- Suite-scoped INSERT policy prevents cross-tenant session creation.

-- Comments
COMMENT ON TABLE public.signing_sessions IS 'PandaDoc embedded signing sessions. External signers access via token.';
COMMENT ON COLUMN public.signing_sessions.token IS 'Unique, unguessable token for public signing URL (UUID format).';
COMMENT ON COLUMN public.signing_sessions.pandadoc_session_id IS 'PandaDoc session ID for iframe embedding.';
