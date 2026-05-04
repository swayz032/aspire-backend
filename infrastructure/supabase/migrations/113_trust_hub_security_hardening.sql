-- =============================================================================
-- Migration 113 — Trust Hub Security Hardening (W1 patch)
-- =============================================================================
-- Plan: docs/plans/per-tenant-trust-hub-cnam.md, Wave 1 verification gate
-- Slim: ~/.claude/plans/the-image-was-off-calm-lynx.md §12 Gate 5 (Security)
--
-- Background. Wave 1 schema (migrations 109-112) shipped with `FOR ALL TO
-- authenticated` policies that allow tenants to UPDATE their own rows.
-- security-reviewer's adversarial audit (2026-05-04) found two critical
-- exploit paths and one operational gap that block Wave 2:
--
--   THREAT-001 (CRITICAL) — Cross-tenant rep injection: an authenticated
--     tenant can INSERT a tenant_authorized_reps row with their own
--     tenant_id but pointing to another tenant's trust_profile_id. The
--     worker would then submit that injected rep to Twilio under the
--     wrong profile, contaminating the victim's KYB.
--
--   THREAT-003 (HIGH) — State machine bypass: with FOR ALL, an authenticated
--     tenant can UPDATE their own trust_state to cnam_approved /
--     number_attached without any Twilio approval, breaking the audit chain
--     and confusing the worker's idempotency checks.
--
--   THREAT-002 (HIGH) — Missing GRANT statements; pure operational
--     correctness / auditability gap.
--
--   THREAT-008 / R-008 (MEDIUM) — dispute_count is unbounded; route layer
--     should cap but DB constraint is defense-in-depth.
--
-- This migration is the schema-layer remediation. Worker-layer remediations
-- (vault secret naming, dangling secret cleanup, PII redaction in receipts)
-- are documented as Wave 2 implementation mandates.
--
-- W11's number-swap schema (was planned as 113) shifts to 114.
-- =============================================================================


-- =============================================================================
-- R-001 — Replace FOR ALL with FOR SELECT for authenticated on 5 writable tables
-- =============================================================================
-- All writes flow through service_role (the FastAPI orchestrator's
-- supabase_client uses the service-role key; routes validate capability
-- tokens then call supabase_insert/update/delete which run as service_role).
-- Authenticated tenants only need SELECT to read their own onboarding state
-- for the W8 status dashboard.
--
-- IMPORTANT: trust_state_transitions already had SELECT-only — no change.

-- 1. tenant_trust_profiles
DROP POLICY IF EXISTS ttp_tenant_isolation ON public.tenant_trust_profiles;
CREATE POLICY ttp_tenant_select ON public.tenant_trust_profiles
    FOR SELECT TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

-- 2. tenant_authorized_reps
DROP POLICY IF EXISTS tar_tenant_isolation ON public.tenant_authorized_reps;
CREATE POLICY tar_tenant_select ON public.tenant_authorized_reps
    FOR SELECT TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

-- 3. tenant_cnam_records
DROP POLICY IF EXISTS tcr_tenant_isolation ON public.tenant_cnam_records;
CREATE POLICY tcr_tenant_select ON public.tenant_cnam_records
    FOR SELECT TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

-- 4. tenant_a2p_brands
DROP POLICY IF EXISTS tab_tenant_isolation ON public.tenant_a2p_brands;
CREATE POLICY tab_tenant_select ON public.tenant_a2p_brands
    FOR SELECT TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

-- 5. tenant_a2p_campaigns
DROP POLICY IF EXISTS tac_tenant_isolation ON public.tenant_a2p_campaigns;
CREATE POLICY tac_tenant_select ON public.tenant_a2p_campaigns
    FOR SELECT TO authenticated
    USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true));

-- service_role policies created in 109/110/111 remain unchanged (FOR ALL).


-- =============================================================================
-- R-002 — FK tenant-coherence triggers (block cross-tenant injection)
-- =============================================================================
-- For each child table with both `tenant_id` AND a FK to a parent table,
-- enforce that the child's tenant_id matches the parent's tenant_id. This
-- closes the gap where RLS validates only the inserter's tenant_id but the
-- FK target could belong to a different tenant.
--
-- Triggers run for ALL roles including service_role — this is intentional
-- defense-in-depth against application-layer bugs in the worker.
--
-- SECURITY DEFINER on the helper function so it can read the parent table
-- regardless of the calling role's RLS context. The function ONLY reads
-- the parent's tenant_id for comparison; no data is leaked.

-- Helper function: tenant_authorized_reps -> tenant_trust_profiles
CREATE OR REPLACE FUNCTION public.validate_authorized_rep_tenant_coherence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    profile_tenant_id UUID;
BEGIN
    SELECT tenant_id INTO profile_tenant_id
    FROM public.tenant_trust_profiles
    WHERE id = NEW.trust_profile_id;

    IF profile_tenant_id IS NULL THEN
        RAISE EXCEPTION
            'tenant_authorized_reps: trust_profile_id % not found',
            NEW.trust_profile_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF profile_tenant_id != NEW.tenant_id THEN
        RAISE EXCEPTION
            'tenant_authorized_reps: tenant_id mismatch — rep.tenant_id=% vs trust_profile.tenant_id=% (Law #6 cross-tenant injection blocked)',
            NEW.tenant_id, profile_tenant_id
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.validate_authorized_rep_tenant_coherence() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.validate_authorized_rep_tenant_coherence() TO authenticated, service_role;

DROP TRIGGER IF EXISTS validate_tar_tenant_coherence ON public.tenant_authorized_reps;
CREATE TRIGGER validate_tar_tenant_coherence
    BEFORE INSERT OR UPDATE OF trust_profile_id, tenant_id
    ON public.tenant_authorized_reps
    FOR EACH ROW EXECUTE FUNCTION public.validate_authorized_rep_tenant_coherence();

-- Helper function: tenant_cnam_records -> tenant_trust_profiles
CREATE OR REPLACE FUNCTION public.validate_cnam_record_tenant_coherence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    profile_tenant_id UUID;
    phone_tenant_id UUID;
BEGIN
    -- Validate trust_profile.tenant_id matches
    SELECT tenant_id INTO profile_tenant_id
    FROM public.tenant_trust_profiles
    WHERE id = NEW.trust_profile_id;

    IF profile_tenant_id IS NULL THEN
        RAISE EXCEPTION
            'tenant_cnam_records: trust_profile_id % not found',
            NEW.trust_profile_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF profile_tenant_id != NEW.tenant_id THEN
        RAISE EXCEPTION
            'tenant_cnam_records: tenant_id mismatch with trust_profile (cnam.tenant_id=% vs profile.tenant_id=%)',
            NEW.tenant_id, profile_tenant_id
            USING ERRCODE = 'check_violation';
    END IF;

    -- Validate phone_number.suite_id matches (tenant_phone_numbers has suite_id, not tenant_id)
    SELECT
        (SELECT tenant_id FROM public.tenant_trust_profiles WHERE suite_id = tpn.suite_id LIMIT 1)
    INTO phone_tenant_id
    FROM public.tenant_phone_numbers tpn
    WHERE tpn.id = NEW.phone_number_id;

    -- If we found a phone_tenant_id, verify it matches; if NULL means no profile yet for the
    -- phone's suite, allow (the cnam record might be created before/with the profile).
    IF phone_tenant_id IS NOT NULL AND phone_tenant_id != NEW.tenant_id THEN
        RAISE EXCEPTION
            'tenant_cnam_records: tenant_id mismatch with phone_number (cnam.tenant_id=% vs phone.suite tenant=%)',
            NEW.tenant_id, phone_tenant_id
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.validate_cnam_record_tenant_coherence() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.validate_cnam_record_tenant_coherence() TO authenticated, service_role;

DROP TRIGGER IF EXISTS validate_tcr_tenant_coherence ON public.tenant_cnam_records;
CREATE TRIGGER validate_tcr_tenant_coherence
    BEFORE INSERT OR UPDATE OF trust_profile_id, phone_number_id, tenant_id
    ON public.tenant_cnam_records
    FOR EACH ROW EXECUTE FUNCTION public.validate_cnam_record_tenant_coherence();

-- Helper function: tenant_a2p_campaigns -> tenant_a2p_brands
CREATE OR REPLACE FUNCTION public.validate_a2p_campaign_tenant_coherence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    brand_tenant_id UUID;
BEGIN
    SELECT tenant_id INTO brand_tenant_id
    FROM public.tenant_a2p_brands
    WHERE id = NEW.brand_id;

    IF brand_tenant_id IS NULL THEN
        RAISE EXCEPTION
            'tenant_a2p_campaigns: brand_id % not found',
            NEW.brand_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF brand_tenant_id != NEW.tenant_id THEN
        RAISE EXCEPTION
            'tenant_a2p_campaigns: tenant_id mismatch — campaign.tenant_id=% vs brand.tenant_id=%',
            NEW.tenant_id, brand_tenant_id
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.validate_a2p_campaign_tenant_coherence() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.validate_a2p_campaign_tenant_coherence() TO authenticated, service_role;

DROP TRIGGER IF EXISTS validate_tac_tenant_coherence ON public.tenant_a2p_campaigns;
CREATE TRIGGER validate_tac_tenant_coherence
    BEFORE INSERT OR UPDATE OF brand_id, tenant_id
    ON public.tenant_a2p_campaigns
    FOR EACH ROW EXECUTE FUNCTION public.validate_a2p_campaign_tenant_coherence();

-- Helper function: trust_state_transitions -> tenant_trust_profiles
-- Same pattern; service_role is the only writer but defense-in-depth applies.
CREATE OR REPLACE FUNCTION public.validate_state_transition_tenant_coherence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    profile_tenant_id UUID;
BEGIN
    SELECT tenant_id INTO profile_tenant_id
    FROM public.tenant_trust_profiles
    WHERE id = NEW.trust_profile_id;

    IF profile_tenant_id IS NULL THEN
        RAISE EXCEPTION
            'trust_state_transitions: trust_profile_id % not found',
            NEW.trust_profile_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF profile_tenant_id != NEW.tenant_id THEN
        RAISE EXCEPTION
            'trust_state_transitions: tenant_id mismatch — transition.tenant_id=% vs profile.tenant_id=%',
            NEW.tenant_id, profile_tenant_id
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.validate_state_transition_tenant_coherence() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.validate_state_transition_tenant_coherence() TO authenticated, service_role;

DROP TRIGGER IF EXISTS validate_tst_tenant_coherence ON public.trust_state_transitions;
CREATE TRIGGER validate_tst_tenant_coherence
    BEFORE INSERT OR UPDATE OF trust_profile_id, tenant_id
    ON public.trust_state_transitions
    FOR EACH ROW EXECUTE FUNCTION public.validate_state_transition_tenant_coherence();


-- =============================================================================
-- Hash-chain receipt-id type fix (receipt-ledger-auditor finding)
-- =============================================================================
-- store_receipts_strict() returns None; worker cannot retrieve the assigned
-- UUID (receipts.id) post-insert to populate trust_state_transitions.receipt_id.
-- Resolution: use receipts.receipt_id (TEXT) which the worker pre-generates
-- and controls. ALTER columns from UUID to TEXT. Tables are empty (Wave 1
-- just applied) so the conversion is trivial.
ALTER TABLE public.trust_state_transitions
    ALTER COLUMN receipt_id TYPE TEXT USING receipt_id::TEXT,
    ALTER COLUMN previous_receipt_id TYPE TEXT USING previous_receipt_id::TEXT;

COMMENT ON COLUMN public.trust_state_transitions.receipt_id IS
    'TEXT-form receipts.receipt_id (worker-generated). NOT receipts.id (UUID PK). '
    'Worker pre-generates the receipt_id, passes it to store_receipts_strict, '
    'and writes the same value here for hash-chain linkage.';

COMMENT ON COLUMN public.trust_state_transitions.previous_receipt_id IS
    'Previous transition''s receipt_id (TEXT). Builds the per-tenant hash chain '
    'within trust_state_transitions for compliance audit (Law #2).';


-- =============================================================================
-- R-008 — dispute_count CHECK constraint (defense-in-depth, route caps too)
-- =============================================================================
-- Cap at 10 to prevent integer overflow attacks or accidental loops.
-- The KYB resubmit route (W3-A) caps at 5 in the application layer;
-- this DB constraint is the wider safety net.

ALTER TABLE public.tenant_trust_profiles
    DROP CONSTRAINT IF EXISTS ttp_dispute_count_capped;
ALTER TABLE public.tenant_trust_profiles
    ADD CONSTRAINT ttp_dispute_count_capped
    CHECK (dispute_count >= 0 AND dispute_count <= 10);


-- =============================================================================
-- R-003 — Explicit GRANTs (operational correctness + audit clarity)
-- =============================================================================
-- Authenticated: SELECT only (RLS further restricts to own tenant)
-- service_role: ALL (worker is sole writer)

GRANT SELECT ON public.tenant_trust_profiles TO authenticated;
GRANT ALL ON public.tenant_trust_profiles TO service_role;

GRANT SELECT ON public.tenant_authorized_reps TO authenticated;
GRANT ALL ON public.tenant_authorized_reps TO service_role;

GRANT SELECT ON public.tenant_cnam_records TO authenticated;
GRANT ALL ON public.tenant_cnam_records TO service_role;

GRANT SELECT ON public.trust_state_transitions TO authenticated;
GRANT ALL ON public.trust_state_transitions TO service_role;

GRANT SELECT ON public.tenant_a2p_brands TO authenticated;
GRANT ALL ON public.tenant_a2p_brands TO service_role;

GRANT SELECT ON public.tenant_a2p_campaigns TO authenticated;
GRANT ALL ON public.tenant_a2p_campaigns TO service_role;


-- =============================================================================
-- Documentation comments
-- =============================================================================

COMMENT ON FUNCTION public.validate_authorized_rep_tenant_coherence() IS
    'Trigger function: validates tenant_authorized_reps.tenant_id matches '
    'tenant_trust_profiles.tenant_id (cross-tenant injection block, R-002 / THREAT-001).';

COMMENT ON FUNCTION public.validate_cnam_record_tenant_coherence() IS
    'Trigger function: validates tenant_cnam_records ties to consistent tenant '
    'across trust_profile_id and phone_number_id (R-002).';

COMMENT ON FUNCTION public.validate_a2p_campaign_tenant_coherence() IS
    'Trigger function: validates campaign.tenant_id matches brand.tenant_id (R-002).';

COMMENT ON FUNCTION public.validate_state_transition_tenant_coherence() IS
    'Trigger function: validates audit-ledger row tenant_id matches profile (R-002).';
