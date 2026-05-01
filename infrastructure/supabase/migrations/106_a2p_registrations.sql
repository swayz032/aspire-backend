-- =============================================================================
-- Migration 106: A2P 10DLC Tenant Registrations (Pass 19 — Lane B §3.7)
-- =============================================================================
-- New table: tenant_a2p_registrations
-- Tracks the A2P 10DLC compliance status per tenant. Required before any
-- outbound SMS is allowed (Law #3 fail-closed gating in services/sms_io.py).
--
-- A2P status lifecycle:
--   unregistered → pending_brand → pending_campaign → registered → suspended
--
-- Aspire Laws enforced:
--   Law #2 (Receipt for All)   — every state change to this table is driven
--                                 by a service that cuts a receipt. The table
--                                 itself is never directly mutated from outside
--                                 the orchestrator service layer.
--   Law #3 (Fail Closed)       — SMS service reads status and blocks on anything
--                                 other than 'registered'.
--   Law #6 (Tenant Isolation)  — RLS FORCED: authenticated user can only see
--                                 rows where tenant_id matches their JWT claim.
--                                 Cross-tenant select returns 0 rows.
--                                 Pattern mirrors tenant_phone_numbers (migration 102).
--   Law #9 (Security)          — No PII stored here. brand_id / campaign_id
--                                 are Twilio TCR identifiers (non-sensitive).
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.tenant_a2p_registrations (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Scope (Law #6) — tenant_id is the isolation key
    tenant_id           UUID            NOT NULL,

    -- Twilio Trust Hub identifiers (non-sensitive TCR IDs)
    brand_id            TEXT            NULL,
        -- e.g. "BN..." — assigned by Twilio TCR after brand registration
    campaign_id         TEXT            NULL,
        -- e.g. "CA..." — assigned by Twilio TCR after campaign registration

    -- Compliance status lifecycle
    status              TEXT            NOT NULL DEFAULT 'unregistered'
                            CHECK (status IN (
                                'unregistered',
                                'pending_brand',
                                'pending_campaign',
                                'registered',
                                'suspended'
                            )),

    -- Timestamps
    registered_at       TIMESTAMPTZ     NULL,
        -- Set when status transitions to 'registered'
    last_verified_at    TIMESTAMPTZ     NULL,
        -- Last time Twilio API confirmed registration is still active

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Primary lookup: service looks up by tenant_id to check A2P gate status
CREATE INDEX IF NOT EXISTS idx_tenant_a2p_registrations_tenant_id
    ON public.tenant_a2p_registrations (tenant_id);

-- Prevent duplicate registrations per tenant
CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_a2p_registrations_tenant_id_unique
    ON public.tenant_a2p_registrations (tenant_id)
    WHERE status != 'suspended';
    -- Allow a new registration after suspension (new brand can be filed)

-- ---------------------------------------------------------------------------
-- updated_at trigger (uses update_updated_at_column — consistent with migration 102)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE TRIGGER set_tenant_a2p_registrations_updated_at
    BEFORE UPDATE ON public.tenant_a2p_registrations
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();

-- ---------------------------------------------------------------------------
-- RLS — FORCED (Law #6: zero cross-tenant leakage)
-- Pattern mirrors tenant_phone_numbers from migration 102:
--   JWT claim `request.jwt.claim.tenant_id` must match the row's tenant_id.
-- ---------------------------------------------------------------------------

ALTER TABLE public.tenant_a2p_registrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_a2p_registrations FORCE ROW LEVEL SECURITY;

-- Authenticated users can only access their own tenant's A2P status.
DROP POLICY IF EXISTS tar_tenant_isolation ON public.tenant_a2p_registrations;
CREATE POLICY tar_tenant_isolation ON public.tenant_a2p_registrations
    FOR ALL
    TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

-- Service role bypass (orchestrator service key has unrestricted access).
DROP POLICY IF EXISTS tar_service_role_all ON public.tenant_a2p_registrations;
CREATE POLICY tar_service_role_all ON public.tenant_a2p_registrations
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

GRANT SELECT ON public.tenant_a2p_registrations TO authenticated;
GRANT ALL    ON public.tenant_a2p_registrations TO service_role;

-- Comment
COMMENT ON TABLE public.tenant_a2p_registrations IS
    'A2P 10DLC compliance registration status per tenant. '
    'sms_io.send_sms checks this table and blocks outbound SMS when '
    'status != ''registered'' (Law #3 fail-closed). '
    'Full Trust Hub wizard deferred to V1.1.';
