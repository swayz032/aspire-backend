-- =============================================================================
-- Migration 121 — A2P brand capability_token_id audit column
-- =============================================================================
-- release-sre P0-3 follow-up (per-tenant-trust-hub-SHIP-VERDICT.md):
--   "Wire capability_token_id on A2P receipts" — Law #5 audit gap.
--
-- Background. Migration 111 created `tenant_a2p_brands` without a
-- capability_token_id column. The W7 OTP-verify path threads the cap
-- token through to receipts (post-W7-hardening commit 9fe3a72), but
-- every OTHER A2P transition (draft → pending, otp_confirmed →
-- brand_pending, brand_approved → campaign creation, _fail_brand)
-- writes receipts with capability_token_id=NULL.
--
-- We persist the cap token on the brand row at POST /v1/a2p/start
-- intake; every downstream state-machine transition reads it back from
-- the brand row when cutting receipts.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS via DO block.
-- =============================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM   information_schema.columns
        WHERE  table_schema = 'public'
          AND  table_name   = 'tenant_a2p_brands'
          AND  column_name  = 'capability_token_id'
    ) THEN
        ALTER TABLE public.tenant_a2p_brands
            ADD COLUMN capability_token_id TEXT NULL;

        COMMENT ON COLUMN public.tenant_a2p_brands.capability_token_id IS
            'Capability token ID (Yellow tier scope a2p:register) that '
            'authorized the original POST /v1/a2p/start. Threaded into '
            'every cut_trust_receipt() call from a2p_state_machine.py so '
            'the audit ledger (Law #5) can trace which token authorized '
            'each A2P state transition. NULL only on rows created before '
            'this migration.';
    END IF;
END $$;
