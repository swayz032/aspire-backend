-- =============================================================================
-- Migration 115 — Phone number release tracking (W11 number swap)
-- =============================================================================
-- Plan: docs/plans/per-tenant-trust-hub-cnam.md §III W11
-- Slim: ~/.claude/plans/the-image-was-off-calm-lynx.md §3 W11
--
-- Background. Wave 11 introduces tenant-initiated number swaps (POST
-- /v1/twilio/swap-number). When a swap completes, the old number is
-- detached from all 3 Trust Hub bundles, optionally released back to
-- Twilio, and marked status='released' in tenant_phone_numbers. We need
-- to track WHEN the release happened and WHY for audit + analytics.
--
-- The existing tenant_phone_numbers.status enum already includes
-- 'released' (verified via prior schema). This migration only adds two
-- columns + an index for ops queries that filter on recently-released
-- numbers (e.g., "show me all swaps in the last 30 days").
--
-- Failure-mode handling:
--   * tenant_swap       — voluntary swap by tenant (W11 endpoint)
--   * spam_flagged      — admin-initiated release after carrier flagging
--   * compliance_release — Twilio-initiated forced release
--   * admin_release      — manual ops release for any other reason
--
-- The CHECK constraint must reject any other reason — we want a closed
-- vocabulary so downstream analytics queries can rely on it.
-- =============================================================================

ALTER TABLE public.tenant_phone_numbers
    ADD COLUMN IF NOT EXISTS released_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS released_reason TEXT NULL;

-- Add the CHECK constraint only if not already present (idempotent re-run).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'tenant_phone_numbers_released_reason_check'
    ) THEN
        ALTER TABLE public.tenant_phone_numbers
            ADD CONSTRAINT tenant_phone_numbers_released_reason_check
            CHECK (
                released_reason IS NULL
                OR released_reason IN (
                    'tenant_swap',
                    'spam_flagged',
                    'compliance_release',
                    'admin_release'
                )
            );
    END IF;
END$$;

-- Index for ops dashboard queries: "released numbers in the last N days"
CREATE INDEX IF NOT EXISTS idx_tpn_status_released_at
    ON public.tenant_phone_numbers (status, released_at DESC)
    WHERE status = 'released';

COMMENT ON COLUMN public.tenant_phone_numbers.released_at IS
    'Timestamp when this phone number was released from the active pool. '
    'Set when status transitions to ''released''. NULL while number is active.';

COMMENT ON COLUMN public.tenant_phone_numbers.released_reason IS
    'Why this number was released. Values: tenant_swap (W11 voluntary swap), '
    'spam_flagged (admin release after carrier flagging), '
    'compliance_release (Twilio-initiated forced release), '
    'admin_release (manual ops release). '
    'Required NOT NULL when status=''released''; enforced by triggers in '
    'state machine code, NOT by a DB constraint (legacy ''released'' rows '
    'predate this column).';
