-- =============================================================================
-- Migration 103: Pass 14 expansion — contract + document + calendar_event types
-- =============================================================================
-- User-requested scope expansion of Pass 14: ensure contracts, document
-- uploads, and calendar events also flow into Office Memory.
--
-- Adds 3 new ingestion-driven types to memory_objects.memory_type CHECK:
--   contract       — PandaDoc contracts (signed/declined/voided/expired) —
--                    distinct from quote, since contract has different
--                    detail fields and is approval/legal-tracked.
--   document       — User-uploaded files (PDF / image / doc) via Aspire's
--                    own upload pipeline (NOT a third-party webhook). The
--                    route layer authenticates; adapter trusts the route.
--   calendar_event — Calendar events from BOTH sources:
--                      detail.calendar_source = "google" — Google Calendar
--                        push notifications + events.list pull
--                      detail.calendar_source = "aspire" — Aspire's own
--                        internal calendar (created/updated/deleted)
--                    Same memory_type → uniform Pass 15 detail UI + Pass 5
--                    search ranking. Origin is in detail field.
--
-- Total memory_types after this migration: 23 (14 original + 6 Pass 14 + 3 here).
--
-- Aspire Laws preserved:
--   Law #2 (Receipt for All)  — immutability trigger untouched.
--   Law #3 (Fail Closed)      — no DEFAULT values.
--   Law #6 (Tenant Isolation) — RLS forced; new types inherit.
--   Law #9 (Security)         — idempotency_key UNIQUE preserved.
--
-- Non-destructive: no data loss, idempotent, reversible.
-- =============================================================================

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.memory_objects'::regclass
          AND conname  = 'memory_objects_memory_type_check'
    ) THEN
        ALTER TABLE public.memory_objects DROP CONSTRAINT memory_objects_memory_type_check;
    END IF;
END $$;

ALTER TABLE public.memory_objects
    ADD CONSTRAINT memory_objects_memory_type_check
    CHECK (memory_type IN (
        -- Original 14 types (Pass 1)
        'session_summary','handoff_note','pending_intent','authority_context',
        'thread_summary','office_brief','finance_brief','decision_fact',
        'risk_flag','followup_task','timeline_event','artifact_reference',
        'receipt_reference','workflow_reference',
        -- Pass 14 ingestion (migration 101)
        'invoice','quote','call','meeting','transcript','sms_thread',
        -- Pass 14 expansion (THIS migration) — contracts, documents, calendar
        'contract','document','calendar_event'
    ));

-- Partial recency indexes for new ingestion-driven types so the search
-- ranker (Pass 5) doesn't seq-scan when filtering by these types.
CREATE INDEX IF NOT EXISTS idx_memory_objects_contract_recent
    ON public.memory_objects (tenant_id, suite_id, office_id, last_activity_at DESC)
    WHERE memory_type = 'contract';

CREATE INDEX IF NOT EXISTS idx_memory_objects_document_recent
    ON public.memory_objects (tenant_id, suite_id, office_id, last_activity_at DESC)
    WHERE memory_type = 'document';

-- Calendar uses event_at (not last_activity_at) since calendar events are
-- about WHEN something is/was scheduled, not when it was ingested.
CREATE INDEX IF NOT EXISTS idx_memory_objects_calendar_recent
    ON public.memory_objects (tenant_id, suite_id, office_id, event_at DESC)
    WHERE memory_type = 'calendar_event';

-- =============================================================================
-- Verification (manual / smoke):
--   SELECT pg_get_constraintdef(oid) FROM pg_constraint
--    WHERE conname = 'memory_objects_memory_type_check';
--   -- Should show 23 types in the IN clause.
--   INSERT INTO memory_objects (..., memory_type='contract', ...);    -- accepted
--   INSERT INTO memory_objects (..., memory_type='document', ...);    -- accepted
--   INSERT INTO memory_objects (..., memory_type='calendar_event', ...); -- accepted
-- =============================================================================
