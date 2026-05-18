-- Wave 2.5: Blueprint Engine — thumbnail persistence + pipeline progress tracking
-- Plan: ~/.claude/plans/serene-seeking-hollerith.md §2.5
--
-- Adds two columns needed by the Wave 2.5 read APIs:
--   1. blueprint_sheets.thumbnail_url      — signed URL after thumbnail upload
--   2. blueprint_projects.stage_progress   — jsonb pipeline stage machine
--
-- RLS unchanged (inherits existing policies from migration 20260517000001).
-- Composite indexes added for the scoped lookups the new GET endpoints do.
-- Reversible DOWN section at the bottom.

-- ──────────────────────────────────────────────────────────────────────────────
-- 1. thumbnail_url column on blueprint_sheets
-- ──────────────────────────────────────────────────────────────────────────────
alter table public.blueprint_sheets
  add column if not exists thumbnail_url text;

comment on column public.blueprint_sheets.thumbnail_url
  is 'Supabase Storage signed URL (7-day expiry). Null if thumbnail upload failed.';

-- ──────────────────────────────────────────────────────────────────────────────
-- 2. stage_progress column on blueprint_projects
-- ──────────────────────────────────────────────────────────────────────────────
alter table public.blueprint_projects
  add column if not exists stage_progress jsonb
  not null
  default '{"ingest":"not_started","classify":"not_started","see":"not_started","reason":"not_started","procure":"not_started"}'::jsonb;

comment on column public.blueprint_projects.stage_progress
  is 'Pipeline stage machine. Values per stage: not_started | in_progress | done | failed.';

-- ──────────────────────────────────────────────────────────────────────────────
-- 3. Composite indexes for RLS-scoped lookups
--    (suite_id, id) pattern matches how the new GET endpoints filter rows)
-- ──────────────────────────────────────────────────────────────────────────────
create index if not exists idx_blueprint_projects_suite_id
  on public.blueprint_projects (suite_id, id);

create index if not exists idx_blueprint_sheets_suite_id
  on public.blueprint_sheets (suite_id, id);

-- ──────────────────────────────────────────────────────────────────────────────
-- DOWN (manual; do not run automatically)
-- ──────────────────────────────────────────────────────────────────────────────
-- drop index if exists idx_blueprint_sheets_suite_id;
-- drop index if exists idx_blueprint_projects_suite_id;
-- alter table public.blueprint_projects drop column if exists stage_progress;
-- alter table public.blueprint_sheets drop column if exists thumbnail_url;
