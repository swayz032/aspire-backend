-- =============================================================================
-- Migration 114 — trust_state_transitions immutability trigger (W2 supporting)
-- =============================================================================
-- Plan: docs/plans/per-tenant-trust-hub-cnam.md, Wave 2 (audit-trail integrity)
-- Slim: ~/.claude/plans/the-image-was-off-calm-lynx.md §11 W2 verification
--
-- Background. The receipt-ledger-auditor (2026-05-04 W1 verification) flagged
-- that trust_state_transitions has RLS preventing authenticated writes but
-- NO database-level trigger preventing service_role from accidentally
-- UPDATEing or DELETEing audit rows. This is a defense-in-depth gap — the
-- existing receipts table uses an immutability trigger (trust_receipts_immutable);
-- we mirror that pattern here.
--
-- Note: W11's number-swap migration (released_at + released_reason columns
-- on tenant_phone_numbers) shifts to migration 115 to make room for this
-- W2 supporting migration.
-- =============================================================================

-- Immutability function: blocks all UPDATE / DELETE on trust_state_transitions
-- regardless of role. INSERT is the only allowed operation. Service-role
-- workers can correct mistakes by inserting compensating transitions, never
-- by mutating audit rows.
CREATE OR REPLACE FUNCTION public.trust_state_transitions_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            'trust_state_transitions is append-only — UPDATE blocked '
            '(violation by role %, attempted on row id %). Insert a compensating '
            'transition instead.',
            current_user, OLD.id
            USING ERRCODE = 'restrict_violation',
                  HINT = 'Trust audit ledger is immutable per Aspire Law #2 (receipts)';
    ELSIF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'trust_state_transitions is append-only — DELETE blocked '
            '(violation by role %, attempted on row id %). Audit rows must '
            'never be removed.',
            current_user, OLD.id
            USING ERRCODE = 'restrict_violation',
                  HINT = 'Trust audit ledger is immutable per Aspire Law #2 (receipts)';
    END IF;
    RETURN NULL;  -- AFTER triggers ignore return value
END;
$$;

REVOKE ALL ON FUNCTION public.trust_state_transitions_immutable() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.trust_state_transitions_immutable() TO authenticated, service_role;

DROP TRIGGER IF EXISTS trust_state_transitions_block_update ON public.trust_state_transitions;
CREATE TRIGGER trust_state_transitions_block_update
    BEFORE UPDATE ON public.trust_state_transitions
    FOR EACH ROW EXECUTE FUNCTION public.trust_state_transitions_immutable();

DROP TRIGGER IF EXISTS trust_state_transitions_block_delete ON public.trust_state_transitions;
CREATE TRIGGER trust_state_transitions_block_delete
    BEFORE DELETE ON public.trust_state_transitions
    FOR EACH ROW EXECUTE FUNCTION public.trust_state_transitions_immutable();

COMMENT ON FUNCTION public.trust_state_transitions_immutable() IS
    'Trigger function — blocks UPDATE and DELETE on trust_state_transitions '
    'for all roles (Law #2 audit ledger immutability). To correct a mistake, '
    'insert a compensating transition row.';
