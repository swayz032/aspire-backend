-- =============================================================================
-- Migration 118 — W9 reputation polling + CNAM display-name change
-- =============================================================================
-- Plan reference: ~/.claude/plans/the-image-was-off-calm-lynx.md §3 W9
--
-- Wave 9 introduces three operational capabilities on top of the W5 trust
-- onboarding ledger:
--
--   1. Carrier reputation polling — every 6h, each tenant in
--      trust_state='number_attached' is polled for spam-flagging by
--      T-Mobile / AT&T / Verizon. The result is recorded inline on
--      tenant_trust_profiles so the FE banner / GET /v1/trust-hub/status
--      can surface a fresh "Spam Likely" warning without joining a
--      separate table.
--
--   2. CNAM display-name change requests — when a tenant renames their
--      business (e.g., "Scott Painting" → "Scott Painting Pro") we run a
--      4-step Twilio sequence: validate new name → update CNAM EndUser →
--      re-submit CNAM Trust Product → wait for new approval. Twilio's
--      policy enforces a 30-day cooldown between display-name changes.
--      The cooldown timestamp lives on the trust profile; the request
--      history lives in a separate table.
--
--   3. Stuck-state auto-recovery — handled in code (cron_jobs.py) by
--      reading existing columns; no new columns needed.
--
-- This migration is ADDITIVE-ONLY (no DROP, no DELETE). Idempotent on
-- re-run (IF NOT EXISTS / DO blocks). RLS FORCED on the new table.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. tenant_trust_profiles — 4 new columns
-- ---------------------------------------------------------------------------

ALTER TABLE public.tenant_trust_profiles
    ADD COLUMN IF NOT EXISTS last_reputation_check        TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS last_reputation_status       JSONB       NULL,
    ADD COLUMN IF NOT EXISTS cnam_display_name_pending    BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS last_cnam_change_at          TIMESTAMPTZ NULL;

COMMENT ON COLUMN public.tenant_trust_profiles.last_reputation_check IS
    'W9 reputation polling: timestamp of the most recent carrier '
    'reputation poll. NULL means never polled. The poll cron job '
    'selects WHERE last_reputation_check IS NULL OR < now() - 6h.';

COMMENT ON COLUMN public.tenant_trust_profiles.last_reputation_status IS
    'W9 reputation polling: JSONB shape mirroring Twilio''s Branded '
    'Calling reputation API response. Keys: t_mobile, att, verizon, '
    'overall. Value: { score, label, last_change_at } per carrier. '
    'NULL when feature flag BRANDED_CALLING_ENABLED is off.';

COMMENT ON COLUMN public.tenant_trust_profiles.cnam_display_name_pending IS
    'W9 CNAM display-name change: TRUE while a re-submission is in '
    'flight (between request and Twilio approval). Cleared by the W5 '
    'status_callback handler on cnam_approved or cnam_rejected.';

COMMENT ON COLUMN public.tenant_trust_profiles.last_cnam_change_at IS
    'W9 CNAM display-name change: timestamp of the most recent change '
    'request. Twilio policy: max 1 display-name change per 30 days. '
    'The W9 cron job + route both validate this server-side.';

-- Index for the W9 reputation cron — selects tenants in number_attached
-- ordered by oldest reputation check first.
CREATE INDEX IF NOT EXISTS idx_trust_profiles_reputation_poll
    ON public.tenant_trust_profiles (last_reputation_check NULLS FIRST)
    WHERE trust_state = 'number_attached';

-- ---------------------------------------------------------------------------
-- 2. tenant_cnam_change_requests — new table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tenant_cnam_change_requests (
    id                          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID        NOT NULL,
    suite_id                    UUID        NOT NULL,
    office_id                   UUID        NULL,
    trust_profile_id            UUID        NOT NULL
                                            REFERENCES public.tenant_trust_profiles(id)
                                            ON DELETE CASCADE,

    -- Display name fields. The route writes both:
    --   requested_display_name   — exactly what the tenant typed
    --   sanitized_display_name   — output of cnam_sanitizer (15-char rules)
    -- Twilio only ever sees the sanitized form.
    requested_display_name      TEXT        NOT NULL CHECK (length(requested_display_name) BETWEEN 2 AND 120),
    sanitized_display_name      TEXT        NOT NULL CHECK (length(sanitized_display_name) BETWEEN 1 AND 15),

    -- Lifecycle status (closed enum).
    --   pending           — INSERTed by route, ARQ job not yet enqueued
    --   cooldown_pending  — 30-day cooldown not met; cron will retry after
    --   in_progress       — ARQ worker has picked up the job
    --   approved          — Twilio approved the new display name
    --   rejected          — Twilio rejected; tenant must resubmit
    --   failed            — terminal worker failure (sanitization regression
    --                       or CNAM Trust Product invariant violation)
    status                      TEXT        NOT NULL DEFAULT 'pending'
                                            CHECK (status IN (
                                                'pending',
                                                'cooldown_pending',
                                                'in_progress',
                                                'approved',
                                                'rejected',
                                                'failed'
                                            )),
    reason_code                 TEXT        NULL,

    -- Audit
    capability_token_id         TEXT        NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at                TIMESTAMPTZ NULL
);

-- One in-flight change per tenant (mirror W11 swap pattern).
CREATE UNIQUE INDEX IF NOT EXISTS uq_cnam_change_active
    ON public.tenant_cnam_change_requests (suite_id)
    WHERE status IN ('pending', 'cooldown_pending', 'in_progress');

-- Cron pickup index — pending + cooldown_pending, oldest first.
CREATE INDEX IF NOT EXISTS idx_cnam_change_status_created
    ON public.tenant_cnam_change_requests (status, created_at);

CREATE INDEX IF NOT EXISTS idx_cnam_change_suite
    ON public.tenant_cnam_change_requests (suite_id, created_at DESC);

-- updated_at trigger (mirrors moddatetime pattern across the schema).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_tenant_cnam_change_requests_updated_at'
    ) THEN
        CREATE TRIGGER trg_tenant_cnam_change_requests_updated_at
            BEFORE UPDATE ON public.tenant_cnam_change_requests
            FOR EACH ROW EXECUTE FUNCTION moddatetime(updated_at);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. RLS FORCED — Law #6 tenant isolation
-- Same pattern as tenant_trust_profiles, tenant_a2p_brands, tenant_phone_swaps
-- ---------------------------------------------------------------------------

ALTER TABLE public.tenant_cnam_change_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_cnam_change_requests FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_cnam_change_authenticated_select
    ON public.tenant_cnam_change_requests;
CREATE POLICY tenant_cnam_change_authenticated_select
    ON public.tenant_cnam_change_requests
    FOR SELECT
    TO authenticated
    USING (
        suite_id IN (
            SELECT tm.suite_id
            FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
    );

DROP POLICY IF EXISTS tenant_cnam_change_service_role_all
    ON public.tenant_cnam_change_requests;
CREATE POLICY tenant_cnam_change_service_role_all
    ON public.tenant_cnam_change_requests
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

GRANT SELECT ON public.tenant_cnam_change_requests TO authenticated;
GRANT ALL    ON public.tenant_cnam_change_requests TO service_role;

COMMENT ON TABLE public.tenant_cnam_change_requests IS
    'W9 CNAM display-name change ledger. One in-flight request per suite '
    '(UNIQUE on suite_id WHERE status IN (pending, cooldown_pending, '
    'in_progress)). The route validates Twilio''s 30-day cooldown '
    'server-side via tenant_trust_profiles.last_cnam_change_at; if not '
    'met the row is created with status=cooldown_pending and the cron '
    'job retries after the cooldown elapses.';

COMMENT ON COLUMN public.tenant_cnam_change_requests.sanitized_display_name IS
    'Output of workers/trust_onboarding/cnam_sanitizer.sanitize_cnam_'
    'display_name applied to requested_display_name. Always 1-15 chars, '
    'starts with a letter, only A-Z 0-9 space period comma. This is the '
    'string Twilio sees; never the raw business name.';
