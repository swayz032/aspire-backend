-- =============================================================================
-- Migration 107: SMS Thread State — is_pinned, is_archived, read_at
-- =============================================================================
-- Pass 19 Lane E1 (the-image-was-off-calm-lynx).
--
-- SMS threads live in memory_objects (memory_type='sms_thread').
-- This migration adds three state columns directly to memory_objects
-- for efficient filtering without JSONB extraction.
--
-- Columns added:
--   is_pinned    BOOLEAN DEFAULT false  — user pinned this thread
--   is_archived  BOOLEAN DEFAULT false  — user archived this thread
--   read_at      TIMESTAMPTZ NULL       — when this thread was last marked read
--
-- Aspire Laws:
--   Law #2 (Receipt for All) — state changes (pin/archive/read) cut receipts in routes.
--   Law #3 (Fail Closed)    — NULLable read_at (NULL = never read); booleans have safe defaults.
--   Law #6 (Tenant Isolation) — RLS already FORCED on memory_objects; these columns
--                                inherit existing policies without change.
-- =============================================================================

-- Add columns (idempotent via IF NOT EXISTS equivalent — ADD COLUMN IF NOT EXISTS)
ALTER TABLE public.memory_objects
    ADD COLUMN IF NOT EXISTS is_pinned   BOOLEAN     NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS is_archived BOOLEAN     NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS read_at     TIMESTAMPTZ NULL;

-- Partial index: fast lookup for pinned threads by tenant/suite/office
CREATE INDEX IF NOT EXISTS idx_mo_sms_thread_pinned
    ON public.memory_objects (tenant_id, suite_id, office_id, last_activity_at DESC)
    WHERE memory_type = 'sms_thread' AND is_pinned = true;

-- Partial index: fast lookup for archived threads
CREATE INDEX IF NOT EXISTS idx_mo_sms_thread_archived
    ON public.memory_objects (tenant_id, suite_id, office_id, last_activity_at DESC)
    WHERE memory_type = 'sms_thread' AND is_archived = true;

-- Partial index: fast lookup for unread threads (read_at IS NULL)
CREATE INDEX IF NOT EXISTS idx_mo_sms_thread_unread
    ON public.memory_objects (tenant_id, suite_id, office_id, last_activity_at DESC)
    WHERE memory_type = 'sms_thread' AND read_at IS NULL;

-- =============================================================================
-- Verification:
--   SELECT column_name, data_type, column_default, is_nullable
--   FROM information_schema.columns
--   WHERE table_name = 'memory_objects'
--     AND column_name IN ('is_pinned','is_archived','read_at');
--
--   SELECT COUNT(*) FROM memory_objects WHERE memory_type = 'sms_thread';
-- =============================================================================
