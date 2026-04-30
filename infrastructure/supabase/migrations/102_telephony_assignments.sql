-- =============================================================================
-- Migration 102: Telephony Assignments — Sarah Receptionist + SMS infrastructure
-- =============================================================================
-- Pass 16 of the Office Memory Engine plan (the-image-was-off-calm-lynx).
--
-- Tables:
--   tenant_phone_numbers       — Aspire-purchased Twilio numbers, per tenant.
--                                Tracks Twilio SID + EL phone_number_id.
--   front_desk_configs         — versioned per-office Front Desk Setup config.
--                                One active version per office (max version_no).
--                                Saved changes apply to NEXT call only (Sarah v2 rule).
--   front_desk_routing_contacts — non-seat routing destinations (owner / sales /
--                                 support / etc.). Resolves to phone | SIP | message-only.
--   sms_messages               — individual SMS messages (helper for fast lookup;
--                                 the parent thread lives in memory_objects of
--                                 type='sms_thread').
--
-- Aspire Laws enforced:
--   Law #2 (Receipt for All)   — every state-changing operation cuts a receipt;
--                                 RLS prevents non-tenant rows.
--   Law #3 (Fail Closed)       — no DEFAULTs that could hide misconfiguration.
--   Law #6 (Tenant Isolation)  — RLS FORCED on every table; (tenant_id, suite_id,
--                                 office_id) triple on every row; cross-tenant
--                                 reads/writes return 0 rows.
--   Law #9 (Security)          — Twilio auth tokens NEVER stored here (EL holds
--                                 them via /v1/convai/phone-numbers import);
--                                 Twilio SIDs are non-sensitive identifiers.
--
-- Source-of-truth references:
--   - Sarah Receptionist Production Handoff v2 (RECEPTIONIST_SARAH_PRODUCTION_HANDOFF_v2.zip)
--   - ElevenLabs Phone Numbers API (verified contract via OpenAPI 2026-04-29)
--   - Plan §16.A
-- =============================================================================

-- =============================================================================
-- TABLE: tenant_phone_numbers
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.tenant_phone_numbers (
    id                          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Scope (Law #6) — all three required
    tenant_id                   UUID        NOT NULL,
    suite_id                    UUID        NOT NULL,
    office_id                   UUID        NOT NULL,

    -- Phone number identity (E.164)
    phone_number                TEXT        NOT NULL UNIQUE,
                                                          -- "+12125550198" — UNIQUE since
                                                          -- Twilio numbers are globally unique

    -- Twilio identifiers (non-sensitive)
    twilio_sid                  TEXT        NOT NULL UNIQUE,
                                                          -- "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    twilio_friendly_name        TEXT        NULL,

    -- ElevenLabs phone-number registration
    elevenlabs_phone_number_id  TEXT        NULL,
                                                          -- "pn_xxxxxxxxxxxxxxxxxxxx" returned by
                                                          -- POST /v1/convai/phone-numbers
    attached_to_agent_id        TEXT        NULL,
                                                          -- "agent_6501..." — typically Sarah Receptionist

    -- Capabilities (Twilio reports per number)
    capabilities                JSONB       NOT NULL DEFAULT '{}'::jsonb,
                                                          -- {"voice": true, "sms": true, "mms": true}
    sms_enabled                 BOOLEAN     NOT NULL DEFAULT TRUE,
    voice_enabled               BOOLEAN     NOT NULL DEFAULT TRUE,

    -- Lifecycle
    status                      TEXT        NOT NULL DEFAULT 'active'
                                            CHECK (status IN (
                                                'reserved','active','released','suspended'
                                            )),
    monthly_cost_cents          INTEGER     NULL,         -- Twilio reported price
    purchased_at                TIMESTAMPTZ NULL,
    released_at                 TIMESTAMPTZ NULL,

    -- Audit
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tpn_tenant_office
    ON public.tenant_phone_numbers (tenant_id, suite_id, office_id);
CREATE INDEX IF NOT EXISTS idx_tpn_status
    ON public.tenant_phone_numbers (status) WHERE status = 'active';

-- RLS forced (Law #6)
ALTER TABLE public.tenant_phone_numbers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_phone_numbers FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tpn_tenant_isolation ON public.tenant_phone_numbers;
CREATE POLICY tpn_tenant_isolation ON public.tenant_phone_numbers
    FOR ALL
    TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

-- Service role bypass (orchestrator + ingestion adapters use service key)
DROP POLICY IF EXISTS tpn_service_role_all ON public.tenant_phone_numbers;
CREATE POLICY tpn_service_role_all ON public.tenant_phone_numbers
    FOR ALL TO service_role USING (true) WITH CHECK (true);


-- =============================================================================
-- TABLE: front_desk_configs (versioned)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.front_desk_configs (
    id                          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Scope
    tenant_id                   UUID        NOT NULL,
    suite_id                    UUID        NOT NULL,
    office_id                   UUID        NOT NULL,

    -- Optional link to the Aspire-purchased number this config is bound to
    phone_number_id             UUID        NULL REFERENCES public.tenant_phone_numbers(id)
                                                ON DELETE SET NULL,

    -- Version (monotonic per office) — Sarah personalization webhook always
    -- reads the row with max version_no for the resolved office_id.
    version_no                  INTEGER     NOT NULL,
    is_current                  BOOLEAN     NOT NULL DEFAULT FALSE,
                                                  -- denormalized "max version" flag
                                                  -- updated by trigger on insert

    -- Public-number mode (Sarah v2 §07)
    public_number_mode          TEXT        NOT NULL DEFAULT 'ASPIRE_NUMBER'
                                            CHECK (public_number_mode IN (
                                                'ASPIRE_NUMBER',
                                                'KEEP_CURRENT_NUMBER'
                                            )),

    -- Catch mode (how owner receives transferred calls)
    catch_mode                  TEXT        NOT NULL DEFAULT 'APP_AND_PHONE_SIMUL_RING'
                                            CHECK (catch_mode IN (
                                                'APP_ONLY',
                                                'PHONE_ONLY',
                                                'APP_AND_PHONE_SIMUL_RING'
                                            )),

    -- Business hours — JSONB structure: {monday: {open: "09:00", close: "17:00", closed: false}, ...}
    business_hours              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    timezone                    TEXT        NOT NULL DEFAULT 'America/New_York',

    -- After-hours + busy handling
    after_hours_mode            TEXT        NOT NULL DEFAULT 'take_message'
                                            CHECK (after_hours_mode IN (
                                                'take_message',
                                                'callback_window',
                                                'try_transfer_then_message'
                                            )),
    busy_mode                   TEXT        NOT NULL DEFAULT 'take_message'
                                            CHECK (busy_mode IN (
                                                'take_message',
                                                'callback_window',
                                                'try_transfer_then_message'
                                            )),

    -- Optional pronunciation override for business name
    pronunciation_override      TEXT        NULL,

    -- Forwarding-status (only meaningful for KEEP_CURRENT_NUMBER mode)
    forwarding_status           TEXT        NOT NULL DEFAULT 'NOT_CONFIGURED'
                                            CHECK (forwarding_status IN (
                                                'NOT_CONFIGURED',
                                                'PENDING',
                                                'VERIFIED',
                                                'LAST_TEST_FAILED'
                                            )),
    last_forwarding_test_at     TIMESTAMPTZ NULL,
    last_forwarding_test_result TEXT        NULL,

    -- Audit
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by_user_id          UUID        NULL,

    -- Versioning UNIQUE: (tenant, suite, office, version_no) is unique;
    -- one row per version per office.
    UNIQUE (tenant_id, suite_id, office_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_fdc_tenant_office_current
    ON public.front_desk_configs (tenant_id, suite_id, office_id, version_no DESC);
CREATE INDEX IF NOT EXISTS idx_fdc_is_current_lookup
    ON public.front_desk_configs (office_id) WHERE is_current = TRUE;

ALTER TABLE public.front_desk_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.front_desk_configs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS fdc_tenant_isolation ON public.front_desk_configs;
CREATE POLICY fdc_tenant_isolation ON public.front_desk_configs
    FOR ALL
    TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

DROP POLICY IF EXISTS fdc_service_role_all ON public.front_desk_configs;
CREATE POLICY fdc_service_role_all ON public.front_desk_configs
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Trigger: when a new version lands, mark prior versions is_current=false.
-- Saved-changes-apply-to-NEXT-call rule: active calls keep their config.
CREATE OR REPLACE FUNCTION public.front_desk_config_mark_current()
RETURNS TRIGGER AS $$
BEGIN
    -- Demote prior versions for this (tenant, suite, office)
    UPDATE public.front_desk_configs
       SET is_current = FALSE
     WHERE office_id   = NEW.office_id
       AND tenant_id   = NEW.tenant_id
       AND suite_id    = NEW.suite_id
       AND id         != NEW.id;
    -- Promote the new row to current
    NEW.is_current := TRUE;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_fdc_mark_current ON public.front_desk_configs;
CREATE TRIGGER trg_fdc_mark_current
    BEFORE INSERT ON public.front_desk_configs
    FOR EACH ROW EXECUTE FUNCTION public.front_desk_config_mark_current();


-- =============================================================================
-- TABLE: front_desk_routing_contacts
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.front_desk_routing_contacts (
    id                          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Scope
    tenant_id                   UUID        NOT NULL,
    suite_id                    UUID        NOT NULL,
    office_id                   UUID        NOT NULL,

    -- Optional link to the front_desk_config row this contact belongs to
    front_desk_config_id        UUID        NULL REFERENCES public.front_desk_configs(id)
                                                ON DELETE CASCADE,

    -- Role-based routing slot — Sarah uses these in transfer-rule conditions
    role                        TEXT        NOT NULL
                                            CHECK (role IN (
                                                'owner','sales','support','billing','scheduling','custom'
                                            )),
    name                        TEXT        NOT NULL,
    phone                       TEXT        NULL,         -- E.164 — preferred destination
    sip_uri                     TEXT        NULL,         -- alternate SIP destination
    email                       TEXT        NULL,         -- for message-only fallback

    -- Transfer / fallback policy
    transfer_allowed            BOOLEAN     NOT NULL DEFAULT TRUE,
    fallback_mode               TEXT        NOT NULL DEFAULT 'message_only'
                                            CHECK (fallback_mode IN (
                                                'transfer_allowed',
                                                'message_only'
                                            )),
    sort_order                  INTEGER     NOT NULL DEFAULT 100,

    -- Audit
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A routing destination must have AT LEAST one resolvable target
    CHECK (phone IS NOT NULL OR sip_uri IS NOT NULL OR email IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_frc_tenant_office_role
    ON public.front_desk_routing_contacts (tenant_id, suite_id, office_id, role, sort_order);

ALTER TABLE public.front_desk_routing_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.front_desk_routing_contacts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS frc_tenant_isolation ON public.front_desk_routing_contacts;
CREATE POLICY frc_tenant_isolation ON public.front_desk_routing_contacts
    FOR ALL
    TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

DROP POLICY IF EXISTS frc_service_role_all ON public.front_desk_routing_contacts;
CREATE POLICY frc_service_role_all ON public.front_desk_routing_contacts
    FOR ALL TO service_role USING (true) WITH CHECK (true);


-- =============================================================================
-- TABLE: sms_messages
-- =============================================================================
-- Helper for fast SMS thread lookup. The thread-level memory lives in
-- memory_objects (type='sms_thread'); each individual message gets a row here
-- linking back via thread_memory_id.
CREATE TABLE IF NOT EXISTS public.sms_messages (
    id                          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Scope
    tenant_id                   UUID        NOT NULL,
    suite_id                    UUID        NOT NULL,
    office_id                   UUID        NOT NULL,

    -- Link to the parent sms_thread memory_object
    thread_memory_id            UUID        NULL,
                                                  -- references memory_objects.memory_id
                                                  -- nullable until Pass 14's adapter populates

    -- Direction + parties
    direction                   TEXT        NOT NULL CHECK (direction IN ('inbound','outbound')),
    from_number                 TEXT        NOT NULL,
    to_number                   TEXT        NOT NULL,

    -- Content
    body                        TEXT        NOT NULL DEFAULT '',
    media_urls                  JSONB       NOT NULL DEFAULT '[]'::jsonb,
                                                  -- ["https://api.twilio.com/.../Media/MEXXXX", ...]
    num_media                   INTEGER     NOT NULL DEFAULT 0,

    -- Twilio identifiers + delivery status
    twilio_message_sid          TEXT        NOT NULL UNIQUE,
                                                  -- "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    status                      TEXT        NOT NULL DEFAULT 'queued'
                                            CHECK (status IN (
                                                'queued','sending','sent',
                                                'delivered','undelivered','failed','received'
                                            )),
    error_code                  INTEGER     NULL,         -- Twilio error code on failure

    -- Time model
    sent_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at                TIMESTAMPTZ NULL,
    last_status_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sms_tenant_office_recent
    ON public.sms_messages (tenant_id, suite_id, office_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_sms_thread_memory
    ON public.sms_messages (thread_memory_id, sent_at DESC) WHERE thread_memory_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sms_status_pending
    ON public.sms_messages (status, last_status_at DESC)
    WHERE status IN ('queued','sending','sent');

ALTER TABLE public.sms_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sms_messages FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sms_tenant_isolation ON public.sms_messages;
CREATE POLICY sms_tenant_isolation ON public.sms_messages
    FOR ALL
    TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

DROP POLICY IF EXISTS sms_service_role_all ON public.sms_messages;
CREATE POLICY sms_service_role_all ON public.sms_messages
    FOR ALL TO service_role USING (true) WITH CHECK (true);


-- =============================================================================
-- Verification (manual / smoke):
--   SELECT COUNT(*) FROM tenant_phone_numbers;
--   SELECT COUNT(*) FROM front_desk_configs;
--   SELECT COUNT(*) FROM front_desk_routing_contacts;
--   SELECT COUNT(*) FROM sms_messages;
--   \d+ tenant_phone_numbers     -- confirms RLS forced
-- =============================================================================
