-- =============================================================================
-- Migration 111 — A2P 10DLC full registration tables
-- =============================================================================
-- Plan: docs/plans/per-tenant-trust-hub-cnam.md, Wave 1-C
-- Slim: ~/.claude/plans/the-image-was-off-calm-lynx.md §3 W7
--
-- Background. Migration 106 created a lightweight tenant_a2p_registrations
-- stub. This migration adds two proper tables for full A2P 10DLC registration
-- (Wave 7) and leaves the stub for backward compatibility.
--
-- A2P 10DLC = "Application-to-Person 10-Digit Long Code". Required by US
-- carriers (T-Mobile, AT&T, Verizon) for any business sending SMS via long
-- codes. Sole Proprietor flow is free; Standard Brand is $4 reg + $10/mo
-- per campaign. ~80% of Aspire ICP qualifies as Sole Prop.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 111-A: tenant_a2p_brands
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tenant_a2p_brands (
    id                              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Scope (1:1 per suite — one brand per business)
    tenant_id                       UUID        NOT NULL,
    suite_id                        UUID        NOT NULL UNIQUE,

    -- Brand type
    brand_type                      TEXT        NOT NULL DEFAULT 'sole_proprietor'
                                                CHECK (brand_type IN (
                                                    'sole_proprietor',
                                                    'standard'
                                                )),

    -- Twilio A2P identifiers
    twilio_brand_sid                TEXT        NULL,   -- "BNxxxxxxxx..."
    twilio_brand_registration_sid   TEXT        NULL,   -- "BRxxxxxxxx..."

    -- Status
    brand_status                    TEXT        NOT NULL DEFAULT 'draft'
                                                CHECK (brand_status IN (
                                                    'draft',
                                                    'pending',
                                                    'approved',
                                                    'rejected',
                                                    'suspended'
                                                )),
    rejection_reason                TEXT        NULL,

    -- OTP verification (Sole Prop flow requires OTP to authorized rep phone)
    otp_sent_at                     TIMESTAMPTZ NULL,
    otp_verified_at                 TIMESTAMPTZ NULL,

    -- Audit
    submitted_at                    TIMESTAMPTZ NULL,
    approved_at                     TIMESTAMPTZ NULL,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tab_suite_id
    ON public.tenant_a2p_brands (suite_id);
CREATE INDEX IF NOT EXISTS idx_tab_brand_status
    ON public.tenant_a2p_brands (brand_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tab_twilio_brand
    ON public.tenant_a2p_brands (twilio_brand_sid)
    WHERE twilio_brand_sid IS NOT NULL;

ALTER TABLE public.tenant_a2p_brands ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_a2p_brands FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tab_tenant_isolation ON public.tenant_a2p_brands;
CREATE POLICY tab_tenant_isolation ON public.tenant_a2p_brands
    FOR ALL TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

DROP POLICY IF EXISTS tab_service_role_all ON public.tenant_a2p_brands;
CREATE POLICY tab_service_role_all ON public.tenant_a2p_brands
    FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP TRIGGER IF EXISTS set_tab_updated_at ON public.tenant_a2p_brands;
CREATE TRIGGER set_tab_updated_at
    BEFORE UPDATE ON public.tenant_a2p_brands
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


-- -----------------------------------------------------------------------------
-- 111-B: tenant_a2p_campaigns
-- -----------------------------------------------------------------------------
-- Sole Prop: 1 brand = 1 campaign = 1 number (Twilio constraint).
-- Standard:  1 brand = N campaigns (paid).

CREATE TABLE IF NOT EXISTS public.tenant_a2p_campaigns (
    id                              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Scope
    tenant_id                       UUID        NOT NULL,
    suite_id                        UUID        NOT NULL,

    -- Parent brand
    brand_id                        UUID        NOT NULL
                                                REFERENCES public.tenant_a2p_brands(id)
                                                ON DELETE RESTRICT,

    -- Campaign details
    campaign_use_case               TEXT        NOT NULL DEFAULT 'MIXED'
                                                CHECK (campaign_use_case IN (
                                                    'MIXED',
                                                    '2FA',
                                                    'ACCOUNT_NOTIFICATION',
                                                    'CUSTOMER_CARE',
                                                    'DELIVERY_NOTIFICATION',
                                                    'FRAUD_ALERT',
                                                    'HIGHER_EDUCATION',
                                                    'LOW_VOLUME',
                                                    'MARKETING',
                                                    'POLLING_VOTING',
                                                    'PUBLIC_SERVICE_ANNOUNCEMENT'
                                                )),
    campaign_description            TEXT        NOT NULL,
    sample_messages                 TEXT[]      NOT NULL DEFAULT '{}',
    has_embedded_links              BOOLEAN     NOT NULL DEFAULT FALSE,
    has_embedded_phone              BOOLEAN     NOT NULL DEFAULT FALSE,
    opt_in_message                  TEXT        NULL,
    opt_in_keywords                 TEXT[]      NULL DEFAULT '{}',
    opt_out_keywords                TEXT[]      NULL DEFAULT '{STOP,UNSUBSCRIBE}',
    help_keywords                   TEXT[]      NULL DEFAULT '{HELP}',

    -- Twilio identifiers
    twilio_messaging_service_sid    TEXT        NULL,   -- "MGxxxxxxxx..."
    twilio_campaign_sid             TEXT        NULL,   -- "QExxxxxxxx..."

    -- Status
    campaign_status                 TEXT        NOT NULL DEFAULT 'draft'
                                                CHECK (campaign_status IN (
                                                    'draft',
                                                    'pending',
                                                    'approved',
                                                    'rejected',
                                                    'suspended'
                                                )),
    rejection_reason                TEXT        NULL,

    -- Audit
    submitted_at                    TIMESTAMPTZ NULL,
    approved_at                     TIMESTAMPTZ NULL,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tac_brand_id
    ON public.tenant_a2p_campaigns (brand_id);
CREATE INDEX IF NOT EXISTS idx_tac_suite_id
    ON public.tenant_a2p_campaigns (suite_id);
CREATE INDEX IF NOT EXISTS idx_tac_campaign_status
    ON public.tenant_a2p_campaigns (campaign_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tac_twilio_campaign
    ON public.tenant_a2p_campaigns (twilio_campaign_sid)
    WHERE twilio_campaign_sid IS NOT NULL;

ALTER TABLE public.tenant_a2p_campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_a2p_campaigns FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tac_tenant_isolation ON public.tenant_a2p_campaigns;
CREATE POLICY tac_tenant_isolation ON public.tenant_a2p_campaigns
    FOR ALL TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

DROP POLICY IF EXISTS tac_service_role_all ON public.tenant_a2p_campaigns;
CREATE POLICY tac_service_role_all ON public.tenant_a2p_campaigns
    FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP TRIGGER IF EXISTS set_tac_updated_at ON public.tenant_a2p_campaigns;
CREATE TRIGGER set_tac_updated_at
    BEFORE UPDATE ON public.tenant_a2p_campaigns
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

COMMENT ON TABLE public.tenant_a2p_brands IS
    'A2P 10DLC brand registration per suite (1:1). Sole Prop is free; Standard '
    'is $4 brand reg + $10/mo per campaign. Worker (W7) drives registration via '
    'Twilio Messaging A2P API.';

COMMENT ON TABLE public.tenant_a2p_campaigns IS
    'A2P 10DLC campaigns under a brand. Sole Prop allows 1 campaign per brand '
    'per number. Standard allows N campaigns. Worker (W7) drives via Twilio '
    'Messaging A2P UsAppToPerson API.';
