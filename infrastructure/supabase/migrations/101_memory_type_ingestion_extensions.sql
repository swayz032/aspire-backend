-- =============================================================================
-- Migration 101: Memory Type Ingestion Extensions
-- =============================================================================
-- Pass 14 of the Office Memory Engine plan (the-image-was-off-calm-lynx).
--
-- Extends `memory_objects.memory_type` CHECK constraint with 6 new types used
-- by the ingestion adapters that flow real business artifacts into Office
-- Memory:
--
--   `invoice`     — Stripe invoice events (created/paid/voided)
--   `quote`       — PandaDoc + internal quote events (sent/viewed/accepted)
--   `call`        — Twilio voice calls with recording + transcript
--   `meeting`     — Zoom meetings with recording + transcript
--   `transcript`  — Raw EL/Anam conversation transcripts (parents `session_summary`)
--   `sms_thread`  — Twilio SMS threads (one per contact per office)
--
-- Existing 14 types remain valid (session_summary, handoff_note, pending_intent,
-- authority_context, thread_summary, office_brief, finance_brief, decision_fact,
-- risk_flag, followup_task, timeline_event, artifact_reference, receipt_reference,
-- workflow_reference). Total after this migration: 20 types.
--
-- Aspire Laws preserved:
--   Law #2 (Receipt for All)  — immutability trigger on terminal status untouched
--   Law #3 (Fail Closed)      — RLS policies untouched
--   Law #6 (Tenant Isolation) — RLS forced; new types inherit
--   Law #9 (Security)         — idempotency_key UNIQUE untouched
--
-- This migration is non-destructive:
--   - No data loss (existing rows pass the new check too)
--   - Idempotent (drops only the named constraint before re-adding)
--   - Reversible (a follow-up could drop and re-add with the original 14 types)
-- =============================================================================

DO $$
BEGIN
    -- Drop the existing CHECK constraint if present.
    -- The constraint name follows Postgres' default naming for an inline CHECK
    -- on a column: "<table>_<column>_check".
    IF EXISTS (
        SELECT 1
        FROM   pg_constraint
        WHERE  conrelid = 'public.memory_objects'::regclass
          AND  conname  = 'memory_objects_memory_type_check'
    ) THEN
        ALTER TABLE public.memory_objects
            DROP CONSTRAINT memory_objects_memory_type_check;
    END IF;
END
$$;

-- Re-add the CHECK with the original 14 types + 6 new ingestion types.
ALTER TABLE public.memory_objects
    ADD CONSTRAINT memory_objects_memory_type_check
    CHECK (memory_type IN (
        -- Original Pass 1 set (preserved)
        'session_summary',
        'handoff_note',
        'pending_intent',
        'authority_context',
        'thread_summary',
        'office_brief',
        'finance_brief',
        'decision_fact',
        'risk_flag',
        'followup_task',
        'timeline_event',
        'artifact_reference',
        'receipt_reference',
        'workflow_reference',

        -- Pass 14 ingestion extensions
        'invoice',
        'quote',
        'call',
        'meeting',
        'transcript',
        'sms_thread'
    ));

-- =============================================================================
-- Performance: partial indexes for the ingestion-driven types so the search
-- ranker (Pass 5) doesn't sequential-scan when filtering by these high-volume
-- types. last_activity_at DESC matches the existing recency-first index pattern
-- on the broader table.
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_memory_objects_invoice_recent
    ON public.memory_objects (tenant_id, suite_id, office_id, last_activity_at DESC)
    WHERE memory_type = 'invoice';

CREATE INDEX IF NOT EXISTS idx_memory_objects_quote_recent
    ON public.memory_objects (tenant_id, suite_id, office_id, last_activity_at DESC)
    WHERE memory_type = 'quote';

CREATE INDEX IF NOT EXISTS idx_memory_objects_call_recent
    ON public.memory_objects (tenant_id, suite_id, office_id, last_activity_at DESC)
    WHERE memory_type = 'call';

CREATE INDEX IF NOT EXISTS idx_memory_objects_meeting_recent
    ON public.memory_objects (tenant_id, suite_id, office_id, last_activity_at DESC)
    WHERE memory_type = 'meeting';

CREATE INDEX IF NOT EXISTS idx_memory_objects_sms_thread_recent
    ON public.memory_objects (tenant_id, suite_id, office_id, last_activity_at DESC)
    WHERE memory_type = 'sms_thread';

-- transcript is read-by-link from session_summary, not browsed directly — no
-- dedicated index. Vector search + tsvector cover ad-hoc queries.

-- =============================================================================
-- Verification (manual / smoke):
--   SELECT count(*) FROM memory_objects;            -- existing rows still query
--   INSERT ... memory_type='invoice' ...;          -- accepted
--   INSERT ... memory_type='not_a_type' ...;       -- rejected (CHECK)
--   \d+ memory_objects                              -- confirms 20 enum members
-- =============================================================================
