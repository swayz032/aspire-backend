-- =============================================================================
-- Migration 109: Receptionist Persona — per-tenant Sarah/Tiffany choice
-- =============================================================================
-- Adds a `receptionist_persona` column to front_desk_configs so each tenant
-- can pick which AI receptionist answers their inbound calls. The default is
-- 'sarah' so existing rows keep the current behavior.
--
-- The column is part of the versioned config row, so changing personas mints
-- a new version (Sarah v2 rule: changes apply to NEXT call, never in-flight).
--
-- The personas registry lives in code (services/receptionist_personas.py),
-- not in the DB — agent_id, voice_id, headshot_url, and preview_url are
-- stable, hand-curated strings. The DB only stores the chosen persona slug.
--
-- Aspire Laws:
--   Law #2  — column is part of versioned table; persona swaps emit
--             receptionist_persona_changed receipts (handled in route layer).
--   Law #3  — CHECK constraint rejects unknown personas at DB layer.
--   Law #6  — RLS already FORCED on front_desk_configs by migration 102.
-- =============================================================================

ALTER TABLE public.front_desk_configs
  ADD COLUMN IF NOT EXISTS receptionist_persona TEXT NOT NULL DEFAULT 'sarah'
    CHECK (receptionist_persona IN ('sarah','tiffany'));

COMMENT ON COLUMN public.front_desk_configs.receptionist_persona IS
  'Tenant''s chosen AI receptionist persona slug. Drives EL agent attachment + UI display name. Registry in services/receptionist_personas.py.';

-- Backfill: any pre-existing row (NULL would be impossible due to NOT NULL +
-- DEFAULT, but explicit no-op so nothing surprises us).
UPDATE public.front_desk_configs
   SET receptionist_persona = 'sarah'
 WHERE receptionist_persona IS NULL;

-- Index: persona-filtered queries are rare, but keep one for analytics
-- ("how many tenants picked Tiffany?") and so the planner picks index scans
-- on single-persona dashboards.
CREATE INDEX IF NOT EXISTS idx_fdc_receptionist_persona
  ON public.front_desk_configs (receptionist_persona);
