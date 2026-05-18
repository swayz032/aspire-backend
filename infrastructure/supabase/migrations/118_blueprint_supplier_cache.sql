-- =============================================================================
-- Migration 118: Wave 5.1a-3 — Blueprint Supplier Cache + Per-Project Credit Cap
-- =============================================================================
-- 24-hour TTL cache for Adam's MATERIAL_SUPPLIER_SEARCH Unwrangle results.
-- Credits are expensive (~10/call, 100-credit trial plan). A 50-line blueprint
-- without a cache would consume all 500 credits in a single pass.
--
-- Aspire Laws enforced:
--   Law #2 (Receipt for All)  — supplier_cache.py emits receipts on hit/miss/cap.
--   Law #3 (Fail Closed)      — RLS denies by default; explicit grants only.
--   Law #6 (Tenant Isolation) — suite_id on every row, RLS via app.current_suite_id
--                               session GUC (matches blueprint_engine pattern mig 117).
--   Law #9 (No PII)           — cache stores normalised product/supplier structs only.
--
-- References:
--   20260517000001_blueprint_engine.sql  — RLS pattern (app.current_suite_id)
--   101_service_brief_cache.sql          — neighbour cache migration for shape guidance
-- =============================================================================

-- =============================================================================
-- TABLE: public.blueprint_supplier_cache
-- =============================================================================
-- One row per (suite_id, cache_key). cache_key = SHA256(category||line_item_lower||zip).
-- expires_at enforces 24-hour TTL. UNIQUE(suite_id, cache_key) prevents duplicate
-- concurrent inserts; the upsert on-conflict strategy handles races.

create table if not exists public.blueprint_supplier_cache (
  id           uuid         primary key default gen_random_uuid(),
  suite_id     uuid         not null,
  cache_key    text         not null,  -- SHA256 hex of (category||line_item_normalised||office_zip)
  payload      jsonb        not null,
  source_apis  jsonb        not null default '[]'::jsonb,
  credits_used int          not null default 0,
  created_at   timestamptz  not null default now(),
  expires_at   timestamptz  not null,  -- created_at + 24h; enforced at query time
  unique (suite_id, cache_key)
);

comment on table public.blueprint_supplier_cache is
  'Wave 5.1a-3: 24-hour TTL cache for Adam MATERIAL_SUPPLIER_SEARCH Unwrangle results. '
  'Keyed by SHA256(category||line_item_lower||zip). Upsert-on-conflict prevents race duplication. '
  'RLS: suite_id via app.current_suite_id GUC (Law #6). '
  'Never stores PII — only normalised supplier/product structs (Law #9).';

comment on column public.blueprint_supplier_cache.cache_key is
  'SHA256 hex digest of (category || line_item_lower_stripped || office_zip_or_empty). '
  'Computed by supplier_cache.py:_cache_key().';

comment on column public.blueprint_supplier_cache.expires_at is
  '24-hour TTL from insertion time. Rows with expires_at < NOW() are treated as misses '
  'and overwritten on next fetch (upsert). No background cleanup required.';

-- Lookup index — the hot path: suite_id + cache_key + expiry filter
create index if not exists idx_blueprint_supplier_cache_lookup
  on public.blueprint_supplier_cache (suite_id, cache_key, expires_at);

-- =============================================================================
-- RLS — matches blueprint_engine pattern exactly (app.current_suite_id GUC)
-- =============================================================================
alter table public.blueprint_supplier_cache enable row level security;

create policy blueprint_supplier_cache_tenant_isolation
  on public.blueprint_supplier_cache
  for all
  using (suite_id = current_setting('app.current_suite_id')::uuid);

-- Service role bypass (internal writes from supplier_cache.py via service key)
create policy blueprint_supplier_cache_service_role
  on public.blueprint_supplier_cache
  for all to service_role
  using (true) with check (true);

grant select, insert, update on public.blueprint_supplier_cache to authenticated;
grant all on public.blueprint_supplier_cache to service_role;


-- =============================================================================
-- ALTER: blueprint_projects — per-project Unwrangle credit counter
-- =============================================================================
-- Tracks cumulative Unwrangle credits spent on a project. When this value reaches
-- ASPIRE_UNWRANGLE_PER_PROJECT_CAP (default 25), supplier_cache.py switches to
-- force_serpapi_only=True so only the free SerpAPI HD path is used.
-- No RLS change needed — row is scoped by blueprint_projects' existing policy.

alter table public.blueprint_projects
  add column if not exists unwrangle_credits_used int not null default 0;

comment on column public.blueprint_projects.unwrangle_credits_used is
  'Cumulative Unwrangle API credits consumed for this project. '
  'Incremented by supplier_cache.py on each non-cached Unwrangle call. '
  'When >= ASPIRE_UNWRANGLE_PER_PROJECT_CAP (default 25), supplier_cache.py '
  'calls fetch_fn(force_serpapi_only=True) and skips caching.';


-- =============================================================================
-- DOWN (commented; do not run automatically)
-- =============================================================================
-- alter table public.blueprint_projects drop column if exists unwrangle_credits_used;
-- drop policy if exists blueprint_supplier_cache_service_role on public.blueprint_supplier_cache;
-- drop policy if exists blueprint_supplier_cache_tenant_isolation on public.blueprint_supplier_cache;
-- drop index if exists idx_blueprint_supplier_cache_lookup;
-- drop table if exists public.blueprint_supplier_cache;
