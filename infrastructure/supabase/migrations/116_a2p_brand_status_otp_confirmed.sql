-- =============================================================================
-- Migration 116 — Add `otp_confirmed` to tenant_a2p_brands.brand_status CHECK
-- =============================================================================
-- Plan reference: aspire-policy-gate W7-L2 (BLOCKING runtime defect)
--
-- Background. Migration 111 created `tenant_a2p_brands.brand_status` with a
-- closed enum CHECK: {'draft','pending','approved','rejected','suspended'}.
-- The W7 state machine (`a2p_state_machine.py:_transition_otp_pending`)
-- writes `brand_status='otp_confirmed'` after the user submits a correct
-- OTP — that is, the value is NOT in the original enum. Every Sole Prop
-- OTP confirmation in production would have failed with a CHECK violation
-- ("violates check constraint tenant_a2p_brands_brand_status_check"),
-- leaving the tenant stuck at brand_status='pending' with no path forward
-- since the state machine cannot record the OTP success.
--
-- This migration drops the legacy constraint and re-adds it with the full
-- 6-state vocabulary the W7 state machine actually uses.
--
-- Idempotent: uses IF EXISTS / DO blocks so re-running is safe.
-- =============================================================================

DO $$
BEGIN
    -- Drop the old constraint if it exists with the original 5-state vocab.
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'tenant_a2p_brands_brand_status_check'
          AND conrelid = 'public.tenant_a2p_brands'::regclass
    ) THEN
        ALTER TABLE public.tenant_a2p_brands
            DROP CONSTRAINT tenant_a2p_brands_brand_status_check;
    END IF;

    -- Re-add with the complete 6-state vocabulary used by the state machine.
    ALTER TABLE public.tenant_a2p_brands
        ADD CONSTRAINT tenant_a2p_brands_brand_status_check
        CHECK (brand_status IN (
            'draft',
            'pending',
            'otp_confirmed',
            'approved',
            'rejected',
            'suspended'
        ));
END$$;

COMMENT ON COLUMN public.tenant_a2p_brands.brand_status IS
    'A2P brand registration state. Values: draft (initial), pending (awaiting '
    'Twilio OTP), otp_confirmed (rep verified the OTP, awaiting Twilio brand '
    'review), approved (Twilio approved brand), rejected (Twilio rejected; '
    'tenant must re-submit), suspended (3+ failed OTP attempts, manual '
    'unlock required). Aligned with state machine in '
    'workers/trust_onboarding/a2p_state_machine.py.';
