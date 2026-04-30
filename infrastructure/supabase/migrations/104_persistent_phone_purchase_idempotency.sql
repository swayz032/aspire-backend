-- =============================================================================
-- Migration 104: Persistent phone-purchase idempotency (Pass 18+ Lane 2)
-- =============================================================================
-- Until now, phone-number purchase idempotency was an in-process Python dict
-- (`_idem_store` in services/twilio_provisioning.py). Every pod restart wiped
-- the cache, so a client retrying with the same idempotency_key after deploy
-- could trigger a second Twilio purchase ($1/mo waste + duplicate inventory).
--
-- This migration moves idempotency state to the durable system of record:
--   tenant_phone_numbers.purchase_idempotency_key
-- with a partial UNIQUE index keyed by (suite_id, purchase_idempotency_key)
-- so a duplicate replay either short-circuits with a SELECT match (fast path)
-- or is caught by INSERT conflict (race path).
--
-- Aspire Laws preserved:
--   Law #2 — every state change still cuts a receipt; idempotent replay
--            does not cut a duplicate receipt (it returns the original).
--   Law #3 — fail closed: lookup before any Twilio call.
--   Law #6 — partial UNIQUE keyed by suite_id (no cross-tenant collision).
--   Law #9 — purchase_idempotency_key is opaque hex; not PII.
--
-- Non-destructive: no data loss, idempotent (safe to re-run), reversible
-- (column is NULLable; existing rows leave it NULL).
-- =============================================================================

ALTER TABLE public.tenant_phone_numbers
    ADD COLUMN IF NOT EXISTS purchase_idempotency_key TEXT NULL;

COMMENT ON COLUMN public.tenant_phone_numbers.purchase_idempotency_key IS
    'Pass 18+ — client-supplied idempotency key for the purchase request. '
    'NULL for pre-Pass-18 rows. Scoped to suite_id via partial UNIQUE index.';

CREATE UNIQUE INDEX IF NOT EXISTS idx_tpn_idempotency_key
    ON public.tenant_phone_numbers (suite_id, purchase_idempotency_key)
    WHERE purchase_idempotency_key IS NOT NULL;

-- Verification queries (manual, NOT executed):
--   SELECT column_name, is_nullable
--     FROM information_schema.columns
--    WHERE table_name = 'tenant_phone_numbers'
--      AND column_name = 'purchase_idempotency_key';
--   SELECT indexname FROM pg_indexes
--    WHERE tablename = 'tenant_phone_numbers'
--      AND indexname = 'idx_tpn_idempotency_key';
