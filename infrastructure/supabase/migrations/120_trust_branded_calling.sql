-- =============================================================================
-- Migration 120 — Branded Calling column for tenant_trust_profiles
-- =============================================================================
-- Wave 6 (W6): Twilio Branded Calling private-beta enrollment scaffolding.
--
-- Background. Migration 109 already added `branded_calling_enabled` (BOOLEAN
-- DEFAULT FALSE) and `branded_calling_display_name` (TEXT NULL) to
-- tenant_trust_profiles, and included 'branded_calling_pending' and
-- 'branded_calling_live' in the trust_state CHECK constraint.
--
-- This migration adds only the ONE column that W6 introduces:
--   twilio_branded_calling_sid  — the SID returned by Twilio's private-beta
--                                  Branded Calling enrollment POST. Used by:
--     * state_machine._transition_number_attached  (idempotency guard)
--     * routes/trust_hub.status_callback           (DB lookup by SID)
--     * W9 reputation cron                         (poll enrollment status)
--
-- Idempotency: uses IF NOT EXISTS / DO $$ EXCEPTION WHEN duplicate_column …
-- so the migration is safe to re-run.
--
-- All other branded-calling columns (branded_calling_enabled,
-- branded_calling_display_name) already exist from migration 109 — do NOT
-- re-add them.
-- =============================================================================

-- Add twilio_branded_calling_sid (idempotent — safe on re-run)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM   information_schema.columns
        WHERE  table_schema = 'public'
          AND  table_name   = 'tenant_trust_profiles'
          AND  column_name  = 'twilio_branded_calling_sid'
    ) THEN
        ALTER TABLE public.tenant_trust_profiles
            ADD COLUMN twilio_branded_calling_sid TEXT NULL;

        COMMENT ON COLUMN public.tenant_trust_profiles.twilio_branded_calling_sid IS
            'SID returned by Twilio Branded Calling private-beta enrollment POST. '
            'Non-PII (Twilio SID). Used by status_callback route to route Twilio '
            'webhooks back to the correct trust profile. Set during '
            '_transition_number_attached when BRANDED_CALLING_ENABLED=true.';
    END IF;
END $$;

-- Index for fast status-callback lookup by this SID
CREATE INDEX IF NOT EXISTS idx_ttp_twilio_branded_calling_sid
    ON public.tenant_trust_profiles (twilio_branded_calling_sid)
    WHERE twilio_branded_calling_sid IS NOT NULL;
