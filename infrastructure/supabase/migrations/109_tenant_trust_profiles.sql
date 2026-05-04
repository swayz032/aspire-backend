-- =============================================================================
-- Migration 109 — tenant_trust_profiles
-- =============================================================================
-- Plan: docs/plans/per-tenant-trust-hub-cnam.md, Wave 1-A
-- Slim: ~/.claude/plans/the-image-was-off-calm-lynx.md §3 W1
--
-- Background. Every Aspire tenant (1:1 suite_id) gets their own Twilio Trust
-- Hub bundle stack so SHAKEN/STIR signs to THEIR identity and CNAM displays
-- THEIR business name on outbound caller ID. This table is the durable store
-- for the KYB data + state-machine progress + Twilio bundle SIDs.
--
-- Encryption. EIN and SSN (sole prop) are PII — Law #9. Stored as UUID
-- references into vault.secrets via Supabase pgsodium. The columns hold
-- only the secret_id; decryption requires vault.decrypted_secrets which is
-- service_role-only.
--
-- RLS. FORCED. Tenants see their own row only. Worker uses service_role to
-- drive the state machine.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.tenant_trust_profiles (
    id                              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Scope (Law #6 — triple required for tenant isolation)
    tenant_id                       UUID        NOT NULL,
    suite_id                        UUID        NOT NULL UNIQUE,   -- 1:1 per business
    office_id                       UUID        NOT NULL,

    -- Business identity (plaintext — non-PII, public-record data)
    legal_business_name             TEXT        NOT NULL,
    dba_name                        TEXT        NULL,
    business_type                   TEXT        NOT NULL
                                                CHECK (business_type IN (
                                                    'sole_proprietor',
                                                    'partnership',
                                                    'llc',
                                                    'corporation',
                                                    'nonprofit',
                                                    'government',
                                                    'other'
                                                )),
    industry_code                   TEXT        NULL,

    -- Business address (non-PII — public record)
    address_street                  TEXT        NOT NULL,
    address_city                    TEXT        NOT NULL,
    address_state                   TEXT        NOT NULL,
    address_zip                     TEXT        NOT NULL,
    address_country                 TEXT        NOT NULL DEFAULT 'US',

    -- Encrypted PII fields (vault.secrets UUID references; never plaintext)
    ein_vault_secret_id             UUID        NULL,   -- EIN encrypted in vault
    ssn_vault_secret_id             UUID        NULL,   -- SSN for sole prop (alternative to EIN)

    -- Twilio Trust Hub identifiers (non-sensitive — just SIDs)
    twilio_secondary_profile_sid    TEXT        NULL,   -- "BUxxxxxxxx..." Secondary Customer Profile
    twilio_shaken_bundle_sid        TEXT        NULL,   -- "BUxxxxxxxx..." SHAKEN/STIR Trust Product
    twilio_cnam_bundle_sid          TEXT        NULL,   -- "BUxxxxxxxx..." CNAM Trust Product
    twilio_voice_integrity_bundle_sid TEXT      NULL,   -- "BUxxxxxxxx..." Voice Integrity (W6 prereq)

    -- State machine status (12 states — see workers/trust_onboarding/state_machine.py)
    trust_state                     TEXT        NOT NULL DEFAULT 'kyb_collected'
                                                CHECK (trust_state IN (
                                                    'kyb_collected',
                                                    'profile_drafted',
                                                    'profile_submitted',
                                                    'profile_approved',
                                                    'profile_rejected',
                                                    'shaken_created',
                                                    'shaken_submitted',
                                                    'shaken_approved',
                                                    'cnam_created',
                                                    'cnam_submitted',
                                                    'cnam_approved',
                                                    'number_attached',
                                                    'branded_calling_pending',
                                                    'branded_calling_live',
                                                    'failed',
                                                    'suspended'
                                                )),

    -- Last rejection details (when trust_state IN profile_rejected | failed)
    rejection_reason                TEXT        NULL,
    rejection_code                  TEXT        NULL,
    rejection_fields                TEXT[]      NULL,

    -- Dispute tracking
    dispute_submitted_at            TIMESTAMPTZ NULL,
    dispute_count                   INTEGER     NOT NULL DEFAULT 0,

    -- Branded Calling (private beta gated; W6)
    branded_calling_enabled         BOOLEAN     NOT NULL DEFAULT FALSE,
    branded_calling_display_name    TEXT        NULL,

    -- Audit timestamps
    kyb_collected_at                TIMESTAMPTZ NULL,
    profile_approved_at             TIMESTAMPTZ NULL,
    cnam_approved_at                TIMESTAMPTZ NULL,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ttp_suite_id
    ON public.tenant_trust_profiles (suite_id);
CREATE INDEX IF NOT EXISTS idx_ttp_trust_state
    ON public.tenant_trust_profiles (trust_state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ttp_twilio_profile_sid
    ON public.tenant_trust_profiles (twilio_secondary_profile_sid)
    WHERE twilio_secondary_profile_sid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ttp_twilio_shaken_sid
    ON public.tenant_trust_profiles (twilio_shaken_bundle_sid)
    WHERE twilio_shaken_bundle_sid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ttp_twilio_cnam_sid
    ON public.tenant_trust_profiles (twilio_cnam_bundle_sid)
    WHERE twilio_cnam_bundle_sid IS NOT NULL;

ALTER TABLE public.tenant_trust_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_trust_profiles FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ttp_tenant_isolation ON public.tenant_trust_profiles;
CREATE POLICY ttp_tenant_isolation ON public.tenant_trust_profiles
    FOR ALL TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

DROP POLICY IF EXISTS ttp_service_role_all ON public.tenant_trust_profiles;
CREATE POLICY ttp_service_role_all ON public.tenant_trust_profiles
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Updated-at auto-maintenance trigger (function exists from prior migrations)
DROP TRIGGER IF EXISTS set_ttp_updated_at ON public.tenant_trust_profiles;
CREATE TRIGGER set_ttp_updated_at
    BEFORE UPDATE ON public.tenant_trust_profiles
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

COMMENT ON TABLE public.tenant_trust_profiles IS
    '1:1 per suite_id. Holds KYB data + state-machine progress + Twilio Trust '
    'Hub bundle SIDs for per-tenant CNAM (caller ID branding). EIN/SSN PII '
    'encrypted via Supabase Vault — columns hold UUID references only. RLS '
    'FORCED. State machine driven by workers/trust_onboarding/state_machine.py.';

COMMENT ON COLUMN public.tenant_trust_profiles.ein_vault_secret_id IS
    'UUID into vault.secrets. Decryption via vault.decrypted_secrets (service_role only). '
    'NEVER expose to authenticated role. NEVER include in receipts or ARQ payloads.';

COMMENT ON COLUMN public.tenant_trust_profiles.ssn_vault_secret_id IS
    'UUID into vault.secrets — set when business_type=sole_proprietor (SSN substitutes EIN). '
    'Same security rules as ein_vault_secret_id.';

COMMENT ON COLUMN public.tenant_trust_profiles.trust_state IS
    'State-machine current state. Driven by workers/trust_onboarding/state_machine.py. '
    'Forward transitions either by ARQ job (after Twilio API call) or by '
    'POST /v1/trust-hub/status-callback (when Twilio approves/rejects async).';
