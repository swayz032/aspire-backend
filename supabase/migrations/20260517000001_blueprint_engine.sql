-- Wave 1A: Blueprint Story Engine — Drew skeleton (append-only state)
-- Plan: ~/.claude/plans/serene-seeking-hollerith.md §1
--
-- 7 tables backing Drew's blueprint pipeline (ingest → classify → see → reason → procure).
-- Every row is tenant-scoped via `suite_id` and enforced by RLS using the
-- `app.current_suite_id` session GUC (matches ≥10 prior migrations).
--
-- Append-only semantics: corrections are NEW rows that reference the prior row via
-- `supersedes_id`. No UPDATE/DELETE in app code — supports Law #2 (immutable audit).

-- ──────────────────────────────────────────────────────────────────────────────
-- Enums
-- ──────────────────────────────────────────────────────────────────────────────
create type truth_class as enum (
  'observed',
  'derived',
  'assumed',
  'field_confirmed',
  'vendor_confirmed',
  'permit_confirmed'
);

create type tariff_flag as enum (
  'section_232_steel',
  'section_232_aluminum',
  'softwood_lumber',
  'none'
);

create type discipline as enum (
  'A','S','M','E','P','FP','C','L','Specs','Schedules','Addenda'
);

-- ──────────────────────────────────────────────────────────────────────────────
-- 1. blueprint_projects
-- ──────────────────────────────────────────────────────────────────────────────
create table public.blueprint_projects (
  id          uuid primary key default gen_random_uuid(),
  suite_id    uuid not null,
  office_id   uuid,
  address     text,
  created_at  timestamptz not null default now(),
  created_by  uuid
);

create index idx_blueprint_projects_suite on public.blueprint_projects (suite_id);

alter table public.blueprint_projects enable row level security;
create policy blueprint_projects_tenant_isolation on public.blueprint_projects
  for all using (suite_id = current_setting('app.current_suite_id')::uuid);

-- ──────────────────────────────────────────────────────────────────────────────
-- 2. blueprint_sheets
-- ──────────────────────────────────────────────────────────────────────────────
create table public.blueprint_sheets (
  id            uuid primary key default gen_random_uuid(),
  suite_id      uuid not null,
  office_id     uuid,
  project_id    uuid not null references public.blueprint_projects(id),
  sheet_number  text,
  discipline    discipline,
  scale         text,
  revision      text,
  supersedes_id uuid references public.blueprint_sheets(id),
  ocr_text      text,
  hash          text,
  created_at    timestamptz not null default now(),
  created_by    uuid
);

create index idx_blueprint_sheets_project on public.blueprint_sheets (project_id);
create index idx_blueprint_sheets_supersedes on public.blueprint_sheets (project_id, supersedes_id);

alter table public.blueprint_sheets enable row level security;
create policy blueprint_sheets_tenant_isolation on public.blueprint_sheets
  for all using (suite_id = current_setting('app.current_suite_id')::uuid);

-- ──────────────────────────────────────────────────────────────────────────────
-- 3. blueprint_symbols
-- ──────────────────────────────────────────────────────────────────────────────
create table public.blueprint_symbols (
  id          uuid primary key default gen_random_uuid(),
  suite_id    uuid not null,
  office_id   uuid,
  sheet_id    uuid not null references public.blueprint_sheets(id),
  class       text,
  bbox        jsonb,
  confidence  numeric,
  created_at  timestamptz not null default now(),
  created_by  uuid
);

create index idx_blueprint_symbols_sheet on public.blueprint_symbols (sheet_id);

alter table public.blueprint_symbols enable row level security;
create policy blueprint_symbols_tenant_isolation on public.blueprint_symbols
  for all using (suite_id = current_setting('app.current_suite_id')::uuid);

-- ──────────────────────────────────────────────────────────────────────────────
-- 4. blueprint_assemblies
-- ──────────────────────────────────────────────────────────────────────────────
create table public.blueprint_assemblies (
  id            uuid primary key default gen_random_uuid(),
  suite_id      uuid not null,
  office_id     uuid,
  project_id    uuid not null references public.blueprint_projects(id),
  type          text,
  quantity      numeric,
  unit          text,
  truth         truth_class,
  supersedes_id uuid references public.blueprint_assemblies(id),
  created_at    timestamptz not null default now(),
  created_by    uuid
);

create index idx_blueprint_assemblies_project on public.blueprint_assemblies (project_id);
create index idx_blueprint_assemblies_truth on public.blueprint_assemblies (project_id, truth);
create index idx_blueprint_assemblies_supersedes on public.blueprint_assemblies (project_id, supersedes_id);

alter table public.blueprint_assemblies enable row level security;
create policy blueprint_assemblies_tenant_isolation on public.blueprint_assemblies
  for all using (suite_id = current_setting('app.current_suite_id')::uuid);

-- ──────────────────────────────────────────────────────────────────────────────
-- 5. blueprint_materials
-- ──────────────────────────────────────────────────────────────────────────────
create table public.blueprint_materials (
  id            uuid primary key default gen_random_uuid(),
  suite_id      uuid not null,
  office_id     uuid,
  project_id    uuid not null references public.blueprint_projects(id),
  line_item     text,
  quantity      numeric,
  unit          text,
  truth         truth_class,
  tariff_flag   tariff_flag default 'none',
  supplier_id   text,
  supersedes_id uuid references public.blueprint_materials(id),
  created_at    timestamptz not null default now(),
  created_by    uuid
);

create index idx_blueprint_materials_project on public.blueprint_materials (project_id);
create index idx_blueprint_materials_truth on public.blueprint_materials (project_id, truth);
create index idx_blueprint_materials_supersedes on public.blueprint_materials (project_id, supersedes_id);

alter table public.blueprint_materials enable row level security;
create policy blueprint_materials_tenant_isolation on public.blueprint_materials
  for all using (suite_id = current_setting('app.current_suite_id')::uuid);

-- ──────────────────────────────────────────────────────────────────────────────
-- 6. blueprint_story
-- ──────────────────────────────────────────────────────────────────────────────
create table public.blueprint_story (
  id                  uuid primary key default gen_random_uuid(),
  suite_id            uuid not null,
  office_id           uuid,
  project_id          uuid not null references public.blueprint_projects(id),
  phase               int,
  markdown            text,
  truth_distribution  jsonb,
  supersedes_id       uuid references public.blueprint_story(id),
  created_at          timestamptz not null default now(),
  created_by          uuid
);

create index idx_blueprint_story_project on public.blueprint_story (project_id);
create index idx_blueprint_story_supersedes on public.blueprint_story (project_id, supersedes_id);

alter table public.blueprint_story enable row level security;
create policy blueprint_story_tenant_isolation on public.blueprint_story
  for all using (suite_id = current_setting('app.current_suite_id')::uuid);

-- ──────────────────────────────────────────────────────────────────────────────
-- 7. blueprint_missing_inputs
-- ──────────────────────────────────────────────────────────────────────────────
create table public.blueprint_missing_inputs (
  id                    uuid primary key default gen_random_uuid(),
  suite_id              uuid not null,
  office_id             uuid,
  project_id            uuid not null references public.blueprint_projects(id),
  description           text,
  suggested_resolution  text,
  resolved_by           uuid,
  resolved_at           timestamptz,
  created_at            timestamptz not null default now(),
  created_by            uuid
);

create index idx_blueprint_missing_inputs_project on public.blueprint_missing_inputs (project_id);

alter table public.blueprint_missing_inputs enable row level security;
create policy blueprint_missing_inputs_tenant_isolation on public.blueprint_missing_inputs
  for all using (suite_id = current_setting('app.current_suite_id')::uuid);

-- ──────────────────────────────────────────────────────────────────────────────
-- DOWN (manual; do not run automatically)
-- ──────────────────────────────────────────────────────────────────────────────
-- drop table if exists public.blueprint_missing_inputs;
-- drop table if exists public.blueprint_story;
-- drop table if exists public.blueprint_materials;
-- drop table if exists public.blueprint_assemblies;
-- drop table if exists public.blueprint_symbols;
-- drop table if exists public.blueprint_sheets;
-- drop table if exists public.blueprint_projects;
-- drop type if exists discipline;
-- drop type if exists tariff_flag;
-- drop type if exists truth_class;
