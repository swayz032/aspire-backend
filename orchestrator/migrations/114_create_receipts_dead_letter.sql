-- Migration 114: Create receipts_dead_letter table
-- Wave W1, INC-2026-05-07-001 — Receipt Flusher Poison-Pill Fix
--
-- Purpose: Provides a durable store for receipt rows that fail to persist
-- after MAX_FLUSH_ATTEMPTS retries. These are NOT lost — they live in the
-- in-memory receipt store and this table is an ops recovery audit trail.
--
-- Risk Tier: RED — applies to production schema.
-- Authority Gate: Founder sign-off required before applying to main project.
-- Apply to a Supabase branch first: mcp__supabase__create_branch + apply_migration.
--
-- Idempotency: IF NOT EXISTS guards make this safe to re-apply.

-- UP -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS receipts_dead_letter (
    dead_letter_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_id         UUID        NOT NULL,
    original_payload   JSONB       NOT NULL,
    failure_reason     TEXT        NOT NULL,
    failure_count      INT         NOT NULL,
    first_failed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_failed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    suite_id           UUID
);

COMMENT ON TABLE receipts_dead_letter IS
    'Receipts that exhausted all flush retry attempts. '
    'Not lost — also present in in-memory store. '
    'Ops recovery: re-insert original_payload into receipts table after root cause resolved.';

COMMENT ON COLUMN receipts_dead_letter.receipt_id IS
    'The receipt_id from the original receipts row (not a FK — receipts table has append-only trigger).';

COMMENT ON COLUMN receipts_dead_letter.original_payload IS
    'Full row dict that failed to insert into receipts. '
    'May contain redacted PII per DLP policy (Law #9).';

COMMENT ON COLUMN receipts_dead_letter.failure_reason IS
    'First 500 chars of the exception that caused the flush failure. No raw PII.';

CREATE INDEX IF NOT EXISTS receipts_dead_letter_suite_idx
    ON receipts_dead_letter(suite_id);

CREATE INDEX IF NOT EXISTS receipts_dead_letter_first_failed_at_idx
    ON receipts_dead_letter(first_failed_at);

CREATE INDEX IF NOT EXISTS receipts_dead_letter_receipt_id_idx
    ON receipts_dead_letter(receipt_id);

-- RLS: service_role bypasses RLS (used by the flusher).
-- Authenticated reads are scoped to the suite the row belongs to.
ALTER TABLE receipts_dead_letter ENABLE ROW LEVEL SECURITY;

CREATE POLICY receipts_dead_letter_service_role
    ON receipts_dead_letter
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Ops read access: authenticated users may only see their own suite's dead letters.
CREATE POLICY receipts_dead_letter_authenticated_read
    ON receipts_dead_letter
    FOR SELECT
    TO authenticated
    USING (suite_id = (current_setting('request.jwt.claims', true)::jsonb ->> 'suite_id')::uuid);

-- DOWN -----------------------------------------------------------------------
-- Run only after confirming all dead-letter rows have been recovered or acknowledged.
--
-- DROP TABLE IF EXISTS receipts_dead_letter;
