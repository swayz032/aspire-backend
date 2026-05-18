-- Wave 3: SEE stage adds an engineer-seal flag to blueprint_sheets.
-- Plan: ~/.claude/plans/serene-seeking-hollerith.md §3
--
-- Drew's seal_detector flags a sheet when a P.E. seal is detected on it.
-- Stage 4 REASON reads this flag to upgrade project trust class
-- (engineer-stamped → permit-confirmed-adjacent).
--
-- RLS unchanged — inherits the existing blueprint_sheets policy.

alter table public.blueprint_sheets
  add column if not exists seal_detected boolean not null default false;

create index if not exists idx_blueprint_sheets_seal
  on public.blueprint_sheets (project_id, seal_detected)
  where seal_detected = true;

-- ──────────────────────────────────────────────────────────────────────────────
-- DOWN (manual)
-- ──────────────────────────────────────────────────────────────────────────────
-- drop index if exists public.idx_blueprint_sheets_seal;
-- alter table public.blueprint_sheets drop column if exists seal_detected;
