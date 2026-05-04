-- =============================================================================
-- Migration 117 — tenant_phone_swaps (W11 number-swap state table)
-- =============================================================================
-- Plan reference: docs/plans/per-tenant-trust-hub-cnam.md §III W11
--                 ~/.claude/plans/the-image-was-off-calm-lynx.md §3 W11
--
-- Background. Wave 11 (number-swap) introduced an 11-step state machine
-- that swaps a tenant's Aspire number for a new one while preserving the
-- existing Trust Hub bundles (Customer Profile + SHAKEN + CNAM). The
-- state machine and route both reference a `tenant_phone_swaps` table to
-- persist the swap-job lifecycle and the per-step `progress` JSONB used
-- for idempotency on worker restart.
--
-- Without this table the route's INSERT fails with "relation does not
-- exist" — every swap submission would 503 in production. The W11
-- implementation tests use mocked supabase_insert so this gap escaped
-- the suite. Caught during the route audit immediately after the W11
-- shipping commit (58054c5).
--
-- Schema mirrors the route's swap_row dict in twilio_swap.py:345-365
-- and the state-machine's _update_swap_status patches.
--
-- Status enum (closed):
--   pending          — created, ARQ job enqueued, not yet picked up
--   in_progress      — worker has started executing the 11-step machine
--   succeeded        — number_swap_complete receipt cut, tenant on new #
--   failed           — terminal: swap aborted before step 7 atomic switch
--                      OR rolled back after a step-7 failure (old # alive)
--   partial_success  — terminal: step 7 committed but post-switch detach
--                      failed; tenant on new #, old # cleanup pending
--                      via admin endpoint (graceful degradation)
--
-- RLS FORCED: same pattern as tenant_a2p_brands, tenant_trust_profiles.
--
-- Idempotency: UNIQUE (suite_id) WHERE status IN ('pending','in_progress')
-- prevents two concurrent swaps for the same tenant. Route relies on this
-- to surface 409 SWAP_ALREADY_IN_PROGRESS when a stuck swap exists.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.tenant_phone_swaps (
    id                          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID        NOT NULL,
    suite_id                    UUID        NOT NULL,
    office_id                   UUID        NULL,

    -- Number identifiers (old comes from tenant_phone_numbers FK; new is
    -- the freshly-purchased number — link to row populated post-step-2)
    old_phone_number_id         UUID        NOT NULL
                                            REFERENCES public.tenant_phone_numbers(id)
                                            ON DELETE RESTRICT,
    new_phone_number_id         UUID        NULL
                                            REFERENCES public.tenant_phone_numbers(id)
                                            ON DELETE SET NULL,
    new_number_e164             TEXT        NOT NULL
                                            CHECK (new_number_e164 ~ '^\+\d{7,15}$'),

    -- Swap options
    release_old_number          BOOLEAN     NOT NULL DEFAULT TRUE,

    -- Job state
    status                      TEXT        NOT NULL DEFAULT 'pending'
                                            CHECK (status IN (
                                                'pending',
                                                'in_progress',
                                                'succeeded',
                                                'failed',
                                                'partial_success'
                                            )),
    reason_code                 TEXT        NULL,

    -- Per-step idempotency map: { "step_1_initiated_receipt": "<uuid>",
    -- "step_2_new_phone_id": "<uuid>", "step_3_cp_attached": true, ... }
    -- See workers/trust_onboarding/swap_state_machine.py:106-128.
    progress                    JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Audit
    capability_token_id         TEXT        NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at                TIMESTAMPTZ NULL
);

-- Idempotency: one in-flight swap per tenant.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_phone_swaps_active
    ON public.tenant_phone_swaps (suite_id)
    WHERE status IN ('pending', 'in_progress');

-- Ops dashboard: recent swaps by status.
CREATE INDEX IF NOT EXISTS idx_tenant_phone_swaps_status_created
    ON public.tenant_phone_swaps (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tenant_phone_swaps_suite
    ON public.tenant_phone_swaps (suite_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tenant_phone_swaps_old_phone
    ON public.tenant_phone_swaps (old_phone_number_id);

-- updated_at trigger (mirrors the moddatetime pattern used elsewhere)
DROP TRIGGER IF EXISTS trg_tenant_phone_swaps_updated_at
    ON public.tenant_phone_swaps;
CREATE TRIGGER trg_tenant_phone_swaps_updated_at
    BEFORE UPDATE ON public.tenant_phone_swaps
    FOR EACH ROW EXECUTE FUNCTION moddatetime(updated_at);

-- =============================================================================
-- RLS FORCED — Law #6 tenant isolation. Same pattern as tenant_a2p_brands
-- and tenant_trust_profiles (migration 109/111).
-- =============================================================================

ALTER TABLE public.tenant_phone_swaps ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_phone_swaps FORCE  ROW LEVEL SECURITY;

-- authenticated: SELECT only their own suite. No INSERT/UPDATE/DELETE
-- via RLS — those are service_role-only (worker runs under service_role).
DROP POLICY IF EXISTS tenant_phone_swaps_authenticated_select
    ON public.tenant_phone_swaps;
CREATE POLICY tenant_phone_swaps_authenticated_select
    ON public.tenant_phone_swaps
    FOR SELECT
    TO authenticated
    USING (
        suite_id IN (
            SELECT tm.suite_id
            FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
    );

-- service_role: full access.
DROP POLICY IF EXISTS tenant_phone_swaps_service_role_all
    ON public.tenant_phone_swaps;
CREATE POLICY tenant_phone_swaps_service_role_all
    ON public.tenant_phone_swaps
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

GRANT SELECT ON public.tenant_phone_swaps TO authenticated;
GRANT ALL    ON public.tenant_phone_swaps TO service_role;

COMMENT ON TABLE public.tenant_phone_swaps IS
    'W11 number-swap job ledger. One in-flight swap per suite (UNIQUE on '
    'suite_id WHERE status IN (pending,in_progress)). progress JSONB '
    'stores per-step completion flags + Twilio SIDs for idempotency on '
    'worker restart. Status terminal values: succeeded, failed, '
    'partial_success (step-7 committed but post-switch detach failed; '
    'tenant has working new number, old-number cleanup is non-blocking).';

COMMENT ON COLUMN public.tenant_phone_swaps.progress IS
    'JSONB step-completion map keyed by step_<n>_<short_name>. Worker '
    'reads this on restart to skip already-completed steps. Receipt '
    'IDs for cut receipts are also persisted here so the worker does '
    'not double-cut. See workers/trust_onboarding/swap_state_machine.py '
    'lines 106-128 for the exact key vocabulary.';

COMMENT ON COLUMN public.tenant_phone_swaps.release_old_number IS
    'When TRUE (default), the old number is fully released back to '
    'Twilio at step 11 (DELETE IncomingPhoneNumber). When FALSE, the '
    'old number remains in the Twilio account but is detached from all '
    'Trust Hub bundles and marked status=released in tenant_phone_numbers.';
