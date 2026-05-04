-- =============================================================================
-- Migration 110 — trust hub support tables
-- =============================================================================
-- Plan: docs/plans/per-tenant-trust-hub-cnam.md, Wave 1-B
-- Slim: ~/.claude/plans/the-image-was-off-calm-lynx.md §3 W1
--
-- Three tables that hang off tenant_trust_profiles (migration 109):
--   1. tenant_authorized_reps — N:1 per trust_profile (Twilio requires 1-2 reps)
--   2. tenant_cnam_records — 1:1 per phone number (W5 CNAM 8-step recipe state)
--   3. trust_state_transitions — append-only audit ledger (separate from `receipts`)
--
-- All three: RLS FORCED, scope-triple, Vault references for PII, no plaintext PII.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 110-A: tenant_authorized_reps
-- -----------------------------------------------------------------------------
-- One row per Twilio EndUser of type=authorized_representative_1|2.
-- Most Aspire ICP qualifies as Sole Prop and only needs 1 rep. Standard
-- A2P + non-sole-prop businesses need 2 reps per Twilio policy.

CREATE TABLE IF NOT EXISTS public.tenant_authorized_reps (
    id                          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Scope (Law #6)
    tenant_id                   UUID        NOT NULL,
    suite_id                    UUID        NOT NULL,
    trust_profile_id            UUID        NOT NULL
                                            REFERENCES public.tenant_trust_profiles(id)
                                            ON DELETE CASCADE,

    -- Identity (non-encrypted — names + business contact info)
    first_name                  TEXT        NOT NULL,
    last_name                   TEXT        NOT NULL,
    title                       TEXT        NOT NULL,
    email                       TEXT        NOT NULL,
    phone_e164                  TEXT        NOT NULL,

    -- Encrypted PII fields (vault.secrets UUID references)
    dob_vault_secret_id         UUID        NULL,   -- Date of birth (YYYY-MM-DD)
    ssn_last4_vault_secret_id   UUID        NULL,   -- Last 4 SSN digits

    -- Twilio EndUser SID once created
    twilio_end_user_sid         TEXT        NULL,   -- "ITxxxxxxxx..."
    twilio_end_user_type        TEXT        NULL    -- "authorized_representative_1" or "_2"
                                            CHECK (twilio_end_user_type IS NULL OR
                                                   twilio_end_user_type IN (
                                                       'authorized_representative_1',
                                                       'authorized_representative_2'
                                                   )),

    -- Audit
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tar_trust_profile
    ON public.tenant_authorized_reps (trust_profile_id);
CREATE INDEX IF NOT EXISTS idx_tar_twilio_end_user
    ON public.tenant_authorized_reps (twilio_end_user_sid)
    WHERE twilio_end_user_sid IS NOT NULL;

ALTER TABLE public.tenant_authorized_reps ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_authorized_reps FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tar_tenant_isolation ON public.tenant_authorized_reps;
CREATE POLICY tar_tenant_isolation ON public.tenant_authorized_reps
    FOR ALL TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

DROP POLICY IF EXISTS tar_service_role_all ON public.tenant_authorized_reps;
CREATE POLICY tar_service_role_all ON public.tenant_authorized_reps
    FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP TRIGGER IF EXISTS set_tar_updated_at ON public.tenant_authorized_reps;
CREATE TRIGGER set_tar_updated_at
    BEFORE UPDATE ON public.tenant_authorized_reps
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

-- App-layer enforces max 2 reps per trust_profile_id (Twilio policy).
-- A DB-level CHECK can't enforce N:1 cardinality cleanly without a trigger;
-- the worker rejects a third rep at the route boundary.
COMMENT ON TABLE public.tenant_authorized_reps IS
    'Authorized representatives for Twilio Trust Hub Secondary Customer Profile. '
    'Max 2 per trust_profile_id (enforced at route layer). DOB + SSN last 4 '
    'encrypted via Supabase Vault.';


-- -----------------------------------------------------------------------------
-- 110-B: tenant_cnam_records
-- -----------------------------------------------------------------------------
-- 1:1 with each tenant_phone_numbers row that has CNAM registered.
-- Tracks the CNAM 8-step recipe state per number (W5 + W11 swap).

CREATE TABLE IF NOT EXISTS public.tenant_cnam_records (
    id                              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Scope
    tenant_id                       UUID        NOT NULL,
    suite_id                        UUID        NOT NULL,

    -- Foreign keys
    phone_number_id                 UUID        NOT NULL UNIQUE
                                                REFERENCES public.tenant_phone_numbers(id)
                                                ON DELETE CASCADE,
    trust_profile_id                UUID        NOT NULL
                                                REFERENCES public.tenant_trust_profiles(id)
                                                ON DELETE CASCADE,

    -- CNAM identity
    cnam_display_name               TEXT        NOT NULL,   -- sanitized, max 15 chars
    raw_business_name               TEXT        NOT NULL,   -- original before sanitization

    -- Twilio CNAM Trust Hub identifiers
    twilio_cnam_bundle_sid          TEXT        NULL,   -- "BUxxxxxxxx..."
    twilio_cnam_end_user_sid        TEXT        NULL,   -- "ITxxxxxxxx..."
    twilio_cnam_channel_endpoint_sid TEXT       NULL,   -- "RAxxxxxxxx..." (ChannelEndpointAssignment)

    -- Status
    cnam_status                     TEXT        NOT NULL DEFAULT 'pending'
                                                CHECK (cnam_status IN (
                                                    'pending',
                                                    'in_review',
                                                    'approved',
                                                    'rejected',
                                                    'suspended'
                                                )),
    caller_id_lookup_enabled        BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Audit
    submitted_at                    TIMESTAMPTZ NULL,
    approved_at                     TIMESTAMPTZ NULL,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tcr_suite_id
    ON public.tenant_cnam_records (suite_id);
CREATE INDEX IF NOT EXISTS idx_tcr_cnam_status
    ON public.tenant_cnam_records (cnam_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tcr_trust_profile
    ON public.tenant_cnam_records (trust_profile_id);

ALTER TABLE public.tenant_cnam_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_cnam_records FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tcr_tenant_isolation ON public.tenant_cnam_records;
CREATE POLICY tcr_tenant_isolation ON public.tenant_cnam_records
    FOR ALL TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

DROP POLICY IF EXISTS tcr_service_role_all ON public.tenant_cnam_records;
CREATE POLICY tcr_service_role_all ON public.tenant_cnam_records
    FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP TRIGGER IF EXISTS set_tcr_updated_at ON public.tenant_cnam_records;
CREATE TRIGGER set_tcr_updated_at
    BEFORE UPDATE ON public.tenant_cnam_records
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

COMMENT ON TABLE public.tenant_cnam_records IS
    'Per-number CNAM (caller ID branding) state. 1:1 with tenant_phone_numbers. '
    'Tracks the W5 CNAM 8-step recipe progression. Multiple records per '
    'trust_profile_id are allowed (e.g., after W11 number swap).';


-- -----------------------------------------------------------------------------
-- 110-C: trust_state_transitions
-- -----------------------------------------------------------------------------
-- Append-only audit ledger SEPARATE from the `receipts` table. Captures
-- every state-machine transition with full context. Receipts are cut for
-- governance-critical state changes only (per architect plan §VIII R4).
-- This table captures EVERY transition for ops visibility.
--
-- IMPORTANT: no UPDATE/DELETE policy for authenticated. service_role only
-- writes. Worker is the sole writer.

CREATE TABLE IF NOT EXISTS public.trust_state_transitions (
    id                          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Scope
    tenant_id                   UUID        NOT NULL,
    suite_id                    UUID        NOT NULL,

    -- Trust profile reference (RESTRICT — preserve audit trail)
    trust_profile_id            UUID        NOT NULL
                                            REFERENCES public.tenant_trust_profiles(id)
                                            ON DELETE RESTRICT,

    -- State transition
    from_state                  TEXT        NOT NULL,
    to_state                    TEXT        NOT NULL,
    event_type                  TEXT        NOT NULL,    -- maps to receipt_type when receipt_id is set

    -- Twilio callback data (when transition driven by Twilio status webhook)
    twilio_resource_sid         TEXT        NULL,
    twilio_status               TEXT        NULL,
    twilio_rejection_code       TEXT        NULL,
    twilio_rejection_reason     TEXT        NULL,

    -- Receipt linkage (hash chain — links to public.receipts.id)
    receipt_id                  UUID        NULL,
    previous_receipt_id         UUID        NULL,

    -- Worker context
    worker_job_id               TEXT        NULL,        -- ARQ job ID for traceability
    retry_count                 INTEGER     NOT NULL DEFAULT 0,

    -- Append-only timestamp
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Sanity: no self-loop transitions
    CONSTRAINT tst_no_self_loop CHECK (from_state != to_state)
);

CREATE INDEX IF NOT EXISTS idx_tst_trust_profile
    ON public.trust_state_transitions (trust_profile_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tst_suite_event
    ON public.trust_state_transitions (suite_id, event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tst_twilio_resource
    ON public.trust_state_transitions (twilio_resource_sid)
    WHERE twilio_resource_sid IS NOT NULL;

ALTER TABLE public.trust_state_transitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trust_state_transitions FORCE ROW LEVEL SECURITY;

-- Authenticated: SELECT own tenant only. NO INSERT/UPDATE/DELETE.
DROP POLICY IF EXISTS tst_tenant_select ON public.trust_state_transitions;
CREATE POLICY tst_tenant_select ON public.trust_state_transitions
    FOR SELECT TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

-- Service role: full access (worker is sole writer).
DROP POLICY IF EXISTS tst_service_role_all ON public.trust_state_transitions;
CREATE POLICY tst_service_role_all ON public.trust_state_transitions
    FOR ALL TO service_role USING (true) WITH CHECK (true);

COMMENT ON TABLE public.trust_state_transitions IS
    'Append-only audit ledger for trust onboarding state machine. Every '
    'transition logs from_state → to_state. Hash-chained per trust_profile_id '
    'via previous_receipt_id when a receipts row was cut for the same event. '
    'Authenticated role can SELECT own tenant rows only. service_role is sole writer.';
