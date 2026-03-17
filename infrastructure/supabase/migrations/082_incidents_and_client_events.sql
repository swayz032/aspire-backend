-- ============================================================
-- Migration 082: incidents + client_events (observability tables)
-- Unblocks the entire observability system / admin portal
-- Applied via Supabase MCP: 2026-03-17
-- ============================================================

-- =========================
-- 1. INCIDENTS TABLE
-- =========================
create table if not exists incidents (
  id uuid primary key default gen_random_uuid(),
  tenant_id text not null references tenants(tenant_id) on delete cascade,
  correlation_id text,
  severity text not null check (severity in ('critical','high','medium','low')),
  source text not null default 'backend' check (source in ('backend','desktop','desktop_provider','sre')),
  title text not null,
  description text,
  stack_trace text,
  component text,
  provider text,
  fingerprint text,
  status text not null default 'open' check (status in ('open','investigating','resolved','dismissed')),
  resolved_by text,
  tags jsonb not null default '{}'::jsonb,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Indexes
create index if not exists idx_incidents_tenant on incidents (tenant_id);
create index if not exists idx_incidents_severity_open on incidents (severity) where status = 'open';
create index if not exists idx_incidents_correlation on incidents (correlation_id) where correlation_id is not null;
create unique index if not exists idx_incidents_fingerprint_open on incidents (fingerprint) where fingerprint is not null and status = 'open';

-- updated_at trigger
create or replace function update_incidents_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_incidents_updated_at on incidents;
create trigger trg_incidents_updated_at
  before update on incidents
  for each row execute function update_incidents_updated_at();

-- RLS
alter table incidents enable row level security;

drop policy if exists incidents_select_members on incidents;
create policy incidents_select_members on incidents for select to authenticated
  using (app.is_member(tenant_id));

drop policy if exists incidents_insert_members on incidents;
create policy incidents_insert_members on incidents for insert to authenticated
  with check (app.is_member(tenant_id));

drop policy if exists incidents_update_admin on incidents;
create policy incidents_update_admin on incidents for update to authenticated
  using (app.is_admin_or_owner(tenant_id))
  with check (app.is_admin_or_owner(tenant_id));

drop policy if exists incidents_no_delete on incidents;
create policy incidents_no_delete on incidents for delete to authenticated
  using (false);

-- Service role bypass (backend inserts via service_role key)
drop policy if exists incidents_service_all on incidents;
create policy incidents_service_all on incidents for all to service_role
  using (true) with check (true);


-- =========================
-- 2. CLIENT_EVENTS TABLE
-- =========================
create table if not exists client_events (
  id uuid primary key default gen_random_uuid(),
  tenant_id text references tenants(tenant_id) on delete cascade,
  session_id text,
  correlation_id text,
  event_type text not null,
  source text default 'desktop' check (source in ('desktop','admin','mobile')),
  severity text default 'info',
  component text,
  page_route text,
  data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- Indexes
create index if not exists idx_client_events_tenant on client_events (tenant_id);
create index if not exists idx_client_events_type on client_events (event_type);
create index if not exists idx_client_events_correlation on client_events (correlation_id) where correlation_id is not null;

-- RLS
alter table client_events enable row level security;

drop policy if exists client_events_select_members on client_events;
create policy client_events_select_members on client_events for select to authenticated
  using (app.is_member(tenant_id));

drop policy if exists client_events_insert_anon on client_events;
create policy client_events_insert_anon on client_events for insert to authenticated
  with check (true);

drop policy if exists client_events_no_update on client_events;
create policy client_events_no_update on client_events for update to authenticated
  using (false);

drop policy if exists client_events_no_delete on client_events;
create policy client_events_no_delete on client_events for delete to authenticated
  using (false);

-- Service role bypass
drop policy if exists client_events_service_all on client_events;
create policy client_events_service_all on client_events for all to service_role
  using (true) with check (true);

-- Allow anonymous inserts (for telemetry without auth)
drop policy if exists client_events_insert_anon_role on client_events;
create policy client_events_insert_anon_role on client_events for insert to anon
  with check (true);
