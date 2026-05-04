-- =============================================================================
-- Migration 119 — Trust Hub backfill (W10 existing-tenant migration)
-- =============================================================================
-- Plan reference: ~/.claude/plans/the-image-was-off-calm-lynx.md §3 W10
--                 docs/plans/per-tenant-trust-hub-cnam.md §III W10
--
-- Background. Today, existing tenants (e.g. suite 94b89098 / Scott Painting
-- Services / +1 (448) 288-5386) have their phone number attached to the
-- SHARED master SHAKEN bundle as a manual smoke-test fix. Wave 1-7 ships
-- the per-tenant trust onboarding flow for NEW tenants. Wave 10 closes the
-- loop: a one-shot admin endpoint that migrates existing tenants from the
-- shared SHAKEN bundle to their per-tenant SHAKEN/CNAM bundles.
--
-- HARD ORDERING RULE. A number must NEVER be detached from the shared
-- SHAKEN until the per-tenant SHAKEN reaches `twilio-approved`. Otherwise
-- the tenant has a window of zero attestation and calls drop to "Spam
-- Likely" until the new SHAKEN approves (~24h). The state machine
-- (`workers/trust_onboarding/backfill_state_machine.py`) enforces this
-- ordering; this migration only adds the audit columns + batch ledger.
--
-- Two structural changes:
--   1. tenant_trust_profiles.is_backfill BOOLEAN — flags rows created by
--      the W10 backfill flow vs the regular W3 KYB intake flow. Used by
--      ops dashboard to surface backfill-progress separately.
--   2. tenant_trust_backfill_batches — admin batch ledger. One row per
--      `POST /v1/admin/trust-hub/batch-backfill` call. Tracks dry_run,
--      enqueued/completed/failed counts, started_by_admin actor.
--
-- RLS FORCED on the new table (Law #6). Same pattern as
-- tenant_phone_swaps (migration 117).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Part 1 — is_backfill flag on tenant_trust_profiles
-- -----------------------------------------------------------------------------

ALTER TABLE public.tenant_trust_profiles
    ADD COLUMN IF NOT EXISTS is_backfill BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN public.tenant_trust_profiles.is_backfill IS
    'TRUE when this trust profile was created by the W10 admin backfill '
    'flow (existing tenant migrating from the shared master SHAKEN bundle '
    'to per-tenant SHAKEN+CNAM). FALSE for normal W3 KYB-intake creation. '
    'Ops dashboard filters on (is_backfill, trust_state) to surface '
    'backfill-cohort progress vs new-signup cohort progress separately.';

-- Index for ops dashboard filter `WHERE is_backfill=true AND trust_state=...`
CREATE INDEX IF NOT EXISTS idx_ttp_is_backfill
    ON public.tenant_trust_profiles (is_backfill, trust_state);

-- -----------------------------------------------------------------------------
-- Part 2 — tenant_trust_backfill_batches (admin batch ledger)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tenant_trust_backfill_batches (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Audit: who started the batch (actor_id from admin JWT subject)
    started_by_admin    TEXT        NOT NULL,

    -- The list of suite_ids the batch was launched against. JSONB array
    -- of UUID strings: ["94b89098-...", "abcdef01-..."]. Hard cap of 100
    -- suites per batch is enforced at the route layer.
    suite_ids           JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- Lifecycle status (closed enum)
    status              TEXT        NOT NULL DEFAULT 'pending'
                                    CHECK (status IN (
                                        'pending',
                                        'in_progress',
                                        'completed',
                                        'failed'
                                    )),

    -- Dry run flag — when TRUE, route returned the plan without enqueuing
    -- ARQ jobs. The row is still inserted for audit (so admins can see
    -- what they previewed).
    dry_run             BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Counts. enqueued_count is set at admission time; completed/failed
    -- are updated as backfill jobs terminate.
    enqueued_count      INTEGER     NOT NULL DEFAULT 0,
    completed_count     INTEGER     NOT NULL DEFAULT 0,
    failed_count        INTEGER     NOT NULL DEFAULT 0,

    -- Throttle interval used (seconds between ARQ enqueues to avoid
    -- Twilio rate limits). Recorded for audit + replay reasoning.
    throttle_seconds    INTEGER     NOT NULL DEFAULT 30
                                    CHECK (throttle_seconds BETWEEN 0 AND 3600),

    -- Suites that were skipped (already onboarded, missing prereqs, etc.).
    -- JSONB array of {"suite_id": "...", "reason": "..."} objects.
    skipped             JSONB       NOT NULL DEFAULT '[]'::jsonb,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_ttbb_status_created
    ON public.tenant_trust_backfill_batches (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ttbb_admin_created
    ON public.tenant_trust_backfill_batches (started_by_admin, created_at DESC);

-- updated_at trigger (mirrors moddatetime usage on tenant_phone_swaps)
DROP TRIGGER IF EXISTS trg_tenant_trust_backfill_batches_updated_at
    ON public.tenant_trust_backfill_batches;
CREATE TRIGGER trg_tenant_trust_backfill_batches_updated_at
    BEFORE UPDATE ON public.tenant_trust_backfill_batches
    FOR EACH ROW EXECUTE FUNCTION moddatetime(updated_at);

-- -----------------------------------------------------------------------------
-- RLS FORCED — Law #6 tenant isolation. Admin batch ledger is service_role
-- only at write time. authenticated users CANNOT read other admins' batches;
-- the admin dashboard endpoint uses service_role and gates with the
-- ASPIRE_ADMIN_JWT_SECRET-validated JWT at the route layer.
-- -----------------------------------------------------------------------------

ALTER TABLE public.tenant_trust_backfill_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_trust_backfill_batches FORCE  ROW LEVEL SECURITY;

-- service_role full access (worker + admin route)
DROP POLICY IF EXISTS tenant_trust_backfill_batches_service_role_all
    ON public.tenant_trust_backfill_batches;
CREATE POLICY tenant_trust_backfill_batches_service_role_all
    ON public.tenant_trust_backfill_batches
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- authenticated: NO access. This is an internal ops table — admins read
-- via the orchestrator's GET /v1/admin/trust-hub/dashboard endpoint
-- (gated by ASPIRE_ADMIN_JWT_SECRET, runs under service_role).
GRANT ALL ON public.tenant_trust_backfill_batches TO service_role;

COMMENT ON TABLE public.tenant_trust_backfill_batches IS
    'W10 admin batch backfill ledger. One row per POST /v1/admin/trust-hub/'
    'batch-backfill call. Used by ops dashboard to track backfill cohort '
    'progress at 10k tenants. dry_run=true rows are audit-only (no ARQ '
    'jobs enqueued). RLS service_role-only — no authenticated access.';

COMMENT ON COLUMN public.tenant_trust_backfill_batches.suite_ids IS
    'JSONB array of UUID strings — the suites the batch was launched '
    'against. Hard-capped at 100 suites per batch (enforced at route). '
    'Used together with skipped[] to derive the actual ARQ-enqueued list.';

COMMENT ON COLUMN public.tenant_trust_backfill_batches.skipped IS
    'JSONB array of {suite_id, reason} objects describing suites that '
    'were filtered out before enqueue (e.g. "already_onboarded", '
    '"no_active_phone_number"). Surfaced in the route response so the '
    'admin sees exactly which suites did NOT get a backfill job.';

COMMENT ON COLUMN public.tenant_trust_backfill_batches.throttle_seconds IS
    'Delay between ARQ job enqueues for the same batch. Default 30s '
    '(below Twilio Trust Hub 10/sec rate limit with safety margin). '
    'Capped at 3600 (1h) to keep batches finite.';
