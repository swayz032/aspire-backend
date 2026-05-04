-- =============================================================================
-- Migration 112 — Add trust_profile_id FK to tenant_phone_numbers
-- =============================================================================
-- Plan: docs/plans/per-tenant-trust-hub-cnam.md, Wave 1-D
-- Slim: ~/.claude/plans/the-image-was-off-calm-lynx.md §3 W1
--
-- Background. After per-tenant Trust Hub onboarding completes (W5), each
-- phone number is bound to a specific tenant_trust_profiles row. NULL means
-- the number is on the SHARED master SHAKEN bundle (status quo for tenants
-- who haven't completed KYB yet — they remain there until W10 backfill).
--
-- ON DELETE SET NULL — if the trust profile is purged for any reason, the
-- number remains active and reverts to NULL (i.e., the shared profile).
-- =============================================================================

ALTER TABLE public.tenant_phone_numbers
    ADD COLUMN IF NOT EXISTS trust_profile_id UUID NULL
        REFERENCES public.tenant_trust_profiles(id)
        ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_tpn_trust_profile_id
    ON public.tenant_phone_numbers (trust_profile_id)
    WHERE trust_profile_id IS NOT NULL;

COMMENT ON COLUMN public.tenant_phone_numbers.trust_profile_id IS
    'FK to tenant_trust_profiles — populated after per-tenant trust onboarding '
    'completes (state_machine reaches `number_attached`). '
    'NULL means this number is still on the shared master SHAKEN bundle. '
    'Numbers stay NULL until W10 backfill migrates them to per-tenant bundles.';
